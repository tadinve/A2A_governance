"""The Auth Broker is the only thing that can sign, so what it refuses matters
as much as what it mints.

The delegated-token tests are the ones that carry weight. A broker that signs
whatever subject and actor chain the caller sends is an expensive way of
notarising the caller's own claims; these check that it derives both from
evidence it verified itself.
"""
from __future__ import annotations

import base64

import jwt
import pytest
from fastapi.testclient import TestClient

from governance_demo import auth_broker_app
from governance_demo.settings import ISSUER


client = TestClient(auth_broker_app.app)

# Deployed principals, as config/broker_clients.json lists them.
INVENTORY_PRINCIPAL = "3222129270558031872"
PROCUREMENT_PRINCIPAL = "1882730593880375296"
PROCUREMENT_ADK_PRINCIPAL = "2745873609963601920"


def auth(client_id="identity-broker", secret="identity-broker-demo-secret") -> dict:
    encoded = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


@pytest.fixture
def as_agent(monkeypatch):
    """Present a verified Agent Identity without standing up Google's verifier."""

    def presenting(principal: str) -> dict:
        monkeypatch.setattr(auth_broker_app, "_verify_google_identity",
                            lambda token: {"sub": principal})
        return {"Authorization": "Bearer pretend-id-token"}

    return presenting


_DEFAULT = object()


def mint(**overrides) -> dict:
    """Mint through the broker itself, so every token in a test is a real one."""
    body = {
        "token_kind": "user_access_token",
        "audience": "inventory-agent",
        "scopes": ["assistant.inventory"],
        "subject": "demo-user",
        "lifetime_seconds": 900,
    }
    headers = overrides.pop("headers", _DEFAULT)
    body.update(overrides)
    return client.post("/sign", json=body,
                       headers=auth() if headers is _DEFAULT else headers)


def human_token() -> str:
    response = mint()
    assert response.status_code == 200, response.text
    return response.json()["token"]


def agent_credential(agent: str) -> str:
    response = mint(token_kind="agent_credential", audience="sts",
                    scopes=["token.exchange"], subject=agent, lifetime_seconds=300)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def claims_of(token: str, audience: str) -> dict:
    published = client.get("/jwks").json()["public_key_pem"].encode()
    return jwt.decode(token, published, algorithms=["RS256"], audience=audience,
                      issuer=ISSUER)


def delegate(**overrides) -> dict:
    body = {
        "token_kind": "delegated_access_token",
        "audience": "zoho-inventory-mcp",
        "scopes": ["inventory.read"],
        "lifetime_seconds": 300,
    }
    headers = overrides.pop("headers", _DEFAULT)
    body.update(overrides)
    return client.post("/sign", json=body,
                       headers=auth() if headers is _DEFAULT else headers)


# Minting an origin token ----------------------------------------------------

def test_broker_mints_a_token_the_policy_allows():
    response = mint()
    assert response.status_code == 200
    assert response.json()["token"].count(".") == 2


def test_broker_publishes_only_the_public_key():
    published = client.get("/jwks").json()
    assert "PUBLIC KEY" in published["public_key_pem"]
    assert "PRIVATE KEY" not in published["public_key_pem"]
    # No field anywhere in the response may carry private key material.
    assert "PRIVATE" not in str(published).upper()


def test_broker_requires_client_authentication():
    assert mint(headers={}).status_code == 401
    assert mint(headers=auth(secret="wrong")).status_code == 401
    assert mint(headers=auth(client_id="ghost")).status_code == 401


def test_broker_refuses_an_audience_outside_policy():
    assert mint(audience="attacker-service").status_code == 403


def test_broker_refuses_a_scope_outside_policy():
    """Asking for purchase-order creation on an inventory-read delegation."""
    response = delegate(subject_token=human_token(),
                        actor_token=agent_credential("inventory-agent"),
                        scopes=["inventory.read", "purchaseorder.create"])
    assert response.status_code == 403


def test_broker_refuses_an_overlong_lifetime():
    assert mint(lifetime_seconds=86400).status_code == 403


def test_broker_refuses_an_unknown_token_kind():
    assert mint(token_kind="root_credential").status_code == 403


def test_broker_refuses_an_origin_token_with_no_subject():
    response = mint(subject=None)
    assert response.status_code == 400


# Deriving a delegation ------------------------------------------------------

def test_broker_derives_subject_and_actor_from_verified_evidence():
    response = delegate(subject_token=human_token(),
                        actor_token=agent_credential("inventory-agent"))
    assert response.status_code == 200, response.text
    claims = claims_of(response.json()["token"], "zoho-inventory-mcp")
    assert claims["sub"] == "demo-user"
    assert claims["act"] == {"sub": "inventory-agent"}


def test_broker_refuses_a_delegation_with_no_subject_token():
    response = delegate(actor_token=agent_credential("inventory-agent"))
    assert response.status_code == 400
    assert "subject_token" in response.text


