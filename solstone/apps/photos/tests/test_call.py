# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solstone.think.call import call_app
from solstone.think.entities.journal import clear_journal_entity_cache
from solstone.think.entities.photos import save_entity_photos

runner = CliRunner()


@pytest.fixture(autouse=True)
def skip_supervisor_check(monkeypatch):
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")


def _create_photos_db(
    db_path: Path,
    people: list[tuple[int, str | None]],
    faces: list[tuple[int, int, int]],
) -> None:
    conn = sqlite3.Connection(db_path)
    try:
        conn.execute(
            "CREATE TABLE ZPERSON (Z_PK INTEGER PRIMARY KEY, ZFULLNAME TEXT, ZMERGEDINTO INTEGER)"
        )
        conn.execute(
            "CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZDATECREATED REAL)"
        )
        conn.execute(
            "CREATE TABLE ZDETECTEDFACE (Z_PK INTEGER PRIMARY KEY, ZPERSON INTEGER, ZASSET INTEGER)"
        )
        conn.executemany(
            "INSERT INTO ZPERSON (Z_PK, ZFULLNAME, ZMERGEDINTO) VALUES (?, ?, NULL)",
            people,
        )
        conn.executemany(
            "INSERT INTO ZASSET (Z_PK, ZDATECREATED) VALUES (?, ?)",
            [
                (1, 730000000),
                (2, 730086400),
            ],
        )
        conn.executemany(
            "INSERT INTO ZDETECTEDFACE (Z_PK, ZPERSON, ZASSET) VALUES (?, ?, ?)",
            faces,
        )
        conn.commit()
    finally:
        conn.close()


def _create_journal(journal_dir: Path, entities: list[dict]) -> None:
    for entity in entities:
        entity_dir = journal_dir / "entities" / entity["id"]
        entity_dir.mkdir(parents=True, exist_ok=True)
        (entity_dir / "entity.json").write_text(json.dumps(entity), encoding="utf-8")
    clear_journal_entity_cache()


def _load_photo_entries(journal_dir: Path, slug: str) -> list[dict]:
    path = journal_dir / "entities" / slug / "photos.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _photo_entry_count(journal_dir: Path) -> int:
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in journal_dir.glob("entities/*/photos.jsonl")
    )


