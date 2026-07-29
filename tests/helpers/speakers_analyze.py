# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Test helpers for speakers-analyze generation startup seams."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def install_enter_generation_stub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    generation_id: str = "test-speakers-analyze-generation",
) -> None:
    from solstone.think import speakers_analyze_installation as installation

    class _NoopLease:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def release(self) -> None:
            if os.environ.get(installation.GENERATION_ENV_KEY) == generation_id:
                monkeypatch.delenv(installation.GENERATION_ENV_KEY, raising=False)
                monkeypatch.delenv(installation.GENERATION_FD_ENV_KEY, raising=False)
                monkeypatch.delenv(installation.GENERATION_TOKEN_ENV_KEY, raising=False)
            try:
                os.close(self.fd)
            except OSError:
                pass

    def enter_ready_generation(**_kwargs: object):
        token = 1
        fd = os.open(
            tmp_path / "speakers-analyze-generation.fake",
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        os.lseek(fd, token, os.SEEK_SET)
        monkeypatch.setenv(installation.GENERATION_ENV_KEY, generation_id)
        monkeypatch.setenv(installation.GENERATION_FD_ENV_KEY, str(fd))
        monkeypatch.setenv(installation.GENERATION_TOKEN_ENV_KEY, str(token))
        return installation.SpeakersAnalyzeGeneration(
            generation_id=generation_id,
            lease=_NoopLease(fd),
        )

    monkeypatch.setattr(
        installation,
        "enter_speakers_analyze_generation",
        enter_ready_generation,
    )
