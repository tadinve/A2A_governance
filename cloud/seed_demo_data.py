#!/usr/bin/env python3
"""Seed the demo vendor, item, and opening stock into live Zoho Inventory.

Operator-only. This script uses the DemoSetup MCP server, which is the only one
of the three that can create contacts, items, and inventory adjustments. That
credential is deliberately NOT stored in Secret Manager and NOT granted to any
agent: seeding is a human setup action, and no agent should ever hold the
ability to invent inventory. Agents get InvRead and ProcureWrite only.

Idempotent by construction, so re-running is safe:

* the vendor and item are looked up by name/SKU and reused if present;
* stock converges on a target rather than being added. The adjustment is for
  (target - current), so running twice does not double the stock. A run that
  finds stock already at target makes no adjustment at all.

Verification reads back through InvRead -- the same read path the agents use --
rather than trusting the write path's own response.

  python3 cloud/seed_demo_data.py             # show the plan, change nothing
  python3 cloud/seed_demo_data.py --apply     # create what is missing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

import requests


DEMOSETUP = "DemoSetup"
INVREAD = "InvRead"
ENV_VARS = {
    DEMOSETUP: "ZOHO_DEMOSETUP_MCP_URL",
    INVREAD: "ZOHO_INVREAD_MCP_URL",
}

VENDOR_NAME = "Demo Widget Supplies"
ITEM_SKU = "DEMO-WIDGET-A"
ITEM_NAME = "Demo Widget A"
PURCHASE_RATE = 10.0
TARGET_STOCK = 30
# Not part of the requested seed. Chosen so stock (30) sits strictly below the
# threshold and the replenishment path triggers, mirroring the original 27/50
# demo shape. The order quantity comes from policy, not from this number.
REORDER_LEVEL = 50
SEED_REFERENCE = f"DEMO-SEED-{ITEM_SKU}"

_urls: dict[str, str] = {}


def _scrub(text: object) -> str:
    out = str(text)
    for url in _urls.values():
        parsed = urlparse(url)
        for secret in (url, parsed.netloc, parsed.netloc.split(".")[0]):
            if secret:
                out = out.replace(secret, "<redacted>")
        for segment in parsed.path.split("/"):
            if len(segment) >= 8:
                out = out.replace(segment, "<redacted>")
    return out


def _url(server: str) -> str:
    if server not in _urls:
        value = os.environ.get(ENV_VARS[server], "").strip()
        if not value:
            sys.exit(f"Set {ENV_VARS[server]} (source .env_zoho_urls) before running.")
        _urls[server] = value
    return _urls[server]


def call(server: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}
    try:
        response = requests.post(
            _url(server), json=body, timeout=90,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        response.raise_for_status()
        envelope = response.json()
    except Exception as exc:
        sys.exit(f"{server}.{tool} transport failure: {_scrub(exc)[:300]}")
    if "error" in envelope:
        sys.exit(f"{server}.{tool} error: {_scrub(envelope['error'])[:400]}")
    content = envelope.get("result", {}).get("content", [])
    if not content:
        sys.exit(f"{server}.{tool} returned no content")
    payload = json.loads(content[0]["text"])
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        sys.exit(f"{server}.{tool} rejected: {_scrub(payload.get('message', payload))[:400]}")
    return payload


def organization() -> str:
    orgs = call(DEMOSETUP, "ZohoInventory_list_organizations", {}).get("organizations", [])
    if len(orgs) != 1:
        sys.exit(f"Expected exactly one organization, found {len(orgs)}. Refusing to guess.")
    print(f"organization : {orgs[0]['name']} ({orgs[0]['organization_id']})")
    return orgs[0]["organization_id"]


def find_vendor(org: str) -> dict[str, Any] | None:
    contacts = call(DEMOSETUP, "ZohoInventory_list_contacts", {
        "query_params": {"organization_id": org, "per_page": "200"}}).get("contacts", [])
    for contact in contacts:
        if contact.get("contact_name", "").strip().lower() == VENDOR_NAME.lower():
            return contact
    return None


def find_item(org: str) -> dict[str, Any] | None:
    items = call(DEMOSETUP, "ZohoInventory_list_items", {
        "query_params": {"organization_id": org, "per_page": "200"}}).get("items", [])
    for item in items:
        if str(item.get("sku", "")).strip().upper() == ITEM_SKU:
            return item
    return None


def stock_of(item: dict[str, Any]) -> float | None:
    """Zoho omits stock fields on non-inventory items. Absent is not zero."""
    for field in ("stock_on_hand", "available_stock", "actual_available_stock"):
        if field in item and item[field] is not None:
            return float(item[field])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="perform writes; without it the script only reports a plan")
    args = parser.parse_args()
    apply = args.apply

    print(f"mode         : {'APPLY (writes to live Zoho)' if apply else 'DRY RUN (no writes)'}")
    org = organization()

    # --- vendor -------------------------------------------------------------
    vendor = find_vendor(org)
    if vendor:
        kind = str(vendor.get("contact_type", "")).lower()
        print(f"vendor       : REUSE {vendor['contact_id']} ({vendor['contact_name']}, type={kind})")
        if kind != "vendor":
            sys.exit(f"Existing contact {vendor['contact_id']} is type {kind!r}, not 'vendor'. "
                     f"Rename or fix it in Zoho; refusing to create a duplicate.")
    elif not apply:
        print(f"vendor       : WOULD CREATE {VENDOR_NAME!r} (contact_type=vendor)")
    else:
        created = call(DEMOSETUP, "ZohoInventory_create_contact", {
            "query_params": {"organization_id": org},
            "body": {"contact_name": VENDOR_NAME, "company_name": VENDOR_NAME,
                     "contact_type": "vendor"}}).get("contact", {})
        vendor = created
        print(f"vendor       : CREATED {created.get('contact_id')} ({created.get('contact_name')})")

    vendor_id = vendor.get("contact_id") if vendor else None

    # --- item ---------------------------------------------------------------
    item = find_item(org)
    if item:
        print(f"item         : REUSE {item['item_id']} (sku={item.get('sku')}, "
              f"rate={item.get('purchase_rate')}, reorder={item.get('reorder_level')})")
    elif not apply:
        print(f"item         : WOULD CREATE {ITEM_NAME!r} sku={ITEM_SKU} "
              f"purchase_rate={PURCHASE_RATE} reorder_level={REORDER_LEVEL} tracked=True")
    else:
        body = {"name": ITEM_NAME, "sku": ITEM_SKU, "item_type": "inventory",
                "product_type": "goods", "purchase_rate": PURCHASE_RATE,
                "rate": PURCHASE_RATE, "reorder_level": REORDER_LEVEL,
                "track_inventory": True, "can_be_purchased": True, "unit": "qty"}
        if vendor_id:
            body["vendor_id"] = vendor_id
        created = call(DEMOSETUP, "ZohoInventory_create_item", {
            "query_params": {"organization_id": org}, "body": body}).get("item", {})
        item = created
        print(f"item         : CREATED {created.get('item_id')} (sku={created.get('sku')})")

    if not item:
        print("\nDRY RUN complete. Re-run with --apply to create these records.")
        return 0

    # --- stock, by convergence rather than addition --------------------------
    current = stock_of(item)
    if current is None:
        print("stock        : UNKNOWN - Zoho returned no stock field for this item. "
              "It may not be inventory-tracked; not treating that as zero.")
        return 1
    delta = TARGET_STOCK - current
    print(f"stock        : current={current:g} target={TARGET_STOCK} delta={delta:+g}")

    if abs(delta) < 0.0001:
        print("             : already at target, no adjustment made")
    elif delta < 0:
        print(f"             : current stock exceeds target by {-delta:g}; "
              f"refusing to remove stock automatically")
    elif not apply:
        print(f"             : WOULD ADJUST +{delta:g} (reference {SEED_REFERENCE})")
    else:
        adjustment = call(DEMOSETUP, "ZohoInventory_create_inventory_adjustment", {
            "query_params": {"organization_id": org},
            "body": {
                "date": dt.date.today().isoformat(),
                "reason": "Demo seed opening stock",
                "adjustment_type": "quantity",
                "reference_number": SEED_REFERENCE,
                "line_items": [{"item_id": item["item_id"], "quantity_adjusted": delta}],
            }}).get("inventory_adjustment", {})
        print(f"             : ADJUSTED +{delta:g} "
              f"(adjustment {adjustment.get('inventory_adjustment_id', '?')})")

    if not apply:
        print("\nDRY RUN complete. Re-run with --apply to perform the writes above.")
        return 0

    # --- verify through the agents' own read path ----------------------------
    print("\nverifying through InvRead (the path the agents use):")
    items = call(INVREAD, "ZohoInventory_list_items", {
        "query_params": {"organization_id": org, "per_page": "200"}}).get("items", [])
    seen = next((i for i in items if str(i.get("sku", "")).upper() == ITEM_SKU), None)
    if not seen:
        print(f"  FAIL: {ITEM_SKU} is not visible through InvRead")
        return 1
    print(f"  item_id        : {seen.get('item_id')}")
    print(f"  sku / name     : {seen.get('sku')} / {seen.get('name')}")
    print(f"  purchase_rate  : {seen.get('purchase_rate')}")
    print(f"  reorder_level  : {seen.get('reorder_level')}")
    for field in ("stock_on_hand", "available_stock", "actual_available_stock", "committed_stock"):
        print(f"  {field:<15}: {seen.get(field, '(absent)')}")
    below = stock_of(seen) is not None and stock_of(seen) < float(seen.get("reorder_level") or 0)
    print(f"  below reorder  : {below}  <- replenishment {'will' if below else 'will NOT'} trigger")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
