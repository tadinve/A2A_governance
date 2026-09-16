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
| 8101 | Identity Broker/Auth Manager | Authenticates clients and decides which delegations are permitted |
| 8102 | Agent Gateway | Audience, actor, scope, route, and content enforcement |
| 8103 | Inventory Agent | Checks stock and orchestrates reorder |
| 8104 | Procurement Agent | Owns PO drafting and status capabilities |
| 8105 | Zoho Inventory MCP | Exposes only `get_inventory` |
| 8106 | Zoho Procurement MCP | Exposes create-draft and status tools; never approve |
| 8107 | Zoho Inventory Emulator | OAuth server, inventory/PO API, and human approval UI API |
| 8108 | Auth Broker | Holds the delegation signing key and is the only component that can sign |

Swagger UI is at `http://127.0.0.1:PORT/docs` while running.

## Deploy to Google Cloud

One command rebuilds the whole thing in a bare project, which is what makes it
usable in an ephemeral lab:

```bash
python3 -m venv cloud/.venv
cloud/.venv/bin/pip install -r cloud/requirements.txt

bash deploy_to_gcp.sh YOUR_PROJECT_ID
bash deploy_to_gcp.sh YOUR_PROJECT_ID --skip-ui --skip-seed   # agents only
```

It creates the KMS signing key, deploys the Auth Broker and the three agents,
reads each agent's attested identity back out of the GEAP Agent Registry to
generate the broker's authorization config, redeploys the broker with it, and
puts the purchasing UI on Cloud Run. Nothing is pinned to a project: engine ids,
the organization id, the project number and the service URLs are all discovered.

Then check that the governance still holds:

```bash
cloud/.venv/bin/python cloud/verify_cloud.py          # denials only, writes nothing
cloud/.venv/bin/python cloud/verify_cloud.py --write  # also proves a retry is idempotent
```

The project must sit under an **organization**, or Agent Runtime issues a
service account instead of an Agent Identity and the per-agent authorization
this demo is built on cannot work.

The UI is deployed `--no-allow-unauthenticated`, and Cloud Run does no
interactive browser sign-in, so reach it with
`gcloud run services proxy a2a-inventory-ui --region us-central1`. It is pinned
to a single instance because its state is SQLite on the instance filesystem;
that state does not survive a new revision. See [SECURITY_NOTES.md](SECURITY_NOTES.md).

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
