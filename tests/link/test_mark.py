# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for journal jid and mark derivation."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from importlib import resources
from typing import Any

import argon2.low_level
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from solstone.think.link import mark as mark_module
from solstone.think.link.mark import (
    Mark,
    jid_from_spki,
    mark_from_jid,
    mark_from_spki,
    pick_distinct,
)


@dataclass(frozen=True)
class ReferenceVector:
    name: str
    spki_hex: str
    jid: str
    digest: str
    icon1: str
    color1: str
    color1_hex: str
    icon2: str
    color2: str
    color2_hex: str
    word1: str
    word2: str
    rot1: int
    rot2: int
    raw16: str | None = None


VECTORS = (
    ReferenceVector(
        name="primary",
        spki_hex="3059301306072a8648ce3d020106082a8648ce3d03010703420004471c3e758c4904285bba7e53118ed0f524adeb0757d25bd2f8e7b0d76dfa714cdd520f7aca8a8b917acc37f51de8f0c9bbe3ad858382e702dc25a12d09f7a858",
        raw16="f30ed159ef466e9c113fe49f0fe7d201",
        jid="f30ed159-ef46-8e9c-913f-e49f0fe7d201",
        digest="4e436f135100f0ecc94146f99ac603b5e31161ed90774e499770618925543fc2",
        icon1="piano",
        color1="blue",
        color1_hex="#3b82f6",
        icon2="key",
        color2="purple",
        color2_hex="#a855f7",
        word1="liquefy",
        word2="smock",
        rot1=45,
        rot2=0,
    ),
    ReferenceVector(
        name="rot-0-0",
        spki_hex="3059301306072a8648ce3d020106082a8648ce3d030107034200047cf27b188d034f7e8a52380304b51ac3c08969e277f21b35a60b48fc4766997807775510db8ed040293d9ac69f7430dbba7dade63ce982299e04b79d227873d1",
        jid="62bde3af-1ef4-8292-84db-1e5ac2c07e8b",
        digest="15b2e261a06544e78334d07e0e6505d9e657a22193b46221b8e7e9ec6b897f50",
        icon1="turtle",
        color1="pink",
        color1_hex="#ec4899",
        icon2="pizza",
        color2="cyan",
        color2_hex="#06b6d4",
        word1="distrust",
        word2="chokehold",
        rot1=0,
        rot2=0,
    ),
    ReferenceVector(
        name="rot-0-1",
        spki_hex="3059301306072a8648ce3d020106082a8648ce3d030107034200048e533b6fa0bf7b4625bb30667c01fb607ef9f8b8a80fef5b300628703187b2a373eb1dbde03318366d069f83a6f5900053c73633cb041b21c55e1a86c1f400b4",
        jid="75e46c0d-1c50-892b-98ff-4d174c135add",
        digest="92703a37b827760c681472dd01e1bc86b9003a1f8e0174215837caba1b5ee2a3",
        icon1="dice-5",
        color1="magenta",
        color1_hex="#d946ef",
        icon2="snail",
        color2="sky",
        color2_hex="#38bdf8",
        word1="delay",
        word2="safari",
        rot1=0,
        rot2=45,
    ),
    ReferenceVector(
        name="rot-1-1",
        spki_hex="3059301306072a8648ce3d020106082a8648ce3d03010703420004ea68d7b6fedf0b71878938d51d71f8729e0acb8c2c6df8b3d79e8a4b90949ee02a2744c972c9fce787014a964a8ea0c84d714feaa4de823fe85a224a4dd048fa",
        jid="bb8f23b4-fd5e-8ca9-98c7-c1dfa927a840",
        digest="00d3c5bc17e50c956badedd1a3c02788ac2990261b885c4dabdd7cbf9506a05c",
        icon1="truck",
        color1="orange",
        color1_hex="#f97316",
        icon2="snail",
        color2="teal",
        color2_hex="#14b8a6",
        word1="duvet",
        word2="capital",
        rot1=45,
        rot2=45,
    ),
)


def _spki_der(vector: ReferenceVector) -> bytes:
    return bytes.fromhex(vector.spki_hex)


