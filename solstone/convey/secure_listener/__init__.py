# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Secure PL listener for paired-device Convey requests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .identity import ConveyIdentity

if TYPE_CHECKING:
    from .runtime import (
        get_authorized_clients,
        start_secure_listener,
        stop_secure_listener,
    )

__all__ = [
    "ConveyIdentity",
    "get_authorized_clients",
    "start_secure_listener",
    "stop_secure_listener",
]


def __getattr__(name: str) -> Any:
    if name in {
        "get_authorized_clients",
        "start_secure_listener",
        "stop_secure_listener",
    }:
        from . import runtime

        return getattr(runtime, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
