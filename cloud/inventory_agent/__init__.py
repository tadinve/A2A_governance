# The gateway CA has to be trusted before anything builds an HTTP client or an
# SSL context, because OpenSSL reads SSL_CERT_FILE when a context is created.
# Importing .agent pulls in google.adk and the Vertex client, so this runs
# first, deliberately, above the imports it protects.
from . import gateway_trust

gateway_trust.install()

from . import agent  # noqa: E402
from .agent import root_agent  # noqa: E402

__all__ = ["agent", "root_agent", "gateway_trust"]
