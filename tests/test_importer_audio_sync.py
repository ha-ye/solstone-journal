# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from solstone.think.importers.shared import (
    find_manifest_by_hash,
    hash_source,
    write_manifest,
)
from solstone.think.importers.sync import load_sync_state, save_sync_state


def _write_audio(root: Path, rel_path: str = "clip.mp3", content: bytes = b"audio"):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _patch_audio_tools(monkeypatch, duration: float | None = 60.0):
    monkeypatch.setattr(
        "solstone.think.importers.audio.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "solstone.think.importers.audio._get_audio_duration",
        lambda _path: duration,
    )


def _write_audio_manifest(journal_root: Path, audio_file: Path, import_id: str):
    return write_manifest(
        journal_root,
        import_id=import_id,
        source_type="audio",
        source_hash=hash_source(audio_file),
        entry_count=1,
        files_created=[str(audio_file)],
        days_affected=["20260303"],
    )


def test_import_one_audio_manifest_is_findable_by_hash(tmp_path, monkeypatch):
    mod = importlib.import_module("solstone.think.importers.cli")

    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake audio")
    callosum = MagicMock()

    def fake_prepare_audio_segments(media_path, day_dir, base_dt, import_id, stream):
        seg_dir = Path(day_dir) / stream / "120000_300"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "imported_audio.mp3").write_bytes(b"sliced audio")
        return [("120000_300", seg_dir, ["imported_audio.mp3"])]

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(mod, "CallosumConnection", lambda **kwargs: callosum)
    monkeypatch.setattr(mod, "get_rev", lambda: "test-rev")
    monkeypatch.setattr(mod, "_status_emitter", lambda: None)
    monkeypatch.setattr(mod, "prepare_audio_segments", fake_prepare_audio_segments)
    monkeypatch.setattr(
        mod,
        "update_stream",
        lambda stream, day, seg, **kwargs: {
            "prev_day": None,
            "prev_segment": None,
            "seq": 1,
        },
    )
    monkeypatch.setattr(mod, "write_segment_stream", lambda *args, **kwargs: None)

    result = mod.import_one(
        audio_file,
        timestamp="20260303_120000",
        source="audio",
        wait_for_processing=False,
    )

    journal_root = tmp_path
    source_hash = hash_source(audio_file)
    manifest = find_manifest_by_hash(journal_root, source_hash)

    assert result is not None
    assert manifest is not None
    assert manifest["source_hash"] == source_hash


def test_audio_sync_protocol_conformance():
    from solstone.think.importers.audio import AudioFolderBackend
    from solstone.think.importers.sync import SyncableBackend

    assert isinstance(AudioFolderBackend(), SyncableBackend)


def test_audio_sync_registry_discovery():
    from solstone.think.importers.sync import get_syncable_backends

    backends = get_syncable_backends()

    assert "audio" in [backend.name for backend in backends]


def test_audio_sync_dry_run_catalogs_available_files(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)
    source_dir = tmp_path / "source"
    audio_file = _write_audio(source_dir, "nested/clip.mp3", b"available audio")

    result = AudioFolderBackend().sync(tmp_path, source_path=source_dir, dry_run=True)

    assert result == {
        "total": 1,
        "imported": 0,
        "available": 1,
        "skipped": 0,
        "downloaded": 0,
        "errors": [],
    }
    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    assert state["backend"] == "audio"
    assert state["source_path"] == str(source_dir.resolve())
    entry = state["files"]["nested/clip.mp3"]
    assert entry["filename"] == "clip.mp3"
    assert entry["filesize"] == len(b"available audio")
    assert entry["hash"] == hash_source(audio_file)
    assert entry["status"] == "available"


