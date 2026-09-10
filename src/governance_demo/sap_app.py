from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace
from pydantic import BaseModel

from .audit import record
from .security import bearer_token, decode_token, scopes
from .settings import load_json
from .telemetry import instrument_fastapi


app = FastAPI(title="SAP Inventory API Simulator", version="1.0")
instrument_fastapi(app, "sap-inventory-api")
tracer = trace.get_tracer(__name__)


class InventoryRequest(BaseModel):
    sku: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": "sap-api"}


@app.post("/inventory")
def inventory(
    payload: InventoryRequest,
    authorization: str | None = Header(None),
    x_gateway_verified: str | None = Header(None),
) -> dict:
    if x_gateway_verified != "true":
        raise HTTPException(403, "Direct SAP access is disabled; use Agent Gateway")
    try:
        claims = decode_token(bearer_token(authorization), audience="sap-api")
    except Exception as exc:
        raise HTTPException(401, f"SAP token validation failed: {type(exc).__name__}") from exc
    if "records.read" not in scopes(claims):
        raise HTTPException(403, "records.read scope required")
    item = load_json("inventory.json").get(payload.sku.upper())
    if not item:
        raise HTTPException(404, "SKU not found")
    with tracer.start_as_current_span("sap.read_inventory") as span:
        span.set_attribute("sap.sku", payload.sku.upper())
        span.set_attribute("enduser.id", claims["sub"])
        record(
            "sap-api",
            "INVENTORY_READ",
            user=claims["sub"],
            actor_chain=claims.get("act"),
            sku=payload.sku.upper(),
        )
        return {"sku": payload.sku.upper(), **item, "authorization": "records.read"}

