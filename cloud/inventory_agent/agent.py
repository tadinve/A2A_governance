"""Inventory Agent deployed to Agent Runtime with its own Agent Identity.

It monitors stock and delegates reordering to Procurement Agent. It has no
authority to create a purchase order itself, and the delegation policy proves it.
"""
from __future__ import annotations

import json
import os
import uuid

import google.auth
import google.auth.transport.requests
from google.adk.agents import Agent

from . import governance

AGENT_ID = "inventory-agent"
SPIFFE_ID = "spiffe://demo.local/agents/inventory-agent"

# Set at deploy time to the Procurement Agent's reasoningEngine resource name.
PROCUREMENT_A2A = os.getenv("PROCUREMENT_A2A", "")


def show_my_identity() -> dict:
    """Report this agent's workload identity and how it becomes authorized."""
    return {
        "status": "success",
        "agent_id": AGENT_ID,
        "demo_identity": SPIFFE_ID,
        "cloud_identity": (
            "Provisioned by Agent Runtime because .agent_engine_config.json set "
            "identity_type=AGENT_IDENTITY. It is a principal:// URI, not a service account."
        ),
        "authentication": "Agent Identity credentials supplied by Agent Runtime ADC",
        "authorization": "Google Cloud IAM bindings on that principal",
        "distinct_from": (
            "Procurement Agent has a different principal. Two agents, two identities, "
            "two separately grantable sets of permissions."
        ),
    }


def check_stock(sku: str) -> dict:
    """Check stock on hand for a SKU and say whether it needs reordering."""
    sku = sku.upper()
    item = governance.ITEMS.get(sku)
    if not item:
        return {"status": "error", "error_message": f"Unknown SKU {sku}"}
    try:
        delegated = governance.exchange_token(
            AGENT_ID, governance.human_token(), "zoho-inventory-mcp", "inventory.read")
    except governance.PolicyDenied as denied:
        return {"status": "error", "error_message": str(denied)}
    low = item["stock_on_hand"] < item["reorder_level"]
    return {
        "status": "success",
        "item": item,
        "reorder_needed": low,
        "suggested_quantity": item["target_stock"] - item["stock_on_hand"] if low else 0,
        "delegation_evidence": delegated["claims"],
        "note": "Read through a delegated inventory.read token. This agent cannot write.",
    }


def attempt_to_create_purchase_order_directly() -> dict:
    """Try to obtain purchase-order authority directly. This is expected to fail."""
    try:
        governance.exchange_token(
            AGENT_ID, governance.human_token(), "zoho-procurement-mcp", "purchaseorder.create")
    except governance.PolicyDenied as denied:
        return {
            "status": "success",
            "outcome": "DENIED, as designed",
            "detail": str(denied),
            "lesson": (
                "Inventory Agent is authenticated and holds a valid human delegation, "
                "and is still refused. Authentication is not authorization. Creating a "
                "purchase order requires going through Procurement Agent over A2A."
            ),
        }
    return {"status": "error",
            "error_message": "Policy unexpectedly allowed this. The demo is misconfigured."}


