"""Structural guard against the three deployed agent packages drifting apart.

Agent Runtime uploads each of cloud/inventory_agent, cloud/procurement_agent,
and cloud/procurement_a2a as a self-contained folder -- that is a platform
constraint, not a design choice, so there is no import across them and no build
step that generates one from the other. governance.py and zoho_mcp.py are
therefore hand-maintained in triplicate: every fix to the delegation policy,
the reorder rule, or the Zoho adapter has to be copied into all three by hand,
and nothing stops that copy from being partial or forgotten.

This is a real gap, not a hypothetical one: it has already happened once in
this repository's history. These tests do not close the gap -- avoiding it
would mean changing how Agent Runtime deploys a package -- but they make a
silent divergence loud, in the same spirit as test_key_custody.py.
"""
from __future__ import annotations

import difflib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"

# Every package that deploys independently and is expected to carry the same
# governance logic. Add a name here the day a fourth one exists.
AGENT_PACKAGES = ["inventory_agent", "procurement_agent", "procurement_a2a"]

# Files each package carries its own copy of, byte-for-byte, because Agent
# Runtime uploads the folder whole and none of them may import another.
SHARED_FILES = ["governance.py", "zoho_mcp.py"]


def _read(package: str, filename: str) -> str:
    return (CLOUD / package / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize("filename", SHARED_FILES)
def test_shared_file_is_identical_across_every_agent_package(filename):
    reference_package = AGENT_PACKAGES[0]
    reference = _read(reference_package, filename)

    for package in AGENT_PACKAGES[1:]:
        contents = _read(package, filename)
        if contents == reference:
            continue
        diff = "\n".join(difflib.unified_diff(
            reference.splitlines(), contents.splitlines(),
            fromfile=f"{reference_package}/{filename}",
            tofile=f"{package}/{filename}", lineterm=""))
        pytest.fail(
            f"{filename} has diverged between {reference_package} and {package}.\n"
            f"Every deployed agent package carries its own copy of this file, so "
            f"a change to one must be copied into all of: {', '.join(AGENT_PACKAGES)}.\n\n"
            f"{diff[:4000]}"
        )


def test_every_agent_package_actually_has_the_shared_files():
    """Guards the guard: a missing file must fail loudly, not compare as absent."""
    for package in AGENT_PACKAGES:
        for filename in SHARED_FILES:
            path = CLOUD / package / filename
            assert path.is_file(), f"expected {path.relative_to(ROOT)} to exist"
