# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Append-only journal writers with per-record durability."""

import json
import os
from pathlib import Path
from typing import Any


def append_text(path: Path, text: str) -> None:
    """Append text as one durable newline-terminated record unit.

    Durability contract: Each append flushes and fsyncs the file before
    returning; the record is written as a single complete unit (payload +
    trailing newline) so a crash leaves the file ending at a record boundary -
    never a torn partial line. A newly-created file's directory entry is also
    fsynced (best-effort, degraded via logging like atomic_replace).

    Callers pass the record payload without a trailing newline; append_text()
    adds exactly one trailing newline.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "ab") as f:
        f.write((text + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    if is_new:
        from solstone.think.journal_io.atomic import _fsync_dir

        _fsync_dir(path.parent)


def append_jsonl(path: Path, record: Any) -> None:
    """Serialize one record as JSON plus newline, then append via append_text()."""
    append_text(path, json.dumps(record))
