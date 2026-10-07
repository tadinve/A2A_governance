# Real Google Cloud Agent Identity Extension

This optional extension creates a billable Agent Runtime deployment. It demonstrates the piece that cannot be reproduced off-cloud: a system-attested, lifecycle-bound Google Cloud Agent Identity.

## Prerequisites

- Google Cloud project with billing
- `gcloud` authenticated and Application Default Credentials configured
- permission to enable APIs, deploy Agent Runtime, create its staging resources, and change IAM if you run the grant step
- a supported Agent Runtime region

## Deploy

```bash
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="us-central1"
gcloud auth application-default login
bash cloud/deploy_identity_agent.sh
```

The checked-in `identity_agent/.agent_engine_config.json` contains:

```json
{ "identity_type": "AGENT_IDENTITY" }
```

ADK reads that file during `adk deploy`. The `--otel_to_cloud` flag sends deployed-runtime telemetry to Cloud Trace.

## Show the identity

```bash
cloud/.venv/bin/python cloud/show_identities.py
```

You can also open Agent Platform -> Deployments -> **Agent Identity Demo** -> **Identity**. The value is a principal URI similar to:

```text
principal://agents.global.project-PROJECT_NUMBER.system.id.goog/resources/aiplatform/projects/PROJECT_NUMBER/locations/us-central1/reasoningEngines/AGENT_ENGINE_ID
```

For organization-owned projects, the trust domain uses `org-ORGANIZATION_ID` instead of `project-PROJECT_NUMBER`.

## Show authentication versus authorization

Deployment creates and authenticates the identity, but that does not grant it arbitrary access. Copy the exact effective identity printed above and grant one demonstrative role:

```bash
bash cloud/grant_role.sh \
  'principal://COPY_THE_EXACT_EFFECTIVE_IDENTITY' \
  roles/browser
```

Now inspect IAM's **View by principals** or the resource policy. The agent principal appears because it is a member of a policy binding. It does not become a service account and should not be searched for as one.

For a production resource, replace `roles/browser` with the narrowest resource-level role. Useful operating roles commonly include `roles/serviceusage.serviceUsageConsumer`, `roles/aiplatform.expressUser`, and resource-specific viewer/user roles.

## Demonstrate it live

1. Open the deployment's **Identity** tab and copy the effective identity.
2. Open **IAM** and show that identity only where a binding exists.
3. Open **Playground**, ask: `Who are you and how do you get authorized?`
4. Open **Traces** or Trace Explorer and show the deployed invocation.
5. Contrast **Sessions**: a local `adk web` session is not a session of this deployment. Invoke the deployed Playground to populate deployed sessions.

## A2A continuation

[DEPLOYED_AGENTS.md](DEPLOYED_AGENTS.md) takes this further: it deploys **both** Inventory Agent and Procurement Agent as separate Agent Runtime instances, each with its own Agent Identity, and has Inventory Agent call Procurement Agent using that identity. The first call returns 403 until you grant an IAM role, which makes the authentication-versus-authorization distinction concrete.

`bash cloud/deploy_agents.sh` deploys both. For the `RemoteA2aAgent` variant, follow Google's current [A2A Agent Runtime codelab](https://codelabs.developers.google.com/adk-a2a-agent-runtime). The authorization rule is the same: the caller's ADC becomes its Agent Identity inside Agent Runtime, and IAM on the target decides whether that principal may invoke it.

## Agent Gateway (real, Google-managed)

Distinct from `src/governance_demo/gateway_app.py`, which is the local FastAPI
simulation of the same pattern and is not a Google product. Three separate
commands, because only the first one is inert:

```bash
bash cloud/setup_agent_gateway.sh          # provision; binds nothing
python3 cloud/verify_agent_gateway.py --pre-bind
python3 cloud/gateway_destinations.py --list

bash cloud/bind_agents_to_gateway.sh       # reroutes all agent egress
python3 cloud/verify_agent_gateway.py --post-bind
python3 cloud/verify_cloud.py              # the demo must still pass

python3 cloud/verify_agent_gateway.py --observed   # what egress really happened
bash cloud/configure_gateway_enforcement.sh        # fail closed; last
```

`deploy_to_gcp.sh --with-agent-gateway` performs the provisioning step only.
Binding is reversible with `--unbind`; enforcement with `--revert`.

## Cleanup

Delete only the deployment created by this lab from Agent Platform -> Deployments. If you granted a role, remove that exact IAM binding. The package does not automate deletion so it cannot accidentally remove another workshop deployment.

If an Agent Gateway was provisioned, unbind the agents first
(`bash cloud/bind_agents_to_gateway.sh --unbind`) and only then delete the
gateway, the authorization policy, and the authorization extension. Deleting a
gateway that agents are still bound to leaves them pointing at a resource that
no longer exists.

## Current authoritative references

- [Use Agent Identity with Agent Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-identity)
- [Agent Identity overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/agent-identity-overview)
- [A2A on Agent Runtime codelab](https://codelabs.developers.google.com/adk-a2a-agent-runtime)
- [Agent Gateway overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview)
- [Route Agent Runtime traffic through Agent Gateway](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy)
- [Agent Gateway egress codelab](https://codelabs.developers.google.com/agw-cuj-arun-egress-gmcp)
