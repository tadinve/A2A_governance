"""Server-side session authentication and approver authorization.

Two rules the spec is emphatic about, and the reason this is a module rather
than a decorator inline:

* actor identity is derived from verified authentication, never from a request
  body, an email field, or a client-side role claim;
* approver permission is checked on the server for every mutation.

Locally this is a signed session cookie over an approver allowlist. In the Cloud
Run pass the same two functions resolve identity from a validated IAP assertion
instead; nothing else in the application changes, because nothing else is
allowed to ask who the caller is.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request


SESSION_COOKIE = "inventory_ui_session"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))

# Demo credentials. Real deployments use IAP or organization sign-in; this
# exists so the local slice can enforce a real server-side identity rather than
# pretending authentication happened.
_USERS = {
    "approver@example.com": {
        "password": os.getenv("DEMO_APPROVER_PASSWORD", "demo-approval-password"),
        "roles": ["inventory.read", "purchaseorder.approver"],
    },
    "viewer@example.com": {
        "password": os.getenv("DEMO_VIEWER_PASSWORD", "demo-viewer-password"),
        "roles": ["inventory.read"],
    },
}

def _session_secret() -> bytes:
    """A signing secret that survives a restart.

    Generating a fresh one per process silently invalidated every session on
    restart, which defeats the requirement that a pending approval survive a
    server restart: the run stayed in the database but nobody could authenticate
    back to it. In a deployment this comes from Secret Manager via
    SESSION_SECRET; locally it is persisted once under runtime/.
    """
    configured = os.getenv("SESSION_SECRET", "").strip()
    if configured:
        return configured.encode()
    path = Path(os.getenv("SESSION_SECRET_FILE", "runtime/session_secret"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


_SECRET = _session_secret()


def _sign(value: str) -> str:
    return hmac.new(_SECRET, value.encode(), hashlib.sha256).hexdigest()


def issue_session(email: str) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{email}|{expires}"
    return f"{payload}|{_sign(payload)}"


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    user = _USERS.get(email.strip().lower())
    if not user or not secrets.compare_digest(user["password"], password):
        return None
    return {"subject": email.strip().lower(), "roles": user["roles"]}


def current_user(request: Request) -> dict[str, Any]:
    """Resolve the caller from their session cookie, or refuse.

    Never consults the request body. A caller cannot assert who they are.
    """
    raw = request.cookies.get(SESSION_COOKIE, "")
    parts = raw.split("|")
    if len(parts) != 3:
        raise HTTPException(401, "Sign in required")
    email, expires, signature = parts
    if not hmac.compare_digest(_sign(f"{email}|{expires}"), signature):
        raise HTTPException(401, "Session signature invalid")
    if int(expires) <= int(time.time()):
        raise HTTPException(401, "Session expired")
    user = _USERS.get(email)
    if not user:
        raise HTTPException(401, "Unknown subject")
    return {"subject": email, "roles": user["roles"]}


def require_approver(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if "purchaseorder.approver" not in user["roles"]:
        raise HTTPException(403, "Caller is not a purchase-order approver")
    return user


def require_same_origin(request: Request) -> None:
    """Reject cross-site cookie-authenticated mutations.

    The session is a cookie, so a state-changing POST needs an origin check or
    any page on the internet could make the browser send it.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return  # same-origin fetches from the page itself omit Origin
    host = request.headers.get("host", "")
    if origin.split("://")[-1] != host:
        raise HTTPException(403, "Cross-origin request refused")
