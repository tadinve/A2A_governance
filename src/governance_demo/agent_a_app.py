from __future__ import annotations

import re
import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace
from pydantic import BaseModel

from .audit import record
from .security import bearer_token, decode_token, public_claims
from .settings import GATEWAY_URL, IDP_URL, REGISTRY_URL
from .telemetry import instrument_fastapi


app = FastAPI(title="Inventory Agent", version="2.0")
instrument_fastapi(app, "inventory-agent")
tracer = trace.get_tracer(__name__)
AGENT_ID = "inventory-agent"
AGENT_SECRET = "inventory-agent-demo-secret"
SPIFFE_ID = "spiffe://demo.local/agents/inventory-agent"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class AskRequest(BaseModel):
    text: str


async def _agent_token(client: httpx.AsyncClient, audience: str, scope: str) -> str:
    response = await client.post(f"{IDP_URL}/oauth/token", data={"grant_type": "client_credentials", "audience": audience, "scope": scope}, auth=(AGENT_ID, AGENT_SECRET))
    response.raise_for_status()
    return response.json()["access_token"]


async def _exchange(client: httpx.AsyncClient, subject_token: str, audience: str, scope: str) -> dict:
    actor_token = await _agent_token(client, "sts", "token.exchange")
    response = await client.post(
        f"{IDP_URL}/oauth/token",
        data={
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "actor_token": actor_token,
            "actor_token_type": ACCESS_TOKEN_TYPE,
            "audience": audience,
            "scope": scope,
        },
        auth=(AGENT_ID, AGENT_SECRET),
    )
    response.raise_for_status()
    return response.json()


async def _route(client: httpx.AsyncClient, target: str, token: str, body: dict) -> dict:
    response = await client.post(f"{GATEWAY_URL}/route/{target}", json=body, headers={"Authorization": f"Bearer {token}"})
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise HTTPException(response.status_code, detail)
    return response.json()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": AGENT_ID}


@app.get("/identity")
def identity() -> dict:
    return {"agent_id": AGENT_ID, "display_name": "Inventory Agent", "identity": SPIFFE_ID, "identity_type": "demo SPIFFE-style identity", "cloud_equivalent": "Google Cloud Agent Identity principal://.../reasoningEngines/AGENT_ID"}


@app.post("/ask")
async def ask(payload: AskRequest, authorization: str | None = Header(None)) -> dict:
    try:
        user_token = bearer_token(authorization)
        user_claims = decode_token(user_token, audience=AGENT_ID)
    except Exception as exc:
        raise HTTPException(401, "User authentication failed") from exc
    request_id = str(uuid.uuid4())
    sku_match = re.search(r"CK-[A-Z]+-\d+", payload.text.upper())
    sku = sku_match.group(0) if sku_match else "CK-GPU-42"
    with tracer.start_as_current_span("agent.invoke inventory-agent") as span:
        span.set_attribute("agent.id", AGENT_ID)
        span.set_attribute("enduser.id", user_claims["sub"])
        record(AGENT_ID, "USER_REQUEST_ACCEPTED", request_id=request_id, user=user_claims["sub"], sku=sku)
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            registry_token = await _agent_token(client, "registry", "registry.read")
            registry_response = await client.get(f"{REGISTRY_URL}/registry", headers={"Authorization": f"Bearer {registry_token}"})
            registry_response.raise_for_status()
            procurement = registry_response.json()["agents"]["procurement-agent"]

            inventory_delegation = await _exchange(client, user_token, "zoho-inventory-mcp", "inventory.read")
            inventory_rpc = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": "get_inventory", "arguments": {"sku": sku}}}
            inventory_result = await _route(client, "zoho-inventory-mcp", inventory_delegation["access_token"], inventory_rpc)
            item = inventory_result["result"]["structuredContent"]
            low_stock = item["stock_on_hand"] < item["reorder_level"]
            procurement_result = None
            if low_stock:
                reorder_quantity = item["target_stock"] - item["stock_on_hand"]
                a2a_delegation = await _exchange(client, user_token, "procurement-agent", "purchase.request")
                message = {
                    "jsonrpc": "2.0", "id": request_id, "method": "message/send",
                    "params": {"message": {"messageId": f"msg-{uuid.uuid4()}", "role": "user", "parts": [{"kind": "text", "text": f"Prepare a purchase order for {reorder_quantity} units of {sku}"}], "metadata": {"action": "create_po", "sku": sku, "item_id": item["item_id"], "quantity": reorder_quantity, "rate": item["purchase_rate"], "vendor_id": item["preferred_vendor_id"], "original_user": user_claims["sub"], "request_id": request_id}}}
                }
                procurement_result = await _route(client, "procurement-agent", a2a_delegation["access_token"], message)
                po = procurement_result["result"]["metadata"]["purchase_order"]
                answer = f"Zoho reports {item['stock_on_hand']} units of {sku}, below the reorder level of {item['reorder_level']}. Procurement Agent created {po['purchaseorder_id']} for {reorder_quantity} units; human approval is pending."
            else:
                answer = f"Zoho reports {item['stock_on_hand']} units of {sku}; no reorder is required."
    return {
        "request_id": request_id,
        "answer": answer,
        "inventory": item,
        "purchase_order": procurement_result["result"]["metadata"]["purchase_order"] if procurement_result else None,
        "governance_evidence": {
            "user_identity": public_claims(user_claims),
            "inventory_agent_identity": SPIFFE_ID,
            "discovered_agent": {"name": procurement["display_name"], "identity": procurement["identity"], "skills": procurement["skills"]},
            "inventory_delegation": inventory_delegation["claims_for_demo"],
            "a2a_delegation": procurement_result["result"]["metadata"]["received_delegation"] if procurement_result else None,
            "human_approval": "required" if procurement_result else "not_required",
        },
    }


@app.get("/orders/{po_id}")
async def order_status(po_id: str, authorization: str | None = Header(None)) -> dict:
    try:
        user_token = bearer_token(authorization)
        decode_token(user_token, audience=AGENT_ID)
    except Exception as exc:
        raise HTTPException(401, "User authentication failed") from exc
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        delegated = await _exchange(client, user_token, "procurement-agent", "purchase.status")
        message = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send", "params": {"message": {"messageId": f"msg-{uuid.uuid4()}", "role": "user", "parts": [{"kind": "text", "text": f"Get status for {po_id}"}], "metadata": {"action": "get_status", "purchaseorder_id": po_id}}}}
        result = await _route(client, "procurement-agent", delegated["access_token"], message)
    return result["result"]["metadata"]["purchase_order"]
