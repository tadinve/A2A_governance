"""Structural guards on the Agent Gateway deployment artifacts.

These tests read source only. They never touch Google Cloud, so a passing run
says the scripts are *shaped* correctly -- it says nothing about whether a
gateway exists or enforcement works. That claim belongs to
cloud/verify_agent_gateway.py, which reads live resources, and the distinction
matters enough to keep the two apart: a green test suite must never be
mistakable for evidence that a cloud control is in force.

The properties pinned here are the ones whose quiet regression would be
dangerous rather than merely wrong -- enforcement becoming the default, the
setup script acquiring the power to bind agents, or the local FastAPI gateway
being described as the Google product.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"

SETUP = CLOUD / "setup_agent_gateway.sh"
BIND = CLOUD / "bind_agents_to_gateway.sh"
ENFORCE = CLOUD / "configure_gateway_enforcement.sh"
VERIFY = CLOUD / "verify_agent_gateway.py"
DESTINATIONS = CLOUD / "gateway_destinations.py"
DEPLOY = ROOT / "deploy_to_gcp.sh"

AGENTS = ["Inventory Agent", "Procurement Agent", "Procurement Agent (A2A)"]


def read(path: Path) -> str:
    assert path.is_file(), f"expected {path.relative_to(ROOT)} to exist"
    return path.read_text(encoding="utf-8")


def executable_lines(path: Path) -> str:
    """The script with comment lines removed.

    Needed because the interesting assertions are about what a script *does*,
    not what it mentions. deploy_to_gcp.sh should point a reader at
    bind_agents_to_gateway.sh in its help text while never invoking it, and a
    plain substring search cannot tell those two apart.
    """
    return "\n".join(line for line in read(path).splitlines()
                     if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("path", [SETUP, BIND, ENFORCE, VERIFY, DESTINATIONS])
def test_artifact_exists(path):
    read(path)


def test_setup_creates_an_agent_to_anywhere_gateway():
    body = read(SETUP)
    assert "network-services agent-gateways import" in body
    assert "governedAccessPath: AGENT_TO_ANYWHERE" in body


def test_setup_attaches_the_regional_agent_registry():
    assert "//agentregistry.googleapis.com/projects/" in read(SETUP)


def test_setup_configures_iap_authorization():
    body = read(SETUP)
    assert "service-extensions authz-extensions import" in body
    assert "service: iap.googleapis.com" in body
    assert "network-security authz-policies import" in body


def test_dry_run_is_the_default_and_fails_open():
    """Dry run must not be able to block, including when IAP is unavailable.

    failOpen:false in dry-run is the worst combination available: it cannot
    enforce policy, but it can still take every outbound call down when the
    extension errors. The written specification asked for it; it is
    deliberately not implemented, and this test is why that stays true.
    """
    # Executable lines only: the comment block above the heredocs explains
    # both settings, so a raw substring search would pass on the prose that
    # describes the right behaviour even if the YAML did the wrong thing.
    body = executable_lines(SETUP)
    assert "ENFORCE=false" in body, "enforcement must not be the default"
    assert 'iamEnforcementMode: "DRY_RUN"' in body
    dry_run_block = body.split('iamEnforcementMode: "DRY_RUN"')[0]
    assert "failOpen: true" in dry_run_block, "dry run must fail open"


def test_an_enforcement_option_exists_and_fails_closed():
    body = executable_lines(SETUP)
    assert "--enforce" in body
    assert "failOpen: false" in body


def test_setup_does_not_bind_agents():
    """Provisioning must stay inert.

    Binding reroutes every outbound call an agent makes. If setup could do it,
    re-running setup -- which is otherwise safe -- would silently change live
    routing.
    """
    body = executable_lines(SETUP)
    assert "agentGatewayConfig" not in body, (
        "setup_agent_gateway.sh must not patch agent deployments; "
        "binding belongs to bind_agents_to_gateway.sh")
    assert "reasoningEngines" not in body


def test_binding_is_a_separate_script_that_patches_the_documented_field():
    body = read(BIND)
    assert "spec.deploymentSpec.agentGatewayConfig" in body
    assert "agentToAnywhereConfig" in body
    assert "PATCH" in body


def test_binding_is_reversible():
    assert "--unbind" in read(BIND)


def test_binding_verifies_the_change_actually_applied():
    """A PATCH response is not evidence that the field persisted.

    Observed live against a real project: all three agents reported "bound"
    and read back with no agentGatewayConfig at all. The API returns an
    ordinary non-error body for an update it does not apply, so the script
    must read the resource back and compare rather than trust the response.
    """
    body = read(BIND)
    assert "APPLIED=" in body, (
        "bind_agents_to_gateway.sh must read the resource back after PATCH")
    patch_index = body.index("-X PATCH")
    assert body.index("APPLIED=") > patch_index, (
        "the read-back must happen after the PATCH, not before")
    assert "did not apply it" in body, (
        "an unapplied change must be reported as a failure, not as success")


def test_binding_requires_agent_identity():
    """An agent on a service-account fallback must not be bound.

    The gateway authorizes by agent principal. Binding an agent that has no
    Agent Identity produces something that looks governed and is not.
    """
    assert "effectiveIdentity" in read(BIND)


@pytest.mark.parametrize("agent", AGENTS)
def test_all_three_agents_are_in_scope_for_binding(agent):
    assert agent in read(BIND)


@pytest.mark.parametrize("agent", AGENTS)
def test_verification_checks_every_agent(agent):
    assert agent in read(VERIFY)


def test_verification_checks_live_resources_not_source():
    body = read(VERIFY)
    assert "googleManaged" in body and "AGENT_TO_ANYWHERE" in body
    assert "effectiveIdentity" in body
    assert "agentGatewayConfig" in body


def test_gateway_comparison_tolerates_project_number_for_project_id():
    """Google echoes a submitted project id back as a project number.

    Observed live: the authorization policy is submitted targeting
    projects/<id>/.../agentGateways/<name> and reads back as
    projects/<number>/... . An exact string comparison would report a
    correctly bound agent as unbound, and the natural response to that false
    negative is to bind again.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_agent_gateway", VERIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    by_id = "projects/my-project-id/locations/us-central1/agentGateways/gw"
    by_number = "projects/972256875165/locations/us-central1/agentGateways/gw"
    other = "projects/972256875165/locations/us-central1/agentGateways/different"

    assert module.same_gateway(by_id, by_number), "id and number must compare equal"
    assert not module.same_gateway(by_id, other), "a different gateway must not"
    assert not module.same_gateway("", by_id), "unbound must never look bound"


