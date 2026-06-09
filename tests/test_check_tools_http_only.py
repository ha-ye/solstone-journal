# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-test for scripts/check_tools_http_only.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_tools_http_only.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_tools_http_only", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctho = _load_checker()


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
        ("from solstone.think.surfaces import health\n", "import"),
        ("from solstone.think.utils import get_journal\n", "import"),
        ("from solstone.think.utils import require_solstone\n", "import"),
        ("from solstone.think.journal_io import get_journal\n", "import"),
        ("def f():\n    open('x')\n", "fs"),
        ("def f(p):\n    p.mkdir(parents=True)\n", "fs"),
        ("def f(p):\n    p.write_text('x')\n", "fs"),
        ("import os\n\ndef f(p):\n    os.remove(p)\n", "fs"),
    ],
)
def test_scan_source_flags_tools_http_only_violations(source: str, kind: str) -> None:
    findings = ctho.scan_source(source)
    assert [finding[1] for finding in findings] == [kind]


@pytest.mark.parametrize(
    "source",
    [
        "from solstone.think.convey_client import get_client\n",
        "from solstone.convey.reasons import ENTITY_NOT_FOUND\n",
        "from solstone.convey.readiness_snapshot import highest_severity_group\n",
        "from solstone.think.pipeline_health import summarize_pipeline_day\n",
        "from solstone.think import convey_client\n",
        "from solstone.convey import reasons\n",
        "from solstone.convey import readiness_snapshot\n",
        "from solstone.think import pipeline_health\n",
        "import typer\n",
        "import json\n",
        "from pathlib import Path\n",
        "def f(Path, x):\n    return Path(x) / 'y'\n",
        "def f(p):\n    return p.parent\n",
        "def f(p):\n    return p.name\n",
    ],
)
def test_scan_source_ignores_allowed_imports_and_path_algebra(source: str) -> None:
    assert ctho.scan_source(source) == []


def test_main_flags_fixed_targets(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    _write_file(
        root,
        "solstone/think/tools/health.py",
        "from solstone.think.utils import get_journal\n\n"
        "def f():\n"
        "    open('x')\n"
        "    return get_journal()\n",
    )

    result = _run(root)

    assert result.returncode == 1
    assert (
        "solstone/think/tools/health.py:1: import solstone.think.utils" in result.stderr
    )
    assert "solstone/think/tools/health.py:4: fs open" in result.stderr


def test_repo_tree_is_green() -> None:
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tools-http-only: pass" in result.stdout
