from __future__ import annotations

from google.adk.agents import Agent


def explain_identity() -> dict:
    """Explain the authority model used by this deployed agent."""
    return {
        "status": "success",
        "authority": "agent-own-authority",
        "authentication": "Agent Identity credentials supplied by Agent Runtime ADC",
        "authorization": "Google Cloud IAM policies bound to this agent's principal",
        "note": "The canonical effective identity is shown on the deployment Identity tab.",
    }


root_agent = Agent(
    name="identity_demo_agent",
    model="gemini-2.5-flash",
    instruction=(
        "You are an Agent Identity demonstration agent. Use explain_identity whenever "
        "asked who you are, how you authenticate, or what you are authorized to do. "
        "Never claim that identity itself grants access; IAM grants access."
    ),
    tools=[explain_identity],
)
