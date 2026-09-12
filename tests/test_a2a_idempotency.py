"""Idempotency on the direct A2A path, and the SKU parsing it depends on.

The defect these cover: the executor generated a fresh UUID per attempt and
turned it into the Zoho reference number, so a retried business request -- the
ordinary consequence of a lost response -- created a second real purchase order.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
_PKG = "a2a_governance_lib"


@pytest.fixture(scope="module")
def governance():
    """Load the agent governance module without the ADK/A2A dependency tree."""
    if _PKG not in sys.modules:
        package = types.ModuleType(_PKG)
        package.__path__ = [str(CLOUD / "procurement_a2a")]  # type: ignore[attr-defined]
        sys.modules[_PKG] = package
        importlib.import_module(f"{_PKG}.zoho_mcp")
        importlib.import_module(f"{_PKG}.governance")
    return sys.modules[f"{_PKG}.governance"]


# A stable operation identity ------------------------------------------------

def test_the_same_business_request_yields_the_same_operation_id(governance):
    first = governance.operation_id("demo-user", "DEMO-WIDGET-A", 73)
    second = governance.operation_id("demo-user", "DEMO-WIDGET-A", 73)
    assert first == second


def test_a_retry_cannot_be_told_apart_by_case_or_numeric_type(governance):
    assert (governance.operation_id("demo-user", "demo-widget-a", 73)
            == governance.operation_id("demo-user", "DEMO-WIDGET-A", 73.0))


def test_a_different_order_yields_a_different_operation_id(governance):
    base = governance.operation_id("demo-user", "DEMO-WIDGET-A", 73)
    assert governance.operation_id("demo-user", "DEMO-WIDGET-A", 74) != base
    assert governance.operation_id("demo-user", "OTHER-SKU", 73) != base
    assert governance.operation_id("someone-else", "DEMO-WIDGET-A", 73) != base


def test_the_operation_id_fits_a_zoho_reference_number(governance):
    reference = f"A2A-{governance.operation_id('demo-user', 'DEMO-WIDGET-A', 73)}"
    assert len(reference) <= 20
    assert reference.replace("-", "").isalnum()


# Both ends derive it the same way -------------------------------------------

def test_the_executor_never_invents_a_per_attempt_identifier():
    source = (CLOUD / "procurement_a2a" / "executor.py").read_text()
    assert "uuid" not in source, "a fresh UUID per attempt is what broke idempotency"
    assert "governance.operation_id(subject, sku, quantity)" in source
    assert 'reference_number=f"A2A-{operation}"' in source


def test_the_inventory_agent_sends_the_operation_id_through_a2a():
    source = (CLOUD / "inventory_agent" / "agent.py").read_text()
    assert '"operation_id": operation' in source
    assert "governance.operation_id(" in source
    # messageId identifies the transmission and must stay unique; only the
    # business operation is stable.
    assert 'messageId": str(uuid.uuid4())' in source


def test_create_draft_no_longer_takes_a_key_it_ignores():
    source = (CLOUD / "procurement_a2a" / "governance.py").read_text()
    signature = source.split("def create_draft(")[1].split(")")[0]
    assert "idempotency_key" not in signature


# SKU parsing ----------------------------------------------------------------

def test_a_sentence_naming_the_live_sku_is_understood(governance):
    assert governance.sku_from_text(
        "Stock is low on DEMO-WIDGET-A, order 73 units", "FALLBACK") == "DEMO-WIDGET-A"


def test_the_old_hardcoded_sku_shape_still_parses(governance):
    """The local emulator still uses CK-GPU-42; the pattern must not regress."""
    assert governance.sku_from_text("Check CK-GPU-42", "DEMO-WIDGET-A") == "CK-GPU-42"


def test_a_purchase_order_number_is_not_mistaken_for_a_sku(governance):
    assert governance.sku_from_text("status of PO-1001", "DEMO-WIDGET-A") == "DEMO-WIDGET-A"
    assert governance.sku_from_text("reference A2A-abc123", "DEMO-WIDGET-A") == "DEMO-WIDGET-A"


def test_an_unrecognisable_sentence_falls_back_to_the_configured_sku(governance):
    assert governance.sku_from_text("please reorder something", "DEMO-WIDGET-A") == "DEMO-WIDGET-A"
    assert governance.sku_from_text("", "DEMO-WIDGET-A") == "DEMO-WIDGET-A"
