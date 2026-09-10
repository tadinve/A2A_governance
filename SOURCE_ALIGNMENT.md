# Alignment to the Supplied Governance Material

The supplied **Google Cloud Agent Governance** PDF describes a governance stack spanning Agent Registry, Agent Identity, Auth Manager, Agent Gateway, Model Armor, observability, evaluation, Security Command Center, and an SAP authorization flow.

This implementation keeps the core runtime path executable and makes each responsibility independently observable:

| Governance concept | Demo realization | Deliberate clarification |
|---|---|---|
| Register agents and MCP/tool endpoints | `config/registry.json` and protected Registry API | Registration does not create identity or grant access |
| Assign a unique identity to A1/A2 | Independent Agent A/B credentials and SPIFFE-style IDs | Cloud mode uses actual Agent Identity principals |
| Configure permissions | Explicit token-exchange and gateway route policies | Authentication alone is never treated as authorization |
| Obtain T2/T3 tokens | Subject/actor token exchange | Raw tokens are never displayed; safe claims are shown |
| Route through Agent Gateway | All A2A and SAP calls pass through port 8102 | Target, actor, audience, scope, and content are checked |
| Call SAP/MCP | Agent B invokes the protected inventory API | Agent A has no direct SAP route |
| Observe activity | JSONL audit stream and OpenTelemetry spans | Sessions and traces are documented as distinct data planes |
| Enforce safety | Visible content-control simulator | It is labeled as a simulator, not represented as Model Armor |

The PDF's final identity/SAP sequence diagrams visually combine several control-plane participants. This demo separates Registry, identity/token exchange, route policy, gateway enforcement, A2A transport, and backend authorization so a presenter can answer “which component made this decision?” at every hop.

Evaluation and Security Command Center are described in the source as broader lifecycle/governance services. They are not faked as runtime microservices here because they are not on the authorization path. The demonstration instead produces evaluation-ready positive/negative cases and security evidence that those services could consume.
