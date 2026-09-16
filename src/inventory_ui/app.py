"""Inventory & Purchasing: one page, one backend, same app locally and on Cloud Run.

Endpoint semantics follow the spec's contract. The client never sends an
authoritative purchase order; it sends the draft version it reviewed and an
idempotency key, and the server owns every id and state transition.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import auth, executor, security_plane, store
from .telemetry import instrument_ui
from .models import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    PO_APPROVED,
    PO_AWAITING_APPROVAL,
    PO_FAILED,
    PO_NEEDS_REVIEW,
    PO_PLACED,
    PO_REJECTED,
    PO_SUBMISSION_UNKNOWN,
    PO_SUBMITTING,
    RUN_AWAITING_APPROVAL,
    RUN_COMPLETE,
    RUN_DRAFTING,
    content_hash,
)


app = FastAPI(title="Inventory & Purchasing", version="1.0")
instrument_ui(app)
STATIC = Path(__file__).resolve().parent / "static"
APPROVAL_TTL = int(os.getenv("APPROVAL_TTL_SECONDS", str(DEFAULT_APPROVAL_TTL_SECONDS)))
INTERNAL_TOKEN = os.getenv("INTERNAL_TASK_TOKEN", "local-task-token")


def _dispatch(run_id: str) -> None:
    """Hand the run to background processing.

    State is persisted before this returns, so losing the worker loses no
    decision -- the run is recoverable from the database. On Cloud Run this
    becomes an authenticated Cloud Tasks enqueue against /internal/process;
    the durable contract is identical.
    """
    threading.Thread(target=_run_task, args=(run_id,), daemon=True).start()


def _run_task(run_id: str) -> None:
    try:
        executor.process_run(run_id)
    except Exception as exc:  # noqa: BLE001 - a failed run must be visible, not silent
        store.set_run_state(run_id, "FAILED", error=f"{type(exc).__name__}: {exc}")
        store.log(run_id, "run_failed", str(exc)[:300])


@app.get("/healthz")
def healthz() -> dict:
    """Minimal liveness. Deliberately reveals no configuration or secrets."""
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# --- session ----------------------------------------------------------------

@app.post("/api/session")
def sign_in(response: Response, email: str = Form(...), password: str = Form(...)) -> dict:
    user = auth.authenticate(email, password)
    if not user:
        raise HTTPException(401, "Invalid credentials")
    response.set_cookie(auth.SESSION_COOKIE, auth.issue_session(user["subject"]),
                        httponly=True, samesite="strict", path="/")
    return {"subject": user["subject"], "roles": user["roles"]}


@app.delete("/api/session")
def sign_out(response: Response) -> dict:
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"signed_out": True}


@app.get("/api/session")
def whoami(request: Request) -> dict:
    user = auth.current_user(request)
    return {**user, "mode": executor.EXECUTION_MODE, "scope_sku": executor.DEMO_SKU,
            "approval_ttl_seconds": APPROVAL_TTL}


# --- inventory runs ---------------------------------------------------------

@app.post("/api/inventory-checks")
def start_check(request: Request) -> dict:
    auth.require_same_origin(request)
    user = auth.current_user(request)
    sku = executor.DEMO_SKU

    existing = store.active_run_for(user["subject"], sku)
    if existing:
        # Repeated clicks return the active run instead of starting another.
        return {"run_id": existing["run_id"], "state": existing["state"], "existing": True}

    try:
        org = executor._governance().organization_id()
    except Exception as exc:
        raise HTTPException(502, f"Zoho unavailable: {type(exc).__name__}") from None

    run_id = store.create_run(user["subject"], org, sku, RUN_DRAFTING,
                              executor.EXECUTION_MODE)
    store.log(run_id, "check_started", f"sku={sku} mode={executor.EXECUTION_MODE}")
    _dispatch(run_id)
    return {"run_id": run_id, "state": RUN_DRAFTING, "existing": False}


@app.get("/api/inventory-checks/active")
def active_check(request: Request) -> dict:
    user = auth.current_user(request)
    existing = store.active_run_for(user["subject"], executor.DEMO_SKU)
    return {"run_id": existing["run_id"] if existing else None}


def _visible_run(request: Request, run_id: str) -> dict[str, Any]:
    """Load a run the caller is actually entitled to see.

    Ownership and organization are checked on every read, so another signed-in
    user cannot read someone else's run by guessing an id.
    """
    user = auth.current_user(request)
    run = store.get_run(run_id)
    if not run or run["subject"] != user["subject"]:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/runs/{run_id}")
def read_run(request: Request, run_id: str) -> dict:
    run = _visible_run(request, run_id)
    drafts = []
    for row in store.drafts_for_run(run_id):
        payload = json.loads(row["payload_json"])
        approval = store.approval_for(row["draft_id"], row["version"])
        drafts.append({
            "draft_id": row["draft_id"],
            "version": row["version"],
            "state": row["state"],
            "payload": payload,
            "content_hash": row["content_hash"],
            "provider_po_id": row["provider_po_id"],
            "provider_number": row["provider_number"],
            "provider_status": row["provider_status"],
            "error": row["error"],
            "approval": None if not approval else {
                "approval_id": approval["approval_id"],
                "decision": approval["decision"],
                "subject": approval["subject"],
                "created_at": approval["created_at"],
                "expires_at": approval["expires_at"],
                "expired": approval["expires_at"] <= time.time(),
            },
        })
    return {
        "run_id": run["run_id"],
        "state": run["state"],
        "mode": run["mode"],
        "scope_sku": run["scope_sku"],
        "organization_id": run["org_id"],
        "stock": json.loads(run["stock_json"]) if run["stock_json"] else None,
        "error": run["error"],
        "drafts": drafts,
        "activity": store.run_activity(run_id),
    }


# --- approval and rejection -------------------------------------------------

class Decision(BaseModel):
    expected_version: int
    expected_content_hash: str
    idempotency_key: str | None = None


def _load_for_decision(request: Request, draft_id: str, body: Decision):
    user = auth.require_approver(request)
    auth.require_same_origin(request)
    draft = store.get_draft(draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    run = store.get_run(draft["run_id"])
    if not run or run["subject"] != user["subject"]:
        raise HTTPException(404, "Draft not found")
    if draft["version"] != body.expected_version:
        raise HTTPException(409, "Draft version moved; refresh and review again")
    # Bind the decision to the exact content that was displayed.
    if draft["content_hash"] != body.expected_content_hash:
        raise HTTPException(409, "Draft content changed since review")
    payload = json.loads(draft["payload_json"])
    if content_hash(payload) != draft["content_hash"]:
        store.transition_draft(draft_id, expected_state=draft["state"],
                               new_state=PO_NEEDS_REVIEW)
        raise HTTPException(409, "Stored draft failed its own integrity check")
    return user, draft, run, payload


@app.post("/api/purchase-orders/{draft_id}/approve")
def approve(request: Request, draft_id: str, body: Decision) -> dict:
    user, draft, run, payload = _load_for_decision(request, draft_id, body)

    try:
        approval_id = store.record_approval(
            run_id=run["run_id"], draft_id=draft_id, draft_version=draft["version"],
            org_id=draft["org_id"], subject=user["subject"], decision="approved",
            content_hash_value=draft["content_hash"], snapshot=payload,
            ttl_seconds=APPROVAL_TTL)
        store.transition_draft(draft_id, expected_state=PO_AWAITING_APPROVAL,
                               new_state=PO_APPROVED,
                               expected_version=body.expected_version)
    except store.Conflict as exc:
        raise HTTPException(409, str(exc)) from None

    store.log(run["run_id"], "approved",
              f"{draft_id} by {user['subject']} approval={approval_id}")

    approval = store.approval_for(draft_id, draft["version"])
    if not approval or approval["expires_at"] <= time.time():
        store.transition_draft(draft_id, expected_state=PO_APPROVED,
                               new_state=PO_NEEDS_REVIEW)
        raise HTTPException(409, "Approval expired before submission")

    try:
        store.transition_draft(draft_id, expected_state=PO_APPROVED,
                               new_state=PO_SUBMITTING)
        store.log(run["run_id"], "submitting", draft_id)
        current = store.get_draft(draft_id)
        purchase_order = executor.submit_approved_draft(current, dict(approval))
    except store.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:
        submission = store.submission_for(f"submit:{draft_id}:v{draft['version']}") or {}
        unknown = submission.get("state") == "UNKNOWN"
        store.transition_draft(
            draft_id, expected_state=PO_SUBMITTING,
            new_state=PO_SUBMISSION_UNKNOWN if unknown else PO_FAILED,
            error=f"{type(exc).__name__}: {exc}"[:400])
        store.log(run["run_id"],
                  "submission_unknown" if unknown else "submission_failed",
                  str(exc)[:300])
        raise HTTPException(502, f"Submission {'outcome unknown' if unknown else 'failed'}: "
                                 f"{type(exc).__name__}") from None

    final = store.transition_draft(
        draft_id, expected_state=PO_SUBMITTING, new_state=PO_PLACED,
        provider_po_id=purchase_order.get("purchaseorder_id"),
        provider_number=purchase_order.get("purchaseorder_number"),
        provider_status=purchase_order.get("status"))
    store.set_run_state(run["run_id"], RUN_COMPLETE)
    store.log(run["run_id"], "order_created",
              f"{final['provider_number']} status={final['provider_status']}")
    return {"draft_id": draft_id, "state": final["state"],
            "provider_number": final["provider_number"],
            "provider_status": final["provider_status"],
            "approval_id": approval_id}


@app.post("/api/purchase-orders/{draft_id}/reject")
def reject(request: Request, draft_id: str, body: Decision) -> dict:
    user, draft, run, payload = _load_for_decision(request, draft_id, body)
    try:
        approval_id = store.record_approval(
            run_id=run["run_id"], draft_id=draft_id, draft_version=draft["version"],
            org_id=draft["org_id"], subject=user["subject"], decision="rejected",
            content_hash_value=draft["content_hash"], snapshot=payload,
            ttl_seconds=APPROVAL_TTL)
        store.transition_draft(draft_id, expected_state=PO_AWAITING_APPROVAL,
                               new_state=PO_REJECTED,
                               expected_version=body.expected_version)
    except store.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    store.set_run_state(run["run_id"], RUN_COMPLETE)
    store.log(run["run_id"], "rejected", f"{draft_id} by {user['subject']}")
    return {"draft_id": draft_id, "state": PO_REJECTED, "approval_id": approval_id}


@app.get("/api/security-plane")
def security_plane_view(request: Request) -> dict:
    """Who may do what, read live from the project's IAM policies.

    Requires a session: it names principals and their grants, which is not
    something to publish anonymously even though it contains no secrets.
    """
    auth.current_user(request)
    return security_plane.collect()


# --- task-only processing ---------------------------------------------------

@app.post("/internal/process")
def internal_process(request: Request, run_id: str) -> JSONResponse:
    """Task-dispatched work. Not for browsers or ordinary users.

    Task authorization is deliberately separate from human approval
    authorization: holding this token lets a worker advance a run, never
    approve a purchase order.
    """
    presented = request.headers.get("x-task-token", "")
    if presented != INTERNAL_TOKEN:
        raise HTTPException(403, "Task authorization required")
    executor.process_run(run_id)
    return JSONResponse({"processed": run_id})
