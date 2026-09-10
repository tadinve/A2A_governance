# Source Alignment

The supplied governance material separates Agent Registry, Agent Identity, Auth Manager, Agent Gateway, observability, enterprise authorization, delegation, and human control. This implementation makes each decision visible in one procurement story.

| Governance concept | Demo implementation |
|---|---|
| Unique agent identities | Inventory Agent and Procurement Agent have distinct workload credentials/principals |
| Registry is discovery | Protected metadata; grants no invocation right |
| Delegation chain | Human `sub` plus nested agent `act` claims |
| Gateway enforcement | Explicit actor-target-scope allow list |
| Agent-to-agent work | A2A `message/send` |
| Agent-to-system work | Narrow MCP `tools/call` |
| Enterprise authentication | Independent Zoho OAuth clients/scopes |
| Human-in-the-loop | Separate approver session and exact-draft binding |
| Evidence | Audit JSONL and OpenTelemetry spans |

The Zoho emulator replaces the earlier SAP/Salesforce placeholder story with one coherent inventory/procurement system while keeping authentication planes separate.
