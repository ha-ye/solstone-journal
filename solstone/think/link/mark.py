# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Journal jid and mark derivation for P-256 public-key SPKI bytes.

This module implements the frozen journal identity contract. The jid and mark
are deterministic functions of a canonicalized P-256 public key, not of the
incoming byte encoding, and the module performs no writes or runtime caching.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from importlib import resources
from types import MappingProxyType
from typing import Mapping

import argon2.low_level
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ICON_COUNT = 60
_COLOR_COUNT = 16
_WORD_COUNT = 7776

_JID_HKDF_SALT = b"solstone/journal/v1"
_JID_HKDF_INFO = b"solstone/jid/uuidv8/v1"
_MARK_ARGON2_SALT = b"solstone-journal-mark-v1"


@dataclass(frozen=True)
class MarkIcon:
    """One icon selected by the journal-mark derivation contract."""

    name: str
    svg: str
    color_name: str
    color_hex: str
    rot: int


@dataclass(frozen=True)
class Mark:
    """Full journal mark selected from the frozen asset sets."""

    icon1: MarkIcon
    icon2: MarkIcon
    words: tuple[str, str]

    def to_render_spec(self) -> dict[str, object]:
        """Return the pinned renderer-facing dict shape for this mark."""
        return {
            "icon1": {
                "name": self.icon1.name,
                "svg": self.icon1.svg,
                "color": {
                    "name": self.icon1.color_name,
                    "hex": self.icon1.color_hex,
                },
                "rot": self.icon1.rot,
            },
            "icon2": {
                "name": self.icon2.name,
                "svg": self.icon2.svg,
                "color": {
                    "name": self.icon2.color_name,
                    "hex": self.icon2.color_hex,
                },
                "rot": self.icon2.rot,
            },
            "words": [self.words[0], self.words[1]],
        }


def _load_json_asset(filename: str) -> object:
    asset = resources.files("solstone.think.link") / "mark_assets" / filename
    return json.loads(asset.read_text(encoding="utf-8"))


def _load_glyphs() -> Mapping[str, str]:
    data = _load_json_asset("glyphs.json")
    if not isinstance(data, dict):
        raise ValueError("mark asset glyphs.json must be a JSON object")

    glyphs: dict[str, str] = {}
    for name, svg in data.items():
        if not isinstance(name, str) or not isinstance(svg, str):
            raise ValueError("mark asset glyphs.json must map strings to strings")
        glyphs[name] = svg

    if len(glyphs) != _ICON_COUNT:
        raise ValueError(
            f"mark asset glyphs.json must contain {_ICON_COUNT} icons; "
            f"found {len(glyphs)}"
        )
    return MappingProxyType(glyphs)


def _load_colors() -> tuple[tuple[str, str], ...]:
    data = _load_json_asset("colors.json")
    if not isinstance(data, list):
        raise ValueError("mark asset colors.json must be a JSON list")

    colors: list[tuple[str, str]] = []
    for item in data:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise ValueError("mark asset colors.json must contain [name, hex] pairs")
        colors.append((item[0], item[1]))

    if len(colors) != _COLOR_COUNT:
        raise ValueError(
            f"mark asset colors.json must contain {_COLOR_COUNT} colors; "
            f"found {len(colors)}"
        )
    return tuple(colors)


def _load_words() -> tuple[str, ...]:
    data = _load_json_asset("words.json")
    if not isinstance(data, list):
        raise ValueError("mark asset words.json must be a JSON list")

    words: list[str] = []
    for word in data:
        if not isinstance(word, str):
            raise ValueError("mark asset words.json must contain only strings")
        words.append(word)

    if len(words) != _WORD_COUNT:
        raise ValueError(
            f"mark asset words.json must contain {_WORD_COUNT} words; "
            f"found {len(words)}"
        )
    return tuple(words)


_GLYPHS = _load_glyphs()
_ICON_NAMES = tuple(_GLYPHS)
_COLORS = _load_colors()
_WORDS = _load_words()


def pick(w: int, n: int) -> int:
    """Select an index from an unsigned word by modulo reduction."""
    return w % n


def pick_distinct(w: int, n: int, not_i: int) -> int:
    """Select an index from ``n`` values while excluding ``not_i``."""
    i = w % (n - 1)
    if i >= not_i:
        i += 1
    return i


def jid_from_spki(spki_der: bytes) -> uuid.UUID:
    """Derive the journal UUIDv8 from a P-256 public-key SPKI.

    The input must be DER-encoded SubjectPublicKeyInfo for an EC P-256 public
    key. The key is loaded and re-serialized before HKDF derivation so the jid
    is a function of the key, not the exact incoming byte encoding.
    """
    key = serialization.load_der_public_key(spki_der)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError("journal jid requires an EC P-256 public-key SPKI")
    if not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("journal jid requires an EC P-256 public-key SPKI")

    ikm = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    raw16 = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=_JID_HKDF_SALT,
        info=_JID_HKDF_INFO,
    ).derive(ikm)

    b = bytearray(raw16)
    b[6] = (b[6] & 0x0F) | 0x80
    b[8] = (b[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(b))


def mark_from_jid(jid: uuid.UUID) -> Mark:
    """Derive the journal mark from a journal UUIDv8."""
    digest = argon2.low_level.hash_secret_raw(
        secret=jid.bytes,
        salt=_MARK_ARGON2_SALT,
        time_cost=3,
        memory_cost=65536,
        parallelism=1,
        hash_len=32,
        type=argon2.low_level.Type.ID,
        version=0x13,
    )
    u = [int.from_bytes(digest[i * 4 : i * 4 + 4], "big") for i in range(7)]

    icon1_i = pick(u[0], _ICON_COUNT)
    icon2_i = pick_distinct(u[1], _ICON_COUNT, icon1_i)
    color1_i = pick(u[2], _COLOR_COUNT)
    color2_i = pick_distinct(u[3], _COLOR_COUNT, color1_i)
    word1_i = pick(u[4], _WORD_COUNT)
    word2_i = pick_distinct(u[5], _WORD_COUNT, word1_i)
    rot1 = u[6] & 1
    rot2 = (u[6] >> 1) & 1

    icon1_name = _ICON_NAMES[icon1_i]
    icon2_name = _ICON_NAMES[icon2_i]
    color1_name, color1_hex = _COLORS[color1_i]
    color2_name, color2_hex = _COLORS[color2_i]

    return Mark(
        icon1=MarkIcon(
            name=icon1_name,
            svg=_GLYPHS[icon1_name],
            color_name=color1_name,
            color_hex=color1_hex,
            rot=45 if rot1 else 0,
        ),
        icon2=MarkIcon(
            name=icon2_name,
            svg=_GLYPHS[icon2_name],
            color_name=color2_name,
            color_hex=color2_hex,
            rot=45 if rot2 else 0,
        ),
        words=(_WORDS[word1_i], _WORDS[word2_i]),
    )


def mark_from_spki(spki_der: bytes) -> Mark:
    """Derive the journal mark from a P-256 public-key SPKI.

    The input must be DER-encoded SubjectPublicKeyInfo for an EC P-256 public
    key. The key is canonicalized through cryptography before jid derivation,
    so the mark is a function of the key, not the exact incoming byte encoding.
    """
    return mark_from_jid(jid_from_spki(spki_der))
