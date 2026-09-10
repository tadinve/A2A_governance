# Presenter Guide

## Learning objectives

By the end, participants can explain:

1. why an agent needs a workload identity distinct from its human caller;
2. how authentication differs from authorization;
3. how a registry, gateway, auth manager, and A2A protocol have different jobs;
4. how user context can survive a multi-agent call without forwarding reusable credentials;
5. where to find identity, IAM, audit, session, and trace evidence.

## 20-minute local demonstration

### 1. Frame the actors — 3 minutes

Open `ARCHITECTURE.md`. Say: “The human is the subject. Agent A and Agent B are independently authenticated workload actors. SAP trusts neither the prompt nor an agent name in a header; it trusts a signed, audience-bound token delivered through the allowed route.”

Show `config/registry.json`, then `config/policies.json`. Point out that discovery does not grant access.

### 2. Start and identify — 3 minutes

```bash
bash scripts/start_local.sh
curl -s http://127.0.0.1:8103/identity
curl -s http://127.0.0.1:8104/identity
```

Expected: two different SPIFFE-style identities. Explain that these are local teaching identities; the cloud extension creates actual Google Cloud Agent Identity principals.

### 3. Run the allowed flow — 6 minutes

```bash
bash scripts/run_demo.sh
```

Pause on T2 and T3 claims. T2 has the user as `sub` and Agent A as `act`. T3 has the user as `sub`, Agent B as the current actor, and Agent A nested beneath it. Audience changes at every hop.

Expected answer: SAP reports the quantity for `CK-GPU-42`.

### 4. Show denial behavior — 3 minutes

The same script calls Agent B directly and sends a deliberately unsafe request. Both return HTTP 403. Explain that a useful authorization demo must show a controlled failure, not just a happy path.

### 5. Show evidence — 3 minutes

```bash
bash scripts/show_evidence.sh
```

Walk down `USER_TOKEN_ISSUED`, `REGISTRY_READ`, `TOKEN_EXCHANGE_ALLOWED`, `ROUTE_ALLOWED`, `A2A_REQUEST_ACCEPTED`, and `INVENTORY_READ`. Then show `CONTENT_BLOCKED`. The OpenTelemetry section proves all six services emitted spans.

### 6. Connect to Google Cloud — 2 minutes

Use `cloud/README.md`. The cloud version's key moment is:

1. deploy Agent B and Agent A with Agent Identity enabled;
2. call before IAM grant and capture the denial;
3. grant Agent A's principal permission to invoke Agent B;
4. repeat and capture success in Agent Platform Traces and Cloud Audit Logs.

## Suggested audience questions

- “Would putting Agent B in the Registry let Agent A call it?” No. Discovery and authorization are independent.
- “Can Agent A reuse the user's token against SAP?” No. T1 is audience-bound to Agent A. Each hop requires exchange.
- “Will local ADK Web sessions appear in a deployed Agent Engine Sessions tab?” No. Those sessions use different session services. Exported OpenTelemetry spans can still appear in Trace Explorer.
- “Will an Agent Identity appear in IAM?” Its principal appears in IAM policy bindings after it is granted access; inspect the deployed agent's Identity tab for the canonical identity.

## Reset

`start_local.sh` clears prior JSONL evidence. Stop the processes with `bash scripts/stop_local.sh`.
