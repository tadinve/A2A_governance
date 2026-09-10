#!/usr/bin/env python3
"""Add or remove one IAM binding on a reasoningEngine, preserving the rest.

setIamPolicy REPLACES the whole policy. Hand-writing the body drops every
binding you forgot to include. This reads the current policy, edits one binding,
and writes it back with its etag so a concurrent change fails loudly instead of
being silently clobbered.

  python3 iam_binding.py --resource projects/P/locations/L/reasoningEngines/ID \
      --member 'principal://...' --role roles/aiplatform.user [--revoke]
  python3 iam_binding.py --resource ... --show
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import requests


def _token() -> str:
    return subprocess.run(["gcloud", "auth", "print-access-token"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _endpoint(resource: str, verb: str) -> str:
    region = resource.split("/locations/")[1].split("/")[0]
    return f"https://{region}-aiplatform.googleapis.com/v1beta1/{resource}:{verb}"


def get_policy(resource: str) -> dict:
    response = requests.post(_endpoint(resource, "getIamPolicy"), json={}, timeout=60,
                             headers={"Authorization": f"Bearer {_token()}"})
    response.raise_for_status()
    return response.json()


def set_policy(resource: str, policy: dict) -> dict:
    response = requests.post(_endpoint(resource, "setIamPolicy"), json={"policy": policy},
                             timeout=60, headers={"Authorization": f"Bearer {_token()}"})
    if response.status_code == 409:
        sys.exit("Policy changed since it was read (etag mismatch). Re-run.")
    response.raise_for_status()
    return response.json()


def show(policy: dict) -> None:
    print(f"etag: {policy.get('etag')}")
    for binding in policy.get("bindings", []):
        print(f"  {binding['role']}")
        for member in binding.get("members", []):
            print(f"    {member}")
    if not policy.get("bindings"):
        print("  (no bindings)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--member")
    parser.add_argument("--role")
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    policy = get_policy(args.resource)
    if args.show:
        show(policy)
        return 0
    if not (args.member and args.role):
        parser.error("--member and --role are required unless --show is given")

    bindings = policy.setdefault("bindings", [])
    binding = next((b for b in bindings if b.get("role") == args.role), None)
    members = binding.get("members", []) if binding else []

    if args.revoke:
        if args.member not in members:
            print(f"{args.member} does not hold {args.role}; nothing to do")
            show(policy)
            return 0
        members.remove(args.member)
        if not members:
            bindings.remove(binding)
        action = "revoked"
    else:
        if args.member in members:
            print(f"{args.member} already holds {args.role}")
            show(policy)
            return 0
        if binding:
            binding["members"] = members + [args.member]
        else:
            bindings.append({"role": args.role, "members": [args.member]})
        action = "granted"

    # The etag round-trips, so a concurrent edit is rejected rather than lost.
    updated = set_policy(args.resource, policy)
    print(f"{action} {args.role}\n")
    show(updated)
    print("\nIAM changes take a few minutes to propagate. Do this before a demo, "
          "not during one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
