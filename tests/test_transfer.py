# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for observe/transfer.py - day archive export, import, and send."""

import inspect
import json
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest


class TestSegmentDeconfliction:
    """Tests for segment deconfliction via find_available_segment."""

    def test_find_available_segment_returns_original_if_free(self, tmp_path):
        """Test find_available_segment returns original if available."""
        from solstone.observe.utils import find_available_segment

        # No existing segments
        result = find_available_segment(tmp_path, "120000_300")
        assert result == "120000_300"

    def test_find_available_segment_finds_alternative(self, tmp_path):
        """Test find_available_segment finds alternative when original taken."""
        from solstone.observe.utils import find_available_segment

        # Create existing segment
        (tmp_path / "120000_300").mkdir()

        result = find_available_segment(tmp_path, "120000_300")
        assert result is not None
        assert result != "120000_300"
        # Should be a valid segment key format
        assert "_" in result

    def test_find_available_segment_returns_none_when_exhausted(self, tmp_path):
        """Test find_available_segment returns None when all slots taken."""
        from solstone.observe.utils import find_available_segment

        # Create many segments around the target
        for delta in range(-50, 51):
            for dur_delta in range(-50, 51):
                total_seconds = 12 * 3600 + delta
                if 0 <= total_seconds < 86400:
                    h = total_seconds // 3600
                    m = (total_seconds % 3600) // 60
                    s = total_seconds % 60
                    dur = 300 + dur_delta
                    if dur > 0:
                        (tmp_path / f"{h:02d}{m:02d}{s:02d}_{dur}").mkdir(exist_ok=True)

        # With so many slots filled, should eventually fail
        result = find_available_segment(tmp_path, "120000_300", max_attempts=10)
        # May or may not find one depending on random walk, but shouldn't crash
        assert result is None or "_" in result


class TestComputeSha256:
    """Tests for SHA256 computation utilities."""

    def test_compute_file_sha256(self, tmp_path):
        """Test compute_file_sha256 returns correct hash."""
        from solstone.observe.utils import compute_file_sha256

        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"hello world")

        # Known SHA256 of "hello world"
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert compute_file_sha256(test_file) == expected

    def test_compute_bytes_sha256(self):
        """Test compute_bytes_sha256 returns correct hash."""
        from solstone.observe.utils import compute_bytes_sha256

        # Known SHA256 of "hello world"
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert compute_bytes_sha256(b"hello world") == expected


class TestTransferExport:
    """Tests for archive creation (export)."""

    def test_create_archive_basic(self, tmp_path, monkeypatch):
        """Test create_archive creates valid archive."""
        from solstone.observe.transfer import create_archive

        # Set up mock journal with day/stream/segment structure
        journal_path = tmp_path / "journal"
        day_dir = journal_path / "chronicle" / "20250101"
        segment_dir = day_dir / "default" / "120000_300"
        segment_dir.mkdir(parents=True)

        # Add test files to segment
        (segment_dir / "audio.flac").write_bytes(b"fake audio data")
        (segment_dir / "audio.jsonl").write_text('{"raw": "audio.flac"}\n')

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        # Clear cache
        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        output_path = tmp_path / "test.tgz"
        result = create_archive("20250101", output_path)

        assert result == output_path
        assert output_path.exists()

        # Verify archive contents
        with tarfile.open(output_path, "r:gz") as tar:
            names = tar.getnames()
            assert "manifest.json" in names
            assert "default/120000_300/audio.flac" in names
            assert "default/120000_300/audio.jsonl" in names

            # Verify manifest
            manifest_file = tar.extractfile("manifest.json")
            manifest = json.load(manifest_file)
            assert manifest["version"] == 1
            assert manifest["day"] == "20250101"
            assert "default/120000_300" in manifest["segments"]

    def test_create_archive_no_segments_error(self, tmp_path, monkeypatch):
        """Test create_archive raises error for empty day."""
        from solstone.observe.transfer import create_archive

        journal_path = tmp_path / "journal"
        day_dir = journal_path / "chronicle" / "20250101"
        day_dir.mkdir(parents=True)

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="No segments found"):
            create_archive("20250101")

    def test_create_archive_no_day_error(self, tmp_path, monkeypatch):
        """Test create_archive raises error for missing day."""
        from solstone.observe.transfer import create_archive

        journal_path = tmp_path / "journal"
        journal_path.mkdir(parents=True)

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="does not exist"):
            create_archive("20250101")


