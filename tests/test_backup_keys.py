# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.backup import keys
from solstone.think.backup.keys import (
    ALPHABET,
    RECOVERY_KEY_LENGTH,
    confirm_recovery_key,
    format_recovery_key_display,
    generate_daily_key,
    generate_recovery_key,
    parse_recovery_key,
)


def test_generate_daily_key_uses_urlsafe_token(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter(["daily-one", "daily-two"])
    calls: list[int] = []

    def fake_token_urlsafe(length: int) -> str:
        calls.append(length)
        return next(values)

    monkeypatch.setattr(keys.secrets, "token_urlsafe", fake_token_urlsafe)

    assert generate_daily_key() == "daily-one"
    assert generate_daily_key() == "daily-two"
    assert calls == [32, 32]


def test_generate_recovery_key_uses_crockford_choice_per_char(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_choice(alphabet: str) -> str:
        nonlocal calls
        calls += 1
        assert alphabet == ALPHABET
        return "A" if calls <= RECOVERY_KEY_LENGTH else "B"

    monkeypatch.setattr(keys.secrets, "choice", fake_choice)

    first = generate_recovery_key()
    second = generate_recovery_key()

    assert first == "A" * RECOVERY_KEY_LENGTH
    assert second == "B" * RECOVERY_KEY_LENGTH
    assert calls == RECOVERY_KEY_LENGTH * 2


def test_recovery_key_display_groups_canonical_key() -> None:
    canonical = "ABCDEFGHJKMNPQRSTVWXYZ0123456789" * 2

    display = format_recovery_key_display(canonical)

    groups = display.split(" ")
    assert len(groups) == 16
    assert all(len(group) == 4 for group in groups)
    assert "".join(groups) == canonical


@pytest.mark.parametrize(
    "canonical",
    [
        "A" * 63,
        "A" * 65,
        "U" * 64,
        "a" * 64,
    ],
)
def test_recovery_key_display_rejects_invalid_canonical(canonical: str) -> None:
    with pytest.raises(ValueError):
        format_recovery_key_display(canonical)


def test_parse_recovery_key_strips_grouping_and_uppercases() -> None:
    canonical = "ABCDEFGHJKMNPQRSTVWXYZ0123456789" * 2
    entered = (
        f"{canonical[:8].lower()} - {canonical[8:16]}\n"
        f"{canonical[16:32].lower()}  {canonical[32:]}"
    )

    assert parse_recovery_key(entered) == canonical


def test_parse_recovery_key_folds_lookalikes_and_confirm_accepts() -> None:
    canonical = "011" + ("A" * 61)
    entered = "OIL " + " ".join(
        canonical[index : index + 4] for index in range(3, RECOVERY_KEY_LENGTH, 4)
    )

    assert parse_recovery_key(entered) == canonical
    assert confirm_recovery_key(entered, canonical) is True


@pytest.mark.parametrize("entered", ["", "not-a-key", "A" * 63, "A" * 65])
def test_parse_recovery_key_rejects_wrong_cleaned_length(entered: str) -> None:
    with pytest.raises(ValueError, match="exactly 64"):
        parse_recovery_key(entered)


def test_confirm_recovery_key_rejects_wrong_key_without_raising() -> None:
    canonical = "A" * 64
    wrong = "A" * 63 + "B"

    assert confirm_recovery_key(wrong, canonical) is False
    assert confirm_recovery_key("short", canonical) is False


def test_lookalike_folding_does_not_collapse_distinct_canonical_keys() -> None:
    canonical = "0" + ("A" * 63)
    distinct = "1" + ("A" * 63)

    assert confirm_recovery_key("O" + ("A" * 63), canonical) is True
    assert confirm_recovery_key("O" + ("A" * 63), distinct) is False
