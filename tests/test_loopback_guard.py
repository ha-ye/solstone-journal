# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""The dl-surface Host/Origin guard (CSRF + DNS-rebind defense).

The guard is gated on ``dl`` identity mode and applies to every path: a Host
allowlist on all methods (defeats DNS rebinding) and a cross-site check on
state-changing methods (defeats same-host browser CSRF). Paired (PL) devices
and non-browser local clients are untouched.
"""

import pytest
from flask import g

from solstone.convey import create_app
from solstone.convey.root import guard_loopback_origin
from solstone.convey.secure_listener import ConveyIdentity


def _identity(mode):
    return ConveyIdentity(
        mode=mode,
        fingerprint="sha256:abc" if mode != "dl" else None,
        device_label=None,
        paired_at=None,
        session_id=None,
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return create_app(str(journal))


def _guard(app, *, headers, method="GET", mode="dl"):
    """Run the guard in a request context; return None (pass) or (resp, status)."""
    with app.test_request_context("/api/whatever", method=method, headers=headers):
        g.identity = _identity(mode)
        return guard_loopback_origin()


def _rejected(result):
    return result is not None and result[1] == 403


# --- Host allowlist (all methods) ---


def test_dl_loopback_host_passes(app):
    assert _guard(app, headers={"Host": "127.0.0.1:5015"}) is None


def test_dl_nonloopback_host_rejected_on_get(app):
    # DNS-rebind read: browser rebinds a hostname to 127.0.0.1 but sends its Host.
    assert _rejected(_guard(app, headers={"Host": "attacker.example"}))


def test_dl_nonloopback_host_rejected_on_post(app):
    assert _rejected(_guard(app, headers={"Host": "attacker.example"}, method="POST"))


def test_dl_ipv6_loopback_host_passes(app):
    assert _guard(app, headers={"Host": "[::1]:5015"}) is None


def test_dl_nondefault_port_passes(app):
    # Port-agnostic: convey's port is configurable.
    assert _guard(app, headers={"Host": "localhost:9999"}) is None


# --- Cross-site guard (state-changing methods) ---


def test_dl_cross_site_sec_fetch_rejected(app):
    assert _rejected(
        _guard(
            app,
            method="POST",
            headers={"Host": "127.0.0.1:5015", "Sec-Fetch-Site": "cross-site"},
        )
    )


def test_dl_foreign_origin_rejected(app):
    assert _rejected(
        _guard(
            app,
            method="POST",
            headers={"Host": "127.0.0.1:5015", "Origin": "http://attacker.example"},
        )
    )


def test_dl_same_origin_post_passes(app):
    assert (
        _guard(
            app,
            method="POST",
            headers={
                "Host": "127.0.0.1:5015",
                "Origin": "http://127.0.0.1:5015",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        is None
    )


def test_dl_same_origin_nondefault_port_passes(app):
    assert (
        _guard(
            app,
            method="POST",
            headers={"Host": "localhost:9999", "Origin": "http://localhost:9999"},
        )
        is None
    )


def test_dl_local_client_no_origin_passes(app):
    # Non-browser / local ingest client: loopback Host, no Origin, no Sec-Fetch.
    assert _guard(app, method="POST", headers={"Host": "127.0.0.1:5015"}) is None


# --- PL non-interference ---


def test_pl_direct_unaffected_by_host_and_origin(app):
    # A paired (cert) device authenticates by fingerprint; the guard never fires.
    assert (
        _guard(
            app,
            method="POST",
            headers={"Host": "anything.example", "Origin": "http://elsewhere"},
            mode="pl-direct",
        )
        is None
    )


def test_pl_via_spl_unaffected(app):
    assert (
        _guard(
            app,
            method="POST",
            headers={"Host": "anything.example", "Sec-Fetch-Site": "cross-site"},
            mode="pl-via-spl",
        )
        is None
    )


# --- Wiring: the guard fires through real request dispatch (before_app_request) ---


def test_guard_is_wired_and_rejects_bad_host_end_to_end(app):
    # /init is exempt from require_access's setup redirect, so a real dl request
    # reaches the guard. A non-loopback Host must be rejected 403 by the wiring.
    client = app.test_client()
    resp = client.get("/init", headers={"Host": "attacker.example"})
    assert resp.status_code == 403


def test_guard_allows_loopback_host_end_to_end(app):
    client = app.test_client()
    resp = client.get("/init", headers={"Host": "127.0.0.1:5015"})
    assert resp.status_code != 403
