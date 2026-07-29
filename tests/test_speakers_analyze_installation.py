# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the speakers-analyze startup installation invariant."""

from __future__ import annotations

import json
import os
import secrets
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from solstone.think import probe
from solstone.think import speakers_analyze_installation as installation
from solstone.think.journal_io.lease import (
    BorrowedFileLease,
    FileLease,
    acquire_file_lease,
    read_file_lease_fd,
    read_file_lease_offset_token,
    set_file_lease_offset_token,
)

_GENERATION_ENV_KEYS = (
    installation.GENERATION_ENV_KEY,
    installation.GENERATION_FD_ENV_KEY,
    installation.GENERATION_TOKEN_ENV_KEY,
)
_GENERATION_ENV_SENTINEL = "__solstone_generation_env_unset__"


def _version_reader(dist_name: str) -> str:
    if dist_name in {
        installation.ROOT_DIST_NAME,
        installation.HELPER_DIST_NAME,
        installation.MODELS_DIST_NAME,
    }:
        return "1.0.18"
    raise PackageNotFoundError(dist_name)


def _platform_reader() -> probe.CorePlatform:
    return ("linux", "x86_64")


def _platform_tags() -> set[str]:
    return {"manylinux_2_27_x86_64"}


def _helper(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    executable = bin_dir / "python"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    helper = bin_dir / installation.HELPER_BINARY_NAME
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    return executable


def _asset_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wespeaker = tmp_path / "wespeaker.onnx"
    pyannote = tmp_path / "pyannote.onnx"
    wespeaker.write_bytes(b"wespeaker")
    pyannote.write_bytes(b"pyannote")
    wespeaker_sha256 = installation._sha256_file(wespeaker)
    pyannote_sha256 = installation._sha256_file(pyannote)
    monkeypatch.setattr(
        installation,
        "_required_assets",
        lambda: (
            ("wespeaker", wespeaker, wespeaker_sha256),
            ("pyannote", pyannote, pyannote_sha256),
        ),
    )


def _entry_kwargs(tmp_path: Path, executable: Path) -> dict:
    return {
        "journal_path": tmp_path,
        "executable": executable,
        "version_reader": _version_reader,
        "platform_reader": _platform_reader,
    }


def _clear_generation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _GENERATION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _generation_env_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _GENERATION_ENV_KEYS:
        monkeypatch.setenv(key, _GENERATION_ENV_SENTINEL)
        monkeypatch.delenv(key, raising=False)


def _restore_generation_env(
    monkeypatch: pytest.MonkeyPatch, generation_id: str, fd: int, token: int
) -> None:
    monkeypatch.setenv(installation.GENERATION_ENV_KEY, generation_id)
    monkeypatch.setenv(installation.GENERATION_FD_ENV_KEY, str(fd))
    monkeypatch.setenv(installation.GENERATION_TOKEN_ENV_KEY, str(token))


def _assert_fd_closed(fd: int) -> None:
    with pytest.raises(OSError):
        os.fstat(fd)


def test_coverage_gate_reads_helper_constants_not_core_constants(monkeypatch):
    core_platform = ("coreos", "core64")
    helper_platform = ("helperos", "helper64")
    monkeypatch.setattr(
        probe,
        "SOLSTONE_CORE_COVERED_PLATFORMS",
        (core_platform,),
    )
    monkeypatch.setattr(
        probe,
        "SOLSTONE_CORE_PLATFORM_TAGS",
        {core_platform: "core-tag"},
    )
    monkeypatch.setattr(
        probe,
        "SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS",
        (helper_platform,),
    )
    monkeypatch.setattr(
        probe,
        "SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS",
        {helper_platform: "helper-tag"},
    )

    assert installation.runtime_has_speakers_analyze_wheel_coverage(
        platform_reader=lambda: helper_platform,
        platform_tag_reader=lambda: {"helper-tag"},
    )
    assert not installation.runtime_has_speakers_analyze_wheel_coverage(
        platform_reader=lambda: core_platform,
        platform_tag_reader=lambda: {"core-tag"},
    )


def test_helper_path_is_sibling_of_python_executable(tmp_path: Path):
    executable = tmp_path / "venv" / "bin" / "python"

    assert installation.speakers_analyze_path_for_executable(executable) == (
        executable.with_name(installation.HELPER_BINARY_NAME)
    )


def test_missing_helper_distribution_metadata(tmp_path: Path):
    def version_reader(dist_name: str) -> str:
        if dist_name == installation.ROOT_DIST_NAME:
            return "1.0.18"
        raise PackageNotFoundError(dist_name)

    result = installation.check_speakers_analyze_installation(
        executable=tmp_path / "bin" / "python",
        version_reader=version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
    )

    assert result.status == "metadata-missing"
    assert installation.HELPER_DIST_NAME in result.message


def test_missing_binary_is_distinct_from_missing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _asset_fixtures(tmp_path, monkeypatch)

    result = installation.check_speakers_analyze_installation(
        executable=tmp_path / "bin" / "python",
        version_reader=_version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
    )

    assert result.status == "helper-missing"
    assert installation.HELPER_BINARY_NAME in result.message


def test_non_executable_binary_is_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _asset_fixtures(tmp_path, monkeypatch)
    executable = _helper(tmp_path)
    helper = executable.with_name(installation.HELPER_BINARY_NAME)
    helper.chmod(0o644)

    result = installation.check_speakers_analyze_installation(
        executable=executable,
        version_reader=_version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
        executable_predicate=lambda _path: False,
    )

    assert result.status == "helper-not-executable"


def test_version_mismatch_is_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _asset_fixtures(tmp_path, monkeypatch)
    executable = _helper(tmp_path)

    def version_reader(dist_name: str) -> str:
        if dist_name == installation.ROOT_DIST_NAME:
            return "1.0.18"
        if dist_name == installation.HELPER_DIST_NAME:
            return "1.0.17"
        if dist_name == installation.MODELS_DIST_NAME:
            return "1.0.18"
        raise PackageNotFoundError(dist_name)

    result = installation.check_speakers_analyze_installation(
        executable=executable,
        version_reader=version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
        executable_predicate=lambda _path: True,
    )

    assert result.status == "metadata-version-mismatch"
    assert "1.0.17" in result.message


def test_uncovered_platform_is_unsupported(tmp_path: Path):
    result = installation.check_speakers_analyze_installation(
        executable=tmp_path / "bin" / "python",
        version_reader=_version_reader,
        platform_reader=lambda: ("unsupported", "machine"),
        platform_tag_reader=lambda: {"unsupported-tag"},
    )

    assert result.status == "platform-unsupported"


def test_asset_digest_mismatch_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    wespeaker = tmp_path / "wespeaker.onnx"
    pyannote = tmp_path / "pyannote.onnx"
    wespeaker.write_bytes(b"wespeaker")
    pyannote.write_bytes(b"pyannote")
    monkeypatch.setattr(
        installation,
        "_required_assets",
        lambda: (
            ("wespeaker", wespeaker, "0" * 64),
            ("pyannote", pyannote, installation._sha256_file(pyannote)),
        ),
    )

    result = installation.check_speakers_analyze_installation(
        executable=executable,
        version_reader=_version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
    )

    assert result.status == "asset-digest-mismatch"


def test_live_generation_record_reuses_digest_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    generation = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    calls = 0

    def fail_digest(_path: Path) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("digest should be reused while generation lease is live")

    monkeypatch.setattr(installation, "_sha256_file", fail_digest)

    try:
        result = installation.check_speakers_analyze_installation(
            journal_path=tmp_path,
            executable=executable,
            version_reader=_version_reader,
            platform_reader=_platform_reader,
            platform_tag_reader=_platform_tags,
            generation_id=generation.generation_id,
        )
    finally:
        generation.release()
        _clear_generation_env(monkeypatch)

    assert result.status == "ok"
    assert calls == 0


def test_stale_generation_record_degrades_to_full_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    generation = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    generation_id = generation.generation_id
    generation.release()
    _clear_generation_env(monkeypatch)
    calls = 0
    original_digest = installation._sha256_file

    def counted_digest(path: Path) -> str:
        nonlocal calls
        calls += 1
        return original_digest(path)

    monkeypatch.setattr(installation, "_sha256_file", counted_digest)

    result = installation.check_speakers_analyze_installation(
        journal_path=tmp_path,
        executable=executable,
        version_reader=_version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
        generation_id=generation_id,
    )

    assert result.status == "ok"
    assert calls == 2


def test_generation_token_max_is_a_portable_file_offset():
    assert installation.GENERATION_TOKEN_MAX == (1 << 31) - 1


@pytest.mark.parametrize(
    ("randbelow_result", "expected_token"),
    (
        (0, 1),
        (
            installation.GENERATION_TOKEN_MAX - 1,
            installation.GENERATION_TOKEN_MAX,
        ),
    ),
)
def test_generation_token_endpoints_publish_and_borrow_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    randbelow_result: int,
    expected_token: int,
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    observed_bounds: list[int] = []

    def stub_randbelow(n: int) -> int:
        observed_bounds.append(n)
        return randbelow_result

    monkeypatch.setattr(secrets, "randbelow", stub_randbelow)
    owner = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    owner_fd = int(os.environ[installation.GENERATION_FD_ENV_KEY])
    inherited_fd = os.dup(owner_fd)
    borrower = None
    try:
        assert observed_bounds == [installation.GENERATION_TOKEN_MAX]
        assert os.environ[installation.GENERATION_TOKEN_ENV_KEY] == str(expected_token)
        assert read_file_lease_offset_token(owner_fd) == expected_token
        _restore_generation_env(
            monkeypatch, owner.generation_id, inherited_fd, expected_token
        )
        borrower = installation.enter_speakers_analyze_generation(
            **_entry_kwargs(tmp_path, executable)
        )
        assert isinstance(borrower.lease, BorrowedFileLease)
    finally:
        if borrower is not None:
            borrower.release()
        try:
            os.close(inherited_fd)
        except OSError:
            pass
        owner.release()
        _clear_generation_env(monkeypatch)


def test_generation_token_max_round_trips_file_lease_offset_token(tmp_path: Path):
    lease_path = tmp_path / "token-roundtrip.lock"
    token = installation.GENERATION_TOKEN_MAX
    lease = acquire_file_lease(lease_path, attempts=1)
    assert lease is not None
    try:
        fd = read_file_lease_fd(lease, lease_path)
        set_file_lease_offset_token(lease, token, lease_path)
        assert read_file_lease_offset_token(fd) == token
    finally:
        lease.release()


def test_out_of_range_generation_token_is_rejected_and_cannot_borrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    max_token = installation.GENERATION_TOKEN_MAX
    assert installation._parse_generation_token(str(max_token)) == max_token
    assert installation._parse_generation_token(str(max_token + 1)) is None
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    owner = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    owner_fd = int(os.environ[installation.GENERATION_FD_ENV_KEY])
    inherited_fd = os.dup(owner_fd)
    try:
        _restore_generation_env(
            monkeypatch,
            owner.generation_id,
            inherited_fd,
            max_token + 1,
        )
        with pytest.raises(RuntimeError, match="generation lease is already held"):
            installation.enter_speakers_analyze_generation(
                **_entry_kwargs(tmp_path, executable)
            )
        assert installation.GENERATION_ENV_KEY not in os.environ
        assert installation.GENERATION_FD_ENV_KEY not in os.environ
        assert installation.GENERATION_TOKEN_ENV_KEY not in os.environ
        _assert_fd_closed(inherited_fd)
    finally:
        try:
            os.close(inherited_fd)
        except OSError:
            pass
        owner.release()
        _clear_generation_env(monkeypatch)


def test_owned_entry_publishes_fd_token_and_token_free_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)

    generation = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    try:
        assert isinstance(generation.lease, FileLease)
        fd = int(os.environ[installation.GENERATION_FD_ENV_KEY])
        token = int(os.environ[installation.GENERATION_TOKEN_ENV_KEY])
        assert os.environ[installation.GENERATION_ENV_KEY] == generation.generation_id
        assert token > 0
        assert read_file_lease_fd(generation.lease) == fd
        assert read_file_lease_offset_token(fd) == token

        record = json.loads(
            (
                tmp_path / "health" / "speakers-analyze" / "install-generation.json"
            ).read_text(encoding="utf-8")
        )
        assert record["schema"] == installation.INSTALL_GENERATION_SCHEMA
        assert record["generation_id"] == generation.generation_id
        assert "token" not in record
        assert installation.GENERATION_TOKEN_ENV_KEY not in record
    finally:
        generation.release()
        _clear_generation_env(monkeypatch)


