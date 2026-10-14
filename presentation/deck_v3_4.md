# Agent Identity and Delegated Procurement — deck v3.4

A live Google Cloud demonstration of workload identity, delegated
authorization, human approval, and denials.

*Architecture review and demo readiness. 16 slides. Text transcription of
`A2A_Governance_Demo_Deck_v3_4.pptx`.*

---

## 1. Agent Identity and Delegated Procurement

A live Google Cloud demonstration of workload identity, delegated
authorization, human approval, and denials.

```
Inventory Agent
  Read-only inventory, reorder decision
        |
        |  A2A delegation
        v
Procurement Agent
  Draft purchase order, human approval required
```

---

## 2. The business scenario

*Low stock triggers a controlled handoff. A human controls the irreversible decision.*

| # | Actor | Does |
|---|---|---|
| 1 | Requester | Asks for an inventory review |
| 2 | Inventory Agent | Reads stock through a read-only MCP connector |
| 3 | Procurement Agent | Receives the proposal through A2A |
| 4 | Purchase order draft | Has an exact vendor, quantity, price, and hash |
| 5 | Approver | Approves the displayed draft before submission |

**The agents recommend and draft. They do not approve. The connector submits
only after server-side approval checks pass.**

---

## 3. Trust boundaries

*Each boundary has a different credential and a different enforcement point.*

**Human boundary** — Requester or approver. Cloud Run UI entry point;
IAM-gated service plus demo session.

**Google Cloud boundary**
- Inventory Agent — Agent Runtime identity, InvRead access
- Auth Broker — verifies agent caller, checks minting policy
- Cloud KMS — non-exportable signing key
- Procurement Agent — A2A target identity, ProcureWrite access

**SaaS boundary** — InvRead (read-only connector), ProcureWrite (write
connector), both on one shared Zoho account.

Flows: `Browser -> UI -> agent`, then `Google ID token` to the broker, then
`Delegated token`.

---

## 4. Four identity and authorization planes

*A successful request must satisfy the relevant plane at every hop.*

| Plane | Credential | What it proves | What it does not prove |
|---|---|---|---|
| Human requester | Cloud Run invoker plus app session | Initiate an inventory check and approve a draft | Does not authorize an agent or a SaaS API call |
| Agent workload | Agent Runtime identity and Google ID token | Which deployed workload called the broker | Does not grant a business action by itself |
| Delegated authorization | KMS-signed JWT with sub, aud, scope, act | Requester provenance and a narrow permission | Does not prove the requester is a real corporate identity in this demo |
| Zoho connectors | Per-connector Secret Manager URL with embedded connector key | Expose configured read or write MCP tools | Do not prove separate Zoho identities; both use one Zoho account |

---

## 5. How the broker verifies workload identity

*The caller cannot choose its own broker principal or delegation actor.*

1. **Agent Runtime** — creates a lifecycle-bound effective identity for each deployed agent
2. **Agent Registry** — returns the effective identity by display name after deployment
3. **Broker config** — maps that verified principal to one broker client and one delegation actor
4. **Google ID token** — agent requests a broker-audience ID token and presents it to Cloud Run
5. **Broker check** — verifies the ID token and matches email, sub, or azp to the allowed principal
6. **Policy decision** — checks token kind, audience, scope, and lifetime before asking KMS to sign

**Result: the actor in the delegation comes from the authenticated runtime
principal, not from a caller-provided string.**

---

## 6. Delegation sequence

*The broker derives the chain from verified evidence and narrows it for each target.*

```
Inventory Agent --> Auth Broker        start human grant for demo-user
Inventory Agent --> Procurement Agent  A2A request with delegated purchase.request token
Procurement Agent --> Auth Broker      exchange delegation for purchaseorder.create
Procurement Agent --> ProcureWrite     MCP create-draft call
ProcureWrite --> Shared Zoho account   managed connector uses its Zoho authorization
```

**The human JWT never reaches Zoho. The Zoho OAuth token never reaches either
agent. Both connector surfaces use one Zoho account.**

---

## 7. Delegated token contents and verification

*Targets treat the token as a bounded authorization grant, not as a free-form instruction.*

