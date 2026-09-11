"""Live Zoho Inventory access through the two scoped MCP servers.

This replaces the in-process emulator that governance.py used to carry. Two
servers, two scopes, and the split is the point:

    InvRead       read-only: list/get items, list/get purchase orders
    ProcureWrite  adds create_purchase_order and submit_purchaseorder

Neither server exposes an approval, issue, or send-to-supplier tool. That is a
property of how the servers were configured, verified by tool discovery -- so
approval necessarily remains a human action outside every agent's toolset. It is
*not* proof of authorization isolation: both servers are authorized by the same
Zoho account, so InvRead's read-only surface is a scoping choice, not a
separate Zoho identity.

The server URLs embed an API key in their path. They are read from Secret
Manager using this agent's own Agent Identity, are never logged, and are scrubbed
from any exception this module raises.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any
from urllib.parse import urlparse

import requests


INVREAD = "InvRead"
PROCUREWRITE = "ProcureWrite"

# Secret Manager secret ids holding each server's full URL.
SECRET_IDS = {
    INVREAD: "zoho-invread-mcp-url",
    PROCUREWRITE: "zoho-procurewrite-mcp-url",
}
# Env fallback, for local development only.
ENV_VARS = {
    INVREAD: "ZOHO_INVREAD_MCP_URL",
    PROCUREWRITE: "ZOHO_PROCUREWRITE_MCP_URL",
}

_endpoints: dict[str, str] = {}
_lock = threading.Lock()


class ZohoUnavailable(Exception):
    """The MCP endpoint could not be resolved or reached."""


class ZohoError(Exception):
    """Zoho returned an error for a well-formed call."""


def _scrub(text: object, url: str | None = None) -> str:
    """Remove anything credential-bearing before text can reach a log."""
    out = str(text)
    for endpoint in list(_endpoints.values()) + ([url] if url else []):
        if not endpoint:
            continue
        parsed = urlparse(endpoint)
        for secret in (endpoint, parsed.netloc, parsed.netloc.split(".")[0]):
            if secret:
                out = out.replace(secret, "<redacted>")
        for segment in parsed.path.split("/"):
            if len(segment) >= 8:
                out = out.replace(segment, "<redacted>")
    return out


def _resolve(server: str) -> str:
    """Fetch a server URL from Secret Manager, falling back to the environment."""
    with _lock:
        if server in _endpoints:
            return _endpoints[server]

        url = os.getenv(ENV_VARS[server], "").strip()
        if not url:
            project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
            if not project:
                raise ZohoUnavailable(
                    f"Set {ENV_VARS[server]} or GOOGLE_CLOUD_PROJECT so "
                    f"{SECRET_IDS[server]} can be read from Secret Manager."
                )
            try:
                from google.cloud import secretmanager

                client = secretmanager.SecretManagerServiceClient()
                name = f"projects/{project}/secrets/{SECRET_IDS[server]}/versions/latest"
                url = client.access_secret_version(name=name).payload.data.decode().strip()
            except Exception as exc:
                raise ZohoUnavailable(
                    f"Could not read {SECRET_IDS[server]} from Secret Manager: "
                    f"{type(exc).__name__}. Run cloud/setup_zoho_secrets.py and confirm "
                    f"this agent's Agent Identity holds roles/secretmanager.secretAccessor "
                    f"on that secret."
                ) from None

        if not url:
            raise ZohoUnavailable(f"{server} MCP URL resolved empty")
        _endpoints[server] = url
        return url


def call(server: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke one MCP tool and return its parsed payload.

    These servers accept a bare ``tools/call`` with no initialize handshake and
    no session id, so this stays a single POST with no client dependency beyond
    requests -- which the agent already ships.
    """
    url = _resolve(server)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    try:
        response = requests.post(
            url,
            json=body,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            timeout=60,
        )
        response.raise_for_status()
        envelope = response.json()
    except Exception as exc:
        raise ZohoUnavailable(f"{server}.{tool} failed: {_scrub(exc, url)[:300]}") from None

    if "error" in envelope:
        raise ZohoError(f"{server}.{tool}: {_scrub(envelope['error'])[:300]}")

    content = envelope.get("result", {}).get("content", [])
    if not content:
        raise ZohoError(f"{server}.{tool} returned no content")
    try:
        payload = json.loads(content[0].get("text", ""))
    except Exception:
        raise ZohoError(f"{server}.{tool} returned non-JSON content") from None

    # Zoho signals business errors with a non-zero code inside a 200 response.
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        raise ZohoError(f"{server}.{tool}: {_scrub(payload.get('message', payload))[:300]}")
    return payload


