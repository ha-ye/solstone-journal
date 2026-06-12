# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.services import portal_client


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
