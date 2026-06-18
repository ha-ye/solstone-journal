# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unit tests for journal transcribe CLI (M3, M8, M9)."""

import argparse
import importlib
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _args(backend: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(backend=backend, cpu=False, model=None, redo=False)


def test_main_accepts_journal_relative_path(tmp_path, monkeypatch):
    """main() resolves audio_path relative to journal when absolute path fails."""
    seg_dir = tmp_path / "chronicle" / "20260201" / "default" / "090000_300"
    seg_dir.mkdir(parents=True)
    audio_file = seg_dir / "audio.wav"
    audio_file.touch()

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv", ["sol transcribe", "20260201/default/090000_300/audio.wav"]
    )

    mock_load = MagicMock(return_value=MagicMock())
    mock_vad_result = MagicMock()
    mock_vad_result.has_speech = False
    mock_vad_result.speech_duration = 0.0
    mock_vad_result.duration = 5.0
    mock_vad = MagicMock(return_value=mock_vad_result)

    with (
        patch("solstone.observe.transcribe.main.load_audio", mock_load),
        patch("solstone.observe.vad.run_vad", mock_vad),
        patch("solstone.observe.transcribe.main.callosum_send"),
        patch(
            "solstone.observe.transcribe.main.get_segment_key",
            return_value="090000_300",
        ),
        patch("solstone.observe.transcribe.main._build_base_event", return_value={}),
        patch("solstone.think.entities.load_recent_entity_names", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.read_available_bytes",
            return_value=8 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.stt_local_floor_bytes",
            return_value=4 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.local_stt_backend",
            return_value="parakeet",
        ),
    ):
        from solstone.observe.transcribe.main import main

        main()

    mock_load.assert_called_once()


def test_main_errors_on_nonexistent_absolute_path(tmp_path, monkeypatch, capsys):
    """main() errors clearly when path doesn't exist as absolute or journal-relative."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["sol transcribe", "/nonexistent/path/audio.wav"])

    from solstone.observe.transcribe.main import main

    with (
        patch(
            "solstone.observe.transcribe.main.read_available_bytes",
            return_value=8 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.stt_local_floor_bytes",
            return_value=4 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.local_stt_backend",
            return_value="parakeet",
        ),
    ):
        with pytest.raises(SystemExit):
            main()

    captured = capsys.readouterr()
    assert "Tried absolute" in captured.err or "not found" in captured.err.lower()


def test_setup_cli_no_message_on_project_journal(tmp_path, monkeypatch, capsys):
    """setup_cli() prints no informational message — journal path is always deterministic."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    with (
        patch("solstone.think.utils.get_journal", return_value=str(tmp_path)),
        patch("solstone.think.utils.get_config", return_value={}),
    ):
        from solstone.think.utils import setup_cli

        parser = argparse.ArgumentParser()
        monkeypatch.setattr("sys.argv", ["test"])
        setup_cli(parser)

    captured = capsys.readouterr()
    assert "docs/INSTALL.md" not in captured.err


def _make_batch_journal(tmp_path: Path) -> Path:
    """Create a minimal temp journal with three segments for batch testing."""
    seg1 = tmp_path / "chronicle" / "20260101" / "default" / "090000_300"
    seg1.mkdir(parents=True)
    (seg1 / "audio.flac").touch()

    seg2 = tmp_path / "chronicle" / "20260101" / "default" / "140000_300"
    seg2.mkdir(parents=True)
    (seg2 / "audio.flac").touch()
    (seg2 / "audio.jsonl").touch()

    seg3 = tmp_path / "chronicle" / "20260101" / "default" / "180000_300"
    seg3.mkdir(parents=True)
    (seg3 / "screen.png").touch()

    return tmp_path


def test_all_batch_processes_unprocessed_skips_transcribed(
    tmp_path, monkeypatch, capsys
):
    """--all processes unprocessed audio, skips already-transcribed, ignores non-audio."""
    journal = _make_batch_journal(tmp_path)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr("sys.argv", ["sol transcribe", "--all"])

    mock_process_one = MagicMock()

    with (
        patch("solstone.observe.transcribe.main._process_one", mock_process_one),
        patch("solstone.think.entities.load_recent_entity_names", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.read_available_bytes",
            return_value=8 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.stt_local_floor_bytes",
            return_value=4 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.local_stt_backend",
            return_value="parakeet",
        ),
    ):
        from solstone.observe.transcribe.main import main

        main()

    assert mock_process_one.call_count == 1
    called_path = mock_process_one.call_args[0][0]
    assert called_path.name == "audio.flac"
    assert "090000_300" in str(called_path)

    captured = capsys.readouterr()
    assert "1 processed" in captured.out
    assert "1 skipped" in captured.out


def test_all_redo_reprocesses_transcribed(tmp_path, monkeypatch):
    """--all --redo reprocesses even segments that already have .jsonl."""
    journal = _make_batch_journal(tmp_path)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr("sys.argv", ["sol transcribe", "--all", "--redo"])

    mock_process_one = MagicMock()

    with (
        patch("solstone.observe.transcribe.main._process_one", mock_process_one),
        patch("solstone.think.entities.load_recent_entity_names", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.read_available_bytes",
            return_value=8 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.stt_local_floor_bytes",
            return_value=4 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.local_stt_backend",
            return_value="parakeet",
        ),
    ):
        from solstone.observe.transcribe.main import main

        main()

    assert mock_process_one.call_count == 2


