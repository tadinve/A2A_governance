"""A2A executor for Procurement Agent.

Deterministic on purpose: the governance decisions are the demonstration, so
they must not depend on a model's phrasing. The A2A hop is a real protocol call;
what rides inside it is this application's payload.
"""
from __future__ import annotations

import json
import re
import os
import uuid

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue

from . import governance

AGENT_ID = "procurement-agent"
DEFAULT_SKU = os.getenv("DEMO_SKU", "DEMO-WIDGET-A")
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
        "sku": sku.group(0) if sku else DEFAULT_SKU,
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

        # A write requires a human delegation. Not "verifies one if offered" --
        # requires one. The earlier version treated the token as optional and
        # minted a substitute human grant when it was missing, which meant the
        # governance held only for callers that chose to participate in it.
        delegation_report: dict = {"presented": False}
        subject_token = request.get("delegated_token")
        if not subject_token and action == "create_po":
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "denied",
                "reason": "No delegated token presented",
                "note": ("Drafting a purchase order requires a human delegation "
                         "this agent received. It cannot mint one: the Auth "
                         "Broker's minting policy gives this principal no rule "
                         "for a user_access_token, so there is no fallback to "
                         "fall back to."),
                "agent_identity": SPIFFE_ID,
            })))
            return
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
            po = governance.get_purchase_order(po_id or "")
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "success" if po else "error",
                "purchase_order": po,
                "error_message": None if po else f"{po_id} not found in Zoho",
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

        sku = str(request.get("sku", DEFAULT_SKU)).upper()
        try:
            item = governance.find_item(sku)
        except Exception as exc:  # noqa: BLE001 - surface the outage, never as zero stock
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "error",
                "error_message": f"Could not reach Zoho Inventory: {type(exc).__name__}"})))
            return
        if not item:
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "error", "error_message": f"Unknown SKU {sku}"})))
            return

        try:
            vendor = governance.vendor_for(item)
        except Exception as exc:  # noqa: BLE001
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "error", "error_message": str(exc)})))
            return

        rate = item.get("purchase_rate")
        if rate is None:
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "error",
                "error_message": f"{sku} has no purchase_rate in Zoho"})))
            return

        quantity = int(request.get("quantity") or 0)
        if quantity <= 0:
            plan = governance.reorder_plan(item)
            if not plan["resolved"]:
                await event_queue.enqueue_event(new_text_message(_reply({
                    "status": "unresolved", "reason": plan["reason"]})))
                return
            quantity = plan["suggested_quantity"]
        if quantity <= 0:
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "no_action",
                "reason": f"{sku} is not below its reorder level; no purchase order needed."})))
            return

        request_id = str(uuid.uuid4())
        try:
            # This agent's own delegation down into the procurement MCP, always
            # extended from the token it was handed. There is no other input.
            mcp = governance.exchange_token(
                AGENT_ID, subject_token, "zoho-procurement-mcp", "purchaseorder.create")
        except governance.PolicyDenied as denied:
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "denied", "reason": str(denied)})))
            return

        try:
            po = governance.create_draft(
                item_id=item["item_id"], quantity=quantity, rate=float(rate),
                vendor_id=vendor["contact_id"],
                reference_number=f"A2A-{request_id[:8]}", idempotency_key=request_id)
        except Exception as exc:  # noqa: BLE001
            await event_queue.enqueue_event(new_text_message(_reply({
                "status": "error", "error_message": f"Zoho refused the purchase order: {exc}"})))
            return

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
