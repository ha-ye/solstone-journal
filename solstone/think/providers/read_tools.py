# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenHands tool wrappers for bounded cogitate journal reads."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from solstone.think.cogitate_read_tools import (
    ReadBudget,
    ReadResult,
    glob,
    grep_search,
    list_directory,
    read_file,
)


def _invoke_read_file(action: Any, journal: Path, budget: ReadBudget) -> ReadResult:
    return read_file(
        journal,
        action.path,
        start_line=action.start_line,
        budget=budget,
    )


def _invoke_list_directory(
    action: Any,
    journal: Path,
    budget: ReadBudget,
) -> ReadResult:
    return list_directory(
        journal,
        action.path,
        recursive=action.recursive,
        include_hidden=action.include_hidden,
        pattern=action.pattern,
        budget=budget,
    )


def _invoke_glob(action: Any, journal: Path, budget: ReadBudget) -> ReadResult:
    return glob(
        journal,
        action.pattern,
        root=action.root,
        include_hidden=action.include_hidden,
        budget=budget,
    )


def _invoke_grep_search(action: Any, journal: Path, budget: ReadBudget) -> ReadResult:
    return grep_search(
        journal,
        action.pattern,
        path=action.path,
        regex=action.regex,
        case_sensitive=action.case_sensitive,
        file_glob=action.file_glob,
        context_lines=action.context_lines,
        include_hidden=action.include_hidden,
        budget=budget,
    )


def _render(result: ReadResult) -> str:
    if result.tool == read_file.__name__:
        text = cast(str, result.payload)
    elif result.tool == list_directory.__name__:
        entries = cast(Iterable[Any], result.payload)
        text = "\n".join(
            f"{entry.path}/" if entry.is_dir else entry.path for entry in entries
        )
    elif result.tool == glob.__name__:
        text = "\n".join(cast(Iterable[str], result.payload))
    elif result.tool == grep_search.__name__:
        lines: list[str] = []
        matches = cast(Iterable[Any], result.payload)
        for match in matches:
            lines.extend(f"{match.path}-{before}" for before in match.before)
            lines.append(f"{match.path}:{match.lineno}:{match.line}")
            lines.extend(f"{match.path}-{after}" for after in match.after)
        text = "\n".join(lines)
    else:
        raise ValueError(f"Unsupported read result tool: {result.tool}")

    if result.truncated and result.notice:
        return f"{text}\n{result.notice}" if text else result.notice
    return text


# Lazy cache for the openhands-derived Read* classes. The classes have to
# live at module level (i.e. without `<locals>` in their __qualname__ and
# discoverable as attributes on this module) because openhands-sdk persists tool
# events to disk and re-validates them via `Event.model_validate_json`, which
# rejects subclasses whose qualname contains "<locals>". OpenHands is installed
# on demand, so define the classes lazily and promote them into this module.
_READ_TYPES: dict[str, Any] = {}


