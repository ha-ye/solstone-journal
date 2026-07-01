# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from solstone.think import parakeet_readiness

ROOT = Path(__file__).resolve().parent.parent


def test_parakeet_readiness_imports_stdlib_only() -> None:
    source = ROOT / "solstone" / "think" / "parakeet_readiness.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    unexpected = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module] if node.module else []
        else:
            continue
        for module in modules:
            root = module.split(".", 1)[0]
            if root not in allowed:
                unexpected.append(module)

    assert unexpected == []


@pytest.mark.parametrize(
    ("arch", "expected"),
    [
        ("amd64", "x86_64-unknown-linux-gnu"),
        ("x64", "x86_64-unknown-linux-gnu"),
        ("x86_64", "x86_64-unknown-linux-gnu"),
        ("arm64", "aarch64-unknown-linux-gnu"),
        ("aarch64", "aarch64-unknown-linux-gnu"),
    ],
)
def test_parakeet_cpp_artifact_key_linux_arches(arch: str, expected: str) -> None:
    assert parakeet_readiness.parakeet_cpp_artifact_key("linux", arch) == expected


def test_parakeet_cpp_artifact_key_rejects_non_linux() -> None:
    with pytest.raises(RuntimeError, match="unsupported"):
        parakeet_readiness.parakeet_cpp_artifact_key("darwin", "arm64")


def test_check_parakeet_cpp_files_reports_missing_and_ready(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    artifact_key = "x86_64-unknown-linux-gnu"

    with pytest.raises(RuntimeError, match="binary_cpu missing"):
        parakeet_readiness.check_parakeet_cpp_files(cache_root, artifact_key)

    cpu = parakeet_readiness.parakeet_cpp_binary_path(cache_root, artifact_key, "cpu")
    cpu.parent.mkdir(parents=True)
    cpu.write_text("cpu\n", encoding="utf-8")
    cpu.chmod(0o755)

    with pytest.raises(RuntimeError, match="binary_vulkan missing"):
        parakeet_readiness.check_parakeet_cpp_files(cache_root, artifact_key)

    vulkan = parakeet_readiness.parakeet_cpp_binary_path(
        cache_root, artifact_key, "vulkan"
    )
    vulkan.parent.mkdir(parents=True)
    vulkan.write_text("vulkan\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="binary_vulkan not executable"):
        parakeet_readiness.check_parakeet_cpp_files(cache_root, artifact_key)

    vulkan.chmod(0o755)

    with pytest.raises(RuntimeError, match="model missing"):
        parakeet_readiness.check_parakeet_cpp_files(cache_root, artifact_key)

    model = parakeet_readiness.parakeet_cpp_model_path(cache_root)
    model.parent.mkdir(parents=True)
    model.write_text("model\n", encoding="utf-8")

    assert parakeet_readiness.check_parakeet_cpp_files(cache_root, artifact_key) == {
        "binary_cpu": cpu,
        "binary_vulkan": vulkan,
        "model": model,
    }
