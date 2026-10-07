#!/usr/bin/env bash
# Bind the deployed agents to the Agent Gateway. THIS CHANGES LIVE ROUTING.
#
# Separate from setup_agent_gateway.sh on purpose. Provisioning a gateway is
# inert; binding is not. Once an agent is bound, *every* outbound call it makes
# is proxied by the gateway -- not only MCP and A2A, but its Gemini model calls,
# Secret Manager, KMS, and the Auth Broker. Binding must therefore never be a
# side effect of running a setup script.
#
#   bash cloud/bind_agents_to_gateway.sh [options]
#
#     --project PROJECT_ID   default $GOOGLE_CLOUD_PROJECT
#     --region REGION        default $GOOGLE_CLOUD_LOCATION, else us-central1
#     --gateway NAME         default a2a-governance-egress
#     --agent "Display Name" bind only this one; repeatable
#     --unbind               remove the gateway binding again
#     --yes                  skip the confirmation prompt
#     -h / --help
#
# Why PATCH and not redeploy: Google documents an explicit PATCH of
# spec.deploymentSpec.agentGatewayConfig. The documented caveat is that PATCH
# does not change identity_type -- an agent that was deployed without
# AGENT_IDENTITY cannot acquire it this way. Every agent here already has
# AGENT_IDENTITY (see each package's .agent_engine_config.json), so the caveat
# does not apply and no redeploy is needed. This script verifies that
# precondition per agent and refuses rather than silently binding an agent that
# would end up on a service-account fallback.
#
# Rollback is --unbind, which clears the same field. It is a routing change,
# not a destructive one: no agent is redeployed and no identity changes.
set -euo pipefail

CLOUD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

usage() {
  awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]:-$0}"
  exit "${1:-0}"
}

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
GATEWAY="a2a-governance-egress"
UNBIND=false
ASSUME_YES=false
AGENTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --project) PROJECT_ID="${2:?--project needs a value}"; shift 2 ;;
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    --gateway) GATEWAY="${2:?--gateway needs a value}"; shift 2 ;;
    --agent) AGENTS+=("${2:?--agent needs a value}"); shift 2 ;;
    --unbind) UNBIND=true; shift ;;
    --yes|-y) ASSUME_YES=true; shift ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
done

