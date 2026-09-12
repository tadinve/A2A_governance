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

For a *delegated* token it does one thing more, and it is the thing that makes
the signature mean something: it derives ``sub`` and ``act`` from evidence
rather than from the request body. The subject comes from the preceding token,
verified here against this broker's own key; the actor comes from the caller's
authenticated identity. An agent therefore cannot ask for a token naming a
human it never received a grant from, which is precisely what "the caller is
authenticated" does not, on its own, rule out.
"""
from __future__ import annotations

import base64
import os
import secrets
import time
from typing import Any

import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.security.utils import get_authorization_scheme_param
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

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


# A delegated token extends a grant that already exists. An origin token starts
# one, so it has no preceding token to derive a subject from.
DELEGATED_TOKEN_KIND = "delegated_access_token"


class SignRequest(BaseModel):
    """What a minter may ask for -- and, just as importantly, what it may not.

    A delegated request carries *evidence*, never assertions: the subject token
    being extended, and nothing else. ``sub`` and ``act`` are derived here from
    that token and from the caller's authenticated identity, so a compromised
    agent cannot name a subject it was never granted or write itself a shorter
    actor chain. ``extra="forbid"`` makes the old contract fail loudly instead
    of having a stale ``subject``/``actor_chain`` silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    token_kind: str
    audience: str
    scopes: list[str] = Field(default_factory=list)
    lifetime_seconds: int = 300
    # Delegated tokens: the evidence the broker derives from.
    subject_token: str | None = None
    actor_token: str | None = None
    # Origin tokens (a human sign-in, an agent credential) only.
    subject: str | None = None


def _unverified_claims(token: str) -> dict:
    """Decode claims WITHOUT verifying, for diagnostics only.

    Never used to authorize. It exists so a verification failure reports which
    issuer and subject were actually presented, instead of a bare 401 that
    leaves an operator guessing.
    """
    try:
        import base64 as _b64
        import json as _json

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return _json.loads(_b64.urlsafe_b64decode(payload))
    except Exception:
        return {}


_jwks_clients: dict[str, Any] = {}


def _pool_jwks_uri(issuer: str) -> str:
    """Discover a workload identity pool's JWKS endpoint via OIDC discovery."""
    import httpx

    document = httpx.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration",
                         timeout=15).json()
    return document["jwks_uri"]


