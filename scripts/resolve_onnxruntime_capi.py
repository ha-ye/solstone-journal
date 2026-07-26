#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Prepare ONNX Runtime C API linkage for Rust speaker tests."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PYTHON = ROOT / ".venv" / "bin" / "python"
MIN_ONNXRUNTIME_VERSION = (1, 24, 0)
ABSENT_MESSAGE = (
    "ONNX Runtime C API unavailable: .venv/bin/python could not import the "
    "onnxruntime module or find its capi library. Run 'make install' from the "
    "repo root; it installs the journal runtime dependency, using "
    "onnxruntime-gpu on CUDA hosts and onnxruntime otherwise."
)
TOO_OLD_TEMPLATE = (
    "ONNX Runtime C API too old: found {version} at {capi}; "
    "solstone-core-speakers-onnx requires C API 24, provided by ONNX Runtime "
    ">=1.24.0. Run 'make install' from the repo root to refresh the journal "
    "runtime dependency."
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class OnnxRuntimeProbe:
    version: str
    module_file: Path
    capi_dir: Path
    platform_tag: str
    system: str


@dataclass(frozen=True)
class PreparedOnnxRuntime:
    probe: OnnxRuntimeProbe
    link_dir: Path
    runtime_library: Path
    staged_links: tuple[Path, ...]


class OnnxRuntimeResolverError(RuntimeError):
    """ONNX Runtime C API resolution failed."""


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise OnnxRuntimeResolverError(
            TOO_OLD_TEMPLATE.format(version=version, capi="<unknown>")
        )
    return tuple(int(part) for part in match.groups())


def _probe_code() -> str:
    return (
        "import json, pathlib, platform, sysconfig\n"
        "import onnxruntime\n"
        "module = pathlib.Path(onnxruntime.__file__).resolve()\n"
        "capi = module.parent / 'capi'\n"
        "print(json.dumps({\n"
        "  'version': onnxruntime.__version__,\n"
        "  'module_file': str(module),\n"
        "  'capi_dir': str(capi),\n"
        "  'platform_tag': sysconfig.get_platform(),\n"
        "  'system': platform.system(),\n"
        "}, sort_keys=True))\n"
    )


def probe_onnxruntime(
    python: Path,
    *,
    runner: Runner = subprocess.run,
) -> OnnxRuntimeProbe:
    if not python.exists():
        raise OnnxRuntimeResolverError(ABSENT_MESSAGE)
    result = runner(
        [str(python), "-c", _probe_code()],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OnnxRuntimeResolverError(ABSENT_MESSAGE)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OnnxRuntimeResolverError(ABSENT_MESSAGE) from exc
    return OnnxRuntimeProbe(
        version=str(payload["version"]),
        module_file=Path(payload["module_file"]),
        capi_dir=Path(payload["capi_dir"]),
        platform_tag=str(payload["platform_tag"]),
        system=str(payload["system"]),
    )


def _require_supported_version(probe: OnnxRuntimeProbe) -> None:
    version_tuple = _version_tuple(probe.version)
    if version_tuple < MIN_ONNXRUNTIME_VERSION:
        raise OnnxRuntimeResolverError(
            TOO_OLD_TEMPLATE.format(version=probe.version, capi=probe.capi_dir)
        )


def _find_runtime_library(probe: OnnxRuntimeProbe) -> Path:
    capi = probe.capi_dir
    if not capi.is_dir():
        raise OnnxRuntimeResolverError(ABSENT_MESSAGE)
    if probe.system == "Linux":
        exact = capi / f"libonnxruntime.so.{probe.version}"
        candidates = (
            [exact] if exact.exists() else sorted(capi.glob("libonnxruntime.so.*"))
        )
    elif probe.system == "Darwin":
        exact = capi / f"libonnxruntime.{probe.version}.dylib"
        candidates = (
            [exact] if exact.exists() else sorted(capi.glob("libonnxruntime.*.dylib"))
        )
    else:
        raise OnnxRuntimeResolverError(
            f"ONNX Runtime C API unsupported platform for Rust tests: {probe.system}"
        )
    if not candidates:
        raise OnnxRuntimeResolverError(ABSENT_MESSAGE)
    return candidates[0].resolve()


def _platform_component(platform_tag: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", platform_tag).strip("-")
    return cleaned or "unknown"


def _replace_symlink(link: Path, target: Path) -> None:
    if link.is_symlink() or link.exists():
        if not link.is_symlink():
            raise OnnxRuntimeResolverError(
                f"ONNX Runtime link target exists and is not a symlink: {link}"
            )
        link.unlink()
    link.symlink_to(target)


def prepare_onnxruntime(
    root: Path,
    python: Path,
    *,
    runner: Runner = subprocess.run,
) -> PreparedOnnxRuntime:
    probe = probe_onnxruntime(python, runner=runner)
    _require_supported_version(probe)
    runtime_library = _find_runtime_library(probe)
    link_dir = (
        root
        / "core"
        / "target"
        / "onnxruntime-link"
        / _platform_component(probe.platform_tag)
        / "lib"
    )
    link_dir.mkdir(parents=True, exist_ok=True)
    if probe.system == "Linux":
        link_names = (
            "libonnxruntime.so",
            "libonnxruntime.so.1",
            runtime_library.name,
        )
    elif probe.system == "Darwin":
        link_names = ("libonnxruntime.dylib", runtime_library.name)
    else:
        raise OnnxRuntimeResolverError(
            f"ONNX Runtime C API unsupported platform for Rust tests: {probe.system}"
        )
    links = tuple(link_dir / name for name in link_names)
    for link in links:
        _replace_symlink(link, runtime_library)
    return PreparedOnnxRuntime(
        probe=probe,
        link_dir=link_dir,
        runtime_library=runtime_library,
        staged_links=links,
    )


def env_for_cargo(
    prepared: PreparedOnnxRuntime,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    link_dir = str(prepared.link_dir)
    env["ORT_LIB_PATH"] = link_dir
    env["ORT_PREFER_DYNAMIC_LINK"] = "true"
    if prepared.probe.system == "Linux":
        key = "LD_LIBRARY_PATH"
    elif prepared.probe.system == "Darwin":
        key = "DYLD_LIBRARY_PATH"
    else:
        key = ""
    if key:
        existing = env.get(key)
        env[key] = link_dir if not existing else f"{link_dir}{os.pathsep}{existing}"
    return env


def _json_payload(prepared: PreparedOnnxRuntime) -> str:
    return json.dumps(
        {
            "capi_dir": str(prepared.probe.capi_dir),
            "link_dir": str(prepared.link_dir),
            "runtime_library": str(prepared.runtime_library),
            "staged_links": [str(path) for path in prepared.staged_links],
            "system": prepared.probe.system,
            "version": prepared.probe.version,
        },
        sort_keys=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare ONNX Runtime C API linkage for Rust speaker tests."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--json", action="store_true", help="Print resolver details.")
    parser.add_argument("--exec", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    exec_command: list[str] | None = None
    if "--exec" in raw_argv:
        index = raw_argv.index("--exec")
        exec_command = raw_argv[index + 1 :]
        raw_argv = raw_argv[: index + 1]
        if exec_command and exec_command[0] == "--":
            exec_command = exec_command[1:]
    args = build_parser().parse_args(raw_argv)
    try:
        prepared = prepare_onnxruntime(args.root.resolve(), args.python)
    except OnnxRuntimeResolverError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(_json_payload(prepared))
    if args.exec:
        command = exec_command or []
        if not command:
            print("--exec requires a command", file=sys.stderr)
            return 2
        result = subprocess.run(command, cwd=args.root, env=env_for_cargo(prepared))
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
