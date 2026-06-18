# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for support app routes."""

import base64
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from solstone.apps.support.call import app as support_cli_app
from solstone.apps.support.diagnostics import collect_recent_errors
from solstone.convey.chat_stream import read_chat_events

DRY_RUN_BANNER = (
    "DRY RUN — nothing was sent. Re-run with --submit to actually file this."
)
STUB_DIAGNOSTICS = {"version": "9.9.9", "revision": "abc1234", "sample": "value"}
_LEAK_NEEDLES = ("private_key", "keypair", "access_token")


def _assert_no_credential_leak(serialized: str) -> None:
    assert not re.search(r"BEGIN.*PRIVATE KEY", serialized)
    for needle in _LEAK_NEEDLES:
        assert needle not in serialized, f"leaked {needle!r} in response body"


def _health_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    health_dir = tmp_path / "health"
    health_dir.mkdir()
    return health_dir


def _write_log(health_dir, name: str, lines: list[str]):
    log_path = health_dir / name
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


@pytest.fixture
def cli(journal, monkeypatch):
    from solstone.think.convey_client import ConveyClient
    from tests._baseline_harness import make_test_client, mark_setup_complete

    mark_setup_complete(journal)
    client = ConveyClient(
        session=make_test_client(journal),
        base_url="",
        require_service=False,
    )
    monkeypatch.setattr("solstone.apps.support.call.get_client", lambda: client)
    return CliRunner()


@pytest.fixture
def support_client():
    """Create a Flask test client with support blueprint."""
    from flask import Flask

    from solstone.apps.support.routes import support_bp

    app = Flask(__name__)
    app.register_blueprint(support_bp)
    yield app.test_client()


class _TicketsClient:
    def __init__(self, tickets=None, error: Exception | None = None):
        self.tickets = tickets or []
        self.error = error

    def list_tickets(self, *, status=None):
        if self.error:
            raise self.error
        return self.tickets


class DownSession:
    def get(self, _url):
        raise requests.exceptions.ConnectionError()

    def post(self, _url, json=None):
        raise requests.exceptions.ConnectionError()


def _enable_support_cli(monkeypatch):
    monkeypatch.setattr("solstone.apps.support.portal.is_enabled", lambda: True)


def _stub_dry_run_context(monkeypatch):
    monkeypatch.setattr(
        "solstone.apps.support.diagnostics.collect_all",
        lambda: dict(STUB_DIAGNOSTICS),
    )
    monkeypatch.setattr(
        "solstone.apps.support.portal._get_portal_url_from_settings",
        lambda: "https://support.example.test",
    )


def _today_support_drafts() -> list[dict]:
    return [
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "support_draft"
    ]


def _patch_flask_upload(monkeypatch):
    from solstone.think.convey_client import (
        MALFORMED_RESPONSE_MESSAGE,
        SERVER_ERROR_MESSAGE,
        ConveyClient,
        ConveyClientError,
    )

    def flask_upload(self, path, *, files, data=None):
        form = dict(data or {})
        handles = []
        try:
            for field, (filename, file_path, content_type) in files.items():
                handle = Path(file_path).open("rb")
                handles.append(handle)
                if content_type is None:
                    form[field] = (handle, filename)
                else:
                    form[field] = (handle, filename, content_type)
            response = self._session.post(
                path,
                data=form,
                content_type="multipart/form-data",
            )
        finally:
            for handle in handles:
                handle.close()

        parsed = response.get_json(silent=True)
        status = response.status_code
        if 200 <= status < 300:
            if parsed is not None:
                return parsed
            raise ConveyClientError(MALFORMED_RESPONSE_MESSAGE, status=status)
        if isinstance(parsed, dict) and ("error" in parsed or "reason_code" in parsed):
            error = parsed.get("error") or parsed.get("reason_code")
            raise ConveyClientError(
                str(error),
                reason_code=parsed.get("reason_code"),
                detail=parsed.get("detail"),
                status=status,
            )
        raise ConveyClientError(SERVER_ERROR_MESSAGE, status=status)

    monkeypatch.setattr(ConveyClient, "upload", flask_upload)


def test_config_route_reports_enabled_and_portal_url(support_client, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.portal.is_enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.apps.support.portal._get_portal_url_from_settings",
        lambda: "https://support.example.test",
    )

    resp = support_client.get("/app/support/api/config")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "enabled": True,
        "portal_url": "https://support.example.test",
    }
    _assert_no_credential_leak(resp.get_data(as_text=True))


def test_portal_url_defaults_to_support_host(journal, monkeypatch):
    from solstone.apps.support import portal

    monkeypatch.delenv(portal.SUPPORT_PORTAL_URL_ENV, raising=False)

    assert portal._get_portal_url_from_settings() == portal.DEFAULT_PORTAL_URL
    assert portal.get_client(anonymous=True).portal_url == portal.DEFAULT_PORTAL_URL


