# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.release_build_host as build_rail
import scripts.release_proof_host as proof_rail
from scripts.channel_adapters import adapter_common as common
from scripts.channel_adapters import build_host_macos, proof_host
from scripts.check_release_preflight import expected_presign_lane_tool_evidence
from scripts.release_tool_pins import (
    HOST_VARIANT_TOOL_KEYS,
    MACOS_SWIFT_FIXTURE_BANNER,
    UV_MACOS_FIXTURE_BANNER,
)


def _completed(
    stdout: str = "",
    *,
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _lane(
    name: str = "lane",
    *,
    host: str = "build-host.example",
    port: int | None = 2222,
) -> common.LaneConfig:
    return common.LaneConfig(
        name=name,
        mode="ssh",
        host=host,
        port=port,
        user="builder",
        identity_file="~/.ssh/solstone-channel-adapter",
        extra_ssh_options=("-o", "BatchMode=yes"),
        remote_python="python3",
        remote_work_prefix="/tmp/solstone-channel-adapter",
        tmux_window="adapter:build",
        unlock_workdir="~/projects/build-worktree",
    )


def _local_lane() -> common.LaneConfig:
    return common.LaneConfig(
        name="proof.linux-x86_64-musl",
        mode="local",
    )


def _tool_stdout(*, uv: str = UV_MACOS_FIXTURE_BANNER) -> str:
    expected = expected_presign_lane_tool_evidence("macos-arm64")
    observed = {
        **expected,
        "uv": uv,
        "swift": MACOS_SWIFT_FIXTURE_BANNER,
    }
    return (
        "\n".join(f"{key}\t{observed[key]}" for key in sorted(observed))
        + f"\n{build_host_macos.TOOLCHAIN_TOKEN}\n"
    )


def _write_build_request(tmp_path: Path) -> tuple[Path, build_rail.SourceBundle, dict]:
    bundle = tmp_path / "source.bundle"
    bundle.write_bytes(b"bundle")
    sha256, byte_count = common.sha256_size(bundle)
    source_bundle = build_rail.SourceBundle(
        path=bundle,
        source_commit="a" * 40,
        sha256=sha256,
        bytes=byte_count,
    )
    channel = build_rail.ExternalBuildHostChannel(["adapter"])
    payload = channel._request_payload(
        cohort_id="cohort",
        source_bundle=source_bundle,
        expected_commit=source_bundle.source_commit,
    )
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    request_bundle = request_dir / "source.bundle"
    request_bundle.write_bytes(bundle.read_bytes())
    request_path = request_dir / "request.json"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_path, source_bundle, payload


def _write_proof_request(tmp_path: Path) -> tuple[Path, dict]:
    request_dir = tmp_path / "proof-request"
    candidate_dir = request_dir / "candidate"
    output_dir = request_dir / "output"
    candidate_dir.mkdir(parents=True)
    output_dir.mkdir()
    wheel = candidate_dir / "solstone-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    (request_dir / "ledger.json").write_text("{}", encoding="utf-8")
    channel = proof_rail.ExternalProofHostChannel(
        "linux-x86_64-musl",
        ["adapter"],
    )
    payload = channel._request_payload(
        cohort_id="cohort",
        target="linux-x86_64-musl",
        version="1.0.0",
        source_commit="b" * 40,
        core_lock_sha256="c" * 64,
        candidate_digest="d" * 64,
        ledger_sha256="e" * 64,
        install_paths=[wheel],
    )
    request_path = request_dir / "request.json"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_path, payload


def test_build_request_response_round_trip_through_rail_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, source_bundle, _payload = _write_build_request(tmp_path)

    def fake_runner(argv, **kwargs):
        if argv[0] == "scp" and ":" in argv[-2]:
            Path(argv[-1]).write_bytes(b"artifact")
        script = kwargs.get("input_text") or ""
        if "emit python" in script:
            return _completed(_tool_stdout())
        if "git checkout" in script:
            return _completed(f"{build_host_macos.CHECKOUT_TOKEN}\n")
        if "for f in" in script:
            return _completed(f"{build_host_macos.DIST_TOKEN}\n")
        return _completed()

    monkeypatch.setattr(common, "run", fake_runner)
    monkeypatch.chdir(request_path.parent)

    build_host_macos.build_macos(_lane(), request_path)

    response = json.loads((request_path.parent / "response.json").read_text())
    build_rail._validate_attestation(
        response,
        expected_commit=source_bundle.source_commit,
        source_bundle=source_bundle,
    )
    evidence = build_rail._validate_macos_tool_evidence(response)
    wheel_names, record_names = build_rail._names_from_payload(response)
    assert set(evidence) == set(expected_presign_lane_tool_evidence("macos-arm64"))
    assert len(wheel_names) == 2
    assert tuple(record_names) == (
        build_rail.MACOS_ROOT_RECORD,
        build_rail.MACOS_CORE_RECORD,
    )


def test_proof_request_response_round_trip_through_rail_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, request_payload = _write_proof_request(tmp_path)
    proof_path = request_path.parent / "output" / "proof.json"

    def fake_runner(argv, **kwargs):
        if argv == ["uname", "-s"]:
            return _completed("Linux\n")
        if argv == ["uname", "-m"]:
            return _completed("x86_64\n")
        if argv[:2] == [sys.executable, "-c"]:
            proof_path.write_bytes(b'{"ok":true}\n')
            sha256, byte_count = common.sha256_size(proof_path)
            return _completed(
                f'{proof_host.PROOF_TOKEN} {{"bytes": {byte_count}, "sha256": "{sha256}"}}\n'
            )
        raise AssertionError(argv)

    monkeypatch.setattr(proof_host, "run", fake_runner)

    proof_host.prove("linux-x86_64-musl", _local_lane(), request_path)

    response = json.loads((request_path.parent / "response.json").read_text())
    channel = proof_rail.ExternalProofHostChannel("linux-x86_64-musl", ["adapter"])
    proof_descriptor = channel._validate_response(
        response,
        cohort_id="cohort",
        target="linux-x86_64-musl",
        candidate_digest=request_payload["candidate_digest"],
        ledger_sha256=request_payload["ledger_sha256"],
    )
    assert proof_descriptor["path"] == "output/proof.json"


def test_source_and_retrieved_digest_verification(tmp_path: Path) -> None:
    proof = tmp_path / "proof.json"
    proof.write_bytes(b"proof")
    sha256, byte_count = common.sha256_size(proof)

    proof_host._verify_retrieved_proof(
        proof,
        expected_sha256=sha256,
        expected_bytes=byte_count,
    )
    with pytest.raises(SystemExit):
        proof_host._verify_retrieved_proof(
            proof,
            expected_sha256="0" * 64,
            expected_bytes=byte_count,
        )


def test_macos_tool_evidence_derives_from_rail_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        common, "run", lambda _argv, **_kwargs: _completed(_tool_stdout())
    )

    evidence = build_host_macos._derive_tool_evidence(_lane())
    expected = expected_presign_lane_tool_evidence("macos-arm64")

    assert set(evidence) == set(expected)
    assert evidence["uv"] == UV_MACOS_FIXTURE_BANNER
    assert evidence["swift"] == MACOS_SWIFT_FIXTURE_BANNER
    for key in set(expected) - set(HOST_VARIANT_TOOL_KEYS):
        assert evidence[key] == expected[key]


