# Troubleshooting

## A service does not start

Run `bash scripts/stop_local.sh`, inspect `runtime/*.log`, then start again. Ports 8100–8105 must be free.

```bash
ss -ltnp | grep -E ':810[0-5]'
```

## Python or dependency failure

Use Python 3.11 or newer and rerun `bash scripts/setup.sh`. The setup creates an isolated `.venv`.

## Token signature errors

Stop all services, delete only the generated files `runtime/issuer_private.pem` and `runtime/issuer_public.pem`, then rerun setup. All services must read the same key pair.

## No trace evidence

Run the request first. Local spans are written under `evidence/*.spans.jsonl`. For Cloud Trace export, verify Application Default Credentials, `GOOGLE_CLOUD_PROJECT`, `roles/cloudtrace.agent`, and `TRACE_EXPORTER=gcp` before starting services.

## `adk web` sessions are missing from Agent Platform

This is expected unless ADK Web is configured to use the same persistent Agent Engine session service. Its local session store does not backfill the deployed agent's Sessions tab. Use Trace Explorer for exported OpenTelemetry spans, or invoke the deployed agent to create deployed-runtime sessions.

## Cloud extension fails

Check the exact SDK versions in `cloud/requirements.txt`, region support, APIs, staging bucket, billing, and organization policies. Agent Identity/A2A features may be Preview. Run `gcloud auth application-default login` in environments where browser-based ADC is appropriate.
