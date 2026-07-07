# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest
from werkzeug.routing import Rule

from tests.link.certless_helpers import make_convey_app


def test_legacy_link_prefix_mirrors_network_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    rules = {rule.rule: rule for rule in app.url_map.iter_rules()}
    network_rules = [
        rule
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/app/network/")
    ]
    assert network_rules

    for network_rule in network_rules:
        suffix = network_rule.rule.removeprefix("/app/network")
        legacy_rule = rules[_full_rule("/app/link", suffix)]
        network_endpoint_suffix = network_rule.endpoint.removeprefix("app:network.")
        legacy_endpoint_suffix = legacy_rule.endpoint.removeprefix("app:link.")

        assert _route_methods(network_rule) == _route_methods(legacy_rule)
        assert network_rule.endpoint.startswith("app:network.")
        assert legacy_rule.endpoint.startswith("app:link.")
        assert network_endpoint_suffix == legacy_endpoint_suffix
        if network_endpoint_suffix == "static":
            # Flask binds static views per registration; app-view parity is the guard.
            continue
        assert (
            app.view_functions[network_rule.endpoint]
            is app.view_functions[legacy_rule.endpoint]
        )


def test_network_root_serves_shell_and_legacy_root_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    client = app.test_client()

    network_response = client.get("/app/network/")
    assert network_response.status_code == 200
    assert b'data-solstone-shell="spa"' in network_response.data

    legacy_response = client.get("/app/link/")
    assert legacy_response.status_code == 302
    assert legacy_response.headers["Location"] == "/app/network/"


def test_legacy_link_prefix_serves_native_client_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    client = app.test_client()

    assert client.get("/app/link/api/status").status_code == 200
    assert client.get("/app/link/local-endpoints").status_code == 200


def _full_rule(prefix: str, suffix: str) -> str:
    return f"{prefix}/" if suffix == "/" else f"{prefix}{suffix}"


def _route_methods(rule: Rule) -> list[str]:
    return sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
