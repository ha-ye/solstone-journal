# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime, timezone

from solstone.convey.backlog_source import BacklogSource


def healthy_backlog_source() -> BacklogSource:
    return BacklogSource(
        backlog={
            "pending_days": 0,
            "stuck_days": 0,
            "days": [],
            "errors": [],
            "degraded": False,
        },
        validity="valid",
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
