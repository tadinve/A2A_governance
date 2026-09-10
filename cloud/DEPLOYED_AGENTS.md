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
| Procurement Agent | `procurement_agent/` | `draft_purchase_order`, `get_purchase_order_status`, `explain_approval_boundary`, `show_my_identity` | approve anything |

Each folder carries `.agent_engine_config.json` containing
`{"identity_type": "AGENT_IDENTITY"}`, which is what causes Agent Runtime to
provision the identity at instance creation.

`governance.py` is a self-contained copy of the delegation, policy, and
draft-hash logic from `src/governance_demo/security.py`. Agent Runtime uploads
only the agent folder, so the module is duplicated in each rather than imported,
and the shared signing key is fetched from Secret Manager at runtime rather
than packaged in.
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

Then provision the shared issuer key and grant the agents access to it:

```bash
cloud/.venv/bin/python cloud/setup_issuer_secret.py          # create + grant
cloud/.venv/bin/python cloud/setup_issuer_secret.py --show   # inspect
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

In the deployment's Playground: *"Stock is low on CK-GPU-42. Ask Procurement
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
| forged token | denied, signature rejected |
| wrong scope | denied, `lacks the 'purchase.request' scope` |
| asked to approve | refused, no such capability |

The delegated token is verified **cryptographically** by Procurement Agent --
signature, issuer, audience and scope. The human `sub` and the nested `act`
chain genuinely survive the network hop. That is what `demo_keys/` exists for:
two separately deployed agents need a shared issuer key to verify each other's
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
| Issued by | Agent Runtime, system-attested | This repo's `demo_keys/` issuer |
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

The delegation issuer key is held in **Secret Manager**, not packaged into
either agent. At startup each agent calls:

```python
client = secretmanager.SecretManagerServiceClient()
private = client.access_secret_version(name=ISSUER_SECRET_NAME).payload.data
```

Three things follow, and all three are the point:

1. **No private key is distributed.** The deployment bundle contains no key
   material, and none is committed. A repository or an image leak yields
   nothing.
2. **The fetch is itself an IAM decision.** The agent authenticates with its own
   Agent Identity, and reads the secret only because
   `roles/secretmanager.secretAccessor` is bound on **that one secret** for
   **that one principal**. Revoke it and the agent starts up but cannot sign or
   verify anything. It is the same authentication/authorization split as the A2A
   hop, on a different resource type.
3. **Rotation stops being a redeployment.** `--rotate` adds a new version and
   both agents pick it up, because both read `versions/latest`. When the key was
   packaged, rotating it meant redeploying every agent in lockstep or the pair
   would hold different keys and reject each other's tokens.

Access is scoped deliberately. `setup_issuer_secret.py` grants only to the three
agents that participate in the delegation chain. Other agents in the same
project, with perfectly valid Agent Identities, are not granted and cannot read
it -- granting to "every agent we can see" is the over-broad binding this demo
argues against.

The public key is **derived** from the private key rather than stored
separately, so the pair cannot drift apart.

For production, this is the shape to keep: Secret Manager or KMS, or a managed
issuer such as Agent Identity auth manager. What it replaces -- a private key
copied into every agent deployment -- is the anti-pattern.

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

- the RS256 signing key is generated per process, so eight Cloud Run services
  would each mint their own and every cross-service verification would fail;
  it must move to Secret Manager;
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
