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


def load_json(filename: str) -> dict:
    return json.loads((CONFIG_DIR / filename).read_text(encoding="utf-8"))


def ensure_directories() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

