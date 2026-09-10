from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from opentelemetry import trace

from .audit import record
from .security import bearer_token, current_actor, decode_token, scopes
from .settings import load_json
from .telemetry import instrument_fastapi


app = FastAPI(title="Demo Agent Gateway", version="1.0")
instrument_fastapi(app, "agent-gateway")
tracer = trace.get_tracer(__name__)

BLOCKED_PHRASES = ("ignore previous", "exfiltrate", "send confidential", "reveal system prompt")


def _contains_blocked_content(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(phrase in lowered for phrase in BLOCKED_PHRASES)
    if isinstance(value, dict):
        return any(_contains_blocked_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_blocked_content(item) for item in value)
    return False


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": "agent-gateway"}


@app.post("/route/{target}")
async def route(target: str, request: Request, authorization: str | None = Header(None)):
    registry = load_json("registry.json")
    target_data = registry["agents"].get(target) or registry["resources"].get(target)
    if not target_data:
        raise HTTPException(404, "Target is not registered")

    try:
        raw_token = bearer_token(authorization)
        claims = decode_token(raw_token, audience=target)
        actor = current_actor(claims)
    except Exception as exc:
        record("agent-gateway", "AUTHENTICATION_DENIED", target=target, reason=type(exc).__name__)
        raise HTTPException(401, f"Gateway authentication failed: {type(exc).__name__}") from exc

    policy = next(
        (
            item
            for item in load_json("policies.json")["gateway_routes"]
            if item["actor"] == actor and item["target"] == target and item["effect"] == "allow"
        ),
        None,
    )
    if not policy or policy["required_scope"] not in scopes(claims):
        record("agent-gateway", "AUTHORIZATION_DENIED", actor=actor, target=target)
        raise HTTPException(403, "IAM-style route policy denied this agent-to-target relationship")

    body = await request.json()
    if _contains_blocked_content(body):
        record("agent-gateway", "CONTENT_BLOCKED", actor=actor, target=target, control="model-armor-simulator")
        raise HTTPException(403, "Content safety policy blocked the request")

    with tracer.start_as_current_span("gateway.authorize_and_route") as span:
        span.set_attribute("agent.actor", actor)
        span.set_attribute("agent.target", target)
        span.set_attribute("auth.scope", policy["required_scope"])
        record(
            "agent-gateway",
            "ROUTE_ALLOWED",
            actor=actor,
            subject=claims["sub"],
            target=target,
            scope=policy["required_scope"],
        )
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(
                target_data["endpoint"],
                json=body,
                headers={
                    "Authorization": f"Bearer {raw_token}",
                    "X-Gateway-Verified": "true",
                },
            )
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text)
    return response.json()
