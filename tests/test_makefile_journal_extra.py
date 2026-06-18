# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Guard: `make install` selects the CUDA transcription bundle only on x86_64.

The Makefile derives JOURNAL_EXTRA from PARAKEET_ONNX_VARIANT, auto-detected
from the host (`uname -m` + `nvidia-smi`). The CUDA variant resolves
onnxruntime-gpu, which ships NO aarch64 wheel on PyPI — so an aarch64 NVIDIA
host (DGX Spark / GB10) that picked `cuda` would die in the `.installed`
`uv sync` before the per-arch `install` guard ever runs. This test pins the
arch -> extra mapping so the x86_64 gate can't silently regress (the build-
manifest analog of test_llama_server_pins_cover_expected_platforms).

It resolves JOURNAL_EXTRA the way the Makefile actually would on a given host
by shimming `uname` and `nvidia-smi` onto PATH and asking make to expand the
variable through a tiny `include`-the-real-Makefile helper (portable across
GNU make 3.81+, no `--eval` dependency).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
REAL_UNAME = shutil.which("uname") or "/usr/bin/uname"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")


def _shim(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _resolve_journal_extra(arch: str, *, nvidia: bool) -> str:
    """Return the Makefile's JOURNAL_EXTRA as it would resolve on a host whose
    `uname -m` is ``arch`` and where `nvidia-smi -L` does (``nvidia``) or does
    not exit 0 — both faked via PATH shims."""
    with tempfile.TemporaryDirectory() as d:
        bindir = Path(d)
        # `uname -m` -> faked arch; every other uname call (e.g. `-s`, used at
        # parse time) delegates to the real binary so the Makefile still parses.
        _shim(
            bindir / "uname",
            f'#!/bin/sh\nif [ "$1" = "-m" ]; then echo "{arch}"; '
            f'else exec "{REAL_UNAME}" "$@"; fi\n',
        )
        _shim(
            bindir / "nvidia-smi",
            "#!/bin/sh\nexit 0\n" if nvidia else "#!/bin/sh\nexit 127\n",
        )
        # No-op uv shim: keeps the parse-time `ifndef UV` guard satisfied even
        # where uv isn't installed. The print target never invokes it.
        _shim(bindir / "uv", "#!/bin/sh\nexit 0\n")
        # Emit the value on its own marker line so extraction tolerates any
        # surrounding decoration make may add to stdout.
        helper = bindir / "print-journal-extra.mk"
        helper.write_text(
            f"include {MAKEFILE}\n"
            "__journal_extra__:\n"
            "\t@printf 'JOURNAL_EXTRA=[%s]\\n' '$(JOURNAL_EXTRA)'\n",
            encoding="utf-8",
        )
        # Run the nested make hermetically: drop any recursive-make state the
        # parent (e.g. `make ci`) exported, or it inherits the jobserver and
        # prints `Entering/Leaving directory` onto our captured stdout.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "MAKEOVERRIDES")
        }
        env["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", str(helper), "__journal_extra__"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        match = re.search(r"^JOURNAL_EXTRA=\[(.*)\]$", result.stdout, re.MULTILINE)
        assert match is not None, (
            f"could not read JOURNAL_EXTRA from make output\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        return match.group(1)


@pytest.mark.parametrize(
    ("arch", "nvidia", "expected"),
    [
        # x86_64 + NVIDIA is the ONLY cell that gets the CUDA (onnxruntime-gpu)
        # bundle — the existing x86_64 install path with its CUDA validation.
        ("x86_64", True, "journal-cuda"),
        ("x86_64", False, "journal"),
        # aarch64 NVIDIA (DGX Spark / GB10) must fall to the CPU bundle: there
        # is no aarch64 onnxruntime-gpu wheel. This is the regression the gate
        # prevents — `nvidia-smi` succeeding must NOT force `cuda` here.
        ("aarch64", True, "journal"),
        ("aarch64", False, "journal"),
        # macOS arm64 stays on the CPU bundle (GPU STT rides parakeet-helper);
        # nvidia-smi is absent there, but the gate holds even if it weren't.
        ("arm64", True, "journal"),
        ("arm64", False, "journal"),
    ],
)
def test_journal_extra_selects_cuda_only_on_x86_64(
    arch: str, nvidia: bool, expected: str
) -> None:
    assert _resolve_journal_extra(arch, nvidia=nvidia) == expected