def test_audio_sync_manifested_file_reports_imported(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    monkeypatch.setattr(
        "solstone.think.importers.audio.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    duration = MagicMock(return_value=60.0)
    monkeypatch.setattr("solstone.think.importers.audio._get_audio_duration", duration)
    source_dir = tmp_path / "source"
    audio_file = _write_audio(source_dir, "clip.mp3")
    _write_audio_manifest(tmp_path, audio_file, "20260303_120000")

    result = AudioFolderBackend().sync(tmp_path, source_path=source_dir, dry_run=True)

    assert result["imported"] == 1
    assert result["available"] == 0
    duration.assert_not_called()
    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    assert state["files"]["clip.mp3"]["status"] == "imported"


def test_audio_sync_second_sync_after_save_has_zero_available(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)
    source_dir = tmp_path / "source"
    audio_file = _write_audio(source_dir, "clip.mp3")

    def fake_import_one(path, **kwargs):
        _write_audio_manifest(tmp_path, Path(path), "20260303_120000")
        return {"segments": ["120000_300"]}

    monkeypatch.setattr("solstone.think.importers.cli.import_one", fake_import_one)

    backend = AudioFolderBackend()
    first = backend.sync(tmp_path, source_path=source_dir, dry_run=False)
    second = backend.sync(tmp_path, source_path=source_dir, dry_run=True)

    assert first["downloaded"] == 1
    assert audio_file.exists()
    assert second["imported"] == 1
    assert second["available"] == 0
    assert second["downloaded"] == 0


def test_audio_sync_skips_too_short_files(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch, duration=29.0)
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "short.mp3")

    result = AudioFolderBackend().sync(tmp_path, source_path=source_dir, dry_run=True)

    assert result["skipped"] == 1
    assert result["available"] == 0
    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    entry = state["files"]["short.mp3"]
    assert entry["status"] == "skipped"
    assert entry["skip_reason"] == "too_short"


@pytest.mark.parametrize("dry_run", [True, False])
def test_audio_sync_probe_failure_prints_error_in_both_modes(
    tmp_path,
    monkeypatch,
    capsys,
    dry_run,
):
    mod = importlib.import_module("solstone.think.importers.cli")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(
        "solstone.think.importers.audio.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "solstone.think.importers.audio._get_audio_duration",
        lambda path: None if "broken" in path else 29.0,
    )
    monkeypatch.setattr(
        "solstone.think.importers.cli.import_one",
        lambda path, **kwargs: {"segments": ["120000_300"]},
    )
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "short.mp3")
    _write_audio(source_dir, "broken.mp3")

    mod._run_sync("audio", dry_run=dry_run, source_path=source_dir)

    output = capsys.readouterr().out
    assert "Errors: 1" in output
    assert "broken.mp3: could not read audio (probe failed)" in output
    assert "too short" in output
    assert "Skipped:             2" not in output
    assert "Everything is up to date." not in output


def test_audio_sync_partitions_available_skipped_and_unreadable(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    monkeypatch.setattr(
        "solstone.think.importers.audio.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    def duration_for(path):
        name = Path(path).name
        if name == "broken.mp3":
            return None
        if name == "short.mp3":
            return 29.0
        return 60.0

    monkeypatch.setattr(
        "solstone.think.importers.audio._get_audio_duration", duration_for
    )
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "long.mp3")
    _write_audio(source_dir, "short.mp3")
    _write_audio(source_dir, "broken.mp3")

    result = AudioFolderBackend().sync(tmp_path, source_path=source_dir, dry_run=True)

    assert result["available"] == 1
    assert result["skipped"] == 1
    assert len(result["errors"]) == 1
    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    assert state["files"]["broken.mp3"]["status"] == "unreadable"


def test_audio_sync_probe_failure_recovery_is_not_sticky(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch, duration=None)
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "clip.mp3")
    backend = AudioFolderBackend()

    backend.sync(tmp_path, source_path=source_dir, dry_run=True)
    first_state = load_sync_state(tmp_path, "audio")
    assert first_state is not None
    assert first_state["files"]["clip.mp3"]["status"] == "unreadable"

    monkeypatch.setattr(
        "solstone.think.importers.audio._get_audio_duration",
        lambda _path: 60.0,
    )
    backend.sync(tmp_path, source_path=source_dir, dry_run=True)

    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    entry = state["files"]["clip.mp3"]
    assert entry["status"] == "available"
    assert "last_error" not in entry
    assert "skip_reason" not in entry


def test_run_sync_skipped_reason_visible_without_verbose(tmp_path, monkeypatch, capsys):
    mod = importlib.import_module("solstone.think.importers.cli")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _patch_audio_tools(monkeypatch, duration=29.0)
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "short.mp3")

    mod._run_sync("audio", dry_run=True, source_path=source_dir)

    output = capsys.readouterr().out
    assert "too short" in output
    skipped_lines = [
        line.rstrip() for line in output.splitlines() if "Skipped:" in line
    ]
    assert "  Skipped:             1" not in skipped_lines


