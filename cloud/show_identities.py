#!/usr/bin/env python3
from __future__ import annotations

import os

import vertexai


def main() -> None:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if location == "global":
        location = "us-central1"
    client = vertexai.Client(
        project=project,
        location=location,
        http_options={"api_version": "v1beta1"},
    )
    print(f"{'DISPLAY NAME':<32} EFFECTIVE IDENTITY")
    print("-" * 120)
    for deployed in client.agent_engines.list():
        resource = deployed.api_resource
        spec = getattr(resource, "spec", None)
        identity = getattr(spec, "effective_identity", None) if spec else None
        print(f"{getattr(resource, 'display_name', '-'):<32} {identity or '(service account / no Agent Identity)'}")


if __name__ == "__main__":
    main()
