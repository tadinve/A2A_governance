# Security and Production Notes

## What is exercised

- RS256 human/agent JWT signatures; issuer, expiry, audience, and scope validation
- delegation signing isolated in an Auth Broker; no other component can sign
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

## Important boundaries

`X-Gateway-Verified` is a classroom marker and forgeable on an untrusted network. Production must use authenticated ingress and TLS/mTLS. Demo client secrets and refresh tokens are committed for self-sufficiency; production Zoho credentials belong in Auth Manager (or Secret Manager until it is available) and should be injected only into the MCP connector. The delegation signing key is the exception and belongs in KMS, as described above.

The agents never receive a Zoho access or refresh token. The Zoho Inventory connector has only `items.READ`; the Procurement connector has purchase-order create/read/update scopes. Because Zoho's UPDATE scope can be broader than the business permission, the demo does not expose an approve tool and uses a separate human session. In production, also use a Zoho integration user/role that cannot approve its own requests where the account edition supports it.

Approval is bound to vendor, reference, line items, rates, quantities, and total. A mutation changes the hash and invalidates approval. Never treat a chat response, agent claim, or generic “approved” flag as transaction approval.

Do not log authorization headers, access tokens, refresh tokens, or client secrets. Restrict outbound connector traffic to the correct Zoho data-center hostnames.
