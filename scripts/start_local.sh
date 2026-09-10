#!/usr/bin/env bash
set -euo pipefail

DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEMO_ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run: bash scripts/setup.sh" >&2
  exit 1
fi

.venv/bin/python scripts/init_demo.py --reset-evidence
export PYTHONPATH="$DEMO_ROOT/src"

services=(
  "registry_app:8100:registry"
  "idp_app:8101:identity"
  "gateway_app:8102:gateway"
  "agent_a_app:8103:agent-a"
  "agent_b_app:8104:agent-b"
  "sap_app:8105:sap"
)

for spec in "${services[@]}"; do
  IFS=: read -r module port name <<<"$spec"
  if [[ -f "runtime/$name.pid" ]] && kill -0 "$(<"runtime/$name.pid")" 2>/dev/null; then
    echo "$name is already running"
    continue
  fi
  .venv/bin/python -m uvicorn "governance_demo.$module:app" \
    --host 127.0.0.1 --port "$port" >"runtime/$name.log" 2>&1 &
  echo $! >"runtime/$name.pid"
done

for port in 8100 8101 8102 8103 8104 8105; do
  ready=false
  for _ in {1..40}; do
    if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 0.25
  done
  if [[ "$ready" != true ]]; then
    echo "Service on port $port failed to start. Check runtime/*.log" >&2
    bash scripts/stop_local.sh
    exit 1
  fi
done

echo "All six services are ready. Run: bash scripts/run_demo.sh"