def test_broker_refuses_a_caller_supplied_subject_on_a_delegation():
    """The subject is read out of the subject token, never taken on trust."""
    response = delegate(subject="ceo@example.com", subject_token=human_token(),
                        actor_token=agent_credential("inventory-agent"))
    assert response.status_code == 400


def test_broker_rejects_the_old_contract_outright():
    """A stale caller sending subject + actor_chain must fail, not be ignored."""
    response = client.post("/sign", headers=auth(), json={
        "subject": "demo-user", "audience": "zoho-inventory-mcp",
        "scopes": ["inventory.read"], "token_kind": "delegated_access_token",
        "actor_chain": {"sub": "inventory-agent"},
    })
    assert response.status_code == 422


def test_broker_refuses_a_subject_token_it_did_not_sign():
    """A well-formed grant from a key the broker does not hold is not a grant."""
    forged = jwt.encode(
        {"iss": ISSUER, "sub": "demo-user", "aud": "inventory-agent",
         "scope": "assistant.inventory", "token_kind": "user_access_token",
         "exp": 2 ** 31 - 1},
        _attacker_key(), algorithm="RS256")
    response = delegate(subject_token=forged,
                        actor_token=agent_credential("inventory-agent"))
    assert response.status_code == 401


def _attacker_key() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def test_delegated_token_cannot_outlive_the_grant_it_extends():
    short = mint(lifetime_seconds=30)
    assert short.status_code == 200
    response = delegate(subject_token=short.json()["token"],
                        actor_token=agent_credential("inventory-agent"),
                        lifetime_seconds=300)
    assert response.status_code == 200
    assert response.json()["expires_in"] <= 30


def test_broker_refuses_an_actor_the_delegation_policy_does_not_permit():
    """procurement-agent may extend a purchase grant, not an inventory one."""
    response = delegate(subject_token=human_token(),
                        actor_token=agent_credential("procurement-agent"))
    assert response.status_code == 403


# Per-principal authorization ------------------------------------------------

def test_each_deployed_agent_authenticates_as_its_own_broker_client(as_agent):
    inventory = delegate(headers=as_agent(INVENTORY_PRINCIPAL),
                         subject_token=human_token())
    assert inventory.status_code == 200, inventory.text
    assert claims_of(inventory.json()["token"], "zoho-inventory-mcp")["act"] == {
        "sub": "inventory-agent"}


def test_a_deployed_agent_may_not_name_its_own_actor(as_agent):
    """Its actor comes from the principal Google attested, not from the body."""
    response = delegate(headers=as_agent(PROCUREMENT_PRINCIPAL),
                        subject_token=human_token(),
                        actor_token=agent_credential("inventory-agent"))
    assert response.status_code == 403


def test_procurement_agent_cannot_mint_the_human_grant_it_should_receive(as_agent):
    """The fallback the A2A executor used to rely on, refused at the broker."""
    response = mint(headers=as_agent(PROCUREMENT_PRINCIPAL))
    assert response.status_code == 403


def test_procurement_agent_cannot_extend_a_grant_addressed_to_inventory(as_agent):
    """It must be handed a purchase.request delegation; it cannot start there."""
    response = delegate(headers=as_agent(PROCUREMENT_PRINCIPAL),
                        subject_token=human_token(),
                        audience="zoho-procurement-mcp",
                        scopes=["purchaseorder.create"])
    assert response.status_code == 403


def test_procurement_agent_can_extend_the_delegation_it_receives(as_agent):
    """The whole A2A path, end to end, with nothing asserted by a caller."""
    handed_over = delegate(headers=as_agent(INVENTORY_PRINCIPAL),
                           subject_token=human_token(),
                           audience="procurement-agent",
                           scopes=["purchase.request"])
    assert handed_over.status_code == 200, handed_over.text

    onward = delegate(headers=as_agent(PROCUREMENT_PRINCIPAL),
                      subject_token=handed_over.json()["token"],
                      audience="zoho-procurement-mcp",
                      scopes=["purchaseorder.create"])
    assert onward.status_code == 200, onward.text
    claims = claims_of(onward.json()["token"], "zoho-procurement-mcp")
    assert claims["sub"] == "demo-user"
    assert claims["act"] == {"sub": "procurement-agent", "act": {"sub": "inventory-agent"}}


def test_inventory_agent_cannot_reach_the_procurement_connector(as_agent):
    """Separate principals, separate minting rules. This is the point of both."""
    response = delegate(headers=as_agent(INVENTORY_PRINCIPAL),
                        subject_token=human_token(),
                        audience="zoho-procurement-mcp",
                        scopes=["purchaseorder.create"])
    assert response.status_code == 403


def test_the_non_a2a_deployment_may_mint_nothing(as_agent):
    assert mint(headers=as_agent(PROCUREMENT_ADK_PRINCIPAL)).status_code == 403
    assert delegate(headers=as_agent(PROCUREMENT_ADK_PRINCIPAL),
                    subject_token=human_token()).status_code == 403


def test_an_unknown_principal_is_not_a_minter(as_agent):
    assert mint(headers=as_agent("8174470379549491200")).status_code == 403


# Key custody ----------------------------------------------------------------

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
