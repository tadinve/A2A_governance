#!/usr/bin/env python3
"""Read each agent's attested identity from the GEAP Agent Registry.

The Auth Broker's minting policy is keyed on *which workload is calling*, so it
needs each deployed agent's principal. Those were once pasted into
config/broker_clients.json by hand, which pinned the file to one project: engine
ids, project number and organization id all change when the demo is rebuilt in a
fresh lab, and every agent is then refused.

The registry already knows. Agent Runtime registers each deployment and records
its identity under a system attribute, so the principal can be looked up by
display name and is authoritative rather than transcribed.

  python3 cloud/registry_principals.py                 # print what is registered
  python3 cloud/registry_principals.py --write-clients # regenerate broker_clients.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import google.auth
import google.auth.transport.requests

RUNTIME_IDENTITY = "agentregistry.googleapis.com/system/RuntimeIdentity"
ROOT = Path(__file__).resolve().parents[1]

# Which broker client each deployment authenticates as, and what it may mint.
# Display name is the join key because it is the one identifier that survives a
# rebuild; everything else about a deployment is allocated fresh.
AGENT_CLIENTS = {
    "Inventory Agent": {
        "client_id": "inventory-agent-principal",
        "delegation_actor": "inventory-agent",
        "may_mint": ["user_access_token", "delegated_access_token"],
        "description": ("Inventory Agent on Agent Runtime. The only deployed principal "
                        "that may start a delegation chain, because it is the one a "
                        "human talks to. That human is still 'demo-user' rather than an "
                        "authenticated identity; see SECURITY_NOTES.md."),
    },
    "Procurement Agent (A2A)": {
        "client_id": "procurement-agent-principal",
        "delegation_actor": "procurement-agent",
        "may_mint": ["delegated_access_token"],
        "description": ("Procurement Agent (A2A) on Agent Runtime. It may extend a "
                        "delegation it was handed and nothing else: no user_access_token "
                        "rule exists for it, so it cannot manufacture the human grant it "
                        "is supposed to receive over A2A."),
    },
    "Procurement Agent": {
        "client_id": "procurement-agent-adk-principal",
        "delegation_actor": "procurement-agent",
        "may_mint": [],
        "description": ("The non-A2A Procurement Agent deployment. Same code base, no "
                        "minting rights at all: it cannot originate a chain and has no "
                        "inbound A2A delegation to extend, so it reads purchase orders "
                        "and refuses to draft. Listed with an empty may_mint rather than "
                        "omitted, so the denial is a stated policy and not an accident "
                        "of configuration."),
    },
}


def registered_agents(project: str, location: str) -> dict[str, dict]:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = google.auth.transport.requests.AuthorizedSession(credentials)
    url = (f"https://agentregistry.googleapis.com/v1/projects/{project}"
           f"/locations/{location}/agents")
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return {a.get("displayName"): a for a in response.json().get("agents", [])}


def principal_of(agent: dict) -> str | None:
    identity = (agent.get("attributes") or {}).get(RUNTIME_IDENTITY) or {}
    return identity.get("principal")


def allowed_principals(principal: str) -> list[str]:
    """Every spelling the broker may see for one identity.

    The broker matches an ID token's `email`, `sub` or `azp` against this list,
    and which claim carries the identity -- and in which of these forms --
    depends on the credential type. Listing all four costs nothing and avoids a
    deployment that authenticates but is not recognised.
    """
    bare = principal.split("://", 1)[-1]
    engine_id = bare.rsplit("/", 1)[-1]
    return sorted({engine_id, bare, f"principal://{bare}", f"spiffe://{bare}"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--write-clients", action="store_true",
                        help="regenerate config/broker_clients.json from the registry")
    parser.add_argument("--print-principal", metavar="DISPLAY_NAME",
                        help="print just this agent's bare principal:// URI and exit")
    args = parser.parse_args()
    if not args.project:
        print("Set GOOGLE_CLOUD_PROJECT or pass --project", file=sys.stderr)
        return 2

    found = registered_agents(args.project, args.location)

    if args.print_principal:
        agent = found.get(args.print_principal)
        principal = principal_of(agent) if agent else None
        if not principal:
            print(f"not registered: {args.print_principal}", file=sys.stderr)
            return 1
        print(principal)
        return 0

    clients: dict[str, dict] = {
        "identity-broker": {
            "description": ("The local control plane's delegation service. It acts for "
                            "several agents rather than being one, so its actor is read "
                            "from the agent credential it presents, not from this entry."),
            "client_secret": "identity-broker-demo-secret",
            "principal": "spiffe://demo.local/services/identity-broker",
            "may_present_actor_token": True,
            "may_mint": ["user_access_token", "agent_credential", "delegated_access_token"],
        }
    }

    missing = []
    for display_name, spec in AGENT_CLIENTS.items():
        agent = found.get(display_name)
        principal = principal_of(agent) if agent else None
        if not principal:
            missing.append(display_name)
            print(f"  {display_name:<26} NOT REGISTERED")
            continue
        print(f"  {display_name:<26} {principal}")
        clients[spec["client_id"]] = {
            "description": spec["description"],
            "allowed_principals": allowed_principals(principal),
            "delegation_actor": spec["delegation_actor"],
            "may_mint": spec["may_mint"],
            "basic_auth_enabled": False,
        }

    if args.write_clients:
        if missing:
            print(f"\nRefusing to write: {missing} not in the registry. Deploy the "
                  f"agents first; an entry written without them would silently "
                  f"refuse those deployments.", file=sys.stderr)
            return 1
        target = ROOT / "config" / "broker_clients.json"
        target.write_text(json.dumps(clients, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {target.relative_to(ROOT)} from the registry")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