# All three deployments, unless --agent narrowed it. Note that Agent Runtime
# requires every agent in a project and region to use the *same* egress
# gateway, so a partial binding is a transitional state, not a destination.
if [[ ${#AGENTS[@]} -eq 0 ]]; then
  AGENTS=("Inventory Agent" "Procurement Agent" "Procurement Agent (A2A)")
fi

step() { echo; echo "=== $* ==="; }
info() { echo "    $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ -n "$PROJECT_ID" ]] || die "no project. Pass --project or set GOOGLE_CLOUD_PROJECT."
command -v gcloud >/dev/null || die "gcloud is not on PATH"
command -v python3 >/dev/null || die "python3 is not on PATH"

GATEWAY_URI="projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${GATEWAY}"
HOST="https://${REGION}-aiplatform.googleapis.com/v1"
TOKEN="$(gcloud auth print-access-token)" || die "no gcloud credentials"

engines_json() {
  curl -sS -H "Authorization: Bearer ${TOKEN}" \
    "https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines"
}

step "Target"
info "project : $PROJECT_ID"
info "region  : $REGION"
info "gateway : $GATEWAY"
info "action  : $([[ "$UNBIND" == true ]] && echo 'UNBIND (clear the binding)' || echo 'BIND (route egress through the gateway)')"
for name in "${AGENTS[@]}"; do info "agent   : $name"; done

if [[ "$UNBIND" != true ]]; then
  gcloud network-services agent-gateways describe "$GATEWAY" \
    --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1 \
    || die "gateway $GATEWAY does not exist. Run cloud/setup_agent_gateway.sh first."
  info "gateway exists"
fi

if [[ "$ASSUME_YES" != true ]]; then
  echo
  if [[ "$UNBIND" == true ]]; then
    echo "    This removes the gateway from the agents above. Their outbound"
    echo "    traffic returns to the direct path."
  else
    echo "    This routes ALL outbound traffic from the agents above through the"
    echo "    gateway: Gemini calls, Secret Manager, KMS, the Auth Broker, Zoho,"
    echo "    and the A2A hop. In DRY_RUN nothing is blocked, but the path"
    echo "    changes. Re-run with --unbind to reverse it."
  fi
  echo
  read -r -p "    Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]] || { echo "    aborted"; exit 1; }
fi

# Resolve display name -> resource id, and read back effectiveIdentity in the
# same pass, so the Agent Identity precondition is checked against what the
# platform currently reports rather than what a config file intended.
RESOLVED="$(engines_json | python3 -c '
import json, sys
wanted = set(sys.argv[1:])
payload = json.load(sys.stdin)
for engine in payload.get("reasoningEngines", []):
    name = engine.get("displayName")
    if name not in wanted:
        continue
    identity = (engine.get("spec") or {}).get("effectiveIdentity") or ""
    bound = (((engine.get("spec") or {}).get("deploymentSpec") or {})
             .get("agentGatewayConfig") or {})
    current = ((bound.get("agentToAnywhereConfig") or {}).get("agentGateway") or "")
    print("\t".join([name, engine["name"], identity, current]))
' "${AGENTS[@]}")"

[[ -n "$RESOLVED" ]] || die "none of the requested agents are deployed in
  $PROJECT_ID/$REGION. Run deploy_to_gcp.sh first."

step "Patching spec.deploymentSpec.agentGatewayConfig"
FAILED=0
# Counted so the closing message can tell "I changed routing" apart from "there
# was nothing to change". Reporting the first when the second happened is how a
# script talks someone into believing a step took effect that never ran.
CHANGED=0
while IFS=$'\t' read -r NAME RESOURCE IDENTITY CURRENT; do
  [[ -n "$NAME" ]] || continue

  # An agent without an Agent Identity is on a service-account fallback. The
  # gateway authorizes by agent principal, so binding one of those produces a
  # deployment that looks governed and is not. Refuse it rather than report
  # success.
  if [[ "$UNBIND" != true && -z "$IDENTITY" ]]; then
    echo "    SKIP $NAME: no effectiveIdentity (service-account fallback)." >&2
    echo "         Binding it would not give IAP a principal to authorize." >&2
    FAILED=1
    continue
  fi

  if [[ "$UNBIND" == true ]]; then
    DESIRED=""
    BODY='{"spec":{"deploymentSpec":{"agentGatewayConfig":{}}}}'
  else
    DESIRED="$GATEWAY_URI"
    BODY="$(python3 -c '
import json, sys
print(json.dumps({"spec": {"deploymentSpec": {"agentGatewayConfig":
      {"agentToAnywhereConfig": {"agentGateway": sys.argv[1]}}}}}))' "$GATEWAY_URI")"
  fi

  # Compared by the part after /locations/, because Google accepts the project
  # id and echoes back the project number -- so the literal strings differ for
  # an agent that is already correctly bound. Comparing them whole would make
  # every re-run issue a redundant PATCH and report a change that did not
  # happen.
  if [[ "${CURRENT#*/locations/}" == "${DESIRED#*/locations/}" ]]; then
    info "$NAME: already $([[ -z "$DESIRED" ]] && echo 'unbound' || echo 'bound'); no change"
    continue
  fi

  RESPONSE="$(curl -sS -X PATCH \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$BODY" \
    "${HOST}/${RESOURCE}?updateMask=spec.deployment_spec.agent_gateway_config" 2>&1)" || true

  # The PATCH response is not evidence, so the resource is read back and
  # compared. But the read-back must be PATIENT: this binding is eventually
  # consistent, and measured on a live project it took minutes, not seconds,
  # to appear.
  #
  # An earlier version of this script waited three seconds, saw nothing, and
  # reported "the API accepted the request and did not apply it" for three
  # agents that were in fact all binding correctly. That false negative sent a
  # whole investigation into redeploying agents and migrating deployment paths,
  # none of which was needed. Polling until a deadline is the fix; the wait is
  # the feature.
  POLL_DEADLINE=$(( SECONDS + 300 ))
  APPLIED=""
  while :; do
    APPLIED="$(curl -sS -H "Authorization: Bearer ${TOKEN}" "${HOST}/${RESOURCE}" \
      | python3 -c '
import json, sys
try:
    spec = (json.load(sys.stdin).get("spec") or {})
except Exception:
    print(""); raise SystemExit(0)
config = (spec.get("deploymentSpec") or {}).get("agentGatewayConfig") or {}
print((config.get("agentToAnywhereConfig") or {}).get("agentGateway") or "")')"
    [[ "${APPLIED#*/locations/}" == "${DESIRED#*/locations/}" ]] && break
    if (( SECONDS >= POLL_DEADLINE )); then
      break
    fi
    printf '    %s: waiting for the change to propagate...\r' "$NAME"
    sleep 15
  done
  printf '                                                              \r'

  if [[ "${APPLIED#*/locations/}" == "${DESIRED#*/locations/}" ]]; then
    info "$NAME: $([[ -z "$DESIRED" ]] && echo 'unbound' || echo "bound -> ${GATEWAY}") (verified)"
    CHANGED=$((CHANGED + 1))
  else
    ERROR_MESSAGE="$(python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    print(json.loads(raw).get("error", {}).get("message", "")[:300])
except Exception:
    print(raw[:300])' <<<"$RESPONSE")"
    echo "    FAIL $NAME: still not applied after 5 minutes of polling." >&2
    echo "         requested : ${DESIRED:-<unbound>}" >&2
    echo "         actual    : ${APPLIED:-<no agentGatewayConfig on the resource>}" >&2
    [[ -n "$ERROR_MESSAGE" ]] && echo "         response  : $ERROR_MESSAGE" >&2
    echo "         This may still be propagation rather than failure. Re-read" >&2
    echo "         before concluding: python3 cloud/verify_agent_gateway.py --post-bind" >&2
    FAILED=1
  fi
done <<<"$RESOLVED"

step "Next"
if [[ $FAILED -ne 0 ]]; then
  cat <<'NEXT'
    At least one agent did not show the change within the polling window.

    Before concluding it failed: this binding is eventually consistent and has
    been measured taking minutes. Re-read the state before acting on this --
    a previous investigation wasted considerable effort treating propagation
    delay as an API that silently ignored writes.

        python3 cloud/verify_agent_gateway.py --post-bind

    If it genuinely never applies, the documented fallback is to set the
    gateway at creation instead: Google's docs say "You must redeploy a new
    reasoning engine with both agent_gateway_config and
    identity_type=AGENT_IDENTITY set at agent creation time."

        config = {
            "agent_gateway_config": {
                "agent_to_anywhere_config": {"agent_gateway": AGENT_GATEWAY}},
            "identity_type": types.IdentityType.AGENT_IDENTITY,
        }

    cloud/deploy_a2a.py already deploys through the Python SDK and is where
    that config fits with a small change. Note that re-creating an engine
    allocates a NEW Agent Identity principal, which invalidates
    config/broker_clients.json and every resource-level IAM grant that names
    the old one -- an update preserves them, which is why it is tried first.
NEXT
  exit 1
fi

if [[ $CHANGED -eq 0 ]]; then
  info "Nothing changed: every agent was already in the requested state."
  info "No routing was altered, so there is nothing to re-verify."
  exit 0
fi

if [[ "$UNBIND" == true ]]; then
  cat <<'NEXT'
    Routing is back on the direct path; the gateway no longer sees this
    traffic. Confirm the demo is healthy again:

      python3 cloud/verify_cloud.py

    The gateway, its authorization policy, and the registered destinations all
    still exist. Re-binding is:

      bash cloud/bind_agents_to_gateway.sh
NEXT
  exit 0
fi

cat <<'NEXT'
    Routing has changed: all agent egress now traverses the gateway. Nothing is
    blocked yet -- the authorization extension is audit-only -- but the
    transport path is different, so confirm the demo still works BEFORE
    touching enforcement:

      python3 cloud/verify_agent_gateway.py --post-bind
      python3 cloud/verify_cloud.py
      bash scripts/verify.sh

    verify_cloud.py must still be 5/5. A 401 on the A2A hop is the signal that
    certificate-bound Agent Identity did not survive the gateway's TLS
    inspection -- that is a transport failure, not an IAM one, and the fix is
    to unbind rather than to change a policy.

    Then read what the gateway actually observed, which is the input to any
    deny policy worth writing:

      python3 cloud/verify_agent_gateway.py --observed

    Reverse this at any time with:

      bash cloud/bind_agents_to_gateway.sh --unbind
NEXT
