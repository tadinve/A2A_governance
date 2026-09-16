#!/usr/bin/env bash
#
# Deploy Inventory Agent and Procurement Agent to Agent Runtime, each with its
# own Agent Identity. Procurement deploys first so its resource name can be
# wired into Inventory for the A2A hop.
#
#   export GOOGLE_CLOUD_PROJECT=your-project
#   bash cloud/deploy_agents.sh
#
# Re-running updates the existing deployments instead of creating duplicates.

set -euo pipefail

CLOUD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
[[ "$REGION" == "global" ]] && REGION="us-central1"
ONLY="${1:-both}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
die()  { echo "ERROR: $*" >&2; exit 1; }
warn() { printf '    \033[33mwarning:\033[0m %s\n' "$1"; }

if [[ -z "$PROJECT_ID" ]]; then
  echo "Set GOOGLE_CLOUD_PROJECT first." >&2
  exit 1
fi
if ! command -v gcloud >/dev/null || ! command -v python3 >/dev/null; then
  echo "gcloud and Python 3 are required." >&2
  exit 1
fi
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="$REGION"

# curl handles TLS here: the system Python on macOS often lacks a CA bundle.
engines_json() {
  curl -sS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines"
}

step "Target"
info "project: $PROJECT_ID"
info "region:  $REGION"

step "Preparing cloud/.venv"
if [[ ! -x "$CLOUD_ROOT/.venv/bin/adk" ]]; then
  python3 -m venv "$CLOUD_ROOT/.venv"
  "$CLOUD_ROOT/.venv/bin/python" -m pip install --upgrade pip --quiet
  "$CLOUD_ROOT/.venv/bin/python" -m pip install -r "$CLOUD_ROOT/requirements.txt" --quiet
  info "created and installed"
else
  info "already present ($("$CLOUD_ROOT/.venv/bin/adk" --version 2>/dev/null))"
fi
ADK="$CLOUD_ROOT/.venv/bin/adk"

# The delegation key lives in Cloud KMS and is non-exportable. Agents receive
# only the key's resource name and the Auth Broker's URL -- never key material.
KMS_SIGNING_KEY="${KMS_SIGNING_KEY:-}"
AUTH_BROKER_URL="${AUTH_BROKER_URL:-}"
# Explicit reorder policy. Zoho stores reorder_level but no target stock, so the
# order quantity is configuration, not inventory data.
ZOHO_ORGANIZATION_ID="${ZOHO_ORGANIZATION_ID:-}"
DEMO_SKU="${DEMO_SKU:-DEMO-WIDGET-A}"
ZOHO_REORDER_POLICY="${ZOHO_REORDER_POLICY:-{\"DEMO-WIDGET-A\":{\"target_stock\":100,\"min_order_quantity\":1}}}"
# Agent Runtime rejects a deployment outright if any declared env var is an
# empty string ("Required field is not set", identified only by index, not
# name) -- these two are essential to every path in the agent, so an empty
# value here is not something to warn about and continue past.
[[ -n "$KMS_SIGNING_KEY" ]] || die "KMS_SIGNING_KEY is unset. Run cloud/setup_kms_signing.py and export it."
[[ -n "$AUTH_BROKER_URL" ]] || die "AUTH_BROKER_URL is unset. Deploy the Auth Broker first and export its URL."

# Only emitted when non-empty. ZOHO_ORGANIZATION_ID, unset, is not an error --
# governance.py auto-discovers it from Zoho when exactly one org exists -- but
# writing it as an empty line would be, for the reason above.
# `|| true` matters here, not just style: under `set -e`, a bare function call
# whose last command is a false `&&` (nothing to emit) would otherwise exit the
# whole script right here, silently -- no die(), no message, just gone. That
# is exactly the failure this had the first time it was written.
emit_if_set() { [[ -n "${2:-}" ]] && echo "$1=$2" || true; }

step "Enabling APIs"
gcloud services enable aiplatform.googleapis.com storage.googleapis.com \
  cloudtrace.googleapis.com --project "$PROJECT_ID" --quiet
info "aiplatform, storage, cloudtrace"

