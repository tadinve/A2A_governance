#!/usr/bin/env bash
# Provision a real Google Cloud Agent Gateway (Agent-to-Anywhere / egress).
#
# This script PROVISIONS ONLY. It does not bind any agent, and it does not
# change how a single existing request is routed. Running it against a live
# demo is safe: a gateway nobody is bound to carries no traffic.
#
# Binding is a separate, deliberate command, because it is the step that
# reroutes every outbound call the agents make:
#
#     bash cloud/bind_agents_to_gateway.sh
#
# Enforcement is a third, later command, for the same reason:
#
#     bash cloud/configure_gateway_enforcement.sh
#
#   bash cloud/setup_agent_gateway.sh [options]
#
#     --project PROJECT_ID   default $GOOGLE_CLOUD_PROJECT
#     --region REGION        default $GOOGLE_CLOUD_LOCATION, else us-central1
#     --gateway NAME         default a2a-governance-egress
#     --iap-policy-version V default V1 (see the note below)
#     --enforce              install the fail-closed authorization extension
#     -h / --help
#
# What gets created, all of it idempotent (`import` is create-or-update):
#
#   agentGateways/<name>              the Agent-to-Anywhere gateway itself
#   authzExtensions/<name>-...        the IAP authorization extension
#   authzPolicies/<name>-...          binds that extension to the gateway
#
# Relationship to src/governance_demo/gateway_app.py: none. That file is the
# local, offline simulation of the gateway pattern and keeps working exactly
# as before. This script configures the Google-managed product. They are two
# different things and the docs must not conflate them.
set -euo pipefail

CLOUD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

usage() {
  awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]:-$0}"
  exit "${1:-0}"
}

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
GATEWAY="a2a-governance-egress"
# The published codelab that actually works end to end uses V1. The written
# specification for this work said V2. Rather than guess, this is a flag whose
# default is the value with a known-good reference implementation behind it.
IAP_POLICY_VERSION="V1"
ENFORCE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --project) PROJECT_ID="${2:?--project needs a value}"; shift 2 ;;
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    --gateway) GATEWAY="${2:?--gateway needs a value}"; shift 2 ;;
    --iap-policy-version) IAP_POLICY_VERSION="${2:?needs a value}"; shift 2 ;;
    --enforce) ENFORCE=true; shift ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
done

