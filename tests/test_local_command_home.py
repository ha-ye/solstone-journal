# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys

import pytest

from solstone.think import sol_cli

LOCAL_COMMANDS = {
    "navigate": "solstone.think.tools.navigate",
    "identity": "solstone.think.tools.sol",
    "install-provider": "solstone.think.install_provider",
}


@pytest.mark.parametrize(("command", "module"), LOCAL_COMMANDS.items())
def test_local_commands_resolve_as_service(command: str, module: str) -> None:
    resolved_module, preset_args, surface = sol_cli.resolve_command(command)

    assert resolved_module == module
    assert preset_args == []
    assert surface == "service"


@pytest.mark.parametrize(
    ("command", "extra_args"),
    [
        ("navigate", ["/x"]),
        ("identity", ["partner"]),
        ("install-provider", ["local"]),
    ],
)
def test_local_commands_run_under_journal(
    monkeypatch, command: str, extra_args: list[str]
) -> None:
    captured = {}

    def run_command(module_path: str) -> int:
        captured["module_path"] = module_path
        captured["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(sol_cli, "run_command", run_command)
    monkeypatch.setattr(sys, "argv", ["journal", command, *extra_args])

    with pytest.raises(SystemExit) as exc_info:
        sol_cli.journal_main()

    assert exc_info.value.code == 0
    assert captured == {
        "module_path": LOCAL_COMMANDS[command],
        "argv": [f"journal {command}", *extra_args],
    }


def test_local_commands_are_journal_help_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sol_cli, "print_status", lambda: None)

    sol_cli.print_journal_help()
    journal_help = capsys.readouterr().out
    for command in LOCAL_COMMANDS:
        assert command in journal_help
        assert sol_cli.COMMANDS[command].surface == "service"
