# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import re

import pytest
import requests
from typer.testing import CliRunner

from solstone.apps.link import call as link_call
from solstone.apps.link import routes as link_routes
from solstone.apps.link.tests.conftest import _StubWatcher
from solstone.think.convey_client import ConveyClient
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.local_endpoints import LocalEndpoint
from solstone.think.link.nonces import NonceStore
from solstone.think.link.paths import (
    LinkState,
    authorized_clients_path,
    nonces_path,
)
from tests._baseline_harness import make_test_client

PAIRED_AT = "2026-04-19T00:00:00Z"
LAST_SEEN_AT = "2026-04-19T00:30:00Z"
FROZEN_NOW = "2026-04-19T01:00:00Z"


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "setup": {"completed_at": 1700000000000},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.link.call.get_client", lambda: client)
    return CliRunner()


def _authorized() -> AuthorizedClients:
    return AuthorizedClients(authorized_clients_path())


def _nonces() -> NonceStore:
    return NonceStore(nonces_path())


def _install_pair_watcher(
    monkeypatch: pytest.MonkeyPatch,
    endpoints: list[LocalEndpoint] | None = None,
) -> None:
    if endpoints is None:
        endpoints = [LocalEndpoint(ip="192.168.1.50", port=7657, scope="lan")]
    monkeypatch.setattr(
        link_routes,
        "get_interface_watcher",
        lambda: _StubWatcher(endpoints),
    )


def _add_device(
    fingerprint: str,
    label: str,
    *,
    role: str = "",
    paired_at: str = PAIRED_AT,
    last_seen_at: str | None = None,
    client_label: str = "",
) -> None:
    store = _authorized()
    store.add(
        fingerprint,
        label,
        "inst-1",
        role=role,
        paired_at=paired_at,
        client_label=client_label,
    )
    if last_seen_at is not None:
        store.touch_last_seen(fingerprint, now=_parse_iso(last_seen_at))


def _parse_iso(value: str):
    import datetime as dt

    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


class _TimeoutTime:
    def __init__(self) -> None:
        self._calls = 0

    def time(self) -> float:
        self._calls += 1
        return 0.0 if self._calls <= 2 else 2.0

    def sleep(self, _seconds: float) -> None:
        return None


class _PairingTime:
    def __init__(self, *, on_sleep) -> None:
        self._on_sleep = on_sleep

    def time(self) -> float:
        return 0.0

    def sleep(self, _seconds: float) -> None:
        self._on_sleep()


def test_pair_rejects_invalid_roles_without_minting(runner):
    for role in ("bogus", "Observer"):
        result = runner.invoke(
            link_call.app,
            ["pair", "--device-label", "test-laptop", "--as", role, "--timeout", "1"],
        )

        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr == "invalid role; expected one of: phone, observer, peer\n"
        assert _nonces().snapshot() == []


def test_pair_mints_nonce_prints_payload_and_times_out(runner, monkeypatch):
    _install_pair_watcher(monkeypatch)
    monkeypatch.setattr(link_call, "time", _TimeoutTime())

    result = runner.invoke(
        link_call.app,
        ["pair", "--device-label", "Test Phone", "--as", "observer", "--timeout", "1"],
    )

    assert result.exit_code == 2
    assert result.stderr == ""
    pair_link_match = re.search(
        r"pair-link: (https://go\.solstone\.app/p#[0-9A-HJKMNP-TV-Z]{64})",
        result.stdout,
    )
    assert pair_link_match
    pair_link = pair_link_match.group(1)
    assert "link this device with:\n" in result.stdout
    assert "  sol link join --code " in result.stdout
    assert pair_link in result.stdout
    assert "--label" in result.stdout
    assert "CA fingerprint: sha256:" in result.stdout
    assert "relay short-code" not in result.stdout
    assert "Pair URL: http" not in result.stdout
    assert "Pair code:" not in result.stdout
    assert "Device: Test Phone\n\nWaiting for linked system…\n" in result.stdout
    assert result.stdout.endswith("Timed out. Pair code expired.\n")
    nonces = _nonces().snapshot()
    assert len(nonces) == 1
    assert nonces[0].device_label == "Test Phone"
    assert nonces[0].role == "observer"


