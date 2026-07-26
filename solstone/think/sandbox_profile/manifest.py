# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Manifest constants for the disposable sandbox profile.

The command owns only lifecycle intent inside an already-marked disposable
sandbox journal. Runtime disable intentionally leaves setup, identity, CA, and
directory deletion to the external sandbox harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

CONTRACT_VERSION = 1
PROFILE = "full"
MARKER_KIND = "solstone-disposable-journal"
INTENT_KIND = "solstone.sandbox_profile.intent"

CAPABILITY_SCOUT = "scout"
CAPABILITY_SPL = "spl"
CAPABILITY_SPB = "spb"
CAPABILITY_SPP = "spp"
CAPABILITY_RUNTIME = "runtime"

CAPABILITY_ORDER: tuple[str, ...] = (
    CAPABILITY_SCOUT,
    CAPABILITY_SPL,
    CAPABILITY_SPB,
    CAPABILITY_SPP,
    CAPABILITY_RUNTIME,
)
APPLY_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_SCOUT,
    CAPABILITY_SPL,
    CAPABILITY_SPB,
    CAPABILITY_SPP,
)


@dataclass(frozen=True, slots=True)
class SyntheticOwner:
    setup_completed_at: int
    identity_name: str
    identity_preferred: str
    identity_timezone: str
    journal_name: str
    home_label: str


def synthetic_owner_metadata(run_id: str) -> SyntheticOwner:
    """Return clearly synthetic owner metadata derived only from ``run_id``.

    This is intentionally non-real and deterministic: a repeated prepare for the
    same canonical UUID converges on the same setup and identity fields.
    """

    parsed = UUID(run_id)
    slug = parsed.hex[:12]
    suffix = parsed.hex[-9:]
    return SyntheticOwner(
        setup_completed_at=1_700_000_000_000 + int(suffix, 16) % 1_000_000_000,
        identity_name=f"Synthetic Sandbox Owner {slug}",
        identity_preferred=f"sandbox-run-{slug}",
        identity_timezone="UTC",
        journal_name=f"Synthetic sandbox {slug}",
        home_label=f"sandbox-{slug}",
    )


def supported_contract_payload() -> dict[str, object]:
    return {
        "profile": PROFILE,
        "contract_version": CONTRACT_VERSION,
        "capabilities": list(CAPABILITY_ORDER),
    }
