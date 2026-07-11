# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import base64
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import solstone.think.services.spp_attest.nvgpu.appraise as appraise_module
from solstone.think.services.spp_attest.nvgpu.binary import (
    build_nvattest_attest_command,
)
from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.nvgpu.evidence import to_nvattest_evidence
from solstone.think.services.spp_attest.tlv import decode_gpu_envelope

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spp_attest"
NVATTEST_DIR = FIXTURE_DIR / "nvattest"


def _envelope():
    return decode_gpu_envelope((FIXTURE_DIR / "gpu-envelope.tlv").read_bytes())


def _owner_nonce() -> bytes:
    return bytes.fromhex("".join((FIXTURE_DIR / "nonce.hex").read_text().split()))


def _stdout(name: str) -> str:
    return (NVATTEST_DIR / f"{name}.stdout").read_text(encoding="utf-8")


def _stderr(name: str) -> str:
    return (NVATTEST_DIR / f"{name}.stderr").read_text(encoding="utf-8")


def _positive_body() -> dict[str, Any]:
    data = json.loads(_stdout("positive"))
    assert isinstance(data, dict)
    return data


def _fake_nvattest_dir(tmp_path: Path) -> Path:
    root = tmp_path / "nvattest"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "nvattest").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "lib").mkdir(exist_ok=True)
    return root


def _run_appraisal_with_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
    *,
    stderr: str = "",
    returncode: int = 0,
    observed: dict[str, Any] | None = None,
    rim_store: str = "remote",
    rim_dir: Path | None = None,
):
    nvattest_dir = _fake_nvattest_dir(tmp_path)

    def fake_run(argv, **kwargs):
        if observed is not None:
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            evidence_path = Path(argv[argv.index("--gpu-evidence-file") + 1])
            observed["evidence"] = json.loads(evidence_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(appraise_module.subprocess, "run", fake_run)
    return appraise_module.appraise_gpu_leg(
        _envelope(),
        _owner_nonce(),
        nvattest_dir=nvattest_dir,
        rim_store=rim_store,
        rim_dir=rim_dir,
    )


def _body_stdout(body: dict[str, Any]) -> str:
    return json.dumps(body, indent=2, sort_keys=True)


def _claim(body: dict[str, Any]) -> dict[str, Any]:
    claims = body["claims"]
    assert isinstance(claims, list)
    claim = claims[0]
    assert isinstance(claim, dict)
    return claim


def _set_path(body: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node: dict[str, Any] = body
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _delete_claim_key(body: dict[str, Any], key: str) -> None:
    del _claim(body)[key]


def _decode_jwt_segment(segment: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data


def _encode_jwt(header: dict[str, Any], payload: dict[str, Any]) -> str:
    def encode(data: dict[str, Any]) -> str:
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode(header)}.{encode(payload)}."


def _mutate_overall_jwt(body: dict[str, Any], *, header=None, payload=None) -> None:
    jwt = body["detached_eat"][0][1]
    parts = jwt.split(".")
    original_header = _decode_jwt_segment(parts[0])
    original_payload = _decode_jwt_segment(parts[1])
    if header:
        original_header.update(header)
    if payload:
        original_payload.update(payload)
    body["detached_eat"][0][1] = _encode_jwt(original_header, original_payload)


def test_to_nvattest_evidence_transforms_gpu_envelope() -> None:
    envelope = _envelope()
    evidence = to_nvattest_evidence(envelope, _owner_nonce())

    assert isinstance(evidence, list)
    assert len(evidence) == 1
    item = evidence[0]
    assert item["arch"] == "HOPPER"
    assert item["nonce"] == _owner_nonce().hex()
    assert base64.b64decode(item["evidence"]) == envelope.field(2)
    assert base64.b64decode(item["certificate"]) == envelope.field(3)


def test_appraise_gpu_leg_accepts_positive_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_appraisal_with_stdout(
        monkeypatch,
        tmp_path,
        _stdout("positive"),
        stderr=_stderr("positive"),
    )

    assert result.driver_version == "595.71.05"
    assert result.vbios_version == "96.00.88.00.11"
    assert result.hwmodel == "GH100 A01 GSP BROM"
    assert result.envelope_gpu_uuid == "GPU-256cc88f-e93b-9396-b581-274543ea3235"
    assert [step.name for step in result.steps] == [
        "nvattest",
        "overall-eat",
        "gpu-claims",
    ]


@pytest.mark.parametrize(
    ("name", "marker"),
    [
        ("negA", "get_evidence"),
        ("negB", "get_evidence"),
        ("negC", "generate_gpu_evidence_claims"),
    ],
)
def test_appraise_gpu_leg_classifies_negative_nonce_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    marker: str,
) -> None:
    with pytest.raises(GpuAppraisalError) as exc_info:
        _run_appraisal_with_stdout(
            monkeypatch,
            tmp_path,
            _stdout(name),
            stderr=_stderr(name),
        )

    assert exc_info.value.reason == "gpu_nonce_mismatch"
    assert marker in exc_info.value.stderr
    if name == "negC":
        assert "nonce_from_ar" in exc_info.value.stderr


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        (
            "secboot false",
            lambda body: _set_path(body, ("claims", 0, "secboot"), False),
        ),
        (
            "secboot string",
            lambda body: _set_path(body, ("claims", 0, "secboot"), "true"),
        ),
        ("secboot int", lambda body: _set_path(body, ("claims", 0, "secboot"), 1)),
        (
            "debug enabled",
            lambda body: _set_path(body, ("claims", 0, "dbgstat"), "enabled"),
        ),
        (
            "driver rim signature",
            lambda body: _set_path(
                body,
                ("claims", 0, "x-nvidia-gpu-driver-rim-signature-verified"),
                False,
            ),
        ),
        (
            "measres failure",
            lambda body: _set_path(body, ("claims", 0, "measres"), "fail"),
        ),
        (
            "missing key",
            lambda body: _delete_claim_key(
                body,
                "x-nvidia-gpu-attestation-report-parsed",
            ),
        ),
        (
            "ocsp bad",
            lambda body: _set_path(
                body,
                (
                    "claims",
                    0,
                    "x-nvidia-gpu-attestation-report-cert-chain",
                    "x-nvidia-cert-ocsp-status",
                ),
                "revoked",
            ),
        ),
        ("claims dict", lambda body: _set_path(body, ("claims",), {})),
        ("result code false", lambda body: _set_path(body, ("result_code",), False)),
        (
            "claims version",
            lambda body: _set_path(
                body,
                ("claims", 0, "x-nvidia-gpu-claims-version"),
                "4.0",
            ),
        ),
    ],
)
def test_appraise_gpu_leg_fail_closed_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    mutate,
) -> None:
    body = _positive_body()
    mutate(body)

    with pytest.raises(GpuAppraisalError) as exc_info:
        _run_appraisal_with_stdout(monkeypatch, tmp_path, _body_stdout(body))

    assert label
    assert exc_info.value.reason == "gpu_appraisal_failed"


