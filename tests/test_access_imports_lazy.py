# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Import-firewall regression for the thin `sol` access surface."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

FORBIDDEN = {
    "flask",
    "werkzeug",
    "jinja2",
    "anthropic",
    "openai",
    "google.genai",
    "httpx",
    "numpy",
    "PIL",
    "soundfile",
    "av",
    "pypdf",
    "frontmatter",
}

MODULES = (
    "solstone.think.chat_cli",
    "solstone.think.call",
    "solstone.think.link",
    "solstone.apps",
    "solstone.think.notify_cli",
    "solstone.think.skills_cli",
    "solstone.think.doctor",
    "solstone.convey.reasons",
    "solstone.think.sol_cli",
    "solstone.think.import_client",
    "solstone.convey.provider_readiness",
    "solstone.think.providers.state",
)

PROBE = """
import importlib
import json
import sys

module = sys.argv[1]
importlib.import_module(module)
print("MODULES_JSON:" + json.dumps(sorted(sys.modules)))
"""


def _probe_modules(module: str) -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", PROBE, module],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (
        f"probe for {module} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    sentinel = [
        line for line in result.stdout.splitlines() if line.startswith("MODULES_JSON:")
    ]
    assert len(sentinel) == 1, (
        f"expected exactly one MODULES_JSON line, got {len(sentinel)}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return set(json.loads(sentinel[0][len("MODULES_JSON:") :]))


def _leaked_families(modules: set[str]) -> list[str]:
    leaked: list[str] = []
    for forbidden in sorted(FORBIDDEN):
        if any(
            loaded == forbidden or loaded.startswith(f"{forbidden}.")
            for loaded in modules
        ):
            leaked.append(forbidden)
    return leaked


@pytest.mark.timeout(120)
@pytest.mark.parametrize("module", MODULES)
def test_access_surface_import_stays_light(module: str) -> None:
    modules = _probe_modules(module)
    assert module in modules
    leaked = _leaked_families(modules)
    assert not leaked, f"{module} pulled in forbidden modules: {leaked}"


def test_chat_cli_import_does_not_load_callosum_or_chat_stream() -> None:
    modules = _probe_modules("solstone.think.chat_cli")
    assert "solstone.think.callosum" not in modules
    assert "solstone.convey.chat_stream" not in modules
