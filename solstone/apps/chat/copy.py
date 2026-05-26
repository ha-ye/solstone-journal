# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy for the chat surface (apps/chat + convey chat-bar)."""

# fmt: off
# T1.3 — owner-language talent labels (CMO subagent voice pass, 2026-05-26)
TALENT_LABEL_EXEC_RUNNING = "Looking in your journal…"
TALENT_LABEL_EXEC_FINISHED = "Looked in your journal"
TALENT_LABEL_EXEC_ERRORED = "Couldn't finish looking in your journal"
TALENT_LABEL_REFLECTION_RUNNING = "Reflecting…"
TALENT_LABEL_REFLECTION_FINISHED = "Reflected"
TALENT_LABEL_REFLECTION_ERRORED = "Couldn't finish reflecting"

# T1.4 — queue depth indicators (lowercase "sol" per system-anatomy canon)
CHAT_QUEUE_INDICATOR_SINGULAR = "1 message waiting"
CHAT_QUEUE_INDICATOR_PLURAL_FORMAT = "{count} messages waiting"
CHAT_QUEUE_DEPTH_CAP_MESSAGE = "Give sol a moment to catch up — you have 10 messages waiting."
# fmt: on

from typing import Literal

_TALENT_LABELS: dict[tuple[str, str], str] = {
    ("exec", "running"): TALENT_LABEL_EXEC_RUNNING,
    ("exec", "finished"): TALENT_LABEL_EXEC_FINISHED,
    ("exec", "errored"): TALENT_LABEL_EXEC_ERRORED,
    ("reflection", "running"): TALENT_LABEL_REFLECTION_RUNNING,
    ("reflection", "finished"): TALENT_LABEL_REFLECTION_FINISHED,
    ("reflection", "errored"): TALENT_LABEL_REFLECTION_ERRORED,
}


def talent_label_for(
    target: str, status: Literal["running", "finished", "errored"]
) -> str:
    """Return owner-facing label for (target, status). Raises ValueError on unknown."""
    try:
        return _TALENT_LABELS[(target, status)]
    except KeyError:
        raise ValueError(
            f"no chat talent label for target={target!r} status={status!r}"
        )
