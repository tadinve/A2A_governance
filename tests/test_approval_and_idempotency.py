"""Human-approval and duplicate-order guarantees.

These sit downstream of the signing change: the Auth Broker decides who may act,
and these decide that acting still requires a human and still cannot double-spend.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from governance_demo import zoho_app


ORG = zoho_app.ORGANIZATION_ID
CONNECTOR = "zoho-procurement-connector"


@pytest.fixture
def client():
    # Each test starts from an empty ledger so PO numbering and idempotency
    # bookkeeping cannot leak between cases.
    zoho_app.PURCHASE_ORDERS.clear()
    zoho_app.IDEMPOTENCY.clear()
    zoho_app.SESSIONS.clear()
    return TestClient(zoho_app.app)


def oauth_headers(client: TestClient, connector: str = CONNECTOR) -> dict:
    clients = zoho_app.load_json("zoho_oauth_clients.json")
    record = clients[connector]
    response = client.post(
        "/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": connector,
            "client_secret": record["client_secret"],
            "refresh_token": record["refresh_token"],
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def draft(client: TestClient, idempotency_key: str | None = None) -> dict:
    item = next(iter(zoho_app.ITEMS.values()))
    headers = oauth_headers(client)
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    response = client.post(
        f"/api/v1/purchaseorders?organization_id={ORG}",
        json={
            "vendor_id": zoho_app.VENDOR["vendor_id"],
            "reference_number": "demo-ref",
            "line_items": [
                {"item_id": item["item_id"], "quantity": 10, "rate": item["purchase_rate"]}
            ],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["purchaseorder"]


def submit(client: TestClient, po_id: str) -> dict:
    """Move a draft to pending_approval, the only state approval accepts."""
    response = client.post(
        f"/api/v1/purchaseorders/{po_id}/submit?organization_id={ORG}",
        headers=oauth_headers(client),
    )
    assert response.status_code == 200, response.text
    return response.json()["purchaseorder"]


def sign_in(client: TestClient) -> None:
    response = client.post(
        "/ui/login",
        data={"email": "approver@example.com", "password": "demo-approval-password"},
    )
    assert response.status_code == 200


# --- missing approval -------------------------------------------------------

def test_approval_without_a_human_session_is_refused(client):
    po = draft(client)
    submit(client, po["purchaseorder_id"])
    response = client.post(
        f"/ui/purchaseorders/{po['purchaseorder_id']}/approve",
        json={"expected_draft_hash": po["draft_hash"]},
    )
    assert response.status_code == 401
    assert zoho_app.PURCHASE_ORDERS[po["purchaseorder_id"]]["approval_status"] != "approved"


def test_review_without_a_human_session_is_refused(client):
    po = draft(client)
    assert client.get(f"/ui/purchaseorders/{po['purchaseorder_id']}").status_code == 401


def test_approval_of_a_tampered_draft_is_refused(client):
    """Approval is bound to the exact draft the human saw. Changing the order
    after review invalidates the hash the approval was issued against."""
    po = draft(client)
    submit(client, po["purchaseorder_id"])
    sign_in(client)
    stale_hash = po["draft_hash"]
    zoho_app.PURCHASE_ORDERS[po["purchaseorder_id"]]["line_items"][0]["quantity"] = 9999

    response = client.post(
        f"/ui/purchaseorders/{po['purchaseorder_id']}/approve",
        json={"expected_draft_hash": stale_hash},
    )
    assert response.status_code == 409
    assert zoho_app.PURCHASE_ORDERS[po["purchaseorder_id"]]["approval_status"] != "approved"


def test_a_reviewed_draft_can_be_approved(client):
    """The positive control: the negative tests above must fail for the right
    reason, not because approval is broken outright."""
    po = draft(client)
    submit(client, po["purchaseorder_id"])
    sign_in(client)
    assert client.get(f"/ui/purchaseorders/{po['purchaseorder_id']}").status_code == 200
    response = client.post(
        f"/ui/purchaseorders/{po['purchaseorder_id']}/approve",
        json={"expected_draft_hash": po["draft_hash"]},
    )
    assert response.status_code == 200
    assert zoho_app.PURCHASE_ORDERS[po["purchaseorder_id"]]["approval_status"] == "approved"


# --- replay and duplicate orders -------------------------------------------

def test_replaying_a_create_with_the_same_key_returns_the_same_order(client):
    first = draft(client, idempotency_key="order-key-1")
    second = draft(client, idempotency_key="order-key-1")
    assert first["purchaseorder_id"] == second["purchaseorder_id"]
    assert len(zoho_app.PURCHASE_ORDERS) == 1, "a replay must not create a second order"


def test_distinct_keys_create_distinct_orders(client):
    first = draft(client, idempotency_key="order-key-1")
    second = draft(client, idempotency_key="order-key-2")
    assert first["purchaseorder_id"] != second["purchaseorder_id"]
    assert len(zoho_app.PURCHASE_ORDERS) == 2


def test_replayed_approval_does_not_change_a_settled_order(client):
    po = draft(client, idempotency_key="order-key-1")
    submit(client, po["purchaseorder_id"])
    sign_in(client)
    path = f"/ui/purchaseorders/{po['purchaseorder_id']}/approve"
    assert client.post(path, json={"expected_draft_hash": po["draft_hash"]}).status_code == 200

    settled = dict(zoho_app.PURCHASE_ORDERS[po["purchaseorder_id"]])
    client.post(path, json={"expected_draft_hash": po["draft_hash"]})
    assert zoho_app.PURCHASE_ORDERS[po["purchaseorder_id"]]["approval_status"] == "approved"
    assert zoho_app.PURCHASE_ORDERS[po["purchaseorder_id"]]["total"] == settled["total"]
    assert len(zoho_app.PURCHASE_ORDERS) == 1


# --- least privilege on the OAuth connectors --------------------------------

def test_inventory_connector_cannot_create_purchase_orders(client):
    item = next(iter(zoho_app.ITEMS.values()))
    response = client.post(
        f"/api/v1/purchaseorders?organization_id={ORG}",
        json={
            "vendor_id": zoho_app.VENDOR["vendor_id"],
            "reference_number": "demo-ref",
            "line_items": [
                {"item_id": item["item_id"], "quantity": 10, "rate": item["purchase_rate"]}
            ],
        },
        headers=oauth_headers(client, "zoho-inventory-connector"),
    )
    assert response.status_code == 403