def test_all_and_audio_path_mutually_exclusive(tmp_path, monkeypatch):
    """Providing both --all and audio_path produces a clear error."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["sol transcribe", "--all", "some/audio.wav"])

    with patch("solstone.think.entities.load_recent_entity_names", return_value=[]):
        from solstone.observe.transcribe.main import main

        with (
            patch(
                "solstone.observe.transcribe.main.read_available_bytes",
                return_value=8 * 1024**3,
            ),
            patch(
                "solstone.observe.transcribe.main.stt_local_floor_bytes",
                return_value=4 * 1024**3,
            ),
            patch(
                "solstone.observe.transcribe.main.local_stt_backend",
                return_value="parakeet",
            ),
        ):
            with pytest.raises(SystemExit):
                main()


def test_neither_all_nor_audio_path_errors(tmp_path, monkeypatch):
    """Providing neither --all nor audio_path produces a clear error."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["sol transcribe"])

    with patch("solstone.think.entities.load_recent_entity_names", return_value=[]):
        from solstone.observe.transcribe.main import main

        with pytest.raises(SystemExit):
            main()


def test_resolve_default_backend_auto_switches_to_gemini(monkeypatch):
    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(transcribe_main, "read_available_bytes", lambda: 2 * 1024**3)
    monkeypatch.setattr(transcribe_main, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(transcribe_main, "local_stt_backend", lambda: "parakeet")

    assert transcribe_main.resolve_default_backend(_args(), {}) == "gemini"


def test_resolve_default_backend_surfaces_when_no_viable_backend(monkeypatch):
    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")

    calls = 0

    def fake_read_available_bytes():
        nonlocal calls
        calls += 1
        return 2 * 1024**3

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(
        transcribe_main, "read_available_bytes", fake_read_available_bytes
    )
    monkeypatch.setattr(transcribe_main, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(transcribe_main, "local_stt_backend", lambda: "parakeet")

    with pytest.raises(SystemExit) as exc_info:
        transcribe_main.resolve_default_backend(_args(), {})

    assert exc_info.value.code == 1
    assert calls == 1


def test_resolve_default_backend_warns_but_honors_explicit_local(monkeypatch, caplog):
    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")

    monkeypatch.setattr(transcribe_main, "read_available_bytes", lambda: 2 * 1024**3)
    monkeypatch.setattr(transcribe_main, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(transcribe_main, "local_stt_backend", lambda: "parakeet")

    with caplog.at_level(logging.WARNING):
        backend = transcribe_main.resolve_default_backend(_args(backend="parakeet"), {})

    assert backend == "parakeet"
    assert "Free memory is below 4 GB" in caplog.text


def test_resolve_default_backend_honors_config_backend(monkeypatch):
    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(transcribe_main, "read_available_bytes", lambda: 2 * 1024**3)
    monkeypatch.setattr(transcribe_main, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(transcribe_main, "local_stt_backend", lambda: "parakeet")

    assert (
        transcribe_main.resolve_default_backend(_args(), {"backend": "gemini"})
        == "gemini"
    )


def test_resolve_default_backend_uses_parakeet_when_memory_fits(monkeypatch):
    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(transcribe_main, "read_available_bytes", lambda: 5 * 1024**3)
    monkeypatch.setattr(transcribe_main, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(transcribe_main, "local_stt_backend", lambda: "parakeet")

    assert transcribe_main.resolve_default_backend(_args(), {}) == "parakeet"


def test_resolve_default_backend_uses_whisper_when_memory_fits(monkeypatch):
    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(transcribe_main, "read_available_bytes", lambda: 5 * 1024**3)
    monkeypatch.setattr(transcribe_main, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(transcribe_main, "local_stt_backend", lambda: "whisper")

    assert transcribe_main.resolve_default_backend(_args(), {}) == "whisper"


def test_all_batch_reads_memory_once_and_reuses_default_backend(tmp_path, monkeypatch):
    journal = _make_batch_journal(tmp_path)
    config_dir = journal / "config"
    config_dir.mkdir()
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "identity": {"name": "Test"},
                "env": {"GOOGLE_API_KEY": "test-key"},
            }
        )
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr("sys.argv", ["sol transcribe", "--all", "--redo"])
    calls = 0

    def fake_read_available_bytes():
        nonlocal calls
        calls += 1
        return 2 * 1024**3

    mock_process_one = MagicMock()

    with (
        patch("solstone.observe.transcribe.main._process_one", mock_process_one),
        patch("solstone.think.entities.load_recent_entity_names", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.read_available_bytes",
            fake_read_available_bytes,
        ),
        patch(
            "solstone.observe.transcribe.main.stt_local_floor_bytes",
            return_value=4 * 1024**3,
        ),
        patch(
            "solstone.observe.transcribe.main.local_stt_backend",
            return_value="parakeet",
        ),
    ):
        from solstone.observe.transcribe.main import main

        main()

    assert calls == 1
    assert mock_process_one.call_count == 2
    assert {call.args[3] for call in mock_process_one.call_args_list} == {"gemini"}
