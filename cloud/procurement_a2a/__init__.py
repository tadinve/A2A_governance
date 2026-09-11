"""Builds the A2A-capable Procurement Agent."""
from __future__ import annotations


def build_agent():
    """Construct the A2aAgent. Imports are local so the module stays importable."""
    from a2a.types import AgentSkill
    from vertexai.agent_engines.templates.a2a import A2aAgent, create_agent_card

    from .executor import ProcurementAgentExecutor

    skills = [
        AgentSkill(
            id="purchase-order-drafting",
            name="Purchase order drafting",
            description=("Creates and submits a purchase-order draft for human "
                         "approval. Verifies a delegated token when one is presented. "
                         "Cannot approve."),
            tags=["procurement", "zoho", "human-approval"],
            examples=['{"action":"create_po","sku":"DEMO-WIDGET-A","quantity":70}'],
        ),
        AgentSkill(
            id="purchase-order-status",
            name="Purchase order status",
            description="Reports the current status of a drafted purchase order.",
            tags=["procurement", "status"],
            examples=['{"action":"get_status","purchaseorder_id":"PO-1001"}'],
        ),
    ]
    card = create_agent_card(
        agent_name="Procurement Agent (A2A)",
        description=("Drafts governed Zoho purchase orders over the A2A protocol. "
                     "Owns purchase-order authority; owns no approval authority."),
        skills=skills,
    )
    return A2aAgent(agent_card=card, agent_executor_builder=ProcurementAgentExecutor)
