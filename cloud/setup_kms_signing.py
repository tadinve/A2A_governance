#!/usr/bin/env python3
"""Create the delegation signing key in Cloud KMS and grant narrow access.

This replaces cloud/setup_issuer_secret.py, which put an exportable RSA private
key in Secret Manager. The difference is not cosmetic:

* A Secret Manager secret is *designed* to be read back. Anyone holding
  roles/secretmanager.secretAccessor gets the key bytes and can sign forever,
  anywhere, with no further access to the project. Rotation does not help,
  because the leaked copy keeps working until you also distrust it.

* A KMS asymmetric key with purpose ASYMMETRIC_SIGN is non-exportable. There is
  no API that returns the private key -- not to us, not to Google's own console.
  Callers holding roles/cloudkms.signerVerifier can ask KMS to *perform* a
  signature; they cannot obtain the ability to sign elsewhere. Revoking the IAM
  binding ends their access immediately, and every signature is logged.

Two distinct roles keep signing and verifying apart:

  roles/cloudkms.signerVerifier   -> the Auth Broker only. Can request signatures.
  roles/cloudkms.publicKeyViewer  -> verifying agents. Can read the public key.

Secret Manager keeps the secrets that genuinely cannot use a broker -- today the
Zoho OAuth client secrets and refresh tokens, which a third party issues to us as
bearer material. Those move to Auth Manager when it is available; the delegation
private key never belonged in either.

  python3 setup_kms_signing.py            # create key ring, key, and IAM bindings
  python3 setup_kms_signing.py --show     # show the key, its version, and bindings
  python3 setup_kms_signing.py --rotate   # add a new key version
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


KEY_RING = "a2a-demo-delegation"
KEY_ID = "delegation-issuer"
SIGNER_ROLE = "roles/cloudkms.signerVerifier"
VERIFIER_ROLE = "roles/cloudkms.publicKeyViewer"

# Only the Auth Broker may sign. Naming the agents here instead would recreate
# the over-broad grant this demo argues against: an agent that can sign can mint
# any delegation it likes, which is precisely what the broker exists to prevent.
SIGNER_AGENTS = ("Auth Broker",)
VERIFIER_AGENTS = ("Inventory Agent", "Procurement Agent (A2A)", "Procurement Agent")


def _project() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit("Set GOOGLE_CLOUD_PROJECT first.")
    return project


def _region() -> str:
    region = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    return "us-central1" if region == "global" else region


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
        if identity:
            found[name] = f"principal://{identity}"
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--rotate", action="store_true",
                        help="add a new key version; verifiers pick it up by kid")
    args = parser.parse_args()

    from google.api_core import exceptions
    from google.cloud import kms

    project, region = _project(), _region()
    client = kms.KeyManagementServiceClient()
    location = f"projects/{project}/locations/{region}"
    ring_name = f"{location}/keyRings/{KEY_RING}"
    key_name = f"{ring_name}/cryptoKeys/{KEY_ID}"

    if args.show:
        try:
            client.get_crypto_key(name=key_name)
        except exceptions.NotFound:
            print(f"{KEY_ID} does not exist yet. Run without --show to create it.")
            return 0
        versions = [v for v in client.list_crypto_key_versions(parent=key_name)
                    if v.state.name == "ENABLED"]
        print(f"key      : {key_name}")
        print(f"versions : {len(versions)} enabled "
              f"(latest: {versions[-1].name.split('/')[-1] if versions else 'none'})")
        print("exportable: no (ASYMMETRIC_SIGN private keys cannot be read back)")
        policy = client.get_iam_policy(request={"resource": key_name})
        print("bindings :")
        for binding in policy.bindings:
            print(f"  {binding.role}")
            for member in binding.members:
                print(f"    {member}")
        if not policy.bindings:
            print("  (none)")
        return 0

    try:
        client.get_key_ring(name=ring_name)
        print(f"key ring exists: {KEY_RING}")
    except exceptions.NotFound:
        client.create_key_ring(parent=location, key_ring_id=KEY_RING, key_ring={})
        print(f"created key ring: {KEY_RING}")

    try:
        client.get_crypto_key(name=key_name)
        print(f"key exists: {KEY_ID}")
        if args.rotate:
            version = client.create_crypto_key_version(
                parent=key_name, crypto_key_version={})
            print(f"added key version: {version.name.split('/')[-1]}")
    except exceptions.NotFound:
        client.create_crypto_key(
            parent=ring_name,
            crypto_key_id=KEY_ID,
            crypto_key={
                "purpose": kms.CryptoKey.CryptoKeyPurpose.ASYMMETRIC_SIGN,
                "version_template": {
                    "algorithm": kms.CryptoKeyVersion.CryptoKeyVersionAlgorithm.RSA_SIGN_PKCS1_2048_SHA256,
                    "protection_level": kms.ProtectionLevel.SOFTWARE,
                },
            },
        )
        print(f"created non-exportable signing key: {KEY_ID}")

    principals = agent_principals(project, region)
    if not principals:
        print("no agents are deployed yet; skipping IAM grants")
        return 0

    policy = client.get_iam_policy(request={"resource": key_name})

    def grant(role: str, wanted: tuple[str, ...]) -> list[str]:
        binding = next((b for b in policy.bindings if b.role == role), None)
        if binding is None:
            binding = policy.bindings.add()
            binding.role = role
        added = []
        for name, principal in principals.items():
            if name in wanted and principal not in binding.members:
                binding.members.append(principal)
                added.append(name)
        return added

    signers = grant(SIGNER_ROLE, SIGNER_AGENTS)
    verifiers = grant(VERIFIER_ROLE, VERIFIER_AGENTS)
    if signers or verifiers:
        client.set_iam_policy(request={"resource": key_name, "policy": policy})
    print(f"{SIGNER_ROLE}: {', '.join(signers) or 'no change'}")
    print(f"{VERIFIER_ROLE}: {', '.join(verifiers) or 'no change'}")

    versions = [v for v in client.list_crypto_key_versions(parent=key_name)
                if v.state.name == "ENABLED"]
    if versions:
        print(f"\nKMS_SIGNING_KEY={versions[-1].name}")
    print("DELEGATION_SIGNER=kms")
    print("IAM changes take a few minutes to propagate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
