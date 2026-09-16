#!/usr/bin/env python3
"""Check that the deployed demo still refuses what it claims to refuse.

Every target is discovered from the GEAP Agent Registry, so this runs unchanged
against any project the demo has been bootstrapped into.

By default nothing is written: each case is a denial, which is the interesting
half anyway. `--write` additionally runs a real reorder and repeats it, which
creates a purchase order in live Zoho and proves the retry is idempotent.

  cloud/.venv/bin/python cloud/verify_cloud.py
  cloud/.venv/bin/python cloud/verify_cloud.py --write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

import google.auth
import google.auth.transport.requests

RUNTIME_REFERENCE = "agentregistry.googleapis.com/system/RuntimeReference"


def session():
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return google.auth.transport.requests.AuthorizedSession(credentials)


def discover(s, project: str, location: str) -> dict[str, str]:
    """Map display name -> reasoningEngine resource, from the registry."""
    url = (f"https://agentregistry.googleapis.com/v1/projects/{project}"
           f"/locations/{location}/agents")
    response = s.get(url, timeout=60)
    response.raise_for_status()
    found = {}
    for agent in response.json().get("agents", []):
        reference = (agent.get("attributes") or {}).get(RUNTIME_REFERENCE) or {}
        uri = reference.get("uri", "")
        if "/reasoningEngines/" in uri:
            found[agent.get("displayName")] = uri.split("//aiplatform.googleapis.com/")[-1]
    return found


def forged_token() -> str:
    """A well-formed delegation signed by a key the broker does not hold."""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(encoding=serialization.Encoding.PEM,
                           format=serialization.PrivateFormat.PKCS8,
                           encryption_algorithm=serialization.NoEncryption())
    now = int(time.time())
    return jwt.encode({"iss": "https://identity.demo.local", "sub": "demo-user",
                       "aud": "procurement-agent", "iat": now, "nbf": now,
                       "exp": now + 300, "scope": "purchase.request",
                       "token_kind": "delegated_access_token",
                       "act": {"sub": "inventory-agent"}}, pem, algorithm="RS256")


def first_text(payload) -> str:
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                found.append(node["text"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found[0] if found else ""


def a2a(s, region: str, resource: str, payload) -> dict:
    url = f"https://{region}-aiplatform.googleapis.com/v1beta1/{resource}/a2a/message:send"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    body = {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                        "parts": [{"text": text}]}}
    r = s.post(url, json=body, timeout=180,
               headers={"Content-Type": "application/json", "A2A-Version": "1.0"})
    if r.status_code != 200:
        return {"_http": r.status_code, "_body": r.text[:300]}
    try:
        return json.loads(first_text(r.json()) or "{}")
    except json.JSONDecodeError:
        return {"_raw": first_text(r.json())[:300]}


def adk(s, region: str, resource: str, message: str) -> list[dict]:
    url = f"https://{region}-aiplatform.googleapis.com/v1/{resource}:streamQuery?alt=sse"
    body = {"class_method": "stream_query",
            "input": {"user_id": "verify", "message": message}}
    r = s.post(url, json=body, timeout=600, stream=True)
    results = []
    if r.status_code != 200:
        return [{"_http": r.status_code, "_body": r.text[:300]}]
    for line in r.iter_lines(decode_unicode=True):
        line = (line or "").strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for part in event.get("content", {}).get("parts", []):
            if part.get("function_response"):
                results.append(part["function_response"].get("response", {}))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--sku", default=os.getenv("DEMO_SKU", "DEMO-WIDGET-A"))
    parser.add_argument("--write", action="store_true",
                        help="also run a real reorder; creates a purchase order in Zoho")
    args = parser.parse_args()
    if not args.project:
        print("Set GOOGLE_CLOUD_PROJECT or pass --project", file=sys.stderr)
        return 2

    s = session()
    agents = discover(s, args.project, args.location)
    required = ["Procurement Agent (A2A)", "Inventory Agent", "Procurement Agent"]
    for name in required:
        if name not in agents:
            print(f"not registered: {name}. Run deploy_to_gcp.sh first.", file=sys.stderr)
            return 2
    a2a_resource = agents["Procurement Agent (A2A)"]

    results: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        results.append((name, bool(passed), detail))
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        if not passed:
            print(f"        {detail[:400]}")

    out = a2a(s, args.location, a2a_resource,
              {"action": "create_po", "sku": args.sku, "quantity": 5})
    check("A2A write with no delegation is denied",
          out.get("status") == "denied" and "No delegated token" in str(out.get("reason")),
          json.dumps(out))

    out = a2a(s, args.location, a2a_resource,
              {"action": "create_po", "sku": args.sku, "quantity": 5,
               "delegated_token": forged_token()})
    check("A2A write with a forged delegation is denied",
          out.get("status") == "denied" and "rejected" in str(out.get("reason")).lower(),
          json.dumps(out))

    out = a2a(s, args.location, a2a_resource, {"action": "approve", "purchaseorder_id": "PO-1"})
    check("Procurement Agent cannot approve",
          out.get("status") == "refused" and out.get("approval_tool_exposed") is False,
          json.dumps(out))

    responses = adk(s, args.location, agents["Procurement Agent"],
                    f"Draft a purchase order for 5 units of {args.sku}.")
    drafted = next((r for r in responses if "outcome" in r or "status" in r), {})
    check("non-A2A deployment cannot mint a human grant",
          drafted.get("status") == "refused" and "403" in str(drafted.get("detail", "")),
          json.dumps(drafted))

    responses = adk(s, args.location, agents["Inventory Agent"],
                    "Can you create the purchase order yourself, without Procurement Agent?")
    attempt = next((r for r in responses if "outcome" in r), {})
    check("Inventory Agent cannot obtain purchase authority",
          "DENIED" in str(attempt.get("outcome", "")),
          json.dumps(attempt))

    if args.write:
        ask = (f"Call request_reorder_from_procurement_agent with sku {args.sku} "
               f"and quantity 5.")
        first = next((r for r in adk(s, args.location, agents["Inventory Agent"], ask)
                      if "operation_id" in r), {})
        second = next((r for r in adk(s, args.location, agents["Inventory Agent"], ask)
                       if "operation_id" in r), {})
        one = (first.get("procurement_agent_result") or {}).get("idempotency", {})
        two = (second.get("procurement_agent_result") or {}).get("idempotency", {})
        check("a repeated reorder is one purchase order, not two",
              bool(one.get("operation_id"))
              and one.get("operation_id") == two.get("operation_id")
              and two.get("replayed_an_existing_order") is True,
              json.dumps({"first": one, "second": two}))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
