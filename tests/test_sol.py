# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the surviving journal CLI dispatcher."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from solstone.think import sol_cli as sol

REPO_ROOT = Path(__file__).resolve().parent.parent
EMPTY_PACKAGING_VERSIONS = {
    "solstone": None,
    "solstone-journal": None,
    "solstone-journal-cuda": None,
    "solstone-journal-host": None,
}
MIGRATION_UV_LINE = (
    "uv tool uninstall solstone && uv tool install solstone-journal && "
    "uv tool install solstone"
)


@pytest.fixture(autouse=True, scope="module")
def neutralize_journal_coherence_guard():
    patch = pytest.MonkeyPatch()
    patch.setattr(
        sol,
        "_installed_packaging_versions",
        lambda: dict(EMPTY_PACKAGING_VERSIONS),
    )
    yield
    patch.undo()


def set_packaging_versions(monkeypatch, **overrides):
    versions = dict(EMPTY_PACKAGING_VERSIONS)
    versions.update(overrides)
    monkeypatch.setattr(sol, "_installed_packaging_versions", lambda: versions)


def service_command_names() -> list[str]:
    return sorted(
        name for name, command in sol.COMMANDS.items() if command.surface == "service"
    )


def universal_command_names() -> list[str]:
    return sorted(
        name for name, command in sol.COMMANDS.items() if command.surface == "universal"
    )


def service_alias_names() -> list[str]:
    return [
        name
        for name, command_alias in sol.ALIASES.items()
        if command_alias.surface == "service"
    ]


def run_journal_dispatch(monkeypatch, name: str) -> dict[str, object]:
    result: dict[str, object] = {}

    def fake_run_command(module_path: str) -> int:
        result["module"] = module_path
        result["argv"] = sys.argv[:]
        return 0

    monkeypatch.setattr(sol, "run_command", fake_run_command)
    monkeypatch.setattr(sol.setproctitle, "setproctitle", lambda _title: None)
    monkeypatch.setattr(sys, "argv", ["journal", name])

    with pytest.raises(SystemExit) as exc_info:
        sol.journal_main()

    assert exc_info.value.code == 0
    return result


def test_python_sol_surface_is_removed() -> None:
    assert not hasattr(sol, "main")
    assert not hasattr(sol, "ACCESS_HELP_GROUPS")
    assert not hasattr(sol, "SOL_SERVICE_CMD_REMOVED_ERROR")
    assert not hasattr(sol, "print_help")
    assert all(command.surface != "access" for command in sol.COMMANDS.values())


class TestResolveCommand:
    def test_resolve_importer_service_command(self):
        module_path, preset_args, surface = sol.resolve_command("importer")
        assert module_path == "solstone.think.importers.cli"
        assert preset_args == []
        assert surface == "service"

    def test_resolve_direct_module_path(self):
        module_path, preset_args, surface = sol.resolve_command(
            "solstone.think.importers.cli"
        )
        assert module_path == "solstone.think.importers.cli"
        assert preset_args == []
        assert surface == "service"

    def test_resolve_nested_module_path(self):
        module_path, preset_args, surface = sol.resolve_command(
            "solstone.observe.linux.observer"
        )
        assert module_path == "solstone.observe.linux.observer"
        assert preset_args == []
        assert surface == "service"

    def test_resolve_unknown_command_raises(self):
        with pytest.raises(ValueError) as exc_info:
            sol.resolve_command("nonexistent")
        assert "Unknown command: nonexistent" in str(exc_info.value)

    def test_resolve_alias_with_preset_args(self):
        sol.ALIASES["test-alias"] = sol.Alias(
            "solstone.think.indexer", ["--rescan"], "service"
        )
        try:
            module_path, preset_args, surface = sol.resolve_command("test-alias")
            assert module_path == "solstone.think.indexer"
            assert preset_args == ["--rescan"]
            assert surface == "service"
        finally:
            del sol.ALIASES["test-alias"]

    def test_alias_takes_precedence_over_command(self):
        sol.ALIASES["importer"] = sol.Alias(
            "solstone.think.cluster", ["--force"], "service"
        )
        try:
            module_path, preset_args, surface = sol.resolve_command("importer")
            assert module_path == "solstone.think.cluster"
            assert preset_args == ["--force"]
            assert surface == "service"
        finally:
            del sol.ALIASES["importer"]


