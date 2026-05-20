# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Guard against regressing the httpx URL-leak silencing.

httpx logs the full request URL at INFO. Gemini API keys ride in `?key=...`
on every call, so any path that lets httpx INFO records reach a file handler
leaks live credentials. The package import in `solstone/__init__.py` sets the
httpx logger to WARNING; these tests assert that contract and that it
survives a later `logging.basicConfig(level=INFO)` call.
"""

import logging

import solstone  # noqa: F401 — import for its side effect (logger config)


def test_httpx_logger_silenced_at_import():
    assert logging.getLogger("httpx").level == logging.WARNING


def test_httpx_info_records_suppressed_after_basicconfig():
    logging.basicConfig(level=logging.INFO)
    httpx_logger = logging.getLogger("httpx")
    assert httpx_logger.getEffectiveLevel() >= logging.WARNING

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.DEBUG)
    httpx_logger.addHandler(handler)
    try:
        httpx_logger.info(
            "HTTP Request: GET https://generativelanguage.googleapis.com/v1beta/models?key=AIzaSyTEST"
        )
        httpx_logger.warning("connection error")
    finally:
        httpx_logger.removeHandler(handler)

    levels = [r.levelno for r in records]
    assert logging.INFO not in levels
    assert logging.WARNING in levels
