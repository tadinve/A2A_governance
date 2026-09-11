from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
RUNTIME_DIR = ROOT / "runtime"
EVIDENCE_DIR = ROOT / "evidence"

ISSUER = "https://identity.demo.local"
IDP_URL = os.getenv("IDP_URL", "http://127.0.0.1:8101")
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://127.0.0.1:8100")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8102")
ZOHO_URL = os.getenv("ZOHO_URL", "http://127.0.0.1:8107")
AUTH_BROKER_URL = os.getenv("AUTH_BROKER_URL", "http://127.0.0.1:8108")

# How the Identity Broker authenticates to the Auth Broker. A demo credential:
# in a deployment this is the service's own Agent Identity, and the Auth Broker
# authorizes it through IAM rather than a shared secret.
BROKER_CLIENT_ID = os.getenv("BROKER_CLIENT_ID", "identity-broker")
BROKER_CLIENT_SECRET = os.getenv("BROKER_CLIENT_SECRET", "identity-broker-demo-secret")


def load_json(filename: str) -> dict:
    return json.loads((CONFIG_DIR / filename).read_text(encoding="utf-8"))


def ensure_directories() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
