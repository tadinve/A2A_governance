#!/usr/bin/env python3
"""Check the live Agent Gateway configuration, not the source that produced it.

Every check reads a cloud resource. A passing run means Google Cloud is in the
expected state; it is not a statement about what the scripts in this repository
intend. That distinction is the whole reason this file exists separately from
tests/test_agent_gateway_deployment.py, which only reads source.

  python3 cloud/verify_agent_gateway.py --pre-bind    # gateway only
  python3 cloud/verify_agent_gateway.py --post-bind   # gateway + bindings
  python3 cloud/verify_agent_gateway.py --observed    # what egress was logged
  python3 cloud/verify_agent_gateway.py               # same as --post-bind

--pre-bind is the honest mode to run straight after setup_agent_gateway.sh:
checking "is the agent bound" before anything was meant to bind it would report
failures that are the correct state.

Exit status is non-zero if any check fails, so this is usable in a pipeline.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import google.auth
import google.auth.transport.requests

AGENTS = ["Inventory Agent", "Procurement Agent", "Procurement Agent (A2A)"]


def session():
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return google.auth.transport.requests.AuthorizedSession(credentials)


def engines(s, project: str, region: str) -> dict[str, dict]:
    """display name -> reasoningEngine resource body."""
    url = (f"https://{region}-aiplatform.googleapis.com/v1beta1/projects/"
           f"{project}/locations/{region}/reasoningEngines")
    response = s.get(url, timeout=60)
    response.raise_for_status()
    return {e.get("displayName"): e for e in response.json().get("reasoningEngines", [])}


def gateway(s, project: str, region: str, name: str) -> dict | None:
    url = (f"https://networkservices.googleapis.com/v1/projects/{project}"
           f"/locations/{region}/agentGateways/{name}")
    response = s.get(url, timeout=60)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def bound_gateway(engine: dict) -> str:
    """The gateway this engine currently routes egress through, or ''."""
    spec = engine.get("spec") or {}
    config = (spec.get("deploymentSpec") or {}).get("agentGatewayConfig") or {}
    return (config.get("agentToAnywhereConfig") or {}).get("agentGateway") or ""


def same_gateway(left: str, right: str) -> bool:
    """Compare two gateway URIs, tolerating project id vs project number.

    Google accepts a resource written with the project *id* and echoes it back
    with the project *number*. That is already visible in the authorization
    policy this repository creates: it is submitted targeting
    projects/<id>/.../agentGateways/<name> and reads back as
    projects/<number>/.../agentGateways/<name>.

    A naive string comparison would therefore report a correctly bound agent
    as unbound -- the worst kind of wrong answer here, because the obvious
    response to it is to bind again.
    """
    def tail(uri: str) -> str:
        return uri.split("/locations/", 1)[-1] if "/locations/" in uri else uri

    return bool(left) and bool(right) and tail(left) == tail(right)


def observed(project: str, region: str) -> int:
    """Print what the gateway actually saw. Requires traffic to have flowed.

    This is the step that turns a guessed destination list into a measured one:
    in DRY_RUN every request is logged with the authorization decision that
    *would* have been made, so a destination missing from the registry shows up
    here as a DENIED line while still succeeding.
    """
    query = (f'resource.type="networkservices.googleapis.com/Gateway" '
             f'AND resource.labels.location="{region}"')
    command = ["gcloud", "logging", "read", query, "--project", project,
               "--limit", "200", "--format", "json"]
    try:
        raw = subprocess.run(command, capture_output=True, text=True,
                             timeout=180, check=True).stdout
    except FileNotFoundError:
        print("gcloud is not on PATH", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"could not read gateway logs: {exc.stderr[:300]}", file=sys.stderr)
        return 2

    try:
        entries = json.loads(raw or "[]")
    except json.JSONDecodeError:
        entries = []
    if not entries:
        print("No gateway log entries yet.\n"
              "Bind the agents, then drive some traffic (cloud/verify_cloud.py,\n"
              "or a Check Inventory in the UI) and run this again.")
        return 0

    seen: dict[tuple[str, str], int] = {}
    for entry in entries:
        request = entry.get("httpRequest") or {}
        payload = entry.get("jsonPayload") or {}
        url = request.get("requestUrl") or ""
        host = url.split("/")[2] if "://" in url else url
        decision = ((payload.get("authzPolicyInfo") or {}).get("result")
                    or str(request.get("status") or "?"))
        key = (host, decision)
        seen[key] = seen.get(key, 0) + 1

    print(f"{len(entries)} gateway log entries, by destination and decision:\n")
    for (host, decision), count in sorted(seen.items(), key=lambda kv: -kv[1]):
        flag = "  <-- would be blocked under enforcement" if decision == "DENIED" else ""
        print(f"  {count:>4}  {decision:<10} {host}{flag}")
    print("\nEvery destination listed here must be registered in the Agent Registry\n"
          "and granted to the calling agent before enforcement is switched on.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--region", default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--gateway", default="a2a-governance-egress")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pre-bind", action="store_true",
                      help="gateway checks only; do not expect agents to be bound")
    mode.add_argument("--post-bind", action="store_true",
                      help="gateway checks plus per-agent binding (the default)")
    mode.add_argument("--observed", action="store_true",
                      help="summarise what the gateway logged, by destination")
    args = parser.parse_args()

    if not args.project:
        print("Set GOOGLE_CLOUD_PROJECT or pass --project", file=sys.stderr)
        return 2
    if args.observed:
        return observed(args.project, args.region)

    check_bindings = not args.pre_bind
    expected_uri = (f"projects/{args.project}/locations/{args.region}"
                    f"/agentGateways/{args.gateway}")

    results: list[bool] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        results.append(bool(passed))
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        if not passed and detail:
            print(f"        {detail[:300]}")

    s = session()

    body = gateway(s, args.project, args.region, args.gateway)
    check("gateway exists", body is not None,
          f"no agentGateways/{args.gateway} in {args.project}/{args.region}")

    managed = ((body or {}).get("googleManaged") or {})
    check("gateway is Agent-to-Anywhere",
          managed.get("governedAccessPath") == "AGENT_TO_ANYWHERE",
          f"governedAccessPath={managed.get('governedAccessPath')!r}")

    registries = (body or {}).get("registries") or []
    wanted_registry = (f"//agentregistry.googleapis.com/projects/{args.project}"
                       f"/locations/{args.region}")
    check("regional Agent Registry attached",
          any(wanted_registry in r for r in registries),
          f"registries={registries}")

    deployed = engines(s, args.project, args.region)
    for name in AGENTS:
        engine = deployed.get(name)
        check(f"{name}: deployed", engine is not None,
              f"not found in {args.project}/{args.region}")
        if engine is None:
            if check_bindings:
                check(f"{name}: bound to gateway", False, "agent is not deployed")
            continue

        identity = (engine.get("spec") or {}).get("effectiveIdentity") or ""
        # A service-account fallback is the failure this check exists for: the
        # deployment still works, but IAP has no agent principal to authorize,
        # so the gateway cannot be the control it is claimed to be.
        check(f"{name}: has Agent Identity", bool(identity),
              "no effectiveIdentity; this is a service-account fallback")

        if check_bindings:
            current = bound_gateway(engine)
            check(f"{name}: bound to gateway", same_gateway(current, expected_uri),
                  f"bound to {current!r}, expected {expected_uri!r}"
                  if current else "not bound to any gateway")

    passed = sum(1 for ok in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    if args.pre_bind and passed == len(results):
        print("\nGateway is provisioned and nothing is bound to it yet, which is\n"
              "the expected state after setup_agent_gateway.sh.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
