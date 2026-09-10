#!/usr/bin/env python3
"""Show each deployed agent's display name and effective Agent Identity.

Reads the ListReasoningEngines JSON on stdin; see find_engine.py for why.
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    engines = payload.get("reasoningEngines", [])
    if not engines:
        print("  No deployed agents in this project/region.")
        return
    for engine in engines:
        identity = engine.get("spec", {}).get("effectiveIdentity")
        print(f"\n  {engine.get('displayName', '-')}")
        print(f"    resource : {engine['name']}")
        if identity:
            print(f"    identity : principal://{identity}")
        else:
            print("    identity : (none - service account backed, no Agent Identity)")


if __name__ == "__main__":
    main()
