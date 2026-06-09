# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-test for scripts/check_journal_io_mechanic.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_journal_io_mechanic.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_journal_io_mechanic", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cjm = _load_checker()


BAD_OS_REPLACE = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
import os


def persist(tmp, path):
    os.replace(tmp, path)
"""

OWNER_OS_REPLACE = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
import os


def persist(tmp, path):
    os.replace(tmp, path)
"""

HOME_OS_REPLACE = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
import os


def atomic(tmp, path):
    os.replace(tmp, path)
"""

EXCLUDED_OS_REPLACE = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
import os


def persist(tmp, path):
    os.replace(tmp, path)
"""

TEST_OS_REPLACE = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
import os


def test_raw_replace(tmp_path):
    os.replace(tmp_path / "a", tmp_path / "b")
"""


def _write_file(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def bad_root(tmp_path) -> Path:
    root = tmp_path / "bad"
    _write_file(root, "solstone/apps/badapp/routes.py", BAD_OS_REPLACE)
    return root


@pytest.fixture
def owner_root(tmp_path) -> Path:
    root = tmp_path / "owner"
    _write_file(root, "solstone/think/entities/saving.py", OWNER_OS_REPLACE)
    return root


@pytest.fixture
def home_root(tmp_path) -> Path:
    root = tmp_path / "home"
    _write_file(root, "solstone/think/journal_io/custom.py", HOME_OS_REPLACE)
    return root


@pytest.fixture
def excluded_root(tmp_path) -> Path:
    root = tmp_path / "excluded"
    _write_file(root, "solstone/think/scheduler.py", EXCLUDED_OS_REPLACE)
    return root


@pytest.fixture
def test_root(tmp_path) -> Path:
    root = tmp_path / "test"
    _write_file(root, "solstone/apps/badapp/test_raw.py", TEST_OS_REPLACE)
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (
            "import os\n\ndef persist(tmp, path):\n    os.replace(tmp, path)\n",
            "os.replace",
        ),
        (
            "import os as ops\n\ndef persist(tmp, path):\n    ops.replace(tmp, path)\n",
            "os.replace",
        ),
        (
            "from os import replace as swap\n\ndef persist(tmp, path):\n    swap(tmp, path)\n",
            "os.replace",
        ),
        ("def persist(tmp, path):\n    tmp.replace(path)\n", "Path.replace"),
        (
            "def persist(dest):\n    dest.with_suffix('.tmp').replace(dest)\n",
            "Path.replace",
        ),
        (
            "import tempfile\n\n"
            "def persist(path):\n"
            "    fd, tmp_path = tempfile.mkstemp(dir=path.parent)\n"
            "    tmp_path.replace(path)\n",
            "Path.replace",
        ),
        (
            "import fcntl\n\ndef lock(f):\n    fcntl.flock(f, fcntl.LOCK_EX)\n",
            "flock(LOCK_EX)",
        ),
        (
            "from fcntl import flock, LOCK_EX\n\ndef lock(f):\n    flock(f, LOCK_EX)\n",
            "flock(LOCK_EX)",
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f():\n"
            '    open(Path(get_journal()) / "config" / "schedules.json", "w")\n',
            "open(write)",
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f(facet):\n"
            "    journal = Path(get_journal())\n"
            '    target = journal / "facets" / facet / "facet.json"\n'
            '    target.write_text("x")\n',
            "Path.write_text",
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f():\n"
            '    Path(get_journal(), "talents", "day.jsonl").open("a")\n',
            "Path.open(write)",
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f(day, seg):\n"
            '    (Path(get_journal()) / "chronicle" / day / seg / "x.bin")'
            '.write_bytes(b"x")\n',
            "Path.write_bytes",
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f():\n"
            "    root = get_journal()\n"
            '    with open(Path(root, "config", "x.json"), "w") as fh:\n'
            '        fh.write("x")\n',
            "open(write)",
        ),
    ],
)
def test_scan_source_flags_raw_mechanics(source: str, kind: str) -> None:
    findings = cjm.scan_source(source)
    assert [finding[1] for finding in findings] == [kind]


@pytest.mark.parametrize(
    "source",
    [
        "def clean(s):\n    return s.replace('a', 'b')\n",
        "def clean(dt, tz):\n    return dt.replace(tzinfo=tz)\n",
        "def clean(path):\n    return path.name.replace('_', ' ')\n",
        "import fcntl\n\ndef lock(f):\n    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n",
        "import fcntl\n\ndef unlock(f):\n    fcntl.flock(f, fcntl.LOCK_UN)\n",
        (
            "import tempfile\n"
            "from solstone.think.journal_io import install_file\n\n"
            "def persist(dest):\n"
            "    with tempfile.NamedTemporaryFile(delete=False) as tmp:\n"
            "        tmp.write(b'ok')\n"
            "    install_file(tmp.name, dest)\n"
        ),
        (
            "from solstone.think.journal_io import write_text\n"
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f(x):\n"
            '    write_text(Path(get_journal()) / "facets" / x / "f.json", "data")\n'
        ),
        (
            "from solstone.think import journal_io\n"
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f(x):\n"
            '    journal_io.write_text(Path(get_journal()) / "facets" / x, "data")\n'
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f():\n"
            '    open(Path(get_journal()) / "logs" / "audit.jsonl", "a")\n'
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f():\n"
            '    (Path(get_journal()) / "health" / "service.port")'
            '.write_text("5015")\n'
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f():\n"
            '    open(Path(get_journal()) / "config" / "x.json", "r")\n'
        ),
        (
            "from solstone.think.utils import get_journal\n"
            "from pathlib import Path\n\n"
            "def f():\n"
            '    Path(get_journal()).write_text("x")\n'
        ),
        "def save(path, data):\n    path.write_text(data)\n",
        (
            "def save(path, data):\n"
            '    tmp = path.with_suffix(".tmp")\n'
            "    tmp.write_text(data)\n"
        ),
        (
            "from solstone.think.journal_io import contained_path\n"
            "from solstone.think.utils import get_journal\n\n"
            "def f(x):\n"
            '    p = contained_path(get_journal(), "facets", x)\n'
            '    p.write_text("data")\n'
        ),
        (
            "from solstone.think.utils import day_path\n\n"
            "def f(day):\n"
            '    (day_path(day) / "timeline.json").write_text("x")\n'
        ),
    ],
)
def test_scan_source_ignores_false_positive_surfaces(source: str) -> None:
    assert cjm.scan_source(source) == []


def test_bad_module_exits_one_and_names_file_and_kind(bad_root):
    result = _run(bad_root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "solstone/apps/badapp/routes.py" in result.stderr
    assert "os.replace" in result.stderr


def test_owner_path_is_scanned(owner_root):
    result = _run(owner_root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "solstone/think/entities/saving.py" in result.stderr
    assert "os.replace" in result.stderr


def test_journal_io_home_is_skipped(home_root):
    result = _run(home_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "journal-io-mechanic: pass" in result.stdout


def test_excluded_ops_path_is_skipped(excluded_root):
    result = _run(excluded_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "journal-io-mechanic: pass" in result.stdout


def test_test_modules_are_skipped(test_root):
    result = _run(test_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "journal-io-mechanic: pass" in result.stdout


def test_ratchet_by_file_kind_count(bad_root):
    new, tracked = cjm.evaluate(bad_root, {})
    assert new
    assert tracked == []

    counts = cjm.count_violations(bad_root)
    new_exact, tracked_exact = cjm.evaluate(bad_root, counts)
    assert new_exact == []
    assert tracked_exact

    key = next(iter(counts))
    ratcheted = dict(counts)
    ratcheted[key] = counts[key] - 1
    new_over, _ = cjm.evaluate(bad_root, ratcheted)
    assert new_over


def test_repo_tree_is_green():
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
