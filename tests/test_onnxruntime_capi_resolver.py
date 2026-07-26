# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import scripts.resolve_onnxruntime_capi as resolver


def _python(tmp_path: Path) -> Path:
    path = tmp_path / "python"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _runner(payload: dict[str, str]):
    def run(argv, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    return run


def _payload(
    capi: Path,
    *,
    version: str = "1.25.0",
    platform_tag: str = "linux-x86_64",
    system: str = "Linux",
) -> dict[str, str]:
    return {
        "version": version,
        "module_file": str(capi.parent / "__init__.py"),
        "capi_dir": str(capi),
        "platform_tag": platform_tag,
        "system": system,
    }


def test_missing_venv_python_reports_actionable_cuda_safe_message(
    tmp_path: Path,
) -> None:
    with pytest.raises(resolver.OnnxRuntimeResolverError) as excinfo:
        resolver.prepare_onnxruntime(tmp_path, tmp_path / "missing-python")

    assert str(excinfo.value) == resolver.ABSENT_MESSAGE


def test_missing_capi_reports_actionable_cuda_safe_message(tmp_path: Path) -> None:
    python = _python(tmp_path)
    capi = tmp_path / "venv" / "onnxruntime" / "capi"

    with pytest.raises(resolver.OnnxRuntimeResolverError) as excinfo:
        resolver.prepare_onnxruntime(
            tmp_path,
            python,
            runner=_runner(_payload(capi)),
        )

    assert str(excinfo.value) == resolver.ABSENT_MESSAGE


def test_too_old_runtime_reports_required_api_level(tmp_path: Path) -> None:
    python = _python(tmp_path)
    capi = tmp_path / "venv" / "onnxruntime" / "capi"
    capi.mkdir(parents=True)
    (capi / "libonnxruntime.so.1.23.0").write_bytes(b"runtime")

    with pytest.raises(resolver.OnnxRuntimeResolverError) as excinfo:
        resolver.prepare_onnxruntime(
            tmp_path,
            python,
            runner=_runner(_payload(capi, version="1.23.0")),
        )

    assert str(excinfo.value) == resolver.TOO_OLD_TEMPLATE.format(
        version="1.23.0",
        capi=capi,
    )


def test_linux_stages_linker_and_soname_symlinks(tmp_path: Path) -> None:
    python = _python(tmp_path)
    capi = tmp_path / "venv" / "onnxruntime" / "capi"
    capi.mkdir(parents=True)
    runtime = capi / "libonnxruntime.so.1.25.0"
    runtime.write_bytes(b"runtime")

    prepared = resolver.prepare_onnxruntime(
        tmp_path,
        python,
        runner=_runner(_payload(capi)),
    )

    link_dir = (
        tmp_path / "core" / "target" / "onnxruntime-link" / "linux-x86_64" / "lib"
    )
    assert prepared.link_dir == link_dir
    assert {path.name for path in prepared.staged_links} == {
        "libonnxruntime.so",
        "libonnxruntime.so.1",
        "libonnxruntime.so.1.25.0",
    }
    for link in prepared.staged_links:
        assert link.is_symlink()
        assert link.resolve() == runtime

    env = resolver.env_for_cargo(prepared, {"LD_LIBRARY_PATH": "/existing"})
    assert env["ORT_LIB_PATH"] == str(link_dir)
    assert env["ORT_PREFER_DYNAMIC_LINK"] == "true"
    assert env["LD_LIBRARY_PATH"] == f"{link_dir}{os.pathsep}/existing"


def test_macos_stages_linker_and_install_name_symlinks(tmp_path: Path) -> None:
    python = _python(tmp_path)
    capi = tmp_path / "venv" / "onnxruntime" / "capi"
    capi.mkdir(parents=True)
    runtime = capi / "libonnxruntime.1.25.0.dylib"
    runtime.write_bytes(b"runtime")

    prepared = resolver.prepare_onnxruntime(
        tmp_path,
        python,
        runner=_runner(
            _payload(
                capi,
                platform_tag="macosx-14.0-arm64",
                system="Darwin",
            )
        ),
    )

    link_dir = (
        tmp_path / "core" / "target" / "onnxruntime-link" / "macosx-14.0-arm64" / "lib"
    )
    assert prepared.link_dir == link_dir
    assert {path.name for path in prepared.staged_links} == {
        "libonnxruntime.dylib",
        "libonnxruntime.1.25.0.dylib",
    }
    for link in prepared.staged_links:
        assert link.is_symlink()
        assert link.resolve() == runtime

    env = resolver.env_for_cargo(prepared, {"DYLD_LIBRARY_PATH": "/existing"})
    assert env["ORT_LIB_PATH"] == str(link_dir)
    assert env["ORT_PREFER_DYNAMIC_LINK"] == "true"
    assert env["DYLD_LIBRARY_PATH"] == f"{link_dir}{os.pathsep}/existing"
