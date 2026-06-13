# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Canonical journal service start entry point."""

from __future__ import annotations


def main() -> None:
    from solstone.think import supervisor

    supervisor.main()
