#!/usr/bin/env python3
"""Send an A2A message to a deployed A2A agent and print the reply.

  python3 a2a_send.py '<json payload>'         # uses the deployed Procurement Agent
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import requests

A2A_VERSION_HEADER = "A2A-Version"
A2A_VERSION = "1.0"


def send(resource: str, payload: dict | str, location: str = "us-central1") -> dict:
    token = subprocess.run(["gcloud", "auth", "print-access-token"],
                           capture_output=True, text=True, check=True).stdout.strip()
    body = payload if isinstance(payload, str) else json.dumps(payload)
    request_body = json.dumps({"message": {
        "messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": body}],
    }}).encode()
    url = f"https://{location}-aiplatform.googleapis.com/v1beta1/{resource}/a2a/message:send"
    response = requests.post(url, data=request_body, timeout=180, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        A2A_VERSION_HEADER: A2A_VERSION,
    })
    response.raise_for_status()
    return response.json()


def reply_text(envelope: dict) -> str:
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                found.append(node["text"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(envelope)
    return found[0] if found else json.dumps(envelope)


if __name__ == "__main__":
    resource = os.environ.get("A2A_RESOURCE") or sys.argv[2]
    print(reply_text(send(resource, sys.argv[1])))
