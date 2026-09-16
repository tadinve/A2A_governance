"""Wires the UI into the same trace a request's downstream calls belong to.

Reuses governance_demo.telemetry's exporter setup (JSONL locally, Cloud Trace
when TRACE_EXPORTER=gcp) rather than a second copy of it -- the destination is
shared with every other service in this demo, only the client libraries this
app actually needs differ.

Two things are worth being deliberate about, because getting either wrong
looks identical to "it's fine" until someone goes looking for the trace and
finds nothing, or finds two:

* This app calls out over `requests` (google.auth's transport session, and
  zoho_mcp.py's own client), never httpx. Instrumenting httpx here -- the
  governance_demo services' own client -- would run cleanly and capture
  nothing, which is a worse failure mode than an obvious error: it looks like
  tracing is working.
* process_run(), the span that actually matters (the reorder check, the
  agent call, the draft), runs in a plain threading.Thread dispatched from
  the request handler (see app.py's _dispatch/_run_task). OpenTelemetry's
  context is carried in a contextvar, and a new Python thread starts with a
  fresh one by default -- without ThreadingInstrumentor, that background span
  would open its own disconnected trace instead of continuing the request's,
  and "one correlated trace per action" would be false despite every other
  piece of instrumentation being present and correctly configured.
"""
from __future__ import annotations

from governance_demo.telemetry import configure_telemetry


def instrument_ui(app) -> None:
    configure_telemetry("inventory-ui")

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.threading import ThreadingInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    RequestsInstrumentor().instrument()
    ThreadingInstrumentor().instrument()
