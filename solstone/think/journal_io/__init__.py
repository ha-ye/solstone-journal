# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Canonical journal file-I/O mechanics.

This package centralizes atomic replace, append-only writers, cross-process
locking, malformed-data readers, and symlink-aware path containment for journal
files. Domain modules remain responsible for deciding which journal state they
own and when writes are allowed.
"""

# Append-only writers
from solstone.think.journal_io.append import append_jsonl, append_text

# Atomic replace writers
from solstone.think.journal_io.atomic import (
    atomic_replace,
    install_file,
    write_json,
    write_jsonl,
    write_text,
)

# Typed errors
from solstone.think.journal_io.errors import (
    LockTimeout,
    MalformedDataError,
    PathEscapeError,
)

# Cross-process locking
from solstone.think.journal_io.locking import (
    DEFAULT_LOCK_TIMEOUT,
    hold_lock,
)

# Path spine and containment
from solstone.think.journal_io.paths import (
    contained_path,
    day_dirs,
    day_path,
    get_journal,
    iter_segments,
    resolve_journal_path,
    segment_path,
)

# Readers and malformed-data policy
from solstone.think.journal_io.readers import (
    MalformedPolicy,
    read_json,
    read_jsonl,
    read_text,
)

__all__ = [
    # Errors
    "LockTimeout",
    "MalformedDataError",
    "PathEscapeError",
    # Paths
    "contained_path",
    "day_dirs",
    "day_path",
    "get_journal",
    "iter_segments",
    "resolve_journal_path",
    "segment_path",
    # Atomic replace
    "atomic_replace",
    "install_file",
    "write_json",
    "write_jsonl",
    "write_text",
    # Append-only writers
    "append_jsonl",
    "append_text",
    # Locking
    "DEFAULT_LOCK_TIMEOUT",
    "hold_lock",
    # Readers
    "MalformedPolicy",
    "read_json",
    "read_jsonl",
    "read_text",
]
