from __future__ import annotations

import httpx
from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace

from .audit import record
from .security import bearer_token, current_actor, decode_token, scopes
from .settings import ZOHO_URL, load_json
from .telemetry import instrument_fastapi


app = FastAPI(title="Zoho Inventory Read MCP Server", version="1.0")
instrument_fastapi(app, "zoho-inventory-mcp")
tracer = trace.get_tracer(__name__)
TARGET = "zoho-inventory-mcp"
ZOHO_SCOPE = "ZohoInventory.items.READ"


async def _zoho_token(client: httpx.AsyncClient) -> str:
    config = load_json("zoho_oauth_clients.json")["zoho-inventory-connector"]
    response = await client.post(
        f"{ZOHO_URL}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "zoho-inventory-connector",
            "client_secret": config["client_secret"],
            "refresh_token": config["refresh_token"],
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": TARGET}


@app.post("/mcp")
async def mcp(payload: dict, authorization: str | None = Header(None), x_gateway_verified: str | None = Header(None)) -> dict:
    if x_gateway_verified != "true":
        raise HTTPException(403, "Direct MCP calls are disabled; use Agent Gateway")
    try:
        claims = decode_token(bearer_token(authorization), audience=TARGET)
    except Exception as exc:
        raise HTTPException(401, "Invalid agent delegation token") from exc
    if current_actor(claims) != "inventory-agent" or "inventory.read" not in scopes(claims):
        raise HTTPException(403, "Inventory Agent with inventory.read is required")
    if payload.get("jsonrpc") != "2.0" or payload.get("method") != "tools/call":
        raise HTTPException(400, "Expected MCP tools/call")
    params = payload.get("params", {})
    if params.get("name") != "get_inventory":
        raise HTTPException(403, "Only the narrow get_inventory tool is exposed")
    sku = str(params.get("arguments", {}).get("sku", "")).upper()
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        zoho_token = await _zoho_token(client)
        response = await client.get(
            f"{ZOHO_URL}/api/v1/items",
            params={"organization_id": "demo-org-1001", "sku": sku},
            headers={"Authorization": f"Bearer {zoho_token}"},
        )
        response.raise_for_status()
    items = response.json()["items"]
    if not items:
        raise HTTPException(404, "SKU not found")
    item = items[0]
    with tracer.start_as_current_span("mcp.tool.get_inventory") as span:
        span.set_attribute("mcp.tool.name", "get_inventory")
        span.set_attribute("zoho.oauth.scope", ZOHO_SCOPE)
    record("zoho-inventory-mcp", "MCP_TOOL_CALLED", tool="get_inventory", actor="inventory-agent", sku=sku, zoho_scope=ZOHO_SCOPE)
    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"content": [{"type": "text", "text": f"Stock on hand: {item['stock_on_hand']}"}], "structuredContent": item}}
