"""Inbound stock accounting for the reorder rule.

The defect these cover: `incoming_quantity` read `quantity`/`quantity_received`
off the *trimmed* record returned by `list_item_purchase_orders`, where neither
field exists. It therefore returned 0.0 however much stock was on order, the
reorder plan made up the entire shortfall again, and the business bought the
same goods twice. Observed live with 145 units on open orders reported as 0.

The fixtures use the field shapes Zoho actually returned, not invented ones.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
_PKG = "incoming_qty_lib"
ITEM_ID = "1247982000000061129"


@pytest.fixture
def governance(monkeypatch):
    if _PKG not in sys.modules:
        package = types.ModuleType(_PKG)
        package.__path__ = [str(CLOUD / "procurement_a2a")]  # type: ignore[attr-defined]
        sys.modules[_PKG] = package
        importlib.import_module(f"{_PKG}.zoho_mcp")
        importlib.import_module(f"{_PKG}.governance")
    module = sys.modules[f"{_PKG}.governance"]
    monkeypatch.setattr(module, "organization_id", lambda: "20119389287")
    return module


def wire(governance, monkeypatch, orders: list[dict], details: dict[str, dict]):
    """Stand in for the two Zoho calls, preserving the real trimmed/full split.

    Records which connector each detail read went to, because that is a
    governance property and not an implementation detail.
    """
    zoho = sys.modules[f"{_PKG}.zoho_mcp"]
    monkeypatch.setattr(zoho, "list_item_purchase_orders",
                        lambda org, item_id: orders)
    used_servers: list[str] = []

    def get_purchase_order(org, po_id, server=zoho.INVREAD):
        used_servers.append(server)
        if po_id not in details:
            raise RuntimeError(f"Zoho unavailable for {po_id}")
        return details[po_id]

    monkeypatch.setattr(zoho, "get_purchase_order", get_purchase_order)
    monkeypatch.setattr(governance, "zoho_mcp", zoho)
    return used_servers


def trimmed(po_id: str, quantity: float) -> dict:
    """Exactly the fields Zoho's list response carries -- note what is missing."""
    return {"purchaseorder_id": po_id, "item_quantity": quantity,
            "order_status": "draft", "received_status": "to_be_received",
            "vendor_id": "v1", "purchaseorder_number": f"PO-{po_id}"}


def detail(po_id: str, quantity: float, *, status: str = "draft",
           received: float = 0.0, cancelled: float = 0.0,
           item_id: str = ITEM_ID) -> dict:
    return {
        "purchaseorder_id": po_id, "purchaseorder_number": f"PO-{po_id}",
        "status": status,
        "line_items": [{"item_id": item_id, "quantity": quantity,
                        "quantity_received": received,
                        "quantity_cancelled": cancelled}],
    }


ITEM = {"item_id": ITEM_ID, "sku": "DEMO-WIDGET-A"}


def test_open_orders_are_counted_not_silently_zero(governance, monkeypatch):
    """The live regression: three open drafts for 145 units reported as 0."""
    wire(governance, monkeypatch,
         [trimmed("a", 70), trimmed("b", 70), trimmed("c", 5)],
         {"a": detail("a", 70), "b": detail("b", 70), "c": detail("c", 5)})
    assert governance.incoming_quantity(ITEM) == 145.0


def test_received_stock_is_not_counted_twice(governance, monkeypatch):
    """Received goods are already in the stock figure."""
    wire(governance, monkeypatch, [trimmed("a", 70)],
         {"a": detail("a", 70, received=50)})
    assert governance.incoming_quantity(ITEM) == 20.0


def test_cancelled_quantity_is_not_coming(governance, monkeypatch):
    wire(governance, monkeypatch, [trimmed("a", 70)],
         {"a": detail("a", 70, cancelled=30)})
    assert governance.incoming_quantity(ITEM) == 40.0


def test_a_fully_received_line_contributes_nothing(governance, monkeypatch):
    wire(governance, monkeypatch, [trimmed("a", 70)],
         {"a": detail("a", 70, received=70)})
    assert governance.incoming_quantity(ITEM) == 0.0


def test_over_receipt_never_goes_negative(governance, monkeypatch):
    """A negative contribution would mask stock inbound on another order."""
    wire(governance, monkeypatch, [trimmed("a", 70), trimmed("b", 10)],
         {"a": detail("a", 70, received=90), "b": detail("b", 10)})
    assert governance.incoming_quantity(ITEM) == 10.0


