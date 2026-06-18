# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.apps.link.copy import PAIR_LINK_HOST, PAIR_LINK_PATH
from solstone.apps.link.routes import _build_pair_link
from solstone.think.link import join_cli


def test_pair_link_without_home_derives_https_target_url() -> None:
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


def test_pair_code_error_names_pair_link_form() -> None:
    with pytest.raises(ValueError) as exc_info:
        join_cli._parse_pair_request("not-a-code", None)

    message = str(exc_info.value)
    assert "pair-link" in message


def test_malformed_pair_link_error_is_distinct() -> None:
    with pytest.raises(ValueError) as exc_info:
        join_cli._parse_pair_request(
            f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#!",
            None,
        )

    assert "Malformed pair-link" in str(exc_info.value)
