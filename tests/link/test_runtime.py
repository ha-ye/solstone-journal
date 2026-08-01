# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from solstone.think.link import runtime as link_runtime
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path


def test_start_link_runtime_backfills_label_ordinals_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    link_runtime.stop_all_link_runtime()
    path = authorized_clients_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "fingerprint": "sha256:a",
                    "device_label": "iPhone",
                    "paired_at": "2026-04-19T00:00:01Z",
                    "instance_id": "inst-1",
                },
                {
                    "fingerprint": "sha256:b",
                    "device_label": "iPhone",
                    "paired_at": "2026-04-19T00:00:02Z",
                    "instance_id": "inst-1",
                },
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    calls = []
    real_backfill = AuthorizedClients.backfill_label_ordinals

    def record_backfill(self: AuthorizedClients) -> bool:
        calls.append(self.path)
        return real_backfill(self)

    monkeypatch.setattr(AuthorizedClients, "backfill_label_ordinals", record_backfill)
    monkeypatch.setattr(
        link_runtime,
        "_thread_main",
        lambda runtime: runtime.started_event.set(),
    )

    try:
        first_app = SimpleNamespace()
        second_app = SimpleNamespace()

        link_runtime.start_link_runtime(first_app)
        link_runtime.start_link_runtime(second_app)

        assert calls == [path]
        entries = {
            item["fingerprint"]: item for item in json.loads(path.read_text("utf-8"))
        }
        assert "label_ordinal" not in entries["sha256:a"]
        assert entries["sha256:b"]["label_ordinal"] == 2
        assert first_app.link_runtime_started is True
        assert second_app.link_runtime_started is True
    finally:
        link_runtime.stop_all_link_runtime()
