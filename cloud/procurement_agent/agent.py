"""Procurement Agent deployed to Agent Runtime with its own Agent Identity.

It reports on purchase orders and explains the approval boundary. It has no
approval tool, by design: approval is a human act performed in the Zoho UI
against an exact draft hash. It also cannot draft: nothing reaches this
deployment over A2A, so no human delegation ever arrives here, and the Auth
Broker grants this principal no minting rule with which to invent one.
"""
from __future__ import annotations

import os
import uuid

from google.adk.agents import Agent

from . import governance

AGENT_ID = "procurement-agent"
SPIFFE_ID = "spiffe://demo.local/agents/procurement-agent"


def show_my_identity() -> dict:
    """Report this agent's workload identity and how it becomes authorized."""
    return {
        "status": "success",
        "agent_id": AGENT_ID,
        "demo_identity": SPIFFE_ID,
        "cloud_identity": (
            "Provisioned by Agent Runtime because .agent_engine_config.json set "
            "identity_type=AGENT_IDENTITY. The canonical value is on the "
            "deployment's Identity tab; it is a principal:// URI, not a service account."
        ),
        "authentication": "Agent Identity credentials supplied by Agent Runtime ADC",
        "authorization": "Google Cloud IAM bindings on that principal",
        "note": "Identity proves which workload called. It never grants access by itself.",
    }


def draft_purchase_order(sku: str, quantity: int) -> dict:
    """Attempt to draft a purchase order. This deployment is expected to fail.

    This is the non-A2A deployment. Nothing reaches it over A2A, so it is never
    handed a human delegation, and it holds no broker permission to mint one:
    `procurement-agent-adk-principal` has an empty `may_mint`. Drafting
    therefore stops at the first token it needs, and the denial comes from the
    Auth Broker rather than from a check this process could be talked out of.

    An earlier version filled the gap by minting its own `demo-user` token and
    carrying on, which produced a complete-looking delegation chain that no
    human was ever at the top of.
    """
    sku = sku.upper()
    try:
        item = governance.find_item(sku)
    except Exception as exc:
        return {"status": "error",
                "error_message": f"Could not reach Zoho Inventory: {type(exc).__name__}"}
    if not item:
        return {"status": "error", "error_message": f"Unknown SKU {sku}"}
    if quantity <= 0:
        return {"status": "error", "error_message": "Quantity must be positive"}

    try:
        governance.human_token()
    except governance.SigningUnavailable as denied:
        return {
            "status": "refused",
            "outcome": "DENIED, as designed",
            "would_have_drafted": {"sku": sku, "quantity": quantity,
                                   "item_id": item.get("item_id")},
            "detail": str(denied),
            "lesson": (
                "This agent is authenticated by its own Agent Identity and is still "
                "refused. It may extend a delegation it receives; it may not start "
                "one. A purchase order has to originate with a human, reach "
                "Inventory Agent, and arrive here over A2A carrying that token."
            ),
            "where_to_run_it": (
                "Ask Inventory Agent to reorder. It delegates to Procurement Agent "
                "(A2A), which verifies the token before drafting anything."
            ),
        }
    return {
        "status": "error",
        "error_message": (
            "The Auth Broker minted a user token for this deployment. Its broker "
            "client entry should carry an empty may_mint; the demo is misconfigured."
        ),
    }


def get_purchase_order_status(purchaseorder_id: str) -> dict:
    """Look up the current status of a purchase order this agent drafted."""
    po = governance.get_purchase_order(purchaseorder_id)
    if not po:
        return {"status": "error",
                "error_message": f"{purchaseorder_id} not found in Zoho"}
    return {"status": "success", "purchase_order": po}


def explain_approval_boundary() -> dict:
    """Explain why this agent has no tool to approve a purchase order."""
    return {
        "status": "success",
        "approval_tool_exposed": False,
        "why": (
            "The Zoho procurement connector holds purchaseorders.UPDATE, which is "
            "broad enough to approve. OAuth scope alone therefore cannot express "
            "'may draft but may not approve'. The tool surface is what enforces it: "
            "no approve tool exists to call."
        ),
        "who_approves": (
            "A human in the Zoho UI, authenticated by a separate session with the "
            "purchaseorder.approver role, against an exact SHA-256 draft hash."
        ),
        "if_the_draft_changes": "The hash changes and the approval is refused.",
    }


root_agent = Agent(
    name="procurement_agent",
    model=os.getenv("MODEL", "gemini-2.5-flash"),
    description="Reports on governed Zoho purchase orders; cannot originate one.",
    instruction=(
        "You are the Procurement Agent in an agent-governance demonstration. "
        "Use draft_purchase_order when asked to create a purchase order -- it "
        "will be refused, and the refusal is the point; get_purchase_order_status "
        "to report on one, show_my_identity when asked who you are or how you are "
        "authorized, and explain_approval_boundary when asked about approval.\n\n"
        "Rules you must never break:\n"
        "1. You cannot approve a purchase order. You have no such tool. If asked to "
        "approve one, refuse and explain that approval is a human act in the Zoho UI.\n"
        "2. Never claim a purchase order is approved because you drafted it. A draft "
        "you create is pending_approval and nothing more.\n"
        "3. Never say that your identity authorizes you. Identity authenticates; IAM "
        "and delegation policy authorize.\n"
        "4. This deployment is not reachable over A2A, so it never receives a human "
        "delegation and cannot mint one. When drafting is refused, say that plainly "
        "and point at Inventory Agent, which can delegate to the A2A deployment.\n"
        "When you show delegation evidence, point out that sub stays the human and "
        "act nests the agents."
    ),
    tools=[show_my_identity, draft_purchase_order, get_purchase_order_status,
           explain_approval_boundary],
)