def test_run_sync_verbose_prints_file_dump(tmp_path, monkeypatch, capsys):
    mod = importlib.import_module("solstone.think.importers.cli")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(
        "solstone.think.importers.audio.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "solstone.think.importers.audio._get_audio_duration",
        lambda path: 29.0 if Path(path).name == "short.mp3" else 60.0,
    )
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "long.mp3")
    _write_audio(source_dir, "short.mp3")

    mod._run_sync("audio", dry_run=True, verbose=True, source_path=source_dir)
    verbose_output = capsys.readouterr().out
    assert "Files:" in verbose_output
    assert "available" in verbose_output
    assert "long.mp3" in verbose_output
    assert "skipped" in verbose_output
    assert "short.mp3" in verbose_output

    mod._run_sync("audio", dry_run=True, verbose=False, source_path=source_dir)
    plain_output = capsys.readouterr().out
    assert "Files:" not in plain_output


def test_importer_main_sync_verbose_wiring(tmp_path, monkeypatch, capsys):
    mod = importlib.import_module("solstone.think.importers.cli")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(mod, "require_solstone", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sol", "--sync", "audio", "--path", str(tmp_path / "source"), "-v"],
    )
    _patch_audio_tools(monkeypatch)
    _write_audio(tmp_path / "source", "clip.mp3")

    mod.main()

    output = capsys.readouterr().out
    assert "Files:" in output
    assert "clip.mp3" in output


def test_run_sync_verbose_handles_obsidian_shaped_state(
    tmp_path,
    monkeypatch,
    capsys,
):
    mod = importlib.import_module("solstone.think.importers.cli")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    save_sync_state(
        tmp_path,
        "audio",
        {
            "backend": "audio",
            "files": {
                "clip.md": {
                    "filename": "clip.md",
                    "status": "available",
                },
                "done.md": {
                    "filename": "done.md",
                    "status": "imported",
                },
            },
        },
    )

    class FakeAudioBackend:
        name = "audio"

        def sync(self, journal_root, *, dry_run=True, source_path=None):
            return {
                "total": 2,
                "imported": 1,
                "available": 1,
                "skipped": 0,
                "downloaded": 0,
                "errors": [],
            }

    monkeypatch.setattr(
        "solstone.think.importers.sync.get_syncable_backends",
        lambda: [FakeAudioBackend()],
    )

    mod._run_sync("audio", dry_run=True, verbose=True, source_path=tmp_path / "source")

    output = capsys.readouterr().out
    assert "Files:" in output
    assert "clip.md" in output
    assert "done.md" in output


def test_audio_sync_auto_passthrough_from_main(tmp_path, monkeypatch, capsys):
    mod = importlib.import_module("solstone.think.importers.cli")
    monkeypatch.setattr(mod, "require_solstone", lambda: None)
    _patch_audio_tools(monkeypatch)
    import_one = MagicMock(return_value={"segments": ["120000_300"]})
    monkeypatch.setattr("solstone.think.importers.cli.import_one", import_one)

    guidance_journal = tmp_path / "guidance-journal"
    guidance_source = tmp_path / "guidance-source"
    _write_audio(guidance_source, "clip.mp3")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(guidance_journal))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sol",
            "--sync",
            "audio",
            "--path",
            str(guidance_source),
            "--save",
            "--auto",
            "guidance text",
        ],
    )
    mod.main()
    assert import_one.call_args.kwargs["auto"] == "guidance text"

    default_journal = tmp_path / "default-journal"
    default_source = tmp_path / "default-source"
    _write_audio(default_source, "clip.mp3", b"default audio")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(default_journal))
    monkeypatch.setattr(
        sys,
        "argv",
        ["sol", "--sync", "audio", "--path", str(default_source), "--save"],
    )
    import_one.reset_mock()
    capsys.readouterr()
    mod.main()
    assert import_one.call_args.kwargs["auto"] is True

    dry_run_journal = tmp_path / "dry-run-journal"
    dry_run_source = tmp_path / "dry-run-source"
    _write_audio(dry_run_source, "clip.mp3", b"dry-run audio")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(dry_run_journal))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sol",
            "--sync",
            "audio",
            "--path",
            str(dry_run_source),
            "--auto",
            "catalog guidance",
        ],
    )
    import_one.reset_mock()
    capsys.readouterr()
    mod.main()
    import_one.assert_not_called()


