# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from solstone.apps.link import call as link_call
from solstone.apps.link import routes as link_routes
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_config import write_journal_config
from solstone.think.link.paths import save_service_token
from solstone.think.services import operations


@pytest.fixture(autouse=True)
def clear_private_link_registry():
    operations.clear_registry()
    yield
    operations.clear_registry()


def _configure_cli(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client = ConveyClient(session=env.client, base_url="")
    monkeypatch.setattr(link_call, "get_client", lambda: client)


def _set_posture(env, posture: str) -> None:
    config_path = env.journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config.setdefault("link", {})["posture"] = posture
    write_journal_config(config)


def _seed_enabled_private_link(env) -> None:
    _set_posture(env, "spl")
    save_service_token("secret-service-token")


def _invoke(*args: str):
    return CliRunner().invoke(link_call.app, ["private-link", *args])


def test_private_link_status(link_env, monkeypatch: pytest.MonkeyPatch) -> None:
    env = link_env()
    _configure_cli(env, monkeypatch)

    result = _invoke("status")

    assert result.exit_code == 0
    assert "posture: direct" in result.stdout
    assert "state: not enabled" in result.stdout
    assert "enrolled: no" in result.stdout


def test_private_link_setup_success(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = link_env()
    _configure_cli(env, monkeypatch)
    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: operations.HandoffResult("enabled", None, False, True, None),
    )

    result = _invoke("setup", "--wait-seconds", "1", "--poll-interval", "0.01")

    assert result.exit_code == 0
    assert "setting up solstone private link" in result.stdout
    assert "solstone private link is on" in result.stdout


def test_private_link_setup_browser_fallback_prints_portal_url(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = link_env()
    _configure_cli(env, monkeypatch)
    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: operations.HandoffResult(
            "enabled",
            None,
            False,
            False,
            "http://portal/x",
        ),
    )

    result = _invoke("setup", "--wait-seconds", "1", "--poll-interval", "0.01")

    assert result.exit_code == 0
    assert "couldn't open your browser" in result.stdout
    assert "http://portal/x" in result.stdout


def test_private_link_setup_error_exits_nonzero(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = link_env()
    _configure_cli(env, monkeypatch)
    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: operations.HandoffResult(
            "error",
            "try again",
            True,
            True,
            None,
        ),
    )

    result = _invoke("setup", "--wait-seconds", "1", "--poll-interval", "0.01")
    output = result.stdout + result.stderr

    assert result.exit_code == 1
    assert "couldn't finish setting up solstone private link" in output
    assert "try again" in output


def test_private_link_setup_needs_subscription_exits_zero(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = link_env()
    _configure_cli(env, monkeypatch)
    subscribe_url = "https://services.test/account/subscription"
    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: operations.HandoffResult(
            "needs_subscription",
            "private link needs an active subscription before it can turn on.",
            False,
            True,
            None,
            subscribe_url,
        ),
    )

    result = _invoke("setup", "--wait-seconds", "1", "--poll-interval", "0.01")

    assert result.exit_code == 0
    assert "private link needs an active subscription" in result.stdout
    assert subscribe_url in result.stdout


def test_private_link_disable_success(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = link_env()
    _configure_cli(env, monkeypatch)
    _seed_enabled_private_link(env)

    result = _invoke("disable")

    assert result.exit_code == 0
    assert "solstone private link is off" in result.stdout


def test_private_link_disable_failure_prints_repair(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = link_env()
    _configure_cli(env, monkeypatch)
    _seed_enabled_private_link(env)

    def fail_disable():
        raise RuntimeError("config locked")

    monkeypatch.setattr(link_routes.spl, "disable_spl", fail_disable)

    result = _invoke("disable")
    output = result.stdout + result.stderr

    assert result.exit_code == 1
    assert "couldn't turn off solstone private link" in output
