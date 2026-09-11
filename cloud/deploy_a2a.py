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

    config = dict(
        display_name=DISPLAY_NAME,
        description="Drafts governed purchase orders over the A2A protocol.",
        staging_bucket=bucket,
        requirements=REQUIREMENTS,
        extra_packages=["./procurement_a2a"],
        identity_type="AGENT_IDENTITY",
        env_vars={
            # Only the *public* key name. This agent verifies delegation tokens
            # with roles/cloudkms.publicKeyViewer and cannot sign: the private
            # half is non-exportable and only the Auth Broker may invoke it.
            "KMS_SIGNING_KEY": os.environ.get("KMS_SIGNING_KEY", ""),
            "AUTH_BROKER_URL": os.environ.get("AUTH_BROKER_URL", ""),
        },
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
