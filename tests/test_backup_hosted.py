# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    HostedCredsUnavailable,
    fetch_hosted_credentials,
    hosted_binding_path,
    load_hosted_binding,
    operated_destination,
    operated_repository,
    save_hosted_binding,
)
from solstone.think.backup.hosted_provider import (
    hosted_append_only_restic_session,
    hosted_restic_session,
)


class _FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body


def _binding(
    *, broker_token: str = "broker-token", prefix: str = "prefix"
) -> HostedBinding:
    return HostedBinding(
        broker_endpoint="https://broker.example",
        account_id="acct",
        instance_id="inst",
        bucket="bkt",
        prefix=prefix,
        broker_token=broker_token,
    )


def _credentials() -> HostedCredentials:
    return HostedCredentials(
        access_key_id="AKID",
        secret_access_key="SAK",
        session_token="SESS",
        endpoint="https://acct.r2.cloudflarestorage.com/",
        expires_at="2026-07-13T12:00:00Z",
    )


def _http_error(status: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://broker.example/backup/credentials",
        status,
        "error",
        {},
        io.BytesIO(body),
    )


def test_binding_round_trip_private_file_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    binding = _binding(prefix="users/acct/inst/")

    save_hosted_binding(binding)

    assert load_hosted_binding() == binding
    assert hosted_binding_path().stat().st_mode & 0o777 == 0o600


def test_load_hosted_binding_returns_none_on_missing_or_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    assert load_hosted_binding() is None

    path = hosted_binding_path()
    path.write_text("{", encoding="utf-8")
    assert load_hosted_binding() is None

    path.write_text(
        json.dumps(
            {
                "broker_endpoint": "https://broker.example",
                "account_id": "acct",
                "instance_id": "inst",
                "bucket": "bkt",
                "prefix": "   ",
            }
        ),
        encoding="utf-8",
    )
    assert load_hosted_binding() is None


def test_operated_repository_preserves_prefix_and_omits_credentials() -> None:
    binding = _binding(prefix="users/acct/inst/")
    creds = _credentials()

    repo = operated_repository(binding, creds)

    assert repo == "s3:https://acct.r2.cloudflarestorage.com/bkt/users/acct/inst/"
    for secret in ("AKID", "SAK", "SESS"):
        assert secret not in repo


def test_operated_destination_includes_s3_session_credentials() -> None:
    destination = operated_destination(_binding(), _credentials())

    assert destination.backend == "s3"
    assert destination.credentials == {
        "access_key_id": "AKID",
        "secret_access_key": "SAK",
        "session_token": "SESS",
    }


def test_fetch_hosted_credentials_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    binding = _binding(broker_token="the-token")

    def fake_urlopen(
        request: urllib.request.Request,
        timeout: float | None = None,
    ) -> _FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(
            json.dumps(
                {
                    "access_key_id": "AKID",
                    "secret_access_key": "SAK",
                    "session_token": "SESS",
                    "endpoint": "https://acct.r2.cloudflarestorage.com",
                    "expires_at": "2026-07-13T12:00:00Z",
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    creds = fetch_hosted_credentials(binding, scope="backup")

    request = captured["request"]
    assert request.get_header("Authorization") == "Bearer the-token"
    assert json.loads((request.data or b"").decode("utf-8")) == {"scope": "backup"}
    assert creds == HostedCredentials(
        access_key_id="AKID",
        secret_access_key="SAK",
        session_token="SESS",
        endpoint="https://acct.r2.cloudflarestorage.com",
        expires_at="2026-07-13T12:00:00Z",
    )


def test_fetch_hosted_credentials_402_entitlement_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        raise _http_error(402, b"")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(HostedCredsUnavailable) as exc_info:
        fetch_hosted_credentials(_binding(), scope="backup")

    assert exc_info.value.reason_code == "hosted_entitlement_inactive"


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(403, b'{"error":"needs_subscription"}'),
        _FakeResponse(b'{"needs_subscription": true}'),
    ],
)
def test_fetch_hosted_credentials_needs_subscription_marker(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception | _FakeResponse,
) -> None:
    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(HostedCredsUnavailable) as exc_info:
        fetch_hosted_credentials(_binding(), scope="backup")

    assert exc_info.value.reason_code == "hosted_entitlement_inactive"


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("x"),
        socket.timeout(),
        ssl.SSLError("tls failed"),
    ],
)
def test_fetch_hosted_credentials_network_failures(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
) -> None:
    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(HostedCredsUnavailable) as exc_info:
        fetch_hosted_credentials(_binding(), scope="backup")

    assert exc_info.value.reason_code == "broker_unreachable"


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(401, b'{"error":"unauthorized"}'),
        _http_error(500, b"server error"),
        _FakeResponse(b"not-json"),
        _FakeResponse(
            b'{"access_key_id":"AKID","secret_access_key":"SAK",'
            b'"endpoint":"https://acct.r2.cloudflarestorage.com"}'
        ),
    ],
)
def test_fetch_hosted_credentials_broker_error(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception | _FakeResponse,
) -> None:
    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(HostedCredsUnavailable) as exc_info:
        fetch_hosted_credentials(_binding(), scope="backup")

    assert exc_info.value.reason_code == "broker_error"


