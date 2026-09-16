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
#   bash deploy_to_gcp.sh PROJECT_ID [options]
#
#     --region REGION   default us-central1
#     --skip-ui         agents and control plane only, no Cloud Run UI
#     --skip-seed       do not touch Zoho: no connector secrets, no demo data
#     --yes             do not ask for confirmation
#
#   bash deploy_to_gcp.sh my-lab-project
#   bash deploy_to_gcp.sh my-lab-project --skip-ui --region europe-west4
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
CLOUD_ROOT="$REPO_ROOT/cloud"
PY="$CLOUD_ROOT/.venv/bin/python"

usage() {
  # Print the header comment block and stop at the first line of code, so the
  # help text cannot drift out of step with the script as it grows.
  awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]:-$0}"
  exit "${1:-0}"
}

PROJECT_ID=""
REGION=""
SKIP_UI=false
SKIP_SEED=false
ASSUME_YES=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    --skip-ui) SKIP_UI=true; shift ;;
    --skip-seed) SKIP_SEED=true; shift ;;
    --yes|-y) ASSUME_YES=true; shift ;;
    -*) echo "unknown option: $1" >&2; usage 2 ;;
    *)
      [[ -z "$PROJECT_ID" ]] || { echo "unexpected argument: $1" >&2; usage 2; }
      PROJECT_ID="$1"; shift ;;
  esac
done

# The project is the one thing this script will not guess. Falling back to the
# active gcloud config would make it possible to rebuild the wrong project by
# running the command in the wrong shell, and every step here is a write.
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: no project given." >&2
  echo >&2
  usage 2
fi
REGION="${REGION:-${GOOGLE_CLOUD_LOCATION:-us-central1}}"

# Everything downstream reads these, so set them from the argument rather than
# inheriting whatever happened to be exported.
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="$REGION"

