#!/usr/bin/env bash
set -euo pipefail
DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEMO_ROOT"
export PYTHONPATH="$DEMO_ROOT/src"

.venv/bin/python scripts/init_demo.py
.venv/bin/python -m pytest -q
bash scripts/start_local.sh
trap 'bash scripts/stop_local.sh' EXIT
bash scripts/run_demo.sh
.venv/bin/python scripts/show_evidence.py >/dev/null
grep -q '"event": "ITEMS_READ"' evidence/audit.jsonl
grep -q '"event": "PURCHASE_ORDER_APPROVED"' evidence/audit.jsonl
grep -q '"event": "MCP_TOOL_CALLED"' evidence/audit.jsonl
echo "Verification passed."
