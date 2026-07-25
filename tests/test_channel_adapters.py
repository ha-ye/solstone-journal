# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.release_build_host as build_rail
import scripts.release_install_smoke as smoke
import scripts.release_proof_host as proof_rail
from scripts.channel_adapters import adapter_common as common
from scripts.channel_adapters import build_host_macos, proof_host
from scripts.check_release_preflight import expected_presign_lane_tool_evidence
from scripts.release_digest import candidate_digest
from scripts.release_tool_pins import (
    HOST_VARIANT_TOOL_KEYS,
    MACOS_SWIFT_FLATTENED_BANNER,
    UV_MACOS_FIXTURE_BANNER,
)
from tests.helpers.release_wheel_fixtures import ROOT_LAUNCHER_BYTES, record_hash


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
        remote_run_wrapper="operator-session-wrapper",
        tmux_window="adapter:build",
        unlock_workdir="~/projects/build-worktree",
    )


def _local_lane() -> common.LaneConfig:
    return common.LaneConfig(
        name="proof.linux-x86_64-musl",
        mode="local",
    )


def _write_metadata_wheel(path: Path) -> None:
    distribution, version = path.name.removesuffix(".whl").split("-")[:2]
    metadata_name = f"{distribution}-{version}.dist-info/METADATA"
    metadata = f"Name: {distribution.replace('_', '-')}\nVersion: {version}\n"
    with zipfile.ZipFile(path, "w") as wheel:
        members = {metadata_name: metadata.encode("utf-8")}
        if path.name.startswith("solstone-"):
            members[f"solstone-{version}.dist-info/WHEEL"] = b"Wheel-Version: 1.0\n"
            for name, content in ROOT_LAUNCHER_BYTES.items():
                members[f"solstone-{version}.data/scripts/{name}"] = content
            record_name = f"solstone-{version}.dist-info/RECORD"
            record = "\n".join(
                f"{name},{record_hash(content)},{len(content)}"
                for name, content in members.items()
            )
            members[record_name] = f"{record}\n{record_name},,".encode("utf-8")
        for name, content in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (
                0o755 << 16
                if Path(name).name in smoke.ROOT_LAUNCHER_NAMES
                else 0o644 << 16
            )
            wheel.writestr(info, content)


def _file_entry(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "name": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _tool_stdout(*, uv: str = UV_MACOS_FIXTURE_BANNER) -> str:
    expected = expected_presign_lane_tool_evidence("macos-arm64")
    observed = {
        **expected,
        "uv": uv,
        "swift": MACOS_SWIFT_FLATTENED_BANNER,
    }
    return (
        "\n".join(f"{key}\t{observed[key]}" for key in sorted(observed))
        + f"\n{build_host_macos.TOOLCHAIN_TOKEN}\n"
    )


def _artifact_listing_stdout(artifact_bytes: dict[str, bytes]) -> str:
    lines = []
    for name, data in artifact_bytes.items():
        lines.append(
            "\t".join(
                (
                    build_host_macos.ARTIFACT_TOKEN,
                    name,
                    hashlib.sha256(data).hexdigest(),
                    str(len(data)),
                )
            )
        )
    lines.append(build_host_macos.DIST_TOKEN)
    return "\n".join(lines) + "\n"


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


def _write_proof_request(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    request_dir = tmp_path / "proof-request"
    candidate_dir = request_dir / "candidate"
    output_dir = request_dir / "output"
    candidate_dir.mkdir(parents=True)
    output_dir.mkdir()
    for name in (
        "solstone-1.0.0-py3-none-any.whl",
        "solstone_core-1.0.0-py3-none-linux_x86_64.whl",
    ):
        _write_metadata_wheel(candidate_dir / name)
    native_members = {
        name: {
            "path": f"linux-x86/{name}",
            "sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
            "bytes": len(name.encode("utf-8")),
        }
        for name in smoke.CORE_SCRIPT_NAMES
    }
    digest = candidate_digest(candidate_dir)
    ledger: dict[str, Any] = {
        "source_commit": "b" * 40,
        "core_lock_sha256": "c" * 64,
        "candidate": {
            "candidate_digest": digest,
            "files": [
                _file_entry(path)
                for path in sorted(candidate_dir.iterdir(), key=lambda item: item.name)
            ],
        },
        "native_members": {"linux-x86_64-musl": native_members},
    }
    ledger_bytes = json.dumps(ledger, sort_keys=True).encode("utf-8")
    ledger_sha = hashlib.sha256(ledger_bytes).hexdigest()
    (request_dir / "ledger.json").write_bytes(ledger_bytes)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="linux-x86_64-musl",
        candidate_dir=candidate_dir,
    )
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
        candidate_digest=digest,
        ledger_sha256=ledger_sha,
        install_paths=install_paths,
    )
    request_path = request_dir / "request.json"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_path, payload, ledger