def test_portal_url_env_overrides_configured_host(journal, monkeypatch):
    from solstone.apps.support import portal

    config_dir = journal / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"support": {"portal_url": "https://support.solstone.app"}})
    )
    monkeypatch.setenv(
        portal.SUPPORT_PORTAL_URL_ENV, "https://support-sink.example.test/"
    )

    assert portal._get_portal_url_from_settings() == "https://support-sink.example.test"
    assert portal.get_client(anonymous=True).portal_url == (
        "https://support-sink.example.test"
    )


def test_config_route_ungated_when_disabled(support_client, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.portal.is_enabled", lambda: False)
    monkeypatch.setattr(
        "solstone.apps.support.portal._get_portal_url_from_settings",
        lambda: "https://support.example.test",
    )

    resp = support_client.get("/app/support/api/config")

    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is False


def test_article_route_returns_portal_article(support_client, monkeypatch):
    class ArticleClient:
        def get_article(self, slug):
            return {"slug": slug, "title": "Intro", "body": "hello"}

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client",
        lambda: ArticleClient(),
    )

    resp = support_client.get("/app/support/api/articles/intro")

    assert resp.status_code == 200
    assert resp.get_json() == {"slug": "intro", "title": "Intro", "body": "hello"}
    _assert_no_credential_leak(resp.get_data(as_text=True))


def test_article_route_disabled_returns_403(support_client, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: False)

    resp = support_client.get("/app/support/api/articles/intro")

    assert resp.status_code == 403
    assert resp.get_json()["reason_code"] == "feature_unavailable"


def test_article_route_portal_failure_returns_500(support_client, monkeypatch):
    class ArticleClient:
        def get_article(self, slug):
            raise RuntimeError("boom")

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client",
        lambda: ArticleClient(),
    )

    resp = support_client.get("/app/support/api/articles/intro")

    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload["error"]
    assert payload["detail"]


def test_register_route_returns_handle_only(support_client, monkeypatch):
    class RegisterClient:
        def register(self):
            return {
                "handle": "solstone-foo",
                "access_token": "sk-SECRET",
                "keypair": "kp",
                "private_key": "-----BEGIN PRIVATE KEY-----abc",
            }

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client",
        lambda: RegisterClient(),
    )

    resp = support_client.post("/app/support/api/register")

    assert resp.status_code == 200
    assert resp.get_json() == {"handle": "solstone-foo"}
    _assert_no_credential_leak(resp.get_data(as_text=True))


def test_register_route_disabled_returns_403(support_client, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: False)

    resp = support_client.post("/app/support/api/register")

    assert resp.status_code == 403


def test_register_route_error_does_not_leak_credentials(support_client, monkeypatch):
    class RegisterClient:
        def register(self):
            raise RuntimeError(
                "POST https://support.solstone.app/api/signup — 500: "
                '{"access_token": "sk-LEAKED", '
                '"private_key": "-----BEGIN PRIVATE KEY-----xyz"}'
            )

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client",
        lambda: RegisterClient(),
    )

    resp = support_client.post("/app/support/api/register")
    serialized = resp.get_data(as_text=True)

    assert resp.status_code == 500
    _assert_no_credential_leak(serialized)
    assert "sk-LEAKED" not in serialized
    assert resp.get_json()["detail"] == "Registration with the support portal failed."


def test_badge_count_enabled_empty(support_client, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client", lambda: _TicketsClient()
    )

    resp = support_client.get("/app/support/api/badge-count")

    assert resp.status_code == 200
    assert resp.get_json() == {"count": 0}


def test_badge_count_disabled_returns_403(support_client, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: False)

    resp = support_client.get("/app/support/api/badge-count")

    assert resp.status_code == 403
    payload = resp.get_json()
    assert payload["error"] == "I couldn't use that feature because it isn't enabled."
    assert payload["reason_code"] == "feature_unavailable"
    assert payload["detail"] == "Support is not enabled"


def test_badge_count_error_returns_500(support_client, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client",
        lambda: _TicketsClient(error=RuntimeError("simulated")),
    )

    resp = support_client.get("/app/support/api/badge-count")

    assert resp.status_code == 500
    assert "error" in resp.get_json()


