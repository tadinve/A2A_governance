from __future__ import annotations

import base64
from typing import Any

import httpx
import jwt

from .settings import AUTH_BROKER_URL, BROKER_CLIENT_ID, BROKER_CLIENT_SECRET, ISSUER


ALGORITHM = "RS256"

# Cached copy of the Auth Broker's public key. Verification is a hot path and the
# key changes only on rotation, so we fetch lazily and refresh on a signature
# failure rather than calling the broker on every request.
_verification_key: dict[str, Any] = {}


def _fetch_verification_key(force_refresh: bool = False) -> bytes:
    if force_refresh or "pem" not in _verification_key:
        response = httpx.get(f"{AUTH_BROKER_URL}/jwks", timeout=10)
        response.raise_for_status()
        published = response.json()
        _verification_key["pem"] = published["public_key_pem"].encode()
        _verification_key["kid"] = published["kid"]
    return _verification_key["pem"]


def request_token(
    *,
    audience: str,
    scopes: list[str],
    token_kind: str,
    subject: str | None = None,
    subject_token: str | None = None,
    actor_token: str | None = None,
    lifetime_seconds: int = 300,
) -> str:
    """Ask the Auth Broker to mint a token.

    This is the only way to obtain a signed token. There is deliberately no
    local signing path: no caller of this module holds key material, so a
    compromised service can request what the broker's minting policy allows it
    and cannot forge anything else.

    A delegated token is requested by handing over the ``subject_token`` being
    extended, and the ``actor_token`` of the agent doing the extending. The
    broker derives ``sub`` and ``act`` from those itself; there is no parameter
    here for asserting either, because asserting them was the hole.
    """
    credentials = base64.b64encode(
        f"{BROKER_CLIENT_ID}:{BROKER_CLIENT_SECRET}".encode()
    ).decode()
    payload: dict[str, Any] = {
        "audience": audience,
        "scopes": scopes,
        "token_kind": token_kind,
        "lifetime_seconds": lifetime_seconds,
    }
    for name, value in (("subject", subject), ("subject_token", subject_token),
                        ("actor_token", actor_token)):
        if value is not None:
            payload[name] = value
    response = httpx.post(
        f"{AUTH_BROKER_URL}/sign",
        json=payload,
        headers={"Authorization": f"Basic {credentials}"},
        timeout=15,
    )
    if response.status_code != 200:
        raise PermissionError(
            f"Auth Broker refused to mint this token: HTTP {response.status_code} "
            f"{response.text[:200]}"
        )
    return response.json()["token"]


def decode_token(token: str, audience: str | None = None) -> dict[str, Any]:
    """Verify a token against the Auth Broker's published public key."""
    options = {"verify_aud": audience is not None}
    try:
        return jwt.decode(
            token,
            _fetch_verification_key(),
            algorithms=[ALGORITHM],
            audience=audience,
            issuer=ISSUER,
            options=options,
        )
    except jwt.InvalidSignatureError:
        # Either the key rotated under us or the token is forged. Refresh once;
        # if it still fails the exception propagates and the caller denies.
        return jwt.decode(
            token,
            _fetch_verification_key(force_refresh=True),
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
