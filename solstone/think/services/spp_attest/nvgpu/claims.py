# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure nvattest stdout parsing and claim appraisal."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalReason
from solstone.think.services.spp_attest.snp import AppraisalStep
from solstone.think.services.spp_attest.tlv import GpuEnvelope


@dataclass(frozen=True, slots=True)
class GpuAppraisal:
    """GPU appraisal provenance.

    arch and envelope_gpu_uuid are copied from our SPP GPU envelope fields 7 and
    6. They are not nvattest-verified claims, so they must not be surfaced as
    verified provenance.
    """

    steps: list[AppraisalStep]
    driver_version: str
    vbios_version: str
    hwmodel: str
    ueid: str
    oemid: str
    eat_nonce: str
    claims_version: str
    arch: str
    envelope_gpu_uuid: str


@dataclass(frozen=True, slots=True)
class NvattestAcceptance:
    claim: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NvattestRejection:
    reason: GpuAppraisalReason
    detail: str


class _ClaimReject(Exception):
    pass


_REPORT_TRUE_KEYS = (
    "x-nvidia-gpu-attestation-report-parsed",
    "x-nvidia-gpu-attestation-report-signature-verified",
    "x-nvidia-gpu-attestation-report-nonce-match",
    "x-nvidia-gpu-attestation-report-cert-chain-fwid-match",
    "x-nvidia-gpu-arch-check",
)
_DRIVER_RIM_TRUE_KEYS = (
    "x-nvidia-gpu-driver-rim-signature-verified",
    "x-nvidia-gpu-driver-rim-version-match",
    "x-nvidia-gpu-driver-rim-measurements-available",
)
_VBIOS_RIM_TRUE_KEYS = (
    "x-nvidia-gpu-vbios-rim-signature-verified",
    "x-nvidia-gpu-vbios-rim-version-match",
    "x-nvidia-gpu-vbios-rim-measurements-available",
    "x-nvidia-gpu-vbios-index-no-conflict",
)
_CERT_CHAIN_KEYS = (
    "x-nvidia-gpu-attestation-report-cert-chain",
    "x-nvidia-gpu-driver-rim-cert-chain",
    "x-nvidia-gpu-vbios-rim-cert-chain",
)


def parse_nvattest_stdout(stdout: str) -> object:
    """Parse nvattest stdout as JSON."""

    if not stdout:
        raise ValueError("nvattest stdout is empty")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"nvattest stdout did not parse: {exc}") from exc


def classify_nvattest_result(
    returncode: int,
    stdout_obj: object,
    *,
    owner_nonce: bytes,
) -> NvattestAcceptance | NvattestRejection:
    """Return a fail-closed appraisal decision without consulting stderr."""

    if not isinstance(stdout_obj, dict):
        return NvattestRejection(
            "gpu_appraisal_failed", "nvattest stdout is not an object"
        )

    try:
        result_code = _required_stdout(stdout_obj, "result_code")
        result_message = _required_stdout(stdout_obj, "result_message")
    except _ClaimReject as exc:
        return NvattestRejection("gpu_appraisal_failed", str(exc))
    returncode_is_green = (
        isinstance(returncode, int)
        and not isinstance(returncode, bool)
        and returncode == 0
    )
    result_code_is_green = (
        isinstance(result_code, int)
        and not isinstance(result_code, bool)
        and result_code == 0
    )
    result_message_is_green = isinstance(result_message, str) and result_message == "Ok"
    if (
        not returncode_is_green
        or not result_code_is_green
        or not result_message_is_green
    ):
        return NvattestRejection(
            _reason_for_result_code(result_code),
            (
                "nvattest non-green result: "
                f"returncode={returncode} result_code={result_code!r} "
                f"result_message={result_message!r}"
            ),
        )

    try:
        claims = _required_stdout(stdout_obj, "claims")
    except _ClaimReject as exc:
        return NvattestRejection("gpu_appraisal_failed", str(exc))
    if not isinstance(claims, list) or len(claims) != 1:
        return NvattestRejection(
            "gpu_appraisal_failed",
            "nvattest claims is not a list of exactly one object",
        )
    claim = claims[0]
    if not isinstance(claim, dict):
        return NvattestRejection(
            "gpu_appraisal_failed", "nvattest claim is not an object"
        )

    try:
        _parse_overall_eat(_required_stdout(stdout_obj, "detached_eat"))
        _check_claim(claim, owner_nonce.hex())
    except _ClaimReject as exc:
        return NvattestRejection("gpu_appraisal_failed", str(exc))
    return NvattestAcceptance(claim=claim)


def build_gpu_appraisal(
    *,
    claim: dict[str, Any],
    envelope: GpuEnvelope,
    steps: list[AppraisalStep],
) -> GpuAppraisal:
    """Build GPU provenance from accepted nvattest claims and envelope metadata."""

    try:
        return GpuAppraisal(
            steps=steps,
            driver_version=_claim_str(claim, "x-nvidia-gpu-driver-version"),
            vbios_version=_claim_str(claim, "x-nvidia-gpu-vbios-version"),
            hwmodel=_claim_str(claim, "hwmodel"),
            ueid=_claim_str(claim, "ueid"),
            oemid=_claim_str(claim, "oemid"),
            eat_nonce=_claim_str(claim, "eat_nonce"),
            claims_version=_claim_str(claim, "x-nvidia-gpu-claims-version"),
            arch=envelope.field(7).decode("utf-8").upper(),
            envelope_gpu_uuid=envelope.field(6).decode("utf-8"),
        )
    except _ClaimReject as exc:
        raise ValueError(str(exc)) from exc


