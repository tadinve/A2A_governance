#!/usr/bin/env python3
"""Store the two Zoho MCP server URLs in Secret Manager and grant the agents access.

These URLs embed an API key in their path, which makes them long-lived bearer
credentials issued to us by a third party. SECURITY_NOTES.md names exactly this
category as what Secret Manager is still for: secrets that cannot use a broker,
because the issuer hands us material we must store verbatim. They move to Auth
Manager once it brokers third-party OAuth credentials.

Contrast with the delegation signing key, which lives in Cloud KMS and is
non-exportable -- see cloud/setup_kms_signing.py. The difference is that we can
choose the delegation key's form; we cannot choose Zoho's.

The URLs are read from the environment and are never printed by this script.

  export $(grep -v '^#' .env_zoho_urls | xargs)   # or source it
  python3 cloud/setup_zoho_secrets.py             # create/update + grant
  python3 cloud/setup_zoho_secrets.py --show      # show bindings, never values
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


SECRETS = {
    "zoho-invread-mcp-url": "ZOHO_INVREAD_MCP_URL",
    "zoho-procurewrite-mcp-url": "ZOHO_PROCUREWRITE_MCP_URL",
}
ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"

# Only agents that actually talk to Zoho. Granting more broadly would be the
# over-broad binding this repository argues against.
ZOHO_AGENTS = ("Inventory Agent", "Procurement Agent (A2A)", "Procurement Agent")


def _project() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit("Set GOOGLE_CLOUD_PROJECT first.")
    return project


def _region() -> str:
    region = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    return "us-central1" if region == "global" else region


def agent_principals(project: str, region: str) -> dict[str, str]:
    import json

    import requests

    token = subprocess.run(["gcloud", "auth", "print-access-token"],
                           capture_output=True, text=True, check=True).stdout.strip()
    url = (f"https://{region}-aiplatform.googleapis.com/v1beta1/projects/"
           f"{project}/locations/{region}/reasoningEngines")
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    response.raise_for_status()
    found = {}
    for engine in response.json().get("reasoningEngines", []):
        identity = engine.get("spec", {}).get("effectiveIdentity")
        name = engine.get("displayName", engine["name"])
        if identity and name in ZOHO_AGENTS:
            found[name] = f"principal://{identity}"
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    from google.api_core import exceptions
    from google.cloud import secretmanager

    project, region = _project(), _region()
    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}"

    if args.show:
        for secret_id in SECRETS:
            name = f"{parent}/secrets/{secret_id}"
            try:
                client.get_secret(name=name)
            except exceptions.NotFound:
                print(f"{secret_id}: does not exist")
                continue
            versions = [v for v in client.list_secret_versions(parent=name)
                        if v.state.name == "ENABLED"]
            print(f"{secret_id}: {len(versions)} enabled version(s)")
            policy = client.get_iam_policy(request={"resource": name})
            for binding in policy.bindings:
                print(f"  {binding.role}")
                for member in binding.members:
                    print(f"    {member}")
            if not policy.bindings:
                print("  (no bindings)")
        return 0

    principals = agent_principals(project, region)
    if not principals:
        print("No Zoho-using agents are deployed; secrets will be created without grants.")

    for secret_id, env_var in SECRETS.items():
        value = os.environ.get(env_var, "").strip()
        if not value:
            print(f"SKIP {secret_id}: ${env_var} is not set in this environment")
            continue

        name = f"{parent}/secrets/{secret_id}"
        try:
            client.get_secret(name=name)
        except exceptions.NotFound:
            client.create_secret(parent=parent, secret_id=secret_id,
                                 secret={"replication": {"automatic": {}}})
            print(f"created secret: {secret_id}")

        # Only add a version when the value actually changed, so rotating the
        # Zoho API key is deliberate rather than a side effect of re-running.
        current = None
        try:
            current = client.access_secret_version(
                name=f"{name}/versions/latest").payload.data.decode()
        except Exception:
            pass
        if current == value:
            print(f"{secret_id}: unchanged")
        else:
            version = client.add_secret_version(
                parent=name, payload={"data": value.encode()})
            print(f"{secret_id}: added version {version.name.split('/')[-1]}")

        if principals:
            policy = client.get_iam_policy(request={"resource": name})
            binding = next((b for b in policy.bindings if b.role == ACCESSOR_ROLE), None)
            if binding is None:
                binding = policy.bindings.add()
                binding.role = ACCESSOR_ROLE
            added = [label for label, principal in principals.items()
                     if principal not in binding.members]
            for label, principal in principals.items():
                if principal not in binding.members:
                    binding.members.append(principal)
            if added:
                client.set_iam_policy(request={"resource": name, "policy": policy})
                print(f"{secret_id}: granted {ACCESSOR_ROLE} to {', '.join(added)}")
            else:
                print(f"{secret_id}: all agents already have access")

    print("\nIAM changes take a few minutes to propagate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
