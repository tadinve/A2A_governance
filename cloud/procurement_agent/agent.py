"""Procurement Agent deployed to Agent Runtime with its own Agent Identity.

It owns purchase-order drafting. It has no approval tool, by design: approval is
a human act performed in the Zoho UI against an exact draft hash.
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
    """Draft and submit a purchase order, returning the full delegation evidence.

    Runs the real nested token exchange: the human subject is preserved while
    each agent is added to the actor chain.
    """
    sku = sku.upper()
    item = governance.ITEMS.get(sku)
    if not item:
        return {"status": "error", "error_message": f"Unknown SKU {sku}"}
    if quantity <= 0:
        return {"status": "error", "error_message": "Quantity must be positive"}

    request_id = str(uuid.uuid4())
    try:
        # Hop 1: the A2A delegation this agent received from Inventory Agent.
        received = governance.exchange_token(
            "inventory-agent", governance.human_token(), AGENT_ID, "purchase.request")
        # Hop 2: this agent's own delegation down into the procurement MCP.
        mcp = governance.exchange_token(
            AGENT_ID, received["access_token"], "zoho-procurement-mcp", "purchaseorder.create")
    except governance.PolicyDenied as denied:
        return {"status": "error", "error_message": str(denied)}

    po = governance.create_draft(
        item_id=item["item_id"], quantity=quantity, rate=item["purchase_rate"],
        vendor_id=item["preferred_vendor_id"],
        reference_number=f"AGENT-{request_id[:8]}", idempotency_key=request_id)

    return {
        "status": "success",
        "purchase_order": po,
        "human_approval": "REQUIRED. This agent cannot approve it.",
        "delegation_evidence": {
            "received_from_inventory_agent": received["claims"],
            "issued_to_procurement_mcp": mcp["claims"],
            "explanation": (
                "sub stays the human across both hops. act nests "
                "procurement-agent over inventory-agent, so the audit trail "
                "shows who asked and which workloads acted."
            ),
        },
    }


def get_purchase_order_status(purchaseorder_id: str) -> dict:
    """Look up the current status of a purchase order this agent drafted."""
    po = governance.PURCHASE_ORDERS.get(purchaseorder_id)
    if not po:
        return {"status": "error",
                "error_message": f"{purchaseorder_id} not found in this instance"}
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
    description="Drafts governed Zoho purchase orders and reports their status.",
    instruction=(
        "You are the Procurement Agent in an agent-governance demonstration. "
        "Use draft_purchase_order to create a purchase order, "
        "get_purchase_order_status to report on one, show_my_identity when asked "
        "who you are or how you are authorized, and explain_approval_boundary when "
        "asked about approval.\n\n"
        "Rules you must never break:\n"
        "1. You cannot approve a purchase order. You have no such tool. If asked to "
        "approve one, refuse and explain that approval is a human act in the Zoho UI.\n"
        "2. Never claim a purchase order is approved because you drafted it. A draft "
        "you create is pending_approval and nothing more.\n"
        "3. Never say that your identity authorizes you. Identity authenticates; IAM "
        "and delegation policy authorize.\n"
        "When you show delegation evidence, point out that sub stays the human and "
        "act nests the agents."
    ),
    tools=[show_my_identity, draft_purchase_order, get_purchase_order_status,
           explain_approval_boundary],
)
