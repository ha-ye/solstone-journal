# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import pytest

import scripts.build_native_sol_journal_host_commands as journal_host

SERVICE_COMMANDS = ("setup", "think") + tuple(f"svc{i:02d}" for i in range(40))
UNIVERSAL_COMMANDS = ("check", "contract", "doctor", "link")
SERVICE_ALIASES = ("down", "up")


def _source(commands: dict[str, str], aliases: dict[str, str]) -> str:
    command_lines = [
        f'    "{name}": Command("module.{name.replace("-", "_")}", "{surface}"),'
        for name, surface in sorted(commands.items())
    ]
    alias_lines = [
        f'    "{name}": Alias("module.{name.replace("-", "_")}", [], "{surface}"),'
        for name, surface in sorted(aliases.items())
    ]
    return "\n".join(
        [
            "COMMANDS = {",
            *command_lines,
            "}",
            "",
            "ALIASES = {",
            *alias_lines,
            "}",
            "",
        ]
    )


def _base_commands() -> dict[str, str]:
    return {
        **{name: "service" for name in SERVICE_COMMANDS},
        **{name: "universal" for name in UNIVERSAL_COMMANDS},
    }


def _base_aliases() -> dict[str, str]:
    return {name: "service" for name in SERVICE_ALIASES}


def _error(commands: dict[str, str], aliases: dict[str, str]) -> str:
    with pytest.raises(RuntimeError) as error:
        journal_host.extract_partitions(_source(commands, aliases))
    message = str(error.value)
    assert "\n" not in message
    return message


def _old_combined_service_surface_count(
    commands: dict[str, str], aliases: dict[str, str]
) -> int:
    service_commands = {
        name for name, surface in commands.items() if surface == "service"
    }
    service_aliases = {
        name for name, surface in aliases.items() if surface == "service"
    }
    return len(service_commands | service_aliases)


def test_extract_keeps_service_commands_and_aliases_as_sorted_moved_list() -> None:
    moved = journal_host.extract(_source(_base_commands(), _base_aliases()))

    assert len(moved) == 44
    assert moved == sorted((*SERVICE_COMMANDS, *SERVICE_ALIASES))


def test_service_command_count_rejects_43_service_commands_and_1_alias() -> None:
    commands = _base_commands()
    commands["audio"] = "service"
    aliases = _base_aliases()
    del aliases["down"]

    assert _old_combined_service_surface_count(commands, aliases) == 44
    message = _error(commands, aliases)

    assert message.startswith("journal-host service COMMANDS count 43 != 42: ")
    assert "'audio'" in message


def test_service_command_count_rejects_41_service_commands_and_3_aliases() -> None:
    commands = _base_commands()
    del commands["svc00"]
    aliases = _base_aliases()
    aliases["extra"] = "service"

    assert _old_combined_service_surface_count(commands, aliases) == 44
    message = _error(commands, aliases)

    assert message.startswith("journal-host service COMMANDS count 41 != 42: ")


@pytest.mark.parametrize("alias", ["up", "down"])
def test_service_aliases_reject_missing_expected_alias(alias: str) -> None:
    aliases = _base_aliases()
    del aliases[alias]

    message = _error(_base_commands(), aliases)

    assert (
        message
        == f"journal-host service ALIASES drifted; missing=['{alias}']; extra=[]; changed_surface=[]"
    )


@pytest.mark.parametrize("alias", ["up", "down"])
def test_service_aliases_report_reclassified_alias(alias: str) -> None:
    aliases = _base_aliases()
    aliases[alias] = "universal"

    message = _error(_base_commands(), aliases)

    assert (
        message
        == f"journal-host service ALIASES drifted; missing=['{alias}']; extra=[]; changed_surface=['{alias}']"
    )


@pytest.mark.parametrize("command", ["doctor", "check", "contract", "link"])
def test_universal_commands_reject_missing_expected_command(command: str) -> None:
    commands = _base_commands()
    del commands[command]

    message = _error(commands, _base_aliases())

    assert (
        message
        == f"journal-host universal COMMANDS drifted; missing=['{command}']; extra=[]; changed_surface=[]"
    )


def test_universal_commands_report_reclassified_command() -> None:
    commands = _base_commands()
    commands["doctor"] = "service"
    del commands["svc00"]

    message = _error(commands, _base_aliases())

    assert (
        message
        == "journal-host universal COMMANDS drifted; missing=['doctor']; extra=[]; changed_surface=['doctor']"
    )


def test_commands_and_aliases_overlap_is_rejected() -> None:
    aliases = _base_aliases()
    aliases["svc00"] = "service"

    message = _error(_base_commands(), aliases)

    assert message == "journal-host COMMANDS and ALIASES overlap: ['svc00']"


def test_empty_extraction_is_rejected() -> None:
    message = _error({}, {})

    assert message == "journal-host command extraction is empty"


@pytest.mark.parametrize("sentinel", ["think", "setup"])
def test_service_command_sentinel_is_required(sentinel: str) -> None:
    commands = _base_commands()
    del commands[sentinel]
    commands["aaa"] = "service"

    message = _error(commands, _base_aliases())

    assert message == f"journal-host service COMMANDS missing sentinels: ['{sentinel}']"
