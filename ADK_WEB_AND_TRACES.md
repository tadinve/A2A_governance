# ADK Web, Sessions, and OpenTelemetry

Your screenshots are consistent with a working telemetry setup:

- Trace Explorer contains ADK spans such as `call_llm`, `invoke_agent`, and `execute_tool`. OpenTelemetry export is working.
- The Agent Platform deployment's **Sessions** page does not show conversations created by local `adk web`. That is expected.

## Why

| Data | Written by local `adk web` | Deployment page reads from |
|---|---|---|
| Conversation/session | ADK's configured local or external session service | That specific deployed Agent Runtime's managed session service |
| Trace/span | Configured OpenTelemetry exporter | Cloud Trace when exported there |
| Agent identity | Local ADC/user or service account; no managed Agent Identity | The deployed runtime's effective Agent Identity, if enabled |

A trace does not create an Agent Platform session. Matching timestamps or `user_id` values does not join those stores.

## Demo options

### Option A — local UI plus Cloud Trace

Run `adk web` with your existing OpenTelemetry-to-Cloud configuration. Use ADK Web for the conversation and Trace Explorer for spans. Present this as a local execution, not a deployed-runtime session.

### Option B — deployed session plus deployment traces

Deploy with `--otel_to_cloud`, then invoke the **deployed Playground** or the deployed query API. This creates a session owned by that deployment and spans associated with the deployed runtime.

### Option C — use a common persistent session service

Configure the application to use `VertexAiSessionService` with the intended Agent Engine ID. This is an application change; merely exporting OpenTelemetry is not sufficient. Be careful not to mix local test traffic into a production deployment's session store.

## Add Agent Identity to your current ADK deployment

In the agent folder—beside `agent.py`—create `.agent_engine_config.json`:

```json
{ "identity_type": "AGENT_IDENTITY" }
```

Then redeploy as a new Agent Runtime instance:

```bash
adk deploy agent_engine \
  --project "$GOOGLE_CLOUD_PROJECT" \
  --region us-central1 \
  --display_name "Movie Pitch Identity Demo" \
  --otel_to_cloud \
  ./movie_pitch
```

Identity is provisioned when the runtime instance is created. Do not assume adding the config and updating an old service-account-backed instance retrofits its identity; use a new instance for the cleanest demo.

After deployment, use the **Identity** tab for the canonical principal. It appears in IAM when you grant that principal a role, but it is not a service account.
