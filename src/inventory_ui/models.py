"""States and canonical hashing for the inventory/purchasing application.

The state names follow the spec. Two of them exist purely to keep the system
honest about the outside world:

* SUBMISSION_UNKNOWN -- we asked the provider to create an order and never
  learned the outcome. An external write and a local transaction are not
  atomic, so this is a real state, not an error to retry blindly.
* NEEDS_REVIEW -- the draft changed, or its approval expired, after a human
  looked at it. Approval binds to content, so the content moving invalidates it.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


# Run lifecycle
RUN_DRAFTING = "DRAFTING"
RUN_SUFFICIENT = "STOCK_SUFFICIENT"
RUN_AWAITING_APPROVAL = "AWAITING_APPROVAL"
RUN_COMPLETE = "COMPLETE"
RUN_FAILED = "FAILED"
RUN_UNRESOLVED = "UNRESOLVED"

RUN_ACTIVE_STATES = (RUN_DRAFTING, RUN_AWAITING_APPROVAL)

# Purchase-order lifecycle
PO_DRAFTING = "DRAFTING"
PO_AWAITING_APPROVAL = "AWAITING_APPROVAL"
PO_APPROVED = "APPROVED"
PO_SUBMITTING = "SUBMITTING"
PO_PLACED = "PLACED"
PO_REJECTED = "REJECTED"
PO_EXPIRED = "EXPIRED"
PO_FAILED = "FAILED"
PO_NEEDS_REVIEW = "NEEDS_REVIEW"
PO_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"

PO_TERMINAL = (PO_PLACED, PO_REJECTED, PO_EXPIRED, PO_FAILED, PO_SUBMISSION_UNKNOWN)

# Default approval lifetime. Configurable; documented demo default is 30 minutes.
DEFAULT_APPROVAL_TTL_SECONDS = 30 * 60


def content_hash(draft: dict[str, Any]) -> str:
    """Hash every purchase-affecting field of a draft.

    Approval binds to this value. If any of it moves -- vendor, a line, a rate,
    the total, the currency -- the hash changes and the prior approval no longer
    matches, which is what forces a fresh review instead of silently shipping a
    different order than the one a human saw.

    Deliberately excludes ids, timestamps, and state, none of which change what
    is being bought.
    """
    material = {
        "organization_id": str(draft.get("organization_id", "")),
        "vendor_id": str(draft.get("vendor_id", "")),
        "currency": str(draft.get("currency", "")),
        "reference_number": str(draft.get("reference_number", "")),
        "lines": [
            {
                "item_id": str(line.get("item_id", "")),
                "sku": str(line.get("sku", "")),
                "quantity": float(line.get("quantity") or 0),
                "rate": float(line.get("rate") or 0),
            }
            for line in draft.get("lines", [])
        ],
        "total": round(float(draft.get("total") or 0), 4),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
