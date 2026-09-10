#!/usr/bin/env bash
set -euo pipefail

CLOUD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "Set GOOGLE_CLOUD_PROJECT first." >&2
  exit 1
fi
if [[ "$REGION" == "global" ]]; then
  REGION="us-central1"
fi
if ! command -v gcloud >/dev/null || ! command -v python3 >/dev/null; then
  echo "gcloud and Python 3 are required." >&2
  exit 1
fi

cd "$CLOUD_ROOT"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

gcloud services enable \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  cloudtrace.googleapis.com \
  --project "$PROJECT_ID" --quiet

echo "Deploying with Agent Identity and OpenTelemetry -> Cloud Trace."
.venv/bin/adk deploy agent_engine \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --display_name "Agent Identity Demo" \
  --otel_to_cloud \
  "$CLOUD_ROOT/identity_agent"

echo
echo "The .agent_engine_config.json file caused Agent Identity provisioning."
echo "Open Agent Platform -> Deployments -> Agent Identity Demo -> Identity."
echo "Then run: cloud/.venv/bin/python cloud/show_identities.py"
