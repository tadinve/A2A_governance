"""Delegation-token signing backends.

The delegation signing key is the most sensitive material in this system: it
mints the tokens every other component trusts. So the only code that can reach a
signing operation lives here, and this module is imported by exactly one
process -- the Auth Broker.

Two backends implement the same narrow interface:

* ``CloudKmsBackend`` is the production shape. The private key is generated
  inside Cloud KMS and is non-exportable: there is no API that returns its
  bytes, to us or to anyone else. Signing is an IAM-authorized ``asymmetricSign``
  call made by the Auth Broker's own identity. Compromising the broker gets an
  attacker the ability to ask for signatures while they hold that identity; it
  does not get them a key they can walk away with.

* ``LocalSoftwareBackend`` keeps the classroom demo runnable with no cloud
  account. It generates an RSA key in memory at broker start and never writes it
  anywhere -- no file, no package, no environment variable. The key dies with the
  process. It is a weaker guarantee than KMS (a local attacker with debugger
  access to the broker could read process memory) and the demo says so out loud,
  but it preserves the property that matters for the lesson: no component other
  than the Auth Broker can sign, and the key exists in exactly one place.

Both backends expose only ``sign`` and ``public_key_pem``. Neither can hand back
private key material, which is what lets the rest of the codebase stay honest.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Protocol


ALGORITHM = "RS256"


def b64url(data: bytes) -> str:
    """Base64url-encode without padding, as JWS requires."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class SigningBackend(Protocol):
    """What the Auth Broker is allowed to do with the signing key: sign, and
    publish the public half. Deliberately no ``private_key()``."""

    name: str
    kid: str

    def sign(self, signing_input: bytes) -> bytes: ...

    def public_key_pem(self) -> bytes: ...


class LocalSoftwareBackend:
    """An in-process RSA key, generated at startup and never persisted."""

    name = "local-software"

    def __init__(self) -> None:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        self._padding = padding
        self._hashes = hashes
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._public_pem = self._key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        # A key id derived from the public key, so verifiers can notice rotation.
        self.kid = hashlib.sha256(self._public_pem).hexdigest()[:16]

    def sign(self, signing_input: bytes) -> bytes:
        return self._key.sign(signing_input, self._padding.PKCS1v15(), self._hashes.SHA256())

    def public_key_pem(self) -> bytes:
        return self._public_pem


class CloudKmsBackend:
    """A Cloud KMS asymmetric signing key whose private half cannot be exported.

    Expects a key created with purpose ASYMMETRIC_SIGN and algorithm
    RSA_SIGN_PKCS1_2048_SHA256, which is exactly the primitive RS256 needs.
    """

    name = "cloud-kms"

    def __init__(self, key_name: str) -> None:
        from google.cloud import kms

        self._client = kms.KeyManagementServiceClient()
        self._key_name = key_name
        # Fetch once at startup: it is public, stable per key version, and this
        # surfaces a misconfigured key or a missing IAM grant immediately rather
        # than on the first token request.
        public = self._client.get_public_key(request={"name": key_name})
        self._public_pem = public.pem.encode()
        self.kid = key_name.rsplit("/", 1)[-1]

    def sign(self, signing_input: bytes) -> bytes:
        # KMS signs a digest we compute, so the token body never leaves as
        # plaintext and the request stays a fixed 32 bytes.
        digest = hashlib.sha256(signing_input).digest()
        response = self._client.asymmetric_sign(
            request={"name": self._key_name, "digest": {"sha256": digest}}
        )
        return response.signature

    def public_key_pem(self) -> bytes:
        return self._public_pem


def build_backend() -> SigningBackend:
    """Select a backend from the environment.

    ``DELEGATION_SIGNER=kms`` with ``KMS_SIGNING_KEY`` set to a key-version
    resource name is the deployed configuration. Anything else defaults to the
    local software key so the offline demo keeps working.
    """
    mode = os.getenv("DELEGATION_SIGNER", "local").strip().lower()
    if mode == "kms":
        key_name = os.getenv("KMS_SIGNING_KEY", "").strip()
        if not key_name:
            raise RuntimeError(
                "DELEGATION_SIGNER=kms requires KMS_SIGNING_KEY "
                "(projects/../locations/../keyRings/../cryptoKeys/../cryptoKeyVersions/N)"
            )
        return CloudKmsBackend(key_name)
    if mode == "local":
        return LocalSoftwareBackend()
    raise RuntimeError(f"Unknown DELEGATION_SIGNER {mode!r}; use 'local' or 'kms'")


def sign_claims(claims: dict[str, Any], backend: SigningBackend) -> str:
    """Assemble and sign a JWS compact token.

    Built by hand rather than with ``jwt.encode`` because a KMS-backed key has no
    bytes to hand PyJWT: the signature comes back from a remote call.
    """
    header = {"alg": ALGORITHM, "typ": "JWT", "kid": backend.kid}
    signing_input = "{}.{}".format(
        b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode()),
        b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()),
    ).encode()
    return f"{signing_input.decode()}.{b64url(backend.sign(signing_input))}"