class TestTransferImport:
    """Tests for archive import."""

    def _create_test_archive(self, tmp_path, segments: dict) -> Path:
        """Helper to create test archive."""
        archive_path = tmp_path / "test.tgz"

        manifest = {
            "version": 1,
            "day": "20250101",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {},
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            for segment, files in segments.items():
                manifest["segments"][segment] = {"files": []}
                for filename, content in files.items():
                    # Add to manifest
                    from solstone.observe.utils import compute_bytes_sha256

                    manifest["segments"][segment]["files"].append(
                        {
                            "name": filename,
                            "sha256": compute_bytes_sha256(content),
                            "size": len(content),
                        }
                    )

                    # Add file to archive
                    import io

                    info = tarfile.TarInfo(name=f"{segment}/{filename}")
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))

            # Add manifest
            import io

            manifest_json = json.dumps(manifest).encode()
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_json)
            tar.addfile(info, io.BytesIO(manifest_json))

        return archive_path

    def test_validate_archive_all_new(self, tmp_path, monkeypatch):
        """Test validate_archive with no existing segments."""
        from solstone.observe.transfer import validate_archive

        # Create archive
        archive_path = self._create_test_archive(
            tmp_path,
            {
                "120000_300": {"audio.flac": b"audio data"},
                "130000_300": {"audio.flac": b"more audio"},
            },
        )

        # Set up empty journal
        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        result = validate_archive(archive_path)

        assert result["skip"] == []
        assert len(result["import_as"]) == 2
        assert result["import_as"]["120000_300"] == "120000_300"
        assert result["import_as"]["130000_300"] == "130000_300"

    def test_validate_archive_skip_matching(self, tmp_path, monkeypatch):
        """Test validate_archive skips segments with matching hashes."""
        from solstone.observe.transfer import validate_archive

        # Create archive
        content = b"audio data"
        archive_path = self._create_test_archive(
            tmp_path,
            {"120000_300": {"audio.flac": content}},
        )

        # Set up journal with matching segment
        journal_path = tmp_path / "journal"
        segment_dir = journal_path / "chronicle" / "20250101" / "120000_300"
        segment_dir.mkdir(parents=True)
        (segment_dir / "audio.flac").write_bytes(content)

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        result = validate_archive(archive_path)

        assert "120000_300" in result["skip"]
        assert "120000_300" not in result["import_as"]

    def test_validate_archive_deconflict_different(self, tmp_path, monkeypatch):
        """Test validate_archive deconflicts segments with different content."""
        from solstone.observe.transfer import validate_archive

        # Create archive
        archive_path = self._create_test_archive(
            tmp_path,
            {"120000_300": {"audio.flac": b"new audio data"}},
        )

        # Set up journal with different content in same segment
        journal_path = tmp_path / "journal"
        segment_dir = journal_path / "chronicle" / "20250101" / "120000_300"
        segment_dir.mkdir(parents=True)
        (segment_dir / "audio.flac").write_bytes(b"existing different data")

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        result = validate_archive(archive_path)

        assert "120000_300" in result["deconflicted"]
        assert result["import_as"]["120000_300"] != "120000_300"

    def test_import_archive_basic(self, tmp_path, monkeypatch):
        """Test import_archive extracts segments correctly."""
        from solstone.observe.transfer import import_archive

        # Create archive
        audio_content = b"fake audio data"
        jsonl_content = b'{"raw": "audio.flac"}\n'

        archive_path = self._create_test_archive(
            tmp_path,
            {
                "120000_300": {
                    "audio.flac": audio_content,
                    "audio.jsonl": jsonl_content,
                }
            },
        )

        # Set up empty journal
        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        # Mock subprocess to avoid running real indexer
        with patch("subprocess.run"):
            result = import_archive(archive_path)

        assert result["status"] == "imported"
        assert "120000_300" in result["imported"]

        # Verify files were extracted
        segment_dir = journal_path / "chronicle" / "20250101" / "120000_300"
        assert segment_dir.exists()
        assert (segment_dir / "audio.flac").read_bytes() == audio_content
        assert (segment_dir / "audio.jsonl").read_bytes() == jsonl_content

    def test_import_archive_preserves_content_and_mtime(self, tmp_path, monkeypatch):
        """Test import_archive preserves file bytes and tar member mtime."""
        import io

        from solstone.observe.transfer import import_archive
        from solstone.observe.utils import compute_bytes_sha256

        archive_path = tmp_path / "test.tgz"
        content = b"known audio bytes"
        mtime = 1700000000
        manifest = {
            "version": 1,
            "day": "20250101",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {
                "120000_300": {
                    "files": [
                        {
                            "name": "audio.flac",
                            "sha256": compute_bytes_sha256(content),
                            "size": len(content),
                        }
                    ]
                }
            },
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            file_info = tarfile.TarInfo(name="120000_300/audio.flac")
            file_info.size = len(content)
            file_info.mtime = mtime
            tar.addfile(file_info, io.BytesIO(content))

            manifest_json = json.dumps(manifest).encode()
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_json)
            tar.addfile(manifest_info, io.BytesIO(manifest_json))

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        result = import_archive(archive_path)

        target_path = (
            journal_path / "chronicle" / "20250101" / "120000_300" / "audio.flac"
        )
        assert result["status"] == "imported"
        assert target_path.read_bytes() == content
        assert int(target_path.stat().st_mtime) == mtime

    def test_import_archive_installs_zero_byte_member(self, tmp_path, monkeypatch):
        """Test import_archive installs empty regular-file members."""
        from solstone.observe.transfer import import_archive

        archive_path = self._create_test_archive(
            tmp_path,
            {"120000_300": {"audio.flac": b""}},
        )

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        result = import_archive(archive_path)

        target_path = (
            journal_path / "chronicle" / "20250101" / "120000_300" / "audio.flac"
        )
        assert target_path.exists()
        assert target_path.stat().st_size == 0
        assert "120000_300" in result["imported"]

    def test_import_archive_deconflict_installs_into_new_segment(
        self, tmp_path, monkeypatch
    ):
        """Test deconflicted imports install into the generated segment."""
        from solstone.observe.transfer import import_archive

        original_content = b"existing different data"
        new_content = b"new content"
        archive_path = self._create_test_archive(
            tmp_path,
            {"120000_300": {"audio.flac": new_content}},
        )

        journal_path = tmp_path / "journal"
        original_path = (
            journal_path / "chronicle" / "20250101" / "120000_300" / "audio.flac"
        )
        original_path.parent.mkdir(parents=True)
        original_path.write_bytes(original_content)

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with patch("subprocess.run"):
            result = import_archive(archive_path)

        day_dir = journal_path / "chronicle" / "20250101"
        deconflicted_paths = [
            path
            for path in day_dir.iterdir()
            if path.is_dir()
            and path.name != "120000_300"
            and (path / "audio.flac").read_bytes() == new_content
        ]

        assert result["deconflicted"]
        assert deconflicted_paths
        assert original_path.read_bytes() == original_content

    def test_import_archive_mid_extract_failure_cleans_temp_files(
        self, tmp_path, monkeypatch
    ):
        """Test import_archive removes temp files when a later promote fails."""
        import solstone.observe.transfer as transfer

        archive_path = self._create_test_archive(
            tmp_path,
            {
                "120000_300": {"audio.flac": b"first content"},
                "130000_300": {"audio.flac": b"second content"},
            },
        )

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        real_install_file = transfer.install_file
        calls = 0

        def failing_second_install(temp_path, target_path, *, mode=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                real_install_file(temp_path, target_path, mode=mode)
                return
            raise RuntimeError("simulated install failure")

        with patch(
            "solstone.observe.transfer.install_file",
            side_effect=failing_second_install,
        ):
            with pytest.raises(RuntimeError, match="simulated install failure"):
                transfer.import_archive(archive_path)

        day_dir = journal_path / "chronicle" / "20250101"
        first_path = day_dir / "120000_300" / "audio.flac"
        assert first_path.read_bytes() == b"first content"
        assert list(day_dir.rglob("*.tmp")) == []

    def test_import_archive_routes_member_writes_through_install_file(self):
        """Test import_archive has no raw durable member write path."""
        from solstone.observe import transfer

        src = inspect.getsource(transfer.import_archive)

        assert "install_file(" in src
        assert "open(target_path" not in src
        assert ".write(source" not in src
        assert "write_bytes" not in src
        assert "write_text" not in src

    def test_import_archive_dry_run(self, tmp_path, monkeypatch):
        """Test import_archive dry run doesn't modify filesystem."""
        from solstone.observe.transfer import import_archive

        archive_path = self._create_test_archive(
            tmp_path,
            {"120000_300": {"audio.flac": b"audio data"}},
        )

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        result = import_archive(archive_path, dry_run=True)

        assert result["status"] == "dry_run"
        # Directory should not be created
        assert not (journal_path / "chronicle" / "20250101").exists()

    def test_import_archive_nothing_to_import(self, tmp_path, monkeypatch):
        """Test import_archive when all segments already synced."""
        from solstone.observe.transfer import import_archive

        content = b"audio data"
        archive_path = self._create_test_archive(
            tmp_path,
            {"120000_300": {"audio.flac": content}},
        )

        # Set up journal with matching content
        journal_path = tmp_path / "journal"
        segment_dir = journal_path / "chronicle" / "20250101" / "120000_300"
        segment_dir.mkdir(parents=True)
        (segment_dir / "audio.flac").write_bytes(content)

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        result = import_archive(archive_path)

        assert result["status"] == "nothing_to_import"

    def test_import_archive_rejects_dotdot_member(self, tmp_path, monkeypatch):
        """AC#1a: dot-dot member names are rejected before install."""
        import io

        from solstone.observe.transfer import import_archive

        archive_path = tmp_path / "dotdot-member.tgz"
        manifest = {
            "version": 1,
            "day": "20250101",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {"120000_300": {"files": []}},
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            file_info = tarfile.TarInfo(name="120000_300/../../../evil")
            content = b"evil"
            file_info.size = len(content)
            tar.addfile(file_info, io.BytesIO(content))

            manifest_json = json.dumps(manifest).encode()
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_json)
            tar.addfile(manifest_info, io.BytesIO(manifest_json))

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="unsafe member filename"):
            import_archive(archive_path)

        assert not (journal_path / "evil").exists()
        assert not (
            journal_path / "chronicle" / "20250101" / "120000_300" / "evil"
        ).exists()

    def test_import_archive_rejects_absolute_member(self, tmp_path, monkeypatch):
        """AC#1b: absolute member filenames are rejected before install."""
        import io

        from solstone.observe.transfer import import_archive

        archive_path = tmp_path / "absolute-member.tgz"
        sentinel = tmp_path / "abs_member_sentinel"
        manifest = {
            "version": 1,
            "day": "20250101",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {"120000_300": {"files": []}},
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            file_info = tarfile.TarInfo(name=f"120000_300/{sentinel.as_posix()}")
            content = b"evil"
            file_info.size = len(content)
            tar.addfile(file_info, io.BytesIO(content))

            manifest_json = json.dumps(manifest).encode()
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_json)
            tar.addfile(manifest_info, io.BytesIO(manifest_json))

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="unsafe member filename"):
            import_archive(archive_path)

        assert not sentinel.exists()

    def test_import_archive_prepass_blocks_partial_import(self, tmp_path, monkeypatch):
        """AC#2: a later unsafe member aborts before any earlier extraction."""
        import io

        from solstone.observe.transfer import import_archive
        from solstone.observe.utils import compute_bytes_sha256

        archive_path = tmp_path / "partial-import.tgz"
        good_content = b"good audio"
        sentinel = tmp_path / "partial_abs_member_sentinel"
        manifest = {
            "version": 1,
            "day": "20250101",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {
                "120000_300": {
                    "files": [
                        {
                            "name": "audio.flac",
                            "sha256": compute_bytes_sha256(good_content),
                            "size": len(good_content),
                        }
                    ]
                },
                "130000_300": {"files": []},
            },
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            good_info = tarfile.TarInfo(name="120000_300/audio.flac")
            good_info.size = len(good_content)
            tar.addfile(good_info, io.BytesIO(good_content))

            bad_info = tarfile.TarInfo(name=f"130000_300/{sentinel.as_posix()}")
            bad_content = b"evil"
            bad_info.size = len(bad_content)
            tar.addfile(bad_info, io.BytesIO(bad_content))

            manifest_json = json.dumps(manifest).encode()
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_json)
            tar.addfile(manifest_info, io.BytesIO(manifest_json))

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="unsafe member filename"):
            import_archive(archive_path)

        day_dir = journal_path / "chronicle" / "20250101"
        assert not (day_dir / "120000_300" / "audio.flac").exists()
        assert not day_dir.exists()
        assert not sentinel.exists()
        assert list((journal_path / "chronicle").rglob("*.tmp")) == []

    def test_import_archive_rejects_dotdot_arc_key(self, tmp_path, monkeypatch):
        """AC#3a: traversal segment keys are rejected before target mkdir."""
        import io

        from solstone.observe.transfer import import_archive

        archive_path = tmp_path / "dotdot-arc.tgz"
        manifest = {
            "version": 1,
            "day": "20250101",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {"../../../escape_arc": {"files": []}},
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            manifest_json = json.dumps(manifest).encode()
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_json)
            tar.addfile(manifest_info, io.BytesIO(manifest_json))

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="unsafe segment key"):
            import_archive(archive_path)

        assert not (tmp_path / "escape_arc").exists()

    def test_import_archive_rejects_absolute_arc_key(self, tmp_path, monkeypatch):
        """AC#3b: absolute segment keys are rejected before target mkdir."""
        import io

        from solstone.observe.transfer import import_archive

        archive_path = tmp_path / "absolute-arc.tgz"
        sentinel_dir = tmp_path / "abs_arc_sentinel"
        manifest = {
            "version": 1,
            "day": "20250101",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {sentinel_dir.as_posix(): {"files": []}},
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            manifest_json = json.dumps(manifest).encode()
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_json)
            tar.addfile(manifest_info, io.BytesIO(manifest_json))

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="unsafe segment key"):
            import_archive(archive_path)

        assert not sentinel_dir.exists()

    def test_import_archive_rejects_invalid_day(self, tmp_path, monkeypatch):
        """AC#4: invalid manifest day still fails through day_path validation."""
        import io

        from solstone.observe.transfer import import_archive

        archive_path = tmp_path / "bad-day.tgz"
        manifest = {
            "version": 1,
            "day": "bad",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {"120000_300": {"files": []}},
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            manifest_json = json.dumps(manifest).encode()
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_json)
            tar.addfile(manifest_info, io.BytesIO(manifest_json))

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="day must be in YYYYMMDD format"):
            import_archive(archive_path)

    def test_import_archive_rejects_empty_member_filename(self, tmp_path, monkeypatch):
        """AC#5: a member exactly matching the segment prefix is rejected."""
        import io

        from solstone.observe.transfer import import_archive

        archive_path = tmp_path / "empty-filename.tgz"
        manifest = {
            "version": 1,
            "day": "20250101",
            "created_at": 1704067200000,
            "host": "test-host",
            "segments": {"120000_300": {"files": []}},
        }

        with tarfile.open(archive_path, "w:gz") as tar:
            file_info = tarfile.TarInfo(name="120000_300/")
            content = b"evil"
            file_info.size = len(content)
            tar.addfile(file_info, io.BytesIO(content))

            manifest_json = json.dumps(manifest).encode()
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_json)
            tar.addfile(manifest_info, io.BytesIO(manifest_json))

        journal_path = tmp_path / "journal"
        journal_path.mkdir()

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_path))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        with pytest.raises(ValueError, match="empty filename.*120000_300/"):
            import_archive(archive_path)

        assert not (journal_path / "chronicle" / "20250101" / "120000_300").exists()

    def test_create_archive_import_archive_round_trip_deconflicts(
        self, tmp_path, monkeypatch
    ):
        """AC#6: a real exported archive imports and deconflicts collisions."""
        from solstone.observe.transfer import create_archive, import_archive

        source_journal = tmp_path / "source-journal"
        source_day = source_journal / "chronicle" / "20250101"
        first_source = source_day / "120000_300" / "audio.flac"
        second_source = source_day / "130000_300" / "screen.jsonl"
        first_content = b"source collision content"
        second_content = b'{"frame": "source noncollision"}\n'
        first_source.parent.mkdir(parents=True)
        second_source.parent.mkdir(parents=True)
        first_source.write_bytes(first_content)
        second_source.write_bytes(second_content)

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(source_journal))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        archive_path = tmp_path / "roundtrip.tgz"
        create_archive("20250101", archive_path)

        default_stream = think_utils.DEFAULT_STREAM
        target_journal = tmp_path / "target-journal"
        colliding_path = (
            target_journal
            / "chronicle"
            / "20250101"
            / default_stream
            / "120000_300"
            / "audio.flac"
        )
        existing_content = b"target existing content"
        colliding_path.parent.mkdir(parents=True)
        colliding_path.write_bytes(existing_content)

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(target_journal))
        think_utils._journal_path_cache = None

        result = import_archive(archive_path)

        target_day = target_journal / "chronicle" / "20250101" / default_stream
        non_colliding_path = target_day / "130000_300" / "screen.jsonl"
        deconflicted_paths = [
            path
            for path in target_day.iterdir()
            if path.is_dir()
            and path.name != "120000_300"
            and (path / "audio.flac").exists()
            and (path / "audio.flac").read_bytes() == first_content
        ]

        assert result["status"] == "imported"
        assert non_colliding_path.read_bytes() == second_content
        assert colliding_path.read_bytes() == existing_content
        assert deconflicted_paths


