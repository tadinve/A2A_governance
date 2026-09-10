#!/usr/bin/env python3
"""Print the reasoningEngine resource name for a display name, or nothing.

Reads the ListReasoningEngines JSON on stdin so curl handles TLS. The system
Python on macOS often has no CA bundle, which made an in-process HTTPS call fail.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    wanted = sys.argv[1]
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    for engine in payload.get("reasoningEngines", []):
        if engine.get("displayName") == wanted:
            print(engine["name"])
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
