# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from pathlib import Path

import pytest

from solstone.apps.chat import copy as chat_copy


def _extract_object_literal(text: str, marker: str) -> dict:
    start = text.index(marker) + len(marker)
    depth = 0
    in_string = False
    escaped = False
    object_start = None

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if object_start is None:
                object_start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and object_start is not None:
                return json.loads(text[object_start : index + 1])

    raise AssertionError(f"Could not extract object after marker {marker!r}")


def test_talent_label_for_all_known_combinations():
    expected = {
        ("exec", "running"): chat_copy.TALENT_LABEL_EXEC_RUNNING,
        ("exec", "finished"): chat_copy.TALENT_LABEL_EXEC_FINISHED,
        ("exec", "errored"): chat_copy.TALENT_LABEL_EXEC_ERRORED,
        ("reflection", "running"): chat_copy.TALENT_LABEL_REFLECTION_RUNNING,
        ("reflection", "finished"): chat_copy.TALENT_LABEL_REFLECTION_FINISHED,
        ("reflection", "errored"): chat_copy.TALENT_LABEL_REFLECTION_ERRORED,
    }

    for (target, status), label in expected.items():
        assert chat_copy.talent_label_for(target, status) == label


def test_talent_label_for_unknown_values_raise():
    with pytest.raises(ValueError, match="no chat talent label"):
        chat_copy.talent_label_for("search", "running")

    with pytest.raises(ValueError, match="no chat talent label"):
        chat_copy.talent_label_for("exec", "queued")


def test_js_parity():
    js_path = Path("solstone/convey/static/chat_copy.js")
    text = js_path.read_text(encoding="utf-8")
    js_labels = _extract_object_literal(text, "const TALENT_LABELS = ")

    assert js_labels == {
        "exec": {
            "running": chat_copy.TALENT_LABEL_EXEC_RUNNING,
            "finished": chat_copy.TALENT_LABEL_EXEC_FINISHED,
            "errored": chat_copy.TALENT_LABEL_EXEC_ERRORED,
        },
        "reflection": {
            "running": chat_copy.TALENT_LABEL_REFLECTION_RUNNING,
            "finished": chat_copy.TALENT_LABEL_REFLECTION_FINISHED,
            "errored": chat_copy.TALENT_LABEL_REFLECTION_ERRORED,
        },
    }
    assert (
        f'CHAT_QUEUE_INDICATOR_SINGULAR: "{chat_copy.CHAT_QUEUE_INDICATOR_SINGULAR}"'
        in text
    )
    assert (
        "CHAT_QUEUE_INDICATOR_PLURAL_FORMAT: "
        f'"{chat_copy.CHAT_QUEUE_INDICATOR_PLURAL_FORMAT}"'
    ) in text
    assert (
        f'CHAT_QUEUE_DEPTH_CAP_MESSAGE: "{chat_copy.CHAT_QUEUE_DEPTH_CAP_MESSAGE}"'
        in text
    )