@pytest.mark.parametrize(
    ("extra_args", "expected_role"),
    [
        ([], ""),
        (["--as", "peer"], "peer"),
    ],
)
def test_pair_mints_default_and_peer_roles(
    runner, monkeypatch, extra_args, expected_role
):
    _install_pair_watcher(monkeypatch)
    monkeypatch.setattr(link_call, "time", _TimeoutTime())

    result = runner.invoke(
        link_call.app,
        [
            "pair",
            "--device-label",
            "test-laptop",
            *extra_args,
            "--timeout",
            "1",
        ],
    )

    assert result.exit_code == 2
    nonces = _nonces().snapshot()
    assert len(nonces) == 1
    assert nonces[0].role == expected_role


def test_pair_without_device_label_mints_empty_label(
    runner,
    monkeypatch,
):
    _install_pair_watcher(monkeypatch)
    monkeypatch.setattr(link_call, "time", _TimeoutTime())

    result = runner.invoke(
        link_call.app,
        ["pair", "--timeout", "1"],
    )

    assert result.exit_code == 2
    assert "\nDevice: " not in result.stdout
    nonces = _nonces().snapshot()
    assert len(nonces) == 1
    assert nonces[0].device_label == ""


def test_pair_reports_newly_paired_device(runner, monkeypatch):
    def add_device() -> None:
        if not _authorized().snapshot():
            _add_device("sha256:" + ("1" * 64), "Phone")

    _install_pair_watcher(monkeypatch)
    monkeypatch.setattr(link_call, "time", _PairingTime(on_sleep=add_device))

    result = runner.invoke(
        link_call.app,
        ["pair", "--device-label", "Phone", "--timeout", "5"],
    )

    assert result.exit_code == 0
    assert (
        "Paired: Phone\n"
        "  fingerprint: sha256:1111111111111111111111111111111111111111111111111111111111111111\n"
        f"  paired_at:   {PAIRED_AT}\n"
    ) in result.stdout


def test_pair_reports_display_label_for_newly_paired_device(runner, monkeypatch):
    def add_device() -> None:
        if not _authorized().snapshot():
            _add_device(
                "sha256:" + ("1" * 64),
                "",
                client_label="client-host",
            )

    _install_pair_watcher(monkeypatch)
    monkeypatch.setattr(link_call, "time", _PairingTime(on_sleep=add_device))

    result = runner.invoke(link_call.app, ["pair", "--timeout", "5"])

    assert result.exit_code == 0
    assert (
        "Paired: client-host\n"
        "  fingerprint: sha256:1111111111111111111111111111111111111111111111111111111111111111\n"
        f"  paired_at:   {PAIRED_AT}\n"
    ) in result.stdout


def test_pair_reports_nonce_consumed_fallback(runner, monkeypatch):
    def consume_nonce() -> None:
        nonces = _nonces().snapshot()
        if nonces and not nonces[0].used:
            _nonces().consume(nonces[0].value)

    _install_pair_watcher(monkeypatch)
    monkeypatch.setattr(link_call, "time", _PairingTime(on_sleep=consume_nonce))

    result = runner.invoke(
        link_call.app,
        ["pair", "--device-label", "Phone", "--timeout", "5"],
    )

    assert result.exit_code == 0
    assert (
        "Pair request completed; device should appear in `sol call link list`.\n"
        in result.stdout
    )


def test_pair_reports_no_lan_address_without_nonce(runner, monkeypatch):
    _install_pair_watcher(monkeypatch, [])

    result = runner.invoke(
        link_call.app,
        ["pair", "--device-label", "Phone", "--timeout", "1"],
    )

    assert result.exit_code == 1
    assert "isn't reachable on a network address" in result.stderr
    assert _nonces().snapshot() == []


def test_list_empty_store(runner):
    result = runner.invoke(link_call.app, ["list"])

    assert result.exit_code == 0
    assert result.stdout == "No devices linked yet.\n"


