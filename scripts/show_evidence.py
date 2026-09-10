#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"


def main() -> None:
    audit_path = EVIDENCE / "audit.jsonl"
    if not audit_path.exists():
        raise SystemExit("No evidence yet. Run the demo first.")
    events = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    print("AUDIT TIMELINE")
    print(f"{'UTC':<16} {'COMPONENT':<19} EVENT")
    print("-" * 76)
    for event in events:
        clock = event["timestamp"][11:26]
        print(f"{clock:<16} {event['component']:<19} {event['event']}")

    spans = []
    for path in EVIDENCE.glob("*.spans.jsonl"):
        spans.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    counts = Counter(span["service"] for span in spans)
    print("\nOPENTELEMETRY SPAN SUMMARY")
    for service, count in sorted(counts.items()):
        print(f"{service:<38} {count:>4} spans")
    trace_ids = {span["trace_id"] for span in spans}
    print(f"\nTrace IDs captured: {len(trace_ids)}")
    print("Raw evidence: evidence/audit.jsonl and evidence/*.spans.jsonl")


if __name__ == "__main__":
    main()
