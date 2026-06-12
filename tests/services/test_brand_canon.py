# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
import re
import ssl
import urllib.error

import pytest

from solstone.think.journal_config import write_journal_config
from solstone.think.services import cli, outcomes, portal_client, status

BLOCKED_COPY_RE = re.compile(
    r"sign(?:ed)?\s+in|signing\s+in|log(?:ged)?\s+in|your\s+account|"
    r"account\s+settings|linked|authenticate",
    re.IGNORECASE,
)


def test_services_cli_copy_avoids_blocked_brand_terms() -> None:
    strings = [
        cli.STDOUT_LINK_TEMPLATE,
        cli.STDOUT_OPENED_BROWSER,
        cli.STDOUT_WAITING,
        cli.STDOUT_SUCCESS,
        cli.STDOUT_PENDING,
        cli.STDOUT_REVOKED,
        cli.STDOUT_REVOKED_PRESERVED_MANUAL_KEY,
        cli.STDOUT_REFRESH,
        cli.STDOUT_SPL_SUCCESS,
        cli.STDOUT_SPL_DISABLE_SUCCESS,
        *cli.ERROR_MESSAGES.values(),
    ]

    assert all(not BLOCKED_COPY_RE.search(value) for value in strings)


def test_new_switchboard_guidance_avoids_blocked_brand_terms() -> None:
    strings = [
        *(value or "" for value in outcomes.GUIDANCE.values()),
        status.SCOUT_MANUAL_KEY_GUIDANCE,
        status.SCOUT_PENDING_GUIDANCE,
        status.SCOUT_DISABLED_GUIDANCE,
        status.SPL_NOT_ENABLED_GUIDANCE,
        status.SPL_INCONSISTENT_GUIDANCE,
    ]

    assert all(not BLOCKED_COPY_RE.search(value) for value in strings)
    assert all("sol private link" not in value.lower() for value in strings)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        return False

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body


def _payload_body() -> bytes:
    return json.dumps(
        {
            "google_api_key": "google-key",
            "dispatch_token": "dispatch-token",
            "account_id": "acct-brand",
            "created_at": "2026-05-24T00:00:00Z",
        }
    ).encode("utf-8")


def _state_payload_body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://services.solstone.app/handoff/scout",
        code,
        "error",
        hdrs=None,
        fp=io.BytesIO(b""),
    )


def _install_urlopen(monkeypatch: pytest.MonkeyPatch, items: list[object]) -> None:
    queue = list(items)

    def fake_urlopen(_request, _timeout):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(portal_client.urllib.request, "urlopen", fake_urlopen)


