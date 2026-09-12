#!/usr/bin/env bash
# Ask a deployed agent a question and print its reply.
#   bash cloud/ask_agent.sh "Inventory Agent" "Check stock on DEMO-WIDGET-A"
set -euo pipefail

DISPLAY_NAME="$1"; MESSAGE="$2"
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
CLOUD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
TOKEN="$(gcloud auth print-access-token)"

RESOURCE="$(curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines" \
  | python3 "$CLOUD_ROOT/find_engine.py" "$DISPLAY_NAME")"

if [[ -z "$RESOURCE" ]]; then
  echo "No deployment named '$DISPLAY_NAME' in $PROJECT_ID/$REGION" >&2
  exit 1
fi

# Note the braces: "$RESOURCE:streamQuery" is a parameter-expansion modifier in zsh.
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"class_method":"stream_query","input":{"user_id":"demo","message":sys.argv[1]}}))' "$MESSAGE")" \
  "https://${REGION}-aiplatform.googleapis.com/v1/${RESOURCE}:streamQuery?alt=sse" \
| python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    for part in event.get("content", {}).get("parts", []):
        call = part.get("function_call")
        if call:
            print("  [tool] " + call["name"])
        if part.get("text"):
            print(part["text"])
'
