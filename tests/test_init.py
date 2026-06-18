# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from solstone.apps.observer.routes import ACTIVE_THRESHOLD_MS, STALE_THRESHOLD_MS
from solstone.apps.observer.utils import save_observer
from solstone.convey import create_app
from solstone.think.utils import get_journal, now_ms


def _read_config(journal_dir):
    return json.loads((journal_dir / "config" / "journal.json").read_text())


def _make_empty_client(tmp_path, monkeypatch, *, timezone="America/Denver"):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(
        "solstone.think.utils._resolve_os_identity", lambda: ("OS User", "osuser")
    )
    monkeypatch.setattr("solstone.think.utils._resolve_os_timezone", lambda: timezone)
    app = create_app(str(journal))
    app.config["TESTING"] = True
    return app.test_client(), journal


def _clear_setup(journal_dir):
    config = _read_config(journal_dir)
    config.pop("setup", None)
    (journal_dir / "config" / "journal.json").write_text(json.dumps(config, indent=2))


def _save_test_observer(
    key_prefix: str,
    name: str,
    *,
    created_at: int,
    last_seen: int | None,
    revoked: bool = False,
):
    key = key_prefix + ("f" * 56)
    assert save_observer(
        {
            "key": key,
            "name": name,
            "created_at": created_at,
            "last_seen": last_seen,
            "last_segment": None,
            "enabled": True,
            "revoked": revoked,
            "revoked_at": created_at + 1 if revoked else None,
            "stats": {},
        }
    )
    return key


@pytest.fixture
def fresh_client(journal_copy):
    _clear_setup(journal_copy)
    app = create_app(str(journal_copy))
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def configured_client(journal_copy):
    app = create_app(str(journal_copy))
    app.config["TESTING"] = True
    return app.test_client()


