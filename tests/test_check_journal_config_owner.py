# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_journal_config_owner.py"
)


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_journal_config_owner", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _kinds(findings: list[tuple[int, str, str]]) -> set[str]:
    return {kind for _lineno, kind, _detail in findings}


def _write_bad_module(tmp_path: Path, source: str) -> None:
    package = tmp_path / "solstone"
    package.mkdir()
    (package / "bad.py").write_text(source, encoding="utf-8")


def _run_gate(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_scanner_flags_private_serializer_import_and_call() -> None:
    gate = _load_gate()

    findings = gate.scan_source(
        "from solstone.think.journal_config import _write_journal_config as raw\n"
        "raw({})\n"
    )

    assert "private_serializer" in _kinds(findings)


def test_scanner_flags_config_specific_atomic_replace() -> None:
    gate = _load_gate()

    findings = gate.scan_source(
        "from solstone.think.journal_config import get_journal_config_path\n"
        "from solstone.think.journal_io.atomic import atomic_replace\n"
        "atomic_replace(get_journal_config_path(), '{}')\n"
    )

    assert "journal_config_replace" in _kinds(findings)


def test_scanner_flags_private_serializer_wrapper() -> None:
    gate = _load_gate()

    findings = gate.scan_source(
        "import solstone.think.journal_config as jc\n"
        "write_journal_config = jc._write_journal_config\n"
    )

    assert "private_serializer_wrapper" in _kinds(findings)


def test_scanner_flags_second_config_lock() -> None:
    gate = _load_gate()

    findings = gate.scan_source(
        "from solstone.think.journal_config import get_journal_config_path\n"
        "from solstone.think.journal_io.locking import hold_lock\n"
        "with hold_lock(get_journal_config_path()):\n"
        "    pass\n"
    )

    assert "second_config_lock" in _kinds(findings)


def test_scanner_flags_hand_rolled_sidecar_flock() -> None:
    gate = _load_gate()

    findings = gate.scan_source(
        "import fcntl\n"
        "from pathlib import Path\n"
        "lock_path = Path('journal') / 'config' / ('.' + 'journal.json' + '.lock')\n"
        "lock_file = open(lock_path, 'w')\n"
        "fcntl.flock(lock_file, fcntl.LOCK_EX)\n"
    )

    assert "second_config_lock" in _kinds(findings)


def test_scanner_allows_atomic_replace_on_non_config_domain() -> None:
    gate = _load_gate()

    findings = gate.scan_source(
        "from pathlib import Path\n"
        "from solstone.think.journal_io.atomic import atomic_replace\n"
        "atomic_replace(Path('config') / 'chat.json', '{}')\n"
    )

    assert findings == []


def test_e2e_flags_private_serializer(tmp_path: Path) -> None:
    _write_bad_module(
        tmp_path,
        "from solstone.think.journal_config import _write_journal_config as raw\n"
        "raw({})\n",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "journal-config-owner: NEW violations:" in result.stderr
    assert "private_serializer" in result.stderr


def test_e2e_flags_config_specific_atomic_replace(tmp_path: Path) -> None:
    _write_bad_module(
        tmp_path,
        "from solstone.think.journal_config import get_journal_config_path\n"
        "from solstone.think.journal_io.atomic import atomic_replace\n"
        "atomic_replace(get_journal_config_path(), '{}')\n",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "journal-config-owner: NEW violations:" in result.stderr
    assert "journal_config_replace" in result.stderr


def test_e2e_flags_private_serializer_wrapper(tmp_path: Path) -> None:
    _write_bad_module(
        tmp_path,
        "import solstone.think.journal_config as jc\n"
        "write_journal_config = jc._write_journal_config\n",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "journal-config-owner: NEW violations:" in result.stderr
    assert "private_serializer_wrapper" in result.stderr


def test_e2e_flags_second_config_lock(tmp_path: Path) -> None:
    _write_bad_module(
        tmp_path,
        "from solstone.think.journal_config import get_journal_config_path\n"
        "from solstone.think.journal_io.locking import hold_lock\n"
        "with hold_lock(get_journal_config_path()):\n"
        "    pass\n",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "journal-config-owner: NEW violations:" in result.stderr
    assert "second_config_lock" in result.stderr


def test_e2e_allows_atomic_replace_on_non_config_domain(tmp_path: Path) -> None:
    _write_bad_module(
        tmp_path,
        "from pathlib import Path\n"
        "from solstone.think.journal_io.atomic import atomic_replace\n"
        "atomic_replace(Path('config') / 'chat.json', '{}')\n",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 0
    assert "journal-config-owner: pass" in result.stdout
    assert result.stderr == ""