def _public_spki(public_key: object, encoding: serialization.Encoding) -> bytes:
    if not hasattr(public_key, "public_bytes"):
        raise TypeError("public key object must support public_bytes")
    return public_key.public_bytes(  # type: ignore[attr-defined]
        encoding,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _p256_spki() -> bytes:
    return _public_spki(
        ec.generate_private_key(ec.SECP256R1()).public_key(),
        serialization.Encoding.DER,
    )


def _raw16_from_spki(spki_der: bytes) -> bytes:
    key = serialization.load_der_public_key(spki_der)
    ikm = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=b"solstone/journal/v1",
        info=b"solstone/jid/uuidv8/v1",
    ).derive(ikm)


def _argon2_digest_hex(jid: uuid.UUID) -> str:
    return argon2.low_level.hash_secret_raw(
        secret=jid.bytes,
        salt=b"solstone-journal-mark-v1",
        time_cost=3,
        memory_cost=65536,
        parallelism=1,
        hash_len=32,
        type=argon2.low_level.Type.ID,
        version=0x13,
    ).hex()


def _assert_mark_matches(mark: Mark, vector: ReferenceVector) -> None:
    assert mark.icon1.name == vector.icon1
    assert mark.icon1.color_name == vector.color1
    assert mark.icon1.color_hex == vector.color1_hex
    assert mark.icon1.rot == vector.rot1
    assert mark.icon2.name == vector.icon2
    assert mark.icon2.color_name == vector.color2
    assert mark.icon2.color_hex == vector.color2_hex
    assert mark.icon2.rot == vector.rot2
    assert mark.words == (vector.word1, vector.word2)


def _load_asset(filename: str) -> Any:
    asset = resources.files("solstone.think.link") / "mark_assets" / filename
    return json.loads(asset.read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", VECTORS, ids=[vector.name for vector in VECTORS])
def test_jid_from_spki_matches_reference_vectors(vector: ReferenceVector) -> None:
    jid = jid_from_spki(_spki_der(vector))

    assert jid == uuid.UUID(vector.jid)
    assert jid.version == 8
    assert (jid.bytes[8] >> 6) == 0b10

    if vector.raw16 is not None:
        assert _raw16_from_spki(_spki_der(vector)).hex() == vector.raw16
        assert jid.bytes.hex() == "f30ed159ef468e9c913fe49f0fe7d201"


@pytest.mark.parametrize("vector", VECTORS, ids=[vector.name for vector in VECTORS])
def test_mark_from_spki_matches_reference_vectors(vector: ReferenceVector) -> None:
    jid = uuid.UUID(vector.jid)

    assert _argon2_digest_hex(jid) == vector.digest

    mark_from_key = mark_from_spki(_spki_der(vector))
    mark_from_identity = mark_from_jid(jid)

    _assert_mark_matches(mark_from_key, vector)
    _assert_mark_matches(mark_from_identity, vector)
    assert mark_from_key == mark_from_identity


def test_primary_render_spec_matches_pinned_shape() -> None:
    vector = VECTORS[0]
    glyphs = _load_asset("glyphs.json")
    mark = mark_from_spki(_spki_der(vector))

    assert mark.to_render_spec() == {
        "icon1": {
            "name": "piano",
            "svg": glyphs["piano"],
            "color": {"name": "blue", "hex": "#3b82f6"},
            "rot": 45,
        },
        "icon2": {
            "name": "key",
            "svg": glyphs["key"],
            "color": {"name": "purple", "hex": "#a855f7"},
            "rot": 0,
        },
        "words": ["liquefy", "smock"],
    }


@pytest.mark.parametrize("n", [60, 16, 7776])
def test_pick_distinct_boundary_branches(n: int) -> None:
    not_i = n // 2
    cases = (
        (not_i - 1, not_i - 1),
        (not_i + 1, not_i + 2),
        (not_i, not_i + 1),
    )

    for w, expected in cases:
        result = pick_distinct(w, n, not_i)
        assert result == expected
        assert result != not_i
        assert result < n


@pytest.mark.parametrize("vector", VECTORS, ids=[vector.name for vector in VECTORS])
def test_reference_vectors_have_distinct_mark_parts(vector: ReferenceVector) -> None:
    mark = mark_from_spki(_spki_der(vector))

    assert mark.icon2.name != mark.icon1.name
    assert mark.icon2.color_name != mark.icon1.color_name
    assert mark.words[1] != mark.words[0]


def test_fresh_p256_keys_have_distinct_mark_parts() -> None:
    for _ in range(3):
        mark = mark_from_spki(_p256_spki())

        assert mark.icon2.name != mark.icon1.name
        assert mark.icon2.color_name != mark.icon1.color_name
        assert mark.words[1] != mark.words[0]


def test_wrong_inputs_raise_value_error() -> None:
    p256_public = ec.generate_private_key(ec.SECP256R1()).public_key()
    invalid_inputs = (
        _public_spki(
            rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key(),
            serialization.Encoding.DER,
        ),
        _public_spki(
            ec.generate_private_key(ec.SECP384R1()).public_key(),
            serialization.Encoding.DER,
        ),
        p256_public.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.CompressedPoint,
        ),
        _public_spki(p256_public, serialization.Encoding.PEM),
        b"not a key",
    )

    for invalid in invalid_inputs:
        with pytest.raises(ValueError):
            jid_from_spki(invalid)
        with pytest.raises(ValueError):
            mark_from_spki(invalid)


