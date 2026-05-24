# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations


class TlsError(RuntimeError):
    """Raised when the client-side TLS handshake or tunnel aborts."""
