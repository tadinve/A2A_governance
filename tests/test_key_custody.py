"""Structural guards for the property this change exists to create.

These tests fail if someone reintroduces a private key into a package, an
environment variable, or a file, or gives a second component the ability to
sign. They are cheap, and they are the only tests that keep the architecture
from eroding back to where it started.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "governance_demo"
CLOUD = ROOT / "cloud"


def test_no_private_key_files_anywhere_in_the_repository():
    """No private key material on disk, judged by content rather than suffix.

    This used to ban every *.pem path outright. That was both too strict and
    too loose: too strict because a PUBLIC certificate is legitimately shipped
    with the agent packages (cloud/*/gateway_ca.pem, the Agent Gateway's TLS
    inspection root, which has to be in the upload for an agent routed through
    the gateway to verify the proxy at all); too loose because a private key
    saved as .key, .txt or with no extension walked straight past it.

    Looking inside every file closes both gaps. A PEM private key announces
    itself in its header no matter what the file is called, and a certificate
    never carries one.
    """
    skip = {".venv", ".git", "__pycache__", "node_modules", "evidence", "runtime"}
    markers = ("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY",
               "BEGIN EC PRIVATE KEY", "BEGIN DSA PRIVATE KEY",
               "BEGIN OPENSSH PRIVATE KEY", "BEGIN ENCRYPTED PRIVATE KEY")

    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or skip & set(path.parts):
            continue
        # This test is itself a file full of those markers; so is anything
        # else that merely names them.
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            continue
        if any(marker in head for marker in markers):
            offenders.append(path.relative_to(ROOT))

    assert offenders == [], f"private key material on disk: {offenders}"


def test_no_staged_demo_key_directories_remain():
    staged = [path.relative_to(ROOT) for path in CLOUD.rglob("demo_keys") if path.is_dir()]
    assert staged == [], f"key staging directories still present: {staged}"


def test_only_the_auth_broker_can_reach_a_signing_backend():
    """Exactly one module may import the signing backends."""
    importers = sorted(
        path.name
        for path in SOURCE.glob("*.py")
        if "from .signing import" in path.read_text() or "import signing" in path.read_text()
    )
    assert importers == ["auth_broker_app.py"], f"unexpected signing importers: {importers}"


def test_security_module_offers_no_local_signing_path():
    source = (SOURCE / "security.py").read_text()
    assert "_private_key" not in source
    assert "jwt.encode" not in source, "tokens must be minted by the Auth Broker, not locally"
    assert "issue_token" not in source, "local issuance was replaced by request_token"


def test_no_component_reads_a_key_from_the_environment():
    """A private key in an env var is a private key in `ps`, in logs, and in
    every process dump. The only key-shaped env var permitted is the KMS key
    *name*, which is a resource identifier, not secret material."""
    for path in list(SOURCE.glob("*.py")) + list(CLOUD.glob("*.py")):
        source = path.read_text()
        for forbidden in ("ISSUER_PRIVATE_KEY", "PRIVATE_KEY_PEM", "SIGNING_KEY_PEM"):
            assert forbidden not in source, f"{path.name} reads key material from the environment"


def test_delegation_key_is_not_stored_in_secret_manager():
    """Secret Manager holds only secrets that cannot use a broker. The
    delegation private key is not one of them -- it belongs in KMS, where it
    cannot be read back at all."""
    setup = CLOUD / "setup_kms_signing.py"
    assert setup.exists(), "expected the KMS signing setup script"
    source = setup.read_text()
    # Prose about roles/secretmanager.* is fine; *using* the API is not.
    assert "add_secret_version" not in source
    assert "SecretManagerServiceClient" not in source
    assert "import secretmanager" not in source
    # And the key must be created non-exportable, for signing only.
    assert "ASYMMETRIC_SIGN" in source

    # No agent package may read the delegation key out of Secret Manager either.
    for governance in CLOUD.glob("*/governance.py"):
        text = governance.read_text()
        assert "access_secret_version" not in text, f"{governance} still reads the key as a secret"
        assert "SecretManagerServiceClient" not in text


def test_the_a2a_write_path_cannot_manufacture_a_human_delegation():
    """Delegation is a precondition on the deployed write path, not an option.

    The earlier executor minted a fresh demo-user chain when no token arrived,
    so a caller that simply omitted the delegation got a purchase order anyway.
    """
    source = (CLOUD / "procurement_a2a" / "executor.py").read_text()
    assert "human_token" not in source, "the A2A path must not mint a human grant"
    assert "No delegated token presented" in source, "a missing delegation must deny"
    # The onward exchange may be built from the received token and nothing else.
    assert 'exchange_token(\n                AGENT_ID, subject_token,' in source
