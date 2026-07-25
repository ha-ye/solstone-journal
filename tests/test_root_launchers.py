# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX launcher tests")

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_DIR = REPO_ROOT / "scripts" / "root-launchers"
REINSTALL = "Reinstall solstone and solstone-core."


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _copy_launcher(bin_dir: Path, name: str = "sol") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / name
    shutil.copy2(LAUNCHER_DIR / name, launcher)
    _make_executable(launcher)
    return launcher


def _write_core(bin_dir: Path, *, executable: bool = True) -> Path:
    core = bin_dir / "solstone-core"
    core.write_text(
        "#!/bin/sh\n"
        "printf 'cwd=%s\\n' \"$(pwd)\"\n"
        "printf 'argv='\n"
        'for arg in "$@"; do printf \'<%s>\' "$arg"; done\n'
        "printf '\\nstdin='\n"
        "cat\n"
        "printf 'stub stderr\\n' >&2\n"
        'exit "${STUB_EXIT:-23}"\n',
        encoding="utf-8",
    )
    if executable:
        _make_executable(core)
    else:
        core.chmod(0o644)
    return core


def _run(
    program: Path,
    args: list[str | bytes] | None = None,
    *,
    cwd: Path | None = None,
    stdin: bytes = b"payload",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [program, *(args or [])],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        check=False,
    )


def _run_with_argv0(
    launcher_source: Path,
    argv0: Path,
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["/bin/sh", "-c", '. "$1"', argv0, launcher_source],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        check=False,
    )


def _arg_bytes(arg: str | bytes) -> bytes:
    if isinstance(arg, bytes):
        return arg
    return os.fsencode(arg)


def _stdout(cwd: Path, args: list[str | bytes], stdin: bytes = b"payload") -> bytes:
    argv = b"".join(b"<" + _arg_bytes(arg) + b">" for arg in args)
    return f"cwd={cwd}\n".encode() + b"argv=" + argv + b"\nstdin=" + stdin


def _error(prefix: str, detail: str) -> bytes:
    return f"{prefix}: native launcher {detail}. {REINSTALL}\n".encode()


def _missing_core_error(prefix: str, core: Path) -> bytes:
    return (
        f"{prefix}: native solstone-core sibling is missing: {core}. {REINSTALL}\n"
    ).encode()


def _non_executable_core_error(prefix: str, core: Path) -> bytes:
    return (
        f"{prefix}: native solstone-core sibling is not executable: {core}. "
        f"{REINSTALL}\n"
    ).encode()


def test_launcher_files_differ_only_by_identity_line() -> None:
    sol = (LAUNCHER_DIR / "sol").read_bytes().splitlines()
    solstone = (LAUNCHER_DIR / "solstone").read_bytes().splitlines()

    diffs = [
        (index, left, right)
        for index, (left, right) in enumerate(zip(sol, solstone), start=1)
        if left != right
    ]

    assert len(sol) == len(solstone)
    assert diffs == [(4, b"id=sol", b"id=solstone")]


