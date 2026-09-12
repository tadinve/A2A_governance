"""Durable state for runs, drafts, approvals, and submission attempts.

SQLite rather than Firestore for the local slice: it is genuinely durable across
process restarts, needs no cloud account, and keeps every state transition in a
real transaction. Every query goes through this module, so the Cloud Run pass can
swap the backend without the application layer noticing.

What must never be the source of truth, per the spec, and is not: process
memory, one long-running request, or anything in the browser.

The transition helpers all use BEGIN IMMEDIATE. That is what makes concurrent
approve/reject, double-clicks, and task redelivery resolve to exactly one
winner instead of racing.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import (
    PO_AWAITING_APPROVAL,
    PO_TERMINAL,
    RUN_ACTIVE_STATES,
)


DB_PATH = Path(os.getenv("INVENTORY_UI_DB", "runtime/inventory_ui.sqlite3"))
_local = threading.local()


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    subject      TEXT NOT NULL,
    org_id       TEXT NOT NULL,
    scope_sku    TEXT NOT NULL,
    state        TEXT NOT NULL,
    mode         TEXT NOT NULL,
    stock_json   TEXT,
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    version         INTEGER NOT NULL,
    state           TEXT NOT NULL,
    org_id          TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    provider_po_id  TEXT,
    provider_number TEXT,
    provider_status TEXT,
    error           TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
-- Approvals are insert-only. There is no update path anywhere in this module.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    draft_id      TEXT NOT NULL,
    draft_version INTEGER NOT NULL,
    org_id        TEXT NOT NULL,
    subject       TEXT NOT NULL,
    decision      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_per_draft
    ON approvals (draft_id, draft_version);
CREATE TABLE IF NOT EXISTS submissions (
    submission_id   TEXT PRIMARY KEY,
    draft_id        TEXT NOT NULL,
    draft_version   INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    state           TEXT NOT NULL,
    provider_po_id  TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
-- One submission per (draft, version, operation). This is the guard against
-- double-clicks, task redelivery, and agent retries creating two orders.
CREATE UNIQUE INDEX IF NOT EXISTS submissions_idempotent
    ON submissions (idempotency_key);
CREATE TABLE IF NOT EXISTS activity (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL,
    at       REAL NOT NULL,
    step     TEXT NOT NULL,
    detail   TEXT
);
"""


