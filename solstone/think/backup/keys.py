# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure key generation and recovery-key formatting for solstone backup."""

from __future__ import annotations

import secrets

from solstone.apps.link.crockford32 import ALPHABET

RECOVERY_KEY_LENGTH = 64
_RECOVERY_GROUP_SIZE = 4
_ALPHABET_SET = frozenset(ALPHABET)
_LOOKALIKE_FOLDS = {
    "I": "1",
    "L": "1",
    "O": "0",
}


def generate_daily_key() -> str:
    return secrets.token_urlsafe(32)


def generate_recovery_key() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(RECOVERY_KEY_LENGTH))


def _validate_canonical_recovery_key(canonical: str) -> None:
    if len(canonical) != RECOVERY_KEY_LENGTH:
        raise ValueError("canonical recovery key must be exactly 64 characters")
    if any(char not in _ALPHABET_SET for char in canonical):
        raise ValueError("canonical recovery key contains invalid Crockford characters")


def format_recovery_key_display(canonical: str) -> str:
    _validate_canonical_recovery_key(canonical)
    return " ".join(
        canonical[index : index + _RECOVERY_GROUP_SIZE]
        for index in range(0, RECOVERY_KEY_LENGTH, _RECOVERY_GROUP_SIZE)
    )


def parse_recovery_key(entered: str) -> str:
    chars: list[str] = []
    for raw_char in entered:
        char = raw_char.upper()
        char = _LOOKALIKE_FOLDS.get(char, char)
        if char in _ALPHABET_SET:
            chars.append(char)

    canonical = "".join(chars)
    if len(canonical) != RECOVERY_KEY_LENGTH:
        raise ValueError(
            "recovery key must contain exactly 64 Crockford characters after cleanup"
        )
    return canonical


def confirm_recovery_key(entered: str, canonical: str) -> bool:
    try:
        return parse_recovery_key(entered) == canonical
    except ValueError:
        return False


__all__ = [
    "ALPHABET",
    "RECOVERY_KEY_LENGTH",
    "confirm_recovery_key",
    "format_recovery_key_display",
    "generate_daily_key",
    "generate_recovery_key",
    "parse_recovery_key",
]
