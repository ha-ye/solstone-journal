# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Credential lookup for the journal push relay."""

import os

from solstone.think.journal_config import read_journal_config


def push_relay_token() -> str | None:
    block = read_journal_config().get("services", {}).get("push", {})
    token = block.get("relay_token")
    if isinstance(token, str) and token:
        return token
    env_token = os.getenv("PUSH_RELAY_SECRET")
    return env_token if env_token else None
