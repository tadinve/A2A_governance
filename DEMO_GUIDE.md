# Presenter Guide

## Learning objectives

Participants should be able to explain Agent Identity, delegated authorization, A2A versus MCP, SaaS OAuth, human approval, and trace/audit evidence as separate controls.

## 20-minute walkthrough

### 1. Show the identities and policy

Open `config/registry.json`, `config/policies.json`, and `config/zoho_oauth_clients.json`. Emphasize that Inventory Agent cannot route to the Procurement MCP and neither agent has an approval permission.

### 2. Start the eight components

```bash
bash scripts/start_local.sh
curl -s http://127.0.0.1:8103/identity
curl -s http://127.0.0.1:8104/identity
```

### 3. Run the complete scenario

```bash
bash scripts/run_demo.sh
```

Pause at these moments:

1. Inventory Agent obtains an `inventory.read` token for the Inventory MCP.
2. The Inventory MCP obtains a Zoho token with only `ZohoInventory.items.READ`.
3. Inventory Agent sends `purchase.request` to Procurement Agent through A2A.
4. Procurement Agent uses the Procurement MCP to create and submit a PO.
5. The agent stops at `pending_approval`.
6. The approver signs into the Zoho UI and approves the displayed hash.
7. Inventory Agent queries final status through Procurement Agent.

### 4. Explain the denials

The script proves:

- direct Procurement Agent invocation is blocked;
- approval without a human UI session is blocked;
- an approval MCP tool is not exposed.

Also point to `tests/test_configuration.py`, which asserts that Inventory Agent has no procurement-MCP route and the policy contains no approval scope.

### 5. Show traces and audit

```bash
bash scripts/show_evidence.sh
```

Look for `ITEMS_READ`, `A2A_REQUEST_ACCEPTED`, `PURCHASE_ORDER_CREATED`, `PURCHASE_ORDER_SUBMITTED`, `PURCHASE_ORDER_APPROVED`, and `PURCHASE_ORDER_READ`. The record separates the initiating human, agent actor chain, OAuth client, approver, draft hash, and PO ID.

## Reset

`start_local.sh` clears prior evidence and starts a fresh in-memory Zoho organization. Stop with `bash scripts/stop_local.sh`.