def _ensure_read_types() -> dict[str, Any]:
    if _READ_TYPES:
        return _READ_TYPES

    from openhands.sdk.tool import ToolAnnotations, ToolDefinition, ToolExecutor
    from openhands.sdk.tool.schema import Action, Observation
    from pydantic import Field

    class ReadFileAction(Action):
        path: str = Field(description="Journal-root-relative text file path to read.")
        start_line: int = Field(
            1,
            description="1-based line number to start reading from.",
        )

    class ListDirectoryAction(Action):
        path: str = Field(".", description="Journal-root-relative directory path.")
        recursive: bool = Field(
            False,
            description="Walk recursively below the directory.",
        )
        include_hidden: bool = Field(
            False,
            description="Include hidden entries that are not otherwise denied.",
        )
        pattern: str | None = Field(
            None,
            description="Optional fnmatch pattern applied to each entry name.",
        )

    class GlobAction(Action):
        pattern: str = Field(
            description="Recursive fnmatch pattern over journal-relative paths."
        )
        root: str = Field(".", description="Journal-root-relative directory to narrow.")
        include_hidden: bool = Field(
            False,
            description="Include hidden entries that are not otherwise denied.",
        )

    class GrepSearchAction(Action):
        pattern: str = Field(description="Literal text or regex pattern to search for.")
        path: str = Field(".", description="Journal-root-relative file or directory.")
        regex: bool = Field(False, description="Treat pattern as a regular expression.")
        case_sensitive: bool = Field(
            False,
            description="Use case-sensitive matching.",
        )
        file_glob: str | None = Field(
            None,
            description="Optional fnmatch pattern over journal-relative file paths.",
        )
        context_lines: int = Field(
            0,
            description="Number of surrounding lines to include for each match.",
        )
        include_hidden: bool = Field(
            False,
            description="Include hidden entries that are not otherwise denied.",
        )

    class ReadObservation(Observation):
        pass

    class ReadToolExecutor(ToolExecutor):
        def __init__(
            self,
            *,
            journal: Path,
            budget: ReadBudget,
            invoke: Any,
        ) -> None:
            self.journal = journal
            self.budget = budget
            self.invoke = invoke

        def __call__(self, action: Any, conversation: Any = None) -> Any:
            del conversation
            result = self.invoke(action, self.journal, self.budget)
            if not result.ok:
                return ReadObservation.from_text(result.refusal, is_error=True)
            return ReadObservation.from_text(_render(result))

    class ReadFileTool(ToolDefinition[ReadFileAction, ReadObservation]):
        name = read_file.__name__

        @classmethod
        def create(cls, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

    class ListDirectoryTool(ToolDefinition[ListDirectoryAction, ReadObservation]):
        name = list_directory.__name__

        @classmethod
        def create(cls, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

    class GlobTool(ToolDefinition[GlobAction, ReadObservation]):
        name = glob.__name__

        @classmethod
        def create(cls, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

    class GrepSearchTool(ToolDefinition[GrepSearchAction, ReadObservation]):
        name = grep_search.__name__

        @classmethod
        def create(cls, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

    # Promote the closure-defined classes onto this module so they look
    # module-level to openhands-sdk's serialization machinery. Without
    # this, `__qualname__` carries `<locals>` and re-deserializing tool
    # events fails inside stuck_detector with
    # "Local classes not supported".
    module = sys.modules[__name__]
    for cls in (
        ReadFileAction,
        ListDirectoryAction,
        GlobAction,
        GrepSearchAction,
        ReadObservation,
        ReadToolExecutor,
        ReadFileTool,
        ListDirectoryTool,
        GlobTool,
        GrepSearchTool,
    ):
        cls.__module__ = __name__
        cls.__qualname__ = cls.__name__
        setattr(module, cls.__name__, cls)

    _READ_TYPES.update(
        ReadFileAction=ReadFileAction,
        ListDirectoryAction=ListDirectoryAction,
        GlobAction=GlobAction,
        GrepSearchAction=GrepSearchAction,
        ReadObservation=ReadObservation,
        ReadToolExecutor=ReadToolExecutor,
        ReadFileTool=ReadFileTool,
        ListDirectoryTool=ListDirectoryTool,
        GlobTool=GlobTool,
        GrepSearchTool=GrepSearchTool,
        ToolAnnotations=ToolAnnotations,
    )
    return _READ_TYPES


def build_read_tools(*, journal: Path, read_call_budget: int) -> list[Any]:
    types = _ensure_read_types()
    read_observation = types["ReadObservation"]
    read_executor_cls = types["ReadToolExecutor"]
    tool_annotations = types["ToolAnnotations"]
    budget = ReadBudget(cap=read_call_budget)

    read_file_tool = types["ReadFileTool"](
        description=(
            "Bounded UTF-8 journal text read. Paths are journal-root-relative; "
            "use start_line to paginate a truncation."
        ),
        action_type=types["ReadFileAction"],
        observation_type=read_observation,
        executor=read_executor_cls(
            journal=journal,
            budget=budget,
            invoke=_invoke_read_file,
        ),
        annotations=tool_annotations(
            title=read_file.__name__,
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    list_directory_tool = types["ListDirectoryTool"](
        description=(
            "Journal-root-relative directory listing. Supports recursive walks "
            "and fnmatch patterns on entry names."
        ),
        action_type=types["ListDirectoryAction"],
        observation_type=read_observation,
        executor=read_executor_cls(
            journal=journal,
            budget=budget,
            invoke=_invoke_list_directory,
        ),
        annotations=tool_annotations(
            title=list_directory.__name__,
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    glob_tool = types["GlobTool"](
        description=(
            "Recursive fnmatch over journal paths where '*' spans '/'; root "
            "narrows the search."
        ),
        action_type=types["GlobAction"],
        observation_type=read_observation,
        executor=read_executor_cls(
            journal=journal,
            budget=budget,
            invoke=_invoke_glob,
        ),
        annotations=tool_annotations(
            title=glob.__name__,
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    grep_search_tool = types["GrepSearchTool"](
        description=(
            "Literal-or-regex search of journal text files. Narrow with path "
            "and file_glob; context_lines adds surrounding lines."
        ),
        action_type=types["GrepSearchAction"],
        observation_type=read_observation,
        executor=read_executor_cls(
            journal=journal,
            budget=budget,
            invoke=_invoke_grep_search,
        ),
        annotations=tool_annotations(
            title=grep_search.__name__,
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    return [read_file_tool, list_directory_tool, glob_tool, grep_search_tool]