| Claim | Value |
|---|---|
| `iss` | Auth Broker |
| `sub` | demo-user |
| `aud` | procurement-agent |
| `scope` | purchase.request |
| `act` | inventory-agent |
| `exp` | short lifetime, never longer than the source grant |

Target verification: **signature** (KMS public key), **issuer**, **audience**,
**expiry**, **scope**, **actor chain**.

> **Important demo limit:** `sub=demo-user` comes from the demo broker. Google
> attests the agent caller, not the human identity.

---

## 8. Identity and policy denials

*`cloud/verify_cloud.py` performs five read-only denial checks. Re-run it
immediately before presenting.*

| | Case | Against | Result |
|---|---|---|---|
| DENIED | No delegated token | A2A purchase-order request | No delegated token presented |
| DENIED | Forged delegated token | A2A purchase-order request | Signature verification rejects it |
| DENIED | Agent requests approval | Procurement Agent | Approval tool is absent |
| DENIED | Standalone Procurement Agent | Attempts to start a human grant | Broker returns HTTP 403 |
| DENIED | Inventory Agent | Attempts to obtain purchase authority | No minting policy permits it |

`--write` adds a sixth check: it repeats one reorder and proves idempotency
against live Zoho. It creates a real draft purchase order.

**The point is not that requests fail. It is that each refusal names the broken
trust condition before a purchase order exists.**

---

## 9. KMS signing custody

*The delegation private key stays non-exportable. Agents can verify, but cannot mint.*

```
Auth Broker                 Cloud KMS                    Inventory and
dedicated Cloud Run   -->   ASYMMETRIC_SIGN        -->   Procurement Agents
service account             non-exportable key           public-key verification only
      roles/cloudkms.signerVerifier      roles/cloudkms.publicKeyViewer
```

- **No private key file** — tests reject PEM files and environment key material
- **No signing secret** — Secret Manager contains Zoho connectors, not the delegation key
- **Auditable signing** — every `asymmetricSign` call can appear in Cloud Audit Logs

---

## 10. Human approval boundary

*Approval binds an authenticated approver to the exact draft, not to an agent's message.*

```
Purchase-order draft  --approval record-->  Approver session  --approved draft-->  Server submission
vendor, lines, price                        server checks role                     one idempotent send
SHA-256 content hash                        reviewed version and hash              rechecks approval
```

Controls that prevent a silent approval:
- the request body cannot provide an approver identity or an `approved=true` flag
- the application checks approver role, draft version, content hash, and approval expiry
- an immutable approval record accepts only one decision for a draft version
- a submission retry reconciles by reference rather than creating another order

> **Provider limitation:** the connected Zoho organization has no native
> purchase-order approval workflow. The demo must describe provider status
> honestly as draft.

---

## 11. Managed Zoho connector boundaries

*Separate secrets and tool surfaces protect the agents. Both endpoints are
authorized by one Zoho account.*

**InvRead** — secret `zoho-invread-mcp-url`, read-only tool surface:
`list_organizations`, `list_items`, `get_item`, `list_item_purchase_orders`,
`get_purchase_order`

**ProcureWrite** — secret `zoho-procurewrite-mcp-url`, write additions:
`create_purchase_order`, `submit_purchaseorder`

Each URL embeds a connector API key; the managed connector owns the Zoho OAuth
relationship. Agents never receive a Zoho OAuth token.

**No approval, issue, or send-to-supplier tool appears in either MCP surface.**

---

## 12. Evidence and observability

*Trace Explorer and audit logs explain the decision chain without logging
bearer credentials.*

**Cloud Trace**
- Agent Runtime produces ADK spans such as `call_llm`, `invoke_agent`, `execute_tool`
- the Auth Broker instruments FastAPI and sets `auth.minter`, `auth.subject`, `auth.audience`, `auth.token_kind`, `auth.actor` on signing spans
- Cloud Trace export requires `TRACE_EXPORTER=gcp` and `cloudtrace.agent` on the executing workload

**Audit evidence**
- `BROKER_CALLER_AUTHENTICATED` identifies the broker minter after ID token verification
- `DELEGATION_TOKEN_SIGNED` records subject, target, scope, actor chain, KMS backend
- `DELEGATION_MINT_DENIED` explains a failed policy request before a token exists
- purchase-order events carry the approval hash and provider reference without secrets

