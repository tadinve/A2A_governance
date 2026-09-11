"""Auth Broker: the only component that can sign a delegation token.

Every other service asks this one for a signature. That concentration is the
point. It means:

* the signing key has exactly one consumer, so its IAM grant
  (``roles/cloudkms.signerVerifier``) names exactly one principal;
* an agent that is compromised can request the tokens policy allows it and
  nothing more, because it never holds key material to forge others;
* every mint is one audited event in one place.

The broker authenticates its callers, checks a minting policy that constrains
token kind, audience, scope, and lifetime, and only then signs. It publishes the
public key so verifiers need no shared secret and no key distribution step.
"""
from __future__ import annotations

import base64
import secrets
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.security.utils import get_authorization_scheme_param
from opentelemetry import trace
from pydantic import BaseModel, Field

from .audit import record
from .settings import ISSUER, load_json
from .signing import ALGORITHM, build_backend, sign_claims
from .telemetry import instrument_fastapi


app = FastAPI(title="Delegation Auth Broker", version="1.0")
instrument_fastapi(app, "auth-broker")
tracer = trace.get_tracer(__name__)

# Built once at startup. For the KMS backend this is a client plus a cached
# public key; for the local backend this is the only copy of the private key.
_backend = build_backend()


class SignRequest(BaseModel):
    subject: str
    audience: str
    scopes: list[str] = Field(default_factory=list)
    token_kind: str
    actor_chain: dict[str, Any] | None = None
    lifetime_seconds: int = 300


def _authenticate_caller(authorization: str | None) -> tuple[str, dict]:
    """Only registered broker clients may ask for a signature."""
    scheme, value = get_authorization_scheme_param(authorization or "")
    if scheme.lower() != "basic" or not value:
        raise HTTPException(401, "Broker clients must authenticate with HTTP Basic")
    try:
        client_id, client_secret = base64.b64decode(value).decode().split(":", 1)
    except Exception as exc:
        raise HTTPException(401, "Invalid broker client authentication") from exc
    client = load_json("broker_clients.json").get(client_id)
    if not client or not secrets.compare_digest(client["client_secret"], client_secret):
        raise HTTPException(401, "Invalid broker client credentials")
    return client_id, client


def _authorize_mint(client_id: str, client: dict, request: SignRequest) -> dict:
    """Find a minting rule that permits exactly what was asked for."""
    if request.token_kind not in client.get("may_mint", []):
        return {}
    rules = load_json("policies.json")["token_minting"]
    requested = set(request.scopes)
    for rule in rules:
        if rule["minter"] != client_id or rule["token_kind"] != request.token_kind:
            continue
        if request.audience not in rule["audiences"]:
            continue
        if not requested or not requested.issubset(set(rule["allowed_scopes"])):
            continue
        if request.lifetime_seconds > rule["max_lifetime_seconds"]:
            continue
        return rule
    return {}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": "auth-broker"}


@app.get("/jwks")
def jwks() -> dict:
    """Publish the public key so any verifier can check a token.

    Public by design: verification needs no authorization, and handing out the
    public half is how we avoid shipping key material to verifiers.
    """
    return {
        "public_key_pem": _backend.public_key_pem().decode(),
        "kid": _backend.kid,
        "algorithm": ALGORITHM,
        "backend": _backend.name,
        "issuer": ISSUER,
    }


@app.post("/sign")
def sign(request: SignRequest, authorization: str | None = Header(None)) -> dict:
    client_id, client = _authenticate_caller(authorization)
    rule = _authorize_mint(client_id, client, request)
    if not rule:
        record(
            "auth-broker",
            "DELEGATION_MINT_DENIED",
            minter=client_id,
            token_kind=request.token_kind,
            audience=request.audience,
            scope=" ".join(request.scopes),
        )
        raise HTTPException(403, "No minting policy permits this token request")

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": request.subject,
        "aud": request.audience,
        "iat": now,
        "nbf": now,
        "exp": now + request.lifetime_seconds,
        "scope": " ".join(request.scopes),
        "token_kind": request.token_kind,
    }
    if request.actor_chain:
        claims["act"] = request.actor_chain

    with tracer.start_as_current_span("auth_broker.sign") as span:
        span.set_attribute("auth.minter", client_id)
        span.set_attribute("auth.subject", request.subject)
        span.set_attribute("auth.audience", request.audience)
        span.set_attribute("auth.token_kind", request.token_kind)
        span.set_attribute("auth.backend", _backend.name)
        token = sign_claims(claims, _backend)

    record(
        "auth-broker",
        "DELEGATION_TOKEN_SIGNED",
        minter=client_id,
        subject=request.subject,
        audience=request.audience,
        scope=" ".join(request.scopes),
        token_kind=request.token_kind,
        backend=_backend.name,
        kid=_backend.kid,
    )
    return {"token": token, "kid": _backend.kid, "expires_in": request.lifetime_seconds}