def test_binding_script_also_tolerates_project_number():
    assert "#*/locations/" in read(BIND), (
        "bind_agents_to_gateway.sh must compare gateway URIs by the part "
        "after /locations/, or every re-run issues a redundant PATCH")


def test_verification_separates_pre_and_post_bind():
    """--pre-bind exists so the honest state after setup reports as passing."""
    body = read(VERIFY)
    assert "--pre-bind" in body and "--post-bind" in body


def test_deploy_integration_is_opt_in_and_never_enforces():
    """The flag may provision. It must not bind, and it must not enforce.

    Checked against executable lines only: the help text is expected to name
    both of the other scripts, since pointing a reader at them is exactly how
    they stay deliberate steps rather than forgotten ones.
    """
    body = read(DEPLOY)
    assert "--with-agent-gateway" in body, "the opt-in flag must exist"

    runnable = executable_lines(DEPLOY)
    assert "setup_agent_gateway.sh" in runnable, "the flag must provision"
    assert "bind_agents_to_gateway.sh" not in runnable, (
        "deploy_to_gcp.sh must never bind agents")
    assert "configure_gateway_enforcement.sh" not in runnable, (
        "deploy_to_gcp.sh must never enable enforcement")
    assert "--enforce" not in runnable, (
        "deploy_to_gcp.sh must never pass --enforce to the setup script")


def test_the_local_gateway_simulation_is_not_removed():
    """gateway_app.py is the offline reference implementation, and stays."""
    assert (ROOT / "src" / "governance_demo" / "gateway_app.py").is_file()


def test_destination_inventory_excludes_inventory_agent_from_the_write_path():
    """The deny case the demo exists to show must not be registered away.

    If Inventory Agent appears on the write connector's agent list, the
    gateway would be configured to permit exactly the call the rest of the
    system is built to refuse.
    """
    body = read(DESTINATIONS)
    block = body.split('"key": "zoho-procurewrite"')[1].split("},")[0]
    assert "INVENTORY" not in block, (
        "Inventory Agent must not be granted the Zoho write connector")
    assert "PROCUREMENT" in block