step() { echo; echo "############ $* ############"; }
info() { echo "    $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ -x "$PY" ]] || die "missing $PY.
  The deploy tooling runs from its own virtualenv, separate from the local demo's:
    python3 -m venv cloud/.venv && cloud/.venv/bin/pip install -r cloud/requirements.txt"

step "0. Preflight"
command -v gcloud >/dev/null || die "gcloud is not on PATH"

# Two separate credential stores, and a fresh machine (or a fresh Qwiklabs
# Cloud Shell) commonly has neither: the gcloud CLI's own login, which every
# plain `gcloud ...` call in this script uses, and Application Default
# Credentials, which the Python tooling reads via google.auth.default() and
# which `gcloud builds submit`/`gcloud run deploy` also rely on. Missing either
# one fails confusingly deep into the run rather than here, so both are
# checked -- and bootstrapped -- before anything else.
#
# Both `gcloud auth login` and `gcloud auth application-default login` are
# interactive: they print a URL, open a browser if one is available, and block
# until you finish there. That is expected. Nothing runs before you do.
#
# `gcloud auth list --filter=status:ACTIVE` only reports what the local config
# *labels* active -- it says nothing about whether that account's cached token
# still works. On Qwiklabs the same student-NN@qwiklabs.net address is reissued
# lab after lab, so a stale entry from a torn-down previous lab reads as
# "active" right up until the first real call, which then fails with
# `invalid_grant: Account has been deleted`. So the check here is not "is an
# account configured" but "does calling out actually work" -- print-access-token
# forces a refresh, which is exactly the call that fails first.
if ! gcloud auth print-access-token >/dev/null 2>&1; then
  echo
  echo "    gcloud has no working credentials (none configured, or a stale"
  echo "    account from a previous lab). Opening the login flow:"
  gcloud auth login || die "gcloud auth login did not complete"
  gcloud auth print-access-token >/dev/null 2>&1 \
    || die "still no working gcloud credentials after login"
fi
info "gcloud account: $(gcloud config get-value account 2>/dev/null)"

# Same failure mode applies to Application Default Credentials, and the two are
# independent: this checks it for real rather than trusting a stale label too.
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo
  echo "    No working Application Default Credentials. Opening that login flow:"
  echo "    (a separate step from the one above -- this is what the Python"
  echo "     tooling and the Cloud Build/Cloud Run API calls actually use)"
  gcloud auth application-default login || die "gcloud auth application-default login did not complete"
  gcloud auth application-default print-access-token >/dev/null 2>&1 \
    || die "still no working Application Default Credentials after login"
fi
info "application default credentials present"

# Silences "does not match the quota project" and avoids a real quota_exceeded
# error later: without this every ADC-based call bills against whatever project
# happened to be active the last time someone ran `gcloud auth
# application-default login` on this machine, which after a lab rebuild is a
# project that may no longer exist.
gcloud auth application-default set-quota-project "$PROJECT_ID" --quiet >/dev/null 2>&1 || true

gcloud config set project "$PROJECT_ID" --quiet \
  || die "gcloud config set project failed even after a working login -- rerun this script"
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1 \
  || die "cannot reach project '$PROJECT_ID'. Check the id and that this account can access it."
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
info "project  : $PROJECT_ID ($PROJECT_NUMBER)"
info "region   : $REGION"
info "account  : $(gcloud config get-value account 2>/dev/null)"
info "ui       : $([[ "$SKIP_UI" == true ]] && echo 'skipped' || echo 'Cloud Run')"
info "zoho     : $([[ "$SKIP_SEED" == true ]] && echo 'skipped' || echo 'secrets + demo data')"

# This creates billable resources, grants IAM, and writes to Zoho. Worth one
# deliberate keystroke, because the project id is the whole blast radius.
if [[ "$ASSUME_YES" != true ]]; then
  echo
  echo "    This will create a KMS key, service accounts, IAM bindings, Cloud Run"
  echo "    services and Agent Runtime deployments in '$PROJECT_ID'."
  [[ "$SKIP_SEED" == true ]] || echo "    It will also write demo data to the configured Zoho organization."
  echo
  read -r -p "    Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]] || { echo "    aborted"; exit 1; }
fi
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
# A brand new account can hit UREQ_TOS_NOT_ACCEPTED here: Google requires a
# one-time, click-through acceptance of the Cloud Terms of Service before any
# API can be enabled, and there is deliberately no API to accept it -- it is a
# legal acknowledgement, not a permission, so no script (this one included) can
# do this step for you. Caught here and turned into one clear instruction
# instead of the raw multi-paragraph error, because it otherwise reads like a
# permissions bug and sends people down the wrong troubleshooting path.
ENABLE_OUTPUT="$(gcloud services enable --project "$PROJECT_ID" --quiet \
  aiplatform.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudkms.googleapis.com \
  secretmanager.googleapis.com cloudtrace.googleapis.com \
  agentregistry.googleapis.com storage.googleapis.com 2>&1)" && ENABLE_STATUS=0 || ENABLE_STATUS=$?
if [[ $ENABLE_STATUS -ne 0 ]]; then
  if grep -q "UREQ_TOS_NOT_ACCEPTED" <<<"$ENABLE_OUTPUT"; then
    die "The Cloud Terms of Service have not been accepted for $(gcloud config get-value account 2>/dev/null).
  This is a one-time, per-account click-through; nothing can do it on your behalf.
  Fix: sign in to https://console.cloud.google.com as that account, accept the
  terms on the screen that appears, then re-run this script."
  fi
  echo "$ENABLE_OUTPUT" >&2
  die "gcloud services enable failed"
fi
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
  # setup_zoho_secrets.py reads the MCP endpoint URLs from the environment; it
  # never hardcodes them and never receives them as an argument, so they must
  # already be exported when this runs. .env_zoho_urls is untracked (it holds
  # the real connector URLs) and is not read by anything automatically.
  if [[ -z "${ZOHO_INVREAD_MCP_URL:-}" || -z "${ZOHO_PROCUREWRITE_MCP_URL:-}" ]]; then
    if [[ -f "$REPO_ROOT/.env_zoho_urls" ]]; then
      set -a; source "$REPO_ROOT/.env_zoho_urls"; set +a
      info "loaded $REPO_ROOT/.env_zoho_urls"
    else
      die "ZOHO_INVREAD_MCP_URL / ZOHO_PROCUREWRITE_MCP_URL are not set and no
  .env_zoho_urls was found at the repo root. Export them yourself, or place
  that file, or pass --skip-seed to deploy without live Zoho."
    fi
  fi
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
# NOT --show: --show only *displays* the current bindings, it grants nothing.
# Running only the display form here meant this step looked like it had done
# its job (it printed something, exit 0) while every agent stayed unable to
# verify a token it had just been handed -- decode_token() needs
# roles/cloudkms.publicKeyViewer to read the key it checks the signature
# against, and this is the step that grants it, now that the agents (and
# hence their principals) actually exist.
"$PY" "$CLOUD_ROOT/setup_kms_signing.py" \
  || die "could not grant roles/cloudkms.publicKeyViewer to the deployed agents"

step "9b. Letting Inventory Agent invoke Procurement Agent (A2A)"
# Agent Identity carries roles/aiplatform.agentDefaultAccess only, which does
# NOT include aiplatform.reasoningEngines.query -- the permission the A2A call
# actually needs. Without this grant every reorder fails at IAM with a 403 that
# has nothing to do with the delegation policy above it, which is confusing to
# debug the first time. Granted on the target resource, never the project: see
# the warning in iam_binding.py about setIamPolicy replacing the whole policy.
INVENTORY_PRINCIPAL="$("$PY" "$CLOUD_ROOT/registry_principals.py" --print-principal "Inventory Agent" 2>/dev/null || true)"
A2A_RESOURCE="$("$PY" - "$PROJECT_ID" "$REGION" <<'PYEOF'
import sys
import google.auth, google.auth.transport.requests
project, region = sys.argv[1], sys.argv[2]
creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
session = google.auth.transport.requests.AuthorizedSession(creds)
url = f"https://{region}-aiplatform.googleapis.com/v1beta1/projects/{project}/locations/{region}/reasoningEngines"
for engine in session.get(url, timeout=60).json().get("reasoningEngines", []):
    if engine.get("displayName") == "Procurement Agent (A2A)":
        print(engine["name"])
        break
PYEOF
)"
if [[ -n "$INVENTORY_PRINCIPAL" && -n "$A2A_RESOURCE" ]]; then
  "$PY" "$CLOUD_ROOT/iam_binding.py" --resource "$A2A_RESOURCE" \
    --member "$INVENTORY_PRINCIPAL" --role roles/aiplatform.user
  info "granted on ${A2A_RESOURCE##*/}"
  info "IAM changes take a few minutes to propagate; an early call may still 403"
else
  info "WARNING: could not resolve principal/resource; grant this by hand -- see"
  info "  cloud/iam_binding.py and the 'authentication is not authorization' demo"
  info "  in cloud/DEPLOYED_AGENTS.md"
fi

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
