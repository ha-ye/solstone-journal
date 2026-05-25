# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Classify service log records into display severity levels."""

from __future__ import annotations

import re

LogLevel = str

_ERROR_TOKEN_RE = re.compile(r"\b(?:ERROR|CRITICAL|FATAL)\b")
_TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\):")
_SHARED_LIBRARY_ERROR_RE = re.compile(r": error while loading shared libraries:")
_SOLSTONE_NOT_RUNNING_RE = re.compile(r"\bsolstone isn't running\b")
_WARNING_TOKEN_RE = re.compile(r"\b(?:WARNING|WARN)\b")
_USER_WARNING_RE = re.compile(r"\bUserWarning\b")
_LLAMA_WARNING_RE = re.compile(r"^\S+\s+W\s+")
_UNREACHABLE_RE = re.compile(r"\b(?:not reachable|Connection refused)\b")
_DEBUG_TOKEN_RE = re.compile(r"\bDEBUG\b")
_INFO_TOKEN_RE = re.compile(r"\bINFO\b")
_LLAMA_INFO_RE = re.compile(r"^\S+\s+I\s+")


def classify_level(stream: str, line: str) -> LogLevel:
    """Return ``error``, ``warning``, ``info``, or ``debug`` for a log line."""
    normalized_stream = (stream or "").strip().lower()
    text = line or ""

    if normalized_stream == "stderr" and _ERROR_TOKEN_RE.search(text):
        return "error"
    if _TRACEBACK_RE.search(text):
        return "error"
    if _SHARED_LIBRARY_ERROR_RE.search(text):
        return "error"
    if _SOLSTONE_NOT_RUNNING_RE.search(text):
        return "error"
    if _ERROR_TOKEN_RE.search(text):
        return "error"

    if _WARNING_TOKEN_RE.search(text):
        return "warning"
    if _USER_WARNING_RE.search(text):
        return "warning"
    if _LLAMA_WARNING_RE.search(text):
        return "warning"
    if _UNREACHABLE_RE.search(text):
        return "warning"

    if _DEBUG_TOKEN_RE.search(text):
        return "debug"

    if _INFO_TOKEN_RE.search(text):
        return "info"
    if _LLAMA_INFO_RE.search(text):
        return "info"

    return "info"
