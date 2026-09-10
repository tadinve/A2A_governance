# Demo signing key

**Not committed.** `issuer_private.pem` and `issuer_public.pem` are generated on
your machine by `cloud/ensure_demo_keys.py`, which the deploy scripts run
automatically, and are staged into each agent package at deploy time.

Inventory Agent and Procurement Agent are separate Agent Runtime deployments in
separate processes. They need the *same* issuer key to verify each other's
delegated tokens; a per-process key would make cross-agent verification
impossible.

This key signs the demo's own delegation JWTs. It is not a Google credential and
grants no access to anything real. It is still not committed, because this
repository is public and publishing a private key inside a demo about credential
hygiene would set the wrong example. Production delegation would use a managed
issuer with rotation, and the private key would live in Secret Manager.
