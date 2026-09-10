from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from fastapi import Cookie, FastAPI, Form, Header, HTTPException, Response
from opentelemetry import trace
from pydantic import BaseModel, Field

from .audit import record
from .security import bearer_token
from .settings import load_json
from .telemetry import instrument_fastapi


app = FastAPI(title="Zoho Inventory Emulator", version="1.0")
instrument_fastapi(app, "zoho-inventory-emulator")
tracer = trace.get_tracer(__name__)

ORGANIZATION_ID = "demo-org-1001"
VENDOR = {"vendor_id": "vendor-2001", "vendor_name": "CloudKarya Demo Supplier", "approved": True}
ITEMS = {
    "CK-GPU-42": {
        "item_id": "item-4242",
        "sku": "CK-GPU-42",
        "name": "CloudKarya GPU Node",
        "description": "GPU compute node for the demo lab",
        "stock_on_hand": 27,
        "reorder_level": 50,
        "target_stock": 100,
        "purchase_rate": 245.00,
        "preferred_vendor_id": VENDOR["vendor_id"],
    }
}
ACCESS_TOKENS: dict[str, dict[str, Any]] = {}
SESSIONS: dict[str, dict[str, Any]] = {}
PURCHASE_ORDERS: dict[str, dict[str, Any]] = {}
IDEMPOTENCY: dict[str, str] = {}


class PurchaseOrderCreate(BaseModel):
    vendor_id: str
    reference_number: str
    line_items: list[dict[str, Any]] = Field(min_length=1)
    notes: str = "Created by governed Procurement Agent"


class ApprovalRequest(BaseModel):
    expected_draft_hash: str


def _oauth_claims(authorization: str | None, required_scope: str) -> dict[str, Any]:
    try:
        raw = bearer_token(authorization)
    except Exception as exc:
        raise HTTPException(401, "Zoho OAuth access token required") from exc
    claims = ACCESS_TOKENS.get(raw)
    if not claims or claims["expires_at"] <= int(time.time()):
        raise HTTPException(401, "Zoho OAuth access token is invalid or expired")
    if required_scope not in claims["scopes"]:
        raise HTTPException(403, f"Missing Zoho OAuth scope: {required_scope}")
    return claims


def _draft_hash(po: dict[str, Any]) -> str:
    approved_fields = {
        "vendor_id": po["vendor_id"],
        "reference_number": po["reference_number"],
        "line_items": po["line_items"],
        "total": po["total"],
    }
    canonical = json.dumps(approved_fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": "zoho-emulator"}


@app.post("/oauth/v2/token")
def oauth_token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    refresh_token: str = Form(...),
) -> dict:
    if grant_type != "refresh_token":
        raise HTTPException(400, "The emulator supports the Zoho refresh_token grant")
    client = load_json("zoho_oauth_clients.json").get(client_id)
    if not client or not secrets.compare_digest(client["client_secret"], client_secret):
        raise HTTPException(401, "Invalid Zoho OAuth client")
    if not secrets.compare_digest(client["refresh_token"], refresh_token):
        raise HTTPException(401, "Invalid Zoho refresh token")
    token = secrets.token_urlsafe(32)
    ACCESS_TOKENS[token] = {
        "client_id": client_id,
        "scopes": set(client["scopes"]),
        "expires_at": int(time.time()) + 3600,
    }
    record("zoho-emulator", "OAUTH_ACCESS_TOKEN_ISSUED", client_id=client_id, scopes=client["scopes"])
    return {"access_token": token, "token_type": "Bearer", "expires_in": 3600, "api_domain": "http://127.0.0.1:8107"}


@app.get("/api/v1/items")
def list_items(
    sku: str,
    organization_id: str,
    authorization: str | None = Header(None),
) -> dict:
    claims = _oauth_claims(authorization, "ZohoInventory.items.READ")
    if organization_id != ORGANIZATION_ID:
        raise HTTPException(404, "Organization not found")
    item = ITEMS.get(sku.upper())
    record("zoho-emulator", "ITEMS_READ", oauth_client=claims["client_id"], sku=sku.upper())
    return {"code": 0, "message": "success", "items": [item] if item else []}


@app.post("/api/v1/purchaseorders")
def create_purchase_order(
    payload: PurchaseOrderCreate,
    organization_id: str,
    authorization: str | None = Header(None),
    x_idempotency_key: str | None = Header(None),
) -> dict:
    claims = _oauth_claims(authorization, "ZohoInventory.purchaseorders.CREATE")
    if organization_id != ORGANIZATION_ID:
        raise HTTPException(404, "Organization not found")
    if payload.vendor_id != VENDOR["vendor_id"] or not VENDOR["approved"]:
        raise HTTPException(403, "Vendor is not on the approved vendor list")
    if x_idempotency_key and x_idempotency_key in IDEMPOTENCY:
        po = PURCHASE_ORDERS[IDEMPOTENCY[x_idempotency_key]]
        return {"code": 0, "message": "idempotent replay", "purchaseorder": po}

    normalized_lines = []
    total = 0.0
    for line in payload.line_items:
        item_id = str(line.get("item_id", ""))
        quantity = int(line.get("quantity", 0))
        rate = float(line.get("rate", 0))
        item = next((value for value in ITEMS.values() if value["item_id"] == item_id), None)
        if not item or quantity <= 0 or rate != item["purchase_rate"]:
            raise HTTPException(400, "Invalid item, quantity, or approved purchase rate")
        normalized_lines.append({"item_id": item_id, "sku": item["sku"], "quantity": quantity, "rate": rate})
        total += quantity * rate

    po_id = f"PO-{len(PURCHASE_ORDERS) + 1001}"
    po = {
        "purchaseorder_id": po_id,
        "purchaseorder_number": po_id,
        "vendor_id": payload.vendor_id,
        "vendor_name": VENDOR["vendor_name"],
        "reference_number": payload.reference_number,
        "line_items": normalized_lines,
        "total": round(total, 2),
        "status": "draft",
        "approval_status": "not_submitted",
        "created_by_oauth_client": claims["client_id"],
    }
    po["draft_hash"] = _draft_hash(po)
    PURCHASE_ORDERS[po_id] = po
    if x_idempotency_key:
        IDEMPOTENCY[x_idempotency_key] = po_id
    record("zoho-emulator", "PURCHASE_ORDER_CREATED", oauth_client=claims["client_id"], po_id=po_id, draft_hash=po["draft_hash"])
    return {"code": 0, "message": "Purchase order created", "purchaseorder": po}