def diagnose_outbound_auth() -> dict:
    """Probe what this runtime's credential can actually do on outbound calls.

    Never returns a token, only its shape and the status codes it produces.
    """
    report: dict = {"status": "success", "procurement_a2a": PROCUREMENT_A2A or "(unset)"}
    try:
        credentials, project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        report["credential_class"] = type(credentials).__name__
        report["adc_project"] = project
        report["service_account_email"] = getattr(credentials, "service_account_email", "(n/a)")
        request = google.auth.transport.requests.Request()
        credentials.refresh(request)
        token = getattr(credentials, "token", None)
        report["token"] = f"present, {len(token)} chars" if token else "NONE"
        report["token_prefix_kind"] = (
            "ya29 (standard oauth2)" if token and token.startswith("ya29.")
            else "jwt-like (3 dot segments)" if token and token.count(".") == 2
            else "other"
        )
        session = google.auth.transport.requests.AuthorizedSession(credentials)
    except Exception as exc:  # noqa: BLE001
        report["status"] = "error"
        report["auth_failure"] = f"{type(exc).__name__}: {exc}"
        return report

    region = (PROCUREMENT_A2A.split("/locations/")[1].split("/")[0]
              if PROCUREMENT_A2A else "us-central1")
    host = f"https://{region}-aiplatform.googleapis.com/v1beta1"
    probes = {
        "GET own resource": ("get", f"{host}/{PROCUREMENT_A2A}", None),
        "GET a2a card": ("get", f"{host}/{PROCUREMENT_A2A}/a2a/v1/card", None),
        "POST a2a message:send": ("post", f"{host}/{PROCUREMENT_A2A}/a2a/message:send",
                                  {"message": {"messageId": "probe", "role": "ROLE_USER",
                                               "parts": [{"text": '{"action":"get_status",'
                                                                  '"purchaseorder_id":"PO-1"}'}]}}),
        "GET storage buckets": ("get",
                                f"https://storage.googleapis.com/storage/v1/b?project={project}", None),
    }
    results: dict = {}
    for label, (method, url, body) in probes.items():
        if PROCUREMENT_A2A == "" and "a2a" in label:
            results[label] = "skipped (PROCUREMENT_A2A unset)"
            continue
        try:
            if method == "get":
                response = session.get(url, timeout=60)
            else:
                response = session.post(url, json=body, timeout=60,
                                        headers={"A2A-Version": "1.0"})
            results[label] = f"HTTP {response.status_code}"
            if response.status_code >= 400:
                results[label] += f" :: {response.text[:160]}"
        except Exception as exc:  # noqa: BLE001
            results[label] = f"{type(exc).__name__}: {exc}"[:160]
    report["outbound_probes"] = results

    # The docs say Google Cloud *client libraries* perform the certificate
    # binding automatically. AuthorizedSession is a raw transport, not a client
    # library, so test a real one side by side.
    library: dict = {}
    try:
        from google.cloud import storage

        client = storage.Client(project=project)
        names = [b.name for b in client.list_buckets(max_results=3)]
        library["google-cloud-storage list_buckets"] = f"OK, {len(names)} buckets"
    except Exception as exc:  # noqa: BLE001
        library["google-cloud-storage list_buckets"] = f"{type(exc).__name__}: {exc}"[:200]
    try:
        import vertexai

        vclient = vertexai.Client(project=project, location=region)
        engine = vclient.agent_engines.get(name=PROCUREMENT_A2A)
        library["vertexai agent_engines.get"] = f"OK, {engine.api_resource.display_name}"
    except Exception as exc:  # noqa: BLE001
        library["vertexai agent_engines.get"] = f"{type(exc).__name__}: {exc}"[:200]
    report["client_library_probes"] = library

    cert_state: dict = {}
    try:
        from google.auth.transport import _mtls_helper

        cert_state["check_use_client_cert"] = _mtls_helper.check_use_client_cert()
        cert_state["cert_config_path"] = str(
            _mtls_helper._get_cert_config_path(include_context_aware=True))
        probe_session = google.auth.transport.requests.AuthorizedSession(credentials)
        probe_session.configure_mtls_channel()
        cert_state["session_is_mtls"] = bool(getattr(probe_session, "is_mtls", False))
        if cert_state["session_is_mtls"]:
            mtls_url = f"https://{region}-aiplatform.mtls.googleapis.com/v1beta1/{PROCUREMENT_A2A}"
            r = probe_session.get(mtls_url, timeout=60)
            cert_state["GET own resource over mTLS"] = f"HTTP {r.status_code} :: {r.text[:150]}"
    except Exception as exc:  # noqa: BLE001
        cert_state["error"] = f"{type(exc).__name__}: {exc}"[:200]
    report["mtls_state"] = cert_state
    return report


