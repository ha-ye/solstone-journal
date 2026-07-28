# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import ast

import pytest

import scripts.build_native_sol_journal_host_commands as journal_host

SERVICE_SENTINELS = ("setup", "think")
SERVICE_COMMANDS = SERVICE_SENTINELS + tuple(
    f"svc{i:02d}"
    for i in range(
        journal_host.EXPECTED_SERVICE_COMMANDS_COUNT - len(SERVICE_SENTINELS)
    )
)
UNIVERSAL_COMMANDS = ("check", "contract", "doctor")
SERVICE_ALIASES = ("down", "up")
SERVICE_SURFACE_COUNT = journal_host.EXPECTED_SERVICE_COMMANDS_COUNT + len(
    SERVICE_ALIASES
)


def _raw_source(lines: list[str]) -> str:
    return "\n".join([*lines, ""])


def _source(commands: dict[str, str], aliases: dict[str, str]) -> str:
    command_lines = [
        f'    "{name}": Command("module.{name.replace("-", "_")}", "{surface}"),'
        for name, surface in sorted(commands.items())
    ]
    alias_lines = [
        f'    "{name}": Alias("module.{name.replace("-", "_")}", [], "{surface}"),'
        for name, surface in sorted(aliases.items())
    ]
    return _raw_source(
        [
            "COMMANDS = {",
            *command_lines,
            "}",
            "",
            "ALIASES = {",
            *alias_lines,
            "}",
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
    return _raw_error(_source(commands, aliases))


def _raw_error(source: str) -> str:
    with pytest.raises(RuntimeError) as error:
        journal_host.extract_partitions(source)
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


def _raw_service_command_literal_count(source: str) -> int:
    tree = ast.parse(source)
    count = 0
    for registry_literal in journal_host.scan_registry_literals(tree):
        if registry_literal.name != "COMMANDS":
            continue
        for key, value in zip(
            registry_literal.node.keys, registry_literal.node.values, strict=True
        ):
            key_value = journal_host.literal_key(key)
            if (
                key_value is not None
                and journal_host.call_surface(value, registry_literal.surface_position)
                == "service"
            ):
                count += 1
    return count


def test_extract_keeps_service_commands_and_aliases_as_sorted_moved_list() -> None:
    moved = journal_host.extract(_source(_base_commands(), _base_aliases()))

    assert len(moved) == SERVICE_SURFACE_COUNT
    assert moved == sorted((*SERVICE_COMMANDS, *SERVICE_ALIASES))


def test_duplicate_commands_rejected_before_partition_validation() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            '    "ghost": Command("module.ghost", SURFACE),',
            '    "ghost": Command("module.ghost_again", SURFACE),',
            "    **EXTRA_COMMANDS,",
            '    NAME: Command("module.dynamic", "service"),',
            "}",
            "",
            "ALIASES = {",
            "}",
        ]
    )

    message = _raw_error(source)

    assert (
        message == "journal-host duplicate registry keys: COMMANDS 'ghost' "
        "[line 2 surface=<unavailable>, line 3 surface=<unavailable>]"
    )


