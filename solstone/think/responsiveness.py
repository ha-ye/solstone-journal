# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Detect talent outputs that decline the requested work.

Non-responsive means the first prose sentence in a leaf that is not purely an
apology lead-in opens with a first-person capability negation, after explicit
short lead-in markers are stripped; JSON outputs are walked recursively and bare
text is one leaf, with sentence boundaries split only on `.`, `!`, and `?`.

The broader spec also names "answers a question that was not asked"; that arm
is out of reach for a non-model detector and is deliberately not implemented.

Both false-positive and false-negative directions are pinned by the corpus, so
the detector does not claim a general bias toward responsive outputs. The
spec's non-goal is that a mediocre-but-real description ships, so a miss that
withholds real content is the worse error.

Chat copy usually lives in solstone/apps/chat/copy.py and provider-readiness copy
inline in provider_readiness.py; these constants live here because the lode B
anti-asks keep those files closed.

This module is pure stdlib and never calls a model, since the local-lane grader
would be the same model that just declined. Prose-likeness is deliberately loose:
it gates only the empty-corpus flag and never a verdict, so a short token like a
timestamp being counted as prose is harmless by design.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

NON_RESPONSIVE_REASON_CODE = "non_responsive"
# Existing diagnostic previews commonly use 100 chars, e.g.
# solstone/think/detect_transcript.py:212 logs response_text[:100]. A refusal
# opening needs little context, so this cap leaves room for surrounding signal
# without approaching full raw-output logging.
NON_RESPONSIVE_RAW_OUTPUT_CAP_CHARS = 512

# Placeholder copy in constant-first shape: VPX rewords after the copy gate, and
# keeping the strings here makes that a one-file change. The support closer branch
# has no {reason} slot (solstone/convey/chat.py:1835-1839; OUTBOUND_TALENTS at
# :1029), so once lode B marks non_responsive deterministic the batch-#18 case
# never renders the fragment. That honest solstone-voiced branch stands.
NON_RESPONSIVE_OUTPUT_MESSAGE = (
    "The requested work was not completed. Open Thinking to choose a different "
    "engine, then try again."
)
NON_RESPONSIVE_OUTPUT_FRAGMENT = "the thinking engine didn't answer the request"
NON_RESPONSIVE_READINESS_SUMMARY = "the thinking engine isn't answering requests"
NON_RESPONSIVE_READINESS_DETAIL = (
    "Open Thinking to choose a different engine, then try again."
)

_NON_RESPONSIVE_LEAD_INS = (
    "unfortunately",
    "my apologies",
    "i am sorry",
    "i'm sorry",
    "apologies",
    "sorry",
)
_NON_RESPONSIVE_NEGATION_HEADS = (
    "i cannot",
    "i can't",
    "i am not able to",
    "i'm not able to",
    "i am unable to",
    "i'm unable to",
    "i do not have access",
    "i don't have access",
    "i do not have the ability",
    "i don't have the ability",
    "as an ai",
)
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ResponsivenessVerdict:
    non_responsive: bool
    # matched_signal is the private table key, never leaf text, because lode B
    # logs it and must not receive raw model output.
    matched_signal: str | None
    empty_corpus: bool


class NonResponsiveOutputError(RuntimeError):
    # RuntimeError, not ValueError, is deliberate: solstone/think/detect_transcript.py
    # :176/:218 catch (ValueError, json.JSONDecodeError) and would swallow this into
    # []/None, the exact failure shape this arc removes.
    reason_code = NON_RESPONSIVE_REASON_CODE

    def __init__(self) -> None:
        super().__init__(NON_RESPONSIVE_OUTPUT_MESSAGE)


def classify_output_responsiveness(output: object) -> ResponsivenessVerdict:
    prose_leaves = [
        normalized
        for leaf in _string_leaves(output)
        if (normalized := _normalize_text(leaf)) and _is_prose_like(normalized)
    ]

    for leaf in prose_leaves:
        opening = _first_substantive_opening(leaf)
        if opening is None:
            continue
        signal = _matched_negation_head(opening)
        if signal is not None:
            return ResponsivenessVerdict(
                non_responsive=True,
                matched_signal=signal,
                empty_corpus=False,
            )

    return ResponsivenessVerdict(
        non_responsive=False,
        matched_signal=None,
        empty_corpus=not prose_leaves,
    )


def _string_leaves(output: object) -> list[str]:
    if output is None:
        return []
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except ValueError:
            return [output]
        return list(_walk_string_leaves(parsed))
    if isinstance(output, (dict, list)):
        return list(_walk_string_leaves(output))
    return []


def _walk_string_leaves(value: object) -> list[str]:
    leaves: list[str] = []
    if isinstance(value, str):
        leaves.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            leaves.extend(_walk_string_leaves(child))
    elif isinstance(value, list):
        for child in value:
            leaves.extend(_walk_string_leaves(child))
    return leaves


def _normalize_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.replace("\u2019", "'")).strip()


def _is_prose_like(text: str) -> bool:
    return any(char.isalpha() for char in text) and (
        any(char.isspace() for char in text) or any(char in ".!?" for char in text)
    )


def _first_substantive_opening(text: str) -> str | None:
    for sentence in _SENTENCE_BOUNDARY_RE.split(text):
        sentence = sentence.strip()
        if not sentence or not _is_prose_like(sentence):
            continue
        opening = _strip_lead_in(sentence)
        if opening:
            return opening
    return None


def _strip_lead_in(opening: str) -> str:
    current = opening
    while True:
        lowered = current.lower()
        for lead_in in _NON_RESPONSIVE_LEAD_INS:
            if lowered == lead_in:
                return ""
            if lowered.startswith(lead_in):
                rest = current[len(lead_in) :]
                if rest[0].isalnum() or rest[0] == "'":
                    continue
                stripped = rest.lstrip(" \t\r\n,:;-").strip()
                # Guarantees strip-loop termination; every table entry is non-empty today.
                if stripped == current:
                    return current
                current = stripped
                break
        else:
            return current


def _matched_negation_head(opening: str) -> str | None:
    lowered = opening.lower()
    for head in _NON_RESPONSIVE_NEGATION_HEADS:
        if lowered == head:
            return head
        if lowered.startswith(head):
            next_char = lowered[len(head) : len(head) + 1]
            if not next_char or not (next_char.isalpha() or next_char == "'"):
                return head
    return None


__all__ = [
    "NON_RESPONSIVE_OUTPUT_FRAGMENT",
    "NON_RESPONSIVE_OUTPUT_MESSAGE",
    "NON_RESPONSIVE_RAW_OUTPUT_CAP_CHARS",
    "NON_RESPONSIVE_READINESS_DETAIL",
    "NON_RESPONSIVE_READINESS_SUMMARY",
    "NON_RESPONSIVE_REASON_CODE",
    "NonResponsiveOutputError",
    "ResponsivenessVerdict",
    "classify_output_responsiveness",
]