def test_p256_spki_canonicalization_is_stable() -> None:
    original_spki = _p256_spki()
    key = serialization.load_der_public_key(original_spki)
    reserialized_spki = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    assert jid_from_spki(original_spki) == jid_from_spki(reserialized_spki)
    assert mark_from_spki(original_spki) == mark_from_spki(reserialized_spki)


def test_same_key_is_deterministic() -> None:
    spki_der = _p256_spki()

    assert jid_from_spki(spki_der) == jid_from_spki(spki_der)
    assert jid_from_spki(spki_der) == jid_from_spki(spki_der)
    assert mark_from_spki(spki_der) == mark_from_spki(spki_der)
    assert mark_from_spki(spki_der) == mark_from_spki(spki_der)


@pytest.mark.parametrize("vector", VECTORS, ids=[vector.name for vector in VECTORS])
def test_uuid_round_trips_wire_shape(vector: ReferenceVector) -> None:
    jid = jid_from_spki(_spki_der(vector))

    assert uuid.UUID(bytes=jid.bytes) == jid
    assert uuid.UUID(str(jid)).bytes == jid.bytes


def test_mark_assets_load_from_package_path_with_expected_counts() -> None:
    glyphs = _load_asset("glyphs.json")
    colors = _load_asset("colors.json")
    words = _load_asset("words.json")

    assert len(glyphs) == 60
    assert len(colors) == 16
    assert len(words) == 7776


def test_asset_loaders_raise_on_wrong_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mark_module,
        "_load_json_asset",
        lambda _filename: {"only": "<svg>"},
    )
    with pytest.raises(ValueError):
        mark_module._load_glyphs()

    monkeypatch.setattr(
        mark_module,
        "_load_json_asset",
        lambda _filename: [["crimson", "#ef4444"]],
    )
    with pytest.raises(ValueError):
        mark_module._load_colors()

    monkeypatch.setattr(
        mark_module,
        "_load_json_asset",
        lambda _filename: ["abacus"],
    )
    with pytest.raises(ValueError):
        mark_module._load_words()


def test_mark_from_jid_uses_locked_argon2_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jid = uuid.UUID(VECTORS[0].jid)
    captured: dict[str, object] = {}

    def fake_hash_secret_raw(**kwargs: object) -> bytes:
        captured.update(kwargs)
        return bytes(32)

    monkeypatch.setattr(
        mark_module.argon2.low_level,
        "hash_secret_raw",
        fake_hash_secret_raw,
    )

    mark_from_jid(jid)

    assert captured == {
        "secret": jid.bytes,
        "salt": b"solstone-journal-mark-v1",
        "time_cost": 3,
        "memory_cost": 65536,
        "parallelism": 1,
        "hash_len": 32,
        "type": argon2.low_level.Type.ID,
        "version": 0x13,
    }
