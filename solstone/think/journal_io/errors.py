# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Typed exceptions for canonical journal file-I/O helpers."""

from pathlib import Path


class LockTimeout(RuntimeError):
    """Raised when a stable sidecar lock is not acquired before the deadline."""

    path: Path
    timeout: float

    def __init__(self, path: Path, timeout: float) -> None:
        """Build a timeout error carrying the locked path and timeout seconds."""
        super().__init__(f"could not acquire lock for {path} within {timeout}s")
        self.path = path
        self.timeout = timeout


class MalformedDataError(ValueError):
    """Raised when JSON or JSONL content is malformed under RAISE policy.

    JSON readers raise this from the original JSONDecodeError. JSONL readers
    also attach the 1-based line number that failed to parse.
    """

    path: Path
    lineno: int | None

    def __init__(self, path: Path, *, lineno: int | None = None) -> None:
        """Build a malformed-data error for a file and optional JSONL line."""
        if lineno is None:
            message = f"malformed data in {path}"
        else:
            message = f"malformed data in {path} at line {lineno}"
        super().__init__(message)
        self.path = path
        self.lineno = lineno


class PathEscapeError(ValueError):
    """Raised when a journal-relative path escapes after symlink resolution."""

    path: Path
    rel: str

    def __init__(self, path: Path, rel: str) -> None:
        """Build a containment error carrying the real candidate and rel input."""
        super().__init__(f"{rel!r} escapes the journal root (resolved to {path})")
        self.path = path
        self.rel = rel
