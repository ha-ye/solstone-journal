# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-test for scripts/check_journal_io_access.py.

Drives the journal_io access check against throwaway good/bad fixture trees and
asserts the detector's import-binding discrimination, owner exclusions,
non-gated symbol handling, and ratcheting allowlist behavior.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_journal_io_access.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_journal_io_access", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cja = _load_checker()


BAD_IMPORT = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from solstone.think.journal_io import write_json


def persist(path):
    write_json(path, {"ok": True})
"""

LOCAL_DISCRIMINATION = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc


def write_json(path, payload):
    return path, payload


def persist(path):
    write_json(path, {"ok": True})
    path.write_text("ok", encoding="utf-8")
"""

OWNER_IMPORT = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from solstone.think.journal_io import write_json


def persist(path):
    write_json(path, {"ok": True})
"""

NON_GATED_IMPORTS = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from solstone.think.journal_io import contained_path, day_path, read_json


def inspect(root, path):
    read_json(path)
    contained_path(root, path)
    day_path("20260606")
"""

SUBMODULE_IMPORT = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from solstone.think.journal_io.atomic import write_json


def persist(path):
    write_json(path, {"ok": True})
"""

NPZ_SUBMODULE_IMPORT = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from solstone.think.journal_io.npz import save_npz, update_npz


def persist(path, arrays, transform):
    save_npz(path, arrays, expected_keys=("data",))
    update_npz(path, transform, expected_keys=("data",))
"""


WRITE_NPZ_SUBMODULE_IMPORT = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from solstone.think.journal_io.npz import write_npz


def persist(path, arrays):
    write_npz(path, arrays, expected_keys=("data",))
"""


def _write_file(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def bad_root(tmp_path) -> Path:
    root = tmp_path / "bad"
    _write_file(root, "solstone/apps/badapp/routes.py", BAD_IMPORT)
    return root


@pytest.fixture
def local_root(tmp_path) -> Path:
    root = tmp_path / "local"
    _write_file(root, "solstone/apps/localapp/routes.py", LOCAL_DISCRIMINATION)
    return root


@pytest.fixture
def owner_root(tmp_path) -> Path:
    root = tmp_path / "owner"
    _write_file(root, "solstone/think/entities/saving.py", OWNER_IMPORT)
    return root


@pytest.fixture
def non_gated_root(tmp_path) -> Path:
    root = tmp_path / "non-gated"
    _write_file(root, "solstone/apps/reader/routes.py", NON_GATED_IMPORTS)
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def test_bad_import_exits_one_and_names_file_and_primitive(bad_root):
    result = _run(bad_root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "solstone/apps/badapp/routes.py" in result.stderr
    assert "write_json" in result.stderr


def test_local_same_name_and_path_write_text_are_not_violations(local_root):
    result = _run(local_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "journal-io-access: pass" in result.stdout


def test_owner_path_is_exempt(owner_root):
    result = _run(owner_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "journal-io-access: pass" in result.stdout


def test_non_gated_imports_are_not_violations(non_gated_root):
    result = _run(non_gated_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "journal-io-access: pass" in result.stdout


def test_submodule_import_is_flagged():
    findings = cja.scan_source(SUBMODULE_IMPORT)
    assert findings == [(7, "write_json", "write_json")]


def test_npz_submodule_write_imports_are_flagged():
    findings = cja.scan_source(NPZ_SUBMODULE_IMPORT)
    assert [(primitive, bound_name) for _lineno, primitive, bound_name in findings] == [
        ("save_npz", "save_npz"),
        ("update_npz", "update_npz"),
    ]


def test_write_npz_submodule_import_is_flagged():
    findings = cja.scan_source(WRITE_NPZ_SUBMODULE_IMPORT)
    assert ("write_npz", "write_npz") in [
        (primitive, bound_name) for _lineno, primitive, bound_name in findings
    ]


def test_ratchet_by_file_kind_count(bad_root):
    # No allowlist -> every violation is new.
    new, tracked = cja.evaluate(bad_root, {})
    assert new
    assert tracked == []

    # Allowlist the exact counts -> green, all tracked.
    counts = cja.count_violations(bad_root)
    new_exact, tracked_exact = cja.evaluate(bad_root, counts)
    assert new_exact == []
    assert tracked_exact

    # Lower a single (file, kind) count below its actual occurrences -> fails.
    key = next(iter(counts))
    ratcheted = dict(counts)
    ratcheted[key] = counts[key] - 1
    new_over, _ = cja.evaluate(bad_root, ratcheted)
    assert new_over


def test_repo_tree_is_green():
    # The committed empty allowlist keeps the real tree green.
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