@app.post("/api/v1/purchaseorders/{po_id}/submit")
def submit_purchase_order(
    po_id: str,
    organization_id: str,
    authorization: str | None = Header(None),
) -> dict:
    claims = _oauth_claims(authorization, "ZohoInventory.purchaseorders.UPDATE")
    po = PURCHASE_ORDERS.get(po_id)
    if organization_id != ORGANIZATION_ID or not po:
        raise HTTPException(404, "Purchase order not found")
    if po["status"] == "draft":
        po["status"] = "submitted"
        po["approval_status"] = "pending_approval"
    record("zoho-emulator", "PURCHASE_ORDER_SUBMITTED", oauth_client=claims["client_id"], po_id=po_id)
    return {"code": 0, "message": "Purchase order submitted for approval", "purchaseorder": po}


@app.get("/api/v1/purchaseorders/{po_id}")
def get_purchase_order(
    po_id: str,
    organization_id: str,
    authorization: str | None = Header(None),
) -> dict:
    claims = _oauth_claims(authorization, "ZohoInventory.purchaseorders.READ")
    po = PURCHASE_ORDERS.get(po_id)
    if organization_id != ORGANIZATION_ID or not po:
        raise HTTPException(404, "Purchase order not found")
    record("zoho-emulator", "PURCHASE_ORDER_READ", oauth_client=claims["client_id"], po_id=po_id)
    return {"code": 0, "message": "success", "purchaseorder": po}


@app.post("/ui/login")
def ui_login(response: Response, email: str = Form(...), password: str = Form(...)) -> dict:
    if email != "approver@example.com" or password != "demo-approval-password":
        raise HTTPException(401, "Invalid human credentials")
    session_id = secrets.token_urlsafe(24)
    SESSIONS[session_id] = {"email": email, "roles": ["purchaseorder.approver"], "expires_at": int(time.time()) + 900}
    response.set_cookie("zoho_session", session_id, httponly=True, samesite="strict")
    record("zoho-emulator", "HUMAN_UI_LOGIN", user=email)
    return {"message": "signed in", "user": email, "roles": ["purchaseorder.approver"]}


def _human_session(session_id: str | None) -> dict[str, Any]:
    session = SESSIONS.get(session_id or "")
    if not session or session["expires_at"] <= int(time.time()):
        raise HTTPException(401, "Valid Zoho human UI session required")
    if "purchaseorder.approver" not in session["roles"]:
        raise HTTPException(403, "Human is not a purchase-order approver")
    return session


@app.get("/ui/purchaseorders/{po_id}")
def review_purchase_order(po_id: str, zoho_session: str | None = Cookie(None)) -> dict:
    session = _human_session(zoho_session)
    po = PURCHASE_ORDERS.get(po_id)
    if not po:
        raise HTTPException(404, "Purchase order not found")
    record("zoho-emulator", "HUMAN_REVIEWED_PURCHASE_ORDER", user=session["email"], po_id=po_id)
    return {"reviewed_by": session["email"], "purchaseorder": po}


@app.post("/ui/purchaseorders/{po_id}/approve")
def approve_purchase_order(
    po_id: str,
    payload: ApprovalRequest,
    zoho_session: str | None = Cookie(None),
) -> dict:
    session = _human_session(zoho_session)
    po = PURCHASE_ORDERS.get(po_id)
    if not po:
        raise HTTPException(404, "Purchase order not found")
    if po["approval_status"] != "pending_approval":
        raise HTTPException(409, "Purchase order is not awaiting approval")
    current_hash = _draft_hash(po)
    if payload.expected_draft_hash != current_hash or po["draft_hash"] != current_hash:
        raise HTTPException(409, "Draft changed after review; approval denied")
    po["status"] = "approved"
    po["approval_status"] = "approved"
    po["approved_by"] = session["email"]
    po["approved_at"] = int(time.time())
    record("zoho-emulator", "PURCHASE_ORDER_APPROVED", user=session["email"], po_id=po_id, draft_hash=current_hash)
    with tracer.start_as_current_span("zoho.human_approval") as span:
        span.set_attribute("purchase_order.id", po_id)
        span.set_attribute("approval.user", session["email"])
    return {"message": "approved", "purchaseorder": po}
