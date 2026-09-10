from __future__ import annotations

import pytest

from governance_demo.security import current_actor, decode_token, extend_actor_chain, issue_token, scopes


def test_audience_and_scope_are_enforced():
    token = issue_token(subject="agent-a", audience="registry", scopes=["registry.read"], token_kind="agent")
    claims = decode_token(token, audience="registry")
    assert claims["sub"] == "agent-a"
    assert scopes(claims) == {"registry.read"}
    with pytest.raises(Exception):
        decode_token(token, audience="sap-api")


def test_nested_actor_chain_preserves_delegation():
    first = extend_actor_chain("agent-a", {"sub": "demo-user"})
    second = extend_actor_chain("agent-b", {"sub": "demo-user", "act": first})
    assert second == {"sub": "agent-b", "act": {"sub": "agent-a"}}
    assert current_actor({"act": second}) == "agent-b"
