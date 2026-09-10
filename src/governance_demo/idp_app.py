from __future__ import annotations

import secrets

from fastapi import FastAPI, Form, Header, HTTPException
from fastapi.security.utils import get_authorization_scheme_param
from opentelemetry import trace

from .audit import record
from .security import decode_token, extend_actor_chain, issue_token, public_claims, scopes
from .settings import load_json
from .telemetry import instrument_fastapi


app = FastAPI(title="Demo Identity Provider and Token Broker", version="1.0")
instrument_fastapi(app, "identity-broker")
tracer = trace.get_tracer(__name__)


def _authenticate_client(authorization: str | None) -> tuple[str, dict]:
    scheme, value = get_authorization_scheme_param(authorization or "")
    if scheme.lower() != "basic" or not value:
        raise HTTPException(401, "Client must authenticate with HTTP Basic")
    import base64

    try:
        client_id, client_secret = base64.b64decode(value).decode().split(":", 1)
    except Exception as exc:
        raise HTTPException(401, "Invalid client authentication") from exc
    client = load_json("client_registry.json").get(client_id)
    if not client or not secrets.compare_digest(client["client_secret"], client_secret):
        raise HTTPException(401, "Invalid client credentials")
    return client_id, client


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": "identity-broker"}


@app.post("/login")
def login(user_id: str = Form("demo-user")) -> dict:
    """Simulate Cloud Identity sign-in; no password because this is a closed demo."""
    with tracer.start_as_current_span("identity.user_login") as span:
        span.set_attribute("enduser.id", user_id)
        token = issue_token(
            subject=user_id,
            audience="agent-a",
            scopes=["assistant.inventory"],
            token_kind="user_access_token",
            lifetime_seconds=900,
        )
        record("identity-broker", "USER_TOKEN_ISSUED", user=user_id, audience="agent-a")
        return {"access_token": token, "token_type": "Bearer", "expires_in": 900}


@app.post("/oauth/token")
def oauth_token(
    grant_type: str = Form(...),
    audience: str = Form("sts"),
    scope: str = Form("token.exchange"),
    subject_token: str | None = Form(None),
    subject_token_type: str | None = Form(None),
    actor_token: str | None = Form(None),
    actor_token_type: str | None = Form(None),
    authorization: str | None = Header(None),
) -> dict:
    client_id, client = _authenticate_client(authorization)

    if grant_type == "client_credentials":
        if audience not in client["allowed_audiences"]:
            raise HTTPException(403, "Client is not allowed to request this audience")
        token = issue_token(
            subject=client_id,
            audience=audience,
            scopes=scope.split(),
            token_kind="agent_credential",
        )
        record(
            "identity-broker",
            "AGENT_TOKEN_ISSUED",
            agent=client_id,
            spiffe_id=client["spiffe_id"],
            audience=audience,
            scope=scope,
        )
        return {"access_token": token, "token_type": "Bearer", "expires_in": 300}

    expected_grant = "urn:ietf:params:oauth:grant-type:token-exchange"
    expected_type = "urn:ietf:params:oauth:token-type:access_token"
    if grant_type != expected_grant:
        raise HTTPException(400, "Unsupported grant_type")
    if not all([subject_token, actor_token, subject_token_type, actor_token_type]):
        raise HTTPException(400, "Token exchange requires subject and actor tokens and types")
    if subject_token_type != expected_type or actor_token_type != expected_type:
        raise HTTPException(400, "Unsupported token type")

    try:
        actor_claims = decode_token(actor_token, audience="sts")
        subject_claims = decode_token(subject_token)
    except Exception as exc:
        raise HTTPException(401, f"Token validation failed: {type(exc).__name__}") from exc
    if actor_claims.get("sub") != client_id:
        raise HTTPException(403, "Authenticated client and actor token do not match")

    requested_scopes = set(scope.split())
    rules = load_json("policies.json")["token_exchange"]
    # PyJWT returns a string for a single audience. Keep policy evaluation explicit.
    subject_aud = subject_claims.get("aud")
    subject_audiences = {subject_aud} if isinstance(subject_aud, str) else set(subject_aud or [])
    matched = next(
        (
            rule
            for rule in rules
            if rule["actor"] == client_id
            and rule["subject_audience"] in subject_audiences
            and rule["subject_scope"] in scopes(subject_claims)
            and rule["target_audience"] == audience
            and requested_scopes == {rule["output_scope"]}
        ),
        None,
    )
    if not matched:
        record(
            "identity-broker",
            "TOKEN_EXCHANGE_DENIED",
            actor=client_id,
            target=audience,
            requested_scope=scope,
        )
        raise HTTPException(403, "No delegation policy permits this token exchange")

    with tracer.start_as_current_span("auth_manager.token_exchange") as span:
        span.set_attribute("auth.actor", client_id)
        span.set_attribute("auth.subject", subject_claims["sub"])
        span.set_attribute("auth.audience", audience)
        span.set_attribute("auth.scope", scope)
        delegated = issue_token(
            subject=subject_claims["sub"],
            audience=audience,
            scopes=scope.split(),
            token_kind="delegated_access_token",
            actor_chain=extend_actor_chain(client_id, subject_claims),
        )
    output_claims = decode_token(delegated, audience=audience)
    record(
        "identity-broker",
        "TOKEN_EXCHANGE_ALLOWED",
        actor=client_id,
        subject=subject_claims["sub"],
        target=audience,
        scope=scope,
        actor_chain=output_claims["act"],
    )
    return {
        "access_token": delegated,
        "issued_token_type": expected_type,
        "token_type": "Bearer",
        "expires_in": 300,
        "scope": scope,
        "claims_for_demo": public_claims(output_claims),
    }