def _write_valid_install_proof(
    proof_path: Path,
    *,
    request_payload: dict[str, Any],
    ledger: dict[str, Any],
) -> None:
    request_dir = proof_path.parents[1]
    candidate_dir = request_dir / request_payload["paths"]["candidate_dir"]
    env_root = request_dir / "env"
    (env_root / "bin").mkdir(parents=True, exist_ok=True)
    python_path = env_root / "bin" / "python"
    python_path.write_bytes(b"python")
    executable_paths = {
        name: env_root / "bin" / name for name in smoke.INSTALL_SCRIPT_NAMES
    }
    for name in smoke.ROOT_LAUNCHER_NAMES:
        executable_paths[name].write_bytes(ROOT_LAUNCHER_BYTES[name])
    for name in smoke.CORE_SCRIPT_NAMES:
        executable_paths[name].write_text(name, encoding="utf-8")
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target=request_payload["target"],
        candidate_dir=candidate_dir,
    )
    expected_members, expected_failures = smoke._expected_install_members(
        ledger,
        request_payload["target"],
        candidate_dir=candidate_dir,
        install_paths=install_paths,
    )
    assert expected_failures == []
    proof = smoke.build_install_proof(
        target=request_payload["target"],
        version=request_payload["version"],
        source_commit=request_payload["source_commit"],
        core_lock_sha256=request_payload["core_lock_sha256"],
        candidate_digest=request_payload["candidate_digest"],
        ledger_sha256=request_payload["ledger_sha256"],
        candidate_dir=candidate_dir,
        candidate_paths=install_paths,
        ledger_payload=ledger,
        observation=smoke.InstallObservation(
            env_root=env_root,
            preexisting_distributions=(),
            install=smoke.CommandResult(
                argv=(
                    str(python_path),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    *(str(path) for path in install_paths),
                ),
                exit_code=0,
                stdout="installed",
                env=smoke.SCRUBBED_COMMAND_ENV,
            ),
            installed_distributions=smoke.expected_distribution_entries(install_paths),
            installed_members=tuple(
                {
                    "name": name,
                    "path": path,
                    "sha256": expected_members[name]["sha256"],
                    "symlink": False,
                }
                for name, path in sorted(executable_paths.items())
            ),
            smoke={
                name: smoke.CommandResult(
                    argv=(str(path), "--version"),
                    exit_code=0,
                    stdout=(
                        f"{smoke.CORE_SMOKE_STDOUT[name]} {request_payload['version']}"
                    ),
                    env=smoke.SCRUBBED_COMMAND_ENV,
                )
                for name, path in sorted(executable_paths.items())
            },
        ),
        recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )
    smoke.write_install_proof(
        proof_path,
        proof,
        target=request_payload["target"],
        version=request_payload["version"],
        source_commit=request_payload["source_commit"],
        core_lock_sha256=request_payload["core_lock_sha256"],
        candidate_digest=request_payload["candidate_digest"],
        ledger_sha256=request_payload["ledger_sha256"],
        candidate_dir=candidate_dir,
        ledger_payload=ledger,
    )


def test_build_request_response_round_trip_through_rail_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, source_bundle, payload = _write_build_request(tmp_path)
    expected_files = list(payload["expected_outputs"].values())
    artifact_bytes = {
        name: f"artifact:{name}".encode("utf-8") for name in expected_files
    }

    def fake_runner(argv, **kwargs):
        if argv[0] == "scp" and ":" in argv[-2]:
            name = str(argv[-2]).rsplit("/", 1)[-1]
            Path(argv[-1]).write_bytes(artifact_bytes[name])
        script = kwargs.get("input_text") or ""
        if "emit python" in script:
            return _completed(_tool_stdout())
        if "git checkout" in script:
            return _completed(f"{build_host_macos.CHECKOUT_TOKEN}\n")
        if "for f in" in script:
            return _completed(_artifact_listing_stdout(artifact_bytes))
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


