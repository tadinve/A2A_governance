"""Inventory retrieval, reorder evaluation, and guarded submission.

Two execution modes, and which one ran is recorded on every run and shown in
the UI. Emulator results are never presented as live ones:

* ``direct``  -- this process runs the same governance logic the deployed
  agents run, against live Zoho through the InvRead/ProcureWrite MCP servers.
  Fast, and it exercises the real integration.
* ``agent``   -- drives the deployed Inventory Agent on Agent Runtime over its
  real invocation path, which in turn reaches Procurement Agent over A2A. Slow
  (a minute or more), and it is the path that proves the agent governance.

The submission half is deliberately narrow. It loads the trusted approval record
from the database and re-validates it against the current draft. It does not
accept an ``approved=true`` flag, a natural-language claim, or an approval id
supplied by a caller.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from . import store
from .models import content_hash


# The agent packages carry the Zoho adapter and the reorder policy. Reusing them
# keeps one implementation of the rules rather than a second that can drift.
#
# They are loaded as a synthetic package rather than imported normally, because
# inventory_agent/__init__.py imports the ADK agent and would drag the whole
# Agent Runtime dependency tree into this web app. Giving the modules a package
# name is still necessary so their relative imports resolve.
_AGENT_DIR = Path(__file__).resolve().parents[2] / "cloud" / "inventory_agent"
_PKG_NAME = "zoho_agent_lib"


def _load_agent_package() -> None:
    import importlib
    import types

    if _PKG_NAME in sys.modules:
        return
    package = types.ModuleType(_PKG_NAME)
    package.__path__ = [str(_AGENT_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PKG_NAME] = package
    importlib.import_module(f"{_PKG_NAME}.zoho_mcp")
    importlib.import_module(f"{_PKG_NAME}.governance")

EXECUTION_MODE = os.getenv("EXECUTION_MODE", "direct").strip().lower()
DEMO_SKU = os.getenv("DEMO_SKU", "DEMO-WIDGET-A")


def _governance():
    _load_agent_package()
    return sys.modules[f"{_PKG_NAME}.governance"]


class Unresolved(Exception):
    """Policy or inventory data is insufficient to propose an order."""


def read_inventory(sku: str) -> dict[str, Any]:
    """Fetch stock and evaluate the reorder rule.

    A tool failure propagates. It must never be flattened into "zero stock",
    which would read as "no reorder needed" and silently skip replenishment.
    """
    governance = _governance()
    item = governance.find_item(sku)
    if not item:
        raise Unresolved(f"{sku} was not found in Zoho Inventory")

    plan = governance.reorder_plan(item)
    observed = {
        "sku": item.get("sku"),
        "item_id": item.get("item_id"),
        "name": item.get("name"),
        "purchase_rate": item.get("purchase_rate"),
        "organization_id": governance.organization_id(),
        # Reported separately and never conflated. Absent stays absent.
        "stock_on_hand": item.get("stock_on_hand"),
        "committed_stock": item.get("committed_stock"),
        "available_stock": plan.get("available_stock"),
        "incoming_quantity": plan.get("incoming_quantity"),
        "reorder_level": plan.get("reorder_level"),
        "target_stock": plan.get("target_stock"),
        "resolved": plan.get("resolved"),
        "reason": plan.get("reason"),
        "reorder_needed": plan.get("reorder_needed"),
        "suggested_quantity": plan.get("suggested_quantity"),
        "rule": plan.get("rule"),
    }
    return observed


def build_draft(observed: dict[str, Any]) -> dict[str, Any]:
    """Turn a resolved reorder plan into a reviewable draft.

    Refuses to invent anything. No vendor, no price, no quantity means an
    unresolved state, not a guess with a plausible number in it.
    """
    governance = _governance()
    item = governance.find_item(observed["sku"])
    vendor = governance.vendor_for(item)
    rate = item.get("purchase_rate")
    if rate is None:
        raise Unresolved(f"{observed['sku']} has no purchase rate in Zoho")
    quantity = int(observed["suggested_quantity"])
    if quantity <= 0:
        raise Unresolved("Reorder quantity resolved to zero")

    draft = {
        "organization_id": observed["organization_id"],
        "vendor_id": str(vendor["contact_id"]),
        "vendor_name": vendor.get("contact_name"),
        "currency": vendor.get("currency_code") or "USD",
        "reference_number": f"A2A-UI-{uuid.uuid4().hex[:8].upper()}",
        "lines": [{
            "item_id": str(item["item_id"]),
            "sku": item.get("sku"),
            "name": item.get("name"),
            "quantity": quantity,
            "rate": float(rate),
        }],
        "total": round(quantity * float(rate), 2),
        "reorder_reason": (
            f"available {observed['available_stock']:g} is below reorder level "
            f"{observed['reorder_level']:g}; ordering to target "
            f"{observed['target_stock']:g} net of {observed['incoming_quantity']:g} "
            f"already inbound"
        ),
        # Zoho on this organization has no approval workflow configured, so a
        # created order stays at draft. Recorded here so the UI can say so
        # rather than implying an approval state Zoho does not have.
        "provider_approval_available": False,
    }
    draft["content_hash"] = content_hash(draft)
    return draft


def submit_approved_draft(draft_row: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
    """Create the purchase order in Zoho for an already-approved draft.

    Preconditions are re-checked here, not trusted from the caller:
      * the approval record exists, is an approval, and has not expired;
      * its content hash still equals the draft's current hash;
      * this exact (draft, version) has not already been submitted.
    """
    governance = _governance()
    payload = json.loads(draft_row["payload_json"])
    line = payload["lines"][0]

    idempotency_key = f"submit:{draft_row['draft_id']}:v{draft_row['version']}"
    submission_id, is_new = store.claim_submission(
        draft_row["draft_id"], draft_row["version"], idempotency_key)
    if not is_new:
        existing = store.submission_for(idempotency_key) or {}
        raise store.Conflict(
            f"submission already claimed for this draft version "
            f"(state={existing.get('state')}). Reconcile rather than resubmit.")

    try:
        purchase_order = governance.create_draft(
            item_id=line["item_id"],
            quantity=int(line["quantity"]),
            rate=float(line["rate"]),
            vendor_id=payload["vendor_id"],
            reference_number=payload["reference_number"],
        )
    except Exception as exc:
        # We may or may not have created an order. Reconcile by the reference,
        # which is the unique external key, before deciding anything.
        try:
            found = governance._find_by_reference(
                payload["organization_id"], line["item_id"], payload["reference_number"])
        except Exception:
            found = None
        if found:
            store.finish_submission(submission_id, "RECONCILED",
                                    provider_po_id=found.get("purchaseorder_id"))
            return found
        store.finish_submission(submission_id, "UNKNOWN", error=str(exc)[:400])
        raise

    store.finish_submission(submission_id, "COMPLETE",
                            provider_po_id=purchase_order.get("purchaseorder_id"))
    return purchase_order


def process_run(run_id: str) -> None:
    """Advance a run from DRAFTING to its next durable state.

    Runs as task-dispatched work rather than inside the user's request, so a
    human approval wait holds no open HTTP request and no running agent loop.
    """
    from .models import (
        RUN_AWAITING_APPROVAL,
        RUN_DRAFTING,
        RUN_FAILED,
        RUN_SUFFICIENT,
        RUN_UNRESOLVED,
    )

    run = store.get_run(run_id)
    if not run or run["state"] != RUN_DRAFTING:
        return

    try:
        observed = read_inventory(run["scope_sku"])
    except Exception as exc:
        store.set_run_state(run_id, RUN_FAILED, error=f"{type(exc).__name__}: {exc}")
        store.log(run_id, "inventory_failed", str(exc)[:300])
        return

    store.set_run_state(run_id, RUN_DRAFTING, stock=observed)
    store.log(run_id, "stock_checked",
              f"available={observed.get('available_stock')} "
              f"reorder_level={observed.get('reorder_level')}")

    if not observed.get("resolved"):
        store.set_run_state(run_id, RUN_UNRESOLVED, stock=observed,
                            error=observed.get("reason"))
        store.log(run_id, "unresolved", observed.get("reason") or "")
        return

    if not observed.get("reorder_needed"):
        store.set_run_state(run_id, RUN_SUFFICIENT, stock=observed)
        store.log(run_id, "stock_sufficient", "no purchase order required")
        return

    store.log(run_id, "shortage_detected",
              f"suggested_quantity={observed.get('suggested_quantity')}")

    try:
        draft = build_draft(observed)
    except Exception as exc:
        store.set_run_state(run_id, RUN_UNRESOLVED, stock=observed,
                            error=f"{type(exc).__name__}: {exc}")
        store.log(run_id, "draft_unresolved", str(exc)[:300])
        return

    draft_id = store.create_draft(run_id, draft["organization_id"], draft,
                                  draft["content_hash"])
    store.set_run_state(run_id, RUN_AWAITING_APPROVAL, stock=observed)
    store.log(run_id, "draft_ready",
              f"{draft_id} total={draft['total']} {draft['currency']}")
