# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-test for scripts/check_call_http_only.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_call_http_only.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_call_http_only", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ccho = _load_checker()


def _write_file(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("from solstone.think.utils import get_journal\n", "import"),
        ("from solstone.convey.utils import contained_path\n", "import"),
        ("from solstone.think.journal_io import LockTimeout\n", "import"),
        ("import solstone.apps.observer.share_delete\n", "import"),
        (
            "def f():\n"
            "    from solstone.think.utils import get_journal\n"
            "    return get_journal()\n",
            "import",
        ),
        ("def f():\n    open('x')\n", "fs"),
        ("def f(p):\n    p.mkdir(parents=True)\n", "fs"),
        ("import shutil\n\ndef f(a, b):\n    shutil.move(a, b)\n", "fs"),
        ("def f(dst):\n    dst.parent.mkdir()\n", "fs"),
        ("def f(p):\n    p.write_text('x')\n", "fs"),
        ("def f(p):\n    p.unlink()\n", "fs"),
        ("import os\n\ndef f(p):\n    os.remove(p)\n", "fs"),
    ],
)
def test_scan_source_flags_http_only_violations(source: str, kind: str) -> None:
    findings = ccho.scan_source(source)
    assert [finding[1] for finding in findings] == [kind]


def test_mixed_parent_mkdir_flags_once() -> None:
    findings = ccho.scan_source("def f(dst):\n    dst.parent.mkdir()\n")
    assert findings == [(2, "fs", "Path.mkdir")]


@pytest.mark.parametrize(
    "source",
    [
        "from solstone.think.convey_client import call_get\n",
        "from solstone.convey.reasons import Reason\n",
        "import typer\n",
        "from pathlib import Path\n",
        "def f(Path, x):\n    return Path(x) / 'y'\n",
        "def f(p):\n    return p.parent\n",
        "def f(p):\n    return p.with_suffix('.tmp')\n",
        "def f(p):\n    return p.name\n",
        "import os\n\ndef f():\n    return os.environ.get('X')\n",
        "import os\n\ndef f():\n    return os.getenv('X')\n",
        "import subprocess\n\ndef f():\n    return subprocess.run(['x'])\n",
        "import os\n\ndef f(a, b):\n    return os.path.join(a, b)\n",
        "import os.path\n\ndef f(a, b):\n    return os.path.join(a, b)\n",
    ],
)
def test_scan_source_ignores_allowed_and_out_of_scope_surfaces(source: str) -> None:
    assert ccho.scan_source(source) == []


def test_ratchet_by_file_kind_count(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    _write_file(
        root,
        "solstone/apps/badapp/call.py",
        "from solstone.think.utils import get_journal\n\n"
        "def f():\n"
        "    open('x')\n"
        "    return get_journal()\n",
    )

    over, stale, tracked = ccho.evaluate(root, {})
    assert over
    assert stale == []
    assert tracked == []

    counts = ccho.count_violations(root)
    over_exact, stale_exact, tracked_exact = ccho.evaluate(root, counts)
    assert over_exact == []
    assert stale_exact == []
    assert tracked_exact

    key = next(iter(counts))
    ratcheted = dict(counts)
    ratcheted[key] = counts[key] - 1
    over_lowered, stale_lowered, _ = ccho.evaluate(root, ratcheted)
    assert over_lowered
    assert stale_lowered == []


def test_stale_allowlist_entries_are_reported_for_vanished_keys(
    tmp_path: Path,
) -> None:
    root = tmp_path / "clean"
    _write_file(root, "solstone/apps/clean/call.py", "import typer\n")
    allowlist = {("solstone/apps/vanished/call.py", "import"): 1}

    over, stale, tracked = ccho.evaluate(root, allowlist)
    assert over == []
    assert any("solstone/apps/vanished/call.py" in line for line in stale)
    assert tracked == []


def test_subprocess_reports_new_violations(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    _write_file(
        root,
        "solstone/apps/badapp/call.py",
        "from solstone.think.utils import get_journal\n\n"
        "def main():\n"
        "    return get_journal()\n",
    )

    result = _run(root)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "call-http-only: NEW violations:" in result.stderr
    assert "solstone/apps/badapp/call.py" in result.stderr


def test_excluded_file_with_violations_is_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "excluded"
    # EXCLUDED_FILES is currently empty (every journal-data call.py is a pure
    # Convey HTTP client), so inject a synthetic exclusion to exercise the
    # skip-an-excluded-file mechanism itself.
    excluded_rel = "solstone/apps/excluded_app/call.py"
    monkeypatch.setattr(ccho, "EXCLUDED_FILES", frozenset({excluded_rel}))
    assert excluded_rel in ccho.EXCLUDED_FILES
    _write_file(
        root,
        excluded_rel,
        "from solstone.think.utils import get_journal\n\n"
        "def f():\n"
        "    open('x')\n"
        "    return get_journal()\n",
    )

    # Excluded file is skipped by discover_modules, so its import+fs violations
    # never reach `live`; with an empty allowlist nothing is over/stale/tracked.
    over, stale, tracked = ccho.evaluate(root, {})
    assert over == []
    assert stale == []
    assert tracked == []


def test_repo_tree_is_green() -> None:
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "call-http-only: pass" in result.stdout
