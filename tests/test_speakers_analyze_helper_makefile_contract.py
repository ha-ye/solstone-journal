# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"


def _makefile_text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _recipe_body(makefile: str, target: str) -> str:
    match = re.search(
        rf"^{re.escape(target)}:[^\n]*\n(?P<body>(?:\t[^\n]*\n)+)",
        makefile,
        re.MULTILINE,
    )
    assert match is not None, f"Makefile missing {target} recipe"
    return match.group("body")


def test_speakers_analyze_helper_target_is_phony_and_defined() -> None:
    makefile = _makefile_text()
    phony = next(
        line for line in makefile.splitlines() if line.startswith(".PHONY:")
    ).split()

    assert "speakers-analyze-helper" in phony
    body = _recipe_body(makefile, "speakers-analyze-helper")
    assert 'test -x "$(VENV_BIN)/python"' in body
    assert "$(VENV_BIN)/python scripts/install_speakers_analyze_helper.py" in body


def test_install_invokes_helper_after_platform_block_before_install_models() -> None:
    body = _recipe_body(_makefile_text(), "install")

    platform_block_end = body.index("\tfi\n")
    helper_index = body.index("$(MAKE) speakers-analyze-helper")
    install_models_index = body.index("$(VENV_BIN)/journal install-models")

    assert platform_block_end < helper_index < install_models_index

    helper_line = next(
        line for line in body.splitlines() if "$(MAKE) speakers-analyze-helper" in line
    )
    assert helper_line.startswith("\t@$(MAKE) speakers-analyze-helper")
    command = helper_line[1:]
    assert not command.startswith("-")
    assert not command.startswith("@-")
    assert "|| true" not in helper_line