def test_borrowed_entry_reuses_proof_without_hash_and_keeps_owner_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    owner = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    owner_fd = int(os.environ[installation.GENERATION_FD_ENV_KEY])
    token = int(os.environ[installation.GENERATION_TOKEN_ENV_KEY])
    inherited_fd = os.dup(owner_fd)
    _restore_generation_env(monkeypatch, owner.generation_id, inherited_fd, token)
    calls = 0

    def fail_digest(_path: Path) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("borrow must reuse the live proof")

    monkeypatch.setattr(installation, "_sha256_file", fail_digest)
    borrower = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    try:
        assert isinstance(borrower.lease, BorrowedFileLease)
        assert calls == 0
        borrower.release()
        assert (
            acquire_file_lease(
                tmp_path / "health" / "speakers-analyze" / "install-generation.lock",
                attempts=1,
            )
            is None
        )
        _restore_generation_env(monkeypatch, owner.generation_id, inherited_fd, token)
        assert (
            installation.check_speakers_analyze_installation(
                journal_path=tmp_path,
                executable=executable,
                version_reader=_version_reader,
                platform_reader=_platform_reader,
                platform_tag_reader=_platform_tags,
            ).status
            == "ok"
        )
    finally:
        try:
            os.close(inherited_fd)
        except OSError:
            pass
        owner.release()
        _clear_generation_env(monkeypatch)


