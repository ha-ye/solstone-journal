# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import os
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOMBSTONE_DIR = REPO_ROOT / "scripts" / "journal-host-tombstone"


def test_tombstone_metadata_prep_fails_with_migration_message(tmp_path):
    env = os.environ.copy()
    env["SOLSTONE_TOMBSTONE_ALLOW_BUILD"] = "1"
    subprocess.run(
        [sys.executable, "setup.py", "sdist", "-d", str(tmp_path)],
        cwd=TOMBSTONE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    archive = next(tmp_path.glob("solstone_journal_host-*.tar.gz"))
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(extract_dir, filter="data")
    source_dir = next(path for path in extract_dir.iterdir() if path.is_dir())

    no_allow_env = os.environ.copy()
    no_allow_env.pop("SOLSTONE_TOMBSTONE_ALLOW_BUILD", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; os.makedirs('meta', exist_ok=True); "
            "from setuptools import build_meta as b; "
            "b.prepare_metadata_for_build_wheel('meta')",
        ],
        cwd=source_dir,
        env=no_allow_env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert (
        "solstone[journal] and solstone-journal-host have moved."
        in result.stdout + result.stderr
    )


def test_root_extras_pin_matches_tombstone_version():
    setup_text = (TOMBSTONE_DIR / "setup.py").read_text(encoding="utf-8")
    match = re.search(r'^TOMBSTONE_VERSION = "([^"]+)"', setup_text, re.MULTILINE)
    assert match is not None
    version = match.group(1)
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    extras = pyproject["project"]["optional-dependencies"]

    assert extras["journal"] == ["solstone-journal-host==" + version]
    assert extras["journal-cuda"] == ["solstone-journal-host==" + version]
    assert '"solstone-journal-host"' in setup_text