class TestJournalCoherenceGuard:
    @pytest.mark.parametrize(
        "versions",
        [
            {
                "solstone": "9.9.9",
                "solstone-journal-cuda": "9.9.9",
            },
            {
                "solstone": "9.9.9",
                "solstone-journal": "9.9.9",
            },
            {
                "solstone": "9.9.9",
                "solstone-journal": "9.9.8",
                "solstone-journal-cuda": "9.9.9",
            },
        ],
    )
    def test_allows_matching_leaf(self, monkeypatch, capsys, versions):
        set_packaging_versions(monkeypatch, **versions)

        sol._guard_journal_coherence()

        assert capsys.readouterr().err == ""

    def test_mismatch_names_cpu_leaf(self, monkeypatch, capsys):
        set_packaging_versions(
            monkeypatch,
            solstone="9.9.9",
            **{"solstone-journal": "9.9.8"},
        )

        with pytest.raises(SystemExit) as exc_info:
            sol._guard_journal_coherence()

        assert exc_info.value.code == 1
        stderr = capsys.readouterr().err
        assert "solstone-journal" in stderr
        assert "solstone-journal-cuda" not in stderr
        assert "9.9.9" in stderr
        assert "9.9.8" in stderr

    def test_mismatch_names_cuda_leaf(self, monkeypatch, capsys):
        set_packaging_versions(
            monkeypatch,
            solstone="9.9.9",
            **{"solstone-journal-cuda": "9.9.8"},
        )

        with pytest.raises(SystemExit) as exc_info:
            sol._guard_journal_coherence()

        assert exc_info.value.code == 1
        stderr = capsys.readouterr().err
        assert "solstone-journal-cuda" in stderr
        assert "9.9.9" in stderr
        assert "9.9.8" in stderr

    def test_shim_migration_message(self, monkeypatch, capsys):
        set_packaging_versions(
            monkeypatch,
            solstone="9.9.9",
            **{"solstone-journal-host": "0.7.0"},
        )

        with pytest.raises(SystemExit) as exc_info:
            sol._guard_journal_coherence()

        assert exc_info.value.code == 1
        stderr = capsys.readouterr().err
        assert "have moved" in stderr
        assert MIGRATION_UV_LINE in stderr

    @pytest.mark.parametrize(
        "versions",
        [
            {},
            {"solstone": "9.9.9"},
        ],
    )
    def test_absence_returns(self, monkeypatch, capsys, versions):
        set_packaging_versions(monkeypatch, **versions)

        sol._guard_journal_coherence()

        assert capsys.readouterr().err == ""


class TestRunCommand:
    def test_run_command_success(self):
        mock_module = MagicMock()
        mock_module.main = MagicMock(return_value=None)

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 0
            mock_module.main.assert_called_once()

    def test_run_command_with_system_exit(self):
        mock_module = MagicMock()
        mock_module.main = MagicMock(side_effect=SystemExit(0))

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 0

    def test_run_command_with_nonzero_exit(self):
        mock_module = MagicMock()
        mock_module.main = MagicMock(side_effect=SystemExit(1))

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 1

    def test_run_command_with_string_exit(self, capsys):
        mock_module = MagicMock()
        mock_module.main = MagicMock(side_effect=SystemExit("Error: something failed"))

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 1

        captured = capsys.readouterr()
        assert "Error: something failed" in captured.err

    def test_run_command_import_error(self):
        with patch(
            "importlib.import_module", side_effect=ImportError("No module named 'fake'")
        ):
            exit_code = sol.run_command("fake.module")
            assert exit_code == 1

    def test_run_command_import_error_keeps_raw_error(self, capsys):
        missing = ModuleNotFoundError("No module named 'numpy'", name="numpy")
        with patch("importlib.import_module", side_effect=missing):
            exit_code = sol.run_command("solstone.think.check")

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "Could not import module 'solstone.think.check'" in captured.err
        assert "solstone[journal]" not in captured.err

    def test_run_command_no_main_function(self):
        mock_module = MagicMock(spec=[])

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 1

    def test_journal_main_propagates_integer_return_code_via_real_subprocess(
        self, tmp_path
    ):
        env = {**os.environ, "SOLSTONE_JOURNAL": str(tmp_path)}
        code = (
            "from solstone.think.sol_cli import journal_main; "
            "import sys; "
            "sys.argv = ['journal', 'config', 'journal', '/tmp/with$dollar']; "
            "journal_main()"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path),
        )

        assert result.returncode == 1


