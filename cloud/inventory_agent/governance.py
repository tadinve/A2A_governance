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

from . import zoho_mcp

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


def _broker_identity_token(audience: str) -> tuple[str, str]:
    """Mint a Google ID token for the Auth Broker, reporting how we got it.

    Agent Identity credentials are certificate-bound, so the strategies differ
    in whether they can be presented to a plain Cloud Run endpoint. Each is
    tried in turn and the one that worked is reported, because "which
    credential type reached the broker" is exactly what we need in the logs
    when this fails.
    """
    errors = []
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token as google_id_token

        request = google.auth.transport.requests.Request()
        return google_id_token.fetch_id_token(request, audience), "fetch_id_token"
    except Exception as exc:
        errors.append(f"fetch_id_token: {type(exc).__name__}")

    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default()
        if hasattr(credentials, "with_target_audience"):
            scoped = credentials.with_target_audience(audience)
            scoped.refresh(google.auth.transport.requests.Request())
            return scoped.token, "with_target_audience"
        errors.append("with_target_audience: unsupported credential type")
    except Exception as exc:
        errors.append(f"with_target_audience: {type(exc).__name__}")

    raise SigningUnavailable(
        "Could not obtain an ID token for the Auth Broker: " + "; ".join(errors))


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
    import requests as _requests

    payload = {
        "subject": subject, "audience": audience, "scopes": scopes,
        "token_kind": token_kind, "actor_chain": actor_chain,
        "lifetime_seconds": lifetime_seconds,
    }
    id_token_value, method = _broker_identity_token(AUTH_BROKER_URL)
    try:
        response = _requests.post(
            f"{AUTH_BROKER_URL}/sign", json=payload, timeout=60,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {id_token_value}"})
    except Exception as exc:
        raise SigningUnavailable(
            f"Auth Broker unreachable via {method}: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise SigningUnavailable(
            f"Auth Broker refused to mint this token (auth via {method}): "
            f"HTTP {response.status_code} {response.text[:200]}")
    return response.json()["token"]


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


# Live Zoho Inventory access -------------------------------------------------
#
# This replaces the in-process emulator that used to live here. Reads go through
# InvRead, writes through ProcureWrite, and neither server exposes an approval
# tool -- so approval stays a human action outside this agent's reach.
#
# One thing Zoho does not supply: a target stock level. Zoho tracks
# reorder_level (when to reorder) but not how much to reorder to. The order
# quantity is therefore business policy, not inventory data, and it lives below
# as explicit configuration. A SKU with no policy entry returns an unresolved
# state rather than a guessed quantity.

# Demo reorder policy. Override with ZOHO_REORDER_POLICY as JSON, e.g.
#   {"DEMO-WIDGET-A": {"target_stock": 100, "min_order_quantity": 1}}
DEFAULT_REORDER_POLICY: dict[str, dict[str, Any]] = {
    "DEMO-WIDGET-A": {"target_stock": 100, "min_order_quantity": 1},
}

_org_id: str | None = None


def reorder_policy() -> dict[str, dict[str, Any]]:
    raw = os.getenv("ZOHO_REORDER_POLICY", "").strip()
    if not raw:
        return DEFAULT_REORDER_POLICY
    try:
        return json.loads(raw)
    except Exception:
        return DEFAULT_REORDER_POLICY


def organization_id() -> str:
    """Resolve the Zoho organization once, refusing to guess between several."""
    global _org_id
    if _org_id:
        return _org_id
    configured = os.getenv("ZOHO_ORGANIZATION_ID", "").strip()
    if configured:
        _org_id = configured
        return _org_id
    orgs = zoho_mcp.list_organizations()
    if len(orgs) != 1:
        raise ZohoPolicyError(
            f"Zoho returned {len(orgs)} organizations. Set ZOHO_ORGANIZATION_ID "
            f"so this agent does not have to guess which one to use."
        )
    _org_id = orgs[0]["organization_id"]
    return _org_id


class ZohoPolicyError(Exception):
    """Inventory or policy data is missing; the caller must not guess."""


def find_item(sku: str) -> dict[str, Any] | None:
    """Look up an item by SKU through the read-only connector."""
    return zoho_mcp.find_item_by_sku(organization_id(), sku)


def available_stock(item: dict[str, Any]) -> float | None:
    """Stock we can actually commit against.

    Prefers Zoho's actual_available_stock, which already excludes committed
    stock, so subtracting committed again would double-count it. Returns None
    when Zoho supplies no stock field at all -- an untracked item is not a
    zero-stock item, and a tool failure is never zero stock.
    """
    for field in ("actual_available_stock", "available_stock", "stock_on_hand"):
        value = item.get(field)
        if value is not None:
            return float(value)
    return None


def incoming_quantity(item: dict[str, Any]) -> float:
    """Quantity already on order and not yet received.

    Counted so a second purchase order is not raised for stock that is already
    inbound. Zoho reports ordered and received quantities per line.
    """
    incoming = 0.0
    try:
        orders = zoho_mcp.list_item_purchase_orders(organization_id(), item["item_id"])
    except Exception:
        # Treated as unknown rather than zero by the caller below.
        raise
    for order in orders:
        status = str(order.get("status", "")).lower()
        if status in ("cancelled", "closed", "billed"):
            continue
        ordered = float(order.get("quantity_ordered") or order.get("quantity") or 0)
        received = float(order.get("quantity_received") or 0)
        incoming += max(ordered - received, 0.0)
    return incoming


def reorder_plan(item: dict[str, Any]) -> dict[str, Any]:
    """Decide whether to reorder and how much, or say why it cannot be decided."""
    sku = str(item.get("sku", "")).upper()
    available = available_stock(item)
    if available is None:
        return {"resolved": False,
                "reason": f"Zoho reports no stock field for {sku}; it may not be "
                          f"inventory-tracked. Not treating that as zero stock."}

    level = item.get("reorder_level")
    if level is None:
        return {"resolved": False,
                "reason": f"{sku} has no reorder_level set in Zoho."}
    level = float(level)

    policy = reorder_policy().get(sku)
    if not policy or policy.get("target_stock") is None:
        return {"resolved": False,
                "reason": f"No target stock configured for {sku}. Zoho stores "
                          f"reorder_level but not a target, so the order quantity "
                          f"is policy data that must be configured."}
    target = float(policy["target_stock"])

    try:
        incoming = incoming_quantity(item)
    except Exception as exc:
        return {"resolved": False,
                "reason": f"Could not read open purchase orders for {sku}: "
                          f"{type(exc).__name__}. Refusing to order without knowing "
                          f"what is already inbound."}

    # Strictly below, per the documented rule. At exactly the threshold we do
    # not reorder.
    below = available < level
    position = available + incoming
    shortfall = max(target - position, 0.0)
    # Zoho carries its own supplier constraints; prefer them over local policy.
    zoho_min = item.get("minimum_order_quantity")
    zoho_max = item.get("maximum_order_quantity")
    minimum = float(zoho_min) if zoho_min else float(policy.get("min_order_quantity") or 1)
    quantity = 0.0 if not below or shortfall <= 0 else max(shortfall, minimum)
    capped_by = None
    if quantity and zoho_max and float(zoho_max) > 0 and quantity > float(zoho_max):
        capped_by = float(zoho_max)
        quantity = capped_by
    quantity = int(quantity)

    return {
        "resolved": True,
        "reorder_needed": below and quantity > 0,
        "available_stock": available,
        "committed_stock": item.get("committed_stock"),
        "incoming_quantity": incoming,
        "reorder_level": level,
        "target_stock": target,
        "suggested_quantity": quantity,
        "minimum_order_quantity": minimum,
        "capped_by_maximum_order_quantity": capped_by,
        "rule": ("Reorder when available < reorder_level. Quantity brings "
                 "available + already-inbound up to target_stock. "
                 "actual_available_stock already nets off committed stock."),
    }


def vendor_for(item: dict[str, Any]) -> dict[str, Any]:
    """The approved vendor for an item, verified to be a vendor in Zoho."""
    vendor_id = item.get("vendor_id") or item.get("preferred_vendor_id")
    if not vendor_id:
        raise ZohoPolicyError(
            f"{item.get('sku')} has no preferred vendor in Zoho. Set one before "
            f"raising a purchase order; this agent will not choose a supplier."
        )
    return zoho_mcp.find_vendor(organization_id(), str(vendor_id))


def draft_hash(po: dict[str, Any]) -> str:
    """Approval binds to exactly these fields. Any mutation invalidates it."""
    approved = {
        "vendor_id": str(po.get("vendor_id", "")),
        "reference_number": po.get("reference_number"),
        "line_items": [
            {"item_id": str(line.get("item_id")),
             "quantity": float(line.get("quantity") or 0),
             "rate": float(line.get("rate") or 0)}
            for line in po.get("line_items", [])
        ],
        "total": float(po.get("total") or 0),
    }
    return hashlib.sha256(
        json.dumps(approved, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create_draft(item_id: str, quantity: int, rate: float, vendor_id: str,
                 reference_number: str, idempotency_key: str) -> dict[str, Any]:
    """Create a real purchase order in Zoho and submit it for human approval.

    Idempotency is by reference_number: before creating, we look for an existing
    order carrying the same reference. Zoho has no idempotency-key header, so the
    reference is the unique external key, and a retry after a lost response finds
    the original instead of raising a duplicate.
    """
    org = organization_id()

    try:
        existing = _find_by_reference(org, item_id, reference_number)
    except Exception as exc:
        raise ZohoPolicyError(
            f"Could not determine whether {reference_number} already exists "
            f"({type(exc).__name__}). Refusing to create a possible duplicate; "
            f"reconcile by reference number before retrying."
        ) from None
    if existing:
        existing["draft_hash"] = draft_hash(existing)
        existing["idempotent_replay"] = True
        return existing

    if quantity <= 0:
        raise ValueError("Quantity must be positive")

    created = zoho_mcp.create_purchase_order(org, {
        "vendor_id": str(vendor_id),
        "reference_number": reference_number,
        "line_items": [{"item_id": str(item_id), "quantity": quantity, "rate": rate}],
    })
    po_id = created.get("purchaseorder_id")
    if not po_id:
        raise ZohoPolicyError("Zoho accepted the create but returned no purchaseorder_id")

    # Submit moves it into Zoho's approval workflow. It is not approval: no
    # approval tool is exposed on any connector this agent can reach.
    try:
        submitted = zoho_mcp.submit_purchase_order(org, po_id)
        if submitted:
            created = submitted
    except Exception:
        created = zoho_mcp.get_purchase_order(org, po_id, server=zoho_mcp.PROCUREWRITE)

    created["draft_hash"] = draft_hash(created)
    created["idempotent_replay"] = False
    return created


def _find_by_reference(org: str, item_id: str, reference_number: str) -> dict[str, Any] | None:
    """Find an existing PO for this item carrying this reference number.

    list_item_purchase_orders returns a trimmed record whose reference_number is
    null, so each candidate has to be re-read in full before comparing. Matching
    against the list directly silently never matches, which would let a retry
    after a lost response create a second real purchase order -- the exact
    duplicate this guard exists to prevent.
    """
    try:
        for order in zoho_mcp.list_item_purchase_orders(org, item_id):
            po_id = order.get("purchaseorder_id")
            if not po_id:
                continue
            reference = order.get("reference_number")
            if reference is None:
                detail = zoho_mcp.get_purchase_order(
                    org, po_id, server=zoho_mcp.PROCUREWRITE)
                reference = detail.get("reference_number")
                if str(reference or "") == reference_number:
                    return detail
                continue
            if str(reference) == reference_number:
                return zoho_mcp.get_purchase_order(
                    org, po_id, server=zoho_mcp.PROCUREWRITE)
    except Exception:
        # Unknown rather than absent: the caller must not treat this as
        # "no existing order" and create another one.
        raise
    return None


def get_purchase_order(purchaseorder_id: str) -> dict[str, Any] | None:
    """Read a purchase order back from Zoho, not from process memory."""
    try:
        po = zoho_mcp.get_purchase_order(
            organization_id(), purchaseorder_id, server=zoho_mcp.PROCUREWRITE)
    except Exception:
        return None
    if po:
        po["draft_hash"] = draft_hash(po)
    return po or None