def test_list_linked_systems_only_omits_peer_heading(runner):
    _add_device("sha256:aaaaaaaaaaaaaaaa0000", "alpha")
    _add_device("sha256:bbbbbbbbbbbbbbbb0000", "beta")

    result = runner.invoke(link_call.app, ["list"])

    assert result.exit_code == 0
    assert "Linked systems:\n" in result.stdout
    assert "Peers:" not in result.stdout
    assert result.stdout.count("- ") == 2


def test_list_grouped_output_and_device_line(runner, monkeypatch):
    monkeypatch.setattr(link_call, "_now_utc", lambda: _parse_iso(FROZEN_NOW))
    _add_device(
        "sha256:0123456789abcdef0000",
        "phone",
        role="phone",
        paired_at=PAIRED_AT,
        last_seen_at=LAST_SEEN_AT,
    )
    _add_device(
        "sha256:aaaaaaaaaaaaaaaa0000",
        "linked",
        paired_at=PAIRED_AT,
    )
    _add_device(
        "sha256:bbbbbbbbbbbbbbbb0000",
        "observer",
        role="observer",
        paired_at=PAIRED_AT,
    )
    _add_device("sha256:cccccccccccccccc0000", "peer", role="peer", paired_at=PAIRED_AT)
    _add_device("sha256:dddddddddddddddd0000", "tablet", role="tablet")

    result = runner.invoke(link_call.app, ["list"])

    assert result.exit_code == 0
    assert (
        result.stdout == "Linked systems:\n"
        "- phone — added 1 hour ago — last seen 30 minutes ago [0123456789abcdef]\n"
        "- linked — added 1 hour ago — last seen never [aaaaaaaaaaaaaaaa]\n"
        "- observer — added 1 hour ago — last seen never [bbbbbbbbbbbbbbbb]\n"
        "- tablet — added 1 hour ago — last seen never [dddddddddddddddd]\n"
        "\n"
        "Peers:\n"
        "- peer — added 1 hour ago — last seen never [cccccccccccccccc]\n"
    )


def test_list_uses_display_label_when_assigned_label_is_empty(runner, monkeypatch):
    monkeypatch.setattr(link_call, "_now_utc", lambda: _parse_iso(FROZEN_NOW))
    _add_device(
        "sha256:0123456789abcdef0000",
        "",
        paired_at=PAIRED_AT,
        client_label="client-host",
    )

    result = runner.invoke(link_call.app, ["list"])

    assert result.exit_code == 0
    assert result.stdout == (
        "Linked systems:\n"
        "- client-host — added 1 hour ago — last seen never [0123456789abcdef]\n"
    )


def test_list_uses_server_composed_display_labels(runner, monkeypatch):
    monkeypatch.setattr(link_call, "_now_utc", lambda: _parse_iso(FROZEN_NOW))
    _add_device(
        "sha256:" + ("a" * 64),
        "laptop",
        paired_at=PAIRED_AT,
        client_label="host-1",
    )
    _add_device(
        "sha256:" + ("b" * 64),
        "phone",
        paired_at=PAIRED_AT,
        client_label="phone",
    )

    result = runner.invoke(link_call.app, ["list"])

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.startswith("- ")]
    assert len(lines) == 2
    assert lines[0] != lines[1]
    assert any("laptop (host-1)" in line for line in lines)
    assert any(line.startswith("- phone —") for line in lines)


def test_list_legacy_observer_role_uses_linked_systems_heading(runner):
    _add_device("sha256:bbbbbbbbbbbbbbbb0000", "observer", role="observer")

    result = runner.invoke(link_call.app, ["list"])

    assert result.exit_code == 0
    assert "Linked systems:\n" in result.stdout
    assert "Peers:" not in result.stdout