@pytest.mark.parametrize("status", ["cancelled", "closed", "CANCELLED", "Closed"])
def test_orders_that_will_not_deliver_are_skipped(governance, monkeypatch, status):
    wire(governance, monkeypatch, [trimmed("a", 70)],
         {"a": detail("a", 70, status=status)})
    assert governance.incoming_quantity(ITEM) == 0.0


def test_a_billed_order_still_has_stock_coming(governance, monkeypatch):
    """Billing is not delivery. Skipping billed orders under-counted inbound."""
    wire(governance, monkeypatch, [trimmed("a", 70)],
         {"a": detail("a", 70, status="billed", received=0)})
    assert governance.incoming_quantity(ITEM) == 70.0


def test_only_lines_for_this_item_are_counted(governance, monkeypatch):
    """A multi-item purchase order must not contribute another item's quantity."""
    mixed = detail("a", 70)
    mixed["line_items"].append(
        {"item_id": "some-other-item", "quantity": 999,
         "quantity_received": 0, "quantity_cancelled": 0})
    wire(governance, monkeypatch, [trimmed("a", 70)], {"a": mixed})
    assert governance.incoming_quantity(ITEM) == 70.0


def test_an_unreadable_order_is_unknown_not_absent(governance, monkeypatch):
    """Refusing to answer beats answering zero and buying the goods again."""
    wire(governance, monkeypatch, [trimmed("a", 70), trimmed("gone", 5)],
         {"a": detail("a", 70)})
    with pytest.raises(Exception):
        governance.incoming_quantity(ITEM)


def test_a_line_with_no_quantity_is_refused_not_guessed(governance, monkeypatch):
    broken = detail("a", 70)
    broken["line_items"][0]["quantity"] = None
    wire(governance, monkeypatch, [trimmed("a", 70)], {"a": broken})
    with pytest.raises(governance.ZohoPolicyError):
        governance.incoming_quantity(ITEM)


def test_no_open_orders_is_a_real_zero(governance, monkeypatch):
    wire(governance, monkeypatch, [], {})
    assert governance.incoming_quantity(ITEM) == 0.0


# The rule this feeds ---------------------------------------------------------

def test_reorder_plan_nets_off_inbound_instead_of_double_buying(governance, monkeypatch):
    """available 30, target 100, 145 already on order => order nothing."""
    wire(governance, monkeypatch,
         [trimmed("a", 70), trimmed("b", 70), trimmed("c", 5)],
         {"a": detail("a", 70), "b": detail("b", 70), "c": detail("c", 5)})
    item = {"item_id": ITEM_ID, "sku": "DEMO-WIDGET-A",
            "actual_available_stock": 30, "reorder_level": 50}
    plan = governance.reorder_plan(item)
    assert plan["resolved"] is True
    assert plan["incoming_quantity"] == 145.0
    assert plan["suggested_quantity"] == 0
    assert plan["reorder_needed"] is False


def test_reorder_plan_is_unresolved_when_inbound_cannot_be_read(governance, monkeypatch):
    wire(governance, monkeypatch, [trimmed("gone", 5)], {})
    item = {"item_id": ITEM_ID, "sku": "DEMO-WIDGET-A",
            "actual_available_stock": 30, "reorder_level": 50}
    plan = governance.reorder_plan(item)
    assert plan["resolved"] is False
    assert "already inbound" in plan["reason"]


# Least privilege ------------------------------------------------------------

def test_inbound_is_read_over_the_read_only_connector(governance, monkeypatch):
    """Inventory Agent must be able to answer this, and it holds no write access.

    Reading detail over ProcureWrite made `check_stock` fail closed in the
    deployed Inventory Agent: Secret Manager refused it the procurement
    connector's URL, which is exactly the boundary the demo exists to show. How
    much is on order is a read, so it goes over the read-only connector.
    """
    zoho = sys.modules[f"{_PKG}.zoho_mcp"]
    servers = wire(governance, monkeypatch, [trimmed("a", 70)], {"a": detail("a", 70)})
    assert governance.incoming_quantity(ITEM) == 70.0
    assert servers, "expected the purchase order to be re-read in full"
    assert zoho.PROCUREWRITE not in servers, (
        "computing inbound stock must not require the procurement connector")
    assert set(servers) == {zoho.INVREAD}
