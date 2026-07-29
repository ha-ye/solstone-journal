# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.observe.pl_http import PlHttpSession


class RecordingTunnel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes, float | None]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append((method, path, headers, body, timeout))
        return 200, {}, b"{}"

    def close(self) -> None:
        self.closed = True


def test_pl_http_post_and_get_forward_timeout_to_tunnel() -> None:
    tunnel = RecordingTunnel()
    session = PlHttpSession(tunnel)
    post_timeout = 1.25
    get_timeout = 2.5

    session.post(
        "https://peer.test/app/import", json={"ok": True}, timeout=post_timeout
    )
    session.get("https://peer.test/app/manifest?area=segments", timeout=get_timeout)

    assert [call[4] for call in tunnel.calls] == [post_timeout, get_timeout]
    assert tunnel.calls[0][0:2] == ("POST", "/app/import")
    assert tunnel.calls[1][0:2] == ("GET", "/app/manifest?area=segments")
