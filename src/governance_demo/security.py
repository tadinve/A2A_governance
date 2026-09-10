from __future__ import annotations

import time
from typing import Any

import jwt

from .settings import ISSUER, RUNTIME_DIR


ALGORITHM = "RS256"


def _private_key() -> bytes:
    return (RUNTIME_DIR / "issuer_private.pem").read_bytes()


def _public_key() -> bytes:
    return (RUNTIME_DIR / "issuer_public.pem").read_bytes()


def issue_token(
    *,
    subject: str,
    audience: str,
    scopes: list[str],
    token_kind: str,
    actor_chain: dict[str, Any] | None = None,
    lifetime_seconds: int = 300,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": subject,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + lifetime_seconds,
        "scope": " ".join(scopes),
        "token_kind": token_kind,
    }
    if actor_chain:
        claims["act"] = actor_chain
    return jwt.encode(claims, _private_key(), algorithm=ALGORITHM)


def decode_token(token: str, audience: str | None = None) -> dict[str, Any]:
    options = {"verify_aud": audience is not None}
    return jwt.decode(
        token,
        _public_key(),
        algorithms=[ALGORITHM],
        audience=audience,
        issuer=ISSUER,
        options=options,
    )


def bearer_token(value: str | None) -> str:
    if not value:
        raise ValueError("Missing Authorization header")
    scheme, separator, token = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        raise ValueError("Authorization must use the Bearer scheme")
    return token


def scopes(claims: dict[str, Any]) -> set[str]:
    return set(str(claims.get("scope", "")).split())


def current_actor(claims: dict[str, Any]) -> str:
    actor = claims.get("act") or {}
    subject = actor.get("sub")
    if not subject:
        raise ValueError("Delegated token has no authenticated actor")
    return str(subject)


def extend_actor_chain(actor: str, subject_claims: dict[str, Any]) -> dict[str, Any]:
    chain: dict[str, Any] = {"sub": actor}
    if subject_claims.get("act"):
        chain["act"] = subject_claims["act"]
    return chain


def public_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """Return claims suitable for a classroom display; never return a raw token."""
    return {
        key: claims.get(key)
        for key in ("iss", "sub", "aud", "scope", "token_kind", "act", "iat", "exp")
        if key in claims
    }

