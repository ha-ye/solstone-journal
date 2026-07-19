# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared journal configuration file helpers."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from solstone.think.journal_io.atomic import atomic_replace
from solstone.think.journal_io.locking import hold_lock
from solstone.think.utils import (
    CorruptConfigError,
    _load_default_config,
    _resolve_os_identity,
    _resolve_os_timezone,
    get_config,
    get_journal,
)

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JournalConfigMutation(Generic[T]):
    """Explicit outcome from a journal config mutator."""

    changed: bool
    value: T


@dataclass(frozen=True)
class JournalConfigTransaction(Generic[T]):
    """Result of a completed journal config transaction."""

    value: T
    changed: bool
    written: bool


class JournalConfigPostCommitError(RuntimeError):
    """Raised when config committed but a required secondary effect failed."""

    def __init__(
        self,
        message: str,
        *,
        result: JournalConfigTransaction[Any],
        error: Exception,
    ):
        super().__init__(message)
        self.result = result
        self.error = error


def get_journal_config_path(journal_path: str | Path | None = None) -> Path:
    """Return the canonical journal config path."""

    return Path(journal_path or get_journal()) / "config" / "journal.json"


def read_journal_config(journal_path: str | Path | None = None) -> dict[str, Any]:
    """Read journal config through the canonical config resolver."""

    if journal_path is None:
        return get_config()

    config_path = get_journal_config_path(journal_path)
    if not config_path.exists():
        return copy.deepcopy(_load_default_config())

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise CorruptConfigError(config_path, error=exc) from exc


def _write_journal_config(
    config: dict[str, Any], journal_path: str | Path | None = None
) -> None:
    """Write journal config atomically with stable formatting and private permissions."""

    config_path = get_journal_config_path(journal_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(
        config_path,
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )


@contextmanager
def _hold_config_lock(journal_path: str | Path | None = None) -> Iterator[None]:
    """Hold the journal config read-modify-write lock."""

    with hold_lock(get_journal_config_path(journal_path), mode=0o600):
        yield


def _materialized_default_config() -> dict[str, Any]:
    config = copy.deepcopy(_load_default_config())
    try:
        full_name, login_name = _resolve_os_identity()
    except Exception:
        logger.debug("Failed to resolve OS identity", exc_info=True)
        full_name = ""
        login_name = ""
    try:
        timezone = _resolve_os_timezone()
    except Exception:
        logger.debug("Failed to resolve OS timezone", exc_info=True)
        timezone = ""
    config.setdefault("identity", {})
    config["identity"]["name"] = full_name
    config["identity"]["preferred"] = login_name
    config["identity"]["timezone"] = timezone
    return config


def _read_existing_journal_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise CorruptConfigError(config_path, error=exc) from exc


def mutate_journal_config(
    mutator: Callable[[dict[str, Any]], JournalConfigMutation[T]],
    *,
    journal_path: str | Path | None = None,
) -> JournalConfigTransaction[T]:
    """Mutate journal config under the canonical read-modify-write lock."""

    config_path = get_journal_config_path(journal_path)
    with _hold_config_lock(journal_path):
        materialized = not config_path.exists()
        config = (
            _materialized_default_config()
            if materialized
            else _read_existing_journal_config(config_path)
        )
        mutation = mutator(config)
        written = materialized or mutation.changed
        if written:
            _write_journal_config(config, journal_path)
        return JournalConfigTransaction(
            value=mutation.value,
            changed=mutation.changed,
            written=written,
        )


def ensure_journal_config(
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    """Materialize config/journal.json and return its contents."""

    result = mutate_journal_config(
        lambda config: JournalConfigMutation(
            changed=False,
            value=copy.deepcopy(config),
        ),
        journal_path=journal_path,
    )
    return result.value


__all__ = [
    "JournalConfigMutation",
    "JournalConfigPostCommitError",
    "JournalConfigTransaction",
    "ensure_journal_config",
    "get_journal_config_path",
    "mutate_journal_config",
    "read_journal_config",
]
