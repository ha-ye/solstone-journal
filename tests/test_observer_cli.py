# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.apps.observer.utils import (
    list_observers,
    revoke_observer_record,
    save_observer,
)
from solstone.observe import observer_cli
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path


@pytest.fixture
def observer_cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    journal = tmp_path / "journal"
    home.mkdir()
    journal.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    import solstone.convey.state as convey_state

    convey_state.journal_root = ""
    return SimpleNamespace(home=home, journal=journal)


def _observer(name: str = "archon", key: str = "existing-key-abcdef") -> dict:
    return {
        "key": key,
        "name": name,
        "created_at": 1,
        "last_seen": None,
        "last_segment": None,
        "enabled": True,
        "stats": {"segments_received": 0, "bytes_received": 0},
    }


def _observer_with_stats(
    *,
    name: str,
    key: str,
    created_at: int,
    segments_received: int,
    bytes_received: int,
    duplicates_rejected: int = 0,
) -> dict:
    record = _observer(name=name, key=key)
    record["created_at"] = created_at
    record["stats"] = {
        "segments_received": segments_received,
        "bytes_received": bytes_received,
        "duplicates_rejected": duplicates_rejected,
    }
    return record


def test_create_observer_record_reuses_existing_without_create_side_effects(
    observer_cli_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _observer()
    assert save_observer(existing)
    monkeypatch.setattr(
        observer_cli,
        "_generate_key",
        lambda: pytest.fail("reuse must not generate a new key"),
    )
    monkeypatch.setattr(
        observer_cli,
        "save_observer",
        lambda _data: pytest.fail("reuse must not save"),
    )
    monkeypatch.setattr(
        observer_cli,
        "log_app_action",
        lambda **_kwargs: pytest.fail("reuse must not log observer_create"),
    )

    record, key, reused = observer_cli.create_observer_record(
        "archon", reuse_existing=True
    )

    assert record["key"] == existing["key"]
    assert record["name"] == existing["name"]
    assert record["filename_prefix"] == "existing"
    assert key == "existing-key-abcdef"
    assert reused is True
    assert list_observers() == [record]


def test_create_observer_record_fresh_create_returns_reused_false_and_logs(
    observer_cli_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = []
    monkeypatch.setattr(observer_cli, "_generate_key", lambda: "fresh-key-abcdef")
    monkeypatch.setattr(
        observer_cli, "log_app_action", lambda **kwargs: logs.append(kwargs)
    )

    record, key, reused = observer_cli.create_observer_record("archon")

    assert key == "fresh-key-abcdef"
    assert reused is False
    assert record["name"] == "archon"
    assert list_observers()[0]["key"] == "fresh-key-abcdef"
    assert logs == [
        {
            "app": "observer",
            "facet": None,
            "action": "observer_create",
            "params": {"name": "archon", "key_prefix": "fresh-ke"},
        }
    ]


def test_create_observer_record_duplicate_without_reuse_still_fails(
    observer_cli_env,
) -> None:
    assert save_observer(_observer())

    with pytest.raises(ValueError, match="observer already exists: archon"):
        observer_cli.create_observer_record("archon")


def test_cmd_create_duplicate_without_reuse_exits_one(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer())
    args = argparse.Namespace(
        name="archon",
        json_output=False,
        reuse_existing=False,
    )

    rc = observer_cli.cmd_create(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err == "Error: observer 'archon' already exists\n"


def test_cmd_create_reuse_existing_json_shape(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = _observer()
    assert save_observer(existing)
    args = argparse.Namespace(
        name="archon",
        json_output=True,
        reuse_existing=True,
    )

    rc = observer_cli.cmd_create(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == (
        json.dumps(
            {
                "name": "archon",
                "key": "existing-key-abcdef",
                "prefix": "existing",
            }
        )
        + "\n"
    )


def test_cmd_create_reuse_existing_human_header(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = _observer()
    assert save_observer(existing)
    args = argparse.Namespace(
        name="archon",
        json_output=False,
        reuse_existing=True,
    )

    rc = observer_cli.cmd_create(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Reusing existing observer:" in captured.out
    assert "Observer created:" not in captured.out
    assert "  api key:     existing-key-abcdef" in captured.out


def test_cmd_create_reuse_existing_creates_normally_when_absent(
    observer_cli_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs = []
    monkeypatch.setattr(observer_cli, "_generate_key", lambda: "fresh-key-abcdef")
    monkeypatch.setattr(
        observer_cli, "log_app_action", lambda **kwargs: logs.append(kwargs)
    )
    args = argparse.Namespace(
        name="archon",
        json_output=False,
        reuse_existing=True,
    )

    rc = observer_cli.cmd_create(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Observer created:" in captured.out
    assert "Reusing existing observer:" not in captured.out
    assert "  api key:     fresh-key-abcdef" in captured.out
    assert list_observers()[0]["key"] == "fresh-key-abcdef"
    assert logs == [
        {
            "app": "observer",
            "facet": None,
            "action": "observer_create",
            "params": {"name": "archon", "key_prefix": "fresh-ke"},
        }
    ]


def test_reconcile_collapses_duplicates_oldest_survives(observer_cli_env) -> None:
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="newest03-key",
            created_at=3,
            segments_received=5,
            bytes_received=100,
            duplicates_rejected=1,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="oldest01-key",
            created_at=1,
            segments_received=7,
            bytes_received=200,
            duplicates_rejected=2,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="middle02-key",
            created_at=2,
            segments_received=11,
            bytes_received=300,
        )
    )
    lone = _observer_with_stats(
        name="fedora",
        key="desktop1-key",
        created_at=4,
        segments_received=13,
        bytes_received=400,
        duplicates_rejected=5,
    )
    assert save_observer(lone)

    plan = observer_cli.reconcile_observers(dry_run=False)

    assert plan == [
        {
            "name": "fedora.tmux",
            "survivor_prefix": "oldest01",
            "revoked_prefixes": ["newest03", "middle02"],
            "stats": {
                "segments_received": 23,
                "bytes_received": 600,
                "duplicates_rejected": 3,
            },
        }
    ]
    records = list_observers()
    tmux_records = [record for record in records if record["name"] == "fedora.tmux"]
    unrevoked_tmux = [
        record for record in tmux_records if not record.get("revoked", False)
    ]
    assert len(unrevoked_tmux) == 1
    assert unrevoked_tmux[0]["created_at"] == 1
    assert unrevoked_tmux[0]["stats"] == {
        "segments_received": 23,
        "bytes_received": 600,
        "duplicates_rejected": 3,
    }
    revoked_tmux = [record for record in tmux_records if record.get("revoked", False)]
    assert {record["created_at"] for record in revoked_tmux} == {2, 3}
    lone_record = next(record for record in records if record["name"] == "fedora")
    assert lone_record.get("revoked", False) is False
    assert lone_record["stats"] == lone["stats"]


def test_reconcile_dry_run_mutates_nothing(observer_cli_env) -> None:
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="newest03-key",
            created_at=3,
            segments_received=5,
            bytes_received=100,
            duplicates_rejected=1,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="oldest01-key",
            created_at=1,
            segments_received=7,
            bytes_received=200,
            duplicates_rejected=2,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="middle02-key",
            created_at=2,
            segments_received=11,
            bytes_received=300,
        )
    )
    observers_dir = observer_cli_env.journal / "apps" / "observer" / "observers"
    before = {path.name: path.read_bytes() for path in observers_dir.glob("*.json")}

    plan = observer_cli.reconcile_observers(dry_run=True)

    assert plan == [
        {
            "name": "fedora.tmux",
            "survivor_prefix": "oldest01",
            "revoked_prefixes": ["newest03", "middle02"],
            "stats": {
                "segments_received": 23,
                "bytes_received": 600,
                "duplicates_rejected": 3,
            },
        }
    ]
    after = {path.name: path.read_bytes() for path in observers_dir.glob("*.json")}
    assert after == before


def test_reconcile_lone_stream_returns_empty_plan(observer_cli_env) -> None:
    lone = _observer_with_stats(
        name="fedora",
        key="desktop1-key",
        created_at=1,
        segments_received=13,
        bytes_received=400,
        duplicates_rejected=5,
    )
    assert save_observer(lone)

    plan = observer_cli.reconcile_observers(dry_run=False)

    assert plan == []
    records = list_observers()
    assert len(records) == 1
    assert records[0].get("revoked", False) is False
    assert records[0]["stats"] == lone["stats"]


def test_cmd_reconcile_reports_plan(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="newest03-key",
            created_at=3,
            segments_received=5,
            bytes_received=100,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="oldest01-key",
            created_at=1,
            segments_received=7,
            bytes_received=200,
        )
    )

    rc = observer_cli.cmd_reconcile(
        argparse.Namespace(dry_run=False, json_output=False)
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Reconciled stream 'fedora.tmux':" in captured.out
    assert "  survivor:  oldest01" in captured.out
    assert "  revoking:  newest03" in captured.out


def test_cmd_reconcile_no_duplicates(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="fedora", key="desktop1-key"))

    rc = observer_cli.cmd_reconcile(
        argparse.Namespace(dry_run=False, json_output=False)
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == "No duplicate observer streams to reconcile.\n"


def test_cmd_list_json_includes_prefix_and_status(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="desktop", key="abcdefgh12345678"))
    args = argparse.Namespace(json_output=True)

    rc = observer_cli.cmd_list(args)

    captured = capsys.readouterr()
    assert rc == 0
    rows = {row["name"]: row for row in json.loads(captured.out)}
    assert rows["desktop"]["prefix"] == "abcdefgh"
    assert rows["desktop"]["status"] == "disconnected"
    assert "mode" not in rows["desktop"]


def test_cmd_list_human_shows_prefix_column(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="desktop", key="abcdefgh12345678"))
    args = argparse.Namespace(json_output=False)

    rc = observer_cli.cmd_list(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert "Name                 Prefix" in captured.out
    assert "Mode" not in captured.out
    assert "desktop              abcdefgh" in captured.out


def test_cmd_status_single_reports_prefix(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="desktop", key="cdefghij12345678"))

    rc = observer_cli.cmd_status(
        argparse.Namespace(identifier="desktop", json_output=True)
    )

    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["prefix"] == "cdefghij"
    assert payload["status"] == "disconnected"
    assert "mode" not in payload


def test_cmd_status_all_table_shows_prefix(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="desktop", key="abcdefgh12345678"))

    rc = observer_cli.cmd_status(argparse.Namespace(identifier=None, json_output=False))

    captured = capsys.readouterr()
    assert rc == 0
    assert "Name                 Prefix" in captured.out
    assert "Mode" not in captured.out
    assert "desktop              abcdefgh" in captured.out


def test_revoke_dl_observer_leaves_authorized_clients_untouched(
    observer_cli_env,
) -> None:
    assert save_observer(_observer(name="desktop", key="abcdefgh12345678"))
    fingerprint = "sha256:" + ("f" * 64)
    authorized = AuthorizedClients(authorized_clients_path())
    authorized.add(
        fingerprint,
        "phone",
        "inst-1",
        paired_at="2026-05-20T00:00:00Z",
    )
    before = authorized_clients_path().read_bytes()

    record = revoke_observer_record("desktop")

    assert record["revoked"] is True
    assert authorized_clients_path().read_bytes() == before
    assert (
        AuthorizedClients(authorized_clients_path()).is_authorized(fingerprint) is True
    )
