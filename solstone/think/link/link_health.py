# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Structured relay health constants shared with the link surface."""

REASON_HOME_MISSING_MOBILE = "home_missing_mobile"
REASON_SERVICE_TOKEN_REJECTED = "service_token_rejected"
REASON_RELAY_TUNNEL_REJECTED = "relay_tunnel_rejected"
REASON_RELAY_TUNNEL_UNREACHABLE = "relay_tunnel_unreachable"
REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE = "local_private_listener_unreachable"
REASON_RELAY_ADMISSION_SATURATED = "relay_admission_saturated"

OFFLINE_TUNNEL_REASONS = frozenset(
    {
        REASON_SERVICE_TOKEN_REJECTED,
        REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE,
    }
)

LINK_HEALTH_EVENT = "health"
