# Two Agents, Two Real Identities

The local demo proves the governance model deterministically, but its agent
identities are SPIFFE-*style* strings this repo makes up. This extension deploys
the same two agents to Agent Runtime so each gets a **real, system-attested
Google Cloud Agent Identity** that IAM understands.

That closes the demo's weakest gap. Everything else stays simulated on purpose;
this one control becomes genuine.

## What gets deployed

| Agent | Folder | Tools | Cannot |
|---|---|---|---|
| Inventory Agent | `inventory_agent/` | `check_stock`, `request_reorder_from_procurement_agent`, `attempt_to_create_purchase_order_directly`, `show_my_identity` | create a purchase order |
| Procurement Agent | `procurement_agent/` | `draft_purchase_order`, `get_purchase_order_status`, `explain_approval_boundary`, `show_my_identity` | approve anything -- or, in this non-A2A deployment, draft anything either |

`procurement_agent/` is the ADK deployment; nothing reaches it over A2A, so it
never receives a human delegation. Its broker client grants it no minting rights
at all, so `draft_purchase_order` demonstrates the denial rather than working
around it. Drafting happens in `procurement_a2a/`, which is handed a delegation
by Inventory Agent.

Each deployment authenticates as its own broker client -- `inventory-agent-principal`,
`procurement-agent-principal`, `procurement-agent-adk-principal` -- with its own
minting rules. Three attested identities collapsed into one policy identity would
authenticate each workload and then ignore which one it was.

Each folder carries `.agent_engine_config.json` containing
`{"identity_type": "AGENT_IDENTITY"}`, which is what causes Agent Runtime to
provision the identity at instance creation.

`governance.py` is a self-contained copy of the delegation, policy, and
draft-hash logic from `src/governance_demo/security.py`. Agent Runtime uploads
only the agent folder, so the module is duplicated in each rather than imported,
and no signing key is packaged in at all: the agents call the Auth Broker to
mint tokens and read only the *public* key to verify them.
The token exchange, the nested `act` chain, the policy denials, and the SHA-256
approval binding all run for real in-process.

## Deploy

```bash
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="us-central1"
gcloud auth application-default login

cloud/.venv/bin/python cloud/deploy_a2a.py   # Procurement Agent (A2A)
bash cloud/deploy_agents.sh                  # Inventory + ADK Procurement
```

Then create the KMS signing key and grant the two narrow roles:

```bash
cloud/.venv/bin/python cloud/setup_kms_signing.py          # create key + grant
cloud/.venv/bin/python cloud/setup_kms_signing.py --show   # inspect bindings
export KMS_SIGNING_KEY="...printed by the command above..."
```

Run this **after** the agents exist, because it grants access to their Agent
Identity principals, which are only created at deployment. The A2A agent deploys
first so its resource name can be staged into Inventory's `.env`.

Re-running updates deployments in place rather than creating duplicates. Deploy
one ADK agent at a time with `bash cloud/deploy_agents.sh inventory` or
`... procurement`.

Agent Identity requires the project to sit under an **organization**. Without
one, deployment still succeeds but falls back to a service account and the whole
point is lost. Check with `gcloud projects get-ancestors "$GOOGLE_CLOUD_PROJECT"`.

## The demo: authentication is not authorization

This is the sequence worth showing, because the interesting result comes first.

**1. Two agents, two principals.**

```bash
bash cloud/deploy_agents.sh   # prints both, or:
curl -sS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/$GOOGLE_CLOUD_PROJECT/locations/us-central1/reasoningEngines" \
  | python3 cloud/show_agent_identities.py
```

Two deployments, two different `principal://` URIs. Neither is a service
account, and neither can be found by searching for one.

**2. Ask Inventory Agent to reorder.**

In the deployment's Playground: *"Stock is low on DEMO-WIDGET-A. Ask Procurement
Agent to draft a purchase order for 73 units."*

Inventory Agent mints a delegated token and sends it to Procurement Agent over
A2A. See step 3 for the protocol details and the current status of this hop when
it originates inside the runtime.

**3. Real A2A, verified end to end.**

`Procurement Agent (A2A)` is deployed as an `A2aAgent` (`agentFramework: a2a`,
`identityType: AGENT_IDENTITY`) serving the A2A protocol at
`.../reasoningEngines/{id}/a2a`. Three things must be right to call it:

- the path is `/a2a/message:send`, **not** `:streamQuery`. An ordinary ADK agent
  does not speak A2A at all, and calling one that way proves nothing about A2A;
- the `A2A-Version: 1.0` header is required, or the handler assumes 0.3 and
  refuses with `VERSION_NOT_SUPPORTED`;
- the agent card is at `/a2a/v1/card`.

Verified with `cloud/a2a_send.py` and from Inventory Agent in the runtime:

