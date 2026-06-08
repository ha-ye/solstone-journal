# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Bounded read-only filesystem tools for cogitate journal evidence.

These tools read only under the journal root. The component and credential
denylist is the hard gate; caps and the per-run budget bound output. Paths are
journal-root-relative and resolve through the same journal-relative path rules
used elsewhere in solstone.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from solstone.think.cogitate_policy import DEFAULT_READ_CALL_BUDGET
from solstone.think.journal_io.errors import PathEscapeError
from solstone.think.journal_io.paths import contained_path

READ_FILE_MAX_LINES = 2000
READ_FILE_MAX_BYTES = 65536
LIST_DIRECTORY_MAX_ENTRIES = 200
GLOB_MAX_MATCHES = 200
GREP_MAX_MATCHES = 100
GREP_MAX_FILES = 1000
GREP_MAX_BYTES_PER_FILE = 20480

DENIED_PATH_COMPONENTS = frozenset(
    {
        ".git",
        ".cache",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        "site-packages",
        ".ssh",
        ".gnupg",
        ".aws",
    }
)
DENIED_CREDENTIAL_PATTERNS = (
    "id_rsa*",
    "*.pem",
    "*.key",
    ".env",
    "*.env",
    "credentials",
    "*.credentials",
)

CLASS_ALLOWED = "allowed"
CLASS_DENIED_COMPONENT = "denied_component"
CLASS_DENIED_CREDENTIAL = "denied_credential"
Classification = Literal["allowed", "denied_component", "denied_credential"]

REFUSAL_PATH_ESCAPE = (
    "path_escape: refused a path that resolves outside the journal; use a "
    "journal-root-relative path inside the journal."
)
REFUSAL_DENIED_COMPONENT = (
    "denied_component: refused a path under a blocked component; use a journal "
    "evidence path outside caches, dependencies, or private config."
)
REFUSAL_CREDENTIAL_FILE = (
    "credential_file: refused a credential-like file; use a non-secret journal "
    "evidence file or a domain command that reports safe status."
)
REFUSAL_NOT_FILE = (
    "not_a_file: refused a directory or non-regular target; choose a regular "
    "text file path instead."
)
REFUSAL_BINARY = (
    "binary_file: refused binary or non-UTF-8 content; use a text export or a "
    "domain command that summarizes the file."
)
REFUSAL_SPECIAL_FILE = (
    "special_file: refused a socket, device, or FIFO; use a regular text file "
    "inside the journal."
)
REFUSAL_MISSING = (
    "missing_or_dangling: refused a missing path or dangling symlink; choose an "
    "existing journal path."
)
REFUSAL_PERMISSION_DENIED = (
    "permission_denied: refused a path the process cannot read; choose a "
    "readable journal file or use a domain command."
)
REFUSAL_BAD_PATH = (
    "bad_path: refused an invalid journal-relative path; use POSIX separators "
    "without absolute, empty, '.', or '..' components."
)
REFUSAL_BAD_PATTERN = (
    "bad_pattern: refused an invalid regex pattern; fix the regex pattern or "
    "drop regex=True for a literal search."
)
REFUSAL_BUDGET_EXHAUSTED = (
    "budget_exhausted: read-call budget is exhausted; stop raw reads and use "
    "the evidence already gathered or a domain command."
)

NOTICE_READ_FILE_TRUNCATED = (
    "read_file_truncated: hit max_lines or max_bytes; use start_line to "
    "continue or choose a smaller file."
)
NOTICE_LIST_DIRECTORY_TRUNCATED = (
    "list_directory_truncated: hit max_entries; narrow with pattern or list a "
    "subdirectory."
)
NOTICE_GLOB_TRUNCATED = (
    "glob_truncated: hit max_matches; use a more specific pattern or root."
)
NOTICE_GREP_TRUNCATED = (
    "grep_search_truncated: hit match, file, or byte cap; narrow pattern, path, "
    "or file_glob."
)

