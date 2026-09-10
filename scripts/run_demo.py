#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

import httpx


BASE = "http://127.0.0.1"


def heading(number: int, title: str) -> None:
    print(f"\n[{number}] {title}\n" + "-" * (len(title) + 4))


def main() -> int:
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            heading(1, "Discover the two agent workload identities")
            for port in (8103, 8104):
                identity = client.get(f"{BASE}:{port}/identity").json()
                print(f"{identity['display_name']}: {identity['identity']}")

            heading(2, "Authenticate the human to Inventory Agent")
            login = client.post(f"{BASE}:8101/login", data={"user_id": "demo-user"})
            login.raise_for_status()
            user_token = login.json()["access_token"]
            print("Audience-bound human token issued; raw credential hidden.")

            heading(3, "Inventory Agent reads Zoho through the read-only MCP connector")
            result = client.post(
                f"{BASE}:8103/ask",
                json={"text": "Check CK-GPU-42 and replenish it if stock is low."},
                headers={"Authorization": f"Bearer {user_token}"},
            )
            result.raise_for_status()
            body = result.json()
            print(body["answer"])
            po = body["purchase_order"]
            print("\nAgent identity delegation into the Inventory MCP:")
            print(json.dumps(body["governance_evidence"]["inventory_delegation"], indent=2))
            print("\nNested A2A delegation received by Procurement Agent:")
            print(json.dumps(body["governance_evidence"]["a2a_delegation"]["act"], indent=2))
            print(f"\nDraft hash: {po['draft_hash']}")
            print("The Procurement Agent has no approval tool.")

            heading(4, "Human signs into the Zoho UI and reviews the exact draft")
            human = httpx.Client(timeout=30, trust_env=False)
            ui_login = human.post(f"{BASE}:8107/ui/login", data={"email": "approver@example.com", "password": "demo-approval-password"})
            ui_login.raise_for_status()
            review = human.get(f"{BASE}:8107/ui/purchaseorders/{po['purchaseorder_id']}")
            review.raise_for_status()
            reviewed = review.json()["purchaseorder"]
            print(f"Approver reviewed {reviewed['purchaseorder_id']}: {reviewed['line_items'][0]['quantity']} units, total ${reviewed['total']:.2f}.")

            heading(5, "Human approves; approval is bound to the draft hash")
            approved = human.post(
                f"{BASE}:8107/ui/purchaseorders/{po['purchaseorder_id']}/approve",
                json={"expected_draft_hash": reviewed["draft_hash"]},
            )
            approved.raise_for_status()
            print(f"Approved by {approved.json()['purchaseorder']['approved_by']}.")
            human.close()

            heading(6, "Inventory Agent asks Procurement Agent for final status")
            status = client.get(f"{BASE}:8103/orders/{po['purchaseorder_id']}", headers={"Authorization": f"Bearer {user_token}"})
            status.raise_for_status()
            final_po = status.json()
            print(f"{final_po['purchaseorder_id']} status: {final_po['approval_status']}; total ${final_po['total']:.2f}.")

            heading(7, "Prove critical denials")
            direct = client.post(f"{BASE}:8104/a2a", json={"jsonrpc": "2.0"})
            no_session = client.post(f"{BASE}:8107/ui/purchaseorders/{po['purchaseorder_id']}/approve", json={"expected_draft_hash": po["draft_hash"]})
            bad_tool = client.post(f"{BASE}:8105/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "approve_purchase_order"}})
            print(f"Direct Procurement Agent call: HTTP {direct.status_code}")
            print(f"Approval without human UI session: HTTP {no_session.status_code}")
            print(f"Direct/unknown MCP approval tool: HTTP {bad_tool.status_code}")
            if (direct.status_code, no_session.status_code, bad_tool.status_code) != (403, 401, 403):
                raise RuntimeError("A required denial did not occur")

            heading(8, "Result")
            print("PASS: Zoho OAuth, MCP, A2A, human approval, audit, and denial paths all worked.")
            print("Next: bash scripts/show_evidence.sh")
            return 0
    except httpx.ConnectError:
        print("Services are not running. Run: bash scripts/start_local.sh", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
