# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Typed native speaker-analysis failures."""

from __future__ import annotations

import re
from pathlib import Path

SPEAKER_ANALYSIS_FAILURE_PATH = "native"
SPEAKER_ANALYSIS_FAILURE_REASON = "speaker_analysis_native_failure"
SPEAKER_ANALYSIS_FAILURE_LABEL = "speaker-analysis-native-failure"
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SpeakerAnalyzeError(RuntimeError):
    """Content-free attribution for a failed native speaker-analysis attempt."""

    def __init__(
        self,
        *,
        path: Path,
        stage: str,
        reason: str,
        native_exit_code: int | None = None,
    ) -> None:
        safe_reason = (
            reason if _REASON_RE.fullmatch(reason) else "invalid-helper-reason"
        )
        super().__init__(f"speaker analysis failed: {stage}/{safe_reason}")
        self.path = Path(path)
        self.stage = stage
        self.reason = safe_reason
        self.native_exit_code = native_exit_code

    def event_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "speaker_analysis_failure_path": SPEAKER_ANALYSIS_FAILURE_PATH,
            "speaker_analysis_failure_stage": self.stage,
            "speaker_analysis_failure_reason": self.reason,
        }
        if self.native_exit_code is not None:
            fields["speaker_analysis_failure_native_exit_code"] = self.native_exit_code
        return fields


__all__ = [
    "SPEAKER_ANALYSIS_FAILURE_LABEL",
    "SPEAKER_ANALYSIS_FAILURE_PATH",
    "SPEAKER_ANALYSIS_FAILURE_REASON",
    "SpeakerAnalyzeError",
]
