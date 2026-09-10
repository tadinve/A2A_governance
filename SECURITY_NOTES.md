# Security and Production Notes

## What is genuinely exercised locally

- RS256 signatures and public-key verification
- issuer, expiry, not-before, audience, and scope validation
- separate client authentication for Agent A and Agent B
- subject/actor token exchange shaped like RFC 8693
- nested delegation evidence
- policy-enforced gateway routing
- A2A-style Agent Card and JSON-RPC `message/send`
- positive and negative authorization paths
- distributed context propagation and OpenTelemetry spans

## What is intentionally simulated

| Demo component | Production Google Cloud analogue |
|---|---|
| Login endpoint | Cloud Identity / workforce identity |
| SPIFFE-style local IDs | Agent Identity resource principals |
| Identity broker | Google Cloud authentication and Auth Manager |
| JSON policy file | IAM policies and gateway policy |
| FastAPI gateway | Agent Gateway |
| Phrase filter | Model Armor/content-safety controls |
| Inventory JSON | SAP system and its authorization model |
| Local JSONL | Cloud Trace, Cloud Logging, and Cloud Audit Logs |

`X-Gateway-Verified` is only a local trust-boundary marker. It is not secure across an untrusted network. Production enforcement should use platform routing, authenticated ingress, TLS/mTLS, and IAM rather than a forgeable header.

The client secrets are conspicuously marked demo values and committed only so the package is self-contained. Production secrets belong in managed identity and Secret Manager flows. The private signing key is generated at setup time, ignored by version control, and never printed.

## Least privilege

The local policy allows only:

- Agent A -> Agent B with `inventory.read`
- Agent B -> SAP with `records.read`

There is no wildcard route and no direct Agent A -> SAP grant. The Cloud extension uses a project-level role for workshop simplicity; replace it with the narrowest resource-level binding available in your environment.

## Cloud status

Agent Identity, Agent Runtime A2A support, and related interfaces may be Preview and can change. Verify region availability, organization policy, SDK version, and current documentation before production adoption.
