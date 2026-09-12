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


def test_each_deployed_agent_has_its_own_broker_client():
    """Separate principals must not collapse back into one policy identity.

    Authenticating three deployments and then authorizing them as a single
    minter proves which workload called and then declines to use the answer.
    """
    clients = load_json("broker_clients.json")
    seen: dict[str, str] = {}
    for client_id, client in clients.items():
        for principal in client.get("allowed_principals", []):
            assert principal not in seen, (
                f"{principal} is a minter under both {seen[principal]} and {client_id}")
            seen[principal] = client_id
    deployed = {client_id for client_id, client in clients.items()
                if client.get("allowed_principals")}
    assert deployed == {"inventory-agent-principal", "procurement-agent-principal",
                        "procurement-agent-adk-principal"}


def test_only_the_agent_a_human_talks_to_may_start_a_delegation_chain():
    rules = load_json("policies.json")["token_minting"]
    starters = {rule["minter"] for rule in rules
                if rule["token_kind"] == "user_access_token"}
    assert starters == {"identity-broker", "inventory-agent-principal"}


def test_procurement_principals_cannot_mint_a_human_grant():
    """The fallback the A2A write path used to take, removed from policy too."""
    clients = load_json("broker_clients.json")
    for client_id in ("procurement-agent-principal", "procurement-agent-adk-principal"):
        assert "user_access_token" not in clients[client_id]["may_mint"]


def test_no_principal_may_mint_across_the_inventory_procurement_boundary():
    """Each deployed minter is confined to the connectors its role needs."""
    rules = load_json("policies.json")["token_minting"]
    reachable = {"inventory-agent-principal": set(), "procurement-agent-principal": set(),
                 "procurement-agent-adk-principal": set()}
    for rule in rules:
        if rule["minter"] in reachable:
            reachable[rule["minter"]].update(rule["audiences"])
    assert "zoho-procurement-mcp" not in reachable["inventory-agent-principal"]
    assert reachable["procurement-agent-principal"] == {"zoho-procurement-mcp"}
    assert reachable["procurement-agent-adk-principal"] == set()