# --- reads (InvRead) --------------------------------------------------------

def list_organizations() -> list[dict[str, Any]]:
    return call(INVREAD, "ZohoInventory_list_organizations", {}).get("organizations", [])


def list_items(organization_id: str, **filters: Any) -> list[dict[str, Any]]:
    query = {"organization_id": organization_id, "per_page": "200", **filters}
    return call(INVREAD, "ZohoInventory_list_items",
                {"query_params": query}).get("items", [])


def get_item(organization_id: str, item_id: str) -> dict[str, Any]:
    return call(INVREAD, "ZohoInventory_get_item", {
        "query_params": {"organization_id": organization_id},
        "path_variables": {"item_id": item_id},
    }).get("item", {})


def find_item_by_sku(organization_id: str, sku: str) -> dict[str, Any] | None:
    """Resolve a SKU to its full item record.

    Zoho has no get-by-SKU tool, so the SKU is matched client-side over the item
    list -- but list_items returns a trimmed record that omits vendor_id,
    committed_stock, and the order-quantity limits. The match is therefore
    followed by get_item, so callers always see the complete record.
    """
    target = sku.strip().upper()
    for item in list_items(organization_id):
        if str(item.get("sku", "")).strip().upper() == target:
            detailed = get_item(organization_id, item["item_id"])
            return detailed or item
    return None


def list_item_purchase_orders(organization_id: str, item_id: str) -> list[dict[str, Any]]:
    """Open purchase orders already covering this item -- incoming supply that
    must be counted before proposing a new order."""
    payload = call(INVREAD, "ZohoInventory_list_item_purchase_orders", {
        "query_params": {"organization_id": organization_id, "item_id": item_id},
    })
    return payload.get("purchaseorders", []) or payload.get("purchase_orders", [])


def get_purchase_order(organization_id: str, purchaseorder_id: str,
                       server: str = INVREAD) -> dict[str, Any]:
    return call(server, "ZohoInventory_get_purchase_order", {
        "query_params": {"organization_id": organization_id},
        "path_variables": {"purchaseorder_id": purchaseorder_id},
    }).get("purchaseorder", {})


# --- vendors and writes (ProcureWrite) --------------------------------------

def list_contacts(organization_id: str, **filters: Any) -> list[dict[str, Any]]:
    query = {"organization_id": organization_id, "per_page": "200", **filters}
    return call(PROCUREWRITE, "ZohoInventory_list_contacts",
                {"query_params": query}).get("contacts", [])


def get_contact(organization_id: str, contact_id: str) -> dict[str, Any]:
    return call(PROCUREWRITE, "ZohoInventory_get_contact", {
        "query_params": {"organization_id": organization_id},
        "path_variables": {"contact_id": contact_id},
    }).get("contact", {})


def find_vendor(organization_id: str, contact_id: str) -> dict[str, Any]:
    """Fetch a contact and refuse it unless Zoho says it is a vendor.

    There is no contact_type filter on list_contacts, so the check has to happen
    on the returned record rather than in the query.
    """
    contact = get_contact(organization_id, contact_id)
    if str(contact.get("contact_type", "")).lower() != "vendor":
        raise ZohoError(
            f"Contact {contact_id} is contact_type="
            f"{contact.get('contact_type')!r}, not 'vendor'"
        )
    return contact


def create_purchase_order(organization_id: str, body: dict[str, Any],
                          ignore_auto_number_generation: bool = False) -> dict[str, Any]:
    """Create a PO. Zoho decides the initial status; the schema has no status field."""
    query: dict[str, Any] = {"organization_id": organization_id}
    if ignore_auto_number_generation:
        query["ignore_auto_number_generation"] = "true"
    return call(PROCUREWRITE, "ZohoInventory_create_purchase_order", {
        "query_params": query, "body": body,
    }).get("purchaseorder", {})


def submit_purchase_order(organization_id: str, purchaseorder_id: str) -> dict[str, Any]:
    """Submit a PO into Zoho's approval workflow. This is not approval."""
    return call(PROCUREWRITE, "ZohoInventory_submit_purchaseorder", {
        "query_params": {"organization_id": organization_id},
        "path_variables": {"purchaseorder_id": purchaseorder_id},
    }).get("purchaseorder", {})