def request_reorder_from_procurement_agent(sku: str, quantity: int) -> dict:
    """Ask Procurement Agent to draft a purchase order, over the A2A protocol.

    Mints a delegated token carrying the demo's human-subject claim and this
    agent's actor claim, then sends it in an A2A message. Procurement Agent
    verifies that token against the shared demo issuer key before doing any work.

    That JWT is this demo's delegation model, separate from the Google Cloud
    Agent Identity that authenticates the transport.
    """
    if not PROCUREMENT_A2A:
        return {"status": "error",
                "error_message": "PROCUREMENT_A2A is not set; redeploy with the A2A "
                                 "Procurement Agent resource name."}
    try:
        delegated = governance.exchange_token(
            AGENT_ID, governance.human_token(), "procurement-agent", "purchase.request")
    except governance.PolicyDenied as denied:
        return {"status": "error", "outcome": "DENIED by delegation policy",
                "detail": str(denied)}

    payload = json.dumps({
        "action": "create_po", "sku": sku.upper(), "quantity": quantity,
        "delegated_token": delegated["access_token"],
    })
    region = PROCUREMENT_A2A.split("/locations/")[1].split("/")[0]
    body = {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                        "parts": [{"text": payload}]}}
    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = google.auth.transport.requests.AuthorizedSession(credentials)
        # Agent Identity tokens are certificate-bound. Without this the token is
        # presented as a plain bearer over ordinary TLS and is refused with 401,
        # which is what a replayed stolen token looks like.
        # configure_mtls_channel() returns None and records the result on the
        # session, so read is_mtls rather than the return value.
        mtls = False
        try:
            session.configure_mtls_channel()
            mtls = bool(getattr(session, "is_mtls", False))
        except Exception:  # noqa: BLE001 - fall through and report what happens
            mtls = False
        host = (f"{region}-aiplatform.mtls.googleapis.com" if mtls
                else f"{region}-aiplatform.googleapis.com")
        url = f"https://{host}/v1beta1/{PROCUREMENT_A2A}/a2a/message:send"
        response = session.post(url, json=body, timeout=180, headers={
            "Content-Type": "application/json", "A2A-Version": "1.0"})
    except Exception as exc:  # noqa: BLE001 - surface the real failure
        return {"status": "error", "error_message": f"{type(exc).__name__}: {exc}"}

    if response.status_code == 401:
        return {
            "status": "error", "outcome": "AUTHENTICATION FAILED", "http_status": 401,
            "mtls_configured": mtls, "endpoint": host,
            "detail": response.text[:400],
            "lesson": ("The credential was not accepted at all. Agent Identity tokens "
                       "are certificate-bound, so they must be presented over mTLS. "
                       "A 401 means the binding did not happen; it is not an IAM result."),
        }

    if response.status_code == 403:
        return {
            "status": "error", "outcome": "DENIED by IAM", "http_status": 403,
            "mtls_configured": mtls, "endpoint": host,
            "detail": response.text[:500],
            "lesson": ("This agent authenticated as its own Agent Identity and was "
                       "refused by IAM, which has no binding letting it invoke "
                       "Procurement Agent. Authentication is not authorization."),
        }
    if response.status_code != 200:
        return {"status": "error", "http_status": response.status_code,
                "detail": response.text[:500],
                "sent_delegation": delegated["claims"]}

    # The reply is an A2A message envelope; pull the first text part out of it.
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

    walk(response.json())
    try:
        result = json.loads(found[0]) if found else {}
    except (json.JSONDecodeError, IndexError):
        result = {"raw": (found[0] if found else "")[:500]}

    return {
        "status": "success", "outcome": "ALLOWED by IAM", "http_status": 200,
        "mtls_configured": mtls, "endpoint": host,
        "sent_delegation": delegated["claims"],
        "procurement_agent_result": result,
        "lesson": ("A real A2A protocol call across two separately identified "
                   "deployments. Two distinct credential planes are at work: Google "
                   "Cloud Agent Identity authenticated this agent over mTLS and IAM "
                   "authorized the call, while the delegated JWT is the demo's own "
                   "signed delegation model. Procurement Agent verified that JWT "
                   "before acting. The 'demo-user' subject is a demo claim, not a "
                   "Google-authenticated human."),
    }


root_agent = Agent(
    name="inventory_agent",
    model=os.getenv("MODEL", "gemini-2.5-flash"),
    description="Monitors Zoho inventory and delegates reordering to Procurement Agent.",
    instruction=(
        "You are the Inventory Agent in an agent-governance demonstration. "
        "Use check_stock to read stock, request_reorder_from_procurement_agent to get "
        "a purchase order drafted, attempt_to_create_purchase_order_directly when asked "
        "whether you could create one yourself, and show_my_identity when asked who you "
        "are or how you are authorized. Use diagnose_outbound_auth when asked to diagnose authentication, and report every field it returns verbatim.\n\n"
        "Rules you must never break:\n"
        "1. You cannot create a purchase order. You have no authority to and no tool for "
        "it. To reorder, you must ask Procurement Agent.\n"
        "2. You cannot approve anything. Approval is a human act in the Zoho UI.\n"
        "3. Never say that your identity authorizes you. Identity authenticates; IAM and "
        "delegation policy authorize.\n"
        "When a call is denied, treat that as the interesting result and explain exactly "
        "which control refused it, rather than looking for a way around it."
    ),
    tools=[show_my_identity, check_stock, attempt_to_create_purchase_order_directly,
           request_reorder_from_procurement_agent, diagnose_outbound_auth],
)
