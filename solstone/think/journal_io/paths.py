# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Journal path helpers and symlink-aware containment validation.

The path spine is re-exported from solstone.think.utils. contained_path()
first delegates lexical validation to resolve_journal_path(), then resolves
symlinks in both the candidate and journal root with non-strict realpath before
requiring the candidate to remain under the journal root.
"""

import os
from pathlib import Path

from solstone.think.journal_io.errors import PathEscapeError
from solstone.think.utils import (
    day_dirs,
    day_path,
    get_journal,
    iter_segments,
    resolve_journal_path,
    segment_path,
)


def contained_path(journal: str | Path, rel: str) -> Path:
    """Return a journal-contained path after lexical and symlink checks.

    This helper validates and contains a path; it does not write. The lexical
    guard in resolve_journal_path() rejects empty, absolute, backslash, and
    dot-dot paths. The containment check then catches in-journal symlinks that
    point outside the real journal root and raises PathEscapeError.
    """
    candidate = resolve_journal_path(journal, rel)
    root_real = Path(os.path.realpath(str(journal)))
    cand_real = Path(os.path.realpath(str(candidate)))
    if not cand_real.is_relative_to(root_real):
        raise PathEscapeError(cand_real, rel)
    return cand_real


__all__ = [
    "contained_path",
    "day_dirs",
    "day_path",
    "get_journal",
    "iter_segments",
    "resolve_journal_path",
    "segment_path",
]