def test_direct_invocation_preserves_process_contract(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    launcher = _copy_launcher(bin_dir)
    _write_core(bin_dir)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    args: list[str | bytes] = ["one", "two words", b"raw-\xff"]

    result = _run(launcher, args, cwd=cwd)

    assert result.returncode == 23
    assert result.stdout == _stdout(
        cwd,
        ["__solstone_identity=sol", *args],
    )
    assert result.stderr == b"stub stderr\n"


def test_solstone_launcher_uses_solstone_identity(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    launcher = _copy_launcher(bin_dir, "solstone")
    _write_core(bin_dir)

    result = _run(launcher, ["status"], cwd=tmp_path)

    assert result.returncode == 23
    assert result.stdout == _stdout(
        tmp_path,
        ["__solstone_identity=solstone", "status"],
    )
    assert result.stderr == b"stub stderr\n"


def test_exit_status_is_propagated(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    launcher = _copy_launcher(bin_dir)
    _write_core(bin_dir)
    env = dict(os.environ)
    env["STUB_EXIT"] = "42"

    result = _run(launcher, cwd=tmp_path, env=env)

    assert result.returncode == 42
    assert result.stdout == _stdout(tmp_path, ["__solstone_identity=sol"])
    assert result.stderr == b"stub stderr\n"


def test_absolute_multi_hop_symlink_resolves_to_launcher_dir(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    launcher = _copy_launcher(bin_dir)
    _write_core(bin_dir)
    links = tmp_path / "links"
    links.mkdir()
    second = links / "second"
    first = links / "first"
    second.symlink_to(launcher)
    first.symlink_to(second)

    result = _run(first, ["ok"], cwd=tmp_path)

    assert result.returncode == 23
    assert result.stdout == _stdout(tmp_path, ["__solstone_identity=sol", "ok"])
    assert result.stderr == b"stub stderr\n"


def test_relative_multi_hop_symlink_resolves_to_launcher_dir(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    launcher = _copy_launcher(bin_dir)
    _write_core(bin_dir)
    rel1 = tmp_path / "rel1"
    rel2 = tmp_path / "rel2"
    rel1.mkdir()
    rel2.mkdir()
    second = rel2 / "second"
    first = rel1 / "first"
    second.symlink_to(Path("..") / "bin" / launcher.name)
    first.symlink_to(Path("..") / "rel2" / second.name)

    result = _run(first, ["ok"], cwd=tmp_path)

    assert result.returncode == 23
    assert result.stdout == _stdout(tmp_path, ["__solstone_identity=sol", "ok"])
    assert result.stderr == b"stub stderr\n"


def test_path_containing_space_resolves(tmp_path: Path) -> None:
    bin_dir = tmp_path / "path with space" / "bin"
    launcher = _copy_launcher(bin_dir)
    _write_core(bin_dir)

    result = _run(launcher, ["ok"], cwd=tmp_path)

    assert result.returncode == 23
    assert result.stdout == _stdout(tmp_path, ["__solstone_identity=sol", "ok"])
    assert result.stderr == b"stub stderr\n"


def test_leading_dash_path_component_resolves(tmp_path: Path) -> None:
    bin_dir = tmp_path / "-leading-dash" / "bin"
    launcher = _copy_launcher(bin_dir)
    _write_core(bin_dir)

    result = _run(launcher, ["ok"], cwd=tmp_path)

    assert result.returncode == 23
    assert result.stdout == _stdout(tmp_path, ["__solstone_identity=sol", "ok"])
    assert result.stderr == b"stub stderr\n"


def test_dangling_link_exits_78_without_stdout(tmp_path: Path) -> None:
    launcher_source = _copy_launcher(tmp_path / "source-bin")
    bin_dir = tmp_path / "bin"
    missing_dir = bin_dir / "missing"
    missing_dir.mkdir(parents=True)
    launcher = bin_dir / "sol"
    launcher.symlink_to(Path("missing") / "sol")
    dangling_target = bin_dir / "missing" / "sol"

    result = _run_with_argv0(launcher_source, launcher, cwd=tmp_path)

    assert result.returncode == 78
    assert result.stdout == b""
    assert result.stderr == _error(
        "sol",
        f"symlink is dangling: {dangling_target}",
    )


def test_link_cycle_exits_78_without_stdout(tmp_path: Path) -> None:
    launcher_source = _copy_launcher(tmp_path / "source-bin")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.symlink_to(second.name)
    second.symlink_to(first.name)

    result = _run_with_argv0(launcher_source, first, cwd=tmp_path)

    assert result.returncode == 78
    assert result.stdout == b""
    assert result.stderr == _error("sol", "symlink cycle detected")


def test_missing_sibling_core_exits_78_without_stdout_and_brands_command(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    launcher = _copy_launcher(bin_dir, "solstone")
    core = bin_dir / "solstone-core"

    result = _run(launcher, cwd=tmp_path)

    assert result.returncode == 78
    assert result.stdout == b""
    assert result.stderr == _missing_core_error("solstone", core)


def test_non_executable_sibling_core_exits_78_without_stdout(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    launcher = _copy_launcher(bin_dir)
    core = _write_core(bin_dir, executable=False)

    result = _run(launcher, cwd=tmp_path)

    assert result.returncode == 78
    assert result.stdout == b""
    assert result.stderr == _non_executable_core_error("sol", core)
