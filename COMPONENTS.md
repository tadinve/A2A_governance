# Components and Proof Points

| Component | Purpose | Working proof |
|---|---|---|
| Human login | Establish requester identity | `sub=demo-user`, `aud=inventory-agent` |
| Agent identities | Distinguish the two workloads | Separate `/identity` outputs, secrets, and `act` claims |
| Registry | Discover an approved capability | Procurement Agent card, owner, endpoint, skills |
| Identity Broker | Issue/exchange audience-bound credentials | `TOKEN_EXCHANGE_ALLOWED` events |
| Gateway | Enforce actor-target-scope relationships | route allow/deny events |
| A2A | Inventory Agent delegates a task to Procurement Agent | JSON-RPC `message/send` task/artifact |
| Inventory MCP | Narrow read-only tool surface | only `get_inventory` is callable |
| Procurement MCP | Narrow write/status surface | create and status tools; no approval tool |
| Zoho OAuth emulator | Model SaaS connector authentication | two opaque access tokens from separate refresh grants |
| Zoho API emulator | Model items and purchase-order endpoints | PO transitions draft -> submitted -> approved |
| Human approval | Keep approval outside agent authority | UI session, approver role, exact draft hash |
| OpenTelemetry | Trace execution across services | `evidence/*.spans.jsonl` |
| Audit trail | Preserve provenance without secrets | `evidence/audit.jsonl` |

## Discovery is not authorization

- A registry entry says what an agent is and where it can be reached.
- An Agent Identity says which workload authenticated.
- A gateway policy connects that identity to a target and semantic scope.
- A delegated token preserves the initiating human and actor chain.
- Zoho OAuth separately authorizes a connector to SaaS API scopes.
- Human approval separately authorizes the exact business transaction.