def test_repr_redacts_secrets() -> None:
    assert "the-token" not in repr(_binding(broker_token="the-token"))

    creds = HostedCredentials(
        access_key_id="AKID-SECRET",
        secret_access_key="SAK-SECRET",
        session_token="SESS-SECRET",
        endpoint="https://acct.r2.cloudflarestorage.com",
        expires_at="2026-07-13T12:00:00Z",
    )
    rendered = repr(creds)
    for secret in ("AKID-SECRET", "SAK-SECRET", "SESS-SECRET"):
        assert secret not in rendered
    assert "https://acct.r2.cloudflarestorage.com" in rendered


def test_broker_token_not_logged_on_degrade(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(b"not-json")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    caplog.set_level("WARNING", logger="solstone.backup.hosted")

    with pytest.raises(HostedCredsUnavailable):
        fetch_hosted_credentials(_binding(broker_token="the-token"), scope="backup")

    assert "the-token" not in caplog.text


def test_hosted_restic_session_uses_one_operation_scoped_credential() -> None:
    initial = _credentials()

    with hosted_restic_session(
        _binding(prefix="users/acct/inst/"),
        initial_credentials=initial,
    ) as session:
        assert session.destination.repository == (
            "s3:https://acct.r2.cloudflarestorage.com/bkt/users/acct/inst/"
        )
        assert session.backend_env == {
            "AWS_ACCESS_KEY_ID": "AKID",
            "AWS_SECRET_ACCESS_KEY": "SAK",
            "AWS_SESSION_TOKEN": "SESS",
        }
        assert "AWS_CONTAINER_CREDENTIALS_FULL_URI" not in session.backend_env
        assert "AWS_CONTAINER_AUTHORIZATION_TOKEN" not in session.backend_env


def test_append_only_session_uses_one_operation_scoped_credential() -> None:
    initial = _credentials()
    binding = _binding(prefix="users/acct/inst/")

    with hosted_append_only_restic_session(
        binding,
        rclone_path=Path("/opt/solstone/rclone"),
        initial_credentials=initial,
    ) as session:
        assert session.destination.repository == "rclone:spb:bkt/users/acct/inst/"
        assert session.destination.credentials == {}
        assert session.global_options == (
            "-o",
            "rclone.program=/opt/solstone/rclone",
            "-o",
            "rclone.args=serve restic --stdio --append-only --config /dev/null",
        )
        assert session.backend_env["RCLONE_CONFIG_SPB_TYPE"] == "s3"
        assert session.backend_env["RCLONE_CONFIG_SPB_PROVIDER"] == "Cloudflare"
        assert session.backend_env["RCLONE_CONFIG_SPB_ENV_AUTH"] == "false"
        assert session.backend_env["RCLONE_CONFIG_SPB_ACCESS_KEY_ID"] == "AKID"
        assert session.backend_env["RCLONE_CONFIG_SPB_SECRET_ACCESS_KEY"] == "SAK"
        assert session.backend_env["RCLONE_CONFIG_SPB_SESSION_TOKEN"] == "SESS"
        assert session.backend_env["RCLONE_CONFIG_SPB_ENDPOINT"] == initial.endpoint
        for name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        ):
            assert name not in session.backend_env
