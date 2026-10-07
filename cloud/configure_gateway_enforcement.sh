#!/usr/bin/env bash
# Switch the Agent Gateway from audit-only to fail-closed enforcement.
#
# DO NOT RUN THIS BEFORE A DEMO. Enforcement makes every unregistered
# destination a 403, and an unregistered destination is indistinguishable from
# a broken one at the moment it fails. The whole point of the DRY_RUN stage is
# to find those first.
#
#   bash cloud/configure_gateway_enforcement.sh [options]
#
#     --project PROJECT_ID   default $GOOGLE_CLOUD_PROJECT
#     --region REGION        default $GOOGLE_CLOUD_LOCATION, else us-central1
#     --gateway NAME         default a2a-governance-egress
#     --revert               go back to the DRY_RUN extension
#     --yes                  skip the preflight prompt
#     -h / --help
#
# This is a thin wrapper over `setup_agent_gateway.sh --enforce`, kept separate
# so that enforcement is never something a person arrives at by re-running a
# setup script with a flag they forgot they had typed. It adds the checks that
# only matter at this transition.
set -euo pipefail

CLOUD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

usage() {
  awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]:-$0}"
  exit "${1:-0}"
}

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
GATEWAY="a2a-governance-egress"
REVERT=false
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --project) PROJECT_ID="${2:?--project needs a value}"; shift 2 ;;
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    --gateway) GATEWAY="${2:?--gateway needs a value}"; shift 2 ;;
    --revert) REVERT=true; shift ;;
    --yes|-y) ASSUME_YES=true; shift ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
done

step() { echo; echo "=== $* ==="; }
info() { echo "    $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ -n "$PROJECT_ID" ]] || die "no project. Pass --project or set GOOGLE_CLOUD_PROJECT."
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="$REGION"

if [[ "$REVERT" == true ]]; then
  step "Reverting to DRY_RUN"
  bash "$CLOUD_ROOT/setup_agent_gateway.sh" \
    --project "$PROJECT_ID" --region "$REGION" --gateway "$GATEWAY"
  info "the authorization policy now points at the audit-only extension again"
  exit 0
fi

step "Preflight"
# Enforcement without a measured destination list is the failure this guards.
# A guessed list is not evidence; the logs are.
info "Checking that the gateway has actually observed traffic..."
if ! python3 "$CLOUD_ROOT/verify_agent_gateway.py" \
     --project "$PROJECT_ID" --region "$REGION" --gateway "$GATEWAY" --observed; then
  die "could not read gateway logs. Enforcement without observation is guesswork."
fi

echo
echo "    Everything listed above must already be registered in the Agent"
echo "    Registry and granted to the calling agent. Any destination shown as"
echo "    DENIED will become a real 403 the moment this completes."
echo
echo "    Reverse with:  bash cloud/configure_gateway_enforcement.sh --revert"
echo

if [[ "$ASSUME_YES" != true ]]; then
  read -r -p "    Enable fail-closed enforcement? [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]] || { echo "    aborted"; exit 1; }
fi

step "Enabling enforcement"
bash "$CLOUD_ROOT/setup_agent_gateway.sh" \
  --project "$PROJECT_ID" --region "$REGION" --gateway "$GATEWAY" --enforce

step "Now prove it"
cat <<'NEXT'
    Enforcement is a security control only if the deny cases are demonstrated,
    not asserted. The matrix worth capturing, from gateway logs rather than
    from agent output:

      Inventory Agent  -> read connector            ALLOW
      Inventory Agent  -> Procurement A2A           ALLOW
      Inventory Agent  -> write connector           DENY   <- the interesting one
      Procurement      -> write connector           ALLOW
      unregistered destination                      DENY

    Collect the evidence:

      python3 cloud/verify_cloud.py
      python3 cloud/verify_agent_gateway.py --observed

    An agent refusing in its own words is NOT proof the gateway denied
    anything. The 403 and the log line are.
NEXT
