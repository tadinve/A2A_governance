"""The UI's OpenTelemetry wiring has two ways to look instrumented while
proving nothing:

* instrumenting the wrong HTTP client. This app calls out over `requests`
  (google.auth's transport session, zoho_mcp.py's own client), never httpx --
  an httpx instrumentor runs cleanly and captures no spans for any of it.
* instrumenting nothing across the thread boundary. process_run() -- the
  span that actually matters, the reorder check or the agent call -- runs in
  a plain threading.Thread dispatched from the request handler. Without
  ThreadingInstrumentor, OpenTelemetry's context does not cross into a new
  thread by default, and that background work opens its own disconnected
  trace instead of continuing the request's.

These tests exercise the real instrument_ui() against the exact
FastAPI-dispatches-a-thread pattern app.py uses, not a reimplementation of
it, and read back the spans it actually wrote.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# OpenTelemetry's TracerProvider is process-global and can only be installed
# once: whichever test module happens to import a service app first (several
# do, at module scope, via configure_telemetry) wins it for the rest of the
# pytest process, and instrument_ui()'s own call becomes a silent no-op for
# every test after that -- run this check in-process alongside
# test_auth_broker.py and watch it fail for exactly that reason, with
# OpenTelemetry's own "Overriding of current TracerProvider is not allowed"
# warning as the tell. So the one test that needs to observe real span
# correlation runs in a fresh subprocess instead.
_SCRIPT = """
import json, shutil, sys, threading, time
from pathlib import Path
sys.path.insert(0, "src")

evidence_dir = Path(sys.argv[1])
shutil.rmtree(evidence_dir, ignore_errors=True)
import governance_demo.settings as settings
settings.EVIDENCE_DIR = evidence_dir

from fastapi import FastAPI
from fastapi.testclient import TestClient
from inventory_ui.telemetry import instrument_ui
from opentelemetry import trace

app = FastAPI()
instrument_ui(app)
tracer = trace.get_tracer(__name__)

def background_work():
    with tracer.start_as_current_span("process_run_equivalent"):
        time.sleep(0.01)

@app.post("/dispatch")
def dispatch():
    t = threading.Thread(target=background_work, daemon=True)
    t.start()
    t.join()
    return {"ok": True}

response = TestClient(app).post("/dispatch")
assert response.status_code == 200

spans_file = evidence_dir / "inventory-ui.spans.jsonl"
spans = [json.loads(line) for line in spans_file.read_text().splitlines() if line.strip()]
print(json.dumps(spans))
"""


def test_a_background_thread_span_joins_the_request_trace(tmp_path):
    """The property this module exists for: one action, one trace.

    Run for real, in a fresh interpreter, against the actual instrument_ui()
    and the exact FastAPI-dispatches-a-thread pattern app.py uses -- not a
    reimplementation of either.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(tmp_path)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr

    spans = json.loads(result.stdout.strip().splitlines()[-1])
    names = {s["name"] for s in spans}
    assert "process_run_equivalent" in names
    assert any("dispatch" in n for n in names)

    trace_ids = {s["trace_id"] for s in spans}
    assert len(trace_ids) == 1, (
        f"expected one correlated trace, got {len(trace_ids)}: "
        f"{[(s['name'], s['trace_id']) for s in spans]}"
    )


def test_requests_not_httpx_is_the_instrumented_client():
    """Regression guard for the specific mistake this module's docstring
    describes: wiring an instrumentor for a client this app does not use."""
    source = (REPO_ROOT / "src" / "inventory_ui" / "telemetry.py").read_text()
    assert "RequestsInstrumentor" in source
    assert "HTTPXClientInstrumentor" not in source


def test_threading_instrumentor_is_present():
    """Regression guard: without this, the fix above passing is a fluke of
    the test running fast enough, not a property of the wiring."""
    source = (REPO_ROOT / "src" / "inventory_ui" / "telemetry.py").read_text()
    assert "ThreadingInstrumentor" in source