def test_audio_sync_save_imports_new_files_and_checkpoints(tmp_path, monkeypatch):
    import solstone.think.importers.audio as audio_mod
    from solstone.think.importers.audio import AudioFolderBackend
    from solstone.think.importers.sync import save_sync_state as real_save_sync_state

    _patch_audio_tools(monkeypatch)
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "one.mp3")
    _write_audio(source_dir, "two.mp3")
    monkeypatch.setattr(
        "solstone.think.importers.cli.import_one",
        lambda path, **kwargs: {"segments": ["120000_300"]},
    )
    save_calls = []

    def spy_save_sync_state(journal_root, backend, state):
        save_calls.append(backend)
        real_save_sync_state(journal_root, backend, state)

    monkeypatch.setattr(audio_mod, "save_sync_state", spy_save_sync_state)

    result = AudioFolderBackend().sync(tmp_path, source_path=source_dir, dry_run=False)

    assert result["downloaded"] == 2
    assert result["imported"] == 2
    assert len(save_calls) == 3


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_downloaded", "expects_error"),
    [
        ("success", "imported", 1, False),
        ("skipped", "available", 0, True),
        ("raised", "available", 0, True),
        ("none", "available", 0, True),
    ],
)
def test_audio_sync_import_one_return_shapes(
    tmp_path,
    monkeypatch,
    mode,
    expected_status,
    expected_downloaded,
    expects_error,
):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "clip.mp3")

    def fake_import_one(path, **kwargs):
        if mode == "success":
            return {"segments": ["120000_300"]}
        if mode == "skipped":
            return {"skipped": True, "reason": "already_imported"}
        if mode == "raised":
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr("solstone.think.importers.cli.import_one", fake_import_one)

    result = AudioFolderBackend().sync(tmp_path, source_path=source_dir, dry_run=False)

    assert result["downloaded"] == expected_downloaded
    assert bool(result["errors"]) is expects_error
    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    entry = state["files"]["clip.mp3"]
    assert entry["status"] == expected_status
    if expects_error:
        assert entry["last_error"]
    else:
        assert "last_error" not in entry


def test_audio_sync_removed_file_marks_removed(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)
    source_dir = tmp_path / "source"
    _write_audio(source_dir, "keep.mp3")
    removed = _write_audio(source_dir, "removed.mp3")
    backend = AudioFolderBackend()
    backend.sync(tmp_path, source_path=source_dir, dry_run=True)

    removed.unlink()
    result = backend.sync(tmp_path, source_path=source_dir, dry_run=True)

    assert result["total"] == 2
    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    assert state["files"]["keep.mp3"]["status"] == "available"
    assert state["files"]["removed.mp3"]["status"] == "removed"


