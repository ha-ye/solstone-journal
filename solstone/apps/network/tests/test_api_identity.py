# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from typing import Any

from solstone.apps.network import routes as link_routes
from solstone.think.link import establish
from solstone.think.link.paths import LinkState

NEUTRAL_IDENTITY = {"committed": False, "instance_id": None, "mark": None}


def _get_identity(env: Any) -> dict[str, Any]:
    response = env.client.get(
        "/app/network/api/identity",
        base_url="http://localhost:7657",
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    return payload


def _get_status(env: Any) -> dict[str, Any]:
    response = env.client.get(
        "/app/network/api/status",
        base_url="http://localhost:7657",
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    return payload


def _assert_mark_shape(mark: dict[str, Any]) -> None:
    assert set(mark) == {"icon1", "icon2", "words"}
    assert {"name", "svg", "color", "rot"} <= set(mark["icon1"])
    assert {"name", "svg", "color", "rot"} <= set(mark["icon2"])
    assert len(mark["words"]) == 2


def test_identity_committed_returns_mark_and_instance_id(link_env) -> None:
    env = link_env()
    state = LinkState.load()
    assert state is not None

    payload = _get_identity(env)

    assert set(payload) == {"committed", "instance_id", "mark"}
    assert payload["committed"] is True
    assert payload["instance_id"] == state.instance_id
    assert payload["mark"] == establish.committed_mark().to_render_spec()
    _assert_mark_shape(payload["mark"])


def test_identity_not_committed_returns_neutral_payload(link_env) -> None:
    env = link_env(provision=False)

    payload = _get_identity(env)

    assert payload == NEUTRAL_IDENTITY


def test_identity_committed_mark_failure_degrades_to_neutral(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    def fail_committed_mark() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "solstone.apps.network.routes.establish.committed_mark",
        fail_committed_mark,
    )

    payload = _get_identity(env)

    assert payload == NEUTRAL_IDENTITY


def test_identity_mark_derivation_failure_degrades_to_neutral(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    def fail_mark_from_spki(_spki_der: bytes) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "solstone.think.link.establish.mark_from_spki",
        fail_mark_from_spki,
    )

    payload = _get_identity(env)

    assert payload == NEUTRAL_IDENTITY


def test_status_does_not_derive_identity_mark(link_env, monkeypatch) -> None:
    env = link_env()
    calls = 0
    original = link_routes.establish.committed_mark

    def count_committed_mark():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(
        "solstone.apps.network.routes.establish.committed_mark",
        count_committed_mark,
    )

    payload = _get_status(env)

    assert calls == 0
    assert "mark" not in payload