def test_build_retrieved_artifact_digest_mismatch_writes_no_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, _source_bundle, payload = _write_build_request(tmp_path)
    expected_files = list(payload["expected_outputs"].values())
    artifact_bytes = {
        name: f"artifact:{name}".encode("utf-8") for name in expected_files
    }
    truncated_name = expected_files[0]

    def fake_runner(argv, **kwargs):
        if argv[0] == "scp" and ":" in argv[-2]:
            name = str(argv[-2]).rsplit("/", 1)[-1]
            data = artifact_bytes[name]
            Path(argv[-1]).write_bytes(data[:-1] if name == truncated_name else data)
        script = kwargs.get("input_text") or ""
        if "emit python" in script:
            return _completed(_tool_stdout())
        if "git checkout" in script:
            return _completed(f"{build_host_macos.CHECKOUT_TOKEN}\n")
        if "for f in" in script:
            return _completed(_artifact_listing_stdout(artifact_bytes))
        return _completed()

    monkeypatch.setattr(common, "run", fake_runner)
    monkeypatch.chdir(request_path.parent)

    with pytest.raises(SystemExit):
        build_host_macos.build_macos(_lane(), request_path)

    assert not (request_path.parent / "response.json").exists()


def test_proof_request_response_round_trip_through_rail_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, request_payload, ledger = _write_proof_request(tmp_path)
    proof_path = request_path.parent / "output" / "proof.json"

    def fake_runner(argv, **kwargs):
        if argv == ["uname", "-s"]:
            return _completed("Linux\n")
        if argv == ["uname", "-m"]:
            return _completed("x86_64\n")
        if argv[:2] == [sys.executable, "-c"]:
            _write_valid_install_proof(
                proof_path,
                request_payload=request_payload,
                ledger=ledger,
            )
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
    proof_failures = smoke.validate_install_proof_bytes(
        proof_path.read_bytes(),
        target=request_payload["target"],
        version=request_payload["version"],
        source_commit=request_payload["source_commit"],
        core_lock_sha256=request_payload["core_lock_sha256"],
        candidate_digest=request_payload["candidate_digest"],
        ledger_sha256=request_payload["ledger_sha256"],
        candidate_dir=request_path.parent / request_payload["paths"]["candidate_dir"],
        ledger_payload=ledger,
    )
    assert proof_failures == []


def test_source_and_retrieved_digest_verification(tmp_path: Path) -> None:
    proof = tmp_path / "proof.json"
    proof.write_bytes(b"proof")
    sha256, byte_count = common.sha256_size(proof)

    common.verify_retrieved_file(
        proof,
        expected_sha256=sha256,
        expected_bytes=byte_count,
        label="proof.json",
    )
    with pytest.raises(SystemExit):
        common.verify_retrieved_file(
            proof,
            expected_sha256="0" * 64,
            expected_bytes=byte_count,
            label="proof.json",
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
    assert evidence["swift"] == MACOS_SWIFT_FLATTENED_BANNER
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


def test_config_validation_fails_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "channel-adapters.json"
    config.write_text(
        '{"schema_version": 1, "build": {}, "proof": {}}', encoding="utf-8"
    )
    calls: list[list[str]] = []

    def fake_runner(argv, **_kwargs):
        calls.append(list(argv))
        return _completed()

    monkeypatch.setenv(common.CONFIG_ENV, str(config))
    monkeypatch.setattr(common, "run", fake_runner)

    with pytest.raises(SystemExit):
        build_host_macos.main(["build-macos", "request.json"])

    assert calls == []


def test_target_env_keys_coupling() -> None:
    config_targets = {
        "linux-x86_64-musl",
        "linux-aarch64-musl",
        "macos-arm64",
    }

    assert config_targets == set(proof_rail.TARGET_ENV_KEYS)
