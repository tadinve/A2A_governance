from __future__ import annotations

import httpx
from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace

from .audit import record
from .security import bearer_token, current_actor, decode_token, scopes
from .settings import ZOHO_URL, load_json
from .telemetry import instrument_fastapi


app = FastAPI(title="Zoho Procurement MCP Server", version="1.0")
instrument_fastapi(app, "zoho-procurement-mcp")
tracer = trace.get_tracer(__name__)
TARGET = "zoho-procurement-mcp"


async def _zoho_token(client: httpx.AsyncClient) -> str:
    config = load_json("zoho_oauth_clients.json")["zoho-procurement-connector"]
    response = await client.post(
        f"{ZOHO_URL}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "zoho-procurement-connector",
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
    if current_actor(claims) != "procurement-agent":
        raise HTTPException(403, "Procurement Agent is required")
    if payload.get("jsonrpc") != "2.0" or payload.get("method") != "tools/call":
        raise HTTPException(400, "Expected MCP tools/call")
    params = payload.get("params", {})
    tool = params.get("name")
    args = params.get("arguments", {})
    required = {"create_purchase_order_draft": "purchaseorder.create", "get_purchase_order_status": "purchaseorder.read"}.get(tool)
    if not required or required not in scopes(claims):
        raise HTTPException(403, "Tool is not exposed or semantic scope is missing")

    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        zoho_token = await _zoho_token(client)
        headers = {"Authorization": f"Bearer {zoho_token}"}
        if tool == "create_purchase_order_draft":
            headers["X-Idempotency-Key"] = str(args["idempotency_key"])
            create = await client.post(
                f"{ZOHO_URL}/api/v1/purchaseorders",
                params={"organization_id": "demo-org-1001"},
                headers=headers,
                json={
                    "vendor_id": args["vendor_id"],
                    "reference_number": args["reference_number"],
                    "line_items": [{"item_id": args["item_id"], "quantity": args["quantity"], "rate": args["rate"]}],
                },
            )
            create.raise_for_status()
            po = create.json()["purchaseorder"]
            submit = await client.post(
                f"{ZOHO_URL}/api/v1/purchaseorders/{po['purchaseorder_id']}/submit",
                params={"organization_id": "demo-org-1001"},
                headers={"Authorization": f"Bearer {zoho_token}"},
            )
            submit.raise_for_status()
            result = submit.json()["purchaseorder"]
        else:
            response = await client.get(
                f"{ZOHO_URL}/api/v1/purchaseorders/{args['purchaseorder_id']}",
                params={"organization_id": "demo-org-1001"},
                headers=headers,
            )
            response.raise_for_status()
            result = response.json()["purchaseorder"]

    with tracer.start_as_current_span(f"mcp.tool.{tool}") as span:
        span.set_attribute("mcp.tool.name", tool)
    record("zoho-procurement-mcp", "MCP_TOOL_CALLED", tool=tool, actor="procurement-agent", po_id=result["purchaseorder_id"])
    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"content": [{"type": "text", "text": f"{result['purchaseorder_id']}: {result['approval_status']}"}], "structuredContent": result}}
