from __future__ import annotations

import pytest

from governance_demo.security import current_actor, decode_token, extend_actor_chain, issue_token, scopes


def test_audience_and_scope_are_enforced():
    token = issue_token(subject="inventory-agent", audience="registry", scopes=["registry.read"], token_kind="agent")
    claims = decode_token(token, audience="registry")
    assert claims["sub"] == "inventory-agent"
    assert scopes(claims) == {"registry.read"}
    with pytest.raises(Exception):
        decode_token(token, audience="zoho-inventory-mcp")


def test_nested_actor_chain_preserves_delegation():
    first = extend_actor_chain("inventory-agent", {"sub": "demo-user"})
    second = extend_actor_chain("procurement-agent", {"sub": "demo-user", "act": first})
    assert second == {"sub": "procurement-agent", "act": {"sub": "inventory-agent"}}
    assert current_actor({"act": second}) == "procurement-agent"
