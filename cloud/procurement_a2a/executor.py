"""A2A executor for Procurement Agent.

Deterministic on purpose: the governance decisions are the demonstration, so
they must not depend on a model's phrasing. The A2A hop is a real protocol call;
what rides inside it is this application's payload.
"""
from __future__ import annotations

import json
import re
import uuid

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue

from . import governance

AGENT_ID = "procurement-agent"
SPIFFE_ID = "spiffe://demo.local/agents/procurement-agent"


def _parse(text: str) -> dict:
    """Accept a structured payload, or fall back to reading a sentence."""
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, TypeError):
        pass
    sku = re.search(r"CK-[A-Z]+-\d+", (text or "").upper())
    quantity = re.search(r"\b(\d+)\s*(?:units?|pcs?)\b", (text or "").lower())
    action = "get_status" if re.search(r"\bstatus\b", (text or "").lower()) else "create_po"
    po = re.search(r"PO-\d+", (text or "").upper())
    return {
        "action": action,
        "sku": sku.group(0) if sku else "CK-GPU-42",
        "quantity": int(quantity.group(1)) if quantity else 0,
        "purchaseorder_id": po.group(0) if po else None,
    }


def _reply(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


class ProcurementAgentExecutor(AgentExecutor):
    """Drafts purchase orders. Has no capability to approve one."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        request = _parse(context.get_user_input())
        action = request.get("action", "create_po")

        # The caller may present a delegated token. When it does, it is verified
        # for real: signature, issuer, audience, and scope.
        delegation_report: dict = {"presented": False}
        subject_token = request.get("delegated_token")
        if subject_token:
            try:
                claims = governance.decode_token(subject_token, audience=AGENT_ID)
                scopes = governance.token_scopes(claims)
                delegation_report = {
                    "presented": True,
                    "verified": True,
                    "claims": governance.public_claims(claims),
                    "note": ("Signature, issuer, audience and scope verified against "
                             "the demo issuer key. This is the demo's own delegation "
                             "mechanism: the 'demo-user' subject is a claim this demo "
                             "mints and signs, not an identity Google authenticated. "
                             "Google authenticated the calling agent, not the human."),
                }
                required = "purchase.request" if action == "create_po" else "purchase.status"
                if required not in scopes:
                    await event_queue.enqueue_event(new_text_message(_reply({
                        "status": "denied",
                        "reason": f"Delegated token lacks the '{required}' scope",
                        "presented_scopes": sorted(scopes),
                    })))
                    return
            except Exception as exc:  # noqa: BLE001 - a rejected token is a result
                await event_queue.enqueue_event(new_text_message(_reply({
                    "status": "denied",
                    "reason": f"Delegated token rejected: {type(exc).__name__}: {exc}",
                    "note": "Procurement Agent refuses work it cannot attribute to a human.",
                })))
                return

        if action == "get_status":
            po_id = request.get("purchaseorder_id")
            po = governance.PURCHASE_ORDERS.get(po_id or "")
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "success" if po else "error",
                "purchase_order": po,
                "error_message": None if po else f"{po_id} not found in this instance",
                "agent_identity": SPIFFE_ID,
            })))
            return

        if action == "approve":
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "refused",
                "approval_tool_exposed": False,
                "reason": ("Procurement Agent has no capability to approve a purchase "
                           "order. Approval is a human act in the Zoho UI, bound to an "
                           "exact draft hash."),
            })))
            return

        sku = str(request.get("sku", "CK-GPU-42")).upper()
        item = governance.ITEMS.get(sku)
        if not item:
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "error", "error_message": f"Unknown SKU {sku}"})))
            return
        quantity = int(request.get("quantity") or 0)
        if quantity <= 0:
            quantity = item["target_stock"] - item["stock_on_hand"]

        request_id = str(uuid.uuid4())
        try:
            # This agent's own delegation down into the procurement MCP.
            base = subject_token or governance.exchange_token(
                "inventory-agent", governance.human_token(), AGENT_ID,
                "purchase.request")["access_token"]
            mcp = governance.exchange_token(
                AGENT_ID, base, "zoho-procurement-mcp", "purchaseorder.create")
        except governance.PolicyDenied as denied:
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "denied", "reason": str(denied)})))
            return

        po = governance.create_draft(
            item_id=item["item_id"], quantity=quantity, rate=item["purchase_rate"],
            vendor_id=item["preferred_vendor_id"],
            reference_number=f"A2A-{request_id[:8]}", idempotency_key=request_id)

        await event_queue.enqueue_event(new_text_message(_reply({
            "status": "success",
            "agent_identity": SPIFFE_ID,
            "purchase_order": po,
            "human_approval": "REQUIRED. This agent cannot approve it.",
            "received_delegation": delegation_report,
            "issued_to_procurement_mcp": mcp["claims"],
        })))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel is not supported by this demo agent")
