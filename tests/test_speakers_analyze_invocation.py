# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for bounded native speakers-analyze helper invocation."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from solstone.observe.transcribe.speakers_analyze_adapter import (
    SpeakersAnalyzeBudget,
    analyze_speakers,
    invoke_speakers_analyze_helper,
)
from solstone.observe.transcribe.speakers_analyze_errors import SpeakerAnalyzeError


def _budget(**overrides) -> SpeakersAnalyzeBudget:
    values = {
        "timeout_s": 1.0,
        "stdout_limit_bytes": 1024,
        "stderr_limit_bytes": 1024,
        "terminate_grace_s": 0.05,
        "kill_grace_s": 0.5,
    }
    values.update(overrides)
    return SpeakersAnalyzeBudget(**values)


def test_invocation_success_returns_captured_streams(tmp_path: Path):
    result = invoke_speakers_analyze_helper(
        [
            sys.executable,
            "-c",
            "import sys; print('ok'); print('warn', file=sys.stderr)",
        ],
        "{}",
        tmp_path / "audio.flac",
        budget=_budget(),
    )

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.stderr == "warn\n"


def test_timeout_terminates_and_reaps_child(tmp_path: Path):
    with pytest.raises(SpeakerAnalyzeError) as exc:
        invoke_speakers_analyze_helper(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            "{}",
            tmp_path / "audio.flac",
            budget=_budget(timeout_s=0.01),
        )

    assert exc.value.stage == "invoke"
    assert exc.value.reason == "timeout"


def test_stdin_write_timeout_terminates_reaps_and_cleans_temp_dir(tmp_path: Path):
    helper = tmp_path / "never_reads_stdin.py"
    helper.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    temp_dir = tmp_path / "adapter-temp"
    statements = [
        {"id": statement_id, "start": 0.0, "end": 1.0, "text": "x"}
        for statement_id in range(20_000)
    ]

    def temp_dir_factory(_raw_path: Path) -> Path:
        temp_dir.mkdir(mode=0o700)
        return temp_dir

    with pytest.raises(SpeakerAnalyzeError) as exc:
        analyze_speakers(
            raw_path=tmp_path / "audio.flac",
            full_audio=np.zeros(10, dtype=np.float32),
            statement_audio=np.zeros(10, dtype=np.float32),
            reduced_audio=None,
            statements_pre_restore=statements,
            statements_restored=statements,
            sample_rate=10,
            min_statement_duration=0.3,
            helper_locator=lambda: helper,
            helper_invoker=lambda argv, stdin, raw_path: invoke_speakers_analyze_helper(
                argv,
                stdin,
                raw_path,
                budget=_budget(timeout_s=0.01),
            ),
            model_path_resolver=lambda: (tmp_path / "w.onnx", tmp_path / "p.onnx"),
            temp_dir_factory=temp_dir_factory,
        )

    assert exc.value.stage == "invoke"
    assert exc.value.reason == "stdin-write-timeout"
    assert not temp_dir.exists()


@pytest.mark.parametrize(
    ("stream", "reason"),
    [("stdout", "stdout-too-large"), ("stderr", "stderr-too-large")],
)
def test_stream_bounds_terminate_and_reap_child(
    tmp_path: Path, stream: str, reason: str
):
    code = (
        "import sys, time; "
        f"sys.{stream}.write('x' * 2048); "
        f"sys.{stream}.flush(); "
        "time.sleep(10)"
    )

    with pytest.raises(SpeakerAnalyzeError) as exc:
        invoke_speakers_analyze_helper(
            [sys.executable, "-c", code],
            "{}",
            tmp_path / "audio.flac",
            budget=_budget(stdout_limit_bytes=64, stderr_limit_bytes=64),
        )

    assert exc.value.stage == "invoke"
    assert exc.value.reason == reason


def test_signal_killed_helper_is_typed_failure(tmp_path: Path):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()

    with pytest.raises(SpeakerAnalyzeError) as exc:
        analyze_speakers(
            raw_path=tmp_path / "audio.flac",
            full_audio=np.zeros(20, dtype=np.float32),
            statement_audio=np.zeros(20, dtype=np.float32),
            reduced_audio=None,
            statements_pre_restore=[{"id": 1, "start": 0.0, "end": 0.5, "text": "x"}],
            statements_restored=[{"id": 1, "start": 0.0, "end": 0.5, "text": "x"}],
            sample_rate=10,
            min_statement_duration=0.3,
            helper_locator=lambda: tmp_path / "helper",
            helper_invoker=lambda _argv, _stdin, _raw_path: type(
                "Result",
                (),
                {"returncode": -9, "stdout": "", "stderr": ""},
            )(),
            model_path_resolver=lambda: (tmp_path / "w.onnx", tmp_path / "p.onnx"),
            temp_dir_factory=lambda _raw_path: temp_dir,
        )

    assert exc.value.stage == "invoke"
    assert exc.value.reason == "signal-9"
    assert exc.value.native_exit_code == -9


def test_malformed_json_response_is_typed_failure(tmp_path: Path):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()

    with pytest.raises(SpeakerAnalyzeError) as exc:
        analyze_speakers(
            raw_path=tmp_path / "audio.flac",
            full_audio=np.zeros(20, dtype=np.float32),
            statement_audio=np.zeros(20, dtype=np.float32),
            reduced_audio=None,
            statements_pre_restore=[{"id": 1, "start": 0.0, "end": 0.5, "text": "x"}],
            statements_restored=[{"id": 1, "start": 0.0, "end": 0.5, "text": "x"}],
            sample_rate=10,
            min_statement_duration=0.3,
            helper_locator=lambda: tmp_path / "helper",
            helper_invoker=lambda _argv, _stdin, _raw_path: type(
                "Result",
                (),
                {"returncode": 0, "stdout": "{", "stderr": ""},
            )(),
            model_path_resolver=lambda: (tmp_path / "w.onnx", tmp_path / "p.onnx"),
            temp_dir_factory=lambda _raw_path: temp_dir,
        )

    assert exc.value.stage == "parse"
    assert exc.value.reason == "malformed-response"
