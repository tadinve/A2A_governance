"""Self-contained governance primitives for the deployed agents.

Agent Runtime uploads only the agent folder, so this mirrors the essentials of
src/governance_demo/security.py rather than importing them. The delegation,
policy, and approval logic is real and runs in-process; only the network hops
between control-plane services collapse into function calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import jwt

ISSUER = "https://identity.demo.local"
ALGORITHM = "RS256"

class IssuerKeyUnavailable(Exception):
    """Raised when the delegation public key cannot be fetched."""


class SigningUnavailable(Exception):
    """Raised when the Auth Broker will not mint a requested token."""


AUTH_BROKER_URL = os.getenv("AUTH_BROKER_URL", "").rstrip("/")
KMS_SIGNING_KEY = os.getenv("KMS_SIGNING_KEY", "")
BROKER_CLIENT_ID = os.getenv("BROKER_CLIENT_ID", "")
BROKER_CLIENT_SECRET = os.getenv("BROKER_CLIENT_SECRET", "")


def _load_verification_key() -> bytes:
    """Fetch the *public* half of the delegation signing key.

    Verification never needs private key material. With a KMS-backed key the
    public half is readable with roles/cloudkms.publicKeyViewer, a strictly
    weaker grant than the roles/cloudkms.signerVerifier the Auth Broker holds.
    That is what lets Procurement Agent check a token while remaining unable to
    mint one -- the property the old shared-secret design could not express,
    because possession of the key granted both.
    """
    if KMS_SIGNING_KEY:
        from google.cloud import kms

        client = kms.KeyManagementServiceClient()
        return client.get_public_key(request={"name": KMS_SIGNING_KEY}).pem.encode()
    if AUTH_BROKER_URL:
        import urllib.request

        with urllib.request.urlopen(f"{AUTH_BROKER_URL}/jwks", timeout=15) as response:
            return json.loads(response.read())["public_key_pem"].encode()
    raise IssuerKeyUnavailable(
        "Set KMS_SIGNING_KEY (preferred) or AUTH_BROKER_URL so this agent can fetch "
        "the delegation public key. Create the key with cloud/setup_kms_signing.py."
    )


# Loaded lazily and cached: importing an agent must not require network access,
# so the module stays importable for packaging, tests, and offline inspection.
_PUBLIC_KEY: bytes | None = None


def _public_key() -> bytes:
    global _PUBLIC_KEY
    if _PUBLIC_KEY is None:
        _PUBLIC_KEY = _load_verification_key()
    return _PUBLIC_KEY


class PolicyDenied(Exception):
    """Raised when no delegation policy permits a requested exchange."""


def request_token(*, subject: str, audience: str, scopes: list[str], token_kind: str,
                  actor_chain: dict[str, Any] | None = None,
                  lifetime_seconds: int = 300) -> str:
    """Ask the Auth Broker to sign a token. This agent holds no signing key.

    The broker re-checks its own minting policy, so a compromised agent can
    obtain only what policy already allows it and cannot forge anything else.
    """
    if not AUTH_BROKER_URL:
        raise SigningUnavailable(
            "Set AUTH_BROKER_URL. Deployed agents do not sign their own tokens; "
            "only the Auth Broker can call KMS asymmetricSign."
        )
    import base64
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "subject": subject, "audience": audience, "scopes": scopes,
        "token_kind": token_kind, "actor_chain": actor_chain,
        "lifetime_seconds": lifetime_seconds,
    }).encode()
    credentials = base64.b64encode(
        f"{BROKER_CLIENT_ID}:{BROKER_CLIENT_SECRET}".encode()).decode()
    request = urllib.request.Request(
        f"{AUTH_BROKER_URL}/sign", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Basic {credentials}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())["token"]
    except urllib.error.HTTPError as exc:
        raise SigningUnavailable(
            f"Auth Broker refused to mint this token: HTTP {exc.code}") from exc


def decode_token(token: str, audience: str | None = None) -> dict[str, Any]:
    return jwt.decode(token, _public_key(), algorithms=[ALGORITHM], audience=audience,
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
    delegated = request_token(subject=subject_claims["sub"], audience=target_audience,
                            scopes=scope.split(), token_kind="delegated_access_token",
                            actor_chain=extend_actor_chain(actor, subject_claims))
    return {"access_token": delegated, "claims": public_claims(decode_token(delegated, audience=target_audience))}


def human_token(user_id: str = "demo-user") -> str:
    """The human sign-in that starts every delegation chain."""
    return request_token(subject=user_id, audience="inventory-agent",
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
