# Security and Production Notes

## What is exercised

- RS256 human/agent JWT signatures; issuer, expiry, audience, and scope validation
- RFC 8693-shaped subject/actor exchange with nested `act` provenance
- separate Inventory Agent and Procurement Agent credentials
- route policy at a gateway
- A2A-style Agent Card and JSON-RPC messages
- MCP-style narrow `tools/call` interfaces
- two independent Zoho OAuth refresh grants and opaque access tokens
- human session authentication and approver-role check
- exact-draft SHA-256 approval binding
- idempotent PO creation
- OpenTelemetry and append-only audit events

## Deliberately simulated

| Demo | Production equivalent |
|---|---|
| SPIFFE-style labels and local JWT issuer | Google Cloud Agent Identity/IAM |
| JSON policy and FastAPI gateway | IAM plus Agent Gateway |
| Local token broker | Auth Manager/platform token exchange |
| MCP JSON-RPC subset | Production MCP SDK/server |
| Zoho OAuth/API/UI emulator | Zoho Accounts and Zoho Inventory |
| JSONL telemetry | Cloud Trace, Logging, Audit Logs |

The Zoho payload shapes and scope names intentionally resemble Zoho Inventory, but the emulator is not a conformance test and does not promise wire-level parity with every API version.

## Important boundaries

`X-Gateway-Verified` is a classroom marker and forgeable on an untrusted network. Production must use authenticated ingress and TLS/mTLS. Demo client secrets and refresh tokens are committed for self-sufficiency; production credentials belong in Secret Manager and should be injected only into the MCP connector.

The agents never receive a Zoho access or refresh token. The Zoho Inventory connector has only `items.READ`; the Procurement connector has purchase-order create/read/update scopes. Because Zoho's UPDATE scope can be broader than the business permission, the demo does not expose an approve tool and uses a separate human session. In production, also use a Zoho integration user/role that cannot approve its own requests where the account edition supports it.

Approval is bound to vendor, reference, line items, rates, quantities, and total. A mutation changes the hash and invalidates approval. Never treat a chat response, agent claim, or generic “approved” flag as transaction approval.

Do not log authorization headers, access tokens, refresh tokens, or client secrets. Restrict outbound connector traffic to the correct Zoho data-center hostnames.
