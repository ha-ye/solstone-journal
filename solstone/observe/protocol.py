# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Observer ingest wire-protocol constants.

Single source of truth for the observer ingest protocol version, imported by
both ``solstone.observe.transfer`` (producer) and
``solstone.apps.observer.routes`` (consumer route). v1 is legacy/unversioned;
v2 is the enveloped segments response plus header-auth (M2 sibling).
"""

OBSERVER_PROTOCOL_VERSION = 2

# Request header a peer uses to advertise the observer ingest protocol version
# it speaks. Absent/unparsable => treated as v1 (legacy/unversioned).
OBSERVER_PROTOCOL_VERSION_HEADER = "X-Solstone-Protocol-Version"
