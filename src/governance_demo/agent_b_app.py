from __future__ import annotations

import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace

from .audit import record
from .security import bearer_token, decode_token, public_claims
from .settings import GATEWAY_URL, IDP_URL
from .telemetry import instrument_fastapi


app = FastAPI(title="Procurement Agent", version="2.0")
instrument_fastapi(app, "procurement-agent")
tracer = trace.get_tracer(__name__)
AGENT_ID = "procurement-agent"
AGENT_SECRET = "procurement-agent-demo-secret"
SPIFFE_ID = "spiffe://demo.local/agents/procurement-agent"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


async def _exchange(client: httpx.AsyncClient, subject_token: str, audience: str, scope: str) -> dict:
    actor = await client.post(f"{IDP_URL}/oauth/token", data={"grant_type": "client_credentials", "audience": "sts", "scope": "token.exchange"}, auth=(AGENT_ID, AGENT_SECRET))
    actor.raise_for_status()
    response = await client.post(
        f"{IDP_URL}/oauth/token",
        data={"grant_type": TOKEN_EXCHANGE_GRANT, "subject_token": subject_token, "subject_token_type": ACCESS_TOKEN_TYPE, "actor_token": actor.json()["access_token"], "actor_token_type": ACCESS_TOKEN_TYPE, "audience": audience, "scope": scope},
        auth=(AGENT_ID, AGENT_SECRET),
    )
    response.raise_for_status()
    return response.json()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": AGENT_ID}


@app.get("/identity")
def identity() -> dict:
    return {"agent_id": AGENT_ID, "display_name": "Procurement Agent", "identity": SPIFFE_ID, "identity_type": "demo SPIFFE-style identity"}


@app.get("/.well-known/agent-card.json")
def agent_card() -> dict:
    return {"name": "Procurement Agent", "description": "Creates governed Zoho purchase-order drafts and reports status", "url": "http://127.0.0.1:8104/a2a", "version": "2.0.0", "capabilities": {"streaming": False}, "securitySchemes": {"oauth2": {"type": "oauth2", "description": "Audience-bound delegated agent token"}}, "security": [{"oauth2": ["purchase.request", "purchase.status"]}], "skills": [{"id": "purchase-order-drafting", "name": "Purchase order drafting", "description": "Creates and submits a draft for human approval", "tags": ["zoho", "procurement", "human-approval"]}]}


@app.post("/a2a")
async def a2a(payload: dict, authorization: str | None = Header(None), x_gateway_verified: str | None = Header(None)) -> dict:
    if x_gateway_verified != "true":
        raise HTTPException(403, "Direct calls are disabled; use the governed gateway path")
    try:
        delegated_token = bearer_token(authorization)
        claims = decode_token(delegated_token, audience=AGENT_ID)
    except Exception as exc:
        raise HTTPException(401, "Procurement Agent authentication failed") from exc
    if payload.get("jsonrpc") != "2.0" or payload.get("method") != "message/send":
        raise HTTPException(400, "Expected an A2A-style JSON-RPC message/send request")
    message = payload.get("params", {}).get("message", {})
    metadata = message.get("metadata", {})
    action = metadata.get("action")
    if action == "create_po" and "purchase.request" in str(claims.get("scope", "")).split():
        tool = "create_purchase_order_draft"
        semantic_scope = "purchaseorder.create"
        arguments = {"item_id": metadata["item_id"], "quantity": metadata["quantity"], "rate": metadata["rate"], "vendor_id": metadata["vendor_id"], "reference_number": f"AGENT-{metadata['request_id'][:8]}", "idempotency_key": metadata["request_id"]}
    elif action == "get_status" and "purchase.status" in str(claims.get("scope", "")).split():
        tool = "get_purchase_order_status"
        semantic_scope = "purchaseorder.read"
        arguments = {"purchaseorder_id": metadata["purchaseorder_id"]}
    else:
        raise HTTPException(403, "Delegated scope does not permit the requested A2A action")

    with tracer.start_as_current_span("agent.invoke procurement-agent") as span:
        span.set_attribute("agent.id", AGENT_ID)
        span.set_attribute("agent.caller", "inventory-agent")
        record(AGENT_ID, "A2A_REQUEST_ACCEPTED", action=action, user=claims["sub"], actor_chain=claims.get("act"))
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            mcp_token = await _exchange(client, delegated_token, "zoho-procurement-mcp", semantic_scope)
            rpc = {"jsonrpc": "2.0", "id": payload.get("id"), "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
            response = await client.post(f"{GATEWAY_URL}/route/zoho-procurement-mcp", json=rpc, headers={"Authorization": f"Bearer {mcp_token['access_token']}"})
            response.raise_for_status()
            po = response.json()["result"]["structuredContent"]
    text = f"{po['purchaseorder_id']} is {po['approval_status']} (total ${po['total']:.2f})."
    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"task": {"id": f"task-{uuid.uuid4()}", "state": "completed"}, "artifacts": [{"name": "purchase-order-evidence", "parts": [{"kind": "text", "text": text}]}], "metadata": {"agent_identity": SPIFFE_ID, "received_delegation": public_claims(claims), "mcp_delegation": mcp_token["claims_for_demo"], "purchase_order": po, "approval_tool_exposed": False}}}
