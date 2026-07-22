from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release_candidate_driver import DriverError
from scripts.transparency_signing import FakeTransparencySigner


def test_fake_signer_verifies_body_and_trusted_comment(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    message = tmp_path / "ledger-entry.json"
    signature = tmp_path / "ledger-entry.json.minisig"
    message.write_bytes(b'{"ok":1}\n')
    signer.sign_file(
        message,
        signature,
        trusted_comment="solpbc-transparency-v1 entry product=solstone-journal seq=1 version=0.0.1 sha256="
        + "a" * 64
        + " prev="
        + "0" * 64,
    )
    signer.verify_file(
        message,
        signature,
        expected_trusted_comment=signer.trusted_comment(signature),
    )


def test_fake_signer_trusted_comment_is_line_three(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    message = tmp_path / "latest.json"
    signature = tmp_path / "latest.json.minisig"
    comment = (
        "solpbc-transparency-v1 latest product=solstone-journal chain_length=1 tip="
    )
    comment += "a" * 64 + " valid_until=2026-08-05T00:00:00Z"
    message.write_bytes(b'{"ok":1}\n')
    signer.sign_file(message, signature, trusted_comment=comment)
    lines = signature.read_text(encoding="utf-8").splitlines()
    assert lines[2] == comment
    assert signer.trusted_comment(signature) == comment


def test_fake_signer_rejects_tampered_message_bytes(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    message = tmp_path / "ledger-entry.json"
    signature = tmp_path / "ledger-entry.json.minisig"
    comment = "solpbc-transparency-v1 entry product=solstone-journal seq=1 version=0.0.1 sha256="
    comment += "a" * 64 + " prev=" + "0" * 64
    message.write_bytes(b'{"ok":1}\n')
    signer.sign_file(message, signature, trusted_comment=comment)
    message.write_bytes(b'{"ok":2}\n')
    with pytest.raises(DriverError) as error:
        signer.verify_file(message, signature, expected_trusted_comment=comment)
    assert (
        error.value.failures[0].error == "fake transparency signature digest mismatch"
    )


def test_fake_signer_rejects_trusted_comment_mismatch(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    message = tmp_path / "latest.json"
    signature = tmp_path / "latest.json.minisig"
    message.write_bytes(b'{"ok":1}\n')
    signer.sign_file(message, signature, trusted_comment="one")
    with pytest.raises(DriverError) as error:
        signer.verify_file(message, signature, expected_trusted_comment="two")
    assert error.value.failures[0].error == "fake transparency trusted comment mismatch"