@pytest.mark.parametrize(
    "branch",
    [
        "happy",
        "pending",
        "revoked",
        "legacy_approved",
        "approved_bad_payload",
        "refresh_pending",
        "refresh_approved",
        "refresh_revoked",
        "no_state_no_key",
        "consent_link_expired",
        "consent_timeout",
        "portal_unreachable",
        "tls_verification_failed",
        "nonce_invalid",
        "unexpected_payload",
        "write_failed",
        "already_enabled",
        "manual_key_present",
        "disable_happy",
        "disable_already_disabled",
        "disable_manual_preserved",
        "disable_rotated_stale_key",
        "journal_not_initialized",
        "unknown_service",
    ],
)
def test_cli_branch_output_avoids_blocked_brand_terms(
    branch: str,
    journal_copy,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_open_browser", lambda _url: True)

    argv = ["enable", "scout"]
    if branch == "happy":
        _install_urlopen(monkeypatch, [FakeResponse(200, _payload_body())])
    elif branch == "pending":
        _install_urlopen(
            monkeypatch,
            [
                FakeResponse(
                    200,
                    _state_payload_body(
                        {
                            "state": "pending",
                            "account_id": "acct-p",
                            "since": 1_700_000_000_000,
                        }
                    ),
                )
            ],
        )
    elif branch == "revoked":
        cli.scout.provision_scout_handoff(
            {
                "google_api_key": "google-revoked",
                "dispatch_token": "dispatch-revoked",
                "account_id": "acct-revoked",
                "created_at": "2026-05-24T00:00:00Z",
            }
        )
        _install_urlopen(
            monkeypatch,
            [
                FakeResponse(
                    200,
                    _state_payload_body({"state": "revoked", "account_id": "acct-r"}),
                )
            ],
        )
        argv = ["enable", "scout", "--force"]
    elif branch == "legacy_approved":
        _install_urlopen(monkeypatch, [FakeResponse(200, _payload_body())])
    elif branch == "approved_bad_payload":
        _install_urlopen(
            monkeypatch,
            [
                FakeResponse(
                    200,
                    _state_payload_body(
                        {
                            "state": "approved",
                            "dispatch_token": "d",
                            "account_id": "a",
                            "created_at": "t",
                        }
                    ),
                )
            ],
        )
    elif branch == "refresh_pending":
        _install_urlopen(
            monkeypatch,
            [
                FakeResponse(
                    200,
                    _state_payload_body(
                        {
                            "state": "pending",
                            "account_id": "acct-p",
                            "since": 1_700_000_000_000,
                        }
                    ),
                )
            ],
        )
        argv = ["refresh", "scout"]
    elif branch == "refresh_approved":
        _install_urlopen(
            monkeypatch,
            [
                FakeResponse(
                    200,
                    _state_payload_body(
                        {
                            "state": "approved",
                            "google_api_key": "google-refresh",
                            "dispatch_token": "dispatch-refresh",
                            "account_id": "acct-refresh",
                            "created_at": "2026-05-24T00:00:00Z",
                        }
                    ),
                )
            ],
        )
        argv = ["refresh", "scout"]
    elif branch == "refresh_revoked":
        cli.scout.provision_scout_handoff(
            {
                "google_api_key": "google-refresh-revoked",
                "dispatch_token": "dispatch-refresh-revoked",
                "account_id": "acct-refresh-revoked",
                "created_at": "2026-05-24T00:00:00Z",
            }
        )
        _install_urlopen(
            monkeypatch,
            [
                FakeResponse(
                    200,
                    _state_payload_body({"state": "revoked", "account_id": "acct-r"}),
                )
            ],
        )
        argv = ["refresh", "scout"]
    elif branch == "no_state_no_key":
        _install_urlopen(
            monkeypatch,
            [FakeResponse(200, _state_payload_body({"account_id": "a"}))],
        )
    elif branch == "consent_link_expired":
        _install_urlopen(monkeypatch, [_http_error(410)])
    elif branch == "consent_timeout":
        monkeypatch.setattr(cli, "_wait_seconds", lambda _value: 0)
        _install_urlopen(monkeypatch, [FakeResponse(204)])
        argv = ["enable", "scout", "--wait", "1"]
    elif branch == "portal_unreachable":
        _install_urlopen(monkeypatch, [urllib.error.URLError("down")])
    elif branch == "tls_verification_failed":
        _install_urlopen(monkeypatch, [urllib.error.URLError(ssl.SSLError("cert"))])
    elif branch == "nonce_invalid":
        _install_urlopen(monkeypatch, [_http_error(400)])
    elif branch == "unexpected_payload":
        _install_urlopen(monkeypatch, [FakeResponse(200, b"{")])
    elif branch == "write_failed":
        _install_urlopen(monkeypatch, [FakeResponse(200, _payload_body())])
        monkeypatch.setattr(
            cli.scout,
            "provision_scout_handoff",
            lambda _payload: (_ for _ in ()).throw(OSError("disk")),
        )
    elif branch == "already_enabled":
        config = json.loads((journal_copy / "config" / "journal.json").read_text())
        config.setdefault("env", {})["GOOGLE_API_KEY"] = "existing"
        config.setdefault("services", {})["scout"] = {"account_id": "acct"}
        write_journal_config(config)
    elif branch == "manual_key_present":
        config = json.loads((journal_copy / "config" / "journal.json").read_text())
        config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual"
        config.pop("services", None)
        write_journal_config(config)
    elif branch == "disable_happy":
        cli.scout.provision_scout_handoff(
            {
                "google_api_key": "google-disable",
                "dispatch_token": "dispatch-disable",
                "account_id": "acct-disable",
                "created_at": "2026-05-24T00:00:00Z",
            }
        )
        argv = ["disable", "scout"]
    elif branch == "disable_already_disabled":
        argv = ["disable", "scout"]
    elif branch == "disable_manual_preserved":
        cli.scout.provision_scout_handoff(
            {
                "google_api_key": "google-manual",
                "dispatch_token": "dispatch-manual",
                "account_id": "acct-manual",
                "created_at": "2026-05-24T00:00:00Z",
            }
        )
        config = json.loads((journal_copy / "config" / "journal.json").read_text())
        config["env"]["GOOGLE_API_KEY"] = "manual-replacement"
        write_journal_config(config)
        argv = ["disable", "scout"]
    elif branch == "disable_rotated_stale_key":
        config = json.loads((journal_copy / "config" / "journal.json").read_text())
        config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual-stale"
        config.setdefault("services", {})["scout"] = {
            "account_id": "acct-stale",
            "key_fingerprint_sha256": "0" * 64,
        }
        write_journal_config(config)
        argv = ["disable", "scout"]
    elif branch == "journal_not_initialized":
        journal = tmp_path / "uninitialized-journal"
        (journal / "config").mkdir(parents=True)
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    elif branch == "unknown_service":
        argv = ["enable", "nope"]

    if branch == "unknown_service":
        with pytest.raises(SystemExit):
            cli.main(argv)
    else:
        cli.main(argv)

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert not BLOCKED_COPY_RE.search(combined), (
        f"blocked phrase in branch {branch}: {combined!r}"
    )
