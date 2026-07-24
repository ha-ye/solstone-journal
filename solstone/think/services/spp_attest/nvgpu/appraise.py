# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shell-out NVIDIA GPU-leg appraisal for SPP attestation."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from solstone.think.services.spp_attest.nvgpu.binary import (
    build_nvattest_attest_command,
)
from solstone.think.services.spp_attest.nvgpu.claims import (
    GpuAppraisal,
    NvattestAcceptance,
    build_gpu_appraisal,
    classify_nvattest_result,
    parse_nvattest_stdout,
)
from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.nvgpu.evidence import to_nvattest_evidence
from solstone.think.services.spp_attest.snp import AppraisalStep
from solstone.think.services.spp_attest.tlv import GpuEnvelope

log = logging.getLogger(__name__)
NVATTEST_TIMEOUT_S = 60.0


def appraise_gpu_leg(
    envelope: GpuEnvelope,
    owner_nonce: bytes,
    *,
    nvattest_dir: Path,
    rim_store: str = "remote",
    rim_dir: Path | None = None,
) -> GpuAppraisal:
    """Appraise the NVIDIA GPU side of an SPP composite attestation.

    ``--rim-store remote`` performs network egress inside the subprocess: a fetch
    of NVIDIA's signed RIM + OCSP reference data only (public golden
    measurements + revocation), never journal content, and the verdict is still
    computed locally. "Offline" here means no local GPU and no NRAS, not "no
    network". ``rim_store="dir"`` + ``--rim-dir`` is the fully-offline-RIM path
    (OCSP still needs network).
    """

    evidence_path: Path | None = None
    try:
        evidence = to_nvattest_evidence(envelope, owner_nonce)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=tempfile.gettempdir(),
            delete=False,
            encoding="utf-8",
            prefix="solstone-nvattest-",
            suffix=".json",
        ) as handle:
            evidence_path = Path(handle.name)
            handle.write(json.dumps(evidence, sort_keys=True))
            handle.write("\n")

        try:
            command = build_nvattest_attest_command(
                nvattest_dir=nvattest_dir,
                evidence_file=evidence_path,
                owner_nonce=owner_nonce,
                rim_store=rim_store,
                rim_dir=rim_dir,
            )
        except GpuAppraisalError as exc:
            _log_gpu_appraisal_failure(
                exc.reason,
                exception_class=type(exc).__name__,
                stderr=None,
            )
            raise
        try:
            completed = subprocess.run(
                command.argv,
                env=command.env,
                capture_output=True,
                text=True,
                check=False,
                timeout=NVATTEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            _log_gpu_appraisal_failure(
                "gpu_appraisal_failed",
                exception_class=type(exc).__name__,
                stderr=exc.stderr,
            )
            raise GpuAppraisalError("gpu_appraisal_failed") from exc
        except OSError as exc:
            _log_gpu_appraisal_failure(
                "nvattest_unavailable",
                exception_class=type(exc).__name__,
                stderr=None,
            )
            raise GpuAppraisalError("nvattest_unavailable") from exc

        try:
            stdout_obj = parse_nvattest_stdout(completed.stdout)
        except ValueError as exc:
            _log_gpu_appraisal_failure(
                "gpu_appraisal_failed",
                returncode=completed.returncode,
                stderr=completed.stderr,
            )
            raise GpuAppraisalError("gpu_appraisal_failed") from exc

        decision = classify_nvattest_result(
            completed.returncode,
            stdout_obj,
            owner_nonce=owner_nonce,
        )
        if not isinstance(decision, NvattestAcceptance):
            _log_gpu_appraisal_failure(
                decision.reason,
                returncode=completed.returncode,
                stderr=completed.stderr,
            )
            raise GpuAppraisalError(decision.reason)

        steps = [
            _ok(
                "nvattest",
                "returncode=0 result_code=0 result_message=Ok",
            ),
            _ok(
                "overall-eat",
                "alg=none iss=NVAT-LOCAL-VERIFIER overall_att_result=True",
            ),
            _ok(
                "gpu-claims",
                "claims-version=3.0 report, driver-RIM, vbios-RIM checks passed",
            ),
        ]
        try:
            return build_gpu_appraisal(
                claim=decision.claim,
                envelope=envelope,
                steps=steps,
            )
        except ValueError as exc:
            _log_gpu_appraisal_failure(
                "gpu_appraisal_failed",
                returncode=completed.returncode,
                stderr=completed.stderr,
            )
            raise GpuAppraisalError("gpu_appraisal_failed") from exc
    finally:
        if evidence_path is not None:
            evidence_path.unlink(missing_ok=True)


def _ok(name: str, detail: str) -> AppraisalStep:
    return AppraisalStep(name=name, status="ok", detail=detail)


def _log_gpu_appraisal_failure(
    reason_code: str,
    *,
    stderr: str | bytes | None,
    returncode: object | None = None,
    exception_class: str | None = None,
) -> None:
    stderr_bytes = _stderr_bytes(stderr)
    digest = hashlib.sha256(stderr_bytes).hexdigest()[:16]
    if exception_class is not None:
        log.warning(
            "event=nvattest_gpu_appraisal_failed reason=%s exception=%s "
            "stderr_len=%d stderr_sha256=%s",
            reason_code,
            exception_class,
            len(stderr_bytes),
            digest,
        )
        return
    log.warning(
        "event=nvattest_gpu_appraisal_failed reason=%s returncode=%s "
        "stderr_len=%d stderr_sha256=%s",
        reason_code,
        returncode,
        len(stderr_bytes),
        digest,
    )


def _stderr_bytes(stderr: str | bytes | None) -> bytes:
    if stderr is None:
        return b""
    if isinstance(stderr, bytes):
        return stderr
    return stderr.encode("utf-8", "surrogateescape")
