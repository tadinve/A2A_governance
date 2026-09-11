#!/usr/bin/env python3
"""Prepare the demo's working directories.

This script used to generate an RSA keypair into runtime/*.pem, which every
service then read in order to sign tokens. It no longer does. The delegation
signing key now lives in exactly one place -- inside the Auth Broker, backed by
Cloud KMS in a deployment and by an in-process key locally -- so there is no key
file to create, distribute, or leak.

Any key files left over from the previous design are deleted here, because a
stale private key on disk is exactly the artifact this change exists to remove.
"""
from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
EVIDENCE = ROOT / "evidence"


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize demo directories and evidence files")
    parser.add_argument("--reset-evidence", action="store_true")
    args = parser.parse_args()

    RUNTIME.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)

    stale = sorted(RUNTIME.glob("*.pem"))
    for path in stale:
        path.unlink()
    if stale:
        print(f"Removed {len(stale)} stale key file(s) from the pre-KMS design: "
              f"{', '.join(p.name for p in stale)}")
    print("No signing key is created on disk; the Auth Broker holds the only key.")

    if args.reset_evidence:
        for path in EVIDENCE.glob("*.jsonl"):
            path.unlink()
        print("Reset prior JSONL evidence.")


if __name__ == "__main__":
    main()
