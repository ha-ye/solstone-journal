# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for observer self-registration contract."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from solstone.apps.observer.utils import (
    append_history_record,
    load_observer,
    save_observer,
)
from solstone.convey import create_app
from solstone.convey.secure_listener import ConveyIdentity
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path
from tests._baseline_harness import copytree_tracked

VALID_REGISTER_PAYLOAD = {
    "platform": "linux",
    "hostname": "fedora",
    "stream_type": "tmux",
    "version": "1",
}
PL_FINGERPRINT = "sha256:" + ("c" * 64)
PL_FINGERPRINT_2 = "sha256:" + ("d" * 64)


def _day_dir(env, day: str = "20250103"):
    return env.journal / "chronicle" / day


def _observers_dir(journal):
    return journal / "apps" / "observer" / "observers"


def _observer_paths(journal):
    return sorted(_observers_dir(journal).glob("*.json"))


def _observer_records(journal):
    return [json.loads(path.read_text()) for path in _observer_paths(journal)]


def _register(client, **overrides):
    payload = {**VALID_REGISTER_PAYLOAD, **overrides}
    return client.post("/app/observer/register", json=payload)


def _pl_identity(fingerprint: str = PL_FINGERPRINT) -> ConveyIdentity:
    return ConveyIdentity(
        mode="pl-via-spl",
        fingerprint=fingerprint,
        device_label="fedora-sat",
        paired_at="2026-06-15T00:00:00Z",
        session_id="session-1",
    )


def _authorize_pl(fingerprint: str = PL_FINGERPRINT) -> None:
    AuthorizedClients(authorized_clients_path()).add(
        fingerprint,
        "fedora-sat",
        "instance-1",
    )


def _assert_no_observer_records(journal) -> None:
    assert _observer_paths(journal) == []


def test_register_loopback_returns_pinned_response(observer_env):
    env = observer_env()

    resp = _register(env.client)

    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data) == {"key", "prefix", "name", "ingest_url", "protocol_version"}
    assert data["key"]
    assert data["prefix"] == data["key"][:8]
    assert data["name"] == "fedora.tmux"
    assert data["ingest_url"] == "/app/observer/ingest"
    assert data["protocol_version"] == 2


def test_register_desktop_and_qualified_streams_persist_distinctly(observer_env):
    env = observer_env()

    tmux = _register(env.client)
    desktop = _register(env.client, stream_type="desktop")

    assert tmux.status_code == 200
    assert desktop.status_code == 200
    assert tmux.get_json()["name"] == "fedora.tmux"
    assert desktop.get_json()["name"] == "fedora"

    records = _observer_records(env.journal)
    assert len(records) == 2
    assert {record["stream"] for record in records} == {"fedora.tmux", "fedora"}


def test_register_persists_descriptor_recoverably(observer_env):
    env = observer_env()

    resp = _register(env.client, label="Terminal capture")

    assert resp.status_code == 200
    key = resp.get_json()["key"]
    loaded = load_observer(key)
    assert loaded is not None

    stored_path = _observers_dir(env.journal) / f"{key[:8]}.json"
    stored = json.loads(stored_path.read_text())

    expected_descriptor = {
        "platform": "linux",
        "hostname": "fedora",
        "stream_type": "tmux",
        "label": "Terminal capture",
        "version": "1",
        "stream": "fedora.tmux",
    }
    assert {
        field: stored[field] for field in expected_descriptor
    } == expected_descriptor
    assert stored["name"] == stored["stream"]
    assert stored["stats"] == {"segments_received": 0, "bytes_received": 0}
    assert {
        field: loaded[field] for field in expected_descriptor
    } == expected_descriptor


@pytest.mark.parametrize(
    "field",
    ("platform", "hostname", "stream_type", "version"),
)
def test_register_requires_descriptor_fields(observer_env, field):
    env = observer_env()
    payload = {**VALID_REGISTER_PAYLOAD, field: " "}

    resp = env.client.post("/app/observer/register", json=payload)

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["reason_code"] == "missing_required_field"
    assert body["detail"] == f"{field} is required"
    _assert_no_observer_records(env.journal)


def test_register_works_before_setup_complete(tmp_path, monkeypatch):
    src = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "journal"
    journal_copy = tmp_path / "journal"
    copytree_tracked(src, journal_copy)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_copy.resolve()))

    config_path = journal_copy / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config.setdefault("setup", {}).pop("completed_at", None)
    config_path.write_text(json.dumps(config, indent=2))

    app = create_app(journal=str(journal_copy))
    app.config["TESTING"] = True
    client = app.test_client()

    resp = _register(client)

    assert resp.status_code == 200
    assert "Location" not in resp.headers
    assert resp.get_json()["key"]


def test_register_non_loopback_mints_nothing(observer_env):
    env = observer_env()

    resp = env.client.post(
        "/app/observer/register",
        json=VALID_REGISTER_PAYLOAD,
        environ_base={"REMOTE_ADDR": "192.168.1.5"},
    )

    assert resp.status_code == 403
    assert resp.get_json()["reason_code"] == "local_request_only"
    _assert_no_observer_records(env.journal)


def test_register_loopback_proxy_header_mints_nothing(observer_env):
    env = observer_env()

    resp = env.client.post(
        "/app/observer/register",
        json=VALID_REGISTER_PAYLOAD,
        headers={"X-Forwarded-For": "1.2.3.4"},
    )

    assert resp.status_code == 403
    assert resp.get_json()["reason_code"] == "local_request_only"
    _assert_no_observer_records(env.journal)


