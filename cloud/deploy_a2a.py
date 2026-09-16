#!/usr/bin/env python3
"""Deploy Procurement Agent as a genuine A2A agent on Agent Runtime.

Unlike `adk deploy agent_engine`, this creates an A2aAgent, which exposes the
A2A protocol operations (on_message_send, on_get_task, ...) rather than the
ordinary stream_query surface.
"""
from __future__ import annotations

import os
import sys

import vertexai

DISPLAY_NAME = "Procurement Agent (A2A)"
REQUIREMENTS = [
    "google-cloud-aiplatform[adk,agent_engines]",
    "a2a-sdk[http-server]",
    "PyJWT>=2.8",
    "cryptography>=42",
    "google-cloud-secret-manager>=2.20",
    "requests>=2.31",
    "google-cloud-kms>=3.0",
]


def main() -> int:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    bucket = os.environ.get("STAGING_BUCKET", f"gs://{project}-a2a-staging")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from procurement_a2a import build_agent

    client = vertexai.Client(project=project, location=location)

    existing = next((a for a in client.agent_engines.list()
                     if getattr(a.api_resource, "display_name", None) == DISPLAY_NAME), None)

    # Agent Runtime rejects a reasoningEngine create/update outright if any
    # declared env var carries an empty string: "Required field is not set" on
    # that variable's index, with no indication of which name it was. So the
    # required ones are checked explicitly here, and the optional ones are
    # left out of the dict entirely rather than sent empty -- omitting
    # ZOHO_ORGANIZATION_ID lets governance.py auto-discover it from Zoho
    # (the code path it already has for exactly this case), which an empty
    # string sent instead would not trigger.
    required = {
        # Only the *public* key name. This agent verifies delegation tokens
        # with roles/cloudkms.publicKeyViewer and cannot sign: the private
        # half is non-exportable and only the Auth Broker may invoke it.
        "KMS_SIGNING_KEY": os.environ.get("KMS_SIGNING_KEY", ""),
        "AUTH_BROKER_URL": os.environ.get("AUTH_BROKER_URL", ""),
        # This agent reaches live Zoho through the MCP connectors, whose URLs
        # it reads from Secret Manager. That lookup needs the project id,
        # which Agent Runtime injects itself -- GOOGLE_CLOUD_PROJECT is a
        # reserved name and setting it here is rejected outright. (ADK
        # quietly strips it from a staged .env, which is why the Inventory
        # Agent appears to set it and does not.)
        "GOOGLE_CLOUD_LOCATION": location,
        "DEMO_SKU": os.environ.get("DEMO_SKU", "DEMO-WIDGET-A"),
        "ZOHO_REORDER_POLICY": (os.environ.get("ZOHO_REORDER_POLICY", "").strip()
                               or '{"DEMO-WIDGET-A":{"target_stock":100,"min_order_quantity":1}}'),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        print(f"ERROR: {', '.join(missing)} must be set (non-empty) to deploy this "
              f"agent; Agent Runtime rejects an empty env var outright.", file=sys.stderr)
        return 2
    env_vars = dict(required)
    optional_org_id = os.environ.get("ZOHO_ORGANIZATION_ID", "").strip()
    if optional_org_id:
        env_vars["ZOHO_ORGANIZATION_ID"] = optional_org_id

    config = dict(
        display_name=DISPLAY_NAME,
        description="Drafts governed purchase orders over the A2A protocol.",
        staging_bucket=bucket,
        requirements=REQUIREMENTS,
        extra_packages=["./procurement_a2a"],
        identity_type="AGENT_IDENTITY",
        env_vars=env_vars,
    )

    agent = build_agent()
    if existing:
        name = existing.api_resource.name
        print(f"Updating existing deployment: {name}")
        result = client.agent_engines.update(name=name, agent=agent, config=config)
    else:
        print("Creating a new A2A deployment...")
        result = client.agent_engines.create(agent=agent, config=config)

    resource = result.api_resource
    print(f"\nresource : {resource.name}")
    identity = getattr(getattr(resource, "spec", None), "effective_identity", None)
    print(f"identity : {identity or '(none - service account backed)'}")
    print(f"a2a url  : https://{location}-aiplatform.googleapis.com/v1beta1/{resource.name}/a2a")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
