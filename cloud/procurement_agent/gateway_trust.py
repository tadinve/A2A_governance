"""Trust the Agent Gateway's TLS inspection CA, without weakening anything else.

An Agent-to-Anywhere gateway is a TLS-terminating proxy: it opens the agent's
outbound TLS, inspects it, and re-originates it with a certificate signed by
its own root -- `O=Google Cloud Managed Service, CN=Agent Gateway TLS
Inspection CA (<region>)`. A Python runtime that has never seen that root
rejects every outbound HTTPS call:

    ClientConnectorCertificateError: Cannot connect to host
    us-central1-aiplatform.mtls.googleapis.com:443
    [SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain

That is what happens here, and it happens on the Agent Runtime's own
session-creation call, before any tool or model call runs -- so the symptom is
not "TLS error", it is an agent that returns an empty response to everything.

The fix is to ADD this root to the trust store, never to replace or disable it:

  * the bundle is built at import time from the *runtime's own* certifi file,
    so every public CA stays trusted and stays current with the deployed
    image, rather than being frozen to whatever the machine that ran the
    deployment happened to have;
  * the gateway root is appended to that copy;
  * SSL_CERT_FILE points OpenSSL (and therefore aiohttp's default context,
    which is what google-auth's async transport uses) at the combined file;
  * REQUESTS_CA_BUNDLE points `requests` at the same file, because requests
    reads certifi directly and ignores SSL_CERT_FILE;
  * GRPC_DEFAULT_SSL_ROOTS_FILE_PATH points gRPC at it too. This one is not
    optional and not obvious: gRPC does its TLS in its own C core against
    BoringSSL, with a root store that reads none of the variables above. Set
    the first two only and the symptom is a *partially* working agent -- the
    model call goes over REST and succeeds, while Secret Manager and every
    other gRPC client keeps failing with
      ssl_transport_security.cc: CERTIFICATE_VERIFY_FAILED
    which surfaces far from its cause, as "could not reach Zoho".

There is deliberately no verify=False, no ssl._create_unverified_context, and
no custom SSLContext with verification relaxed. A proxy that cannot be
verified is indistinguishable from an attacker, and the whole point of this
project is that controls are real.

Activation is explicit: nothing happens unless AGENT_GATEWAY_CA_TRUST=1 is set
at deploy time, which the deployment only does when the agent is being bound to
a gateway. An unbound agent therefore behaves exactly as it did before, and the
extra trust does not exist in deployments that have no proxy to trust.
"""
from __future__ import annotations

import os
import tempfile

CA_FILENAME = "gateway_ca.pem"
ENV_FLAG = "AGENT_GATEWAY_CA_TRUST"


def install() -> str | None:
    """Build the combined CA bundle and point the TLS stack at it.

    Returns the bundle path, or None when the fix is not active. Never raises:
    an agent that cannot build the bundle should fail later with a real TLS
    error that names the problem, rather than failing to import with a
    traceback that hides it.
    """
    if os.getenv(ENV_FLAG, "").strip() != "1":
        return None

    ca_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CA_FILENAME)
    if not os.path.exists(ca_path):
        return None

    try:
        import certifi

        with open(certifi.where(), "rb") as handle:
            public_roots = handle.read()
        with open(ca_path, "rb") as handle:
            gateway_root = handle.read()

        # A real file on disk, because OpenSSL reads the path, not a buffer.
        # delete=False: it has to outlive this function for the life of the
        # process.
        bundle = tempfile.NamedTemporaryFile(
            prefix="agent-gateway-ca-", suffix=".pem", delete=False)
        with bundle:
            bundle.write(public_roots)
            if not public_roots.endswith(b"\n"):
                bundle.write(b"\n")
            bundle.write(gateway_root)
            if not gateway_root.endswith(b"\n"):
                bundle.write(b"\n")

        os.environ["SSL_CERT_FILE"] = bundle.name
        os.environ["REQUESTS_CA_BUNDLE"] = bundle.name
        # gRPC reads neither of the above; it has its own root store.
        os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = bundle.name
        return bundle.name
    except Exception:  # noqa: BLE001 - see the docstring: never block import
        return None
