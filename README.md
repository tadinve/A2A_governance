# Agent Identity, A2A, MCP, and Zoho Governance Demo

This self-contained classroom demo shows a realistic procurement workflow:

1. a human asks **Inventory Agent** to check an item;
2. Inventory Agent uses a read-only MCP server to query the Zoho Inventory emulator;
3. low stock causes Inventory Agent to call **Procurement Agent** through A2A;
4. Procurement Agent uses a separate MCP server and OAuth client to create and submit a purchase-order draft;
5. a human reviews and approves the exact draft in a simulated Zoho UI;
6. Inventory Agent obtains final status through Procurement Agent.

The package needs only Python 3.11+, Bash, and `curl`. It uses signed RS256 JWTs, RFC 8693-style token exchange, audience and scope checks, A2A-style JSON-RPC, MCP-style `tools/call`, Zoho-style OAuth refresh-token exchange, human session authentication, tamper-evident approval, idempotency, audit events, and OpenTelemetry spans. No cloud account, LLM, or external SaaS account is required.

## Five-minute demo

```bash
git clone git@github.com:tadinve/A2A_governance.git
cd A2A_governance
bash scripts/setup.sh
bash scripts/verify.sh
```

Or run each stage:

```bash
bash scripts/start_local.sh
bash scripts/run_demo.sh
bash scripts/show_evidence.sh
bash scripts/stop_local.sh
```

Raw bearer credentials are never printed. The script also proves that direct calls, approval without a human session, and an agent-visible approval tool are denied.

## Components and ports

| Port | Component | Responsibility |
|---:|---|---|
| 8100 | Agent Registry | Agent discovery metadata |
| 8101 | Identity Broker/Auth Manager | Human token, agent credentials, delegated tokens |
| 8102 | Agent Gateway | Audience, actor, scope, route, and content enforcement |
| 8103 | Inventory Agent | Checks stock and orchestrates reorder |
| 8104 | Procurement Agent | Owns PO drafting and status capabilities |
| 8105 | Zoho Inventory MCP | Exposes only `get_inventory` |
| 8106 | Zoho Procurement MCP | Exposes create-draft and status tools; never approve |
| 8107 | Zoho Inventory Emulator | OAuth server, inventory/PO API, and human approval UI API |

Swagger UI is at `http://127.0.0.1:PORT/docs` while running.

## Read next

- [ARCHITECTURE.md](ARCHITECTURE.md): trust planes, sequence, and tokens
- [COMPONENTS.md](COMPONENTS.md): every component and proof point
- [DEMO_GUIDE.md](DEMO_GUIDE.md): presenter script
- [SECURITY_NOTES.md](SECURITY_NOTES.md): production mapping and caveats
- [ADK_WEB_AND_TRACES.md](ADK_WEB_AND_TRACES.md): sessions versus Cloud Trace
- [cloud/README.md](cloud/README.md): actual Google Cloud Agent Identity extension
- [cloud/DEPLOYED_AGENTS.md](cloud/DEPLOYED_AGENTS.md): deploy both agents to Agent Runtime with real identities

## Trace export

JSONL traces are always written locally. To additionally export to Google Cloud Trace:

```bash
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
export TRACE_EXPORTER=gcp
bash scripts/start_local.sh
```

Local `adk web` sessions do not automatically appear under a deployed Agent Engine's Sessions tab. OpenTelemetry spans can still appear in Trace Explorer when the process exports them.
