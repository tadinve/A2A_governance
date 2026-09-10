#!/usr/bin/env python3
"""Put the shared delegation issuer key in Secret Manager and grant access.

Replaces packaging a private key into every agent deployment. The key lives in
Secret Manager; each deployed agent fetches it at runtime using its own Agent
Identity, and can only do so because an IAM binding on that specific secret says
it may.

That makes the key handling production-shaped, and it demonstrates the same
authentication/authorization split as the A2A hop on a second resource type:
the agent authenticates as itself, and IAM decides whether it may read.

  python3 setup_issuer_secret.py                  # create/rotate + grant to all agents
  python3 setup_issuer_secret.py --show           # show the secret and its bindings
  python3 setup_issuer_secret.py --revoke AGENT   # remove one agent's access
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

SECRET_ID = "a2a-demo-delegation-issuer"
ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"

# Only these agents participate in the delegation chain. Every other agent in
# the project -- including unrelated ones -- must not be able to read the
# signing key. Granting by "every agent we can see" is precisely the over-broad
# binding this demo exists to argue against.
DELEGATION_AGENTS = ("Inventory Agent", "Procurement Agent (A2A)", "Procurement Agent")


def _project() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit("Set GOOGLE_CLOUD_PROJECT first.")
    return project


def _region() -> str:
    region = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    return "us-central1" if region == "global" else region


def _new_private_key() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def agent_principals(project: str, region: str) -> dict[str, str]:
    """Map display name -> effective identity for every deployed agent."""
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
        if identity and name in DELEGATION_AGENTS:
            found[name] = f"principal://{identity}"
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--revoke", metavar="DISPLAY_NAME")
    parser.add_argument("--rotate", action="store_true",
                        help="add a new key version; redeploy nothing, agents pick it up")
    args = parser.parse_args()

    from google.api_core import exceptions
    from google.cloud import secretmanager

    project, region = _project(), _region()
    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}"
    secret_name = f"{parent}/secrets/{SECRET_ID}"

    if args.show:
        try:
            client.get_secret(name=secret_name)
        except exceptions.NotFound:
            print(f"{SECRET_ID} does not exist yet. Run without --show to create it.")
            return 0
        versions = list(client.list_secret_versions(parent=secret_name))
        print(f"secret   : {secret_name}")
        print(f"versions : {len(versions)} (latest enabled: "
              f"{next((v.name.split('/')[-1] for v in versions if v.state.name == 'ENABLED'), 'none')})")
        policy = client.get_iam_policy(request={"resource": secret_name})
        print("bindings :")
        for binding in policy.bindings:
            print(f"  {binding.role}")
            for member in binding.members:
                print(f"    {member}")
        if not policy.bindings:
            print("  (none)")
        return 0

    # Create the secret if it is missing.
    try:
        client.get_secret(name=secret_name)
        print(f"secret exists: {SECRET_ID}")
        created = False
    except exceptions.NotFound:
        client.create_secret(parent=parent, secret_id=SECRET_ID,
                             secret={"replication": {"automatic": {}}})
        print(f"created secret: {SECRET_ID}")
        created = True

    versions = [v for v in client.list_secret_versions(parent=secret_name)
                if v.state.name == "ENABLED"] if not created else []
    if created or args.rotate or not versions:
        version = client.add_secret_version(
            parent=secret_name, payload={"data": _new_private_key()})
        print(f"added key version: {version.name.split('/')[-1]}")
    else:
        print(f"keeping existing key version (use --rotate to add a new one)")

    # Grant or revoke the accessor role on this one secret.
    policy = client.get_iam_policy(request={"resource": secret_name})
    binding = next((b for b in policy.bindings if b.role == ACCESSOR_ROLE), None)
    principals = agent_principals(project, region)
    if not principals:
        print("none of the delegation agents are deployed yet; skipping grants")
        print(f"expected one of: {', '.join(DELEGATION_AGENTS)}")
        return 0

    if args.revoke:
        target = principals.get(args.revoke)
        if not target:
            sys.exit(f"No deployed agent named {args.revoke!r}")
        if binding and target in binding.members:
            binding.members.remove(target)
            client.set_iam_policy(request={"resource": secret_name, "policy": policy})
            print(f"revoked {ACCESSOR_ROLE} from {args.revoke}")
        else:
            print(f"{args.revoke} does not hold {ACCESSOR_ROLE}")
        return 0

    if binding is None:
        binding = policy.bindings.add()
        binding.role = ACCESSOR_ROLE
    added = [name for name, principal in principals.items()
             if principal not in binding.members]
    for name, principal in principals.items():
        if principal not in binding.members:
            binding.members.append(principal)
    if added:
        client.set_iam_policy(request={"resource": secret_name, "policy": policy})
        print(f"granted {ACCESSOR_ROLE} to: {', '.join(added)}")
    else:
        print("all deployed agents already have access")

    print(f"\nISSUER_SECRET_NAME={secret_name}/versions/latest")
    print("IAM changes take a few minutes to propagate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