_TOOL_READ_FILE = "read_file"
_TOOL_LIST_DIRECTORY = "list_directory"
_TOOL_GLOB = "glob"
_TOOL_GREP_SEARCH = "grep_search"


@dataclass(frozen=True)
class Entry:
    """A directory listing entry."""

    path: str
    is_dir: bool


@dataclass(frozen=True)
class GrepMatch:
    """A grep match with 1-based line number and optional context."""

    path: str
    lineno: int
    line: str
    before: list[str]
    after: list[str]


@dataclass(frozen=True)
class ReadResult:
    """Uniform result wrapper for all cogitate read tools."""

    tool: str
    ok: bool
    payload: object
    refusal: str | None
    truncated: bool
    notice: str | None


class ReadBudget:
    """Per-run read-call budget shared by the raw-read tools."""

    def __init__(self, cap: int = DEFAULT_READ_CALL_BUDGET) -> None:
        self.cap = int(cap)
        self.count = 0

    def charge(self) -> bool:
        """Increment once if budget remains; return False when exhausted."""
        if self.count >= self.cap:
            return False
        self.count += 1
        return True


def _empty_payload(tool: str) -> object:
    return "" if tool == _TOOL_READ_FILE else []


def _refused(tool: str, refusal: str) -> ReadResult:
    return ReadResult(
        tool=tool,
        ok=False,
        payload=_empty_payload(tool),
        refusal=refusal,
        truncated=False,
        notice=None,
    )


def _ok(
    tool: str,
    payload: object,
    *,
    truncated: bool = False,
    notice: str | None = None,
) -> ReadResult:
    return ReadResult(
        tool=tool,
        ok=True,
        payload=payload,
        refusal=None,
        truncated=truncated,
        notice=notice if truncated else None,
    )


def _charge(tool: str, budget: ReadBudget | None) -> ReadResult | None:
    if budget is not None and not budget.charge():
        return _refused(tool, REFUSAL_BUDGET_EXHAUSTED)
    return None


def _journal_root_real(journal: str | Path) -> Path:
    return Path(os.path.realpath(str(journal)))


def _journal_rel(resolved: Path, journal_root_real: Path) -> str:
    return resolved.relative_to(journal_root_real).as_posix()


def _resolve_target(
    journal: str | Path,
    rel: str | Path,
    tool: str,
) -> tuple[Path | None, ReadResult | None]:
    rel_text = str(rel)
    if rel_text in {"", "."}:
        return _journal_root_real(journal), None
    try:
        return contained_path(journal, rel_text), None
    except PathEscapeError:
        return None, _refused(tool, REFUSAL_PATH_ESCAPE)
    except ValueError:
        return None, _refused(tool, REFUSAL_BAD_PATH)


def _classify(resolved: Path, journal_root_real: Path) -> Classification:
    if resolved == journal_root_real:
        return CLASS_ALLOWED
    parts = resolved.relative_to(journal_root_real).parts
    if any(part in DENIED_PATH_COMPONENTS for part in parts):
        return CLASS_DENIED_COMPONENT
    if any(
        fnmatch.fnmatch(resolved.name, pattern)
        for pattern in DENIED_CREDENTIAL_PATTERNS
    ):
        return CLASS_DENIED_CREDENTIAL
    return CLASS_ALLOWED


def _explicit_denial(tool: str, classification: Classification) -> ReadResult | None:
    if classification == CLASS_DENIED_COMPONENT:
        return _refused(tool, REFUSAL_DENIED_COMPONENT)
    if classification == CLASS_DENIED_CREDENTIAL:
        return _refused(tool, REFUSAL_CREDENTIAL_FILE)
    return None


def _stat_path(
    tool: str, resolved: Path
) -> tuple[os.stat_result | None, ReadResult | None]:
    try:
        return os.stat(resolved, follow_symlinks=True), None
    except FileNotFoundError:
        return None, _refused(tool, REFUSAL_MISSING)
    except PermissionError:
        return None, _refused(tool, REFUSAL_PERMISSION_DENIED)
    except IsADirectoryError:
        return None, _refused(tool, REFUSAL_NOT_FILE)
    except OSError:
        return None, _refused(tool, REFUSAL_PERMISSION_DENIED)