def test_audio_sync_removed_skipped_file_marks_removed(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    monkeypatch.setattr(
        "solstone.think.importers.audio.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "solstone.think.importers.audio._get_audio_duration",
        lambda path: 29.0 if Path(path).name == "short.mp3" else 60.0,
    )
    source_dir = tmp_path / "source"
    other_source_dir = tmp_path / "other-source"
    _write_audio(source_dir, "short.mp3")
    _write_audio(other_source_dir, "long.mp3")
    backend = AudioFolderBackend()
    first = backend.sync(tmp_path, source_path=source_dir, dry_run=True)
    assert first["skipped"] == 1

    second = backend.sync(tmp_path, source_path=other_source_dir, dry_run=True)

    assert second["skipped"] == 0
    assert second["available"] == 1
    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    assert state["files"]["short.mp3"]["status"] == "removed"
    assert state["files"]["long.mp3"]["status"] == "available"


def test_audio_sync_force_save_does_not_reimport_manifested_file(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)
    source_dir = tmp_path / "source"
    audio_file = _write_audio(source_dir, "clip.mp3")
    _write_audio_manifest(tmp_path, audio_file, "20260303_120000")
    import_one = MagicMock(return_value={"segments": ["120000_300"]})
    monkeypatch.setattr("solstone.think.importers.cli.import_one", import_one)

    result = AudioFolderBackend().sync(
        tmp_path,
        source_path=source_dir,
        dry_run=False,
        force=True,
    )

    assert result["imported"] == 1
    assert result["available"] == 0
    assert result["downloaded"] == 0
    import_one.assert_not_called()


def test_audio_sync_missing_path_raises_value_error(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)

    with pytest.raises(ValueError, match="--path"):
        AudioFolderBackend().sync(tmp_path)


def test_audio_sync_non_directory_path_raises_value_error(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)
    source_path = tmp_path / "not-a-dir.mp3"
    source_path.write_bytes(b"audio")

    with pytest.raises(ValueError, match="not a directory"):
        AudioFolderBackend().sync(tmp_path, source_path=source_path)


def test_audio_sync_no_audio_files_raises_value_error(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "notes.txt").write_text("not audio", encoding="utf-8")

    with pytest.raises(ValueError, match="no audio"):
        AudioFolderBackend().sync(tmp_path, source_path=source_dir)


@pytest.mark.parametrize("missing_tool", ["ffmpeg", "ffprobe"])
def test_audio_sync_missing_tool_fails_fast(tmp_path, monkeypatch, missing_tool):
    from solstone.think.importers.audio import AudioFolderBackend

    source_dir = tmp_path / "source"
    source_dir.mkdir()

    def fake_which(name):
        if name == missing_tool:
            return None
        return f"/usr/bin/{name}"

    monkeypatch.setattr("solstone.think.importers.audio.shutil.which", fake_which)

    with pytest.raises(ValueError, match=missing_tool):
        AudioFolderBackend().sync(tmp_path, source_path=source_dir)


def test_audio_sync_excludes_journal_imports_subtree(tmp_path, monkeypatch):
    from solstone.think.importers.audio import AudioFolderBackend

    _patch_audio_tools(monkeypatch)
    _write_audio(tmp_path, "root.mp3")
    _write_audio(tmp_path, "imports/20260303_120000/imported_audio.mp3")

    result = AudioFolderBackend().sync(tmp_path, source_path=tmp_path, dry_run=True)

    assert result["total"] == 1
    state = load_sync_state(tmp_path, "audio")
    assert state is not None
    assert set(state["files"]) == {"root.mp3"}


def test_run_sync_dry_run_hint_includes_path_for_audio(tmp_path, monkeypatch, capsys):
    mod = importlib.import_module("solstone.think.importers.cli")
    source_dir = tmp_path / "audio source"
    source_dir.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    class FakeAudioBackend:
        name = "audio"

        def sync(self, journal_root, *, dry_run=True, source_path=None):
            save_sync_state(
                journal_root,
                "audio",
                {
                    "backend": "audio",
                    "source_path": str(source_path),
                    "files": {
                        "clip.mp3": {
                            "filename": "clip.mp3",
                            "filesize": 4,
                            "status": "available",
                        }
                    },
                },
            )
            return {
                "total": 1,
                "imported": 0,
                "available": 1,
                "skipped": 0,
                "downloaded": 0,
                "errors": [],
            }

    monkeypatch.setattr(
        "solstone.think.importers.sync.get_syncable_backends",
        lambda: [FakeAudioBackend()],
    )

    mod._run_sync("audio", dry_run=True, source_path=source_dir)

    output = capsys.readouterr().out
    assert f"sol import --sync audio --save --path {source_dir}" in output


def test_run_sync_dry_run_hint_omits_path_for_pathless_backend(
    tmp_path,
    monkeypatch,
    capsys,
):
    mod = importlib.import_module("solstone.think.importers.cli")
    source_dir = tmp_path / "audio"
    source_dir.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    class FakeAudioBackend:
        name = "audio"

        def sync(self, journal_root, *, dry_run=True):
            save_sync_state(
                journal_root,
                "audio",
                {
                    "backend": "audio",
                    "files": {
                        "clip.mp3": {
                            "filename": "clip.mp3",
                            "filesize": 4,
                            "status": "available",
                        }
                    },
                },
            )
            return {
                "total": 1,
                "imported": 0,
                "available": 1,
                "skipped": 0,
                "downloaded": 0,
                "errors": [],
            }

    monkeypatch.setattr(
        "solstone.think.importers.sync.get_syncable_backends",
        lambda: [FakeAudioBackend()],
    )

    mod._run_sync("audio", dry_run=True, source_path=source_dir)

    output = capsys.readouterr().out
    assert "sol import --sync audio --save" in output
    assert "--path" not in output


def test_run_sync_save_errors_suppress_up_to_date_message(
    tmp_path,
    monkeypatch,
    capsys,
):
    mod = importlib.import_module("solstone.think.importers.cli")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    class FakeAudioBackend:
        name = "audio"

        def sync(self, journal_root, *, dry_run=True):
            return {
                "total": 1,
                "imported": 0,
                "available": 0,
                "skipped": 0,
                "downloaded": 0,
                "errors": ["clip.mp3: boom"],
            }

    monkeypatch.setattr(
        "solstone.think.importers.sync.get_syncable_backends",
        lambda: [FakeAudioBackend()],
    )

    mod._run_sync("audio", dry_run=False)

    output = capsys.readouterr().out
    assert "Errors: 1" in output
    assert "Everything is up to date." not in output