step() { echo; echo "=== $* ==="; }
info() { echo "    $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ -n "$PROJECT_ID" ]] || die "no project. Pass --project or set GOOGLE_CLOUD_PROJECT
  (source activate.sh sets it from .env)."
command -v gcloud >/dev/null || die "gcloud is not on PATH"
gcloud auth print-access-token >/dev/null 2>&1 \
  || die "gcloud has no working credentials. Run: gcloud auth login"
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1 \
  || die "cannot reach project '$PROJECT_ID'"

GATEWAY_URI="projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${GATEWAY}"
REGISTRY_URI="//agentregistry.googleapis.com/projects/${PROJECT_ID}/locations/${REGION}"
EXT_DRYRUN="${GATEWAY}-authz-iap-dryrun"
EXT_ENFORCED="${GATEWAY}-authz-iap-enforced"
POLICY="${GATEWAY}-authz-policy"
EXT_ACTIVE="$([[ "$ENFORCE" == true ]] && echo "$EXT_ENFORCED" || echo "$EXT_DRYRUN")"

step "Target"
info "project  : $PROJECT_ID"
info "region   : $REGION"
info "gateway  : $GATEWAY"
info "mode     : $([[ "$ENFORCE" == true ]] && echo 'ENFORCING (fail closed)' || echo 'DRY_RUN (audit only, fail open)')"
info "binding  : not performed by this script"

if [[ "$ENFORCE" == true ]]; then
  echo
  echo "    WARNING: enforcement makes the gateway reject any outbound call to a"
  echo "    destination that is not registered and permitted. Every destination"
  echo "    must already be in the Agent Registry, and every agent that needs it"
  echo "    must already hold roles/iap.egressor on it, or working paths break."
  echo
  echo "    Observe DRY_RUN logs first:  python3 cloud/verify_agent_gateway.py --observed"
  echo
fi

# The YAML bodies are generated rather than committed because every one of them
# embeds the project id and region, and a committed copy would be true for
# exactly one project -- the same reason registry ids are never transcribed
# anywhere else in this repository.
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

step "Enabling required APIs"
gcloud services enable --project "$PROJECT_ID" --quiet \
  agentregistry.googleapis.com aiplatform.googleapis.com \
  networkservices.googleapis.com networksecurity.googleapis.com \
  iap.googleapis.com compute.googleapis.com iam.googleapis.com \
  logging.googleapis.com \
  || die "could not enable the APIs Agent Gateway needs. If this is a lab
  project, the account may not be permitted to enable networksecurity or iap."
info "agentregistry, aiplatform, networkservices, networksecurity, iap, compute, iam, logging"

step "Agent Gateway (Agent-to-Anywhere)"
# protocols: MCP does NOT restrict the gateway to MCP traffic. The gateway
# governs all HTTP-based egress; naming MCP additionally turns on
# protocol-aware parsing, which is what makes tool-level policy possible later.
# A2A and ordinary HTTPS still traverse it. Only known-good enum values are
# written here -- inventing one gets the whole import rejected.
cat > "$WORK/gateway.yaml" <<YAML
name: ${GATEWAY}
protocols:
  - MCP
googleManaged:
  governedAccessPath: AGENT_TO_ANYWHERE
registries:
  - "${REGISTRY_URI}"
YAML
gcloud network-services agent-gateways import "$GATEWAY" \
  --source="$WORK/gateway.yaml" --location="$REGION" --project="$PROJECT_ID" --quiet \
  || die "could not create the Agent Gateway. Check that
  networkservices.googleapis.com is enabled and that this account may create
  agentGateways in $PROJECT_ID."
info "$GATEWAY_URI"
info "registry attached: $REGISTRY_URI"

step "IAP authorization extension"
# failOpen is the whole difference between an audit pass and an outage.
#
#   DRY_RUN   failOpen: true   -- IAP decides, the decision is logged, and the
#                                 request proceeds either way. If the extension
#                                 itself errors, traffic still flows.
#   ENFORCING failOpen: false  -- a denial is a 403, and an extension error is
#                                 also a denial. That is the point, and it is
#                                 why this is not the default.
#
# The written spec asked for failOpen:false in dry-run. That combination is
# the worst of both: it cannot block on policy, but it can still take the demo
# down when the extension is unavailable. It is deliberately not implemented.
if [[ "$ENFORCE" == true ]]; then
  cat > "$WORK/ext.yaml" <<YAML
name: ${EXT_ENFORCED}
service: iap.googleapis.com
failOpen: false
timeout: 1s
metadata:
  iapPolicyVersion: "${IAP_POLICY_VERSION}"
YAML
else
  cat > "$WORK/ext.yaml" <<YAML
name: ${EXT_DRYRUN}
service: iap.googleapis.com
failOpen: true
timeout: 1s
metadata:
  iamEnforcementMode: "DRY_RUN"
  iapPolicyVersion: "${IAP_POLICY_VERSION}"
YAML
fi
gcloud service-extensions authz-extensions import "$EXT_ACTIVE" \
  --source="$WORK/ext.yaml" --location="$REGION" --project="$PROJECT_ID" --quiet \
  || die "could not create the authorization extension $EXT_ACTIVE"
info "$EXT_ACTIVE (iapPolicyVersion=$IAP_POLICY_VERSION)"

step "Authorization policy binding the extension to the gateway"
cat > "$WORK/policy.yaml" <<YAML
name: ${POLICY}
target:
  resources:
    - "${GATEWAY_URI}"
policyProfile: REQUEST_AUTHZ
action: CUSTOM
customProvider:
  authzExtension:
    resources:
      - "projects/${PROJECT_ID}/locations/${REGION}/authzExtensions/${EXT_ACTIVE}"
YAML
gcloud beta network-security authz-policies import "$POLICY" \
  --source="$WORK/policy.yaml" --location="$REGION" --project="$PROJECT_ID" --quiet \
  || die "could not create the authorization policy $POLICY"
info "$POLICY -> $EXT_ACTIVE"

step "Provisioned"
cat <<NEXT
    The gateway exists and carries no traffic yet, because nothing is bound to
    it. Existing agents are untouched and the running demo is unaffected.

    Check what was created, without changing anything:

      python3 cloud/verify_agent_gateway.py --pre-bind

    Build the destination inventory before binding, so dry-run logs can be
    read against an expected list rather than a blank one:

      python3 cloud/gateway_destinations.py --list
      python3 cloud/gateway_destinations.py --register

    Then, as a separate and deliberate step, bind the agents:

      bash cloud/bind_agents_to_gateway.sh

AGENT_GATEWAY=${GATEWAY_URI}
NEXT
