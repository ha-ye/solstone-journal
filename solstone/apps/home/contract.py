# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client home routes."""

from __future__ import annotations

from solstone.apps.home.routes import _FIRST_WEEK_FRAMING
from solstone.convey.contract import FieldSpec, OperationSpec, ResponseSpec

OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="home.pulse",
        method="GET",
        rule="/app/home/api/pulse",
        summary="Read the home Pulse snapshot",
        description=(
            "Return the home Pulse: a snapshot recomputed per request from journal "
            "state. Only the new-owner onboarding signals are named in this contract "
            "— journal_age_days, home_state, and welcome_framing; the rest of the "
            "payload is intentionally open and may change without a contract bump."
        ),
        responses=(
            ResponseSpec(
                status=200,
                description=(
                    "Home Pulse snapshot. Fields beyond the three named here are "
                    "present but not contracted."
                ),
                named_fields=(
                    FieldSpec(
                        "journal_age_days",
                        "integer",
                        required=True,
                        description=(
                            "Whole days from the earliest chronicle day to today; 0 "
                            "when no chronicle day exists yet."
                        ),
                    ),
                    FieldSpec(
                        "home_state",
                        "string",
                        required=True,
                        raw_schema={
                            "type": "string",
                            "enum": ["welcome", "active"],
                            "description": (
                                "Home onboarding posture: 'welcome' until the home "
                                "surface has any narrative, activity, anticipated item, "
                                "briefing, attention item, pulse need, or weekly "
                                "reflection; 'active' once any exists."
                            ),
                        },
                    ),
                    FieldSpec(
                        "welcome_framing",
                        "string",
                        required=True,
                        raw_schema={
                            "type": ["string", "null"],
                            "description": (
                                "First-week framing line for new owners: the framing "
                                "text when home_state is 'welcome' and journal_age_days "
                                "<= 7, otherwise null."
                            ),
                        },
                    ),
                ),
                example={
                    "journal_age_days": 0,
                    "home_state": "welcome",
                    "welcome_framing": _FIRST_WEEK_FRAMING,
                },
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
