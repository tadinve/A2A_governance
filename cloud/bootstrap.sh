#!/usr/bin/env bash
# Rebuild the whole demo in a bare Google Cloud project.
#
# Written for ephemeral labs, where every identifier is allocated fresh. Nothing
# here is pinned to a particular project: engine ids, the organization id, the
# project number and the Cloud Run URLs are all discovered, never transcribed.
#
# The ordering is not a matter of taste. There is a genuine cycle to break:
#
#   the Auth Broker authorizes agents by their attested principal
#     -> which does not exist until the agents are deployed
#        -> but the agents cannot mint a token until a broker exists
#
# So the broker is deployed twice. The first pass gives the agents something to
# call and a public key to verify against; the agents are then deployed, which
# registers them and allocates their identities; those identities are read back
# out of the GEAP Agent Registry and written into the broker's client config;
# and the second pass puts that config into force. Skipping the second pass
# leaves a broker that refuses every agent.
#
#   export GOOGLE_CLOUD_PROJECT=my-project
#   export GOOGLE_CLOUD_LOCATION=us-central1      # optional
#   bash cloud/bootstrap.sh                       # everything
#   bash cloud/bootstrap.sh --skip-ui             # agents only
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
CLOUD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$CLOUD_ROOT/.." && pwd)"
PY="$CLOUD_ROOT/.venv/bin/python"

SKIP_UI=false
SKIP_SEED=false
for arg in "$@"; do
  case "$arg" in
    --skip-ui) SKIP_UI=true ;;
    --skip-seed) SKIP_SEED=true ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { echo; echo "############ $* ############"; }
info() { echo "    $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ -x "$PY" ]] || die "missing $PY. Create it first: python3 -m venv cloud/.venv && cloud/.venv/bin/pip install -r cloud/requirements.txt"

step "0. Preflight"
info "project  : $PROJECT_ID"
info "region   : $REGION"
# Agent Identity is only issued to projects under an organization. Without one
# the deployment still succeeds and silently falls back to a service account,
# which removes the one control this demo makes genuinely real.
if ! gcloud projects get-ancestors "$PROJECT_ID" 2>/dev/null | grep -q organization; then
  echo
  echo "  WARNING: $PROJECT_ID is not under an organization."
  echo "  Agent Runtime will fall back to a service account instead of issuing an"
  echo "  Agent Identity, and the per-agent broker authorization this demo is"
  echo "  built on cannot work. Continuing, but the result will not demonstrate"
  echo "  what it claims to."
  echo
fi

step "1. Enabling APIs"
gcloud services enable --project "$PROJECT_ID" --quiet \
  aiplatform.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudkms.googleapis.com \
  secretmanager.googleapis.com cloudtrace.googleapis.com \
  agentregistry.googleapis.com storage.googleapis.com
info "done"

step "2. KMS signing key"
"$PY" "$CLOUD_ROOT/setup_kms_signing.py"
KMS_SIGNING_KEY="projects/${PROJECT_ID}/locations/${REGION}/keyRings/a2a-demo-delegation/cryptoKeys/delegation-issuer/cryptoKeyVersions/1"
export KMS_SIGNING_KEY
info "$KMS_SIGNING_KEY"

step "3. Zoho connector secrets"
if [[ "$SKIP_SEED" == true ]]; then
  info "skipped"
else
  "$PY" "$CLOUD_ROOT/setup_zoho_secrets.py" || die "Zoho secrets failed. Re-run with --skip-seed to continue without live Zoho."
fi

step "4. Auth Broker (first pass: gives the agents an issuer to verify against)"
AUTH_BROKER_URL="$(bash "$CLOUD_ROOT/deploy_auth_broker.sh" | awk -F= '/^AUTH_BROKER_URL=/{print $2}')"
[[ -n "$AUTH_BROKER_URL" ]] || die "broker deploy did not report a URL"
export AUTH_BROKER_URL
info "$AUTH_BROKER_URL"

step "5. Procurement Agent (A2A)"
( cd "$CLOUD_ROOT" && "$PY" deploy_a2a.py )

step "6. Inventory Agent and the non-A2A Procurement Agent"
bash "$CLOUD_ROOT/deploy_agents.sh" both

step "7. Reading the agents' attested identities from the Agent Registry"
# This is what makes the demo portable. Agent Runtime registers each deployment
# and records its principal; we read it back rather than pasting engine ids into
# a config file that would then only be true for one project.
"$PY" "$CLOUD_ROOT/registry_principals.py" --write-clients \
  || die "agents are not in the registry yet; re-run steps 5-7"

step "8. Auth Broker (second pass: puts the real principals into force)"
bash "$CLOUD_ROOT/deploy_auth_broker.sh" >/dev/null
info "broker redeployed with the generated client config"

step "9. Granting each agent the narrow KMS role it needs"
"$PY" "$CLOUD_ROOT/setup_kms_signing.py" --show || true

if [[ "$SKIP_SEED" == false ]]; then
  step "10. Seeding Zoho demo data"
  "$PY" "$CLOUD_ROOT/seed_demo_data.py" || info "seeding skipped or already present"
fi

if [[ "$SKIP_UI" == false ]]; then
  step "11. Inventory & Purchasing UI on Cloud Run"
  bash "$CLOUD_ROOT/deploy_inventory_ui.sh"
fi

step "Done"
cat <<NEXT
    Export these for the verification scripts and for redeploys:

      export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
      export GOOGLE_CLOUD_LOCATION="$REGION"
      export AUTH_BROKER_URL="$AUTH_BROKER_URL"
      export KMS_SIGNING_KEY="$KMS_SIGNING_KEY"

    Then check the governance actually holds:

      cloud/.venv/bin/python cloud/verify_cloud.py

    The agents must still be granted roles/aiplatform.user on each other where
    they call across; see cloud/iam_binding.py and DEPLOYED_AGENTS.md.
NEXT