class TestGetStatus:
    def test_status_with_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        status = sol.get_status()
        assert status["journal_path"] == str(tmp_path)
        assert status["journal_source"] == "env"
        assert status["journal_exists"] is True

    def test_status_with_nonexistent_journal(self, monkeypatch, tmp_path):
        nonexistent = tmp_path / "nonexistent"
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(nonexistent))

        status = sol.get_status()
        assert status["journal_path"] == str(nonexistent)
        assert status["journal_source"] == "env"
        assert status["journal_exists"] is False

    def test_status_without_override(self, monkeypatch):
        monkeypatch.delenv("SOLSTONE_JOURNAL", raising=False)
        monkeypatch.setattr("solstone.think.user_config.read_user_config", lambda: {})
        status = sol.get_status()
        assert status["journal_path"].endswith("/journal")
        assert status["journal_source"] == "source"
        assert isinstance(status["journal_exists"], bool)


class TestJournalMain:
    def test_journal_bare_verbose_shows_help_and_configures_debug(
        self, monkeypatch, capsys
    ):
        calls = []
        monkeypatch.setattr(sys, "argv", ["journal", "-v"])
        monkeypatch.setenv("SOLSTONE_JOURNAL", "/tmp/test")
        monkeypatch.setattr(
            sol.logging,
            "basicConfig",
            lambda **kwargs: calls.append(kwargs),
        )

        sol.journal_main()

        captured = capsys.readouterr()
        assert "journal - the journal host CLI (solstone)" in captured.out
        assert "Usage: journal <command>" in captured.out
        assert calls == [{"level": logging.DEBUG}]

    def test_leading_verbose_strips_before_dispatch(self, monkeypatch):
        captured: dict[str, object] = {}
        calls = []

        def fake_run_command(module_path: str) -> int:
            captured["module"] = module_path
            captured["argv"] = sys.argv[:]
            return 0

        monkeypatch.setattr(sol, "run_command", fake_run_command)
        monkeypatch.setattr(sol.setproctitle, "setproctitle", lambda _title: None)
        monkeypatch.setattr(
            sol.logging,
            "basicConfig",
            lambda **kwargs: calls.append(kwargs),
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["journal", "-v", "importer", "--day", "20250101"],
        )

        with pytest.raises(SystemExit) as exc_info:
            sol.journal_main()

        rewritten_argv = captured["argv"]
        assert exc_info.value.code == 0
        assert captured["module"] == "solstone.think.importers.cli"
        assert isinstance(rewritten_argv, list)
        assert rewritten_argv[0] == "journal importer"
        assert "-v" not in rewritten_argv
        assert "--verbose" not in rewritten_argv
        assert "--day" in rewritten_argv
        assert "20250101" in rewritten_argv
        assert calls == [{"level": logging.DEBUG}]

    def test_journal_help_flag(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["journal", "--help"])
        monkeypatch.setenv("SOLSTONE_JOURNAL", "/tmp/test")

        sol.journal_main()

        captured = capsys.readouterr()
        assert "journal - the journal host CLI (solstone)" in captured.out

    def test_journal_version_flag(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["journal", "--version"])

        sol.journal_main()

        captured = capsys.readouterr()
        assert "journal (solstone)" in captured.out

    def test_journal_path_flag(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["journal", "--path"])
        monkeypatch.setenv("SOLSTONE_JOURNAL", "/tmp/test-journal")

        sol.journal_main()

        captured = capsys.readouterr()
        assert captured.out.strip() == "/tmp/test-journal"

    def test_journal_path_flag_default(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["journal", "--path"])
        monkeypatch.delenv("SOLSTONE_JOURNAL", raising=False)
        sol.journal_main()

        captured = capsys.readouterr()
        path = captured.out.strip()
        assert path != ""
        assert path.endswith("/journal")

    def test_journal_unknown_command_exits(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["journal", "unknown-command"])

        with pytest.raises(SystemExit) as exc_info:
            sol.journal_main()
        assert exc_info.value.code == 1

    def test_journal_adjusts_sys_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["journal", "importer", "--day", "20250101"])

        captured_argv = []

        def mock_main():
            captured_argv.extend(sys.argv)

        mock_module = MagicMock()
        mock_module.main = mock_main

        with patch("importlib.import_module", return_value=mock_module):
            with pytest.raises(SystemExit):
                sol.journal_main()

        assert captured_argv[0] == "journal importer"
        assert "--day" in captured_argv
        assert "20250101" in captured_argv


