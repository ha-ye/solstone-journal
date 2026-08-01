# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from solstone.think.link.nonces import NONCE_TTL_SECONDS, NonceStore


def test_add_persists_same_machine(tmp_path: Path) -> None:
    path = tmp_path / "nonces.json"
    store = NonceStore(path)

    store.add("abc123", "Home", same_machine=True, now=1000)

    payload = json.loads(path.read_text("utf-8"))
    assert payload[0]["same_machine"] is True


def test_add_defaults_not_same_machine(tmp_path: Path) -> None:
    path = tmp_path / "nonces.json"
    store = NonceStore(path)

    store.add("abc123", "Phone", now=1000)

    payload = json.loads(path.read_text("utf-8"))
    assert payload[0]["same_machine"] is False


def test_consume_preserves_same_machine(tmp_path: Path) -> None:
    store = NonceStore(tmp_path / "nonces.json")
    store.add("abc123", "Home", same_machine=True, now=1000)

    consumed = store.consume("abc123", now=1001)

    assert consumed is not None
    assert consumed.same_machine is True


def test_read_legacy_nonce_defaults_not_same_machine(tmp_path: Path) -> None:
    """A nonce minted before the field existed must still consume cleanly."""
    path = tmp_path / "nonces.json"
    path.write_text(
        json.dumps(
            [
                {
                    "value": "abc123",
                    "device_label": "Legacy",
                    "issued_at": 1000,
                    "expires_at": 1000 + NONCE_TTL_SECONDS,
                    "used": False,
                }
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    store = NonceStore(path)

    consumed = store.consume("abc123", now=1001)

    assert consumed is not None
    assert consumed.same_machine is False