def _is_special(mode: int) -> bool:
    return (
        stat.S_ISSOCK(mode)
        or stat.S_ISBLK(mode)
        or stat.S_ISCHR(mode)
        or stat.S_ISFIFO(mode)
    )


def _decode_clipped(raw_clipped: bytes) -> str | None:
    try:
        return raw_clipped.decode("utf-8")
    except UnicodeDecodeError:
        pass

    for trim in range(1, 4):
        if len(raw_clipped) < trim:
            break
        try:
            return raw_clipped[:-trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _resolve_entry(
    journal: str | Path,
    journal_root_real: Path,
    entry_abs: Path,
) -> tuple[Path | None, Classification | None]:
    rel = entry_abs.relative_to(journal_root_real).as_posix()
    try:
        resolved = contained_path(journal, rel)
    except (PathEscapeError, ValueError):
        return None, None
    classification = _classify(resolved, journal_root_real)
    return resolved, classification


def _hidden_name(path: Path) -> bool:
    return path.name.startswith(".")


def _walk_allowed(
    journal: str | Path,
    journal_root_real: Path,
    start_resolved: Path,
    *,
    include_hidden: bool,
) -> Iterator[tuple[Path, bool]]:
    """Yield allowed contained entries below a directory, pruning unsafe dirs."""
    seen_resolved: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(start_resolved, followlinks=False):
        current = Path(dirpath)
        dirnames.sort()
        filenames.sort()

        kept_dirnames: list[str] = []
        pending_dirs: list[Path] = []
        for name in dirnames:
            entry_abs = current / name
            resolved, classification = _resolve_entry(
                journal,
                journal_root_real,
                entry_abs,
            )
            if resolved is None or classification != CLASS_ALLOWED:
                continue
            if not include_hidden and _hidden_name(entry_abs):
                continue
            if not os.path.islink(entry_abs):
                kept_dirnames.append(name)
            if resolved not in seen_resolved:
                seen_resolved.add(resolved)
                pending_dirs.append(resolved)
        dirnames[:] = kept_dirnames

        for resolved in pending_dirs:
            yield resolved, True

        for name in filenames:
            entry_abs = current / name
            resolved, classification = _resolve_entry(
                journal,
                journal_root_real,
                entry_abs,
            )
            if resolved is None or classification != CLASS_ALLOWED:
                continue
            if not include_hidden and _hidden_name(entry_abs):
                continue
            if resolved in seen_resolved:
                continue
            seen_resolved.add(resolved)
            yield resolved, False


def _iter_allowed_children(
    journal: str | Path,
    journal_root_real: Path,
    start_resolved: Path,
    *,
    include_hidden: bool,
) -> Iterator[tuple[Path, bool]]:
    seen_resolved: set[Path] = set()
    try:
        with os.scandir(start_resolved) as entries:
            sorted_entries = sorted(entries, key=lambda entry: entry.name)
    except PermissionError:
        return
    except OSError:
        return

    for entry in sorted_entries:
        entry_abs = Path(entry.path)
        resolved, classification = _resolve_entry(journal, journal_root_real, entry_abs)
        if resolved is None or classification != CLASS_ALLOWED:
            continue
        if not include_hidden and _hidden_name(entry_abs):
            continue
        if resolved in seen_resolved:
            continue
        seen_resolved.add(resolved)
        try:
            mode = os.stat(resolved, follow_symlinks=True).st_mode
        except OSError:
            continue
        yield resolved, stat.S_ISDIR(mode)


def read_file(
    journal: str | Path,
    path: str | Path,
    *,
    start_line: int = 1,
    max_lines: int = READ_FILE_MAX_LINES,
    max_bytes: int = READ_FILE_MAX_BYTES,
    budget: ReadBudget | None = None,
) -> ReadResult:
    """Read a bounded UTF-8 text file from the journal.

    A date-prefixed path with an 8-digit first component resolves under
    ``chronicle/`` through solstone's journal path rules. ``.`` and ``""`` mean
    the journal root.
    """
    charged = _charge(_TOOL_READ_FILE, budget)
    if charged:
        return charged

    journal_root_real = _journal_root_real(journal)
    resolved, refusal = _resolve_target(journal, path, _TOOL_READ_FILE)
    if refusal:
        return refusal
    assert resolved is not None

    denial = _explicit_denial(
        _TOOL_READ_FILE,
        _classify(resolved, journal_root_real),
    )
    if denial:
        return denial

    stat_result, stat_refusal = _stat_path(_TOOL_READ_FILE, resolved)
    if stat_refusal:
        return stat_refusal
    assert stat_result is not None

    if _is_special(stat_result.st_mode):
        return _refused(_TOOL_READ_FILE, REFUSAL_SPECIAL_FILE)
    if stat.S_ISDIR(stat_result.st_mode) or not stat.S_ISREG(stat_result.st_mode):
        return _refused(_TOOL_READ_FILE, REFUSAL_NOT_FILE)

    byte_limit = max(0, int(max_bytes))
    line_limit = max(0, int(max_lines))
    first_line = max(1, int(start_line))
    try:
        with open(resolved, "rb") as handle:
            raw = handle.read(byte_limit + 1)
    except FileNotFoundError:
        return _refused(_TOOL_READ_FILE, REFUSAL_MISSING)
    except PermissionError:
        return _refused(_TOOL_READ_FILE, REFUSAL_PERMISSION_DENIED)
    except IsADirectoryError:
        return _refused(_TOOL_READ_FILE, REFUSAL_NOT_FILE)
    except OSError:
        return _refused(_TOOL_READ_FILE, REFUSAL_PERMISSION_DENIED)

    if b"\x00" in raw[: min(len(raw), 8192)]:
        return _refused(_TOOL_READ_FILE, REFUSAL_BINARY)

    byte_truncated = len(raw) > byte_limit
    text = _decode_clipped(raw[:byte_limit])
    if text is None:
        return _refused(_TOOL_READ_FILE, REFUSAL_BINARY)

    lines = text.splitlines()
    start_idx = first_line - 1
    if start_idx >= len(lines):
        selected: list[str] = []
        line_truncated = False
    else:
        selected = lines[start_idx : start_idx + line_limit]
        line_truncated = start_idx + len(selected) < len(lines)

    truncated = byte_truncated or line_truncated
    return _ok(
        _TOOL_READ_FILE,
        "\n".join(selected),
        truncated=truncated,
        notice=NOTICE_READ_FILE_TRUNCATED,
    )


def list_directory(
    journal: str | Path,
    path: str | Path = ".",
    *,
    recursive: bool = False,
    max_entries: int = LIST_DIRECTORY_MAX_ENTRIES,
    include_hidden: bool = False,
    pattern: str | None = None,
    budget: ReadBudget | None = None,
) -> ReadResult:
    """List allowed journal directory entries."""
    charged = _charge(_TOOL_LIST_DIRECTORY, budget)
    if charged:
        return charged

    journal_root_real = _journal_root_real(journal)
    resolved, refusal = _resolve_target(journal, path, _TOOL_LIST_DIRECTORY)
    if refusal:
        return refusal
    assert resolved is not None

    denial = _explicit_denial(
        _TOOL_LIST_DIRECTORY,
        _classify(resolved, journal_root_real),
    )
    if denial:
        return denial

    stat_result, stat_refusal = _stat_path(_TOOL_LIST_DIRECTORY, resolved)
    if stat_refusal:
        return stat_refusal
    assert stat_result is not None
    if not stat.S_ISDIR(stat_result.st_mode):
        return _ok(_TOOL_LIST_DIRECTORY, [])

    iterator = (
        _walk_allowed(
            journal,
            journal_root_real,
            resolved,
            include_hidden=include_hidden,
        )
        if recursive
        else _iter_allowed_children(
            journal,
            journal_root_real,
            resolved,
            include_hidden=include_hidden,
        )
    )

    entries: list[Entry] = []
    limit = max(0, int(max_entries))
    truncated = False
    for entry_resolved, is_dir in iterator:
        if pattern and not fnmatch.fnmatch(entry_resolved.name, pattern):
            continue
        if len(entries) >= limit:
            truncated = True
            break
        entries.append(
            Entry(path=_journal_rel(entry_resolved, journal_root_real), is_dir=is_dir)
        )

    return _ok(
        _TOOL_LIST_DIRECTORY,
        entries,
        truncated=truncated,
        notice=NOTICE_LIST_DIRECTORY_TRUNCATED,
    )


def glob(
    journal: str | Path,
    pattern: str,
    *,
    root: str | Path = ".",
    max_matches: int = GLOB_MAX_MATCHES,
    include_hidden: bool = False,
    budget: ReadBudget | None = None,
) -> ReadResult:
    """Find allowed journal paths matching a recursive fnmatch pattern.

    Matching uses ``fnmatch`` against journal-relative POSIX paths, so ``*``
    spans ``/`` and matching is inherently recursive under ``root``.
    """
    charged = _charge(_TOOL_GLOB, budget)
    if charged:
        return charged

    journal_root_real = _journal_root_real(journal)
    resolved, refusal = _resolve_target(journal, root, _TOOL_GLOB)
    if refusal:
        return refusal
    assert resolved is not None

    denial = _explicit_denial(_TOOL_GLOB, _classify(resolved, journal_root_real))
    if denial:
        return denial

    stat_result, stat_refusal = _stat_path(_TOOL_GLOB, resolved)
    if stat_refusal:
        return stat_refusal
    assert stat_result is not None
    if not stat.S_ISDIR(stat_result.st_mode):
        return _ok(_TOOL_GLOB, [])

    matches: list[str] = []
    limit = max(0, int(max_matches))
    truncated = False
    for entry_resolved, _is_dir in _walk_allowed(
        journal,
        journal_root_real,
        resolved,
        include_hidden=include_hidden,
    ):
        rel = _journal_rel(entry_resolved, journal_root_real)
        if not fnmatch.fnmatch(rel, pattern):
            continue
        if len(matches) >= limit:
            truncated = True
            break
        matches.append(rel)

    return _ok(
        _TOOL_GLOB,
        matches,
        truncated=truncated,
        notice=NOTICE_GLOB_TRUNCATED,
    )


def _grep_file(
    resolved: Path,
    journal_root_real: Path,
    matcher: re.Pattern[str],
    *,
    context_lines: int,
    max_bytes_per_file: int,
) -> tuple[list[GrepMatch], bool, ReadResult | None]:
    try:
        with open(resolved, "rb") as handle:
            raw = handle.read(max(0, int(max_bytes_per_file)) + 1)
    except FileNotFoundError:
        return [], False, _refused(_TOOL_GREP_SEARCH, REFUSAL_MISSING)
    except PermissionError:
        return [], False, _refused(_TOOL_GREP_SEARCH, REFUSAL_PERMISSION_DENIED)
    except IsADirectoryError:
        return [], False, _refused(_TOOL_GREP_SEARCH, REFUSAL_NOT_FILE)
    except OSError:
        return [], False, _refused(_TOOL_GREP_SEARCH, REFUSAL_PERMISSION_DENIED)

    if b"\x00" in raw[: min(len(raw), 8192)]:
        return [], False, None

    byte_limit = max(0, int(max_bytes_per_file))
    byte_truncated = len(raw) > byte_limit
    text = _decode_clipped(raw[:byte_limit])
    if text is None:
        return [], byte_truncated, None

    lines = text.splitlines()
    context = max(0, int(context_lines))
    path_text = _journal_rel(resolved, journal_root_real)
    matches: list[GrepMatch] = []
    for idx, line in enumerate(lines):
        if not matcher.search(line):
            continue
        matches.append(
            GrepMatch(
                path=path_text,
                lineno=idx + 1,
                line=line,
                before=lines[max(0, idx - context) : idx],
                after=lines[idx + 1 : idx + 1 + context],
            )
        )
    return matches, byte_truncated, None


def grep_search(
    journal: str | Path,
    pattern: str,
    *,
    path: str | Path = ".",
    regex: bool = False,
    case_sensitive: bool = False,
    file_glob: str | None = None,
    context_lines: int = 0,
    max_matches: int = GREP_MAX_MATCHES,
    max_files: int = GREP_MAX_FILES,
    max_bytes_per_file: int = GREP_MAX_BYTES_PER_FILE,
    include_hidden: bool = False,
    budget: ReadBudget | None = None,
) -> ReadResult:
    """Search allowed UTF-8 journal files for a literal or regex pattern."""
    charged = _charge(_TOOL_GREP_SEARCH, budget)
    if charged:
        return charged

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        matcher = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error:
        return _refused(_TOOL_GREP_SEARCH, REFUSAL_BAD_PATTERN)

    journal_root_real = _journal_root_real(journal)
    resolved, refusal = _resolve_target(journal, path, _TOOL_GREP_SEARCH)
    if refusal:
        return refusal
    assert resolved is not None

    denial = _explicit_denial(
        _TOOL_GREP_SEARCH,
        _classify(resolved, journal_root_real),
    )
    if denial:
        return denial

    stat_result, stat_refusal = _stat_path(_TOOL_GREP_SEARCH, resolved)
    if stat_refusal:
        return stat_refusal
    assert stat_result is not None
    if _is_special(stat_result.st_mode):
        return _refused(_TOOL_GREP_SEARCH, REFUSAL_SPECIAL_FILE)

    matches: list[GrepMatch] = []
    match_limit = max(0, int(max_matches))
    file_limit = max(0, int(max_files))
    scanned_files = 0
    truncated = False

    if stat.S_ISREG(stat_result.st_mode):
        rel = _journal_rel(resolved, journal_root_real)
        if file_glob is None or fnmatch.fnmatch(rel, file_glob):
            file_matches, byte_truncated, file_refusal = _grep_file(
                resolved,
                journal_root_real,
                matcher,
                context_lines=context_lines,
                max_bytes_per_file=max_bytes_per_file,
            )
            if file_refusal:
                return file_refusal
            truncated = byte_truncated
            matches.extend(file_matches[:match_limit])
            if len(file_matches) > match_limit:
                truncated = True
        return _ok(
            _TOOL_GREP_SEARCH,
            matches,
            truncated=truncated,
            notice=NOTICE_GREP_TRUNCATED,
        )

    if not stat.S_ISDIR(stat_result.st_mode):
        return _ok(_TOOL_GREP_SEARCH, [])

    for entry_resolved, is_dir in _walk_allowed(
        journal,
        journal_root_real,
        resolved,
        include_hidden=include_hidden,
    ):
        if is_dir:
            continue
        try:
            entry_mode = os.stat(entry_resolved, follow_symlinks=True).st_mode
        except OSError:
            continue
        if _is_special(entry_mode) or not stat.S_ISREG(entry_mode):
            continue
        rel = _journal_rel(entry_resolved, journal_root_real)
        if file_glob is not None and not fnmatch.fnmatch(rel, file_glob):
            continue
        if scanned_files >= file_limit:
            truncated = True
            break
        scanned_files += 1
        file_matches, byte_truncated, _file_refusal = _grep_file(
            entry_resolved,
            journal_root_real,
            matcher,
            context_lines=context_lines,
            max_bytes_per_file=max_bytes_per_file,
        )
        if byte_truncated:
            truncated = True
        for match in file_matches:
            if len(matches) >= match_limit:
                truncated = True
                break
            matches.append(match)
        if len(matches) >= match_limit:
            break

    return _ok(
        _TOOL_GREP_SEARCH,
        matches,
        truncated=truncated,
        notice=NOTICE_GREP_TRUNCATED,
    )
