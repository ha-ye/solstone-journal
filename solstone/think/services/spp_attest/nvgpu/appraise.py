# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shell-out NVIDIA GPU-leg appraisal for SPP attestation."""

from __future__ import annotations

import json
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

        command = build_nvattest_attest_command(
            nvattest_dir=nvattest_dir,
            evidence_file=evidence_path,
            owner_nonce=owner_nonce,
            rim_store=rim_store,
            rim_dir=rim_dir,
        )
        try:
            completed = subprocess.run(
                command.argv,
                env=command.env,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise GpuAppraisalError(
                "nvattest_unavailable",
                f"failed to execute nvattest: {exc}",
            ) from exc

        try:
            stdout_obj = parse_nvattest_stdout(completed.stdout)
        except ValueError as exc:
            raise GpuAppraisalError(
                "gpu_appraisal_failed",
                str(exc),
                stderr=completed.stderr,
            ) from exc

        decision = classify_nvattest_result(
            completed.returncode,
            stdout_obj,
            owner_nonce=owner_nonce,
        )
        if not isinstance(decision, NvattestAcceptance):
            raise GpuAppraisalError(
                decision.reason,
                decision.detail,
                stderr=completed.stderr,
            )

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
            raise GpuAppraisalError(
                "gpu_appraisal_failed",
                str(exc),
                stderr=completed.stderr,
            ) from exc
    finally:
        if evidence_path is not None:
            evidence_path.unlink(missing_ok=True)


def _ok(name: str, detail: str) -> AppraisalStep:
    return AppraisalStep(name=name, status="ok", detail=detail)
