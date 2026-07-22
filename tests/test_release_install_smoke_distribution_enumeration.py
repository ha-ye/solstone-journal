# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Real distribution enumeration coverage for install-proof isolation.

This is intentionally in the unit lane despite AGENTS.md/CLAUDE.md §6's normal
mock-process-boundary rule: it creates local scratch venvs, installs tiny local
wheels with --no-index --no-deps, performs no network access, and touches no
journal state. The install proof needs a real importlib.metadata probe because
Fedora/RHEL venvs can expose one site-packages directory under both lib64 and
lib spellings. Each test chdirs to tmp_path because python -c puts cwd on
sys.path, and the repo root's editable solstone.egg-info would otherwise be
enumerated as a real solstone distribution.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import scripts.release_install_smoke as smoke


def _env_python(env_root: Path) -> Path:
    if sys.platform == "win32":
        return env_root / "Scripts" / "python.exe"
    return env_root / "bin" / "python"


def _run(
    argv: Sequence[Path | str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(token) for token in argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _record_hash(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"sha256={encoded}"


def _write_py3_wheel(wheel_dir: Path, distribution: str, version: str) -> Path:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    wheel_path = wheel_dir / f"{normalized}-{version}-py3-none-any.whl"
    members = {
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n".encode()
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: solstone test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    rows = [
        f"{name},{_record_hash(content)},{len(content)}"
        for name, content in members.items()
    ]
    rows.append(f"{dist_info}/RECORD,,")
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, content in members.items():
            wheel.writestr(name, content)
        wheel.writestr(f"{dist_info}/RECORD", "\n".join(rows) + "\n")
    return wheel_path


def _create_venv(interpreter: Path, env_root: Path) -> bool:
    script = (
        "import os, venv; "
        "venv.EnvBuilder(with_pip=True, symlinks=False).create(os.environ['ENV_ROOT'])"
    )
    env = {**os.environ, "ENV_ROOT": str(env_root)}
    result = _run((interpreter, "-c", script), cwd=env_root.parent, env=env)
    return result.returncode == 0


def _candidate_interpreters() -> tuple[Path, ...]:
    raw = [Path("/usr/bin/python3")]
    discovered = shutil.which("python3")
    if discovered is not None:
        raw.append(Path(discovered))
    raw.append(Path(sys.executable))

    selected: list[Path] = []
    seen: set[str] = set()
    for candidate in raw:
        if not candidate.exists() or not os.access(candidate, os.X_OK):
            continue
        real = os.path.realpath(candidate)
        if real in seen:
            continue
        seen.add(real)
        selected.append(candidate)
    return tuple(selected)


def _site_paths(env_python: Path, *, cwd: Path) -> tuple[str, ...] | None:
    script = (
        "import json, sys; "
        "print(json.dumps([path for path in sys.path if 'site-packages' in path]))"
    )
    result = _run(
        (env_python, "-c", script),
        cwd=cwd,
        env=dict(smoke.SCRUBBED_COMMAND_ENV),
    )
    if result.returncode != 0:
        return None
    return tuple(json.loads(result.stdout))


def _has_dual_spelling_site_paths(paths: Sequence[str]) -> bool:
    by_realpath: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        by_realpath[os.path.realpath(path)].add(path)
    return any(len(spellings) > 1 for spellings in by_realpath.values())


def _select_dual_spelling_venv(tmp_path: Path) -> Path:
    for index, interpreter in enumerate(_candidate_interpreters()):
        env_root = tmp_path / f"probe-{index}"
        if not _create_venv(interpreter, env_root):
            continue
        paths = _site_paths(_env_python(env_root), cwd=tmp_path)
        if paths is not None and _has_dual_spelling_site_paths(paths):
            return env_root

    pytest.skip(
        "no candidate interpreter produced dual-spelling site-packages topology"
    )


def _wrapper_for_pythonpath(
    path: Path,
    site_paths: Sequence[Path],
) -> Path:
    wrapper = path / "python-wrapper"
    pythonpath = ":".join(str(site_path) for site_path in site_paths)
    wrapper.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(pythonpath)} "
        f'exec {shlex.quote(sys.executable)} -S "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper


def _write_dist_info(site_path: Path, distribution: str, version: str) -> Path:
    normalized = distribution.replace("-", "_")
    dist_info = site_path / f"{normalized}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n"
    )
    return dist_info


def _distribution_pairs(
    observed: Sequence[Mapping[str, str]],
) -> Counter[tuple[str, str]]:
    return Counter((str(entry["name"]), str(entry["version"])) for entry in observed)


def test_solstone_distributions_deduplicates_real_dual_spelling_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    env_root = _select_dual_spelling_venv(tmp_path)
    env_python = _env_python(env_root)
    wheel_dir = tmp_path / "wheels"
    expected = {
        ("solstone-probe-one", "0.1.0"),
        ("solstone-probe-two", "0.2.0"),
        ("solstone-probe-three", "0.3.0"),
        ("solstone-probe-four", "0.4.0"),
    }
    wheels = [
        _write_py3_wheel(wheel_dir, distribution, version)
        for distribution, version in sorted(expected)
    ]

    result = _run(
        (env_python, "-m", "pip", "install", "--no-index", "--no-deps", *wheels),
        cwd=tmp_path,
        env=dict(smoke.SCRUBBED_COMMAND_ENV),
    )

    assert result.returncode == 0, result.stderr or result.stdout
    observed = smoke._solstone_distributions(env_python)
    assert len(observed) == 4
    assert _distribution_pairs(observed) == Counter({pair: 1 for pair in expected})


def test_solstone_distributions_deduplicates_symlinked_dist_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    site = tmp_path / "site"
    alias = tmp_path / "site-alias"
    _write_dist_info(site, "solstone-symlinked", "1.0.0")
    alias.symlink_to(site, target_is_directory=True)
    wrapper = _wrapper_for_pythonpath(tmp_path, (site, alias))

    observed = smoke._solstone_distributions(wrapper)

    assert observed == ({"name": "solstone-symlinked", "version": "1.0.0"},)


def test_solstone_distributions_preserves_distinct_duplicate_dist_infos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    site_one = tmp_path / "site-one"
    site_two = tmp_path / "site-two"
    _write_dist_info(site_one, "solstone", "1.0.0")
    _write_dist_info(site_two, "solstone", "1.0.0")
    wrapper = _wrapper_for_pythonpath(tmp_path, (site_one, site_two))

    observed = smoke._solstone_distributions(wrapper)

    assert len(observed) == 2
    assert _distribution_pairs(observed) == Counter({("solstone", "1.0.0"): 2})
