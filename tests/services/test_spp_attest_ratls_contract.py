# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.services.spp_attest.ratls.contract import (
    COMPOSITE_FIELDS,
    PROTOCOL_VERSION,
    CompositeEvidence,
    decode_sequence,
    encode_sequence,
    render_contract_artifact,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "solstone"
    / "think"
    / "services"
    / "spp_attest"
    / "ratls"
    / "ratls-contract.json"
)
COMPOSITE_OCTET_COUNT = len(COMPOSITE_FIELDS) - 1


def test_ratls_contract_artifact_matches_generated_constants() -> None:
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render_contract_artifact()


def test_composite_evidence_der_round_trips() -> None:
    fields = [f"field-{index}".encode() for index in range(COMPOSITE_OCTET_COUNT)]
    evidence = CompositeEvidence(*fields)

    assert CompositeEvidence.from_der(evidence.to_der()) == evidence


@pytest.mark.parametrize(
    "payload",
    [
        encode_sequence(PROTOCOL_VERSION, [b"x"] * COMPOSITE_OCTET_COUNT) + b"\x00",
        b"\x31\x00",
        b"\x30\x81\x01\x05",
        b"\x30\x04\x02\x02\x00\x01",
        encode_sequence(PROTOCOL_VERSION, [b"x"]),
        encode_sequence(2, [b"x"] * COMPOSITE_OCTET_COUNT),
    ],
)
def test_composite_evidence_strict_der_rejects_invalid_encodings(
    payload: bytes,
) -> None:
    with pytest.raises(ValueError):
        CompositeEvidence.from_der(payload)


def test_decode_sequence_rejects_wrong_field_count() -> None:
    with pytest.raises(ValueError):
        decode_sequence(encode_sequence(PROTOCOL_VERSION, [b"x"]), 2)
