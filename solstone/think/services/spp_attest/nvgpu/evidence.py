# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Transform SPP GPU envelopes into nvattest evidence JSON."""

from __future__ import annotations

import base64

from solstone.think.services.spp_attest.tlv import GpuEnvelope


def to_nvattest_evidence(
    envelope: GpuEnvelope, owner_nonce: bytes
) -> list[dict[str, str]]:
    """Return the one-item nvattest serialized-evidence array."""

    return [
        {
            "arch": envelope.field(7).decode("utf-8").upper(),
            "certificate": base64.b64encode(envelope.field(3)).decode("ascii"),
            "evidence": base64.b64encode(envelope.field(2)).decode("ascii"),
            "nonce": owner_nonce.hex(),
        }
    ]
