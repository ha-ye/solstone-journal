#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Shared helpers for release channel adapters."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_ENV = "RELEASE_CHANNEL_ADAPTER_CONFIG"
DEFAULT_RELATIVE_CONFIG = Path("solstone") / "channel-adapters.json"


@dataclass(frozen=True)
class LaneConfig:
    name: str
    mode: str
    host: str | None = None
    port: int | None = None
    user: str | None = None
    identity_file: str | None = None
    extra_ssh_options: tuple[str, ...] = ()
    remote_python: str = "python3"
    remote_work_prefix: str = "/tmp/solstone-channel-adapter"
    remote_run_wrapper: str | None = None
    tmux_window: str | None = None
    unlock_workdir: str | None = None

    @property
    def is_local(self) -> bool:
        return self.mode == "local"

    @property
    def endpoint(self) -> str:
        if self.host is None:
            die(f"lane {self.name} has no host")
        if self.user:
            return f"{self.user}@{self.host}"
        return self.host


def die(message: str, *, detail: str = "") -> NoReturn:
    sys.stderr.write(f"adapter error: {message}\n")
    if detail:
        sys.stderr.write(detail.rstrip() + "\n")
    raise SystemExit(1)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        die(f"could not read {path}", detail=str(exc))
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON", detail=str(exc))


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def sha256_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def verify_retrieved_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
    label: str,
) -> None:
    if not path.is_file():
        die(f"{label} was not produced")
    actual_sha256, actual_bytes = sha256_size(path)
    if actual_bytes <= 0:
        die(f"{label} is empty")
    if actual_sha256 != expected_sha256 or actual_bytes != expected_bytes:
        die(
            f"{label} digest/size mismatch after retrieval",
            detail=(
                f"expected {expected_sha256}/{expected_bytes} "
                f"got {actual_sha256}/{actual_bytes}"
            ),
        )


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    override = source.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    config_home = source.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / DEFAULT_RELATIVE_CONFIG
    return Path.home() / ".config" / DEFAULT_RELATIVE_CONFIG


def _config_error(path: Path, message: str) -> NoReturn:
    die(
        message,
        detail=(
            f"config path searched: {path}\n"
            f"set {CONFIG_ENV} to override the config path"
        ),
    )


