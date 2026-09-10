# Troubleshooting

## A port is busy

Run `bash scripts/stop_local.sh`, inspect `runtime/*.log`, and start again. Ports 8100–8107 must be free.

## A service fails health checks

Run `tail -n 100 runtime/*.log`. Re-run `bash scripts/setup.sh` if the virtual environment is missing dependencies.

## A second run returns an existing PO

The emulator implements idempotent creation per request ID. Restart the services to reset its in-memory organization.

## No local traces

Run the business flow first, then `bash scripts/show_evidence.sh`. Each service writes its own `evidence/*.spans.jsonl` file.

## No Cloud Trace entries

Confirm Application Default Credentials, `roles/cloudtrace.agent`, `GOOGLE_CLOUD_PROJECT`, and `TRACE_EXPORTER=gcp`. Cloud Trace export is independent of Agent Engine Sessions.
