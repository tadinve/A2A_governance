from __future__ import annotations

import re
import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace

from .audit import record
from .security import bearer_token, decode_token, public_claims
from .settings import GATEWAY_URL, IDP_URL
from .telemetry import instrument_fastapi


app = FastAPI(title="Inventory Specialist Agent B", version="1.0")
instrument_fastapi(app, "inventory-specialist-agent-b")
tracer = trace.get_tracer(__name__)

AGENT_ID = "agent-b"
AGENT_SECRET = "agent-b-demo-secret"
SPIFFE_ID = "spiffe://demo.local/agents/agent-b"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": AGENT_ID}


@app.get("/identity")
def identity() -> dict:
    return {
        "agent_id": AGENT_ID,
        "identity": SPIFFE_ID,
        "identity_type": "demo SPIFFE-style identity",
    }


@app.get("/.well-known/agent-card.json")
def agent_card() -> dict:
    return {
        "name": "Inventory Specialist Agent B",
        "description": "Retrieves governed inventory evidence from SAP",
        "url": "http://127.0.0.1:8104/a2a",
        "version": "1.0.0",
        "capabilities": {"streaming": False},
        "securitySchemes": {"oauth2": {"type": "oauth2", "description": "Audience-bound delegated token"}},
        "security": [{"oauth2": ["inventory.read"]}],
        "skills": [
            {
                "id": "sap-inventory-lookup",
                "name": "SAP inventory lookup",
                "description": "Read inventory quantity for an approved SKU",
                "tags": ["sap", "inventory", "read-only"],
                "examples": ["Get inventory for CK-GPU-42"],
            }
        ],
    }


@app.post("/a2a")
async def a2a(payload: dict, authorization: str | None = Header(None), x_gateway_verified: str | None = Header(None)):
    if x_gateway_verified != "true":
        raise HTTPException(403, "Direct calls are disabled; use the governed gateway path")
    try:
        delegated_token = bearer_token(authorization)
        claims = decode_token(delegated_token, audience=AGENT_ID)
    except Exception as exc:
        raise HTTPException(401, f"Agent B authentication failed: {type(exc).__name__}") from exc
    if payload.get("jsonrpc") != "2.0" or payload.get("method") != "message/send":
        raise HTTPException(400, "Expected an A2A-style JSON-RPC message/send request")

    actor = (claims.get("act") or {}).get("sub")
    message = payload.get("params", {}).get("message", {})
    text = " ".join(part.get("text", "") for part in message.get("parts", []))
    match = re.search(r"CK-[A-Z]+-\d+", text.upper())
    sku = match.group(0) if match else "CK-GPU-42"

    with tracer.start_as_current_span("agent.invoke agent-b") as span:
        span.set_attribute("agent.id", AGENT_ID)
        span.set_attribute("agent.caller", actor or "unknown")
        span.set_attribute("enduser.id", claims["sub"])
        record("agent-b", "A2A_REQUEST_ACCEPTED", caller=actor, user=claims["sub"], sku=sku)

        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            actor_response = await client.post(
                f"{IDP_URL}/oauth/token",
                data={"grant_type": "client_credentials", "audience": "sts", "scope": "token.exchange"},
                auth=(AGENT_ID, AGENT_SECRET),
            )
            actor_response.raise_for_status()
            exchange = await client.post(
                f"{IDP_URL}/oauth/token",
                data={
                    "grant_type": TOKEN_EXCHANGE_GRANT,
                    "subject_token": delegated_token,
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                    "actor_token": actor_response.json()["access_token"],
                    "actor_token_type": ACCESS_TOKEN_TYPE,
                    "audience": "sap-api",
                    "scope": "records.read",
                },
                auth=(AGENT_ID, AGENT_SECRET),
            )
            exchange.raise_for_status()
            sap_token = exchange.json()
            sap_response = await client.post(
                f"{GATEWAY_URL}/route/sap-api",
                json={"sku": sku},
                headers={"Authorization": f"Bearer {sap_token['access_token']}"},
            )
            sap_response.raise_for_status()

    inventory = sap_response.json()
    text_result = (
        f"SAP reports {inventory['quantity']} units of {inventory['description']} "
        f"({sku}) in warehouse {inventory['warehouse']}."
    )
    return {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "result": {
            "task": {"id": f"task-{uuid.uuid4()}", "state": "completed"},
            "artifacts": [{"name": "inventory-evidence", "parts": [{"kind": "text", "text": text_result}]}],
            "metadata": {
                "agent_identity": SPIFFE_ID,
                "received_delegation": public_claims(claims),
                "resource_token_claims": sap_token["claims_for_demo"],
            },
        },
    }