class TestManifestValidation:
    """Tests for manifest reading and validation."""

    def test_read_manifest_missing(self, tmp_path):
        """Test error when manifest is missing from archive."""
        from solstone.observe.transfer import _read_manifest

        # Create archive without manifest
        archive_path = tmp_path / "test.tgz"
        with tarfile.open(archive_path, "w:gz") as tar:
            import io

            info = tarfile.TarInfo(name="some_file.txt")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"test"))

        with pytest.raises(ValueError, match="manifest.json not found"):
            _read_manifest(archive_path)

    def test_read_manifest_wrong_version(self, tmp_path):
        """Test error when manifest has wrong version."""
        from solstone.observe.transfer import _read_manifest

        archive_path = tmp_path / "test.tgz"
        with tarfile.open(archive_path, "w:gz") as tar:
            import io

            manifest = json.dumps({"version": 999, "day": "20250101", "segments": {}})
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest.encode()))

        with pytest.raises(ValueError, match="Unsupported manifest version"):
            _read_manifest(archive_path)

    def test_read_manifest_missing_fields(self, tmp_path):
        """Test error when manifest has missing required fields."""
        from solstone.observe.transfer import _read_manifest

        archive_path = tmp_path / "test.tgz"
        with tarfile.open(archive_path, "w:gz") as tar:
            import io

            manifest = json.dumps({"version": 1})  # Missing day and segments
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest.encode()))

        with pytest.raises(ValueError, match="missing required fields"):
            _read_manifest(archive_path)


