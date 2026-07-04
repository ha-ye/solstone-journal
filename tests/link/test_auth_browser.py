# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from solstone.think.link.auth import AuthorizedClients


def test_browser_entry_survives_unrelated_cert_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    store = AuthorizedClients(path)

    browser = store.add_browser(
        fingerprint="sha256:" + "aa" * 32,
        device_label="Browser",
        instance_id="inst-1",
        pubkey_spki="30aa",
        observer_handle="handle123",
        paired_at="2026-07-01T00:00:00Z",
        network="anywhere",
    )
    assert browser.kind == "browser"

    store.add(
        "sha256:" + "bb" * 32,
        "Phone",
        "inst-1",
        paired_at="2026-07-01T00:01:00Z",
    )
    reloaded = AuthorizedClients(path)

    entry = reloaded.get("sha256:" + "aa" * 32)
    assert entry is not None
    assert entry.kind == "browser"
    assert entry.pubkey_spki == "30aa"
    assert entry.observer_handle == "handle123"
    assert entry.network == "anywhere"

    payload = json.loads(path.read_text("utf-8"))
    browser_payload = next(item for item in payload if item["kind"] == "browser")
    assert browser_payload["pubkey_spki"] == "30aa"
    assert browser_payload["observer_handle"] == "handle123"


def test_old_cert_entry_defaults_to_cert_kind(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            [
                {
                    "fingerprint": "sha256:legacy",
                    "device_label": "legacy",
                    "paired_at": "2026-07-01T00:00:00Z",
                    "instance_id": "inst-1",
                }
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    entry = AuthorizedClients(path).get("sha256:legacy")

    assert entry is not None
    assert entry.kind == "cert"
    assert entry.pubkey_spki is None
    assert entry.observer_handle is None
