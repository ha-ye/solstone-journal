# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy for the chat surface (apps/chat + convey chat-bar)."""

# fmt: off
# T1.3 — owner-language talent labels (CMO subagent voice pass, 2026-05-26)
TALENT_LABEL_READ_RUNNING = "Reading your journal…"
TALENT_LABEL_READ_FINISHED = "Read your journal"
TALENT_LABEL_READ_ERRORED = "Couldn't finish reading your journal"
TALENT_LABEL_EXEC_RUNNING = "Making that change…"
TALENT_LABEL_EXEC_FINISHED = "Made the change"
TALENT_LABEL_EXEC_ERRORED = "Couldn't finish the change"
TALENT_LABEL_SUPPORT_RUNNING = "Reaching solstone support…"
TALENT_LABEL_SUPPORT_FINISHED = "Reached solstone support"
TALENT_LABEL_SUPPORT_ERRORED = "Couldn't reach solstone support"

# T1.4 — queue depth indicators (lowercase "sol" per system-anatomy canon)
CHAT_QUEUE_INDICATOR_SINGULAR = "1 message waiting"
CHAT_QUEUE_INDICATOR_PLURAL_FORMAT = "{count} messages waiting"
CHAT_QUEUE_DEPTH_CAP_MESSAGE = "Give sol a moment to catch up — you have 10 messages waiting."

# T1.1 — liveness placeholder bubble
CHAT_LIVENESS_THINKING = "sol is thinking…"
CHAT_LIVENESS_TASK_FORMAT = "{label} {task}"

# T2.2 — closer framing (CPO LOCKED)
CHAT_CLOSER_LOOP_EXHAUSTED_PREFIX = "Here's what I have so far:"
CHAT_CLOSER_DIFFERENT_ANGLE_SUFFIX = "Want me to try a different angle?"
CHAT_CLOSER_TALENT_ERRORED_FORMAT = "I couldn't finish that lookup — {reason}. Want to try a different angle, or rephrase the question?"
CHAT_CLOSER_TALENT_ERRORED_GENERIC = "I couldn't finish that lookup. Want to try a different angle, or rephrase the question?"

# T2.4 — thinking summary surfaces (CPO LOCKED)
CHAT_THINKING_EXPANDER_LABEL = "Show thinking"
CHAT_THINKING_COLLAPSER_LABEL = "Hide thinking"
CHAT_ERROR_DETAIL_EXPANDER_LABEL = "Show details"
CHAT_ERROR_DETAIL_COLLAPSER_LABEL = "Hide details"
CHAT_THINKING_SETTING_LABEL = "Thinking surfaces"
CHAT_THINKING_OPT_ON_TAP = "Show on tap"
CHAT_THINKING_OPT_ALWAYS = "Always show"
CHAT_THINKING_OPT_NEVER = "Never show"
CHAT_THINKING_SETTING_HELP = "sol does some thinking before replying. Choose how much you want to see."

# Deterministic support-offer gate (backend-emitted; rides the sol_message text).
# Brand rule: "solstone support", never "sol pbc".
CHAT_OFFER_SUPPORT_PROMPT = "Sounds like something's not working — want me to bring in solstone support?"
CHAT_OFFER_SUPPORT_DECLINE = "Okay — I'll keep this local. Tell me if you'd like me to bring in solstone support after all."
# fmt: on

from typing import Literal

_TALENT_LABELS: dict[tuple[str, str], str] = {
    ("read", "running"): TALENT_LABEL_READ_RUNNING,
    ("read", "finished"): TALENT_LABEL_READ_FINISHED,
    ("read", "errored"): TALENT_LABEL_READ_ERRORED,
    ("exec", "running"): TALENT_LABEL_EXEC_RUNNING,
    ("exec", "finished"): TALENT_LABEL_EXEC_FINISHED,
    ("exec", "errored"): TALENT_LABEL_EXEC_ERRORED,
    ("support", "running"): TALENT_LABEL_SUPPORT_RUNNING,
    ("support", "finished"): TALENT_LABEL_SUPPORT_FINISHED,
    ("support", "errored"): TALENT_LABEL_SUPPORT_ERRORED,
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
