# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import scripts.stage_speakers_analyze_runtime as stage

RUNTIME_MEMBER = "onnxruntime/capi/libonnxruntime.so.1.25.0"
LICENSE_MEMBER = "onnxruntime/LICENSE"
THIRD_PARTY_MEMBER = "onnxruntime/ThirdPartyNotices.txt"
RUNTIME_BYTES = b"fixture onnxruntime\n"
LICENSE_BYTES = b"fixture license\n"
THIRD_PARTY_BYTES = b"fixture third party notices\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_wheel(path: Path, members: dict[str, bytes]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as wheel:
        for name, content in members.items():
            wheel.writestr(name, content)
    return stage._sha256_file(path)


def _spec(tmp_path: Path, *, wheel_sha256: str) -> stage.TargetSpec:
    return stage.TargetSpec(
        key="fixture-linux",
        wheel_url=f"https://example.invalid/{tmp_path.name}/onnxruntime-fixture.whl",
        wheel_sha256=wheel_sha256,
        runtime_member=RUNTIME_MEMBER,
        runtime_sha256=_sha256(RUNTIME_BYTES),
        runtime_staged_name="libonnxruntime.so.1",
        link_names=("libonnxruntime.so.1", "libonnxruntime.so"),
        notices=(
            stage.NoticeSpec(
                source_member=LICENSE_MEMBER,
                staged_name="onnxruntime-LICENSE.txt",
                sha256=_sha256(LICENSE_BYTES),
            ),
            stage.NoticeSpec(
                source_member=THIRD_PARTY_MEMBER,
                staged_name="onnxruntime-ThirdPartyNotices.txt",
                sha256=_sha256(THIRD_PARTY_BYTES),
            ),
        ),
    )


def _stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    members: dict[str, bytes] | None = None,
    wheel_sha256: str | None = None,
) -> dict[str, object]:
    cache_dir = tmp_path / "cache"
    wheel_path = cache_dir / "onnxruntime-fixture.whl"
    actual_wheel_sha = _write_wheel(
        wheel_path,
        members
        or {
            RUNTIME_MEMBER: RUNTIME_BYTES,
            LICENSE_MEMBER: LICENSE_BYTES,
            THIRD_PARTY_MEMBER: THIRD_PARTY_BYTES,
        },
    )
    monkeypatch.setattr(stage, "_assert_lock_contains", lambda _spec: None)
    spec = _spec(tmp_path, wheel_sha256=wheel_sha256 or actual_wheel_sha)
    return stage.stage_runtime(
        spec=spec,
        package_dir=tmp_path / "package",
        cache_dir=cache_dir,
        link_root=tmp_path / "link",
        receipt_path=tmp_path / "receipt.json",
        offline=True,
    )


def test_stage_runtime_stages_minimal_library_notices_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _stage(tmp_path, monkeypatch)

    runtime = (
        tmp_path
        / "package"
        / "wheel-data"
        / stage.RUNTIME_INSTALL_DIR
        / "libonnxruntime.so.1"
    )
    license_notice = (
        tmp_path
        / "package"
        / "wheel-data"
        / stage.NOTICE_INSTALL_DIR
        / "onnxruntime-LICENSE.txt"
    )
    third_party_notice = (
        tmp_path
        / "package"
        / "wheel-data"
        / stage.NOTICE_INSTALL_DIR
        / "onnxruntime-ThirdPartyNotices.txt"
    )

    assert runtime.read_bytes() == RUNTIME_BYTES
    assert license_notice.read_bytes() == LICENSE_BYTES
    assert third_party_notice.read_bytes() == THIRD_PARTY_BYTES
    assert (tmp_path / "link" / "fixture-linux" / "libonnxruntime.so.1").exists()
    assert receipt["runtime_library"]["sha256"] == _sha256(RUNTIME_BYTES)
    recorded = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert recorded == receipt


def test_stage_runtime_fails_loudly_on_whole_wheel_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(stage.StageError) as exc:
        _stage(tmp_path, monkeypatch, wheel_sha256="0" * 64)

    assert "cached onnxruntime wheel digest mismatch" in str(exc.value)
    assert (
        "expected: 0000000000000000000000000000000000000000000000000000000000000000"
        in str(exc.value)
    )
    assert "repair:" in str(exc.value)


def test_stage_runtime_fails_loudly_on_missing_expected_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = {
        LICENSE_MEMBER: LICENSE_BYTES,
        THIRD_PARTY_MEMBER: THIRD_PARTY_BYTES,
    }

    with pytest.raises(stage.StageError) as exc:
        _stage(tmp_path, monkeypatch, members=members)

    assert "onnxruntime wheel missing expected member" in str(exc.value)
    assert f"expected: {RUNTIME_MEMBER}" in str(exc.value)


def test_stage_runtime_rejects_gpu_provider_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = {
        RUNTIME_MEMBER: RUNTIME_BYTES,
        LICENSE_MEMBER: LICENSE_BYTES,
        THIRD_PARTY_MEMBER: THIRD_PARTY_BYTES,
        "onnxruntime/capi/libonnxruntime_providers_cuda.so": b"cuda",
    }

    with pytest.raises(stage.StageError) as exc:
        _stage(tmp_path, monkeypatch, members=members)

    assert "forbidden GPU provider members" in str(exc.value)
    assert "libonnxruntime_providers_cuda.so" in str(exc.value)


def test_stage_runtime_fails_loudly_on_notice_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = {
        RUNTIME_MEMBER: RUNTIME_BYTES,
        LICENSE_MEMBER: b"changed license\n",
        THIRD_PARTY_MEMBER: THIRD_PARTY_BYTES,
    }

    with pytest.raises(stage.StageError) as exc:
        _stage(tmp_path, monkeypatch, members=members)

    assert "extracted onnxruntime notice digest mismatch" in str(exc.value)
    assert f"expected: {LICENSE_MEMBER} sha256 {_sha256(LICENSE_BYTES)}" in str(
        exc.value
    )
