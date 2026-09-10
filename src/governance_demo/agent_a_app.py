from __future__ import annotations

import re
import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException
from opentelemetry import trace
from pydantic import BaseModel

from .audit import record
from .security import bearer_token, decode_token, public_claims
from .settings import GATEWAY_URL, IDP_URL, REGISTRY_URL
from .telemetry import instrument_fastapi


app = FastAPI(title="Inventory Orchestrator Agent A", version="1.0")
instrument_fastapi(app, "inventory-orchestrator-agent-a")
tracer = trace.get_tracer(__name__)

AGENT_ID = "agent-a"
AGENT_SECRET = "agent-a-demo-secret"
SPIFFE_ID = "spiffe://demo.local/agents/agent-a"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class AskRequest(BaseModel):
    text: str


async def _agent_token(client: httpx.AsyncClient, audience: str, scope: str) -> str:
    response = await client.post(
        f"{IDP_URL}/oauth/token",
        data={"grant_type": "client_credentials", "audience": audience, "scope": scope},
        auth=(AGENT_ID, AGENT_SECRET),
    )
    response.raise_for_status()
    return response.json()["access_token"]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": AGENT_ID}


@app.get("/identity")
def identity() -> dict:
    return {
        "agent_id": AGENT_ID,
        "identity": SPIFFE_ID,
        "identity_type": "demo SPIFFE-style identity",
        "cloud_equivalent": "Google Cloud Agent Identity principal://.../reasoningEngines/AGENT_ID",
    }


@app.post("/ask")
async def ask(payload: AskRequest, authorization: str | None = Header(None)) -> dict:
    try:
        user_token = bearer_token(authorization)
        user_claims = decode_token(user_token, audience=AGENT_ID)
    except Exception as exc:
        raise HTTPException(401, f"User authentication failed: {type(exc).__name__}") from exc

    request_id = str(uuid.uuid4())
    with tracer.start_as_current_span("agent.invoke agent-a") as span:
        span.set_attribute("agent.id", AGENT_ID)
        span.set_attribute("enduser.id", user_claims["sub"])
        span.set_attribute("request.id", request_id)
        record("agent-a", "USER_REQUEST_ACCEPTED", request_id=request_id, user=user_claims["sub"])

        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            # Agent A uses its own authority to discover Agent B.
            registry_token = await _agent_token(client, "registry", "registry.read")
            registry_response = await client.get(
                f"{REGISTRY_URL}/registry",
                headers={"Authorization": f"Bearer {registry_token}"},
            )
            registry_response.raise_for_status()
            agent_b = registry_response.json()["agents"]["agent-b"]

            # Auth Manager / STS preserves the user subject and adds Agent A as actor.
            actor_token = await _agent_token(client, "sts", "token.exchange")
            exchange = await client.post(
                f"{IDP_URL}/oauth/token",
                data={
                    "grant_type": TOKEN_EXCHANGE_GRANT,
                    "subject_token": user_token,
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                    "actor_token": actor_token,
                    "actor_token_type": ACCESS_TOKEN_TYPE,
                    "audience": "agent-b",
                    "scope": "inventory.read",
                },
                auth=(AGENT_ID, AGENT_SECRET),
            )
            exchange.raise_for_status()
            delegated = exchange.json()

            match = re.search(r"CK-[A-Z]+-\d+", payload.text.upper())
            sku = match.group(0) if match else "CK-GPU-42"
            a2a_message = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": f"msg-{uuid.uuid4()}",
                        "role": "user",
                        "parts": [{"kind": "text", "text": payload.text}],
                        "metadata": {"original_user": user_claims["sub"]},
                    }
                },
            }
            a2a_response = await client.post(
                f"{GATEWAY_URL}/route/agent-b",
                json=a2a_message,
                headers={"Authorization": f"Bearer {delegated['access_token']}"},
            )
            if a2a_response.status_code >= 400:
                record(
                    "agent-a",
                    "A2A_REQUEST_DENIED",
                    request_id=request_id,
                    target="agent-b",
                    status=a2a_response.status_code,
                )
                try:
                    detail = a2a_response.json().get("detail", a2a_response.text)
                except ValueError:
                    detail = a2a_response.text
                raise HTTPException(a2a_response.status_code, detail)

    record("agent-a", "A2A_RESPONSE_RECEIVED", request_id=request_id, target="agent-b")
    return {
        "request_id": request_id,
        "answer": a2a_response.json()["result"]["artifacts"][0]["parts"][0]["text"],
        "governance_evidence": {
            "user_identity": public_claims(user_claims),
            "agent_identity": SPIFFE_ID,
            "discovered_agent": {
                "name": agent_b["display_name"],
                "identity": agent_b["identity"],
                "skills": agent_b["skills"],
            },
            "delegated_token_claims": delegated["claims_for_demo"],
            "a2a_method": a2a_message["method"],
            "gateway": "allow",
        },
        "a2a_result": a2a_response.json(),
    }