| Call | Result |
|---|---|
| agent card | 200, both skills |
| valid delegated token | 200, PO drafted, `pending_approval` |
| **no delegated token** | **denied, `No delegated token presented`** |
| forged token | denied, signature rejected |
| wrong scope | denied, `lacks the 'purchase.request' scope` |
| asked to approve | refused, no such capability |

The third row used to be a 200. The executor treated the delegation as optional
and minted a substitute `demo-user` chain when none arrived, so a caller that
simply left the token out got a real purchase order with a complete-looking
provenance chain behind it. A write now requires the token, and the Auth Broker
grants this principal no rule with which to mint the replacement, so removing
the check would not reopen the path.

**3a. Retrying a request does not buy the goods twice.**

A purchase order is real money, and a lost response is the normal case, not the
exceptional one. So the A2A payload carries an `operation_id` that Inventory
Agent *derives* -- `sha256(human | sku | quantity)` -- rather than generates.
Procurement Agent turns it into the Zoho reference number, and `create_draft`
looks for that reference before creating anything, so the second attempt
returns the first order with `idempotent_replay: true`.

It used to be `uuid4()` per attempt, which guaranteed the opposite: every retry
produced a new reference, and therefore a second purchase order. A caller that
omits `operation_id` gets one derived from the same three facts, so even a
hand-rolled `a2a_send.py` retry is safe. Note the A2A `messageId` stays unique
per transmission -- it identifies the message, not the business operation, and
conflating the two is how this defect gets reintroduced.

The delegated token is verified **cryptographically** by Procurement Agent --
signature, issuer, audience and scope. The human `sub` and the nested `act`
chain genuinely survive the network hop. That is what the shared KMS key is for:
two separately deployed agents need a common issuer to verify each other's
tokens.

**3b. How a deployed agent authenticates outbound: mTLS.**

Agent Identity access tokens are **certificate-bound**. A Google-managed
Context-Aware Access policy enforces mTLS/DPoP so a token only works from the
runtime it was issued to. This has one very practical consequence:

> A raw `AuthorizedSession` bearer request over ordinary TLS gets **401**.
> That is not an IAM result. It is the anti-replay protection doing its job --
> an unbound token looks exactly like a stolen one.

Google Cloud client libraries handle the binding automatically, which is why
`google-cloud-storage` and the `vertexai` client reach **403** from the same
runtime where a hand-rolled request gets 401. To do it by hand:

```python
session = google.auth.transport.requests.AuthorizedSession(credentials)
session.configure_mtls_channel()          # returns None; sets session.is_mtls
if session.is_mtls:
    host = f"{region}-aiplatform.mtls.googleapis.com"
```

`configure_mtls_channel()` returns `None` and records the outcome on
`session.is_mtls`. Testing its return value silently disables mTLS and produces
a 401 that looks like a platform limitation but is not.

Google documents a client-library call, `remote_agent.on_message_send(...)`,
which would replace this hand-rolled transport. It is **not available in
google-cloud-aiplatform 1.165.1**, which this repo pins: `agent_engines.get()`
returns a plain model exposing no A2A operations, on `v1beta1` or otherwise. A
2.x release may provide it. Treat that as an experiment to run, not a known fix:
keep this working mTLS path until a replacement passes the same tests in the
table above.

**3c. Two credential planes. Do not conflate them.**

This demo has two independent credential systems in play, and only one of them
is Google's:

| | Google Cloud Agent Identity | Demo delegation JWT |
|---|---|---|
| Issued by | Agent Runtime, system-attested | This repo's Auth Broker, signing via KMS |
| Proves | which deployed workload is calling | nothing Google vouches for |
| Enforced by | Google Cloud IAM | Procurement Agent's own code |
| Carries | the agent principal | `sub: demo-user`, nested `act` chain |
| Real? | yes | a classroom model of RFC 8693 |

The `sub`/`act` chain is a **useful model** of end-user delegation, and the
signature checks on it are genuine. But `demo-user` was never authenticated by
Google, or by anything else: this demo mints that claim itself. Do not present
the nested actor chain as evidence that Google authenticated a human.

What Google actually authenticated is the *agent*, and that is what the
401/403/200 sequence below measures.

**3d. Authentication versus authorization, measured.**

With the mTLS binding in place, the target-level IAM binding decides the result.
The gating permission is `aiplatform.reasoningEngines.query`, which is **not**
included in `roles/aiplatform.agentDefaultAccess` -- the only role agents hold
by default. So agents cannot invoke each other until someone says so.

| State | Result |
|---|---|
| no mTLS binding | **401 UNAUTHENTICATED** -- credential not accepted |
| mTLS, no target-level grant | **403** `aiplatform.reasoningEngines.query` denied |
| mTLS + `roles/aiplatform.user` on the target | **200** |

Grant it on the **target resource**, not the project:

```bash
python3 cloud/iam_binding.py \
  --resource projects/PROJECT/locations/REGION/reasoningEngines/PROCUREMENT_ID \
  --member 'principal://INVENTORY_AGENT_IDENTITY' \
  --role roles/aiplatform.user            # --revoke to remove, --show to inspect
```