def connection() -> sqlite3.Connection:
    """One connection per thread, WAL so readers never block the writer."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _local.conn = conn
    return conn


class Conflict(Exception):
    """A transition lost a race, or was attempted from the wrong state."""


def _now() -> float:
    return time.time()


def log(run_id: str, step: str, detail: str = "") -> None:
    connection().execute(
        "INSERT INTO activity (run_id, at, step, detail) VALUES (?,?,?,?)",
        (run_id, _now(), step, detail))


# --- runs -------------------------------------------------------------------

def active_run_for(subject: str, scope_sku: str) -> dict[str, Any] | None:
    """The caller's one active run for this scope, if any.

    Repeated clicks return this rather than starting a second run.
    """
    placeholders = ",".join("?" for _ in RUN_ACTIVE_STATES)
    row = connection().execute(
        f"SELECT * FROM runs WHERE subject=? AND scope_sku=? "
        f"AND state IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
        (subject, scope_sku, *RUN_ACTIVE_STATES)).fetchone()
    return dict(row) if row else None


def create_run(subject: str, org_id: str, scope_sku: str, state: str, mode: str) -> str:
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    now = _now()
    connection().execute(
        "INSERT INTO runs (run_id, subject, org_id, scope_sku, state, mode, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, subject, org_id, scope_sku, state, mode, now, now))
    return run_id


def set_run_state(run_id: str, state: str, *, stock: dict | None = None,
                  error: str | None = None) -> None:
    conn = connection()
    fields, values = ["state=?", "updated_at=?"], [state, _now()]
    if stock is not None:
        fields.append("stock_json=?")
        values.append(json.dumps(stock))
    if error is not None:
        fields.append("error=?")
        values.append(error)
    values.append(run_id)
    conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id=?", values)


def get_run(run_id: str) -> dict[str, Any] | None:
    row = connection().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def run_activity(run_id: str) -> list[dict[str, Any]]:
    rows = connection().execute(
        "SELECT at, step, detail FROM activity WHERE run_id=? ORDER BY id", (run_id,))
    return [dict(r) for r in rows]


# --- drafts -----------------------------------------------------------------

def create_draft(run_id: str, org_id: str, payload: dict, content_hash_value: str) -> str:
    draft_id = f"po_{uuid.uuid4().hex[:16]}"
    now = _now()
    connection().execute(
        "INSERT INTO drafts (draft_id, run_id, version, state, org_id, payload_json, "
        "content_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (draft_id, run_id, 1, PO_AWAITING_APPROVAL, org_id,
         json.dumps(payload), content_hash_value, now, now))
    return draft_id


def get_draft(draft_id: str) -> dict[str, Any] | None:
    row = connection().execute(
        "SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    return dict(row) if row else None


def drafts_for_run(run_id: str) -> list[dict[str, Any]]:
    rows = connection().execute(
        "SELECT * FROM drafts WHERE run_id=? ORDER BY created_at", (run_id,))
    return [dict(r) for r in rows]


def transition_draft(draft_id: str, *, expected_state: str, new_state: str,
                     expected_version: int | None = None,
                     provider_po_id: str | None = None,
                     provider_number: str | None = None,
                     provider_status: str | None = None,
                     error: str | None = None) -> dict[str, Any]:
    """Move a draft between states, atomically and exactly once.

    BEGIN IMMEDIATE takes the write lock before reading, so two concurrent
    approve/reject requests cannot both observe AWAITING_APPROVAL and both win.
    The loser raises Conflict.
    """
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None:
            raise Conflict(f"{draft_id} does not exist")
        current = dict(row)
        if current["state"] != expected_state:
            raise Conflict(
                f"{draft_id} is {current['state']}, expected {expected_state}")
        if expected_version is not None and current["version"] != expected_version:
            raise Conflict(
                f"{draft_id} is at version {current['version']}, "
                f"caller expected {expected_version}")

        fields, values = ["state=?", "updated_at=?"], [new_state, _now()]
        for column, value in (("provider_po_id", provider_po_id),
                              ("provider_number", provider_number),
                              ("provider_status", provider_status),
                              ("error", error)):
            if value is not None:
                fields.append(f"{column}=?")
                values.append(value)
        values.append(draft_id)
        conn.execute(f"UPDATE drafts SET {', '.join(fields)} WHERE draft_id=?", values)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_draft(draft_id)  # type: ignore[return-value]


# --- approvals (insert only) ------------------------------------------------

def record_approval(*, run_id: str, draft_id: str, draft_version: int, org_id: str,
                    subject: str, decision: str, content_hash_value: str,
                    snapshot: dict, ttl_seconds: int) -> str:
    """Write an immutable approval record.

    The unique index on (draft_id, draft_version) is what makes a second
    approval of the same draft version impossible rather than merely unlikely.
    """
    approval_id = f"apr_{uuid.uuid4().hex[:16]}"
    now = _now()
    try:
        connection().execute(
            "INSERT INTO approvals (approval_id, run_id, draft_id, draft_version, "
            "org_id, subject, decision, content_hash, snapshot_json, created_at, "
            "expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (approval_id, run_id, draft_id, draft_version, org_id, subject, decision,
             content_hash_value, json.dumps(snapshot), now, now + ttl_seconds))
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"{draft_id} v{draft_version} already has a decision") from exc
    return approval_id


def approval_for(draft_id: str, draft_version: int) -> dict[str, Any] | None:
    row = connection().execute(
        "SELECT * FROM approvals WHERE draft_id=? AND draft_version=?",
        (draft_id, draft_version)).fetchone()
    return dict(row) if row else None


# --- submissions ------------------------------------------------------------

def claim_submission(draft_id: str, draft_version: int, idempotency_key: str) -> tuple[str, bool]:
    """Claim the right to submit this exact draft version exactly once.

    Returns (submission_id, is_new). A caller that gets is_new=False must not
    submit: someone else already holds the claim, and the provider write may
    already be in flight.
    """
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM submissions WHERE idempotency_key=?",
                           (idempotency_key,)).fetchone()
        if row is not None:
            conn.execute("COMMIT")
            return dict(row)["submission_id"], False
        submission_id = f"sub_{uuid.uuid4().hex[:16]}"
        now = _now()
        conn.execute(
            "INSERT INTO submissions (submission_id, draft_id, draft_version, "
            "idempotency_key, state, attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (submission_id, draft_id, draft_version, idempotency_key,
             "CLAIMED", 1, now, now))
        conn.execute("COMMIT")
        return submission_id, True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def finish_submission(submission_id: str, state: str, *,
                      provider_po_id: str | None = None,
                      error: str | None = None) -> None:
    connection().execute(
        "UPDATE submissions SET state=?, provider_po_id=?, error=?, updated_at=? "
        "WHERE submission_id=?",
        (state, provider_po_id, error, _now(), submission_id))


def submission_for(idempotency_key: str) -> dict[str, Any] | None:
    row = connection().execute(
        "SELECT * FROM submissions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    return dict(row) if row else None
