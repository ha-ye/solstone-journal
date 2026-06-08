# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.cogitate_read_tools import (
    glob,
    grep_search,
    list_directory,
    read_file,
)
from solstone.think.providers import read_tools
from tests.openhands_fakes import install_fake_openhands


@pytest.fixture
def fake_openhands(monkeypatch):
    return install_fake_openhands(monkeypatch)


def _build_journal(tmp_path: Path) -> Path:
    journal = tmp_path / "journal"
    (journal / "notes").mkdir(parents=True)
    (journal / "notes" / "a.txt").write_text("hello x\n", encoding="utf-8")
    (journal / ".git").mkdir()
    (journal / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (journal / "bin.dat").write_bytes(b"hello\x00x")
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    (journal / "outside-link.txt").symlink_to(tmp_path / "outside.txt")
    return journal


def _build_tools(journal: Path, read_call_budget: int = 20) -> dict[str, object]:
    read_tools._READ_TYPES.clear()
    tools = read_tools.build_read_tools(
        journal=journal,
        read_call_budget=read_call_budget,
    )
    return {tool.name: tool for tool in tools}


def _read_file_args(path: str) -> dict[str, object]:
    return {"path": path, "start_line": 1}


def _list_directory_args(path: str = ".") -> dict[str, object]:
    return {
        "path": path,
        "recursive": False,
        "include_hidden": False,
        "pattern": None,
    }


def test_read_tools_names_and_promoted_qualnames(fake_openhands, tmp_path):
    journal = _build_journal(tmp_path)
    tools = read_tools.build_read_tools(journal=journal, read_call_budget=20)

    assert [tool.name for tool in tools] == [
        read_file.__name__,
        list_directory.__name__,
        glob.__name__,
        grep_search.__name__,
    ]
    for tool in tools:
        assert "<locals>" not in type(tool).__qualname__


def test_read_file_allowed_returns_file_content(fake_openhands, tmp_path):
    journal = _build_journal(tmp_path)
    tools = _build_tools(journal)
    tool = tools[read_file.__name__]

    observation = tool(tool.action_from_arguments(_read_file_args("notes/a.txt")))

    assert observation.is_error is False
    assert "hello x" in observation.text


def test_read_file_denies_blocked_component(fake_openhands, tmp_path):
    journal = _build_journal(tmp_path)
    tools = _build_tools(journal)
    tool = tools[read_file.__name__]

    observation = tool(tool.action_from_arguments(_read_file_args(".git/config")))

    assert observation.is_error is True
    assert observation.text.startswith("denied_component:")


def test_read_file_denies_path_escape_outside_journal(fake_openhands, tmp_path):
    journal = _build_journal(tmp_path)
    tools = _build_tools(journal)
    tool = tools[read_file.__name__]

    observation = tool(tool.action_from_arguments(_read_file_args("outside-link.txt")))

    assert observation.is_error is True
    assert observation.text.startswith("path_escape:")


def test_read_file_denies_binary_file(fake_openhands, tmp_path):
    journal = _build_journal(tmp_path)
    tools = _build_tools(journal)
    tool = tools[read_file.__name__]

    observation = tool(tool.action_from_arguments(_read_file_args("bin.dat")))

    assert observation.is_error is True
    assert observation.text.startswith("binary_file:")


def test_read_tools_share_one_cross_tool_budget(fake_openhands, tmp_path):
    journal = _build_journal(tmp_path)
    tools = _build_tools(journal, read_call_budget=1)
    read_tool = tools[read_file.__name__]
    list_tool = tools[list_directory.__name__]

    first = read_tool(read_tool.action_from_arguments(_read_file_args("notes/a.txt")))
    second = list_tool(list_tool.action_from_arguments(_list_directory_args()))

    assert first.is_error is False
    assert second.is_error is True
    assert second.text.startswith("budget_exhausted:")


def test_list_directory_renders_newline_joined_paths(fake_openhands, tmp_path):
    journal = _build_journal(tmp_path)
    tools = _build_tools(journal)
    tool = tools[list_directory.__name__]

    observation = tool(tool.action_from_arguments(_list_directory_args()))

    assert observation.is_error is False
    assert "notes/" in observation.text
    assert "Entry(" not in observation.text
