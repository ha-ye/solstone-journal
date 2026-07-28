# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Real Zig env contract for release-driver build isolation.

This is intentionally in the unit lane despite AGENTS.md/CLAUDE.md §6's normal
mock-process-boundary rule: it runs only `zig env`, performs no compile, no
network access, and no journal I/O. The release driver needs a real Zig check
because Zig 0.16.0 fails cache resolution under the scrubbed no-HOME env unless
the global cache directory is explicit.

Limitation: `zig cc -v` is not a falsifying probe here. It was measured exiting
0 under the broken env, so this test uses `zig env`, which exercises the cache
resolution path that failed in the release build stack.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import scripts.release_candidate_driver as driver

ZIG_STRING_RE = re.compile(r"\.(?P<key>[A-Za-z_]+)\s*=\s*\"(?P<value>[^\"]*)\"")


def _zig_or_skip() -> str:
    zig = shutil.which("zig")
    if zig is None:
        pytest.skip(
            "zig is not installed; release-driver Zig env contract needs real zig"
        )
    return zig


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _zig_string_value(output: str, key: str) -> str:
    for match in ZIG_STRING_RE.finditer(output):
        if match.group("key") == key:
            return match.group("value")
    raise AssertionError(f"zig env output did not include {key!r}: {output}")


def _resolved_zig_path(value: str, cwd: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def test_scrubbed_build_env_makes_zig_cache_resolution_explicit(
    tmp_path: Path,
) -> None:
    zig = _zig_or_skip()
    env = driver._scrubbed_build_env(
        tmp_path,
        driver.CORE_X86_64_MATURIN_ARGS,
        None,
    )
    cache_root = tmp_path / "target" / "release-zig-cache"
    expected_global = (cache_root / "zig-global").resolve()
    expected_local = (cache_root / "zig-local").resolve()

    result = subprocess.run(
        [zig, "env"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = _combined_output(result)
    assert result.returncode == 0, output
    assert _zig_string_value(output, "ZIG_GLOBAL_CACHE_DIR") == str(expected_global)
    assert _zig_string_value(output, "ZIG_LOCAL_CACHE_DIR") == str(expected_local)
    assert (
        _resolved_zig_path(_zig_string_value(output, "global_cache_dir"), tmp_path)
        == expected_global
    )


def test_scrubbed_build_env_without_zig_cache_keys_fails_zig_resolution(
    tmp_path: Path,
) -> None:
    zig = _zig_or_skip()
    env = driver._scrubbed_build_env(
        tmp_path,
        driver.CORE_X86_64_MATURIN_ARGS,
        None,
    )
    stripped_env = {
        key: value for key, value in env.items() if not key.startswith("ZIG_")
    }

    result = subprocess.run(
        [zig, "env"],
        cwd=tmp_path,
        env=stripped_env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = _combined_output(result)
    assert result.returncode != 0, output
    assert "AppDataDirUnavailable" in output
