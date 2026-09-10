from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import EVIDENCE_DIR, ensure_directories


def record(component: str, event: str, **details: Any) -> None:
    ensure_directories()
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "event": event,
        **details,
    }
    path = Path(EVIDENCE_DIR) / "audit.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")