UI telemetry is active in commit `430fd66`: FastAPI, requests, and
background-thread spans remain correlated with the Inventory Agent request.
Verify the trace live before presenting.

---

## 13. Live demonstration sequence

*Use the UI for the browser-to-Inventory Agent check; use cloud verification
for the A2A purchase proof.*

1. **Show the three demo Agent Identities** — Agent Registry and IAM identify Inventory Agent, Procurement Agent, and the A2A deployment. Explain or delete any unrelated Movie Pitch engine.
2. **Run `cloud/verify_cloud.py`** — show all five denials. Writes no purchase order.
3. **Check real stock in the Cloud Run UI** — in agent mode, Check Inventory uses streamQuery to invoke the deployed Inventory Agent for DEMO-WIDGET-A.
4. **Show the correlated check trace** — Trace Explorer: UI request, background work, outbound agent request, Inventory Agent activity.
5. **Trigger one A2A reorder** — use the explicit `--write` check only after announcing it creates a real Zoho draft and proves idempotency.
6. **Review and approve a UI draft** — the UI is a guarded approval application. Its draft/submission path is separate from the A2A Procurement Agent demonstration.

> Do not start the live demo immediately after IAM changes. The deployment
> script itself warns that target-level IAM propagation can take several
> minutes.

---

## 14. Demo readiness assessment

*The identity, denial, and browser-to-Inventory Agent check are demonstrable
now. Keep the remaining workflow boundary precise.*

**Ready to demonstrate**
- Agent Registry binds each Agent Runtime identity to a separate broker client
- broker caller verification and KMS keep the delegation key non-exportable
- Inventory Agent has target-level IAM permission to invoke the A2A Procurement Agent
- supplied log shows five denials passed; re-run before the demo
- UI Check Inventory invokes Inventory Agent through streamQuery and emits correlated telemetry
- separate secrets and tool surfaces protect two connectors on one shared Zoho account

**Frame carefully in the demo**
- the human subject remains `demo-user`, not an IAP or enterprise identity; the app login uses demo credentials after Cloud Run IAM
- only the UI inventory check invokes Agent Runtime; draft and submission call direct governance/Zoho code
- single-instance SQLite loses approval and idempotency state at a new revision
- Zoho seeding defaults to a dry run; only `--apply` writes
- the connector split does not isolate distinct Zoho identities

**Verdict:** ready for a strong identity, delegation, denial, approval-control,
and trace demo. Do not present it as a production-ready end-to-end purchasing
workflow.

---

## 15. Presenter language for an accurate demo

*The strongest explanation makes the boundary clear without overstating any component.*

**Say**
- Agent Runtime attests the deployed workload that called the Auth Broker
- the broker derives the delegation actor from that caller
- KMS signs after policy checks; agents can verify tokens without holding a signing key
- the A2A target requires a delegation and refuses a missing or forged one
- UI Check Inventory calls the deployed Inventory Agent over streamQuery and records correlated trace evidence
- human approval binds the exact draft hash before the application submits a purchase order

**Do not claim**
- the `demo-user` subject proves the human identity — it does not
- the whole browser purchasing flow runs through both agents — only the UI inventory check invokes Agent Runtime today
- Zoho approves or sends the order to the supplier — the connected tenant has no native approval workflow
- the two Zoho connectors are separate Zoho identities — they are one account with separate tool surfaces and secret access
- Agent Identity alone grants purchase authority — IAM, broker policy, scope, and approval still decide access

---

## 16. What closes the remaining gaps

1. **Human identity** — replace demo-user issuance with IAP or an enterprise OIDC assertion. Verify group or role claims server-side.
2. **One end-to-end agent path** — extend the UI from its current Inventory Agent check to delegate the purchase workflow through A2A Procurement Agent, preserving run and approval correlation.
3. **Durable approval state** — replace single-instance SQLite with Cloud SQL or Firestore and dispatch background work through authenticated Cloud Tasks.
4. **Provider identity separation** — use distinct Zoho OAuth identities or integration users for read and write connectors, not just different tool surfaces and URL secrets.
5. **Provider execution** — use a Zoho role and workflow that can receive an approved order without allowing the agent or integration user to self-approve.

**The control-plane lesson survives these changes. The improvements make the
human and operational story equally strong.**