class TestInitDetection:
    def test_redirects_to_init_when_setup_incomplete(self, fresh_client):
        resp = fresh_client.get("/")
        assert resp.status_code == 302
        assert "/init" in resp.headers["Location"]

    def test_setup_complete_serves_root(self, configured_client):
        resp = configured_client.get("/")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/app/home/")

    def test_init_page_renders(self, fresh_client):
        resp = fresh_client.get("/init")
        assert resp.status_code == 200
        assert b"set up solstone" in resp.data
        assert b'value="Test User"' in resp.data
        assert b'value="Tester"' in resp.data
        assert b'id="section-password"' not in resp.data
        assert b'id="password"' not in resp.data

    def test_init_title_is_welcome_setup(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b"<title>solstone welcome setup</title>" in resp.data

    def test_init_renders_version(self, fresh_client):
        try:
            from importlib.metadata import version as _v

            expected = _v("solstone")
        except Exception:
            expected = "dev"

        resp = fresh_client.get("/init")
        assert (
            f"journal version {expected}".encode() in resp.data
            or b"journal version dev" in resp.data
        )

    def test_init_renders_journal_path_in_welcome(self, fresh_client):
        journal_path = str(Path(get_journal()))

        resp = fresh_client.get("/init")

        assert f"<code>{journal_path}</code>".encode() in resp.data
        assert b"solstone is three things working together" not in resp.data
        assert b"solstone is two things working together" not in resp.data
        assert b"solstone runs on your device." in resp.data
        assert (
            b"your observers, your journal, and sol \xe2\x80\x94 all right here"
            in resp.data
        )

    def test_init_sol_agent_section_renders(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b">how should sol think?<" in resp.data
        assert b"become a solstone scout" in resp.data

    def test_init_sol_agent_paragraphs(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b"Claude, Gemini, or GPT" in resp.data
        assert b"init captures your choice" in resp.data

    def test_init_no_legacy_trust_note(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b"your key is stored locally" not in resp.data

    def test_init_gemini_label_canonical_case(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b"Gemini API key" in resp.data
        assert b">gemini api key<" not in resp.data

    def test_machine_card_present_and_verbatim(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b"solstone runs on your device." in resp.data
        assert (
            b"your observers, your journal, and sol "
            b"\xe2\x80\x94 all right here. nothing else is required to start."
        ) in resp.data
        assert b"sol pbc offers a few optional services" not in resp.data
        assert (
            b"turn them on if they help. turn them off whenever you want. or never."
            not in resp.data
        )
        assert (
            b"observers \xe2\x80\x94 experience your day along with you"
            not in resp.data
        )
        assert b"where memories are stored and curated by sol" not in resp.data

    def test_machine_card_section_id(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b'id="section-machine-card"' in resp.data
        assert b'id="section-trinity"' not in resp.data

    def test_identity_hint_refresh(self, fresh_client):
        resp = fresh_client.get("/init")
        assert (
            b"optional \xe2\x80\x94 just helps sol know what to call you." in resp.data
        )
        assert (
            b"optional \xe2\x80\x94 helps sol address you correctly." not in resp.data
        )

    def test_footer_note_refresh(self, fresh_client):
        resp = fresh_client.get("/init")
        assert (
            b"your journal stays on your device. solstone runs right here "
            b"\xe2\x80\x94 nothing leaves unless you send it."
        ) in resp.data
        assert b"your data stays on your machine" not in resp.data

    def test_no_lowercase_gemini_in_body_copy(self, fresh_client):
        resp = fresh_client.get("/init")
        allowed_contexts = (
            "gemini-key",
            "gemini-validate",
            "geminiKey",
            "gemini_key",
            "gemini.google.com",
            "gemini-api/terms",
            "a gemini key is already on this machine",
        )
        # DOM identifiers, JS selectors, JSON keys, and literal domains stay lowercase.
        body_copy = "\n".join(
            line
            for line in resp.data.decode().splitlines()
            if not any(context in line for context in allowed_contexts)
        )
        assert re.search(r"\bgemini\b", body_copy) is None

    def test_no_banned_terms_or_surveillance_verbs(self, fresh_client):
        resp = fresh_client.get("/init")
        for phrase in (
            b"your account",
            b"sign up for",
            b"log in to",
            b"create an account",
            b"account.solstone.app",
        ):
            assert phrase not in resp.data
        text = resp.data.decode()
        assert (
            re.search(
                r"\bwatch\b|\bcapture\b|\bmonitor\b|\btrack\b|\bcollect\b|\brecord\b",
                text,
                re.I,
            )
            is None
        )

    def test_portal_unreachable_stub_inert_on_default_path(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b"portal-unreachable" not in resp.data
        assert b"can't reach sol pbc right now." not in resp.data
        assert b"L11-stub: portal-unreachable" not in resp.data

    def test_init_validate_button_present(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b'id="gemini-validate"' in resp.data

    def test_init_retention_radios_present(self, fresh_client):
        resp = fresh_client.get("/init")
        assert resp.data.count(b'<input type="radio" name="retention_mode"') == 3
        assert b'name="retention_mode" value="keep" checked' in resp.data
        assert b'name="retention_mode" value="days"' in resp.data
        assert b'name="retention_mode" value="processed"' in resp.data

    def test_init_retention_reflects_persisted_state(self, journal_copy):
        config = _read_config(journal_copy)
        config.pop("setup", None)
        config["retention"] = {"raw_media": "days", "raw_media_days": 14}
        (journal_copy / "config" / "journal.json").write_text(
            json.dumps(config, indent=2)
        )
        app = create_app(str(journal_copy))
        app.config["TESTING"] = True

        resp = app.test_client().get("/init")

        assert b'name="retention_mode" value="days" checked' in resp.data
        assert b'id="retention-days-input" min="1" value="14"' in resp.data

    def test_init_observed_media_copy_updated(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b"so you can access it again later" in resp.data
        assert b"re-derive insights" not in resp.data
        assert b"we recommend leaving this on" not in resp.data

    def test_init_observers_section_removed(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b'id="section-observers"' not in resp.data

    def test_init_get_started_section_removed(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b'id="section-finalize"' not in resp.data

    def test_init_finalize_button_text(self, fresh_client):
        resp = fresh_client.get("/init")
        assert b"finish welcome setup" in resp.data
        assert b'type="submit"' in resp.data
        body = resp.data.decode()
        form_start = body.index("<form ")
        button = body.index("finish welcome setup")
        form_end = body.index("</form>")
        assert form_start < button < form_end

    def test_init_redirects_when_configured(self, configured_client):
        resp = configured_client.get("/init")
        assert resp.status_code == 302

    def test_init_empty_journal_materializes_config(self, tmp_path, monkeypatch):
        client, journal = _make_empty_client(tmp_path, monkeypatch)

        resp = client.get("/init")

        assert resp.status_code == 200
        config = _read_config(journal)
        assert config["identity"]["name"] == "OS User"
        assert config["identity"]["preferred"] == "osuser"
        assert config["identity"]["timezone"] == "America/Denver"
        assert "convey" not in config
        assert b'value="OS User"' in resp.data
        assert b'value="osuser"' in resp.data

    def test_init_escapes_identity_values(self, journal_copy):
        config = _read_config(journal_copy)
        config.pop("setup", None)
        config["identity"]["name"] = "<script>alert(1)</script>"
        (journal_copy / "config" / "journal.json").write_text(
            json.dumps(config, indent=2)
        )
        app = create_app(str(journal_copy))
        app.config["TESTING"] = True

        resp = app.test_client().get("/init")

        assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in resp.data
        assert b"<script>alert(1)</script>" not in resp.data

    def test_init_does_not_overwrite_existing_identity(self, journal_copy):
        config = _read_config(journal_copy)
        config.pop("setup", None)
        config["identity"]["name"] = "Existing User"
        config["identity"]["preferred"] = "Existing"
        config["identity"]["timezone"] = "UTC"
        (journal_copy / "config" / "journal.json").write_text(
            json.dumps(config, indent=2)
        )
        before = _read_config(journal_copy)
        app = create_app(str(journal_copy))
        app.config["TESTING"] = True

        resp = app.test_client().get("/init")
        after = _read_config(journal_copy)

        assert resp.status_code == 200
        assert after == before

    def test_init_get_does_not_overwrite_corrupt_config(self, journal_copy):
        app = create_app(str(journal_copy))
        app.config["TESTING"] = True
        client = app.test_client()
        config_path = journal_copy / "config" / "journal.json"
        config_path.write_bytes(b"{ invalid json }")
        before = config_path.read_bytes()

        with pytest.raises(json.JSONDecodeError):
            client.get("/init")

        assert config_path.read_bytes() == before


class TestInitValidateProvider:
    """Tests for the validate-only provider endpoint."""

    def test_validate_provider_valid_key(self, fresh_client, monkeypatch):
        monkeypatch.setattr(
            "solstone.think.providers.validate_key",
            lambda provider, key: {"valid": True},
        )
        resp = fresh_client.post(
            "/init/validate-provider",
            json={"key": "test-api-key-123"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is True

    def test_validate_provider_invalid_key(self, fresh_client, monkeypatch):
        monkeypatch.setattr(
            "solstone.think.providers.validate_key",
            lambda provider, key: {"valid": False, "error": "Invalid key"},
        )
        resp = fresh_client.post(
            "/init/validate-provider",
            json={"key": "bad-key"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        assert data["error"] == "Invalid key"

    def test_validate_provider_no_config_write(
        self, fresh_client, journal_copy, monkeypatch
    ):
        """Validate endpoint must not write to config."""
        monkeypatch.setattr(
            "solstone.think.providers.validate_key",
            lambda provider, key: {"valid": True},
        )
        config_before = _read_config(journal_copy)
        fresh_client.post(
            "/init/validate-provider",
            json={"key": "test-api-key-123"},
            content_type="application/json",
        )
        config_after = _read_config(journal_copy)
        assert config_before == config_after


class TestInitObservers:
    """Tests for the observer list endpoint during onboarding."""

    def test_init_observers_returns_thresholds_and_observers_dict(
        self, fresh_client, monkeypatch
    ):
        monkeypatch.setattr(
            "solstone.apps.observer.utils.list_observers",
            lambda: [],
        )
        resp = fresh_client.get("/init/observers")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {
            "thresholds": {
                "active_ms": ACTIVE_THRESHOLD_MS,
                "stale_ms": STALE_THRESHOLD_MS,
            },
            "observers": [],
        }
        assert isinstance(data["thresholds"]["active_ms"], int)
        assert isinstance(data["thresholds"]["stale_ms"], int)

    def test_observers_available_before_setup(self, fresh_client, monkeypatch):
        """Observers endpoint works before setup completes."""
        monkeypatch.setattr(
            "solstone.apps.observer.utils.list_observers",
            lambda: [],
        )
        resp = fresh_client.get("/init/observers")
        assert resp.status_code == 200

    def test_observers_returns_list(self, fresh_client, monkeypatch):
        monkeypatch.setattr(
            "solstone.apps.observer.utils.list_observers",
            lambda: [
                {
                    "key": "abcd1234xxxx",
                    "name": "my-phone",
                    "created_at": 100,
                    "last_seen": None,
                    "last_segment": None,
                    "enabled": True,
                    "revoked": False,
                    "revoked_at": None,
                    "stats": {},
                },
                {
                    "key": "revoked1xxxx",
                    "name": "old-device",
                    "created_at": 50,
                    "last_seen": None,
                    "last_segment": None,
                    "enabled": False,
                    "revoked": True,
                    "revoked_at": 90,
                    "stats": {},
                },
            ],
        )
        resp = fresh_client.get("/init/observers")
        assert resp.status_code == 200
        data = resp.get_json()
        observers = data["observers"]
        assert len(observers) == 1
        assert observers[0]["name"] == "my-phone"
        assert observers[0]["prefix"] == "abcd1234"
        assert observers[0]["state"] == "disconnected"
        assert observers[0]["group"] == "inactive"
        assert observers[0]["label"] == "Disconnected"
        assert observers[0]["elapsed_ms"] is None
        assert observers[0]["clock_skew"] is False

    def test_init_observers_endpoint_parity(self, fresh_client, journal_copy):
        current_now = now_ms()
        _save_test_observer(
            "aaaa0000",
            "active-observer",
            created_at=10,
            last_seen=current_now - 5_000,
        )
        _save_test_observer(
            "bbbb0000",
            "stale-observer",
            created_at=20,
            last_seen=current_now - 60_000,
        )
        _save_test_observer(
            "cccc0000",
            "disconnected-observer",
            created_at=30,
            last_seen=current_now - 600_000,
        )
        config = _read_config(journal_copy)
        config["setup"] = {"completed_at": current_now}
        (journal_copy / "config" / "journal.json").write_text(
            json.dumps(config, indent=2),
            encoding="utf-8",
        )

        api_resp = fresh_client.get("/app/observer/api/list")
        init_resp = fresh_client.get("/init/observers")
        assert api_resp.status_code == 200
        assert init_resp.status_code == 200

        api_by_key = {
            observer["prefix"]: observer
            for observer in api_resp.get_json()["observers"]
            if not observer["revoked"]
        }
        init_by_key = {
            observer["prefix"]: observer
            for observer in init_resp.get_json()["observers"]
        }

        assert set(init_by_key) == set(api_by_key)
        for key_prefix, init_observer in init_by_key.items():
            api_observer = api_by_key[key_prefix]
            assert init_observer["state"] == api_observer["state"]
            assert init_observer["group"] == api_observer["group"]
            assert init_observer["label"] == api_observer["label"]
            assert init_observer["clock_skew"] == api_observer["clock_skew"]
            assert abs(init_observer["elapsed_ms"] - api_observer["elapsed_ms"]) < 200


class TestInitFinalize:
    """Tests for the atomic finalize endpoint."""

    def test_finalize_saves_all_config(self, fresh_client, journal_copy):
        resp = fresh_client.post(
            "/init/finalize",
            json={
                "name": "Jane Doe",
                "preferred": "Jane",
                "timezone": "America/Denver",
                "gemini_key": "test-api-key-123",
            },
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["redirect"] == "/app/thinking/"

        config = _read_config(journal_copy)
        assert "allow_network_access" not in config["convey"]
        # Identity
        assert config["identity"]["name"] == "Jane Doe"
        assert config["identity"]["preferred"] == "Jane"
        assert config["identity"]["timezone"] == "America/Denver"
        # Provider
        assert config["env"]["GOOGLE_API_KEY"] == "test-api-key-123"
        # Setup
        assert "completed_at" in config["setup"]

    def test_finalize_succeeds(self, fresh_client, journal_copy):
        resp = fresh_client.post(
            "/init/finalize",
            json={"name": "Jane"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["redirect"] == "/app/thinking/"
        config = _read_config(journal_copy)
        assert "completed_at" in config["setup"]
        assert "allow_network_access" not in config["convey"]

    def test_finalize_minimal(self, fresh_client, journal_copy):
        """Finalize with optional fields omitted."""
        resp = fresh_client.post(
            "/init/finalize",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 200
        config = _read_config(journal_copy)
        assert "completed_at" in config["setup"]
        # No gemini key written
        assert "GOOGLE_API_KEY" not in config.get("env", {})

    def test_finalize_form_timezone_overrides_os_default(self, tmp_path, monkeypatch):
        client, journal = _make_empty_client(
            tmp_path, monkeypatch, timezone="America/Denver"
        )
        client.get("/init")

        resp = client.post(
            "/init/finalize",
            json={
                "name": "Form User",
                "preferred": "Form",
                "timezone": "America/New_York",
            },
            content_type="application/json",
        )

        assert resp.status_code == 200
        config = _read_config(journal)
        assert config["identity"]["name"] == "Form User"
        assert config["identity"]["preferred"] == "Form"
        assert config["identity"]["timezone"] == "America/New_York"
        assert "completed_at" in config["setup"]

    def test_finalize_without_timezone_preserves_os_default(
        self, tmp_path, monkeypatch
    ):
        client, journal = _make_empty_client(
            tmp_path, monkeypatch, timezone="America/Denver"
        )
        client.get("/init")

        resp = client.post(
            "/init/finalize",
            json={"name": "Form User", "preferred": "Form"},
            content_type="application/json",
        )

        assert resp.status_code == 200
        config = _read_config(journal)
        assert config["identity"]["name"] == "Form User"
        assert config["identity"]["preferred"] == "Form"
        assert config["identity"]["timezone"] == "America/Denver"
        assert "completed_at" in config["setup"]

    def test_finalize_completes_setup_access(self, fresh_client, journal_copy):
        response = fresh_client.post(
            "/init/finalize",
            json={},
            content_type="application/json",
        )
        assert response.get_json()["redirect"] == "/app/thinking/"
        resp = fresh_client.get("/")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/app/home/")

    def test_finalize_no_early_config_write(self, fresh_client, journal_copy):
        """Before finalize, config should have no setup."""
        config = _read_config(journal_copy)
        assert "setup" not in config or "completed_at" not in config.get("setup", {})

    def test_post_init_redirect(self, fresh_client, journal_copy):
        """After finalize, /init redirects away."""
        fresh_client.post(
            "/init/finalize",
            json={},
            content_type="application/json",
        )
        resp = fresh_client.get("/init")
        assert resp.status_code == 302

    def test_finalize_with_retention_config(self, fresh_client, journal_copy):
        """Finalize with explicit retention config writes correct values."""
        resp = fresh_client.post(
            "/init/finalize",
            json={
                "retention_mode": "processed",
                "retention_days": 30,
            },
            content_type="application/json",
        )
        assert resp.status_code == 200
        config = _read_config(journal_copy)
        assert config["retention"]["raw_media"] == "processed"
        assert config["retention"]["raw_media_days"] is None

    def test_finalize_default_retention(self, fresh_client, journal_copy):
        """Finalize without retention fields writes default (keep/null)."""
        resp = fresh_client.post(
            "/init/finalize",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 200
        config = _read_config(journal_copy)
        assert config["retention"]["raw_media"] == "keep"
        assert config["retention"]["raw_media_days"] is None

    def test_finalize_corrupt_config_returns_reason_without_writing(
        self, fresh_client, journal_copy
    ):
        config_path = journal_copy / "config" / "journal.json"
        config_path.write_bytes(b"{ invalid json }")
        before = config_path.read_bytes()

        with patch("solstone.convey.root.write_journal_config") as write_config:
            resp = fresh_client.post(
                "/init/finalize",
                json={"name": "Jane"},
                content_type="application/json",
            )

        assert resp.status_code == 500
        assert resp.get_json()["reason_code"] == "corrupt_config"
        write_config.assert_not_called()
        assert config_path.read_bytes() == before
