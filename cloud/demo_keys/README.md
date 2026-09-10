# Local development keys only

**Not used by any deployment, and never committed.**

The shared delegation issuer key lives in **Secret Manager**
(`a2a-demo-delegation-issuer`). Deployed agents fetch it at runtime with their
own Agent Identity; nothing is packaged into the agent bundle. See
`cloud/setup_issuer_secret.py`.

This directory exists only so the agent packages can be exercised offline,
without Google Cloud. `cloud/ensure_demo_keys.py` writes a throwaway keypair
here, and `governance.py` falls back to it when `ISSUER_SECRET_NAME` is unset.

If you are deploying, you do not need this directory at all.
