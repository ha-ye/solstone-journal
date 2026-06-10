# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Import-firewall regression for the convey app-factory refactor.

A bare `import solstone.convey.state` (or `.config`) must not drag the web
stack (AppRegistry, provider_readiness) or any provider SDK into sys.modules —
every heavy import now lives inside create_app(). A fresh interpreter per probe
gives a pristine sys.modules unaffected by whatever else ran on this xdist
worker, so the guard measures the real static import graph.
"""

import json
import subprocess
import sys

import pytest

FORBIDDEN = {
    "solstone.apps",
    "solstone.convey.apps",
    "solstone.convey.provider_readiness",
    "google.genai",
    "openai",
    "anthropic",
}

PROBE_STATE = """
import json
import sys
import solstone.convey.state  # noqa: F401
print("MODULES_JSON:" + json.dumps(sorted(sys.modules)))
"""

PROBE_CONFIG = """
import json
import sys
import solstone.convey.config  # noqa: F401
print("MODULES_JSON:" + json.dumps(sorted(sys.modules)))
"""


def _probe_modules(probe: str) -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (
        f"probe subprocess exited {result.returncode}\n"
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


@pytest.mark.timeout(120)
def test_convey_state_import_stays_light():
    modules = _probe_modules(PROBE_STATE)
    assert "solstone.convey.state" in modules
    leaked = FORBIDDEN & modules
    assert not leaked, f"convey.state pulled in forbidden modules: {sorted(leaked)}"


@pytest.mark.timeout(120)
def test_convey_config_import_stays_light():
    modules = _probe_modules(PROBE_CONFIG)
    assert "solstone.convey.config" in modules
    # config legitimately imports flask; that is permitted.
    assert "flask" in modules
    leaked = FORBIDDEN & modules
    assert not leaked, f"convey.config pulled in forbidden modules: {sorted(leaked)}"


def test_emit_resolves_via_module_getattr():
    # Exercises __getattr__ via both the from-import and attribute-access paths.
    from solstone.convey import emit

    assert callable(emit)

    import solstone.convey as convey

    assert callable(convey.emit)