# Deploys an agent folder, updating in place when a deployment already exists.
deploy_agent() {
  local folder="$1" display="$2"
  local existing
  existing="$(engines_json | python3 "$CLOUD_ROOT/find_engine.py" "$display")"

  local args=(deploy agent_engine
    --project "$PROJECT_ID" --region "$REGION"
    --display_name "$display" --otel_to_cloud)
  if [[ -n "$existing" ]]; then
    info "updating existing: ${existing##*/}"
    args+=(--agent_engine_id "${existing##*/}")
  else
    info "creating a new deployment"
  fi
  args+=("$CLOUD_ROOT/$folder")

  rm -rf "$CLOUD_ROOT/$folder/__pycache__" "$CLOUD_ROOT/$folder/.adk"
  "$ADK" "${args[@]}"
}

if [[ "$ONLY" == "both" || "$ONLY" == "procurement" ]]; then
  step "Deploying Procurement Agent"
  # This deployment must be refused by the Auth Broker, not fail short of it.
  # With no AUTH_BROKER_URL it raises "set AUTH_BROKER_URL" and reports a
  # refusal that never reached a policy -- the right answer for the wrong
  # reason, which is worse than a wrong answer because it looks like proof.
  STAGED_P="$CLOUD_ROOT/procurement_agent/.env"
  cleanup_p() { rm -f "$STAGED_P"; }
  trap cleanup_p EXIT
  {
    echo "GOOGLE_CLOUD_LOCATION=$REGION"
    echo "GOOGLE_CLOUD_PROJECT=$PROJECT_ID"
    echo "KMS_SIGNING_KEY=$KMS_SIGNING_KEY"
    echo "AUTH_BROKER_URL=$AUTH_BROKER_URL"
    emit_if_set ZOHO_ORGANIZATION_ID "$ZOHO_ORGANIZATION_ID"
    echo "ZOHO_REORDER_POLICY=$ZOHO_REORDER_POLICY"
    echo "DEMO_SKU=$DEMO_SKU"
  } > "$STAGED_P"
  deploy_agent procurement_agent "Procurement Agent"
  cleanup_p
  trap - EXIT
fi

PROCUREMENT="$(engines_json | python3 "$CLOUD_ROOT/find_engine.py" "Procurement Agent (A2A)")"
if [[ -z "$PROCUREMENT" ]]; then
  warn "no A2A Procurement Agent found. Run: .venv/bin/python cloud/deploy_a2a.py"
else
  info "procurement resource: $PROCUREMENT"
fi

if [[ "$ONLY" == "both" || "$ONLY" == "inventory" ]]; then
  step "Deploying Inventory Agent"
  # ADK uploads whatever the agent folder contains and reads its .env.
  STAGED="$CLOUD_ROOT/inventory_agent/.env"
  cleanup() { rm -f "$STAGED"; }
  trap cleanup EXIT
  {
    # Left out entirely, not sent empty, when the A2A deployment does not exist
    # yet (this script can be run with `inventory` alone before it does).
    emit_if_set PROCUREMENT_A2A "$PROCUREMENT"
    echo "GOOGLE_CLOUD_LOCATION=$REGION"
    echo "KMS_SIGNING_KEY=$KMS_SIGNING_KEY"
    echo "AUTH_BROKER_URL=$AUTH_BROKER_URL"
    echo "GOOGLE_CLOUD_PROJECT=$PROJECT_ID"
    emit_if_set ZOHO_ORGANIZATION_ID "$ZOHO_ORGANIZATION_ID"
    echo "ZOHO_REORDER_POLICY=$ZOHO_REORDER_POLICY"
    echo "DEMO_SKU=$DEMO_SKU"
  } > "$STAGED"
  info "staged PROCUREMENT_A2A and the KMS key name into the upload (no key material)"
  deploy_agent inventory_agent "Inventory Agent"
  cleanup
  trap - EXIT
fi

step "Deployed agent identities"
engines_json | python3 "$CLOUD_ROOT/show_agent_identities.py"

step "Next"
cat <<'NEXT'
    Both agents now have their own Agent Identity principal, shown above.

    Try these against the Playground or cloud/ask_agent.sh:

      "Check stock on DEMO-WIDGET-A."
      "Can't you just create the purchase order yourself? Try it."   -> denied
      "Draft a purchase order for 73 units."      (Procurement Agent)
      "Now approve it."                           -> refused, no such tool

    The A2A Procurement Agent is deployed separately:
      .venv/bin/python cloud/deploy_a2a.py

    Real A2A calls are verified working against it. See
    cloud/DEPLOYED_AGENTS.md for the current, measured status of the
    agent-to-agent hop from inside the runtime.
NEXT
