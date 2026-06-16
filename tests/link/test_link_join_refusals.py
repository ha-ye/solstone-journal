# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse

import pytest

from solstone.apps.link.copy import PAIR_LINK_HOST, PAIR_LINK_PATH
from solstone.apps.link.routes import _build_pair_link
from solstone.think.link import join_cli


class _FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        url: str = "",
        content_type: str = "application/json",
    ) -> None:
        self._body = body
        self.status = status
        self._url = url
        self.headers = _FakeHeaders(content_type)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self._url


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        home="http://receiver",
        code="ABCD-EFGH",
        as_role=None,
        label="laptop",
    )


def test_pair_request_refuses_html_bounce(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def fake_urlopen(request, **_kwargs):
        return _FakeResponse(
            b"<html>unexpected</html>",
            url="http://receiver/unexpected",
            content_type="text/html",
        )

    monkeypatch.setattr(join_cli.urllib.request, "urlopen", fake_urlopen)

    result = join_cli.main(_args())

    assert result == 1
    err = capsys.readouterr().err
    assert "http://receiver/app/link/by-code" in err
    assert "http://receiver/unexpected" in err
    assert "text/html" in err


def test_pair_link_without_home_derives_https_target_url() -> None:
    # Criterion 2: pair-link parsing marks the request for framed transport.
    nonce = "a1b2c3d4e5f607181122334455667788"
    pair_link = _build_pair_link(
        "192.0.2.42",
        7657,
        nonce,
        "deadbeefcafebabe0123456789abcdef",
    )

    request = join_cli._parse_pair_link(pair_link, None)

    assert request.url == f"https://192.0.2.42:7657/app/link/pair?token={nonce}"
    assert request.body_base == {}
    assert request.secure is True


def test_manual_code_derives_plain_target_url() -> None:
    # Criterion 2: manual-code parsing stays on the plain HTTP transport.
    request = join_cli._parse_pair_request("ABCD-EFGH", "http://receiver")

    assert request.url == "http://receiver/app/link/by-code"
    assert request.body_base == {"code": "ABCDEFGH"}
    assert request.secure is False


def test_pair_code_error_names_both_accepted_forms() -> None:
    with pytest.raises(ValueError) as exc_info:
        join_cli._parse_pair_request("not-a-code", None)

    message = str(exc_info.value)
    assert "pair-link" in message
    assert "manual" in message
    assert "--home" in message


def test_malformed_pair_link_error_is_distinct() -> None:
    with pytest.raises(ValueError) as exc_info:
        join_cli._parse_pair_request(
            f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#!",
            None,
        )

    assert "Malformed pair-link" in str(exc_info.value)
