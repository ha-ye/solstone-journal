# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from solstone.think.link import establish
from solstone.think.link.ca import LoadedCa, load_ca, load_or_generate_ca
from solstone.think.link.mark import Mark, jid_from_spki, mark_from_spki
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


def test_lock_in_normalizes_legacy_random_state_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    load_or_generate_ca(ca_dir())
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()

    establish.lock_in()

    state = LinkState.load()
    assert state is not None
    assert state.instance_id == _derived_jid(load_ca(ca_dir()))
    assert state.home_label == "legacy"
    assert state.locked_at is not None


def test_create_link_state_normalizes_legacy_id_against_committed_ca(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    load_or_generate_ca(ca_dir())
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()

    state = establish.create_link_state()

    reloaded = LinkState.load()
    derived = _derived_jid(load_ca(ca_dir()))
    assert state.instance_id == derived
    assert state.home_label == "legacy"
    assert state.locked_at is not None
    assert reloaded is not None
    assert reloaded.instance_id == derived
    assert reloaded.home_label == "legacy"
    assert reloaded.locked_at is not None


def test_load_or_create_normalizes_legacy_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    load_or_generate_ca(ca_dir())
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()

    state = LinkState.load_or_create()

    assert state.instance_id == _derived_jid(load_ca(ca_dir()))
    assert state.home_label == "legacy"
    assert state.locked_at is not None


def test_normalize_legacy_id_without_ca_leaves_state_and_makes_no_ca(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()
    before = state_path().read_bytes()

    state = LinkState.load_or_create()

    ca_path = ca_dir()
    assert not (ca_path / "cert.pem").exists()
    assert not (ca_path / "private.pem").exists()
    assert state_path().read_bytes() == before
    assert state.instance_id == legacy_id


def test_normalize_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    load_or_generate_ca(ca_dir())
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()

    first = LinkState.load_or_create()
    first_payload = state_path().read_bytes()
    second = LinkState.load_or_create()
    second_payload = state_path().read_bytes()

    assert first.instance_id == _derived_jid(load_ca(ca_dir()))
    assert second.instance_id == first.instance_id
    assert second_payload == first_payload


def test_already_derived_id_untouched(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    created = establish.create_link_state()
    before = state_path().read_bytes()

    loaded = LinkState.load_or_create()

    assert loaded.instance_id == created.instance_id
    assert state_path().read_bytes() == before


def test_normalize_invariant_raises_on_nondeterministic_derivation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    load_or_generate_ca(ca_dir())
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()
    values = iter([uuid.UUID(int=1), uuid.UUID(int=2)])
    monkeypatch.setattr(establish, "jid_from_spki", lambda _spki: next(values))

    with pytest.raises(
        RuntimeError,
        match="link identity normalization produced a non-deterministic instance_id",
    ):
        establish.create_link_state()


def test_normalize_invariant_does_not_raise_for_already_derived_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    created = establish.create_link_state()
    values = iter([uuid.UUID(created.instance_id), uuid.UUID(int=2)])
    monkeypatch.setattr(establish, "jid_from_spki", lambda _spki: next(values))

    state = establish.create_link_state()

    assert state.instance_id == created.instance_id


def test_normalization_leaves_ca_and_mark_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_journal(monkeypatch, tmp_path)
    load_or_generate_ca(ca_dir())
    ca_path = ca_dir()
    cert_before = (ca_path / "cert.pem").read_bytes()
    key_before = (ca_path / "private.pem").read_bytes()
    mark_before = mark_from_spki(_spki_der(load_ca(ca_dir())))
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()

    state = LinkState.load_or_create()

    assert (ca_path / "cert.pem").read_bytes() == cert_before
    assert (ca_path / "private.pem").read_bytes() == key_before
    assert mark_from_spki(_spki_der(load_ca(ca_dir()))) == mark_before
    assert state.instance_id == _derived_jid(load_ca(ca_dir()))


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
