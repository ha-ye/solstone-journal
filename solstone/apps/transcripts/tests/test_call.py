# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Golden parity tests for transcripts CLI commands."""

import pytest
import requests
from typer.testing import CliRunner

from solstone.apps.transcripts.call import app
from solstone.think.convey_client import ConveyClient
from tests._baseline_harness import make_logged_in_test_client


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.transcripts.call.get_client", lambda: client)
    return CliRunner()


def _write_segment(
    journal_root,
    day: str,
    segment: str,
    *,
    audio_jsonl: bool = False,
    audio_flac: bool = False,
    screen_jsonl: bool = False,
) -> None:
    segment_dir = journal_root / "chronicle" / day / "default" / segment
    segment_dir.mkdir(parents=True, exist_ok=True)
    if audio_jsonl:
        (segment_dir / "audio.jsonl").write_text(
            '{"raw": "audio.flac"}\n{"start": "00:00:01", "text": "audio"}\n',
            encoding="utf-8",
        )
    if audio_flac:
        (segment_dir / "audio.flac").write_bytes(b"audio")
    if screen_jsonl:
        (segment_dir / "screen.jsonl").write_text(
            '{"raw": "screen.webm"}\n'
            '{"timestamp": 1, "analysis": {"primary": "work"}}\n',
            encoding="utf-8",
        )


def _route_markdown(journal, day: str, params: dict[str, str]) -> str:
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    return client.request("GET", f"/app/transcripts/api/read/{day}", params=params)[
        "markdown"
    ]


def test_scan_output_byte_identical_when_no_pending_segments(runner, journal):
    day = "20990102"
    _write_segment(
        journal,
        day,
        "090000_300",
        audio_jsonl=True,
        screen_jsonl=True,
    )

    result = runner.invoke(app, ["scan", day])

    assert result.exit_code == 0
    assert result.output == (
        "Transcripts:\n  09:00 - 09:15\nPercepts:\n  09:00 - 09:15\n"
    )


def test_scan_output_annotates_pending_inside_range(runner, journal):
    day = "20990103"
    _write_segment(journal, day, "090000_300", audio_jsonl=True)
    _write_segment(journal, day, "090500_300", audio_flac=True)

    result = runner.invoke(app, ["scan", day])

    assert result.exit_code == 0
    assert result.output == (
        "Transcripts:\n"
        "  09:00 - 09:15 (1 segment pending at 09:05)\n"
        "Percepts:\n"
        "  (none)\n"
    )


def test_scan_output_reports_pending_only_range(runner, journal):
    day = "20990104"
    _write_segment(journal, day, "091500_300", audio_flac=True)

    result = runner.invoke(app, ["scan", day])

    assert result.exit_code == 0
    assert result.output == (
        "Transcripts:\n"
        "  09:15 - 09:30 (1 segment pending at 09:15)\n"
        "Percepts:\n"
        "  (none)\n"
    )


def test_scan_output_pluralizes_multiple_pending(runner, journal):
    day = "20990105"
    _write_segment(journal, day, "090000_300", audio_jsonl=True)
    _write_segment(journal, day, "090500_300", audio_flac=True)
    _write_segment(journal, day, "091000_300", audio_flac=True)

    result = runner.invoke(app, ["scan", day])

    assert result.exit_code == 0
    assert result.output == (
        "Transcripts:\n"
        "  09:00 - 09:15 (2 segments pending at 09:05, 09:10)\n"
        "Percepts:\n"
        "  (none)\n"
    )


def test_scan_empty_day(runner):
    result = runner.invoke(app, ["scan", "20990101"])

    assert result.exit_code == 0
    assert result.output == "Transcripts:\n  (none)\nPercepts:\n  (none)\n"


def test_segments_seeded_day_byte_identical(runner, journal):
    day = "20990106"
    _write_segment(journal, day, "090000_300", audio_jsonl=True, screen_jsonl=True)

    result = runner.invoke(app, ["segments", day])

    assert result.exit_code == 0
    assert result.output == "090000_300  09:00 - 09:05  [audio, screen]\n"


def test_segments_empty(runner):
    result = runner.invoke(app, ["segments", "20990101"])

    assert result.exit_code == 0
    assert result.output == "No segments.\n"


def test_read_default_matches_route_markdown(runner, journal):
    day = "20990107"
    _write_segment(journal, day, "090000_300", audio_jsonl=True)
    expected = _route_markdown(
        journal,
        day,
        {"transcripts": "1", "percepts": "0", "agents": "1"},
    )

    result = runner.invoke(app, ["read", day])

    assert result.exit_code == 0
    assert result.stdout == expected + "\n"