def _verify_google_identity(token: str) -> dict | None:
    """Verify a caller's ID token and return its claims.

    Two issuers matter here, and they need different verification paths:

    * ordinary Google ID tokens (service accounts, users), verified against
      Google's OAuth certificates;
    * Agent Identity tokens, which Agent Runtime issues through the
      organization's workload identity pool. Their ``iss`` is an STS pool URL
      and their ``sub`` is a SPIFFE ID, so Google's standard certificates do
      not contain the signing key. Those are verified against the pool's own
      JWKS, discovered from the issuer.

    Cloud Run has already enforced roles/run.invoker before the request lands.
    Verifying again here is what turns "admitted" into "this specific agent",
    which is the identity the minting allowlist is checked against.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    audience = os.getenv("AUTH_BROKER_AUDIENCE", "").strip() or None
    seen = _unverified_claims(token)
    issuer = str(seen.get("iss", ""))
    failures = []

    if issuer.startswith("https://sts.googleapis.com/"):
        try:
            import jwt as pyjwt

            if issuer not in _jwks_clients:
                _jwks_clients[issuer] = pyjwt.PyJWKClient(_pool_jwks_uri(issuer))
            signing_key = _jwks_clients[issuer].get_signing_key_from_jwt(token)
            return pyjwt.decode(
                token, signing_key.key, algorithms=["RS256"],
                audience=audience, issuer=issuer,
                options={"verify_aud": audience is not None},
            )
        except Exception as exc:
            failures.append(f"workload_identity_pool:{type(exc).__name__}:{str(exc)[:140]}")
    else:
        request = google_requests.Request()
        for label, kwargs in (("with_audience", {"audience": audience} if audience else None),
                              ("no_audience", {})):
            if kwargs is None:
                continue
            try:
                return google_id_token.verify_token(token, request, **kwargs)
            except Exception as exc:
                failures.append(f"{label}:{type(exc).__name__}:{str(exc)[:120]}")

    record("auth-broker", "BROKER_TOKEN_VERIFY_FAILED",
           failures=failures,
           unverified_iss=seen.get("iss"),
           unverified_aud=seen.get("aud"),
           unverified_sub=seen.get("sub"),
           unverified_claim_names=sorted(seen.keys()))
    return None


def _authenticate_caller(authorization: str | None) -> tuple[str, dict]:
    """Identify the caller, by Google identity in cloud or Basic locally."""
    scheme, value = get_authorization_scheme_param(authorization or "")
    clients = load_json("broker_clients.json")

    if scheme.lower() == "bearer" and value:
        claims = _verify_google_identity(value)
        if not claims:
            raise HTTPException(401, "ID token failed verification")
        # Agent Identity and service accounts surface under different claims.
        presented = {str(claims.get(field)) for field in ("email", "sub", "azp")
                     if claims.get(field)}
        for client_id, client in clients.items():
            allowed = set(client.get("allowed_principals", []))
            if allowed & presented:
                matched = sorted(allowed & presented)[0]
                record("auth-broker", "BROKER_CALLER_AUTHENTICATED",
                       minter=client_id, method="google_id_token", principal=matched)
                return client_id, client
        record("auth-broker", "BROKER_CALLER_REJECTED", method="google_id_token",
               presented=sorted(presented),
               reason="verified identity is not an allowed minter")
        raise HTTPException(
            403, f"Verified identity is not an allowed minter: {sorted(presented)}")

    if scheme.lower() != "basic" or not value:
        raise HTTPException(
            401, "Authenticate with a Google ID token (Bearer) or broker client Basic auth")
    try:
        client_id, client_secret = base64.b64decode(value).decode().split(":", 1)
    except Exception as exc:
        raise HTTPException(401, "Invalid broker client authentication") from exc
    client = clients.get(client_id)
    if not client or not secrets.compare_digest(client.get("client_secret", ""), client_secret):
        raise HTTPException(401, "Invalid broker client credentials")
    if client.get("basic_auth_enabled") is False:
        raise HTTPException(403, f"{client_id} may not use Basic auth in this deployment")
    return client_id, client


def _verify_delegation_token(token: str, audience: str | None = None) -> dict:
    """Verify a token this broker itself signed, against its own public key.

    The broker is the sole issuer, so it can check any delegation it is asked to
    extend without trusting the caller's account of what that token said.
    """
    return jwt.decode(
        token,
        _backend.public_key_pem(),
        algorithms=[ALGORITHM],
        audience=audience,
        issuer=ISSUER,
        options={"verify_aud": audience is not None},
    )


def _extend_actor_chain(actor: str, subject_claims: dict) -> dict:
    """Nest the new actor over whatever chain the subject token already carried."""
    chain: dict[str, Any] = {"sub": actor}
    if subject_claims.get("act"):
        chain["act"] = subject_claims["act"]
    return chain


def _derive_delegation(client_id: str, client: dict,
                       request: SignRequest) -> tuple[dict, str]:
    """Derive the subject claims and the acting identity from verified evidence.

    Two ways a caller's actor identity becomes known, and neither is "the caller
    said so":

    * a deployed agent authenticates with its own Agent Identity, and its broker
      client entry names the one delegation actor that principal may act as;
    * the Identity Broker is a delegation service acting for several agents, so
      it presents the agent's own credential and the actor is read from that
      token's verified ``sub``.
    """
    if request.subject is not None:
        raise HTTPException(
            400, "A delegated token's subject is derived from subject_token, not supplied")
    if not request.subject_token:
        raise HTTPException(400, "A delegated token requires subject_token")
    try:
        subject_claims = _verify_delegation_token(request.subject_token)
    except Exception as exc:
        record("auth-broker", "DELEGATION_SUBJECT_TOKEN_REJECTED",
               minter=client_id, error=type(exc).__name__)
        raise HTTPException(
            401, f"subject_token failed verification: {type(exc).__name__}") from None

    bound_actor = client.get("delegation_actor")
    if bound_actor:
        if request.actor_token:
            raise HTTPException(
                403, f"{client_id} is authenticated as '{bound_actor}' and may not "
                     f"present an actor token")
        return subject_claims, str(bound_actor)

    if not client.get("may_present_actor_token"):
        raise HTTPException(
            403, f"{client_id} has no delegation actor and may not present one")
    if not request.actor_token:
        raise HTTPException(400, f"{client_id} must present an actor_token")
    try:
        actor_claims = _verify_delegation_token(request.actor_token, audience="sts")
    except Exception as exc:
        raise HTTPException(
            401, f"actor_token failed verification: {type(exc).__name__}") from None
    if actor_claims.get("token_kind") != "agent_credential":
        raise HTTPException(403, "actor_token must be an agent credential")
    actor = str(actor_claims.get("sub") or "")
    if not actor:
        raise HTTPException(403, "actor_token carries no subject")
    return subject_claims, actor


def _authorize_delegation(actor: str, subject_claims: dict, request: SignRequest) -> dict:
    """Apply the delegation policy: who may extend which grant, to what.

    This is the same rule set the Identity Broker evaluates, re-evaluated here
    against claims the broker verified itself. A caller that skips the Identity
    Broker, or lies to it, gets the same answer.
    """
    audience = subject_claims.get("aud")
    # PyJWT returns a bare string for a single audience.
    subject_audiences = {audience} if isinstance(audience, str) else set(audience or [])
    subject_scopes = set(str(subject_claims.get("scope", "")).split())
    requested = set(request.scopes)
    for rule in load_json("policies.json")["token_exchange"]:
        if rule["actor"] != actor:
            continue
        if rule["subject_audience"] not in subject_audiences:
            continue
        if rule["subject_scope"] not in subject_scopes:
            continue
        if rule["target_audience"] != request.audience:
            continue
        if requested != {rule["output_scope"]}:
            continue
        return rule
    return {}


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

    now = int(time.time())
    lifetime = request.lifetime_seconds
    subject = request.subject
    actor_chain: dict[str, Any] | None = None

    if request.token_kind == DELEGATED_TOKEN_KIND:
        subject_claims, actor = _derive_delegation(client_id, client, request)
        if not _authorize_delegation(actor, subject_claims, request):
            record(
                "auth-broker",
                "DELEGATION_EXCHANGE_DENIED",
                minter=client_id,
                actor=actor,
                subject=subject_claims.get("sub"),
                audience=request.audience,
                scope=" ".join(request.scopes),
            )
            raise HTTPException(
                403, f"No delegation policy permits {actor} to obtain "
                     f"'{' '.join(request.scopes)}' for '{request.audience}'")
        subject = str(subject_claims["sub"])
        actor_chain = _extend_actor_chain(actor, subject_claims)
        # A delegation cannot outlive the grant it extends. Without this a short
        # human session could be laundered into a longer-lived agent token.
        remaining = int(subject_claims.get("exp", 0)) - now
        if remaining <= 0:
            raise HTTPException(401, "subject_token has expired")
        lifetime = min(lifetime, remaining)
    else:
        if request.subject_token or request.actor_token:
            raise HTTPException(
                400, f"{request.token_kind} starts a delegation chain and takes no "
                     f"subject_token or actor_token")
        if not subject:
            raise HTTPException(400, f"{request.token_kind} requires a subject")

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

    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": subject,
        "aud": request.audience,
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
        "scope": " ".join(request.scopes),
        "token_kind": request.token_kind,
    }
    if actor_chain:
        claims["act"] = actor_chain

    with tracer.start_as_current_span("auth_broker.sign") as span:
        span.set_attribute("auth.minter", client_id)
        span.set_attribute("auth.subject", claims["sub"])
        span.set_attribute("auth.audience", request.audience)
        span.set_attribute("auth.token_kind", request.token_kind)
        span.set_attribute("auth.backend", _backend.name)
        if actor_chain:
            span.set_attribute("auth.actor", actor_chain["sub"])
        token = sign_claims(claims, _backend)

    record(
        "auth-broker",
        "DELEGATION_TOKEN_SIGNED",
        minter=client_id,
        subject=claims["sub"],
        audience=request.audience,
        scope=" ".join(request.scopes),
        token_kind=request.token_kind,
        actor_chain=actor_chain,
        backend=_backend.name,
        kid=_backend.kid,
    )
    return {"token": token, "kid": _backend.kid, "expires_in": lifetime}
