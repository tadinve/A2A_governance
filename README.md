# Agent Identity, Authorization, and A2A Governance Demo

This package is a self-contained, classroom-ready demonstration of how a human request becomes a governed agent-to-agent call and then an authorized governed enterprise-resource call.

It has two modes:

- **Local mode** runs on any machine with Python 3.11+, Bash, and `curl`. It needs no cloud account, LLM, or API key. It uses real signed JWTs, audience and scope validation, RFC 8693-style token exchange, an A2A-style JSON-RPC message, OpenTelemetry spans, and audit logs. Google Cloud products are explicitly represented as local simulators.
- **Google Cloud extension** deploys a real Agent Development Kit agent to Agent Runtime with Agent Identity and Cloud Trace enabled, then shows its canonical identity and how to bind it in IAM. The local runtime remains the deterministic end-to-end A2A authorization lab.

## Five-minute local demo

```bash
git clone <this-repo> && cd A2A_governance
bash scripts/setup.sh
bash scripts/start_local.sh
bash scripts/run_demo.sh
bash scripts/show_evidence.sh
bash scripts/stop_local.sh
```

For one-command verification after setup:

```bash
bash scripts/verify.sh
```

The successful path is:

```text
Human -> Agent A -> Registry -> Identity Broker -> Gateway -> Agent B
                                                        Agent B -> Identity Broker -> Gateway -> SAP API
```

The demo prints only safe token claims; it never prints bearer credentials. It also proves two negative cases: direct access to Agent B is rejected, and unsafe content is blocked at the gateway.

## What to read

- [ARCHITECTURE.md](ARCHITECTURE.md): trust boundaries, tokens, and sequence
- [COMPONENTS.md](COMPONENTS.md): every component, control, and proof point
- [DEMO_GUIDE.md](DEMO_GUIDE.md): presenter script and expected results
- [SOURCE_ALIGNMENT.md](SOURCE_ALIGNMENT.md): mapping to the supplied governance deck
- [SECURITY_NOTES.md](SECURITY_NOTES.md): what is real, simulated, and production-grade
- [ADK_WEB_AND_TRACES.md](ADK_WEB_AND_TRACES.md): why local sessions and Cloud Trace differ
- [cloud/README.md](cloud/README.md): actual Agent Identity and IAM extension
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): common fixes

## Ports

| Port | Component | Role |
|---:|---|---|
| 8100 | Agent Registry simulator | Discovers approved agents and resources |
| 8101 | Identity Provider/Auth Manager simulator | Issues and exchanges signed tokens |
| 8102 | Agent Gateway simulator | Enforces route, scope, and content policy |
| 8103 | Agent A: Inventory Orchestrator | Accepts the user request and delegates |
| 8104 | Agent B: Inventory Specialist | Receives A2A and invokes SAP |
| 8105 | SAP Inventory API simulator | Returns protected enterprise data |

Swagger UIs are available at `http://127.0.0.1:PORT/docs` while the demo is running.

## Trace export

Local JSONL export is the default and is always available. To additionally send spans to Google Cloud Trace, authenticate Application Default Credentials, grant the caller `roles/cloudtrace.agent`, set the project, and start with:

```bash
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
export TRACE_EXPORTER=gcp
bash scripts/start_local.sh
```

This answers a common ADK Web confusion: **sessions and traces are different data planes**. Local `adk web` sessions remain in its configured session service and do not automatically appear under a deployed Agent Engine's Sessions tab. OpenTelemetry spans can still appear in Trace Explorer if the process exports them to Cloud Trace. This demo makes that separation visible.

## Requirements and cost

Local mode is free. The cloud extension uses billable Google Cloud resources and Preview APIs; run its explicit cleanup command when finished.
