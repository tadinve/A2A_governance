"""The Auth Broker is the only thing that can sign, so what it refuses matters
as much as what it mints."""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from governance_demo import auth_broker_app


client = TestClient(auth_broker_app.app)


def auth(client_id="identity-broker", secret="identity-broker-demo-secret") -> dict:
    encoded = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def sign_request(**overrides) -> dict:
    body = {
        "subject": "demo-user",
        "audience": "zoho-inventory-mcp",
        "scopes": ["inventory.read"],
        "token_kind": "delegated_access_token",
        "lifetime_seconds": 300,
    }
    body.update(overrides)
    return body


def test_broker_mints_a_token_the_policy_allows():
    response = client.post("/sign", json=sign_request(), headers=auth())
    assert response.status_code == 200
    assert response.json()["token"].count(".") == 2


def test_broker_publishes_only_the_public_key():
    published = client.get("/jwks").json()
    assert "PUBLIC KEY" in published["public_key_pem"]
    assert "PRIVATE KEY" not in published["public_key_pem"]
    # No field anywhere in the response may carry private key material.
    assert "PRIVATE" not in str(published).upper()


def test_broker_requires_client_authentication():
    assert client.post("/sign", json=sign_request()).status_code == 401
    assert client.post("/sign", json=sign_request(), headers=auth(secret="wrong")).status_code == 401
    assert client.post("/sign", json=sign_request(), headers=auth(client_id="ghost")).status_code == 401


def test_broker_refuses_an_audience_outside_policy():
    response = client.post("/sign", json=sign_request(audience="attacker-service"), headers=auth())
    assert response.status_code == 403


def test_broker_refuses_a_scope_outside_policy():
    """Asking for purchase-order creation on an inventory-read delegation."""
    response = client.post(
        "/sign",
        json=sign_request(scopes=["inventory.read", "purchaseorder.create"], audience="zoho-inventory-mcp"),
        headers=auth(),
    )
    assert response.status_code == 403


def test_broker_refuses_an_overlong_lifetime():
    response = client.post("/sign", json=sign_request(lifetime_seconds=86400), headers=auth())
    assert response.status_code == 403


def test_broker_refuses_an_unknown_token_kind():
    response = client.post("/sign", json=sign_request(token_kind="root_credential"), headers=auth())
    assert response.status_code == 403


def test_broker_exposes_no_endpoint_that_returns_key_material():
    paths = {route.path for route in auth_broker_app.app.routes}
    assert paths & {"/sign", "/jwks", "/health"} == {"/sign", "/jwks", "/health"}
    # Whatever else exists, nothing may serve an export/private-key route.
    assert not any("private" in path or "export" in path for path in paths)


def test_signing_backend_cannot_surrender_private_key_material():
    backend = auth_broker_app._backend
    assert not hasattr(backend, "private_key")
    assert not hasattr(backend, "private_key_pem")
    assert "PRIVATE" not in backend.public_key_pem().decode().upper()
