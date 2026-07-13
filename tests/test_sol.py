# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for sol.py unified CLI."""

import logging
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from solstone.think import sol_cli as sol
from solstone.think.sol_cli import JOURNAL_ACCESS_CMD_ERROR

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


def access_command_names() -> list[str]:
    return sorted(
        name for name, command in sol.COMMANDS.items() if command.surface == "access"
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


def run_dispatch(monkeypatch, binary: str, name: str) -> dict[str, object]:
    result: dict[str, object] = {}

    def fake_run_command(module_path: str) -> int:
        result["module"] = module_path
        result["argv"] = sys.argv[:]
        return 0

    monkeypatch.setattr(sol, "run_command", fake_run_command)
    monkeypatch.setattr(sol.setproctitle, "setproctitle", lambda _title: None)
    monkeypatch.setattr(sys, "argv", [binary, name])

    with pytest.raises(SystemExit) as exc_info:
        if binary == "journal":
            sol.journal_main()
        else:
            sol.main()

    assert exc_info.value.code == 0
    return result


class TestResolveCommand:
    """Tests for resolve_command() function."""

    def test_resolve_known_command(self):
        """Test resolving a known command from registry."""
        module_path, preset_args, surface = sol.resolve_command("import")
        assert module_path == "solstone.think.import_client"
        assert preset_args == []
        assert surface == "access"

    def test_resolve_importer_service_command(self):
        """Test resolving the service-side import engine."""
        module_path, preset_args, surface = sol.resolve_command("importer")
        assert module_path == "solstone.think.importers.cli"
        assert preset_args == []
        assert surface == "service"

    def test_resolve_direct_module_path(self):
        """Test resolving a direct module path with dot."""
        module_path, preset_args, surface = sol.resolve_command(
            "solstone.think.importers.cli"
        )
        assert module_path == "solstone.think.importers.cli"
        assert preset_args == []
        assert surface == "service"

    def test_resolve_nested_module_path(self):
        """Test resolving a deeply nested module path."""
        module_path, preset_args, surface = sol.resolve_command(
            "solstone.observe.linux.observer"
        )
        assert module_path == "solstone.observe.linux.observer"
        assert preset_args == []
        assert surface == "service"

    def test_resolve_unknown_command_raises(self):
        """Test that unknown command raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            sol.resolve_command("nonexistent")
        assert "Unknown command: nonexistent" in str(exc_info.value)

    def test_resolve_alias_with_preset_args(self):
        """Test resolving an alias that includes preset arguments."""
        # Add a test alias
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
        """Test that aliases override commands with same name."""
        # Add an alias that shadows a command
        sol.ALIASES["import"] = sol.Alias(
            "solstone.think.cluster", ["--force"], "service"
        )
        try:
            module_path, preset_args, surface = sol.resolve_command("import")
            assert module_path == "solstone.think.cluster"
            assert preset_args == ["--force"]
            assert surface == "service"
        finally:
            del sol.ALIASES["import"]


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
    """Tests for run_command() function."""

    def test_run_command_success(self):
        """Test running a command that exits cleanly."""
        mock_module = MagicMock()
        mock_module.main = MagicMock(return_value=None)

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 0
            mock_module.main.assert_called_once()

    def test_run_command_with_system_exit(self):
        """Test running a command that calls sys.exit(0)."""
        mock_module = MagicMock()
        mock_module.main = MagicMock(side_effect=SystemExit(0))

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 0

    def test_run_command_with_nonzero_exit(self):
        """Test running a command that calls sys.exit(1)."""
        mock_module = MagicMock()
        mock_module.main = MagicMock(side_effect=SystemExit(1))

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 1

    def test_run_command_with_string_exit(self, capsys):
        """Test running a command that raises SystemExit with a string message."""
        mock_module = MagicMock()
        mock_module.main = MagicMock(side_effect=SystemExit("Error: something failed"))

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 1

        captured = capsys.readouterr()
        assert "Error: something failed" in captured.err

    def test_run_command_import_error(self):
        """Test handling ImportError for nonexistent module."""
        with patch(
            "importlib.import_module", side_effect=ImportError("No module named 'fake'")
        ):
            exit_code = sol.run_command("fake.module")
            assert exit_code == 1

    def test_run_command_access_import_error_keeps_raw_error(self, capsys):
        """Import errors keep the canonical raw message."""
        missing = ModuleNotFoundError("No module named 'numpy'", name="numpy")
        with patch("importlib.import_module", side_effect=missing):
            exit_code = sol.run_command("solstone.think.notify_cli")

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "Could not import module 'solstone.think.notify_cli'" in captured.err
        assert "solstone[journal]" not in captured.err

    def test_run_command_no_main_function(self):
        """Test handling module without main() function."""
        mock_module = MagicMock(spec=[])  # No 'main' attribute

        with patch("importlib.import_module", return_value=mock_module):
            exit_code = sol.run_command("test.module")
            assert exit_code == 1

    def test_main_propagates_integer_return_code_via_real_subprocess(self, tmp_path):
        """Would fail on the parent commit because cmd_journal() returned 1 but journal exited 0."""
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
    """Tests for get_status() function."""

    def test_status_with_override(self, monkeypatch, tmp_path):
        """Test status when journal env is set and exists."""
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        status = sol.get_status()
        assert status["journal_path"] == str(tmp_path)
        assert status["journal_source"] == "env"
        assert status["journal_exists"] is True

    def test_status_with_nonexistent_journal(self, monkeypatch, tmp_path):
        """Test status when the journal env points to a nonexistent dir."""
        nonexistent = tmp_path / "nonexistent"
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(nonexistent))

        status = sol.get_status()
        assert status["journal_path"] == str(nonexistent)
        assert status["journal_source"] == "env"
        assert status["journal_exists"] is False

    def test_status_without_override(self, monkeypatch):
        """Test status when no journal env is set uses source-tree fallback."""
        monkeypatch.delenv("SOLSTONE_JOURNAL", raising=False)
        monkeypatch.setattr("solstone.think.user_config.read_user_config", lambda: {})
        status = sol.get_status()
        assert status["journal_path"].endswith("/journal")
        assert status["journal_source"] == "source"
        assert isinstance(status["journal_exists"], bool)


class TestMain:
    """Tests for main() function."""

    def test_main_no_args_shows_help(self, monkeypatch, capsys):
        """Test that running with no args shows help."""
        monkeypatch.setattr(sys, "argv", ["sol"])
        monkeypatch.setenv("SOLSTONE_JOURNAL", "/tmp/test")

        sol.main()

        captured = capsys.readouterr()
        assert "sol - journal access CLI (solstone)" in captured.out
        assert "Usage: sol <command>" in captured.out

    def test_journal_bare_verbose_shows_help_and_configures_debug(
        self, monkeypatch, capsys
    ):
        """Test bare journal -v shows help after enabling DEBUG logging."""
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
        """Test leading sol -v is consumed before subcommand argv rewrite."""
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
            ["sol", "-v", "import", "--day", "20250101"],
        )

        with pytest.raises(SystemExit) as exc_info:
            sol.main()

        rewritten_argv = captured["argv"]
        assert exc_info.value.code == 0
        assert captured["module"] == "solstone.think.import_client"
        assert isinstance(rewritten_argv, list)
        assert rewritten_argv[0] == "sol import"
        assert "-v" not in rewritten_argv
        assert "--verbose" not in rewritten_argv
        assert "--day" in rewritten_argv
        assert "20250101" in rewritten_argv
        assert calls == [{"level": logging.DEBUG}]

    def test_main_help_flag(self, monkeypatch, capsys):
        """Test --help flag shows help."""
        monkeypatch.setattr(sys, "argv", ["sol", "--help"])
        monkeypatch.setenv("SOLSTONE_JOURNAL", "/tmp/test")

        sol.main()

        captured = capsys.readouterr()
        assert "sol - journal access CLI (solstone)" in captured.out

    def test_main_help_command_without_question(self, monkeypatch, capsys):
        """Test bare 'help' command shows static help."""
        monkeypatch.setattr(sys, "argv", ["sol", "help"])
        monkeypatch.setenv("SOLSTONE_JOURNAL", "/tmp/test")

        sol.main()

        captured = capsys.readouterr()
        assert "sol - journal access CLI (solstone)" in captured.out

    def test_main_version_flag(self, monkeypatch, capsys):
        """Test --version flag shows version."""
        monkeypatch.setattr(sys, "argv", ["sol", "--version"])

        sol.main()

        captured = capsys.readouterr()
        assert "sol (solstone)" in captured.out

    def test_main_path_flag(self, monkeypatch, capsys):
        """Test --path flag prints resolved journal path."""
        monkeypatch.setattr(sys, "argv", ["sol", "--path"])
        monkeypatch.setenv("SOLSTONE_JOURNAL", "/tmp/test-journal")

        sol.main()

        captured = capsys.readouterr()
        assert captured.out.strip() == "/tmp/test-journal"

    def test_main_path_flag_default(self, monkeypatch, capsys):
        """Test --path prints project root journal when no override set."""
        monkeypatch.setattr(sys, "argv", ["sol", "--path"])
        monkeypatch.delenv("SOLSTONE_JOURNAL", raising=False)
        sol.main()

        captured = capsys.readouterr()
        path = captured.out.strip()
        assert path != ""
        assert path.endswith("/journal")

    def test_main_root_command(self, monkeypatch, capsys):
        """Test 'root' command prints the project root directory."""
        monkeypatch.setattr(sys, "argv", ["sol", "root"])

        sol.main()

        captured = capsys.readouterr()
        path = captured.out.strip()
        assert path != ""
        # root should NOT end with /journal — that's --path
        assert not path.endswith("/journal")
        # root is the repo root regardless of checkout location or dir name
        assert path == str(REPO_ROOT)

    def test_main_unknown_command_exits(self, monkeypatch):
        """Test that unknown command exits with code 1."""
        monkeypatch.setattr(sys, "argv", ["sol", "unknown-command"])

        with pytest.raises(SystemExit) as exc_info:
            sol.main()
        assert exc_info.value.code == 1

    def test_main_adjusts_sys_argv(self, monkeypatch):
        """Test that sys.argv is adjusted for subcommand."""
        monkeypatch.setattr(sys, "argv", ["sol", "import", "--day", "20250101"])

        captured_argv = []

        def mock_main():
            captured_argv.extend(sys.argv)

        mock_module = MagicMock()
        mock_module.main = mock_main

        with patch("importlib.import_module", return_value=mock_module):
            with pytest.raises(SystemExit):
                sol.main()

        assert captured_argv[0] == "sol import"
        assert "--day" in captured_argv
        assert "20250101" in captured_argv


class TestCommandRegistry:
    """Tests for command registry completeness."""

    def test_all_commands_have_modules(self):
        """Test that all registered commands point to valid module paths."""
        for cmd, command in sol.COMMANDS.items():
            assert "." in command.module, f"Command '{cmd}' has invalid module path"

    def test_groups_contain_valid_commands(self):
        """Test that all commands in groups exist in registry."""
        for group in sol.help_groups():
            for cmd in group.commands:
                assert cmd in sol.COMMANDS, (
                    f"Command '{cmd}' in group '{group.heading}' not in registry"
                )

    def test_critical_commands_registered(self):
        """Test that critical commands are registered."""
        critical = ["import", "providers", "think", "indexer", "transcribe"]
        for cmd in critical:
            assert cmd in sol.COMMANDS, f"Critical command '{cmd}' not registered"

    def test_services_namespace_removed(self):
        """The dissolved services switchboard is not a journal CLI namespace."""
        assert "services" not in sol.COMMANDS
        assert "services" not in sol.service_help_group().commands

    def test_pyproject_scripts_split_thin_base_and_host(self):
        """Root ships thin scripts; both journal leaves own service scripts."""
        root_pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        leaf_pyprojects = [
            tomllib.loads(
                (REPO_ROOT / "packages" / name / "pyproject.toml").read_text(
                    encoding="utf-8"
                )
            )
            for name in ("solstone-journal", "solstone-journal-cuda")
        ]
        root_scripts = root_pyproject["project"]["scripts"]

        assert set(root_scripts) == {"sol", "solstone"}
        assert root_scripts["sol"] == "solstone.think.sol_cli:main"
        assert root_scripts["solstone"] == "solstone.think.sol_cli:main"
        assert "journal" not in root_scripts
        assert "mlx-vlm-server" not in root_scripts

        for leaf_pyproject in leaf_pyprojects:
            leaf_scripts = leaf_pyproject["project"]["scripts"]
            assert set(leaf_scripts) == {"journal", "mlx-vlm-server"}
            assert leaf_scripts["journal"] == "solstone.think.sol_cli:journal_main"
            assert (
                leaf_scripts["mlx-vlm-server"]
                == "solstone.think.providers.mlx_server:main"
            )

    def test_pyproject_declares_journal_parakeet_dependencies(self):
        """The journal host keeps ONNX runtime deps out of the thin base."""
        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        base = pyproject["project"]["dependencies"]
        cpu_leaf = tomllib.loads(
            (REPO_ROOT / "packages" / "solstone-journal" / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        cuda_leaf = tomllib.loads(
            (
                REPO_ROOT / "packages" / "solstone-journal-cuda" / "pyproject.toml"
            ).read_text(encoding="utf-8")
        )
        cpu_deps = cpu_leaf["project"]["dependencies"]
        cuda_deps = cuda_leaf["project"]["dependencies"]

        # The thin base carries no ONNX / STT runtime.
        assert not any("onnxruntime" in dep or "onnx-asr" in dep for dep in base), (
            "thin base must not carry the transcription runtime"
        )

        # The CPU leaf pulls the CPU onnxruntime floor.
        assert "onnxruntime>=1.20.0,!=1.24.1" in cpu_deps
        assert (
            "onnxruntime>=1.25.0,!=1.24.1; sys_platform == 'linux' and platform_machine == 'x86_64'"
            in cpu_deps
        )
        assert "onnxruntime-gpu>=1.25.0" not in cpu_deps

        # The CUDA leaf swaps in the GPU runtime and never the CPU onnxruntime.
        assert "onnxruntime-gpu>=1.25.0" in cuda_deps
        assert not any(
            dep.split(";")[0].strip() == "onnxruntime>=1.20.0,!=1.24.1"
            for dep in cuda_deps
        )

    def test_every_registry_entry_has_surface_tag(self):
        """All commands and aliases declare the CLI surface they belong to."""
        valid_surfaces = {"access", "service", "universal"}
        for name, command in sol.COMMANDS.items():
            assert command.surface in valid_surfaces, (
                f"Command '{name}' has invalid surface '{command.surface}'"
            )
        for name, command_alias in sol.ALIASES.items():
            assert command_alias.surface in valid_surfaces, (
                f"Alias '{name}' has invalid surface '{command_alias.surface}'"
            )

    def test_service_entries_dispatch_through_journal(self, monkeypatch):
        """Service commands and aliases preserve module and preset args on journal."""
        for name in service_command_names():
            journal_result = run_dispatch(monkeypatch, "journal", name)
            command = sol.COMMANDS[name]

            assert journal_result["module"] == command.module
            assert journal_result["argv"] == [f"journal {name}"]

        for name in service_alias_names():
            journal_result = run_dispatch(monkeypatch, "journal", name)
            command_alias = sol.ALIASES[name]

            assert journal_result["module"] == command_alias.module
            assert (
                journal_result["argv"]
                == [f"journal {name}"] + command_alias.preset_args
            )

    @pytest.mark.parametrize("name", universal_command_names())
    def test_universal_entries_dispatch_through_sol_and_journal(
        self, monkeypatch, name
    ):
        """Universal commands are available from both binaries."""
        command = sol.COMMANDS[name]

        sol_result = run_dispatch(monkeypatch, "sol", name)
        journal_result = run_dispatch(monkeypatch, "journal", name)

        assert sol_result["module"] == command.module
        assert sol_result["argv"] == [f"sol {name}"]
        assert journal_result["module"] == command.module
        assert journal_result["argv"] == [f"journal {name}"]

    @pytest.mark.parametrize("name", access_command_names())
    def test_journal_rejects_access_tagged_commands(self, monkeypatch, capsys, name):
        """The journal binary exposes only service-tagged registry entries."""
        monkeypatch.setattr(
            sol,
            "run_command",
            lambda _module_path, **_kwargs: pytest.fail(
                "access command should not run"
            ),
        )
        monkeypatch.setattr(sys, "argv", ["journal", name])

        with pytest.raises(SystemExit) as exc_info:
            sol.journal_main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 2
        assert JOURNAL_ACCESS_CMD_ERROR.format(cmd=name) in captured.err

    def test_journal_import_keeps_access_routing_error(self, monkeypatch, capsys):
        """The service-side engine is `journal importer`; `journal import` remains invalid."""
        monkeypatch.setattr(
            sol,
            "run_command",
            lambda _module_path, **_kwargs: pytest.fail(
                "journal import should not run"
            ),
        )
        monkeypatch.setattr(sys, "argv", ["journal", "import", "--help"])

        with pytest.raises(SystemExit) as exc_info:
            sol.journal_main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 2
        assert JOURNAL_ACCESS_CMD_ERROR.format(cmd="import") in captured.err

    def test_journal_help_lists_service_and_universal_surfaces(self):
        """journal --help renders service and universal command lists."""
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
        for name in access_command_names():
            assert name not in rendered_commands
        assert "sol call" not in result.stdout

    def test_sol_help_lists_access_groups_only(self):
        """sol --help renders only access top-level entries."""
        code = (
            "from solstone.think.sol_cli import main; "
            "import sys; "
            "sys.argv = ['sol', '--help']; "
            "main()"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        rendered_commands = {
            line.strip().split()[0]
            for line in lines
            if line.strip().split() and line.strip().split()[0] in sol.COMMANDS
        }
        expected_group_headers = {group.heading for group in sol.help_groups()}
        rendered_group_headers = {
            line for line in lines if line in expected_group_headers
        }

        rendered_aliases = set()
        in_aliases = False
        for line in lines:
            if line == sol.SOL_HELP_GROUP_ALIASES:
                in_aliases = True
                continue
            if in_aliases and not line.strip():
                break
            if in_aliases:
                name = line.strip().split()[0]
                if name in sol.ALIASES:
                    rendered_aliases.add(name)

        assert rendered_commands == set(
            access_command_names() + universal_command_names()
        )
        assert rendered_group_headers == expected_group_headers
        assert rendered_aliases == set()

    def test_setproctitle_prefix_uses_active_binary(self, monkeypatch):
        """The process title identifies whether sol or journal dispatched the command."""
        titles = []
        monkeypatch.setattr(sol, "run_command", lambda _module_path, **_kwargs: 0)
        monkeypatch.setattr(sol.setproctitle, "setproctitle", titles.append)

        monkeypatch.setattr(sys, "argv", ["sol", "chat"])
        with pytest.raises(SystemExit):
            sol.main()

        monkeypatch.setattr(sys, "argv", ["journal", "supervisor"])
        with pytest.raises(SystemExit):
            sol.journal_main()

        assert titles[0].startswith("sol:")
        assert titles[1].startswith("journal:")