def _require_mapping(
    value: object,
    *,
    key: str,
    path: Path,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _config_error(path, f"operator config key {key!r} must be an object")
    return value


def _require_string(
    value: object,
    *,
    key: str,
    path: Path,
) -> str:
    if not isinstance(value, str) or not value:
        _config_error(path, f"operator config key {key!r} must be a string")
    return value


def _optional_string(
    mapping: Mapping[str, object],
    *,
    key: str,
    path: Path,
) -> str | None:
    if key not in mapping:
        return None
    return _require_string(mapping[key], key=key, path=path)


def _optional_string_list(
    mapping: Mapping[str, object],
    *,
    key: str,
    path: Path,
) -> tuple[str, ...]:
    if key not in mapping:
        return ()
    value = mapping[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _config_error(path, f"operator config key {key!r} must be a list of strings")
    return tuple(value)


def _optional_port(
    mapping: Mapping[str, object],
    *,
    key: str,
    path: Path,
) -> int | None:
    if key not in mapping:
        return None
    value = mapping[key]
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        _config_error(path, f"operator config key {key!r} must be a TCP port number")
    return value


def _validate_no_unknown_keys(
    mapping: Mapping[str, object],
    *,
    allowed: set[str],
    prefix: str,
    path: Path,
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        _config_error(path, f"operator config key {prefix}.{unknown[0]} is unknown")


def _parse_lane(
    name: str,
    raw: object,
    *,
    path: Path,
    allow_local: bool,
    require_build_fields: bool = False,
) -> LaneConfig:
    mapping = _require_mapping(raw, key=name, path=path)
    allowed = {
        "mode",
        "host",
        "port",
        "user",
        "identity_file",
        "extra_ssh_options",
        "remote_python",
        "remote_work_prefix",
    }
    if require_build_fields:
        allowed.update({"remote_run_wrapper", "tmux_window", "unlock_workdir"})
    _validate_no_unknown_keys(mapping, allowed=allowed, prefix=name, path=path)
    if "mode" not in mapping:
        _config_error(path, f"operator config missing key {name}.mode")
    mode = _require_string(mapping["mode"], key=f"{name}.mode", path=path)
    if mode not in {"ssh", "local"}:
        _config_error(path, f"operator config key {name}.mode must be ssh or local")
    if mode == "local":
        if not allow_local:
            _config_error(path, f"operator config lane {name} cannot use local mode")
        forbidden = sorted(set(mapping) - {"mode", "remote_work_prefix"})
        if forbidden:
            _config_error(
                path,
                f"operator config local lane {name} must not set {forbidden[0]}",
            )
        return LaneConfig(
            name=name,
            mode=mode,
            remote_work_prefix=_optional_string(
                mapping,
                key="remote_work_prefix",
                path=path,
            )
            or "/tmp/solstone-channel-adapter",
        )

    if "host" not in mapping:
        _config_error(path, f"operator config missing key {name}.host")
    lane = LaneConfig(
        name=name,
        mode=mode,
        host=_require_string(mapping["host"], key=f"{name}.host", path=path),
        port=_optional_port(mapping, key="port", path=path),
        user=_optional_string(mapping, key="user", path=path),
        identity_file=_optional_string(mapping, key="identity_file", path=path),
        extra_ssh_options=_optional_string_list(
            mapping,
            key="extra_ssh_options",
            path=path,
        ),
        remote_python=_optional_string(mapping, key="remote_python", path=path)
        or "python3",
        remote_work_prefix=_optional_string(
            mapping,
            key="remote_work_prefix",
            path=path,
        )
        or "/tmp/solstone-channel-adapter",
        remote_run_wrapper=_optional_string(
            mapping,
            key="remote_run_wrapper",
            path=path,
        ),
        tmux_window=_optional_string(mapping, key="tmux_window", path=path),
        unlock_workdir=_optional_string(mapping, key="unlock_workdir", path=path),
    )
    if require_build_fields:
        if lane.remote_run_wrapper is None:
            _config_error(
                path,
                f"operator config missing key {name}.remote_run_wrapper",
            )
        if lane.tmux_window is None:
            _config_error(path, f"operator config missing key {name}.tmux_window")
        if lane.unlock_workdir is None:
            _config_error(path, f"operator config missing key {name}.unlock_workdir")
    return lane


def load_config(
    *,
    proof_targets: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> tuple[LaneConfig, dict[str, LaneConfig]]:
    path = default_config_path(env)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _config_error(path, "operator config file is missing")
    except OSError as exc:
        _config_error(path, f"operator config file could not be read: {exc}")
    except json.JSONDecodeError as exc:
        _config_error(path, f"operator config file is not valid JSON: {exc}")
    if not isinstance(payload, Mapping):
        _config_error(path, "operator config must be a JSON object")
    _validate_no_unknown_keys(
        payload,
        allowed={"schema_version", "build", "proof"},
        prefix="config",
        path=path,
    )
    if payload.get("schema_version") != 1:
        _config_error(path, "operator config missing key schema_version=1")
    if "build" not in payload:
        _config_error(path, "operator config missing key build")
    if "proof" not in payload:
        _config_error(path, "operator config missing key proof")
    build = _require_mapping(payload["build"], key="build", path=path)
    proof = _require_mapping(payload["proof"], key="proof", path=path)
    if "macos-arm64" not in build:
        _config_error(path, "operator config missing key build.macos-arm64")
    _validate_no_unknown_keys(
        build,
        allowed={"macos-arm64"},
        prefix="build",
        path=path,
    )
    expected_targets = set(proof_targets)
    for target in sorted(expected_targets):
        if target not in proof:
            _config_error(path, f"operator config missing key proof.{target}")
    extra_targets = sorted(set(proof) - expected_targets)
    if extra_targets:
        _config_error(path, f"operator config key proof.{extra_targets[0]} is unknown")
    build_lane = _parse_lane(
        "build.macos-arm64",
        build["macos-arm64"],
        path=path,
        allow_local=False,
        require_build_fields=True,
    )
    proof_lanes = {
        target: _parse_lane(
            f"proof.{target}",
            proof[target],
            path=path,
            allow_local=(target == "linux-x86_64-musl"),
        )
        for target in proof_targets
    }
    return build_lane, proof_lanes


def build_ssh_argv(
    lane: LaneConfig,
    remote_command: Sequence[str] | None = None,
) -> list[str]:
    if lane.is_local:
        die(f"lane {lane.name} is local and cannot build ssh argv")
    argv = ["ssh", *lane.extra_ssh_options]
    if lane.port is not None:
        argv.extend(["-p", str(lane.port)])
    if lane.identity_file is not None:
        argv.extend(["-i", lane.identity_file])
    argv.append(lane.endpoint)
    if remote_command:
        argv.extend(remote_command)
    return argv


def build_scp_argv(
    lane: LaneConfig,
    source: str | Path,
    dest: str | Path,
    *,
    direction: str,
) -> list[str]:
    if lane.is_local:
        die(f"lane {lane.name} is local and cannot build scp argv")
    argv = ["scp", "-q", *lane.extra_ssh_options]
    if lane.port is not None:
        argv.extend(["-P", str(lane.port)])
    if lane.identity_file is not None:
        argv.extend(["-i", lane.identity_file])
    if direction == "to":
        argv.extend([str(source), f"{lane.endpoint}:{dest}"])
    elif direction == "from":
        argv.extend([f"{lane.endpoint}:{source}", str(dest)])
    else:
        die(f"unsupported scp direction: {direction}")
    return argv


def run(
    argv: Sequence[str],
    *,
    capture: bool = True,
    check: bool = True,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(argv),
        capture_output=capture,
        text=True,
        input=input_text,
        env=dict(env) if env is not None else None,
        check=False,
    )
    if check and result.returncode != 0:
        die(
            f"command failed ({result.returncode}): {' '.join(argv)}",
            detail=(result.stderr or result.stdout or ""),
        )
    return result


def ssh_run(
    lane: LaneConfig,
    remote_script: str,
    *,
    check: bool = True,
    capture: bool = True,
    runner=None,
) -> subprocess.CompletedProcess[str]:
    selected_runner = run if runner is None else runner
    return selected_runner(
        build_ssh_argv(lane, ["bash", "-s"]),
        input_text=remote_script,
        check=check,
        capture=capture,
    )


def scp_to(
    lane: LaneConfig,
    local: Path,
    remote_path: str,
    *,
    runner=None,
) -> None:
    selected_runner = run if runner is None else runner
    selected_runner(build_scp_argv(lane, local, remote_path, direction="to"))


def scp_from(
    lane: LaneConfig,
    remote_path: str,
    local: Path,
    *,
    runner=None,
) -> None:
    selected_runner = run if runner is None else runner
    selected_runner(build_scp_argv(lane, remote_path, local, direction="from"))


def require_success_token(
    result: subprocess.CompletedProcess[str],
    token: str,
    label: str,
) -> None:
    if result.returncode != 0 or token not in (result.stdout or ""):
        die(f"{label} failed", detail=(result.stderr or result.stdout or ""))
