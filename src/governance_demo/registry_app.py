from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace

from .audit import record
from .security import bearer_token, decode_token, scopes
from .settings import load_json
from .telemetry import instrument_fastapi


app = FastAPI(title="Demo Agent Registry", version="1.0")
instrument_fastapi(app, "agent-registry")
tracer = trace.get_tracer(__name__)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": "agent-registry"}


@app.get("/registry")
def registry(authorization: str | None = Header(None)) -> dict:
    try:
        claims = decode_token(bearer_token(authorization), audience="registry")
    except Exception as exc:
        raise HTTPException(401, f"Registry authentication failed: {type(exc).__name__}") from exc
    if "registry.read" not in scopes(claims):
        raise HTTPException(403, "registry.read scope required")
    with tracer.start_as_current_span("registry.discover") as span:
        span.set_attribute("agent.id", claims["sub"])
        record("agent-registry", "REGISTRY_READ", actor=claims["sub"])
        return load_json("registry.json")

