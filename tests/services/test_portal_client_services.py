# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from typing import Any

import pytest

from solstone.think.services import portal_client


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_scout_is_default_handoff_service() -> None:
    assert (
        portal_client.browser_url("https://services.test", "NONCE")
        == "https://services.test/enable/scout?nonce=NONCE"
    )
    assert (
        portal_client.poll_url("https://services.test", "NONCE")
        == "https://services.test/handoff/scout?nonce=NONCE"
    )


def test_spl_handoff_urls_are_supported() -> None:
    assert (
        portal_client.browser_url("https://services.test", "NONCE", service="spl")
        == "https://services.test/enable/spl?nonce=NONCE"
    )
    assert (
        portal_client.poll_url("https://services.test", "NONCE", service="spl")
        == "https://services.test/handoff/spl?nonce=NONCE"
    )


def test_spp_handoff_urls_are_supported() -> None:
    assert (
        portal_client.browser_url("https://services.test", "NONCE", service="spp")
        == "https://services.test/enable/spp?nonce=NONCE"
    )
    assert (
        portal_client.poll_url("https://services.test", "NONCE", service="spp")
        == "https://services.test/handoff/spp?nonce=NONCE"
    )


def test_spl_browser_url_includes_instance_when_provided() -> None:
    instance = "00000000-0000-4000-8000-000000000000"

    assert (
        portal_client.browser_url(
            "https://services.test",
            "NONCE",
            service="spl",
            instance=instance,
        )
        == f"https://services.test/enable/spl?nonce=NONCE&instance={instance}"
    )


def test_spb_browser_url_includes_instance_when_provided() -> None:
    instance = "00000000-0000-4000-8000-000000000000"

    assert (
        portal_client.browser_url(
            "https://services.test",
            "NONCE",
            service="backup",
            instance=instance,
        )
        == f"https://services.test/enable/backup?nonce=NONCE&instance={instance}"
    )


def test_browser_url_omits_instance_when_not_provided() -> None:
    assert "instance=" not in portal_client.browser_url(
        "https://services.test", "NONCE", service="spl"
    )
    assert "instance=" not in portal_client.browser_url(
        "https://services.test", "NONCE", service="backup"
    )
    assert "instance=" not in portal_client.browser_url(
        "https://services.test",
        "NONCE",
        service="scout",
    )
    assert "instance=" not in portal_client.browser_url(
        "https://services.test",
        "NONCE",
        service="spp",
    )


def test_scout_browser_url_includes_instance_when_explicitly_provided() -> None:
    instance = "00000000-0000-4000-8000-000000000000"

    assert (
        portal_client.browser_url(
            "https://services.test",
            "NONCE",
            service="scout",
            instance=instance,
        )
        == f"https://services.test/enable/scout?nonce=NONCE&instance={instance}"
    )


@pytest.mark.parametrize("builder", [portal_client.browser_url, portal_client.poll_url])
def test_unknown_service_url_builder_raises(builder) -> None:
    with pytest.raises(ValueError, match="unsupported handoff service"):
        builder("https://services.test", "NONCE", service="bogus")


def test_poll_handoff_unknown_service_never_opens_network(monkeypatch) -> None:
    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("urlopen should not be reached for invalid service")

    monkeypatch.setattr(portal_client.urllib.request, "urlopen", fail_urlopen)

    with pytest.raises(ValueError, match="unsupported handoff service"):
        portal_client.poll_handoff_once(
            "https://services.test",
            "NONCE",
            service="bogus",
        )


def test_poll_handoff_early_access_is_terminal_kind(monkeypatch) -> None:
    monkeypatch.setattr(
        portal_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(200, {"state": "early_access"}),
    )

    outcome = portal_client.poll_handoff_once(
        "https://services.test",
        "NONCE",
        service="spp",
    )

    assert outcome == portal_client.PollOutcome(kind="early_access")