@pytest.mark.parametrize("stdout", ["", "not json"])
def test_appraise_gpu_leg_rejects_empty_or_garbage_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> None:
    with pytest.raises(GpuAppraisalError) as exc_info:
        _run_appraisal_with_stdout(monkeypatch, tmp_path, stdout)

    assert exc_info.value.reason == "gpu_appraisal_failed"


def test_appraise_gpu_leg_rejects_missing_nvattest_dir() -> None:
    with pytest.raises(GpuAppraisalError) as exc_info:
        appraise_module.appraise_gpu_leg(
            _envelope(),
            _owner_nonce(),
            nvattest_dir=Path("/does/not/exist"),
        )

    assert exc_info.value.reason == "nvattest_unavailable"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: _set_path(body, ("detached_eat",), {}),
        lambda body: _set_path(body, ("detached_eat",), []),
        lambda body: _mutate_overall_jwt(body, header={"alg": "HS256"}),
        lambda body: _mutate_overall_jwt(body, payload={"iss": "OTHER"}),
    ],
)
def test_overall_eat_is_veto_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
) -> None:
    body = _positive_body()
    mutate(body)

    with pytest.raises(GpuAppraisalError):
        _run_appraisal_with_stdout(monkeypatch, tmp_path, _body_stdout(body))

    green_eat_flipped_claim = _positive_body()
    _set_path(
        green_eat_flipped_claim,
        ("claims", 0, "x-nvidia-gpu-vbios-rim-version-match"),
        False,
    )
    with pytest.raises(GpuAppraisalError):
        _run_appraisal_with_stdout(
            monkeypatch,
            tmp_path,
            _body_stdout(green_eat_flipped_claim),
        )


def test_stderr_does_not_influence_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_appraisal_with_stdout(
        monkeypatch,
        tmp_path,
        _stdout("positive"),
        stderr=_stderr("negC"),
    )

    assert result.driver_version == "595.71.05"


def test_nonce_is_written_to_evidence_and_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    _run_appraisal_with_stdout(
        monkeypatch,
        tmp_path,
        _stdout("positive"),
        observed=observed,
    )

    argv = observed["argv"]
    evidence = observed["evidence"][0]
    assert evidence["nonce"] == _owner_nonce().hex()
    assert argv[argv.index("--nonce") + 1] == _owner_nonce().hex()


def test_nvattest_command_env_inherits_parent_and_sets_library_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nvattest_dir = _fake_nvattest_dir(tmp_path)
    monkeypatch.setenv("SPP_NVATTEST_PARENT_SENTINEL", "kept")

    command = build_nvattest_attest_command(
        nvattest_dir=nvattest_dir,
        evidence_file=tmp_path / "evidence.json",
        owner_nonce=_owner_nonce(),
    )

    assert command.env["SPP_NVATTEST_PARENT_SENTINEL"] == "kept"
    assert command.env["LD_LIBRARY_PATH"] == str(nvattest_dir / "lib")


def test_rim_store_dir_argv_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    rim_dir = tmp_path / "rim"
    rim_dir.mkdir()

    _run_appraisal_with_stdout(
        monkeypatch,
        tmp_path,
        _stdout("positive"),
        observed=observed,
        rim_store="dir",
        rim_dir=rim_dir,
    )

    argv = observed["argv"]
    assert argv[argv.index("--rim-store") + 1] == "dir"
    assert argv[argv.index("--rim-dir") + 1] == str(rim_dir)

    with pytest.raises(ValueError, match="rim_dir is required"):
        _run_appraisal_with_stdout(
            monkeypatch,
            tmp_path,
            _stdout("positive"),
            rim_store="dir",
        )
    with pytest.raises(ValueError, match="only valid"):
        _run_appraisal_with_stdout(
            monkeypatch,
            tmp_path,
            _stdout("positive"),
            rim_store="remote",
            rim_dir=rim_dir,
        )


def test_driver_and_vbios_rim_fetched_are_not_predicate_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = deepcopy(_positive_body())
    _set_path(body, ("claims", 0, "x-nvidia-gpu-driver-rim-fetched"), False)
    _set_path(body, ("claims", 0, "x-nvidia-gpu-vbios-rim-fetched"), False)

    result = _run_appraisal_with_stdout(monkeypatch, tmp_path, _body_stdout(body))

    assert result.driver_version == "595.71.05"
