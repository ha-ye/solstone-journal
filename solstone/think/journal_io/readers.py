# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""JSON, JSONL, and text readers with explicit malformed-data policy."""

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any

from solstone.think.journal_io.errors import MalformedDataError


class MalformedPolicy(str, Enum):
    """Policy for malformed JSON and JSONL input."""

    SKIP = "skip"
    WARN_AND_SKIP = "warn_and_skip"
    RAISE = "raise"


def read_json(
    path: Path,
    *,
    on_error: MalformedPolicy = MalformedPolicy.RAISE,
    default: Any = None,
) -> Any:
    """Read one JSON value, separating missing/empty from malformed content.

    Missing files and zero-byte files return default without consulting
    on_error. Malformed content follows on_error: RAISE raises
    MalformedDataError from the JSONDecodeError, SKIP returns default, and
    WARN_AND_SKIP logs an assertable warning before returning default.
    """
    if not path.exists():
        return default
    raw = path.read_text(encoding="utf-8")
    if raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        if on_error == MalformedPolicy.RAISE:
            raise MalformedDataError(path) from exc
        if on_error == MalformedPolicy.WARN_AND_SKIP:
            logging.getLogger(__name__).warning(
                "skipping malformed JSON in %s: %s", path, exc
            )
        return default


def read_jsonl(
    path: Path,
    *,
    on_error: MalformedPolicy = MalformedPolicy.RAISE,
) -> list[Any]:
    """Read JSONL records, separating missing/empty from malformed lines.

    Missing files and zero-byte files return []. Malformed lines follow
    on_error: RAISE raises MalformedDataError(path, lineno=...) from the
    JSONDecodeError, SKIP omits the bad line, and WARN_AND_SKIP logs
    warning("skipping malformed line %d in %s", lineno, path) per bad line.
    """
    if not path.exists():
        return []
    out: list[Any] = []
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if on_error == MalformedPolicy.RAISE:
                raise MalformedDataError(path, lineno=lineno) from exc
            if on_error == MalformedPolicy.WARN_AND_SKIP:
                logging.getLogger(__name__).warning(
                    "skipping malformed line %d in %s", lineno, path
                )
    return out


def read_text(path: Path, *, default: str | None = None) -> str | None:
    """Read text or return default when missing.

    Text has no malformed-record policy in this package; callers that need
    validation should parse the returned string explicitly.
    """
    return path.read_text(encoding="utf-8") if path.exists() else default