def test_host_variant_banner_mutation_writes_no_evidence_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, _source_bundle, _payload = _write_build_request(tmp_path)
    mutated = "uv 0.11.5 (aarch64-apple-darwin)"

    monkeypatch.setattr(
        common,
        "run",
        lambda _argv, **kwargs: (
            _completed(_tool_stdout(uv=mutated))
            if "emit python" in (kwargs.get("input_text") or "")
            else _completed(f"{build_host_macos.CHECKOUT_TOKEN}\n")
        ),
    )
    monkeypatch.chdir(request_path.parent)

    with pytest.raises(SystemExit):
        build_host_macos.build_macos(_lane(), request_path)

    assert not (request_path.parent / "response.json").exists()


def test_public_evidence_failure_writes_no_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, _source_bundle, _payload = _write_build_request(tmp_path)
    private_shape = "uv 0.11.4 (builder.local)"

    monkeypatch.setattr(
        common,
        "run",
        lambda _argv, **kwargs: (
            _completed(_tool_stdout(uv=private_shape))
            if "emit python" in (kwargs.get("input_text") or "")
            else _completed(f"{build_host_macos.CHECKOUT_TOKEN}\n")
        ),
    )
    monkeypatch.chdir(request_path.parent)

    with pytest.raises(SystemExit):
        build_host_macos.build_macos(_lane(), request_path)

    assert not (request_path.parent / "response.json").exists()


def test_sentinel_requires_exit_zero_and_token() -> None:
    common.require_success_token(_completed("TOKEN\n"), "TOKEN", "label")
    with pytest.raises(SystemExit):
        common.require_success_token(_completed("", returncode=0), "TOKEN", "label")
    with pytest.raises(SystemExit):
        common.require_success_token(
            _completed("TOKEN\n", returncode=1), "TOKEN", "label"
        )


def test_ssh_argv_from_structured_config() -> None:
    flag = "-" + "p"

    argv = common.build_ssh_argv(_lane(), ["bash", "-s"])

    assert argv == [
        "ssh",
        "-o",
        "BatchMode=yes",
        flag,
        "2222",
        "-i",
        "~/.ssh/solstone-channel-adapter",
        "builder@build-host.example",
        "bash",
        "-s",
    ]


def test_scp_argv_from_structured_config() -> None:
    flag = "-" + "P"

    argv = common.build_scp_argv(_lane(), "local", "remote", direction="to")

    assert argv == [
        "scp",
        "-q",
        "-o",
        "BatchMode=yes",
        flag,
        "2222",
        "-i",
        "~/.ssh/solstone-channel-adapter",
        "local",
        "builder@build-host.example:remote",
    ]


def test_cleanup_argument_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], str]] = []

    def fake_runner(argv, **kwargs):
        calls.append((list(argv), kwargs.get("input_text") or ""))
        return _completed()

    monkeypatch.setattr(common, "run", fake_runner)

    build_host_macos.cleanup(_lane(), "cohort", "f" * 64)
    proof_host.cleanup(
        "macos-arm64", _lane(name="proof.macos-arm64"), "cohort", "e" * 64
    )

    assert len(calls) == 2
    assert all("cohort" in script for _argv, script in calls)


def test_config_validation_fails_before_side_effects(tmp_path: Path) -> None:
    config = tmp_path / "channel-adapters.json"
    config.write_text(
        '{"schema_version": 1, "build": {}, "proof": {}}', encoding="utf-8"
    )

    with pytest.raises(SystemExit):
        common.load_config(
            proof_targets=tuple(proof_rail.TARGET_ENV_KEYS),
            env={common.CONFIG_ENV: str(config)},
        )


def test_target_env_keys_coupling() -> None:
    config_targets = {
        "linux-x86_64-musl",
        "linux-aarch64-musl",
        "macos-arm64",
    }

    assert config_targets == set(proof_rail.TARGET_ENV_KEYS)
