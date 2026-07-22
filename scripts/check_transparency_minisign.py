#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Exercise the real minisign transparency signing path."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol, Sequence
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_candidate_driver import DriverError  # noqa: E402
from scripts.transparency_core import failure  # noqa: E402
from scripts.transparency_signing import (  # noqa: E402
    LocalMinisignSigner,
    check_minisign_binary,
)

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "transparency"
ENTRY_FIXTURE = FIXTURE_DIR / "canonical-entry-v1.json"
ENTRY_TRUSTED_COMMENT = FIXTURE_DIR / "entry-trusted-comment.txt"


class _Verifier(Protocol):
    def verify_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        expected_trusted_comment: str,
    ) -> None: ...


def _print_failures(error: DriverError) -> None:
    for item in error.failures:
        print(f"ERROR: {item.error}", file=sys.stderr)
        print(f"  expected: {item.expected}", file=sys.stderr)
        print(f"  actual: {item.actual}", file=sys.stderr)
        print(f"  repair: {item.repair}", file=sys.stderr)


def _run_minisign(args: Sequence[str], *, input_text: str) -> None:
    result = subprocess.run(
        ["minisign", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise DriverError(
            [
                failure(
                    "transparency minisign key generation failed",
                    expected="minisign -G exit 0",
                    actual=(
                        result.stderr or result.stdout or str(result.returncode)
                    ).strip(),
                    repair="retry after confirming the minisign binary works locally",
                )
            ]
        )


def _generate_keypair(stage: Path, *, passphrase: str) -> tuple[Path, Path]:
    secret_key = stage / "secret.key"
    public_key = stage / "public.key"
    _run_minisign(
        ["-G", "-s", str(secret_key), "-p", str(public_key)],
        input_text=f"{passphrase}\n{passphrase}\n",
    )
    return secret_key, public_key


def _tamper_one_byte(payload: bytes) -> bytes:
    if not payload:
        raise DriverError(
            [
                failure(
                    "transparency minisign tamper fixture is empty",
                    expected="non-empty message bytes",
                    actual="0 bytes",
                    repair="restore tests/fixtures/transparency/canonical-entry-v1.json",
                )
            ]
        )
    return bytes((payload[0] ^ 0x01,)) + payload[1:]


def _assert_tampered_verify_fails(
    verifier: _Verifier,
    message_path: Path,
    signature_path: Path,
    *,
    expected_trusted_comment: str,
) -> None:
    tampered_path = message_path.with_name(f"{message_path.name}.tampered")
    tampered_path.write_bytes(_tamper_one_byte(message_path.read_bytes()))
    try:
        verifier.verify_file(
            tampered_path,
            signature_path,
            expected_trusted_comment=expected_trusted_comment,
        )
    except DriverError:
        return
    raise DriverError(
        [
            failure(
                "transparency minisign tampered message verified",
                expected="tampered message verification fails",
                actual="verification succeeded",
                repair="inspect minisign verification and trusted-comment handling",
            )
        ]
    )


def run_gate() -> None:
    check_minisign_binary()
    passphrase = "transparency-minisign-check"
    with tempfile.TemporaryDirectory(prefix="transparency-minisign-") as tmp:
        stage = Path(tmp)
        message_path = stage / ENTRY_FIXTURE.name
        comment_path = stage / ENTRY_TRUSTED_COMMENT.name
        shutil.copy2(ENTRY_FIXTURE, message_path)
        shutil.copy2(ENTRY_TRUSTED_COMMENT, comment_path)
        trusted_comment = comment_path.read_text(encoding="utf-8").rstrip("\n")
        secret_key, public_key = _generate_keypair(stage, passphrase=passphrase)
        signature_path = stage / f"{message_path.name}.minisig"
        signer = LocalMinisignSigner(secret_key=secret_key, public_key=public_key)
        with patch("getpass.getpass", return_value=passphrase):
            signer.sign_file(
                message_path,
                signature_path,
                trusted_comment=trusted_comment,
            )
        signer.verify_file(
            message_path,
            signature_path,
            expected_trusted_comment=trusted_comment,
        )
        extracted_comment = signer.trusted_comment(signature_path)
        if extracted_comment != trusted_comment:
            raise DriverError(
                [
                    failure(
                        "transparency minisign trusted comment extraction mismatch",
                        expected=trusted_comment,
                        actual=extracted_comment,
                        repair="inspect LocalMinisignSigner.trusted_comment",
                    )
                ]
            )
        _assert_tampered_verify_fails(
            signer,
            message_path,
            signature_path,
            expected_trusted_comment=trusted_comment,
        )


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Check real transparency minisign signing."
    )


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        run_gate()
    except DriverError as exc:
        _print_failures(exc)
        return 1
    print("transparency minisign check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
