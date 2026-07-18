# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from pathlib import Path

from solstone.convey import copy


def test_console_copy_matches_browser_mirror() -> None:
    text = Path("solstone/convey/static/convey_copy.js").read_text(encoding="utf-8")

    for python_name in copy.__all__:
        if not python_name.startswith("CONVEY_CONSOLE_"):
            continue
        js_name = python_name.removeprefix("CONVEY_")
        assert _js_constant(text, js_name) == getattr(copy, python_name)


def test_non_console_copy_matches_browser_mirror() -> None:
    text = Path("solstone/convey/static/convey_copy.js").read_text(encoding="utf-8")

    assert _js_constant(text, "UNKNOWN_ERROR") == copy.CONVEY_UNKNOWN_ERROR
    assert _js_constant(text, "LOG_READ_FAILED") == copy.CONVEY_LOG_READ_FAILED


def test_manifest_touched_action_and_report_copy_matches_browser_mirror() -> None:
    text = Path("solstone/convey/static/convey_copy.js").read_text(encoding="utf-8")

    names = (
        "CONVEY_ACTION_RECONNECT",
        "CONVEY_ACTION_RESTART",
        "CONVEY_REPORT_ANONYMOUS_LABEL",
        "CONVEY_REPORT_DIAGNOSTIC_REASON_CODE_LABEL",
        "CONVEY_REPORT_DIAGNOSTIC_TIME_LABEL",
        "CONVEY_REPORT_ACTION_SEND",
        "CONVEY_REPORT_ACTION_SENDING",
        "CONVEY_REPORT_ACTION_CANCEL",
        "CONVEY_REPORT_ACTION_CLOSE",
        "CONVEY_REPORT_ACTION_OPEN_EMAIL",
        "CONVEY_REPORT_ACTION_VIEW_TICKET",
        "CONVEY_REPORT_SUCCESS_BODY",
        "CONVEY_REPORT_FALLBACK_BODY",
        "CONVEY_REPORT_BUTTON_LABEL",
    )

    for python_name in names:
        js_name = python_name.removeprefix("CONVEY_")
        assert _js_constant(text, js_name) == getattr(copy, python_name)


def _js_constant(text: str, name: str) -> str:
    match = re.search(rf'{name}: "((?:[^"\\]|\\.)*)"', text)
    assert match is not None, name
    return json.loads(f'"{match.group(1)}"')
