"""Self-contained governance primitives for the deployed agents.

Agent Runtime uploads only the agent folder, so this mirrors the essentials of
src/governance_demo/security.py rather than importing them. The delegation,
policy, and approval logic is real and runs in-process; only the network hops
between control-plane services collapse into function calls.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import jwt

ISSUER = "https://identity.demo.local"
ALGORITHM = "RS256"

# A fixed committed demo keypair, so two separately deployed agents can verify
# each other's delegated tokens. Generating one per process would make
# cross-agent verification impossible. See demo_keys/README.md.
_KEY_DIR = Path(__file__).resolve().parent / "demo_keys"
_PRIVATE = (_KEY_DIR / "issuer_private.pem").read_bytes()
_PUBLIC = (_KEY_DIR / "issuer_public.pem").read_bytes()


class PolicyDenied(Exception):
    """Raised when no delegation policy permits a requested exchange."""


def issue_token(*, subject: str, audience: str, scopes: list[str], token_kind: str,
                actor_chain: dict[str, Any] | None = None, lifetime_seconds: int = 300) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER, "sub": subject, "aud": audience,
        "iat": now, "nbf": now, "exp": now + lifetime_seconds,
        "scope": " ".join(scopes), "token_kind": token_kind,
    }
    if actor_chain:
        claims["act"] = actor_chain
    return jwt.encode(claims, _PRIVATE, algorithm=ALGORITHM)


def decode_token(token: str, audience: str | None = None) -> dict[str, Any]:
    return jwt.decode(token, _PUBLIC, algorithms=[ALGORITHM], audience=audience,
                      issuer=ISSUER, options={"verify_aud": audience is not None})


def token_scopes(claims: dict[str, Any]) -> set[str]:
    return set(str(claims.get("scope", "")).split())


def extend_actor_chain(actor: str, subject_claims: dict[str, Any]) -> dict[str, Any]:
    chain: dict[str, Any] = {"sub": actor}
    if subject_claims.get("act"):
        chain["act"] = subject_claims["act"]
    return chain


def public_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """Claims safe to display. Never return a raw token to a model or a user."""
    return {k: claims.get(k) for k in ("iss", "sub", "aud", "scope", "token_kind", "act", "exp")
            if k in claims}


# Mirrors config/policies.json. An exchange not listed here is denied.
TOKEN_EXCHANGE = [
    {"actor": "inventory-agent", "subject_audience": "inventory-agent",
     "subject_scope": "assistant.inventory", "target_audience": "zoho-inventory-mcp",
     "output_scope": "inventory.read"},
    {"actor": "inventory-agent", "subject_audience": "inventory-agent",
     "subject_scope": "assistant.inventory", "target_audience": "procurement-agent",
     "output_scope": "purchase.request"},
    {"actor": "inventory-agent", "subject_audience": "inventory-agent",
     "subject_scope": "assistant.inventory", "target_audience": "procurement-agent",
     "output_scope": "purchase.status"},
    {"actor": "procurement-agent", "subject_audience": "procurement-agent",
     "subject_scope": "purchase.request", "target_audience": "zoho-procurement-mcp",
     "output_scope": "purchaseorder.create"},
    {"actor": "procurement-agent", "subject_audience": "procurement-agent",
     "subject_scope": "purchase.status", "target_audience": "zoho-procurement-mcp",
     "output_scope": "purchaseorder.read"},
]


def exchange_token(actor: str, subject_token: str, target_audience: str, scope: str) -> dict[str, Any]:
    """RFC 8693-shaped exchange. Raises PolicyDenied when no rule matches."""
    subject_claims = decode_token(subject_token)
    aud = subject_claims.get("aud")
    audiences = {aud} if isinstance(aud, str) else set(aud or [])
    matched = next((r for r in TOKEN_EXCHANGE
                    if r["actor"] == actor
                    and r["subject_audience"] in audiences
                    and r["subject_scope"] in token_scopes(subject_claims)
                    and r["target_audience"] == target_audience
                    and r["output_scope"] == scope), None)
    if not matched:
        raise PolicyDenied(
            f"No delegation policy permits {actor} to obtain '{scope}' for "
            f"'{target_audience}'. This is the same denial the local Identity "
            f"Broker returns with HTTP 403."
        )
    delegated = issue_token(subject=subject_claims["sub"], audience=target_audience,
                            scopes=scope.split(), token_kind="delegated_access_token",
                            actor_chain=extend_actor_chain(actor, subject_claims))
    return {"access_token": delegated, "claims": public_claims(decode_token(delegated, audience=target_audience))}


def human_token(user_id: str = "demo-user") -> str:
    """The human sign-in that starts every delegation chain."""
    return issue_token(subject=user_id, audience="inventory-agent",
                       scopes=["assistant.inventory"], token_kind="user_access_token",
                       lifetime_seconds=900)


# Zoho Inventory emulation -------------------------------------------------

ORGANIZATION_ID = "demo-org-1001"
VENDOR = {"vendor_id": "vendor-2001", "vendor_name": "CloudKarya Demo Supplier"}
ITEMS = {
    "CK-GPU-42": {
        "item_id": "item-4242", "sku": "CK-GPU-42", "name": "CloudKarya GPU Node",
        "stock_on_hand": 27, "reorder_level": 50, "target_stock": 100,
        "purchase_rate": 245.00, "preferred_vendor_id": VENDOR["vendor_id"],
    }
}
PURCHASE_ORDERS: dict[str, dict[str, Any]] = {}
_IDEMPOTENCY: dict[str, str] = {}


def draft_hash(po: dict[str, Any]) -> str:
    """Approval binds to exactly these fields. Any mutation invalidates it."""
    approved = {"vendor_id": po["vendor_id"], "reference_number": po["reference_number"],
                "line_items": po["line_items"], "total": po["total"]}
    return hashlib.sha256(json.dumps(approved, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create_draft(item_id: str, quantity: int, rate: float, vendor_id: str,
                 reference_number: str, idempotency_key: str) -> dict[str, Any]:
    if idempotency_key in _IDEMPOTENCY:
        return PURCHASE_ORDERS[_IDEMPOTENCY[idempotency_key]]
    item = next((v for v in ITEMS.values() if v["item_id"] == item_id), None)
    if not item or quantity <= 0 or rate != item["purchase_rate"]:
        raise ValueError("Invalid item, quantity, or approved purchase rate")
    if vendor_id != VENDOR["vendor_id"]:
        raise ValueError("Vendor is not on the approved vendor list")
    po_id = f"PO-{len(PURCHASE_ORDERS) + 1001}"
    po = {
        "purchaseorder_id": po_id, "vendor_id": vendor_id, "vendor_name": VENDOR["vendor_name"],
        "reference_number": reference_number,
        "line_items": [{"item_id": item_id, "sku": item["sku"], "quantity": quantity, "rate": rate}],
        "total": round(quantity * rate, 2), "status": "submitted",
        "approval_status": "pending_approval",
    }
    po["draft_hash"] = draft_hash(po)
    PURCHASE_ORDERS[po_id] = po
    _IDEMPOTENCY[idempotency_key] = po_id
    return po
