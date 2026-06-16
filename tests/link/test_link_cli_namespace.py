# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys

import pytest

from solstone.think.link import cli

PAIR_LINK = "https://go.solstone.app/p#PAIRLINK"


def test_link_join_dispatches_to_join_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_join(args) -> int:
        calls.append((args.home, args.code, args.as_role, args.label))
        return 0

    monkeypatch.setattr("solstone.think.link.join_cli.main", fake_join)

    assert (
        cli.main(
            [
                "join",
                "--home",
                "http://receiver",
                "--code",
                PAIR_LINK,
                "--as",
                "observer",
                "--label",
                "laptop",
            ]
        )
        == 0
    )

    assert calls == [("http://receiver", PAIR_LINK, "observer", "laptop")]


def test_link_no_subcommand_help_lists_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([]) == 0

    out = capsys.readouterr().out
    assert "{join,serve}" in out
    assert "join" in out
    assert "serve" in out
    assert "list" not in out


def test_journal_link_routes_to_management_app_help(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from typer.main import get_command

    from solstone.apps.link import call as link_call

    monkeypatch.setattr(sys, "argv", ["journal link", "--help"])

    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    management_commands = set(get_command(link_call.app).commands)
    assert "Usage: journal link" in out
    assert "unpair" in out
    assert "authorized-clients" in out
    assert "join" not in management_commands
    assert "serve" not in management_commands
    assert "join a solstone with a short code or pair link" not in out
    assert "serve a loopback proxy over a link tunnel" not in out


def test_sol_link_list_is_unknown_client_subcommand(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["sol link", "list"])

    with pytest.raises(SystemExit) as exc:
        cli.main(["list"])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "invalid choice: 'list'" in captured.err
    assert "Traceback" not in output


def test_link_serve_dispatches_to_serve_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_serve(args) -> int:
        calls.append((args.command, args.label, args.port, args.relay_url))
        return 0

    monkeypatch.setattr("solstone.think.link.serve_cli.main", fake_serve)

    assert cli.main(["serve", "--label", "x", "--port", "5099"]) == 0

    assert calls == [("serve", "x", 5099, None)]
