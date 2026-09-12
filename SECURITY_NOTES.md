# Security and Production Notes

## What is exercised

- RS256 human/agent JWT signatures; issuer, expiry, audience, and scope validation
- delegation signing isolated in an Auth Broker; no other component can sign
- RFC 8693-shaped subject/actor exchange with nested `act` provenance, where the
  broker derives `sub` and `act` from verified tokens rather than from the request
- delegation required, not optional, on the deployed A2A write path
- separate Inventory Agent and Procurement Agent credentials, and a separate
  broker authorization identity for each deployed principal
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
| Auth Broker's in-process key | Cloud KMS asymmetric key, non-exportable |
| MCP JSON-RPC subset | Production MCP SDK/server |
| Zoho OAuth/API/UI emulator | Zoho Accounts and Zoho Inventory |
| JSONL telemetry | Cloud Trace, Logging, Audit Logs |

The Zoho payload shapes and scope names intentionally resemble Zoho Inventory, but the emulator is not a conformance test and does not promise wire-level parity with every API version.

## Where the delegation signing key lives

The delegation signing key is the most consequential secret here: whoever holds
it can mint any token every other component trusts. So it is held in exactly one
place, and that place is not a file.

**Cloud KMS (deployed).** The key is created with purpose `ASYMMETRIC_SIGN` and
is non-exportable -- no API returns its private half, to anyone. The Auth Broker
signs by calling `asymmetricSign`; it never possesses key bytes. Two IAM roles
keep the capability split honest:

| Role | Held by | Can |
|---|---|---|
| `roles/cloudkms.signerVerifier` | Auth Broker only | request a signature |
| `roles/cloudkms.publicKeyViewer` | verifying agents | read the public key |

Procurement Agent verifies delegated tokens with the public key and is
structurally unable to mint one. Revoking the broker's binding stops signing
immediately, and every signature lands in Cloud Audit Logs.

**Local (offline demo).** The Auth Broker generates an RSA key in memory at
startup and never writes it anywhere. This is a weaker guarantee than KMS -- a
local attacker who can read the broker's process memory can extract it -- but it
preserves the property the lesson depends on: exactly one component can sign, and
no key exists in any package, environment variable, or file.

**What this replaced.** Earlier revisions generated `runtime/issuer_private.pem`
that every service read, then copied a private key into each agent package, then
moved it to Secret Manager. All three shared a flaw: the key was *readable*, so
possessing it granted the ability to sign anywhere, forever, and rotation could
not retract a copy already taken. `scripts/init_demo.py` now deletes any stale
`*.pem` it finds, and `tests/test_key_custody.py` fails if one returns.

**What Secret Manager is still for.** Only secrets that cannot use a broker --
today the Zoho OAuth client secrets and refresh tokens, which a third party
issues to us as bearer material we must store verbatim. Those move to Auth
Manager once it brokers third-party OAuth credentials, at which point the demo
holds no long-lived readable secret at all. The delegation private key never
belonged in Secret Manager, because KMS can hold it in a form nobody can read.

## What the broker will sign, and what it works out for itself

A broker that signs whatever subject and actor chain its caller sends is an
expensive way of notarising the caller's own claims. Authentication tells you
*which* agent asked; it says nothing about whether the grant it describes ever
existed. So for a delegated token the request body carries evidence only:

| Field | Who decides it |
|---|---|
| `sub` | read from the subject token, after the broker verifies it against its own key |
| `act` | the caller's authenticated identity, nested over the chain the subject token already carried |
| `aud`, `scope` | requested, then checked against both the delegation policy and the minting policy |
| `exp` | requested, then clamped so a delegation never outlives the grant it extends |

There is no parameter for asserting a subject or an actor chain, and the request
model rejects unknown fields, so a stale caller sending the old `subject` plus
`actor_chain` shape fails loudly instead of being quietly ignored.

Two ways the acting identity becomes known, and neither is "the caller said so".
A deployed agent authenticates with its own Agent Identity, and its broker client
entry names the single delegation actor that principal may act as. The Identity
Broker is a delegation service acting for several agents rather than being one,
so it presents the agent's own credential and the actor is read from that
token's verified `sub`.

## One principal, one set of minting rights

Each deployed reasoning engine has its own entry in `config/broker_clients.json`
and its own rules in `config/policies.json`. Grouping them under a shared client
would authenticate three distinct workloads and then decline to use the answer.

| Broker client | May mint | Cannot |
|---|---|---|
| `inventory-agent-principal` | the demo human grant; delegations to the inventory connector and to Procurement Agent | reach the procurement connector at all |
| `procurement-agent-principal` | delegations to the procurement connector, extended from a grant it was handed | mint a human grant, or extend a grant addressed to Inventory Agent |
| `procurement-agent-adk-principal` | nothing | draft anything; the non-A2A deployment receives no delegation and can invent none |

The second row is what closes the A2A write path. Procurement Agent now refuses
a `create_po` that arrives without a delegated token, and if that check were
ever removed the broker would still refuse to mint the human grant the old
fallback depended on. Two independent controls, in two different components.

**Still open.** The human at the top of the chain is `demo-user`, a claim this
demo mints and signs, not an identity anything authenticated. Inventory Agent
retains the right to mint it because it is the agent a human talks to directly.
Replacing that with IAP-authenticated sign-in is the remaining gap, and until it
closes, the nested `act` chain is evidence about *workloads*, not about a person.

## Important boundaries

`X-Gateway-Verified` is a classroom marker and forgeable on an untrusted network. Production must use authenticated ingress and TLS/mTLS. Demo client secrets and refresh tokens are committed for self-sufficiency; production Zoho credentials belong in Auth Manager (or Secret Manager until it is available) and should be injected only into the MCP connector. The delegation signing key is the exception and belongs in KMS, as described above.

The agents never receive a Zoho access or refresh token. The Zoho Inventory connector has only `items.READ`; the Procurement connector has purchase-order create/read/update scopes. Because Zoho's UPDATE scope can be broader than the business permission, the demo does not expose an approve tool and uses a separate human session. In production, also use a Zoho integration user/role that cannot approve its own requests where the account edition supports it.

Approval is bound to vendor, reference, line items, rates, quantities, and total. A mutation changes the hash and invalidates approval. Never treat a chat response, agent claim, or generic “approved” flag as transaction approval.

Do not log authorization headers, access tokens, refresh tokens, or client secrets. Restrict outbound connector traffic to the correct Zoho data-center hostnames.
