#!/usr/bin/env python3
"""The outbound destinations the deployed agents actually use.

Enforcement turns an unregistered destination into a 403. Hostname matching is
exact and there are no wildcards, so "register the destinations" is not a
formality -- a single omission silently breaks whichever governance path
depends on it, and it breaks it only once enforcement is switched on, long
after the change that caused it.

This file is therefore the inventory, derived from the code rather than
remembered: each entry names the destination, which agent needs it, and the
call site that proves it. Nothing here is guesswork, and anything that cannot
be traced to a call site does not belong in the list.

  python3 cloud/gateway_destinations.py --list       # print the inventory
  python3 cloud/gateway_destinations.py --register   # create registry entries
  python3 cloud/gateway_destinations.py --grant      # roles/iap.egressor

--list writes nothing and is the right thing to run first. The inventory is a
starting point for observation, never a substitute for it: run the demo in
DRY_RUN and reconcile this list against what the gateway actually logged
(`verify_agent_gateway.py --observed`) before trusting it.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

INVENTORY = "Inventory Agent"
PROCUREMENT = "Procurement Agent"
A2A = "Procurement Agent (A2A)"

# host template -> (why it is needed, which agents, where the call is made)
# {region} and {project} are substituted at run time.
DESTINATIONS: list[dict] = [
    {
        "key": "vertex-ai",
        "host": "{region}-aiplatform.googleapis.com",
        "why": "Gemini model calls, and the A2A hop between the two agents",
        "agents": [INVENTORY, PROCUREMENT, A2A],
        "evidence": "cloud/inventory_agent/agent.py "
                    "request_reorder_from_procurement_agent()",
    },
    {
        "key": "vertex-ai-mtls",
        "host": "{region}-aiplatform.mtls.googleapis.com",
        "why": "the same A2A hop once the certificate binding is in effect; "
               "Agent Identity tokens are certificate-bound, so this is the "
               "host the call actually lands on",
        "agents": [INVENTORY],
        "evidence": "cloud/inventory_agent/agent.py, configure_mtls_channel()",
    },
    {
        "key": "secretmanager",
        "host": "secretmanager.googleapis.com",
        "why": "reading the Zoho connector URL under the agent's own identity",
        "agents": [INVENTORY, PROCUREMENT, A2A],
        "evidence": "cloud/*/zoho_mcp.py _resolve()",
    },
    {
        "key": "cloudkms",
        "host": "cloudkms.googleapis.com",
        "why": "fetching the delegation public key to verify a token; agents "
               "verify, they never sign",
        "agents": [INVENTORY, PROCUREMENT, A2A],
        "evidence": "cloud/*/governance.py decode_token()",
    },
    {
        "key": "auth-broker",
        "host": None,  # resolved from AUTH_BROKER_URL at run time
        "env": "AUTH_BROKER_URL",
        "why": "minting a delegated token; the broker is the only signer",
        "agents": [INVENTORY, PROCUREMENT, A2A],
        "evidence": "cloud/*/governance.py exchange_token()",
    },
    {
        "key": "zoho-invread",
        "host": None,
        "env": "ZOHO_INVREAD_MCP_URL",
        "why": "read-only Zoho inventory tools",
        "agents": [INVENTORY, PROCUREMENT, A2A],
        "evidence": "cloud/*/zoho_mcp.py call(INVREAD, ...)",
    },
    {
        "key": "zoho-procurewrite",
        "host": None,
        "env": "ZOHO_PROCUREWRITE_MCP_URL",
        "why": "creating and submitting a draft purchase order. Inventory "
               "Agent is deliberately NOT on this list: it must be refused "
               "this path, which is the deny case worth proving",
        "agents": [PROCUREMENT, A2A],
        "evidence": "cloud/procurement_agent/zoho_mcp.py create_purchase_order()",
    },
    {
        "key": "cloudtrace",
        "host": "cloudtrace.googleapis.com",
        "why": "span export; losing this silently breaks the trace evidence "
               "the demo relies on, without breaking the demo itself",
        "agents": [INVENTORY, PROCUREMENT, A2A],
        "evidence": "--otel_to_cloud at deploy time",
    },
]


def host_for(entry: dict, project: str, region: str) -> str | None:
    if entry.get("host"):
        return entry["host"].format(project=project, region=region)
    url = os.getenv(entry.get("env", ""), "").strip()
    if not url:
        return None
    # Host only. The Zoho URLs embed an API key in the path, which must never
    # be printed or passed to a registry entry.
    without_scheme = url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0]


def run(command: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        return 2, "gcloud is not on PATH"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--region", default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="print the inventory (default)")
    action.add_argument("--register", action="store_true",
                        help="create an Agent Registry service per destination")
    action.add_argument("--grant", action="store_true",
                        help="grant roles/iap.egressor per agent, per destination")
    args = parser.parse_args()

    if not args.project:
        print("Set GOOGLE_CLOUD_PROJECT or pass --project", file=sys.stderr)
        return 2

    rows = []
    missing = []
    for entry in DESTINATIONS:
        host = host_for(entry, args.project, args.region)
        if host is None:
            missing.append(entry)
            continue
        rows.append((entry, host))

    if args.list or not (args.register or args.grant):
        print(f"Outbound destinations for {args.project}/{args.region}\n")
        for entry, host in rows:
            who = ", ".join(a.replace(" Agent", "") for a in entry["agents"])
            print(f"  {host}")
            print(f"      needed by : {who}")
            print(f"      why       : {entry['why']}")
            print(f"      evidence  : {entry['evidence']}")
            print()
        if missing:
            print("Not resolvable from this environment (source activate.sh, and")
            print("source .env_zoho_urls, so the connector hosts can be read):")
            for entry in missing:
                print(f"  {entry['key']:<20} needs ${entry['env']}")
            print()
        print("This inventory is derived from call sites. Confirm it against what")
        print("the gateway actually logged before enforcing:")
        print("  python3 cloud/verify_agent_gateway.py --observed")
        return 1 if missing else 0

    if missing:
        print("Refusing to proceed: these destinations cannot be resolved, and a",
              file=sys.stderr)
        print("partial registration is what produces a 403 under enforcement.",
              file=sys.stderr)
        for entry in missing:
            print(f"  {entry['key']} needs ${entry['env']}", file=sys.stderr)
        return 2

    if args.register:
        print("Registering destinations in the Agent Registry\n")
        failed = 0
        for entry, host in rows:
            name = f"a2a-demo-{entry['key']}"
            code, output = run([
                "gcloud", "agent-registry", "services", "create", name,
                "--project", args.project, "--location", args.region,
                "--display-name", f"A2A demo: {entry['key']}",
                "--endpoint-spec-type=no-spec",
                f"--interfaces=url=https://{host},protocolBinding=jsonrpc",
            ])
            if code == 0:
                print(f"  registered {name:<28} {host}")
            elif "ALREADY_EXISTS" in output or "already exists" in output:
                print(f"  exists     {name:<28} {host}")
            else:
                print(f"  FAILED     {name:<28} {output.strip()[:200]}", file=sys.stderr)
                failed += 1
        return 1 if failed else 0

    if args.grant:
        # Deliberately not implemented as a blanket grant. The desired policy
        # is per agent, per destination -- Inventory Agent must be refused the
        # write connector, and a project-wide principalSet would hand it over.
        print("Per-agent, per-destination grants are not scripted yet, on purpose.\n")
        print("A project-wide principalSet:// grant is the easy version and it")
        print("destroys the property worth demonstrating: Inventory Agent would")
        print("gain the write connector it is supposed to be refused.\n")
        print("Grant one at a time, against the registry endpoint id, e.g.:\n")
        print("  gcloud iap web add-iam-policy-binding \\")
        print("    --resource-type=agent-registry --endpoint=ENDPOINT_ID \\")
        print(f"    --region={args.region} --project={args.project} \\")
        print("    --member=principal://<the agent's effectiveIdentity> \\")
        print("    --role=roles/iap.egressor\n")
        print("Endpoint ids come from:")
        print(f"  gcloud agent-registry endpoints list --location={args.region}")
        print("Agent principals come from:")
        print("  python3 cloud/registry_principals.py --show")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
