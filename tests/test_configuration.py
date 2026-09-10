from __future__ import annotations

from governance_demo.settings import load_json


def test_registry_identity_and_policy_are_distinct():
    registry = load_json("registry.json")
    policies = load_json("policies.json")
    assert registry["agents"]["agent-b"]["skills"] == ["sap-inventory-lookup"]
    assert any(
        route["actor"] == "agent-a"
        and route["target"] == "agent-b"
        and route["required_scope"] == "inventory.read"
        for route in policies["gateway_routes"]
    )


def test_agent_a_has_no_direct_sap_route():
    routes = load_json("policies.json")["gateway_routes"]
    assert not any(route["actor"] == "agent-a" and route["target"] == "sap-api" for route in routes)
