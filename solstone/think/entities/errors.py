# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Typed exceptions for entity write owner operations."""


class EntityWriteError(Exception):
    """Base class for entity write owner errors."""


class EntityExistsError(EntityWriteError):
    """Raised when an entity already exists in the requested scope."""


class EntityBlockedError(EntityWriteError):
    """Raised when a write targets a blocked entity."""


class EntityNotFoundError(EntityWriteError):
    """Raised when a write target cannot be found."""


class AkaConflictError(EntityWriteError):
    """Raised when an alias would collide with another entity."""

    alias: str
    conflict_name: str

    def __init__(self, alias: str, conflict_name: str) -> None:
        """Build an alias conflict carrying both names for adapters."""
        super().__init__(f"Alias '{alias}' conflicts with entity '{conflict_name}'.")
        self.alias = alias
        self.conflict_name = conflict_name