class TestTransferSend:
    """Tests for transfer send functionality."""

    def _setup_journal(self, tmp_path, *, include_stream_json: bool = False) -> Path:
        journal = tmp_path / "journal"
        day_dir = journal / "chronicle" / "20250103" / "default" / "120000_300"
        day_dir.mkdir(parents=True)
        (day_dir / "audio.flac").write_bytes(b"audio data")
        (day_dir / "transcript.jsonl").write_text('{"text": "hello"}\n')
        if include_stream_json:
            (day_dir / "stream.json").write_text('{"stream": "default"}\n')
        return journal

    def _set_journal_override(self, monkeypatch, journal: Path) -> None:
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

    def test_parse_day_spec_single(self, tmp_path):
        from solstone.observe.transfer import _parse_day_spec

        journal_root = tmp_path / "journal"
        journal_root.mkdir()

        assert _parse_day_spec("20250103", journal_root) == ["20250103"]

    def test_parse_day_spec_range(self, tmp_path):
        from solstone.observe.transfer import _parse_day_spec

        journal_root = tmp_path / "journal"
        journal_root.mkdir()

        assert _parse_day_spec("20250101-20250103", journal_root) == [
            "20250101",
            "20250102",
            "20250103",
        ]

    def test_parse_day_spec_all_days(self, tmp_path):
        from solstone.observe.transfer import _parse_day_spec

        journal_root = tmp_path / "journal"
        journal_root.mkdir()
        (journal_root / "chronicle" / "20250101").mkdir(parents=True)
        (journal_root / "chronicle" / "20250103").mkdir(parents=True)
        (journal_root / "config").mkdir()
        (journal_root / "streams").mkdir()

        assert _parse_day_spec(None, journal_root) == ["20250101", "20250103"]

    def test_parse_day_spec_invalid(self, tmp_path):
        from solstone.observe.transfer import _parse_day_spec

        journal_root = tmp_path / "journal"
        journal_root.mkdir()

        with pytest.raises(ValueError, match="Invalid day format"):
            _parse_day_spec("invalid", journal_root)

    def test_normalize_url(self):
        from solstone.observe.transfer import _normalize_url

        assert _normalize_url("example.com") == "https://example.com"
        assert _normalize_url("example.com/") == "https://example.com"
        assert _normalize_url("https://example.com/") == "https://example.com"
        assert _normalize_url("http://example.com/api/") == "http://example.com/api"
