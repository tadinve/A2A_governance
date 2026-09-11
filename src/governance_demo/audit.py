from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import EVIDENCE_DIR, ensure_directories


# Never let an audit line carry credential-shaped values, wherever it is written.
_REDACT_KEYS = {"token", "access_token", "authorization", "client_secret",
                "refresh_token", "password", "url", "endpoint", "api_key"}


def _safe(details: dict[str, Any]) -> dict[str, Any]:
    return {k: ("<redacted>" if k.lower() in _REDACT_KEYS else v)
            for k, v in details.items()}


def record(component: str, event: str, **details: Any) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "event": event,
        **_safe(details),
    }

    # stdout first: on Cloud Run this is the only durable destination, since the
    # container filesystem is ephemeral. Structured so Cloud Logging parses it.
    try:
        print(json.dumps({"severity": "INFO", "audit": entry}, sort_keys=True),
              file=sys.stdout, flush=True)
    except Exception:
        pass

    # The local demo reads evidence/audit.jsonl, so keep writing it when we can.
    try:
        ensure_directories()
        path = Path(EVIDENCE_DIR) / "audit.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass

