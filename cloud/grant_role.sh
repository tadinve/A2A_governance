#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 'principal://...' roles/ROLE_NAME" >&2
  exit 1
fi
if [[ -z "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
  echo "Set GOOGLE_CLOUD_PROJECT first." >&2
  exit 1
fi

AGENT_PRINCIPAL="$1"
ROLE_NAME="$2"
if [[ "$AGENT_PRINCIPAL" != principal://* ]]; then
  echo "The first argument must be the exact effective identity from show_identities.py." >&2
  exit 1
fi

gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
  --member="$AGENT_PRINCIPAL" \
  --role="$ROLE_NAME"

echo "Granted $ROLE_NAME to the one agent principal. It will now appear in this IAM binding."