def test_copied_or_separately_opened_generation_env_cannot_borrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    owner = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    owner_fd = int(os.environ[installation.GENERATION_FD_ENV_KEY])
    token = int(os.environ[installation.GENERATION_TOKEN_ENV_KEY])
    lock_path = tmp_path / "health" / "speakers-analyze" / "install-generation.lock"

    cases: list[tuple[str, int | None]] = []
    cases.append(("missing-fd", None))
    unrelated_fd = os.open(tmp_path / "unrelated.lock", os.O_RDWR | os.O_CREAT, 0o600)
    cases.append(("unrelated-fd", unrelated_fd))
    separate_fd = os.open(lock_path, os.O_RDWR)
    os.lseek(separate_fd, token, os.SEEK_SET)
    cases.append(("separate-same-path-fd", separate_fd))
    mismatched_id_fd = os.dup(owner_fd)
    cases.append(("mismatched-id", mismatched_id_fd))

    try:
        for name, fd in cases:
            _restore_generation_env(
                monkeypatch,
                "stale-generation" if name == "mismatched-id" else owner.generation_id,
                fd if fd is not None else owner_fd,
                token,
            )
            if name == "missing-fd":
                monkeypatch.delenv(installation.GENERATION_FD_ENV_KEY, raising=False)
            monkeypatch.setenv("SOL_SUPERVISOR_SPAWNED", "1")
            with pytest.raises(RuntimeError, match="generation lease is already held"):
                installation.enter_speakers_analyze_generation(
                    **_entry_kwargs(tmp_path, executable)
                )
            assert installation.GENERATION_ENV_KEY not in os.environ
            assert installation.GENERATION_FD_ENV_KEY not in os.environ
            assert installation.GENERATION_TOKEN_ENV_KEY not in os.environ
            if fd is not None:
                _assert_fd_closed(fd)
    finally:
        owner.release()
        _clear_generation_env(monkeypatch)


