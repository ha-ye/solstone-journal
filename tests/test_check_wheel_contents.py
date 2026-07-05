# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import importlib.util
import sys
import zipfile
from pathlib import Path


def _load_check_wheel_contents():
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "check_wheel_contents.py"
    )
    spec = importlib.util.spec_from_file_location(
        "check_wheel_contents_test", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_wheel(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as wheel:
        for member, data in members.items():
            wheel.writestr(member, data)


def test_check_dist_passes_clean_split(tmp_path: Path):
    check_wheel_contents = _load_check_wheel_contents()
    expected = {"a.onnx": _sha256(b"a"), "b.onnx": _sha256(b"b")}
    _write_wheel(tmp_path / "solstone-1.0.0-py3-none-any.whl", {"pkg/data.txt": b"ok"})
    _write_wheel(
        tmp_path / "solstone_journal_models-1.0.0-py3-none-any.whl",
        {
            "solstone_journal_models/assets/a.onnx": b"a",
            "solstone_journal_models/assets/b.onnx": b"b",
        },
    )

    assert check_wheel_contents.check_dist(tmp_path, expected, 100_000) == []


def test_check_dist_fails_when_base_wheel_carries_onnx(tmp_path: Path):
    check_wheel_contents = _load_check_wheel_contents()
    expected = {"a.onnx": _sha256(b"a")}
    _write_wheel(
        tmp_path / "solstone-1.0.0-py3-none-any.whl",
        {"solstone/observe/assets/a.onnx": b"a"},
    )
    _write_wheel(
        tmp_path / "solstone_journal_models-1.0.0-py3-none-any.whl",
        {"solstone_journal_models/assets/a.onnx": b"a"},
    )

    errors = check_wheel_contents.check_dist(tmp_path, expected, 100_000)

    assert errors
    assert any("base wheel contains ONNX members" in error for error in errors)


def test_check_dist_fails_on_models_digest_mismatch(tmp_path: Path):
    check_wheel_contents = _load_check_wheel_contents()
    expected = {"a.onnx": _sha256(b"expected")}
    _write_wheel(tmp_path / "solstone-1.0.0-py3-none-any.whl", {"pkg/data.txt": b"ok"})
    _write_wheel(
        tmp_path / "solstone_journal_models-1.0.0-py3-none-any.whl",
        {"solstone_journal_models/assets/a.onnx": b"actual"},
    )

    errors = check_wheel_contents.check_dist(tmp_path, expected, 100_000)

    assert errors
    assert any("sha256 mismatch" in error for error in errors)


def test_check_dist_fails_when_models_wheel_missing(tmp_path: Path):
    check_wheel_contents = _load_check_wheel_contents()
    _write_wheel(tmp_path / "solstone-1.0.0-py3-none-any.whl", {"pkg/data.txt": b"ok"})

    errors = check_wheel_contents.check_dist(
        tmp_path, {"a.onnx": _sha256(b"a")}, 100_000
    )

    assert errors
    assert any("no solstone_journal_models wheel found" in error for error in errors)