def test_register_authorized_pl_identity_from_non_loopback(observer_env):
    env = observer_env()
    _authorize_pl()

    resp = env.client.post(
        "/app/observer/register",
        json=VALID_REGISTER_PAYLOAD,
        environ_base={"REMOTE_ADDR": "192.168.1.5"},
        environ_overrides={"pl.identity": _pl_identity()},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["name"] == "fedora.tmux"
    records = _observer_records(env.journal)
    assert len(records) == 1
    assert records[0]["stream"] == "fedora.tmux"


def test_register_unknown_pl_identity_from_non_loopback_mints_nothing(observer_env):
    env = observer_env()
    _authorize_pl()

    resp = env.client.post(
        "/app/observer/register",
        json=VALID_REGISTER_PAYLOAD,
        environ_base={"REMOTE_ADDR": "192.168.1.5"},
        environ_overrides={"pl.identity": _pl_identity(PL_FINGERPRINT_2)},
    )

    assert resp.status_code == 403
    assert resp.get_json()["reason_code"] == "local_request_only"
    _assert_no_observer_records(env.journal)


def test_registered_observer_ingest_ignores_conflicting_meta_stream(observer_env):
    env = observer_env()
    register_resp = _register(env.client)
    key = register_resp.get_json()["key"]

    test_data = b"locked stream content"
    resp = env.client.post(
        "/app/observer/ingest",
        headers={"Authorization": f"Bearer {key}"},
        data={
            "day": "20250103",
            "segment": "120000_300",
            "meta": json.dumps({"stream": "wrongstream"}),
            "files": (io.BytesIO(test_data), "tmux.jsonl"),
        },
    )

    assert resp.status_code == 200
    expected_file = _day_dir(env) / "fedora.tmux" / "120000_300" / "tmux.jsonl"
    wrong_file = _day_dir(env) / "wrongstream" / "120000_300" / "tmux.jsonl"
    assert expected_file.exists()
    assert expected_file.read_bytes() == test_data
    assert not wrong_file.exists()

    segments_resp = env.client.get(
        "/app/observer/ingest/segments/20250103",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert segments_resp.status_code == 200
    segments = segments_resp.get_json()
    assert len(segments) == 1
    assert segments[0]["key"] == "120000_300"
    assert segments[0]["files"][0]["status"] == "present"


def test_registered_observer_segments_legacy_record_uses_locked_stream(observer_env):
    env = observer_env()
    register_resp = _register(env.client)
    key = register_resp.get_json()["key"]
    observer = load_observer(key)
    assert observer is not None

    day = "20250103"
    segment = "120000_300"
    content = b"legacy history content"
    segment_dir = _day_dir(env, day) / "fedora.tmux" / segment
    segment_dir.mkdir(parents=True)
    file_path = segment_dir / "legacy.flac"
    file_path.write_bytes(content)
    stat = file_path.stat()

    append_history_record(
        observer["filename_prefix"],
        day,
        {
            "ts": 1704312345000,
            "segment": segment,
            "files": [
                {
                    "submitted": "legacy.flac",
                    "written": "legacy.flac",
                    "inode": stat.st_ino,
                    "size": stat.st_size,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        },
    )

    resp = env.client.get(
        f"/app/observer/ingest/segments/{day}",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data[0]["files"][0]["status"] == "present"
    # This proves the read-path fallback used observer["stream"] ("fedora.tmux");
    # stream_name(observer="fedora.tmux") would look under fedora/ and fall
    # through to the inode-based relocated/missing path, not present.


def test_legacy_key_only_observer_still_honors_meta_stream(observer_env):
    env = observer_env()
    key = "legacy" + ("a" * 58)
    assert save_observer(
        {
            "key": key,
            "name": "legacy-observer",
            "created_at": 1704312345000,
            "last_seen": None,
            "last_segment": None,
            "enabled": True,
            "stats": {
                "segments_received": 0,
                "bytes_received": 0,
            },
        }
    )
    assert load_observer(key) is not None

    test_data = b"legacy meta stream content"
    resp = env.client.post(
        "/app/observer/ingest",
        headers={"Authorization": f"Bearer {key}"},
        data={
            "day": "20250103",
            "segment": "120000_300",
            "meta": json.dumps({"stream": "foo"}),
            "files": (io.BytesIO(test_data), "audio.flac"),
        },
    )

    assert resp.status_code == 200
    expected_file = _day_dir(env) / "foo" / "120000_300" / "audio.flac"
    assert expected_file.exists()
    assert expected_file.read_bytes() == test_data


def test_keyless_manifest_routes_accept_bearer_key(observer_env):
    env = observer_env()
    register_resp = _register(env.client)
    key = register_resp.get_json()["key"]

    ingest_resp = env.client.post(
        "/app/observer/ingest",
        headers={"Authorization": f"Bearer {key}"},
        data={
            "day": "20250103",
            "segment": "120000_300",
            "files": (io.BytesIO(b"manifest content"), "audio.flac"),
        },
    )
    assert ingest_resp.status_code == 200

    day_resp = env.client.get(
        "/app/observer/ingest/manifest/20250103",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert day_resp.status_code == 200
    day_manifest = day_resp.get_json()
    assert day_manifest["day"] == "20250103"
    assert "fedora.tmux/120000_300" in day_manifest["segments"]

    list_resp = env.client.get(
        "/app/observer/ingest/manifest",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert list_resp.status_code == 200
    assert list_resp.get_json() == {"days": {"20250103": {"segments": 1}}}
