# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_provider_install_owner.py"
)


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_provider_install_owner", SCRIPT
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


def test_scan_flags_direct_status_replace() -> None:
    findings = checker.scan_source(
        "from pathlib import Path\n"
        "from solstone.think.journal_io.atomic import atomic_replace\n"
        "status = Path('journal') / 'health' / 'providers' / 'local.json'\n"
        "atomic_replace(status, '{}')\n"
    )

    assert "provider_status_replace" in _kinds(findings)


def test_scan_flags_proof_cache_write() -> None:
    findings = checker.scan_source(
        "from solstone.think.providers.artifact_proof import proof_cache_path\n"
        "path = proof_cache_path('local')\n"
        "path.write_text('{}')\n"
    )

    assert "proof_cache_write" in _kinds(findings)


def test_scan_flags_raw_lease_open_and_flock() -> None:
    findings = checker.scan_source(
        "import fcntl\n"
        "import os\n"
        "from pathlib import Path\n"
        "lease = Path('journal') / 'health' / 'providers' / 'local.lease'\n"
        "fd = os.open(lease, os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    )

    kinds = _kinds(findings)
    assert "provider_lease_raw_open" in kinds
    assert "second_provider_install_lock" in kinds


def test_scan_flags_manifest_write() -> None:
    findings = checker.scan_source(
        "from pathlib import Path\n"
        "manifest = Path('cache') / '.solstone-provider-manifest.json'\n"
        "manifest.write_text('{}')\n"
    )

    assert "provider_manifest_write" in _kinds(findings)


def test_scan_flags_private_owner_alias() -> None:
    findings = checker.scan_source(
        "from solstone.think.providers.install_state import _read_current_unlocked as raw\n"
        "writer = raw\n"
    )

    kinds = _kinds(findings)
    assert "private_owner_symbol" in kinds
    assert "private_owner_wrapper" in kinds


def test_scan_flags_providers_bundled_access() -> None:
    findings = checker.scan_source(
        "def f(config):\n"
        "    providers = config.get('providers', {})\n"
        "    return providers.get('bundled')\n"
    )

    assert "providers_bundled_operational" in _kinds(findings)


def test_scan_allows_owner_api_calls() -> None:
    findings = checker.scan_source(
        "from solstone.think.providers.install_lease import acquire_install_lease\n"
        "from solstone.think.providers.install_state import write_install_status\n"
        "lease = acquire_install_lease('local')\n"
        "write_install_status(status)\n"
    )

    assert findings == []


def test_e2e_flags_violation(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "solstone/bad.py",
        "from pathlib import Path\n"
        "p = Path('journal') / 'health' / 'providers' / 'local.json'\n"
        "p.write_text('{}')\n",
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "provider-install-owner: violations:" in result.stderr
    assert "provider_status_write" in result.stderr


def test_e2e_clean_source_passes(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "solstone/good.py",
        "from solstone.think.providers.install_state import write_install_status\n"
        "def f(status):\n"
        "    return write_install_status(status)\n",
    )

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "provider-install-owner: pass" in result.stdout
    assert result.stderr == ""


def test_allowlist_ratchet_and_stale_entry(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "solstone/bad.py",
        "from pathlib import Path\n"
        "p = Path('journal') / 'health' / 'providers' / 'local.json'\n"
        "p.write_text('{}')\n",
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

    stale_allowlist = {("solstone/missing.py", "provider_status_write"): 1}
    over, stale, _tracked = checker.evaluate(tmp_path, stale_allowlist)
    assert over
    assert stale


def test_landed_tree_is_clean() -> None:
    over, stale, _tracked = checker.evaluate(checker.ROOT, checker.ALLOWLIST)

    assert over == []
    assert stale == []