def _reason_for_result_code(result_code: object) -> GpuAppraisalReason:
    if result_code == 504:
        return "gpu_nonce_mismatch"
    return "gpu_appraisal_failed"


def _required_stdout(stdout: dict[str, Any], key: str) -> Any:
    try:
        return stdout[key]
    except KeyError as exc:
        raise _ClaimReject(f"nvattest stdout missing key {key!r}") from exc


def _required_claim(claim: dict[str, Any], key: str) -> Any:
    try:
        return claim[key]
    except KeyError as exc:
        raise _ClaimReject(f"nvattest claim missing key {key!r}") from exc


def _claim_str(claim: dict[str, Any], key: str) -> str:
    value = _required_claim(claim, key)
    if not isinstance(value, str):
        raise _ClaimReject(f"nvattest claim {key!r} is not a string")
    return value


def _require_equal(claim: dict[str, Any], key: str, expected: object) -> None:
    value = _required_claim(claim, key)
    if value != expected:
        raise _ClaimReject(f"nvattest claim {key!r}={value!r}, expected {expected!r}")


def _require_is(claim: dict[str, Any], key: str, expected: object) -> None:
    value = _required_claim(claim, key)
    if value is not expected:
        raise _ClaimReject(f"nvattest claim {key!r}={value!r}, expected {expected!r}")


def _check_claim(claim: dict[str, Any], owner_nonce_hex: str) -> None:
    _require_equal(claim, "x-nvidia-gpu-claims-version", "3.0")
    _require_equal(claim, "x-nvidia-device-type", "gpu")
    _require_equal(claim, "measres", "success")
    _require_is(claim, "secboot", True)
    _require_equal(claim, "dbgstat", "disabled")
    _require_equal(claim, "eat_nonce", owner_nonce_hex)

    for key in _REPORT_TRUE_KEYS:
        _require_is(claim, key, True)
    for key in _DRIVER_RIM_TRUE_KEYS:
        _require_is(claim, key, True)
    for key in _VBIOS_RIM_TRUE_KEYS:
        _require_is(claim, key, True)

    # Deliberately not checking *-rim-fetched: legitimate --rim-store dir runs
    # verify local RIM data while reporting those fetch markers as false.
    _require_is(claim, "x-nvidia-mismatch-measurement-records", None)
    for key in _CERT_CHAIN_KEYS:
        _check_cert_chain(_required_claim(claim, key), key)


def _check_cert_chain(value: object, key: str) -> None:
    if not isinstance(value, dict):
        raise _ClaimReject(f"nvattest claim {key!r} is not an object")
    _require_equal(value, "x-nvidia-cert-status", "valid")
    _require_equal(value, "x-nvidia-cert-ocsp-status", "good")
    _require_is(value, "x-nvidia-cert-ocsp-response-valid", True)
    _require_is(value, "x-nvidia-cert-ocsp-nonce-matches", True)
    _require_is(value, "x-nvidia-cert-revocation-reason", None)


def _parse_overall_eat(detached_eat: object) -> dict[str, Any]:
    if not isinstance(detached_eat, list) or not detached_eat:
        raise _ClaimReject("nvattest detached_eat does not contain an overall JWT")
    overall = detached_eat[0]
    if (
        not isinstance(overall, list)
        or len(overall) != 2
        or overall[0] != "JWT"
        or not isinstance(overall[1], str)
    ):
        raise _ClaimReject("nvattest detached_eat overall JWT has invalid shape")

    parts = overall[1].split(".")
    if len(parts) != 3:
        raise _ClaimReject("nvattest overall JWT does not have three segments")
    header = _decode_jwt_segment(parts[0], "header")
    payload = _decode_jwt_segment(parts[1], "payload")
    try:
        alg = header["alg"]
        iss = payload["iss"]
        overall_result = payload["x-nvidia-overall-att-result"]
    except KeyError as exc:
        raise _ClaimReject(f"nvattest overall JWT missing key {exc.args[0]!r}") from exc
    if alg != "none":
        raise _ClaimReject(f"nvattest overall JWT alg={alg!r}, expected 'none'")
    if iss != "NVAT-LOCAL-VERIFIER":
        raise _ClaimReject(
            f"nvattest overall JWT iss={iss!r}, expected 'NVAT-LOCAL-VERIFIER'"
        )
    if overall_result is not True:
        raise _ClaimReject("nvattest overall attestation result is not true")
    return payload


def _decode_jwt_segment(segment: str, label: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        decoded = json.loads(raw)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _ClaimReject(f"nvattest overall JWT {label} did not parse") from exc
    if not isinstance(decoded, dict):
        raise _ClaimReject(f"nvattest overall JWT {label} is not an object")
    return decoded
