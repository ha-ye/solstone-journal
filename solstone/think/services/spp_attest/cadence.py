# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Freshness cadence for process-local SPP attestation sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from solstone.think.services.spp_attest.composite import CompositeVerdict

# TPM quotes are cheap enough to refresh often, keeping the CPU binding fresh.
TPM_HEARTBEAT_INTERVAL = timedelta(minutes=10)
# GPU re-attestation is costlier, so it refreshes less often while still bounded.
GPU_REATTEST_INTERVAL = timedelta(minutes=30)
# Sessions are capped independently so long-lived processes cannot drift forever.
SESSION_CAP = timedelta(minutes=60)


@dataclass(frozen=True, slots=True)
class AttestationSession:
    verdict: CompositeVerdict
    started_at: datetime
    tpm_heartbeat_at: datetime
    gpu_reattest_at: datetime

    @property
    def tpm_heartbeat_due_at(self) -> datetime:
        return self.tpm_heartbeat_at + TPM_HEARTBEAT_INTERVAL

    @property
    def gpu_reattest_due_at(self) -> datetime:
        return self.gpu_reattest_at + GPU_REATTEST_INTERVAL

    @property
    def session_cap_at(self) -> datetime:
        return self.started_at + SESSION_CAP

    def status(self, now: datetime) -> str:
        if (
            now >= self.tpm_heartbeat_due_at
            or now >= self.gpu_reattest_due_at
            or now >= self.session_cap_at
        ):
            return "stale"
        return "verified"