def test_final_duplicate_rejection_closes_candidate_before_fresh_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    owner = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    old_fd = int(os.environ[installation.GENERATION_FD_ENV_KEY])
    token = int(os.environ[installation.GENERATION_TOKEN_ENV_KEY])
    final_duplicate_fd = os.dup(old_fd)
    os.close(old_fd)
    owner.lease._fd = None
    _restore_generation_env(monkeypatch, "stale-generation", final_duplicate_fd, token)

    calls = 0
    original_digest = installation._sha256_file

    def counted_digest(path: Path) -> str:
        nonlocal calls
        calls += 1
        return original_digest(path)

    monkeypatch.setattr(installation, "_sha256_file", counted_digest)
    fresh = installation.enter_speakers_analyze_generation(
        **_entry_kwargs(tmp_path, executable)
    )
    try:
        assert fresh.generation_id != owner.generation_id
        assert calls == 2
        _assert_fd_closed(final_duplicate_fd)
    finally:
        fresh.release()
        _clear_generation_env(monkeypatch)


@pytest.mark.parametrize(
    "failure", ["proof-key", "validation", "token-init", "record-write"]
)
def test_owned_entry_failures_release_and_allow_reacquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    if failure == "proof-key":
        monkeypatch.setattr(
            installation,
            "_installation_proof_key",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("proof failed")),
        )
    elif failure == "validation":
        monkeypatch.setattr(
            installation,
            "_validated_asset_digests",
            lambda _proof_key: (
                installation.SpeakersAnalyzeInstallationResult(
                    "asset-missing", "validation failed"
                ),
                [],
            ),
        )
    elif failure == "token-init":
        monkeypatch.setattr(
            installation,
            "set_file_lease_offset_token",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("token failed")
            ),
        )
    else:
        monkeypatch.setattr(
            installation,
            "_write_generation_record",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("write failed")
            ),
        )

    with pytest.raises(RuntimeError):
        installation.enter_speakers_analyze_generation(
            **_entry_kwargs(tmp_path, executable)
        )
    assert installation.GENERATION_ENV_KEY not in os.environ
    assert installation.GENERATION_FD_ENV_KEY not in os.environ
    assert installation.GENERATION_TOKEN_ENV_KEY not in os.environ

    lease = acquire_file_lease(
        tmp_path / "health" / "speakers-analyze" / "install-generation.lock",
        attempts=1,
    )
    assert lease is not None
    lease.release()