def test_duplicate_service_command_preserves_old_count_but_is_rejected() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            '    "think": Command("module.think", "service"),',
            '    "setup": Command("module.setup", "service"),',
            '    "think": Command("module.think_again", "service"),',
            '    "svc00": Command("module.svc00", "service"),',
            '    "svc01": Command("module.svc01", "service"),',
            '    "svc02": Command("module.svc02", "service"),',
            '    "svc03": Command("module.svc03", "service"),',
            '    "svc04": Command("module.svc04", "service"),',
            '    "svc05": Command("module.svc05", "service"),',
            '    "svc06": Command("module.svc06", "service"),',
            '    "svc07": Command("module.svc07", "service"),',
            '    "svc08": Command("module.svc08", "service"),',
            '    "svc09": Command("module.svc09", "service"),',
            '    "svc10": Command("module.svc10", "service"),',
            '    "svc11": Command("module.svc11", "service"),',
            '    "svc12": Command("module.svc12", "service"),',
            '    "svc13": Command("module.svc13", "service"),',
            '    "svc14": Command("module.svc14", "service"),',
            '    "svc15": Command("module.svc15", "service"),',
            '    "svc16": Command("module.svc16", "service"),',
            '    "svc17": Command("module.svc17", "service"),',
            '    "svc18": Command("module.svc18", "service"),',
            '    "svc19": Command("module.svc19", "service"),',
            '    "svc20": Command("module.svc20", "service"),',
            '    "svc21": Command("module.svc21", "service"),',
            '    "svc22": Command("module.svc22", "service"),',
            '    "svc23": Command("module.svc23", "service"),',
            '    "svc24": Command("module.svc24", "service"),',
            '    "svc25": Command("module.svc25", "service"),',
            '    "svc26": Command("module.svc26", "service"),',
            '    "svc27": Command("module.svc27", "service"),',
            '    "svc28": Command("module.svc28", "service"),',
            '    "svc29": Command("module.svc29", "service"),',
            '    "svc30": Command("module.svc30", "service"),',
            '    "svc31": Command("module.svc31", "service"),',
            '    "svc32": Command("module.svc32", "service"),',
            '    "svc33": Command("module.svc33", "service"),',
            '    "svc34": Command("module.svc34", "service"),',
            '    "svc35": Command("module.svc35", "service"),',
            '    "svc36": Command("module.svc36", "service"),',
            '    "svc37": Command("module.svc37", "service"),',
            '    "svc38": Command("module.svc38", "service"),',
            '    "check": Command("module.check", "universal"),',
            '    "contract": Command("module.contract", "universal"),',
            '    "doctor": Command("module.doctor", "universal"),',
            "}",
            "",
            "ALIASES = {",
            '    "down": Alias("module.down", [], "service"),',
            '    "up": Alias("module.up", [], "service"),',
            "}",
        ]
    )

    assert (
        _raw_service_command_literal_count(source)
        == journal_host.EXPECTED_SERVICE_COMMANDS_COUNT
    )
    message = _raw_error(source)

    assert (
        message == "journal-host duplicate registry keys: COMMANDS 'think' "
        "[line 2 surface=service, line 4 surface=service]"
    )


def test_duplicate_universal_command_is_rejected() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            '    "doctor": Command("module.doctor", "universal"),',
            '    "doctor": Command("module.doctor_again", "universal"),',
            "}",
            "",
            "ALIASES = {",
            "}",
        ]
    )

    message = _raw_error(source)

    assert (
        message == "journal-host duplicate registry keys: COMMANDS 'doctor' "
        "[line 2 surface=universal, line 3 surface=universal]"
    )


def test_duplicate_service_alias_is_rejected() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            "}",
            "",
            "ALIASES = {",
            '    "up": Alias("module.up", [], "service"),',
            '    "up": Alias("module.up_again", [], "service"),',
            "}",
        ]
    )

    message = _raw_error(source)

    assert (
        message == "journal-host duplicate registry keys: ALIASES 'up' "
        "[line 5 surface=service, line 6 surface=service]"
    )


def test_duplicate_keys_sort_key_names_before_source_order() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            '    "zeta": Command("module.zeta", "service"),',
            '    "zeta": Command("module.zeta_again", "service"),',
            '    "alpha": Command("module.alpha", "service"),',
            '    "alpha": Command("module.alpha_again", "service"),',
            "}",
            "",
            "ALIASES = {",
            "}",
        ]
    )

    message = _raw_error(source)

    assert (
        message == "journal-host duplicate registry keys: COMMANDS 'alpha' "
        "[line 4 surface=service, line 5 surface=service]; COMMANDS 'zeta' "
        "[line 2 surface=service, line 3 surface=service]"
    )


def test_duplicate_command_reports_disagreeing_surfaces() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            '    "think": Command("module.think", "service"),',
            '    "think": Command("module.think_again", "universal"),',
            "}",
            "",
            "ALIASES = {",
            "}",
        ]
    )

    message = _raw_error(source)

    assert (
        message == "journal-host duplicate registry keys: COMMANDS 'think' "
        "[line 2 surface=service, line 3 surface=universal]"
    )


