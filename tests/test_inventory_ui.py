"""Approval and idempotency invariants for the purchasing UI.

These use the store directly so they exercise the real transitions without
writing a purchase order to live Zoho.
"""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("INVENTORY_UI_DB", os.path.join(tempfile.mkdtemp(), "t.sqlite3"))

from inventory_ui import store  # noqa: E402
from inventory_ui.models import (  # noqa: E402
    PO_APPROVED,
    PO_AWAITING_APPROVAL,
    PO_REJECTED,
    content_hash,
)


DRAFT = {
    "organization_id": "org1", "vendor_id": "v1", "currency": "USD",
    "reference_number": "REF-1",
    "lines": [{"item_id": "i1", "sku": "S", "quantity": 70, "rate": 10.0}],
    "total": 700.0,
}


def make_draft():
    run_id = store.create_run("a@example.com", "org1", "SKU", "DRAFTING", "direct")
    draft_id = store.create_draft(run_id, "org1", DRAFT, content_hash(DRAFT))
    return run_id, draft_id


def test_content_hash_covers_purchase_affecting_fields():
    changed = {**DRAFT, "lines": [{**DRAFT["lines"][0], "quantity": 71}]}
    assert content_hash(changed) != content_hash(DRAFT)
    changed = {**DRAFT, "total": 701.0}
    assert content_hash(changed) != content_hash(DRAFT)
    # Ids and state are not purchase-affecting and must not change the hash.
    assert content_hash({**DRAFT, "draft_id": "x"}) == content_hash(DRAFT)


def test_one_decision_per_draft_version():
    run_id, draft_id = make_draft()
    store.record_approval(run_id=run_id, draft_id=draft_id, draft_version=1,
                          org_id="org1", subject="a@example.com", decision="approved",
                          content_hash_value=content_hash(DRAFT), snapshot=DRAFT,
                          ttl_seconds=60)
    with pytest.raises(store.Conflict):
        store.record_approval(run_id=run_id, draft_id=draft_id, draft_version=1,
                              org_id="org1", subject="a@example.com", decision="rejected",
                              content_hash_value=content_hash(DRAFT), snapshot=DRAFT,
                              ttl_seconds=60)


def test_transition_requires_expected_state():
    _, draft_id = make_draft()
    store.transition_draft(draft_id, expected_state=PO_AWAITING_APPROVAL,
                           new_state=PO_REJECTED)
    with pytest.raises(store.Conflict):
        store.transition_draft(draft_id, expected_state=PO_AWAITING_APPROVAL,
                               new_state=PO_APPROVED)


def test_transition_requires_expected_version():
    _, draft_id = make_draft()
    with pytest.raises(store.Conflict):
        store.transition_draft(draft_id, expected_state=PO_AWAITING_APPROVAL,
                               new_state=PO_APPROVED, expected_version=99)


def test_submission_claimed_exactly_once():
    _, draft_id = make_draft()
    key = f"submit:{draft_id}:v1"
    first, is_new = store.claim_submission(draft_id, 1, key)
    second, again = store.claim_submission(draft_id, 1, key)
    assert is_new is True and again is False and first == second


def test_active_run_is_one_per_subject_and_scope():
    store.create_run("solo@example.com", "org1", "SKU-X", "AWAITING_APPROVAL", "direct")
    active = store.active_run_for("solo@example.com", "SKU-X")
    assert active is not None
    assert store.active_run_for("solo@example.com", "OTHER-SKU") is None
    assert store.active_run_for("nobody@example.com", "SKU-X") is None
