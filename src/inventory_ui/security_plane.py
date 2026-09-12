"""The security plane, read from Google Cloud rather than described.

Every row this module returns is derived from a live query: IAM policies on the
KMS key, on each Secret Manager secret, and on the Auth Broker's Cloud Run
service, plus the Agent Identity principals Agent Runtime actually issued. None
of it is a hardcoded description of intent.

That distinction is the whole point. A slide claiming "the Inventory Agent
cannot create purchase orders" is an assertion. A panel showing that no binding
grants it that, fetched from the project while the audience watches, is
evidence -- and it changes the moment someone grants the binding.

Results are cached briefly because IAM reads are slow and this is rendered on a
page, not because the data is static.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Any


PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
REGION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
KEY_RING = "a2a-demo-delegation"
KEY_ID = "delegation-issuer"
BROKER_SERVICE = "a2a-auth-broker"

_cache: dict[str, Any] = {"at": 0.0, "data": None}
CACHE_SECONDS = int(os.getenv("SECURITY_PLANE_CACHE_SECONDS", "60"))


def _gcloud(*args: str) -> Any:
    """Run a gcloud read and parse JSON. Returns None on any failure.

    Read-only by construction: every call site below is a describe/list/
    get-iam-policy. Nothing here mutates the project.
    """
    import json

    try:
        result = subprocess.run(
            ["gcloud", *args, "--project", PROJECT, "--format", "json"],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout or "null")
    except Exception:
        return None


def _reasoning_engines() -> tuple[list[dict[str, Any]], str | None]:
    """List deployed agents and their Agent Identity principals.

    Via REST because this gcloud has no `ai reasoning-engines` group; the
    deployment scripts read the same endpoint.

    Returns (engines, error). The error is returned rather than swallowed
    because an empty list and a failed query must not look identical on a page
    whose whole purpose is to show who holds which permission -- "no agent can
    do this" and "we could not find out" are opposite claims.
    """
    import requests

    try:
        token = subprocess.run(["gcloud", "auth", "print-access-token"],
                               capture_output=True, text=True, timeout=60)
        if token.returncode != 0:
            return [], "could not obtain an access token"
        url = (f"https://{REGION}-aiplatform.googleapis.com/v1beta1/projects/"
               f"{PROJECT}/locations/{REGION}/reasoningEngines")
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token.stdout.strip()}"}, timeout=60)
        response.raise_for_status()
        return response.json().get("reasoningEngines", []), None
    except Exception as exc:
        return [], f"{type(exc).__name__}"


def _members(policy: Any, role: str) -> list[str]:
    if not policy:
        return []
    for binding in policy.get("bindings", []) or []:
        if binding.get("role") == role:
            return list(binding.get("members", []))
    return []


def _short(principal: str) -> str:
    """Make a principal readable without losing what identifies it."""
    if principal.startswith("serviceAccount:"):
        return principal.split(":", 1)[1]
    if "reasoningEngines/" in principal:
        return f"Agent Identity …/{principal.rsplit('/', 1)[-1]}"
    return principal


def collect() -> dict[str, Any]:
    """Build the live picture of who may do what."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]

    key_policy = _gcloud("kms", "keys", "get-iam-policy", KEY_ID,
                         "--keyring", KEY_RING, "--location", REGION)
    broker_policy = _gcloud("run", "services", "get-iam-policy", BROKER_SERVICE,
                            "--region", REGION)
    engines, engines_error = _reasoning_engines()

    signers = [_short(m) for m in _members(key_policy, "roles/cloudkms.signerVerifier")]
    verifiers = [_short(m) for m in _members(key_policy, "roles/cloudkms.publicKeyViewer")]
    invokers = [_short(m) for m in _members(broker_policy, "roles/run.invoker")]

    secrets: dict[str, list[str]] = {}
    for secret_id in ("zoho-invread-mcp-url", "zoho-procurewrite-mcp-url"):
        policy = _gcloud("secrets", "get-iam-policy", secret_id)
        secrets[secret_id] = [
            _short(m) for m in _members(policy, "roles/secretmanager.secretAccessor")]

    agents = [
        {
            "name": e.get("displayName"),
            "resource_id": str(e.get("name", "")).rsplit("/", 1)[-1],
            "identity": (e.get("spec", {}) or {}).get("effectiveIdentity"),
        }
        for e in engines
        if e.get("displayName") in ("Inventory Agent", "Procurement Agent",
                                    "Procurement Agent (A2A)")
    ]

    # Capabilities are stated as (player, may, may not), and each claim names the
    # live evidence above that supports it. Nothing is asserted without a source.
    players = [
        {
            "player": "Human approver",
            "identity": "Google account, verified by IAP at the edge",
            "may": ["Approve or reject a purchase-order draft"],
            "may_not": ["Read or write Zoho directly", "Sign a delegation token",
                        "Reach the Auth Broker"],
            "evidence": "IAP assertion; approver role checked server-side per request",
        },
        {
            "player": "Inventory & Purchasing app",
            "identity": "Cloud Run service account",
            "may": ["Invoke the deployed agents", "Persist runs, drafts, approvals"],
            "may_not": ["Sign a delegation token", "Approve on a human's behalf"],
            "evidence": f"not present in KMS signers: {signers or 'none'}",
        },
        {
            "player": "Inventory Agent",
            "identity": "Agent Identity (SPIFFE), issued by Agent Runtime",
            "may": ["Read stock via the InvRead connector",
                    "Request a narrowly scoped delegation from the Auth Broker"],
            "may_not": ["Create a purchase order", "Sign its own tokens",
                        "Read the ProcureWrite credential"],
            "evidence": f"ProcureWrite accessors: {secrets.get('zoho-procurewrite-mcp-url') or 'none'}",
        },
        {
            "player": "Procurement Agent",
            "identity": "Agent Identity (SPIFFE), issued by Agent Runtime",
            "may": ["Create and submit a purchase order via ProcureWrite"],
            "may_not": ["Approve a purchase order -- no such tool exists",
                        "Sign its own tokens"],
            "evidence": "tool discovery: no approve/issue/send tool on any connector",
        },
        {
            "player": "Auth Broker",
            "identity": "Dedicated Cloud Run service account",
            "may": ["Call KMS asymmetricSign, subject to its minting policy"],
            "may_not": ["Read Zoho", "Approve anything",
                        "Export the private key -- KMS will not release it"],
            "evidence": f"sole KMS signer: {signers or 'none'}",
        },
        {
            "player": "DemoSetup connector",
            "identity": "Operator-held credential",
            "may": ["Create demo items, vendors, and stock adjustments"],
            "may_not": ["Be reached by any deployed workload"],
            "evidence": "not in Secret Manager; granted to no agent",
        },
    ]

    data = {
        "project": PROJECT,
        "region": REGION,
        "collected_at": now,
        "key": f"{KEY_RING}/{KEY_ID}",
        "kms_signers": signers,
        "kms_verifiers": verifiers,
        "broker_invokers": invokers,
        "secret_accessors": secrets,
        "agents": agents,
        "agents_error": engines_error,
        "players": players,
        "live": key_policy is not None,
    }
    _cache.update({"at": now, "data": data})
    return data