def test_duplicates_aggregate_across_plain_and_annotated_assignments() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            '    "think": Command("module.think", "service"),',
            "}",
            "COMMANDS: dict[str, Command] = {",
            '    "think": Command("module.think_again", "service"),',
            "}",
            "ALIASES: dict[str, Alias] = {",
            '    "up": Alias("module.up", [], "service"),',
            "}",
            "ALIASES = {",
            '    "up": Alias("module.up_again", [], "service"),',
            "}",
        ]
    )

    message = _raw_error(source)

    assert (
        message == "journal-host duplicate registry keys: COMMANDS 'think' "
        "[line 2 surface=service, line 5 surface=service]; ALIASES 'up' "
        "[line 8 surface=service, line 11 surface=service]"
    )


def test_simultaneous_duplicates_report_unavailable_surface() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            '    "think": Command("module.think", "service"),',
            '    "think": Command("module.think_again", SURFACE),',
            "}",
            "ALIASES = {",
            '    "up": Alias("module.up", [], "service"),',
            '    "up": Alias("module.up_again", [], alias_surface()),',
            "    **EXTRA_ALIASES,",
            '    ALIAS_NAME: Alias("module.dynamic", [], "service"),',
            "}",
        ]
    )

    message = _raw_error(source)

    assert (
        message == "journal-host duplicate registry keys: COMMANDS 'think' "
        "[line 2 surface=service, line 3 surface=<unavailable>]; ALIASES 'up' "
        "[line 6 surface=service, line 7 surface=<unavailable>]"
    )


def test_spread_and_non_literal_keys_are_skipped_on_a_valid_source() -> None:
    source = _raw_source(
        [
            "COMMANDS = {",
            *[
                f'    "{name}": Command("module.{name.replace("-", "_")}", "service"),'
                for name in SERVICE_COMMANDS
            ],
            "    **EXTRA_COMMANDS,",
            '    NAME: Command("module.dynamic", "service"),',
            *[
                f'    "{name}": Command("module.{name.replace("-", "_")}", "universal"),'
                for name in UNIVERSAL_COMMANDS
            ],
            "}",
            "",
            "ALIASES = {",
            *[
                f'    "{name}": Alias("module.{name.replace("-", "_")}", [], "service"),'
                for name in SERVICE_ALIASES
            ],
            "    **EXTRA_ALIASES,",
            '    ALIAS_NAME: Alias("module.dynamic", [], "service"),',
            "}",
        ]
    )

    moved = journal_host.extract(source)

    assert len(moved) == SERVICE_SURFACE_COUNT
    assert moved == sorted((*SERVICE_COMMANDS, *SERVICE_ALIASES))


def test_production_registry_extracts_expected_partitions() -> None:
    partitions = journal_host.extract_partitions()

    assert set(partitions.service_aliases) == {"up", "down"}
    assert set(partitions.universal_commands) == {
        "doctor",
        "check",
        "contract",
    }
    assert partitions.universal_aliases == ()


def test_service_command_count_rejects_one_extra_service_command_despite_missing_alias() -> (
    None
):
    commands = _base_commands()
    commands["audio"] = "service"
    aliases = _base_aliases()
    del aliases["down"]

    assert (
        _old_combined_service_surface_count(commands, aliases) == SERVICE_SURFACE_COUNT
    )
    message = _error(commands, aliases)
    expected = journal_host.EXPECTED_SERVICE_COMMANDS_COUNT

    assert message.startswith(
        f"journal-host service COMMANDS count {expected + 1} != {expected}: "
    )
    assert "'audio'" in message


def test_service_command_count_rejects_one_missing_service_command_despite_extra_alias() -> (
    None
):
    commands = _base_commands()
    del commands["svc00"]
    aliases = _base_aliases()
    aliases["extra"] = "service"

    assert (
        _old_combined_service_surface_count(commands, aliases) == SERVICE_SURFACE_COUNT
    )
    message = _error(commands, aliases)
    expected = journal_host.EXPECTED_SERVICE_COMMANDS_COUNT

    assert message.startswith(
        f"journal-host service COMMANDS count {expected - 1} != {expected}: "
    )


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


@pytest.mark.parametrize("command", ["doctor", "check", "contract"])
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