> **Do not hand-write a `setIamPolicy` body.** That API **replaces the entire
> policy**: every binding missing from your request is deleted. These engines
> carry a self-binding (`roles/aiplatform.agentContextEditor`) that a careless
> call silently destroys. `iam_binding.py` reads the current policy, edits one
> binding, and writes it back **with its `etag`**, so a concurrent change is
> rejected rather than clobbered.

> **Presenting this live:** IAM changes take a few minutes to propagate. A
> revoked binding kept returning 200 for about three minutes before flipping to
> 403. Change the policy before the session, not during it.

**3e. Where the signing key lives, and why that matters.**

The delegation key is a **Cloud KMS asymmetric key** with purpose
`ASYMMETRIC_SIGN`. Its private half is non-exportable: there is no API that
returns those bytes, to us or to anyone. The Auth Broker signs by asking KMS to
perform the operation:

```python
digest = hashlib.sha256(signing_input).digest()
response = client.asymmetric_sign(request={"name": KMS_SIGNING_KEY,
                                           "digest": {"sha256": digest}})
```

Three things follow, and all three are the point:

1. **There is no key to steal.** Not in the deployment bundle, not in an
   environment variable, not in the repository, not in a secret anyone can read
   back. Agents receive the key's *resource name*, which is an identifier, not
   material. A repository or image leak yields nothing usable.
2. **Signing is a revocable capability, not a possession.** Only the Auth Broker
   holds `roles/cloudkms.signerVerifier`. Verifying agents hold
   `roles/cloudkms.publicKeyViewer`, which lets them read the public key and
   nothing else -- so Procurement Agent can check a delegation it could never
   mint. Revoke the broker's binding and signing stops *immediately and
   everywhere*, which is not true of a key someone has already copied.
3. **Rotation is additive.** `--rotate` creates a new key version; tokens carry
   a `kid`, and verifiers refresh on signature mismatch. No redeployment, and no
   window where two agents hold different keys.

Every signature is a KMS API call, so Cloud Audit Logs record who signed what
and when -- evidence that a file-based key cannot produce.

**What this replaced.** Earlier revisions of this demo copied a private key into
each agent package, then moved it to Secret Manager. Secret Manager was a real
improvement over packaging, but it shares the underlying flaw: the secret is
*designed to be read back*, so `roles/secretmanager.secretAccessor` hands over
the ability to sign anywhere, forever, and revocation cannot retract a copy
already taken. KMS removes the copy from existence.

**What Secret Manager is still for.** Secrets that cannot use a broker -- here
the Zoho OAuth client secrets and refresh tokens, which a third party issues as
bearer material we must store verbatim. Those move to Auth Manager once it
brokers third-party OAuth credentials. The delegation private key never belonged
in either.

**4. Ask Inventory Agent to create the purchase order itself.**

*"Can't you just create the purchase order yourself?"* It calls
`attempt_to_create_purchase_order_directly`, which is refused by the delegation
policy — a second, independent control that IAM has nothing to do with.

**5. Ask Procurement Agent to approve its own draft.**

*"Now approve PO-1001."* It has no approval tool and must refuse. Approval stays
a human act in the Zoho UI, bound to an exact draft hash.

## What this does not deploy

The Registry, Identity Broker, Gateway, MCP servers, and Zoho emulator stay
local. The deployed agents carry their governance logic in-process instead of
calling those services, because a deployed agent cannot reach `127.0.0.1`.

Agent Gateway is **not** required to make A2A work -- that is demonstrated
above without it. It is an additional governance layer (outbound policy, DPoP
and credential handling, content inspection, audit) and is not modelled here.

Deploying the full control plane to Cloud Run is a larger job with four real
obstacles, all of them worth naming before anyone attempts it:

- the delegation signing key is held by the Auth Broker alone, so eight Cloud Run services
  would each mint their own and every cross-service verification would fail;
  they must all point at the same Auth Broker and KMS key;
- `X-Gateway-Verified` is forgeable, which is harmless on localhost and a real
  bypass on a public URL, so services must be `--no-allow-unauthenticated`;
- `config/registry.json` hardcodes `127.0.0.1` endpoints that only exist after
  deployment;
- `audit.jsonl` is a local file, so the unified evidence timeline fragments
  across containers and needs GCS or Cloud Logging.

## Cleanup

Delete only the two deployments this script created, from Agent Platform ->
Deployments. Remove the IAM binding if you granted one. Nothing is deleted
automatically, so a shared workshop project stays safe.

## References

- [Authenticate using an agent's own authority](https://docs.cloud.google.com/iam/docs/auth-agent-own-identity)
- [Agent Identity overview](https://docs.cloud.google.com/iam/docs/agent-identity-overview)
- [Use Agent Identity with Agent Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-identity)
- [Agent Gateway overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview)