@pytest.mark.parametrize(
    ("args", "stderr"),
    [
        (
            ["read", "20990107", "--full", "--raw"],
            "Error: Cannot use --full and --raw together.\n",
        ),
        (
            ["read", "20990107", "--full", "--transcripts"],
            "Error: Cannot mix --full/--raw with individual source flags.\n",
        ),
        (
            [
                "read",
                "20990107",
                "--segment",
                "090000_300",
                "--start",
                "090000",
                "--length",
                "5",
            ],
            "Error: Cannot mix --segment, --segments, and --start/--length.\n",
        ),
        (
            ["read", "20990107", "--start", "090000"],
            "Error: --start and --length must be used together.\n",
        ),
    ],
)
def test_read_cli_side_validation_errors_byte_exact(runner, args, stderr):
    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert result.stderr == stderr
    assert result.stdout == ""


def test_read_truncation_reports_exact_byte_counts(runner, journal):
    day = "20990108"
    max_bytes = 12
    _write_segment(journal, day, "090000_300", audio_jsonl=True)
    expected = _route_markdown(
        journal,
        day,
        {"transcripts": "1", "percepts": "0", "agents": "1"},
    )
    expected_stdout = expected.encode("utf-8")[:max_bytes].decode(
        "utf-8", errors="ignore"
    )

    result = runner.invoke(app, ["read", day, "--max", str(max_bytes)])

    assert result.exit_code == 0
    assert result.stdout == expected_stdout + "\n"
    assert result.stderr == (
        f"[truncated: {len(expected.encode('utf-8')):,} bytes total, "
        f"--max {max_bytes:,}]\n"
    )


def test_stats_seeded_month_byte_identical(runner, journal):
    day = "20990109"
    _write_segment(journal, day, "090000_300", audio_jsonl=True, screen_jsonl=True)

    result = runner.invoke(app, ["stats", "209901"])

    assert result.exit_code == 0
    assert result.output == (
        "20990109  transcripts:1 percepts:1\n\nTotal: 1 days with data\n"
    )


def test_stats_empty_month(runner):
    result = runner.invoke(app, ["stats", "209902"])

    assert result.exit_code == 0
    assert result.output == "No data for 209902.\n"


def test_scan_from_sol_day(runner, journal, monkeypatch):
    day = "20990110"
    _write_segment(journal, day, "090000_300", audio_jsonl=True)
    monkeypatch.setenv("SOL_DAY", day)

    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 0
    assert result.output == "Transcripts:\n  09:00 - 09:15\nPercepts:\n  (none)\n"


def test_read_from_sol_day(runner, journal, monkeypatch):
    day = "20990111"
    _write_segment(journal, day, "090000_300", audio_jsonl=True)
    monkeypatch.setenv("SOL_DAY", day)

    result = runner.invoke(app, ["read"])

    assert result.exit_code == 0
    assert "## 2099-01-11 09:00:00 - 09:05:00" in result.stdout


def test_read_from_sol_day_and_segment(runner, journal, monkeypatch):
    day = "20990112"
    _write_segment(journal, day, "090000_300", audio_jsonl=True)
    monkeypatch.setenv("SOL_DAY", day)
    monkeypatch.setenv("SOL_SEGMENT", "090000_300")

    result = runner.invoke(app, ["read"])

    assert result.exit_code == 0
    assert "## 2099-01-12 09:00:00 - 09:05:00" in result.stdout


def test_read_from_sol_stream(runner, journal, monkeypatch):
    day = "20990113"
    _write_segment(journal, day, "090000_300", audio_jsonl=True)
    monkeypatch.setenv("SOL_DAY", day)
    monkeypatch.setenv("SOL_SEGMENT", "090000_300")
    monkeypatch.setenv("SOL_STREAM", "default")

    result = runner.invoke(app, ["read"])

    assert result.exit_code == 0
    assert "## 2099-01-13 09:00:00 - 09:05:00" in result.stdout


def test_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.transcripts.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["scan", "20240101"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""


@pytest.mark.parametrize("command", ["scan", "segments", "read"])
def test_malformed_day_prints_owner_voice_error(runner, command):
    result = runner.invoke(app, [command, "notaday"])

    assert result.exit_code == 1
    assert result.stderr == "I couldn't use that day.\n"
    assert result.stdout == ""


def test_malformed_month_prints_owner_voice_error(runner):
    result = runner.invoke(app, ["stats", "bad"])

    assert result.exit_code == 1
    assert result.stderr == "I couldn't use that month.\n"
    assert result.stdout == ""
