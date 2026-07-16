# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Migrate legacy pairing host URLs to bare home addresses."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from solstone.think.journal_config import (
    hold_config_lock,
    read_journal_config,
    write_journal_config,
)
from solstone.think.pairing.config import InvalidHomeAddress, validate_home_address
from solstone.think.utils import get_journal


def _legacy_host_url_to_home_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme != "http" or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        return None
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return None
    if host is None or port is None:
        return None
    try:
        return validate_home_address(f"{host}:{port}")
    except InvalidHomeAddress:
        return None


def migrate(config: dict[str, Any]) -> bool:
    pairing = config.get("pairing")
    if not isinstance(pairing, dict) or "host_url" not in pairing:
        return False

    home_address = _legacy_host_url_to_home_address(pairing.get("host_url"))
    if home_address is not None:
        pairing["home_address"] = home_address

    pairing.pop("host_url")
    return True


def main() -> None:
    journal = get_journal()
    with hold_config_lock(journal):
        config = read_journal_config(journal)
        if not migrate(config):
            print("Pairing home address already migrated.")
            return
        write_journal_config(config, journal)
    print("Migrated pairing home address config.")


if __name__ == "__main__":
    main()
