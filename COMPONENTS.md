# Components and Proof Points

| Component | Question it answers | Local implementation | Proof shown |
|---|---|---|---|
| Human identity | Who initiated the task? | `/login` issues T1 for `demo-user` | `sub=demo-user`, `aud=agent-a` |
| Agent Identity | Which workload is acting? | Separate client credential and SPIFFE-style ID for each agent | Agent A/B `/identity`; `act` chain |
| Agent Registry | Which approved agent performs a skill? | Protected registry with identities, endpoints, and skills | `REGISTRY_READ`; discovered skill |
| Auth Manager | How is downstream authorization delegated? | RFC 8693-style subject/actor token exchange | T2 and T3 safe claims |
| IAM-style policy | May actor X access target Y with scope Z? | Declarative `config/policies.json` | allow/deny audit events |
| Agent Gateway | Where is route policy enforced? | Validates target, audience, actor, scope, content | `ROUTE_ALLOWED` or denial |
| A2A protocol | How do agents exchange a task? | Agent Card plus JSON-RPC `message/send` | returned A2A task/artifact |
| Agent B | Who owns backend-specific reasoning/action? | Inventory specialist | `A2A_REQUEST_ACCEPTED` |
| SAP API | Which enterprise resource is protected? | Read-only inventory API | `INVENTORY_READ` with full actor chain |
| Model Armor analogue | Is unsafe content blocked? | Transparent phrase-based classroom simulator | `CONTENT_BLOCKED` |
| OpenTelemetry | What executed and how long did it take? | FastAPI/HTTPX and named spans to JSONL | `evidence/*.spans.jsonl` |
| Audit trail | Who did what, to which target, and why? | Append-only JSONL event stream | `evidence/audit.jsonl` |

## Identity is not a registry entry

These concepts are deliberately separate:

- A **registry entry** is metadata: name, card, endpoint, owner, and skills.
- An **agent identity** is a cryptographic workload principal used during authentication.
- An **authorization binding** connects a principal to a permission on a resource.
- A **delegated token** carries the human subject plus the authenticated agent actor for one audience and narrow scope.

In the Google Cloud extension, the effective Agent Identity is a resource principal such as `principal://.../reasoningEngines/RESOURCE_ID`. It is visible on the deployed agent's Identity tab and in IAM policy bindings where it has been granted a role. It is not necessarily presented as a normal service-account row in IAM.
