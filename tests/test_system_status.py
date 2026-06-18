# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the /api/system/status endpoint."""

import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import solstone.convey.system as system_mod
from solstone.convey import create_app


@pytest.fixture(autouse=True)
def _temp_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    journal = tmp_path
    (journal / "config").mkdir(parents=True, exist_ok=True)
    config = {
        "setup": {"completed_at": 1},
    }
    (journal / "config" / "journal.json").write_text(json.dumps(config))
    app = create_app(str(journal))
    return app.test_client()


class TestSystemStatusEndpoint:
    def test_latest_version_check_targets_journal_release_repo(self, monkeypatch):
        seen: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"tag_name": "v0.6.4"}'

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        assert system_mod._check_latest_version() == {"latest": "0.6.4"}
        assert (
            seen["url"]
            == "https://api.github.com/repos/solpbc/solstone-journal/releases/latest"
        )
        assert seen["timeout"] == 5

    def test_returns_valid_json_shape(self, client):
        with patch.object(
            system_mod,
            "get_capture_health",
            return_value={"status": "no_observers", "observers": []},
        ):
            resp = client.get("/api/system/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "version" in data
        assert "capture" in data
        assert "ok" in data
        assert "current" in data["version"]
        assert "status" in data["capture"]
        assert "observers" in data["capture"]

    def test_no_observers(self, client):
        with patch.object(
            system_mod,
            "get_capture_health",
            return_value={"status": "no_observers", "observers": []},
        ):
            data = client.get("/api/system/status").get_json()
        assert data["capture"]["status"] == "no_observers"
        assert data["capture"]["observers"] == []
        assert data["ok"] is True

    def test_active_observer(self, client):
        with patch.object(
            system_mod,
            "get_capture_health",
            return_value={
                "status": "active",
                "observers": [{"name": "phone", "last_seen": 1000, "status": "active"}],
            },
        ):
            data = client.get("/api/system/status").get_json()
        assert data["capture"]["status"] == "active"
        assert data["ok"] is True

    def test_stale_observer(self, client):
        with patch.object(
            system_mod,
            "get_capture_health",
            return_value={
                "status": "stale",
                "observers": [{"name": "phone", "last_seen": 1000, "status": "stale"}],
            },
        ):
            data = client.get("/api/system/status").get_json()
        assert data["capture"]["status"] == "stale"
        assert data["ok"] is False

    def test_offline_observer(self, client):
        with patch.object(
            system_mod,
            "get_capture_health",
            return_value={
                "status": "offline",
                "observers": [
                    {"name": "phone", "last_seen": 1000, "status": "offline"}
                ],
            },
        ):
            data = client.get("/api/system/status").get_json()
        assert data["capture"]["status"] == "offline"
        assert data["ok"] is False

    def test_revoked_observers_excluded(self, client):
        with patch.object(
            system_mod,
            "get_capture_health",
            return_value={"status": "no_observers", "observers": []},
        ):
            data = client.get("/api/system/status").get_json()
        assert data["capture"]["status"] == "no_observers"

    def test_worst_of_multiple_observers(self, client):
        with patch.object(
            system_mod,
            "get_capture_health",
            return_value={
                "status": "active",
                "observers": [
                    {"name": "phone", "last_seen": 1000, "status": "active"},
                    {"name": "laptop", "last_seen": 500, "status": "stale"},
                ],
            },
        ):
            data = client.get("/api/system/status").get_json()
        # At least one is active, so overall is active
        assert data["capture"]["status"] == "active"

    def test_version_github_failure_graceful(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(system_mod, "_check_latest_version", return_value=None),
        ):
            data = client.get("/api/system/status").get_json()
        assert "current" in data["version"]
        # No "latest" or "update_available" when GitHub fails and no cache
        assert (
            data["version"].get("update_available") is None
            or "latest" not in data["version"]
        )

    def test_version_with_update_available(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(
                system_mod, "_check_latest_version", return_value={"latest": "99.0.0"}
            ),
            patch.object(system_mod, "collect_version", return_value="0.1.0"),
        ):
            data = client.get("/api/system/status").get_json()
        assert data["version"]["current"] == "0.1.0"
        assert data["version"]["latest"] == "99.0.0"
        assert data["version"]["update_available"] is True

    def test_version_update_available_tight(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(
                system_mod, "_check_latest_version", return_value={"latest": "0.4.9"}
            ),
            patch.object(system_mod, "collect_version", return_value="0.4.8"),
        ):
            data = client.get("/api/system/status").get_json()
        assert data["version"]["update_available"] is True

    def test_version_current_newer_than_latest(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(
                system_mod, "_check_latest_version", return_value={"latest": "0.4.8"}
            ),
            patch.object(system_mod, "collect_version", return_value="0.4.9"),
        ):
            data = client.get("/api/system/status").get_json()
        assert data["version"]["update_available"] is False

    def test_version_equal_no_update(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(
                system_mod, "_check_latest_version", return_value={"latest": "0.4.9"}
            ),
            patch.object(system_mod, "collect_version", return_value="0.4.9"),
        ):
            data = client.get("/api/system/status").get_json()
        assert data["version"]["update_available"] is False

    def test_version_unknown_current_no_update(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(
                system_mod, "_check_latest_version", return_value={"latest": "0.4.9"}
            ),
            patch.object(system_mod, "collect_version", return_value=None),
        ):
            data = client.get("/api/system/status").get_json()
        assert "update_available" in data["version"]
        assert data["version"]["update_available"] is False

    def test_version_prerelease_latest_is_newer(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(
                system_mod, "_check_latest_version", return_value={"latest": "0.5.0rc1"}
            ),
            patch.object(system_mod, "collect_version", return_value="0.4.9"),
        ):
            data = client.get("/api/system/status").get_json()
        assert data["version"]["update_available"] is True

    def test_version_dev_current_ahead_of_latest(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(
                system_mod, "_check_latest_version", return_value={"latest": "0.4.8"}
            ),
            patch.object(system_mod, "collect_version", return_value="0.4.9.dev1"),
        ):
            data = client.get("/api/system/status").get_json()
        assert data["version"]["update_available"] is False

    def test_version_unparseable_latest_no_update(self, client):
        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(
                system_mod, "_check_latest_version", return_value={"latest": "nightly"}
            ),
            patch.object(system_mod, "collect_version", return_value="0.4.9"),
        ):
            data = client.get("/api/system/status").get_json()
        assert "update_available" in data["version"]
        assert data["version"]["update_available"] is False

    def test_version_stale_cache_degraded(self, client, tmp_path):
        awareness = tmp_path / "awareness"
        awareness.mkdir(parents=True, exist_ok=True)
        (awareness / "current.json").write_text(
            json.dumps({"version": {"latest": "0.4.8", "checked_at": 0}})
        )

        with (
            patch.object(
                system_mod,
                "get_capture_health",
                return_value={"status": "no_observers", "observers": []},
            ),
            patch.object(system_mod, "_check_latest_version", return_value=None),
            patch.object(system_mod, "collect_version", return_value="0.4.9"),
        ):
            data = client.get("/api/system/status").get_json()
        assert data["version"]["latest"] == "0.4.8"
        assert data["version"]["update_available"] is False

    def test_unknown_status_is_not_ok(self, client):
        with patch.object(
            system_mod,
            "get_capture_health",
            return_value={"status": "unknown", "observers": []},
        ):
            data = client.get("/api/system/status").get_json()
        assert data["capture"]["status"] == "unknown"
        assert data["ok"] is False