def test_create_ticket_accepts_error_report_contract(support_client, monkeypatch):
    captured: list[dict] = []

    def recorder(**kwargs):
        captured.append(kwargs)
        return {"id": 123, "subject": kwargs["subject"]}

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr("solstone.apps.support.tools.support_create", recorder)

    resp = support_client.post(
        "/app/support/api/tickets",
        json={
            "subject": "I couldn't refresh vitals",
            "description": "owner-visible report body",
            "category": "error_report",
            "severity": "low",
            "anonymous": False,
            "auto_context": True,
            "user_context": {
                "url": "/app/home/",
                "correlation_id": "test-cid",
            },
        },
    )

    assert resp.status_code == 201
    payload = resp.get_json()
    assert isinstance(payload, dict)
    assert payload.get("id") or payload.get("ticket_id")
    assert captured == [
        {
            "subject": "I couldn't refresh vitals",
            "description": "owner-visible report body",
            "product": "solstone",
            "severity": "low",
            "category": "error_report",
            "user_context": {
                "url": "/app/home/",
                "correlation_id": "test-cid",
            },
            "auto_context": True,
            "anonymous": False,
        }
    ]


def test_feedback_anonymous_no_email_kwarg(support_client, monkeypatch):
    captured: list[dict] = []

    def recorder(**kwargs):
        captured.append(kwargs)
        return {"ok": True, "ticket_id": "t1"}

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr("solstone.apps.support.tools.support_feedback", recorder)

    resp = support_client.post(
        "/app/support/api/feedback", json={"body": "hi", "anonymous": True}
    )

    assert resp.status_code == 201
    assert len(captured) == 1
    assert "user_email" not in captured[0]


def test_feedback_identified_forwards_email(support_client, monkeypatch):
    captured: list[dict] = []

    def recorder(**kwargs):
        captured.append(kwargs)
        return {"ok": True, "ticket_id": "t1"}

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr("solstone.apps.support.tools.support_feedback", recorder)

    resp = support_client.post(
        "/app/support/api/feedback",
        json={"body": "hi", "anonymous": False, "user_email": "a@b.com"},
    )

    assert resp.status_code == 201
    assert len(captured) == 1
    assert captured[0]["user_email"] == "a@b.com"


def test_feedback_anonymous_drops_smuggled_email(support_client, monkeypatch):
    captured: list[dict] = []

    def recorder(**kwargs):
        captured.append(kwargs)
        return {"ok": True, "ticket_id": "t1"}

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr("solstone.apps.support.tools.support_feedback", recorder)

    resp = support_client.post(
        "/app/support/api/feedback",
        json={"body": "hi", "anonymous": True, "user_email": "smug@x.com"},
    )

    assert resp.status_code == 201
    assert len(captured) == 1
    assert "user_email" not in captured[0]


def test_cli_dry_run_never_constructs_portal_client(cli, monkeypatch):
    tripped: list[str] = []

    class BlockedPortalClient:
        def __init__(self, *args, **kwargs):
            tripped.append("PortalClient")
            raise AssertionError("PortalClient must not be constructed")

    def blocked_get_client(*args, **kwargs):
        tripped.append("get_client")
        raise AssertionError("get_client must not be called")

    _enable_support_cli(monkeypatch)
    _stub_dry_run_context(monkeypatch)
    monkeypatch.setattr(
        "solstone.apps.support.portal.PortalClient", BlockedPortalClient
    )
    monkeypatch.setattr("solstone.apps.support.portal.get_client", blocked_get_client)

    # Convey localhost only: no external support.solstone.app, no account mint.
    feedback_result = cli.invoke(support_cli_app, ["feedback", "-b", "x"])
    create_result = cli.invoke(support_cli_app, ["create", "-s", "s", "-d", "d"])

    assert feedback_result.exit_code == 0
    assert create_result.exit_code == 0
    assert tripped == []


def test_cli_feedback_dry_run_preview_content(cli, monkeypatch):
    _enable_support_cli(monkeypatch)
    _stub_dry_run_context(monkeypatch)

    result = cli.invoke(support_cli_app, ["feedback", "-b", "owner feedback"])

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == DRY_RUN_BANNER
    assert "Build identity — version:" in result.stdout
    assert "Body:        owner feedback" in result.stdout
    assert "Severity:    low" in result.stdout
    assert "Category:    feedback" in result.stdout


def test_cli_create_dry_run_preview_content(cli, monkeypatch):
    _enable_support_cli(monkeypatch)
    _stub_dry_run_context(monkeypatch)

    result = cli.invoke(support_cli_app, ["create", "-s", "subj", "-d", "desc"])

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == DRY_RUN_BANNER
    assert "Build identity — version:" in result.stdout
    assert "Subject:     subj" in result.stdout


def test_cli_create_dry_run_captures_exact_submit_body(cli, monkeypatch):
    _enable_support_cli(monkeypatch)
    _stub_dry_run_context(monkeypatch)

    result = cli.invoke(
        support_cli_app,
        ["create", "-s", "Subj", "-d", "Desc", "--anonymous"],
    )

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    drafts = _today_support_drafts()
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["verb"] == "create"
    assert draft["diagnostics_snapshot"] == STUB_DIAGNOSTICS
    assert set(draft["payload"]) == {
        "subject",
        "description",
        "product",
        "severity",
        "category",
        "user_context",
        "auto_context",
        "anonymous",
    }
    assert draft["payload"]["subject"] == "Subj"
    assert draft["payload"]["description"] == "Desc"
    assert draft["payload"]["product"] == "solstone"
    assert draft["payload"]["severity"] == "medium"
    assert draft["payload"]["category"] is None
    assert draft["payload"]["user_context"] == STUB_DIAGNOSTICS
    assert draft["payload"]["auto_context"] is False
    assert draft["payload"]["anonymous"] is True


