# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure nvattest binary path and command construction."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.tlv import SPDM_NONCE_SIZE


@dataclass(frozen=True, slots=True)
class NvattestCommand:
    argv: list[str]
    env: dict[str, str]


def locate_nvattest(nvattest_dir: Path) -> tuple[Path, Path]:
    """Return the nvattest binary and lib directory under an injected install dir."""

    binary = nvattest_dir / "bin" / "nvattest"
    lib_dir = nvattest_dir / "lib"
    if not nvattest_dir.is_dir():
        raise GpuAppraisalError("nvattest_unavailable")
    if not binary.is_file():
        raise GpuAppraisalError("nvattest_unavailable")
    if not lib_dir.is_dir():
        raise GpuAppraisalError("nvattest_unavailable")
    return binary, lib_dir


def build_nvattest_attest_command(
    *,
    nvattest_dir: Path,
    evidence_file: Path,
    owner_nonce: bytes,
    rim_store: str = "remote",
    rim_dir: Path | None = None,
) -> NvattestCommand:
    """Build the nvattest local-verifier attest command."""

    if len(owner_nonce) != SPDM_NONCE_SIZE:
        raise ValueError(f"owner_nonce is {len(owner_nonce)} bytes, expected 32")
    if rim_store not in {"remote", "dir"}:
        raise ValueError("rim_store must be 'remote' or 'dir'")
    if rim_store == "dir" and rim_dir is None:
        raise ValueError("rim_dir is required when rim_store == 'dir'")
    if rim_store == "remote" and rim_dir is not None:
        raise ValueError("rim_dir is only valid when rim_store == 'dir'")

    binary, lib_dir = locate_nvattest(nvattest_dir)
    argv = [
        str(binary),
        "--format",
        "json",
        "attest",
        "--device",
        "gpu",
        "--gpu-evidence-source",
        "file",
        "--gpu-evidence-file",
        str(evidence_file),
        "--verifier",
        "local",
        "--rim-store",
        rim_store,
    ]
    if rim_dir is not None:
        argv.extend(["--rim-dir", str(rim_dir)])
    argv.extend(["--nonce", owner_nonce.hex()])
    return NvattestCommand(
        argv=argv, env={**os.environ, "LD_LIBRARY_PATH": str(lib_dir)}
    )