class TestCommandRegistry:
    def test_all_commands_have_modules(self):
        for cmd, command in sol.COMMANDS.items():
            assert "." in command.module, f"Command '{cmd}' has invalid module path"

    def test_service_group_contains_valid_commands(self):
        for cmd in sol.service_help_group().commands:
            assert cmd in sol.COMMANDS, f"Command '{cmd}' not in registry"

    def test_critical_commands_registered(self):
        critical = ["importer", "brain", "think", "indexer", "transcribe"]
        for cmd in critical:
            assert cmd in sol.COMMANDS, f"Critical command '{cmd}' not registered"

    def test_access_commands_are_not_registered(self):
        for cmd in ("import", "chat", "call", "notify", "skills"):
            assert cmd not in sol.COMMANDS

    def test_services_namespace_removed(self):
        assert "services" not in sol.COMMANDS
        assert "services" not in sol.service_help_group().commands

    def test_every_registry_entry_has_surface_tag(self):
        valid_surfaces = {"service", "universal"}
        for name, command in sol.COMMANDS.items():
            assert command.surface in valid_surfaces, (
                f"Command '{name}' has invalid surface '{command.surface}'"
            )
        for name, command_alias in sol.ALIASES.items():
            assert command_alias.surface in valid_surfaces, (
                f"Alias '{name}' has invalid surface '{command_alias.surface}'"
            )

    def test_service_entries_dispatch_through_journal(self, monkeypatch):
        for name in service_command_names():
            journal_result = run_journal_dispatch(monkeypatch, name)
            command = sol.COMMANDS[name]

            assert journal_result["module"] == command.module
            assert journal_result["argv"] == [f"journal {name}"]

        for name in service_alias_names():
            journal_result = run_journal_dispatch(monkeypatch, name)
            command_alias = sol.ALIASES[name]

            assert journal_result["module"] == command_alias.module
            assert (
                journal_result["argv"]
                == [f"journal {name}"] + command_alias.preset_args
            )

    @pytest.mark.parametrize("name", universal_command_names())
    def test_universal_entries_dispatch_through_journal(self, monkeypatch, name):
        command = sol.COMMANDS[name]

        journal_result = run_journal_dispatch(monkeypatch, name)

        assert journal_result["module"] == command.module
        assert journal_result["argv"] == [f"journal {name}"]

    def test_journal_import_is_access_rejection(self, monkeypatch, capsys):
        monkeypatch.setattr(
            sol,
            "run_command",
            lambda _module_path: pytest.fail("journal import should not run"),
        )
        monkeypatch.setattr(sys, "argv", ["journal", "import", "--help"])

        with pytest.raises(SystemExit) as exc_info:
            sol.journal_main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 2
        assert "is a journal-access command" in captured.err
        assert "sol import" in captured.err

    def test_journal_help_lists_service_and_universal_surfaces(self):
        code = (
            "from solstone.think.sol_cli import journal_main; "
            "import sys; "
            "sys.argv = ['journal', '--help']; "
            "journal_main()"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        rendered_commands = {
            line.strip().split()[0]
            for line in result.stdout.splitlines()
            if line.startswith("  ") and line.strip()
        }
        for name in service_command_names() + universal_command_names():
            assert name in rendered_commands
        assert "sol call" not in result.stdout

    def test_setproctitle_prefix_uses_journal_binary(self, monkeypatch):
        titles = []
        monkeypatch.setattr(sol, "run_command", lambda _module_path: 0)
        monkeypatch.setattr(sol.setproctitle, "setproctitle", titles.append)

        monkeypatch.setattr(sys, "argv", ["journal", "supervisor"])
        with pytest.raises(SystemExit):
            sol.journal_main()

        assert titles == ["journal:supervisor"]
