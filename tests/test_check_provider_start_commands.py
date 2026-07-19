# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_provider_start_commands.py"
)


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_provider_start_commands", SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_file(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def _kinds(findings: list[tuple[int, str, str]]) -> set[str]:
    return {kind for _lineno, kind, _detail in findings}


checker = _load_checker()


def test_scan_flags_provider_start_commands() -> None:
    findings = checker.scan_source(
        "from solstone.think.callosum import callosum_send as send\n"
        "def f(client):\n"
        "    send('supervisor', 'start_local')\n"
        "    client.emit(tract='supervisor', event='start_parakeet')\n"
        "    msg = {'tract': 'supervisor', 'event': 'start_local'}\n"
        "    return msg\n"
    )

    kinds = _kinds(findings)
    assert "provider_start_command" in kinds
    assert "provider_start_message" in kinds


@pytest.mark.parametrize(
    "source",
    [
        "from solstone.think import callosum\n"
        "callosum.callosum_send('supervisor', 'start_local')\n",
        "from solstone.think import callosum as bus\n"
        "bus.callosum_send('supervisor', 'start_parakeet')\n",
        "import solstone.think.callosum as callosum\n"
        "callosum.callosum_send('supervisor', 'start_local')\n",
    ],
)
def test_scan_flags_reasonable_callosum_import_spellings(source: str) -> None:
    findings = checker.scan_source(source)

    assert _kinds(findings) == {"provider_start_command"}


def test_scan_flags_restored_handler_names() -> None:
    findings = checker.scan_source(
        "def _handle_supervisor_start_local(message):\n"
        "    return None\n"
        "def _request_parakeet_server_start():\n"
        "    return None\n"
    )

    assert _kinds(findings) == {"provider_start_handler"}


def test_scan_allows_unrelated_callosum_traffic_and_raw_strings() -> None:
    findings = checker.scan_source(
        "def f(client):\n"
        "    client.emit('thinking', 'start_local')\n"
        "    client.emit('supervisor', 'restart')\n"
        "    msg = {'tract': 'thinking', 'event': 'start_parakeet'}\n"
        "    raw = 'start_local start_parakeet'\n"
        "    return msg, raw\n"
    )

    assert findings == []


def test_e2e_flags_violation(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "solstone/bad.py",
        "from solstone.think.callosum import callosum_send\n"
        "callosum_send('supervisor', 'start_parakeet')\n",
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "provider-start-commands: violations:" in result.stderr
    assert "provider_start_command" in result.stderr


def test_e2e_clean_source_passes(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "solstone/good.py",
        "def f(client):\n    client.emit('supervisor', 'restart')\n",
    )

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "provider-start-commands: pass" in result.stdout
    assert result.stderr == ""


def test_allowlist_is_empty_and_ratcheted(tmp_path: Path) -> None:
    assert checker.ALLOWLIST == {}
    _write_file(
        tmp_path,
        "solstone/bad.py",
        "def _handle_supervisor_start_parakeet(message):\n    return None\n",
    )
    counts = checker.count_violations(tmp_path)

    over, stale, tracked = checker.evaluate(tmp_path, counts)
    assert over == []
    assert stale == []
    assert tracked

    ratcheted = {next(iter(counts)): 0}
    over, stale, _tracked = checker.evaluate(tmp_path, ratcheted)
    assert over
    assert stale == []

    stale_allowlist = {("solstone/missing.py", "provider_start_handler"): 1}
    over, stale, _tracked = checker.evaluate(tmp_path, stale_allowlist)
    assert over
    assert stale


def test_landed_tree_is_clean() -> None:
    over, stale, _tracked = checker.evaluate(checker.ROOT, checker.ALLOWLIST)

    assert over == []
    assert stale == []
