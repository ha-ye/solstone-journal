# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from solstone.think.link import establish
from solstone.think.link.ca import LoadedCa, load_ca, load_or_generate_ca
from solstone.think.link.mark import Mark, jid_from_spki
from solstone.think.link.paths import LinkState, ca_dir, staging_dir, state_path


def _set_journal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None


def _spki_der(ca: LoadedCa) -> bytes:
    return ca.cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _derived_jid(ca: LoadedCa) -> str:
    return str(jid_from_spki(_spki_der(ca)))


def test_lock_in_persists_id_derived_from_permanent_ca(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)

    establish.regenerate_candidate()
    establish.lock_in()

    permanent = load_ca(ca_dir())
    state = LinkState.load()
    assert state is not None
    assert state.instance_id == _derived_jid(permanent)


def test_lock_in_preserves_legacy_random_state_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    load_or_generate_ca(ca_dir())
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()
    before = state_path().read_bytes()

    establish.lock_in()

    assert state_path().read_bytes() == before
    state = LinkState.load()
    assert state is not None
    assert state.instance_id == legacy_id
    assert state.locked_at is None


def test_lock_in_self_heals_missing_state_from_committed_ca(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    permanent = load_or_generate_ca(ca_dir())
    assert not state_path().exists()

    establish.lock_in()

    state = LinkState.load()
    assert state is not None
    assert state.instance_id == _derived_jid(permanent)
    assert state.locked_at is not None


def test_regenerate_candidate_changes_mark_without_committing_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)

    first = establish.regenerate_candidate()
    second = establish.regenerate_candidate()

    assert isinstance(first, Mark)
    assert isinstance(second, Mark)
    assert second != first
    assert not (tmp_path / "link" / "ca" / "cert.pem").exists()
    assert not (tmp_path / "link" / "ca" / "private.pem").exists()
    assert not (tmp_path / "link" / "state.json").exists()


def test_corrupt_staging_candidate_is_repaired(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    staging = staging_dir()
    staging.mkdir(parents=True)
    (staging / "cert.pem").write_text("not a cert", encoding="utf-8")
    (staging / "private.pem").write_text("not a key", encoding="utf-8")

    mark = establish.current_candidate_mark()
    regenerated = establish.regenerate_candidate()

    assert isinstance(mark, Mark)
    assert isinstance(regenerated, Mark)
    load_ca(staging_dir())


def test_lock_in_is_one_way_idempotent_and_private(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)

    establish.current_candidate_mark()
    first_mark = establish.lock_in()
    state = LinkState.load()
    assert state is not None
    first_id = state.instance_id
    first_locked_at = state.locked_at
    first_ca = load_ca(ca_dir())

    second_mark = establish.lock_in()
    second_state = LinkState.load()
    assert second_state is not None

    assert isinstance(first_mark, Mark)
    assert second_mark == first_mark
    assert first_id == _derived_jid(first_ca)
    assert second_state.instance_id == first_id
    assert second_state.locked_at == first_locked_at
    assert (ca_dir() / "cert.pem").exists()
    assert (ca_dir() / "private.pem").exists()
    assert (ca_dir() / "private.pem").stat().st_mode & 0o777 == 0o600


def test_uuidv8_round_trip_and_jid_accessor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)

    establish.lock_in()
    state = LinkState.load()
    assert state is not None
    parsed = uuid.UUID(state.instance_id)

    assert parsed.version == 8
    assert uuid.UUID(bytes=parsed.bytes) == parsed
    assert state.jid == state.instance_id


def test_create_link_state_uses_permanent_ca_without_locked_at(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)

    state = establish.create_link_state(default_label="laptop")
    permanent = load_ca(ca_dir())
    payload = json.loads(state_path().read_text("utf-8"))

    assert state.instance_id == _derived_jid(permanent)
    assert state.home_label == "laptop"
    assert state.locked_at is None
    assert "locked_at" not in payload
