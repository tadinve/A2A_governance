#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
EVIDENCE = ROOT / "evidence"


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize local demo keys and evidence files")
    parser.add_argument("--reset-evidence", action="store_true")
    args = parser.parse_args()

    RUNTIME.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    private_path = RUNTIME / "issuer_private.pem"
    public_path = RUNTIME / "issuer_public.pem"
    if not private_path.exists() or not public_path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        public_path.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        private_path.chmod(0o600)
        print("Created the demo signing key pair.")
    else:
        print("Using the existing demo signing key pair.")

    if args.reset_evidence:
        for path in EVIDENCE.glob("*.jsonl"):
            path.unlink()
        print("Reset prior JSONL evidence.")


if __name__ == "__main__":
    main()
