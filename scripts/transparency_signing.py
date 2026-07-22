#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Minisign integration for transparency ledger objects."""

from __future__ import annotations

import base64
import getpass
import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from scripts.release_candidate_driver import DriverError
from scripts.transparency_core import failure

REQUIRED_MINISIGN_VERSION = "minisign 0.12"
MISSING_MINISIGN_MESSAGE = "transparency-minisign: minisign 0.12 is required; install it with: sudo dnf install minisign"


class TransparencySigner(Protocol):
    def check(self) -> None: ...

    def sign_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        trusted_comment: str,
    ) -> None: ...

    def verify_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        expected_trusted_comment: str,
    ) -> None: ...

    def trusted_comment(self, signature_path: Path) -> str: ...


def check_minisign_binary(minisign: str = "minisign") -> str:
    if shutil.which(minisign) is None:
        raise DriverError(
            [
                failure(
                    MISSING_MINISIGN_MESSAGE,
                    expected=REQUIRED_MINISIGN_VERSION,
                    actual="missing",
                    repair="sudo dnf install minisign",
                )
            ]
        )
    result = subprocess.run(
        [minisign, "-v"],
        capture_output=True,
        text=True,
        check=False,
    )
    observed = (result.stdout + result.stderr).strip().splitlines()[0:1]
    version = observed[0] if observed else "<empty>"
    if result.returncode != 0 or version != REQUIRED_MINISIGN_VERSION:
        raise DriverError(
            [
                failure(
                    "transparency-minisign: minisign 0.12 is required",
                    expected=REQUIRED_MINISIGN_VERSION,
                    actual=version,
                    repair="sudo dnf install minisign",
                )
            ]
        )
    return version


@dataclass
class LocalMinisignSigner:
    secret_key: Path
    public_key: Path
    minisign: str = "minisign"
    _passphrase: str | None = field(default=None, init=False, repr=False)
    fallback_prompt_count: int = field(default=0, init=False)

    def check(self) -> None:
        check_minisign_binary(self.minisign)
        if not self.secret_key.is_file():
            raise DriverError(
                [
                    failure(
                        "transparency minisign secret key is missing",
                        expected=str(self.secret_key),
                        actual="missing",
                        repair="set TRANSPARENCY_MINISIGN_KEY to the encrypted secret key",
                    )
                ]
            )
        if not self.public_key.is_file():
            raise DriverError(
                [
                    failure(
                        "transparency minisign public key is missing",
                        expected=str(self.public_key),
                        actual="missing",
                        repair="set TRANSPARENCY_MINISIGN_PUB to the pinned public key",
                    )
                ]
            )

    def _read_passphrase(self) -> str:
        if self._passphrase is None:
            self._passphrase = getpass.getpass("Transparency minisign passphrase: ")
        return self._passphrase

    def sign_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        trusted_comment: str,
    ) -> None:
        passphrase = self._read_passphrase()
        signature_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                self.minisign,
                "-S",
                "-s",
                str(self.secret_key),
                "-m",
                str(message_path),
                "-t",
                trusted_comment,
                "-x",
                str(signature_path),
            ],
            input=f"{passphrase}\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise DriverError(
                [
                    failure(
                        "transparency minisign signing failed",
                        expected="minisign -S exit 0",
                        actual=(
                            result.stderr or result.stdout or str(result.returncode)
                        ).strip(),
                        repair="retry with the correct encrypted-key passphrase",
                    )
                ]
            )

    def verify_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        expected_trusted_comment: str,
    ) -> None:
        result = subprocess.run(
            [
                self.minisign,
                "-V",
                "-Q",
                "-p",
                str(self.public_key),
                "-m",
                str(message_path),
                "-x",
                str(signature_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        comment = result.stdout.strip()
        if result.returncode != 0:
            raise DriverError(
                [
                    failure(
                        "transparency minisign verification failed",
                        expected="valid minisign signature",
                        actual=(
                            result.stderr or result.stdout or str(result.returncode)
                        ).strip(),
                        repair="re-sign the transparency object with the pinned key",
                    )
                ]
            )
        if comment != expected_trusted_comment:
            raise DriverError(
                [
                    failure(
                        "transparency minisign trusted comment mismatch",
                        expected=expected_trusted_comment,
                        actual=comment,
                        repair="re-sign the transparency object with the fixed trusted comment",
                    )
                ]
            )

    def trusted_comment(self, signature_path: Path) -> str:
        lines = signature_path.read_text(encoding="utf-8").splitlines()
        if len(lines) < 3:
            raise DriverError(
                [
                    failure(
                        "transparency minisign file is malformed",
                        expected="trusted comment on line 3",
                        actual=f"{len(lines)} lines",
                        repair="re-sign the transparency object with minisign 0.12",
                    )
                ]
            )
        return lines[2]


@dataclass(frozen=True)
class FakeTransparencySigner:
    public_key: str = "fake-public"

    def check(self) -> None:
        return None

    def sign_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        trusted_comment: str,
    ) -> None:
        body = message_path.read_bytes()
        digest = hashlib.sha256(body + b"\n" + trusted_comment.encode("utf-8")).digest()
        signature_path.parent.mkdir(parents=True, exist_ok=True)
        signature_path.write_text(
            "\n".join(
                (
                    "untrusted comment: fake transparency signature",
                    base64.b64encode(digest).decode("ascii"),
                    trusted_comment,
                    f"trusted comment signature: {hashlib.sha256(digest).hexdigest()}",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def verify_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        expected_trusted_comment: str,
    ) -> None:
        lines = signature_path.read_text(encoding="utf-8").splitlines()
        if len(lines) < 3:
            raise DriverError(
                [
                    failure(
                        "fake transparency signature is malformed",
                        expected="trusted comment on line 3",
                        actual=f"{len(lines)} lines",
                        repair="re-sign the transparency object",
                    )
                ]
            )
        comment = lines[2]
        if comment != expected_trusted_comment:
            raise DriverError(
                [
                    failure(
                        "fake transparency trusted comment mismatch",
                        expected=expected_trusted_comment,
                        actual=comment,
                        repair="re-sign the transparency object with the fixed trusted comment",
                    )
                ]
            )
        expected = base64.b64encode(
            hashlib.sha256(
                message_path.read_bytes() + b"\n" + comment.encode("utf-8")
            ).digest()
        ).decode("ascii")
        if lines[1] != expected:
            raise DriverError(
                [
                    failure(
                        "fake transparency signature digest mismatch",
                        expected=expected,
                        actual=lines[1],
                        repair="re-sign the transparency object",
                    )
                ]
            )

    def trusted_comment(self, signature_path: Path) -> str:
        lines = signature_path.read_text(encoding="utf-8").splitlines()
        if len(lines) < 3:
            raise DriverError(
                [
                    failure(
                        "fake transparency signature is malformed",
                        expected="trusted comment on line 3",
                        actual=f"{len(lines)} lines",
                        repair="re-sign the transparency object",
                    )
                ]
            )
        return lines[2]
