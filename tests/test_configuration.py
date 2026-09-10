from __future__ import annotations

from governance_demo.settings import load_json


def test_registry_has_named_agents_and_separate_mcp_connectors():
    registry = load_json("registry.json")
    assert registry["agents"]["inventory-agent"]["display_name"] == "Inventory Agent"
    assert registry["agents"]["procurement-agent"]["display_name"] == "Procurement Agent"
    assert set(registry["resources"]) == {"zoho-inventory-mcp", "zoho-procurement-mcp"}


def test_inventory_agent_cannot_create_purchase_orders():
    routes = load_json("policies.json")["gateway_routes"]
    assert not any(route["actor"] == "inventory-agent" and route["target"] == "zoho-procurement-mcp" for route in routes)


def test_no_agent_has_an_approval_route_or_tool_scope():
    serialized = str(load_json("policies.json")).lower()
    assert "approve" not in serialized


def test_zoho_oauth_clients_are_separated_by_least_privilege():
    clients = load_json("zoho_oauth_clients.json")
    inventory_scopes = set(clients["zoho-inventory-connector"]["scopes"])
    procurement_scopes = set(clients["zoho-procurement-connector"]["scopes"])
    assert inventory_scopes == {"ZohoInventory.items.READ"}
    assert "ZohoInventory.purchaseorders.CREATE" in procurement_scopes
    assert "ZohoInventory.purchaseorders.CREATE" not in inventory_scopes