class TestPhotosSync:
    def test_non_macos_exits(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

        result = runner.invoke(call_app, ["photos", "sync"])

        assert result.exit_code != 0
        assert "macOS" in result.output

    def test_missing_db_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")

        result = runner.invoke(
            call_app,
            ["photos", "sync", "--library", str(tmp_path / "missing.sqlite")],
        )

        assert result.exit_code != 0
        assert "not found" in result.output

    def test_sync_with_mock_photos_db(self, tmp_path, monkeypatch):
        photos_db = tmp_path / "Photos.sqlite"
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        _create_photos_db(
            photos_db,
            [(1, "Alice Johnson")],
            [(1, 1, 1), (2, 1, 2)],
        )
        _create_journal(
            journal_dir,
            [{"id": "alice_johnson", "name": "Alice Johnson", "type": "Person"}],
        )

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_dir))
        monkeypatch.setattr(sys, "platform", "darwin")

        result = runner.invoke(
            call_app,
            ["photos", "sync", "--library", str(photos_db)],
        )

        assert result.exit_code == 0
        assert "Found 1 named face clusters." in result.output
        assert "Matched 1 to entities." in result.output
        assert "Created 2 photo entries." in result.output

        assert _load_photo_entries(journal_dir, "alice_johnson") == [
            {"day": "20240218", "face_cluster_pk": 1},
            {"day": "20240219", "face_cluster_pk": 1},
        ]

    def test_idempotent_sync(self, tmp_path, monkeypatch):
        photos_db = tmp_path / "Photos.sqlite"
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        _create_photos_db(
            photos_db,
            [(1, "Alice Johnson")],
            [(1, 1, 1), (2, 1, 2)],
        )
        _create_journal(
            journal_dir,
            [{"id": "alice_johnson", "name": "Alice Johnson", "type": "Person"}],
        )

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_dir))
        monkeypatch.setattr(sys, "platform", "darwin")

        first = runner.invoke(call_app, ["photos", "sync", "--library", str(photos_db)])
        assert first.exit_code == 0
        photos_path = journal_dir / "entities" / "alice_johnson" / "photos.jsonl"
        first_content = photos_path.read_text(encoding="utf-8")
        second = runner.invoke(
            call_app,
            ["photos", "sync", "--library", str(photos_db)],
        )

        assert second.exit_code == 0
        assert _photo_entry_count(journal_dir) == 2
        assert photos_path.read_text(encoding="utf-8") == first_content

    def test_zero_faces(self, tmp_path, monkeypatch):
        photos_db = tmp_path / "Photos.sqlite"
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        _create_photos_db(photos_db, [], [])

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_dir))
        monkeypatch.setattr(sys, "platform", "darwin")

        result = runner.invoke(
            call_app,
            ["photos", "sync", "--library", str(photos_db)],
        )

        assert result.exit_code == 0
        assert "Found 0 named face clusters." in result.output
        assert "Created 0 photo entries." in result.output

    def test_zero_matches(self, tmp_path, monkeypatch):
        photos_db = tmp_path / "Photos.sqlite"
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        _create_photos_db(
            photos_db,
            [(1, "Unmatched Person")],
            [(1, 1, 1)],
        )

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_dir))
        monkeypatch.setattr(sys, "platform", "darwin")
        save_entity_photos(
            "stale_person",
            [{"day": "20240218", "face_cluster_pk": 1}],
        )

        result = runner.invoke(
            call_app,
            ["photos", "sync", "--library", str(photos_db)],
        )

        assert result.exit_code == 0
        assert "Found 1 named face clusters." in result.output
        assert "Matched 0 to entities." in result.output
        assert "Created 0 photo entries." in result.output
        assert not (journal_dir / "entities" / "stale_person" / "photos.jsonl").exists()

    def test_fallback_tables(self, tmp_path, monkeypatch):
        photos_db = tmp_path / "Photos.sqlite"
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        conn = sqlite3.Connection(photos_db)
        try:
            conn.execute(
                "CREATE TABLE ZGENERICPERSON (Z_PK INTEGER PRIMARY KEY, ZFULLNAME TEXT, ZMERGEDINTO INTEGER)"
            )
            conn.execute(
                "CREATE TABLE ZGENERICASSET (Z_PK INTEGER PRIMARY KEY, ZDATECREATED REAL)"
            )
            conn.execute(
                "CREATE TABLE ZDETECTEDFACE (Z_PK INTEGER PRIMARY KEY, ZPERSON INTEGER, ZASSET INTEGER)"
            )
            conn.execute(
                "INSERT INTO ZGENERICPERSON (Z_PK, ZFULLNAME, ZMERGEDINTO) VALUES (1, 'Alice Johnson', NULL)"
            )
            conn.execute(
                "INSERT INTO ZGENERICASSET (Z_PK, ZDATECREATED) VALUES (1, 730000000)"
            )
            conn.execute(
                "INSERT INTO ZDETECTEDFACE (Z_PK, ZPERSON, ZASSET) VALUES (1, 1, 1)"
            )
            conn.commit()
        finally:
            conn.close()
        _create_journal(
            journal_dir,
            [{"id": "alice_johnson", "name": "Alice Johnson", "type": "Person"}],
        )

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_dir))
        monkeypatch.setattr(sys, "platform", "darwin")

        result = runner.invoke(
            call_app, ["photos", "sync", "--library", str(photos_db)]
        )
        assert result.exit_code == 0
        assert "Found 1 named face clusters." in result.output
        assert _load_photo_entries(journal_dir, "alice_johnson") == [
            {"day": "20240218", "face_cluster_pk": 1},
        ]
