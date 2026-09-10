#!/usr/bin/env python3
"""Generate the shared demo signing key and stage it into each agent package.

Inventory Agent and Procurement Agent are separate deployments in separate
processes, so they need the *same* issuer key to verify each other's delegated
tokens. That key is generated here on the operator's machine and is never
committed: this repository is public, and publishing a private key in material
about credential hygiene would be a poor example to set.

Mirrors what scripts/init_demo.py does for the local demo's runtime/*.pem.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

CLOUD_ROOT = Path(__file__).resolve().parent
KEY_DIR = CLOUD_ROOT / "demo_keys"
AGENT_PACKAGES = ("inventory_agent", "procurement_agent", "procurement_a2a")


def ensure_keypair() -> tuple[Path, Path]:
    private_path = KEY_DIR / "issuer_private.pem"
    public_path = KEY_DIR / "issuer_public.pem"
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    if private_path.exists() and public_path.exists():
        print("    using the existing demo signing key")
        return private_path, public_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    public_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    private_path.chmod(0o600)
    print("    generated a new demo signing key")
    return private_path, public_path


def stage() -> None:
    private_path, public_path = ensure_keypair()
    readme = KEY_DIR / "README.md"
    for package in AGENT_PACKAGES:
        target = CLOUD_ROOT / package / "demo_keys"
        if not (CLOUD_ROOT / package).is_dir():
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(private_path, target / private_path.name)
        shutil.copy2(public_path, target / public_path.name)
        if readme.exists():
            shutil.copy2(readme, target / readme.name)
        print(f"    staged into {package}/demo_keys")


if __name__ == "__main__":
    stage()