def test_cli_feedback_dry_run_captures_submit_body(cli, monkeypatch):
    _enable_support_cli(monkeypatch)
    _stub_dry_run_context(monkeypatch)

    result = cli.invoke(support_cli_app, ["feedback", "-b", "Nice", "--anonymous"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    drafts = _today_support_drafts()
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["verb"] == "feedback"
    assert draft["payload"] == {
        "body": "Nice",
        "product": "solstone",
        "anonymous": True,
    }
    assert draft["diagnostics_snapshot"] == STUB_DIAGNOSTICS


def test_cli_reply_no_submit_captures_draft_without_submit(cli, monkeypatch):
    _enable_support_cli(monkeypatch)

    result = cli.invoke(
        support_cli_app,
        ["reply", "42", "-b", "more info", "--no-submit"],
    )

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    drafts = _today_support_drafts()
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["verb"] == "reply"
    assert draft["payload"] == {"ticket_id": 42, "content": "more info"}
    assert draft["diagnostics_snapshot"] is None


def test_cli_reply_default_still_submits(cli, monkeypatch):
    replies: list[tuple[int, str]] = []

    class ReplyClient:
        def reply_to_ticket(self, ticket_id: int, content: str) -> dict:
            replies.append((ticket_id, content))
            return {"id": 1}

    _enable_support_cli(monkeypatch)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client",
        lambda: ReplyClient(),
    )

    result = cli.invoke(support_cli_app, ["reply", "42", "-b", "hi", "--yes"])

    assert result.exit_code == 0
    assert "Reply sent to ticket #42." in result.stdout
    assert replies == [(42, "hi")]
    assert _today_support_drafts() == []


def test_cli_attach_no_submit_captures_file_bytes_without_submit(
    cli, monkeypatch, tmp_path
):
    _enable_support_cli(monkeypatch)
    _patch_flask_upload(monkeypatch)
    monkeypatch.setattr(
        "solstone.apps.support.portal.get_client",
        lambda *_args, **_kwargs: pytest.fail("portal client should not be used"),
    )
    monkeypatch.setattr(
        "solstone.apps.support.tools.support_attach",
        lambda *_args, **_kwargs: pytest.fail("support_attach should not be called"),
    )
    file_path = tmp_path / "evidence.txt"
    file_bytes = b"captured at draft time"
    file_path.write_bytes(file_bytes)

    result = cli.invoke(
        support_cli_app,
        ["attach", "42", str(file_path), "--no-submit"],
    )

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert "evidence.txt" in result.stdout
    drafts = _today_support_drafts()
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["verb"] == "attach"
    assert draft["diagnostics_snapshot"] is None
    assert draft["payload"]["ticket_id"] == 42
    assert draft["payload"]["filename"] == "evidence.txt"
    assert draft["payload"]["content_type"] == "text/plain"
    assert draft["payload"]["byte_size"] == len(file_bytes)
    assert draft["payload"]["content_b64"] == base64.b64encode(file_bytes).decode(
        "ascii"
    )


def test_cli_attach_no_submit_requires_one_file(cli, monkeypatch, tmp_path):
    _enable_support_cli(monkeypatch)
    one = tmp_path / "one.txt"
    two = tmp_path / "two.txt"
    one.write_text("one", encoding="utf-8")
    two.write_text("two", encoding="utf-8")

    result = cli.invoke(
        support_cli_app,
        ["attach", "42", str(one), str(two), "--no-submit"],
    )

    assert result.exit_code != 0
    assert "Attach one file at a time" in result.stderr
    assert _today_support_drafts() == []


def test_cli_attach_no_submit_rejects_disallowed_type(cli, monkeypatch, tmp_path):
    _enable_support_cli(monkeypatch)
    _patch_flask_upload(monkeypatch)
    file_path = tmp_path / "payload.exe"
    file_path.write_bytes(b"nope")

    result = cli.invoke(
        support_cli_app,
        ["attach", "42", str(file_path), "--no-submit"],
    )

    assert result.exit_code != 0
    assert "Unsupported file type: .exe" in result.stderr
    assert "Allowed:" in result.stderr
    assert ".txt" in result.stderr
    assert _today_support_drafts() == []


def test_cli_attach_no_submit_rejects_oversize(cli, monkeypatch, tmp_path):
    from solstone.apps.support.portal import PortalClient

    _enable_support_cli(monkeypatch)
    _patch_flask_upload(monkeypatch)
    monkeypatch.setattr(PortalClient, "MAX_ATTACHMENT_SIZE", 3)
    file_path = tmp_path / "large.txt"
    file_path.write_bytes(b"larger")

    result = cli.invoke(
        support_cli_app,
        ["attach", "42", str(file_path), "--no-submit"],
    )

    assert result.exit_code != 0
    assert "File too large" in result.stderr
    assert _today_support_drafts() == []


def test_cli_draft_capture_failure_is_nonfatal_and_visible(cli, monkeypatch):
    from solstone.think.convey_client import ConveyClient, ConveyClientError

    original_request = ConveyClient.request

    def fail_draft(self, method, path, **kwargs):
        if path == "/app/support/api/draft":
            raise ConveyClientError("draft failed")
        return original_request(self, method, path, **kwargs)

    _enable_support_cli(monkeypatch)
    _stub_dry_run_context(monkeypatch)
    monkeypatch.setattr(ConveyClient, "request", fail_draft)

    result = cli.invoke(support_cli_app, ["create", "-s", "Subj", "-d", "Desc"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert "(Draft not captured" in result.stderr


def test_cli_feedback_yes_without_submit_is_still_dry_run(cli, monkeypatch):
    _enable_support_cli(monkeypatch)
    _stub_dry_run_context(monkeypatch)

    result = cli.invoke(support_cli_app, ["feedback", "-b", "x", "-y"])

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == DRY_RUN_BANNER


def test_cli_feedback_submit_calls_tool(cli, monkeypatch):
    captured: list[dict] = []

    def recorder(**kwargs):
        captured.append(kwargs)
        return {"id": 1}

    _enable_support_cli(monkeypatch)
    monkeypatch.setattr("solstone.apps.support.tools.support_feedback", recorder)

    result = cli.invoke(support_cli_app, ["feedback", "-b", "hello", "--submit", "-y"])

    assert result.exit_code == 0
    assert len(captured) == 1
    assert captured[0]["body"] == "hello"


def test_cli_create_submit_calls_tool(cli, monkeypatch):
    captured: list[dict] = []
    diagnostics = {"version": "9.9.9", "revision": "abc1234"}

    def recorder(**kwargs):
        captured.append(kwargs)
        return {"id": 1}

    _enable_support_cli(monkeypatch)
    monkeypatch.setattr(
        "solstone.apps.support.diagnostics.collect_all",
        lambda: diagnostics,
    )
    monkeypatch.setattr("solstone.apps.support.tools.support_create", recorder)

    result = cli.invoke(
        support_cli_app,
        ["create", "-s", "S", "-d", "D", "--skip-kb", "--submit", "-y"],
    )

    assert result.exit_code == 0
    assert len(captured) == 1
    assert captured[0]["subject"] == "S"
    assert captured[0]["description"] == "D"
    assert captured[0]["auto_context"] is False
    assert captured[0]["user_context"] == diagnostics


def test_cli_create_submit_confirm_negative_does_not_call_tool(cli, monkeypatch):
    captured: list[dict] = []

    def recorder(**kwargs):
        captured.append(kwargs)
        return {"id": 1}

    _enable_support_cli(monkeypatch)
    monkeypatch.setattr(
        "solstone.apps.support.diagnostics.collect_all",
        lambda: {"version": "9.9.9", "revision": "abc1234"},
    )
    monkeypatch.setattr("solstone.apps.support.tools.support_create", recorder)

    result = cli.invoke(
        support_cli_app,
        ["create", "-s", "S", "-d", "D", "--skip-kb", "--submit"],
        input="n\n",
    )

    assert result.exit_code == 0
    assert captured == []
    assert "Cancelled — nothing was sent." in result.stdout


def test_search_disabled_prints_byte_locked_message(cli, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.portal.is_enabled", lambda: False)

    result = cli.invoke(support_cli_app, ["search", "foo"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Support agent is disabled in settings.\n"


def test_diagnose_is_ungated_when_disabled(cli, monkeypatch):
    monkeypatch.setattr("solstone.apps.support.portal.is_enabled", lambda: False)

    result = cli.invoke(support_cli_app, ["diagnose"])

    assert result.exit_code == 0
    assert "# Local Diagnostics" in result.stdout
    assert "disabled" not in result.stdout


def test_search_convey_down_prints_notice(monkeypatch):
    from solstone.think.convey_client import ConveyClient

    client = ConveyClient(
        session=DownSession(),
        base_url="http://localhost:5015",
        require_service=False,
    )
    monkeypatch.setattr("solstone.apps.support.call.get_client", lambda: client)

    result = CliRunner().invoke(support_cli_app, ["search", "foo"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        "I couldn't reach support because solstone isn't reachable right now.\n"
        "To file a support ticket, visit https://support.solstone.app\n"
    )


def test_diagnose_convey_down_prints_build_identity_then_notice(monkeypatch):
    from solstone.think.convey_client import ConveyClient

    client = ConveyClient(
        session=DownSession(),
        base_url="http://localhost:5015",
        require_service=False,
    )
    monkeypatch.setattr("solstone.apps.support.call.get_client", lambda: client)

    result = CliRunner().invoke(support_cli_app, ["diagnose"])

    assert result.exit_code == 1
    assert result.stdout.startswith("# Local Diagnostics")
    assert "Version:" in result.stdout
    assert "Platform:" in result.stdout
    assert result.stderr == (
        "I couldn't reach support because solstone isn't reachable right now.\n"
        "To file a support ticket, visit https://support.solstone.app\n"
    )


def test_article_portal_error_is_reason_message(cli, monkeypatch):
    class ArticleClient:
        def get_article(self, slug):
            raise RuntimeError("boom")

    _enable_support_cli(monkeypatch)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client",
        lambda: ArticleClient(),
    )

    result = cli.invoke(support_cli_app, ["article", "intro"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "I couldn't reach support right now.\n"


def test_list_success_renders_rows(cli, monkeypatch):
    class ListClient:
        def list_tickets(self, status=None):
            return [{"id": 7, "status": "open", "subject": "Hi"}]

    _enable_support_cli(monkeypatch)
    monkeypatch.setattr(
        "solstone.apps.support.routes._get_client",
        lambda: ListClient(),
    )

    result = cli.invoke(support_cli_app, ["list"])

    assert result.exit_code == 0
    assert "  #   7  [open        ] Hi" in result.stdout
    assert "1 ticket(s)." in result.stdout


def test_attach_success_and_skip_via_fake_client(monkeypatch, tmp_path):
    from solstone.think.convey_client import ConveyClientError

    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    class FakeClient:
        def __init__(self):
            self.uploaded = []

        def request(self, method, path, **kw):
            assert path == "/app/support/api/config"
            return {"enabled": True, "portal_url": "https://support.example.test"}

        def upload(self, path, *, files, data=None):
            self.uploaded.append((path, files))
            return {"id": len(self.uploaded)}

    fake = FakeClient()
    monkeypatch.setattr("solstone.apps.support.call.get_client", lambda: fake)

    result = CliRunner().invoke(support_cli_app, ["attach", "42", str(a), str(b), "-y"])

    assert result.exit_code == 0
    assert "Attached: a.png (id: 1)" in result.stdout
    assert "Attached: b.png (id: 2)" in result.stdout
    assert len(fake.uploaded) == 2
    assert fake.uploaded[0][0] == "/app/support/api/tickets/42/attachments"
    assert fake.uploaded[0][1]["file"] == ("a.png", str(a), None)
    assert fake.uploaded[1][0] == "/app/support/api/tickets/42/attachments"
    assert fake.uploaded[1][1]["file"] == ("b.png", str(b), None)

    class SkippingFakeClient(FakeClient):
        def upload(self, path, *, files, data=None):
            self.uploaded.append((path, files))
            if len(self.uploaded) == 1:
                raise ConveyClientError("I couldn't use one of those values.")
            return {"id": len(self.uploaded)}

    skipping_fake = SkippingFakeClient()
    monkeypatch.setattr("solstone.apps.support.call.get_client", lambda: skipping_fake)

    skipped = CliRunner().invoke(
        support_cli_app, ["attach", "42", str(a), str(b), "-y"]
    )

    assert skipped.exit_code == 0
    assert "Attached: b.png (id: 2)" in skipped.stdout
    assert skipped.stderr == "Skipped a.png: I couldn't use one of those values.\n"
    assert len(skipping_fake.uploaded) == 2


def test_feedback_identified_empty_email_omits_kwarg(support_client, monkeypatch):
    captured: list[dict] = []

    def recorder(**kwargs):
        captured.append(kwargs)
        return {"ok": True, "ticket_id": "t1"}

    monkeypatch.setattr("solstone.apps.support.routes._enabled", lambda: True)
    monkeypatch.setattr("solstone.apps.support.tools.support_feedback", recorder)

    resp = support_client.post(
        "/app/support/api/feedback",
        json={"body": "hi", "anonymous": False, "user_email": "   "},
    )

    assert resp.status_code == 201
    assert len(captured) == 1
    assert "user_email" not in captured[0]


def test_revision_hash_when_git_available(monkeypatch):
    import subprocess

    from solstone.apps.support import diagnostics

    class _CP:
        returncode = 0
        stdout = "abc1234\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP())
    assert diagnostics.collect_revision() == "abc1234"


def test_revision_none_when_not_a_repo(monkeypatch):
    import subprocess

    from solstone.apps.support import diagnostics

    class _CP:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP())
    assert diagnostics.collect_revision() is None


def test_revision_none_when_git_raises(monkeypatch):
    import subprocess

    from solstone.apps.support import diagnostics

    def _boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert diagnostics.collect_revision() is None


def test_collect_all_includes_revision(monkeypatch):
    from solstone.apps.support import diagnostics

    monkeypatch.setattr(diagnostics, "collect_revision", lambda: "deadbee")
    bundle = diagnostics.collect_all()
    assert bundle["revision"] == "deadbee"
    assert "version" in bundle


def test_proactive_support_suppresses_readiness_blockers(monkeypatch):
    from solstone.apps.events import EventContext
    from solstone.apps.support import events

    captured: list[tuple[tuple, dict]] = []
    events._error_counts.clear()
    monkeypatch.setattr(events, "_is_proactive_enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.think.callosum.callosum_send",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    for reason_code in (
        "provider_key_missing",
        "provider_key_missing",
        "local_server_unhealthy",
    ):
        events.detect_repeated_errors(
            EventContext(
                msg={"service": "cortex", "reason_code": reason_code},
                app="support",
                tract="cortex",
                event="error",
            )
        )

    assert captured == []
    assert "cortex" not in events._error_counts


def test_proactive_support_still_emits_for_generic_errors(monkeypatch):
    from solstone.apps.events import EventContext
    from solstone.apps.support import events

    captured: list[tuple[tuple, dict]] = []
    events._error_counts.clear()
    monkeypatch.setattr(events, "_is_proactive_enabled", lambda: True)
    monkeypatch.setattr(
        "solstone.think.callosum.callosum_send",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    for _ in range(3):
        events.detect_repeated_errors(
            EventContext(
                msg={"service": "cortex", "reason_code": "chat_timeout"},
                app="support",
                tract="cortex",
                event="error",
            )
        )

    assert len(captured) == 1
    args, kwargs = captured[0]
    assert args == ("support", "proactive_suggestion")
    assert kwargs["service"] == "cortex"
    assert kwargs["count"] == 3


def test_collect_provider_readiness_is_redacted(monkeypatch):
    from solstone.apps.support import diagnostics

    snapshot = {
        "summary": {
            "status": "blocked",
            "severity": "blocker",
            "active_groups": 1,
            "blocked_count": 1,
        },
        "interfaces": {
            "generate": {
                "provider": "anthropic",
                "model": "/home/jer/private/model",
                "reason_code": "provider_key_missing",
                "status": "blocked",
                "severity": "blocker",
                "summary": "Anthropic needs credentials before it can read your screen descriptions",
                "operator_detail": (
                    "reason_code=provider_key_missing; provider=anthropic; "
                    "reset_at_ms=123; message=Traceback (most recent call last): "
                    "/home/jer/.config ANTHROPIC_API_KEY=sk-testsecret"
                ),
            }
        },
        "groups": [
            {
                "provider": "anthropic",
                "model": "claude-test",
                "reason_code": "provider_key_missing",
                "status": "blocked",
                "severity": "blocker",
                "summary": "Anthropic needs credentials before it can read your screen descriptions",
                "operator_detail": "reason_code=provider_key_missing; provider=anthropic",
            }
        ],
    }
    monkeypatch.setattr(
        "solstone.convey.readiness_snapshot.build_readiness_snapshot",
        lambda: snapshot,
    )

    payload = diagnostics.collect_provider_readiness()
    serialized = json.dumps(payload)

    assert payload["interfaces"]["generate"]["provider"] == "anthropic"
    assert payload["interfaces"]["generate"]["reason_code"] == "provider_key_missing"
    assert payload["interfaces"]["generate"]["status"] == "blocked"
    assert payload["interfaces"]["generate"]["reset_at_ms"] == 123
    assert "ANTHROPIC_API_KEY" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "sk-testsecret" not in serialized
    assert "/home/jer" not in serialized
    assert "Traceback" not in serialized


def test_collect_all_includes_provider_readiness(monkeypatch):
    from solstone.apps.support import diagnostics

    monkeypatch.setattr(
        diagnostics,
        "collect_provider_readiness",
        lambda: {"interfaces": {"generate": {"provider": "anthropic"}}},
    )

    assert diagnostics.collect_all()["provider_readiness"] == {
        "interfaces": {"generate": {"provider": "anthropic"}}
    }


def test_recent_beats_stale_under_limit(tmp_path, monkeypatch):
    health_dir = _health_dir(tmp_path, monkeypatch)
    stale = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    recent = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")

    _write_log(
        health_dir,
        "old.log",
        [f"{stale} [old:stderr] ERROR:root:stale-{i}" for i in range(12)],
    )
    _write_log(
        health_dir,
        "new.log",
        [f"{recent} [new:stderr] ERROR:root:recent-boom"],
    )

    result = collect_recent_errors()

    assert len(result) <= 10
    assert result[0]["message"] == "[new:stderr] ERROR:root:recent-boom"
    assert any("recent-boom" in entry["message"] for entry in result)
    assert all("stale-" not in entry["message"] for entry in result)
    assert [entry["time"] for entry in result] == sorted(
        entry["time"] for entry in result
    )[::-1]


def test_unparseable_line_inherits_preceding_timestamp(tmp_path, monkeypatch):
    health_dir = _health_dir(tmp_path, monkeypatch)
    line_dt = datetime.now() - timedelta(hours=2)
    mtime_dt = datetime.now() - timedelta(hours=1)
    line_ts = line_dt.isoformat(timespec="seconds")

    log_path = _write_log(
        health_dir,
        "mixed.log",
        [
            f"{line_ts} [mixed:stderr] ERROR:root:line-timestamp",
            "ERROR something with no timestamp",
        ],
    )
    # mtime is more recent than the parsed line; carry-forward must win over it.
    os.utime(log_path, (mtime_dt.timestamp(), mtime_dt.timestamp()))

    result = collect_recent_errors()
    line_entry = next(e for e in result if "line-timestamp" in e["message"])
    carried_entry = next(e for e in result if "no timestamp" in e["message"])

    assert line_entry["time"] == line_ts
    assert line_entry["time_approximate"] is False
    # Inherits the preceding parsed timestamp, NOT the file mtime.
    assert carried_entry["time"] == line_ts
    assert carried_entry["time_approximate"] is True


def test_old_anchor_excludes_carried_unparseable(tmp_path, monkeypatch):
    health_dir = _health_dir(tmp_path, monkeypatch)
    stale_dt = datetime.now() - timedelta(days=30)
    stale_ts = stale_dt.isoformat(timespec="seconds")

    log_path = _write_log(
        health_dir,
        "stale_carry.log",
        [
            f"{stale_ts} [carry:stderr] ERROR:root:old-anchor",
            "ERROR continuation with no timestamp",
        ],
    )
    # A recent mtime would, under the old bug, pull the unparseable line in.
    now_ts = datetime.now().timestamp()
    os.utime(log_path, (now_ts, now_ts))

    # Anchor is outside the window; the unparseable line inherits it -> both excluded.
    assert collect_recent_errors() == []


def test_unparseable_first_line_uses_mtime(tmp_path, monkeypatch):
    health_dir = _health_dir(tmp_path, monkeypatch)
    mtime_dt = datetime.now() - timedelta(hours=1)
    mtime_ts = mtime_dt.isoformat(timespec="seconds")

    log_path = _write_log(
        health_dir,
        "headless.log",
        ["ERROR boom with no leading timestamp"],
    )
    os.utime(log_path, (mtime_dt.timestamp(), mtime_dt.timestamp()))

    result = collect_recent_errors()
    entry = next(e for e in result if "boom" in e["message"])
    assert entry["time"] == mtime_ts
    assert entry["time_approximate"] is True


def test_window_excludes_old_and_cli_empty_state(cli, tmp_path, monkeypatch):
    health_dir = _health_dir(tmp_path, monkeypatch)
    stale = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    _write_log(
        health_dir,
        "stale.log",
        [f"{stale} [stale:stderr] ERROR:root:too-old"],
    )

    assert collect_recent_errors() == []

    result = cli.invoke(support_cli_app, ["diagnose"])

    assert result.exit_code == 0
    assert "No recent errors." in result.stdout


def test_unreadable_log_degrades_gracefully(tmp_path, monkeypatch):
    health_dir = _health_dir(tmp_path, monkeypatch)
    (health_dir / "bad.log").mkdir()
    recent = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    _write_log(
        health_dir,
        "good.log",
        [f"{recent} [good:stderr] ERROR:root:survived"],
    )

    result = collect_recent_errors()

    assert any("survived" in entry["message"] for entry in result)


def test_cli_count_matches_printed_rows(cli, tmp_path, monkeypatch):
    health_dir = _health_dir(tmp_path, monkeypatch)
    now = datetime.now()
    count = 3
    lines = [
        (
            f"{(now - timedelta(minutes=i + 1)).isoformat(timespec='seconds')} "
            f"[count:stderr] ERROR:root:count-{i}"
        )
        for i in range(count)
    ]
    _write_log(health_dir, "count.log", lines)

    result = cli.invoke(support_cli_app, ["diagnose"])

    assert result.exit_code == 0
    assert f"Recent errors ({count}):" in result.stdout
    rows = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("  ") and "[count]" in line and "ERROR:root:count-" in line
    ]
    assert len(rows) == count
