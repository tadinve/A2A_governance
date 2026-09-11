from __future__ import annotations

import time

import pytest

from governance_demo import security
from governance_demo.security import current_actor, decode_token, extend_actor_chain, scopes
from governance_demo.settings import ISSUER
from governance_demo.signing import LocalSoftwareBackend, sign_claims


# The key the Auth Broker would hold, and a key an attacker controls. Verifying
# against the first while forging with the second is the whole point of the
# forged-token tests below.
ISSUING_KEY = LocalSoftwareBackend()
ATTACKER_KEY = LocalSoftwareBackend()


@pytest.fixture(autouse=True)
def trust_only_the_issuing_key(monkeypatch):
    """Verify against the test issuing key without standing up an Auth Broker.

    Patched at the fetch function rather than the cache dict so that
    decode_token's rotation retry also resolves here instead of making a real
    HTTP call to the broker.
    """
    monkeypatch.setattr(
        security, "_fetch_verification_key", lambda force_refresh=False: ISSUING_KEY.public_key_pem()
    )


def mint(
    backend=ISSUING_KEY,
    *,
    subject="inventory-agent",
    audience="registry",
    scope="registry.read",
    token_kind="agent_credential",
    lifetime=300,
    actor_chain=None,
    issuer=ISSUER,
):
    now = int(time.time())
    claims = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
        "scope": scope,
        "token_kind": token_kind,
    }
    if actor_chain:
        claims["act"] = actor_chain
    return sign_claims(claims, backend)


def test_audience_and_scope_are_enforced():
    claims = decode_token(mint(), audience="registry")
    assert claims["sub"] == "inventory-agent"
    assert scopes(claims) == {"registry.read"}
    with pytest.raises(Exception):
        decode_token(mint(), audience="zoho-inventory-mcp")


def test_nested_actor_chain_preserves_delegation():
    first = extend_actor_chain("inventory-agent", {"sub": "demo-user"})
    second = extend_actor_chain("procurement-agent", {"sub": "demo-user", "act": first})
    assert second == {"sub": "procurement-agent", "act": {"sub": "inventory-agent"}}
    assert current_actor({"act": second}) == "procurement-agent"


def test_forged_token_signed_with_another_key_is_rejected():
    """The negative test that KMS custody is meant to make unwinnable: an
    attacker who does not hold the signing key cannot mint an accepted token."""
    forged = mint(ATTACKER_KEY, subject="inventory-agent", scope="registry.read")
    with pytest.raises(Exception):
        decode_token(forged, audience="registry")


def test_tampered_payload_is_rejected():
    """Escalating scope by editing the payload breaks the signature."""
    import base64
    import json

    header, payload, signature = mint(scope="registry.read").split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["scope"] = "purchaseorder.create"
    swapped = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    with pytest.raises(Exception):
        decode_token(f"{header}.{swapped}.{signature}", audience="registry")


def test_expired_token_is_rejected():
    with pytest.raises(Exception):
        decode_token(mint(lifetime=-60), audience="registry")


def test_wrong_issuer_is_rejected():
    with pytest.raises(Exception):
        decode_token(mint(issuer="https://attacker.example"), audience="registry")


def test_unsigned_alg_none_token_is_rejected():
    """A classic JWT downgrade: strip the signature and claim alg=none."""
    import base64
    import json

    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    now = int(time.time())
    header = segment({"alg": "none", "typ": "JWT"})
    payload = segment(
        {"iss": ISSUER, "sub": "inventory-agent", "aud": "registry",
         "iat": now, "exp": now + 300, "scope": "registry.read"}
    )
    with pytest.raises(Exception):
        decode_token(f"{header}.{payload}.", audience="registry")
