# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared service-enable constants from solstone.app/account/src/enable-constants.js."""

from __future__ import annotations

import re

NONCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # Crockford-style base32 (no I/L/O), per deployed worker
NONCE_LENGTH_CHARS = 52
NONCE_REGEX = re.compile(r"^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{52}$")

SERVICE_SCOUT = "scout"
SERVICE_SPL = "spl"
SUPPORTED_SERVICES = frozenset({SERVICE_SCOUT, SERVICE_SPL})