def test_authorized_clients_flat_view(runner, monkeypatch):
    monkeypatch.setattr(link_call, "_now_utc", lambda: _parse_iso(FROZEN_NOW))
    first_fingerprint = "sha256:" + ("a" * 64)
    second_fingerprint = "sha256:" + ("b" * 64)
    _add_device(
        first_fingerprint,
        "laptop",
        paired_at=PAIRED_AT,
        last_seen_at=LAST_SEEN_AT,
        client_label="host-1",
    )
    _add_device(
        second_fingerprint,
        "phone",
        paired_at=PAIRED_AT,
        client_label="phone",
    )

    result = runner.invoke(link_call.app, ["authorized-clients"])

    assert result.exit_code == 0
    assert result.stdout == (
        f"{first_fingerprint}  laptop (host-1)  last seen 30 minutes ago\n"
        f"{second_fingerprint}  phone  last seen never\n"
    )


def test_authorized_clients_empty_store(runner):
    result = runner.invoke(link_call.app, ["authorized-clients"])

    assert result.exit_code == 0
    assert result.stdout == "No authorized clients.\n"


def test_observer_pause_stub_does_not_require_service(runner, monkeypatch):
    def fail_get_client():
        raise AssertionError("observer-pause must not make an HTTP request")

    monkeypatch.setattr(link_call, "get_client", fail_get_client)

    result = runner.invoke(link_call.app, ["observer-pause"])

    assert result.exit_code == 0
    assert result.stdout == "observer-pause is not yet available.\n"


def test_unpair_success_and_not_found_outputs(runner):
    _add_device("sha256:" + ("a" * 64), "phone")

    success = runner.invoke(link_call.app, ["unpair", "phone"])
    missing_fp = runner.invoke(link_call.app, ["unpair", "sha256:" + ("b" * 64)])
    missing_label = runner.invoke(link_call.app, ["unpair", "missing label"])

    assert success.exit_code == 0
    assert success.stdout == "Unpaired.\n"
    assert _authorized().is_authorized("sha256:" + ("a" * 64)) is False
    assert missing_fp.exit_code == 1
    assert missing_fp.stdout == f"No paired device with fingerprint sha256:{'b' * 64}\n"
    assert missing_fp.stderr == ""
    assert missing_label.exit_code == 1
    assert missing_label.stdout == "No paired device with label 'missing label'\n"
    assert missing_label.stderr == ""


def test_unpair_same_label_removes_first_inserted_match(runner):
    _add_device("sha256:phone", "laptop")
    _add_device("sha256:observer", "laptop", role="observer")

    result = runner.invoke(link_call.app, ["unpair", "laptop"])

    assert result.exit_code == 0
    remaining = _authorized().snapshot()
    assert len(remaining) == 1
    assert remaining[0].fingerprint == "sha256:observer"
    assert remaining[0].role == "observer"


def test_status_provisioned_and_not_provisioned(journal, runner):
    state = LinkState.load_or_create()
    _add_device("sha256:" + ("a" * 64), "phone")

    provisioned = runner.invoke(link_call.app, ["status"])

    assert provisioned.exit_code == 0
    assert provisioned.stdout == (
        f"Instance ID:   {state.instance_id}\n"
        f"Home label:    {state.home_label}\n"
        "Relay URL:     https://link.solstone.app\n"
        "Enrolled:      no\n"
        "Reach posture: direct\n"
        "Private link:  not enabled\n"
        "Paired devices: 1\n"
        "Listen-WS state: (query convey /app/link/api/status for live state)\n"
    )


def test_status_unprovisioned_does_not_write_state(journal, runner, monkeypatch):
    def fail_save(self) -> None:
        raise AssertionError("LinkState.save should not be called by status")

    monkeypatch.setattr(LinkState, "save", fail_save)

    result = runner.invoke(link_call.app, ["status"])

    assert result.exit_code == 0
    assert result.stdout == (
        "Instance ID:   (not provisioned — pair a device to provision)\n"
        "Home label:    (not provisioned)\n"
        "Relay URL:     https://link.solstone.app\n"
        "Enrolled:      no\n"
        "Reach posture: direct\n"
        "Private link:  not enabled\n"
        "Paired devices: 0\n"
        "Listen-WS state: (query convey /app/link/api/status for live state)\n"
    )
    assert not (journal / "link" / "state.json").exists()


def test_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.link.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(link_call.app, ["status"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""
