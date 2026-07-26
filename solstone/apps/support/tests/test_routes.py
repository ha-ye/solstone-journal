# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

WORKSPACE_PATH = Path(__file__).resolve().parents[1] / "workspace.html"
SUPPORT_SENTENCE_START_ALLOWLIST: frozenset[str] = frozenset()


class _SupportHelpCardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards: list[tuple[dict[str, str], str]] = []
        self._help_depth = 0
        self._card_depth = 0
        self._capturing_p = False
        self._seen_card_p = False
        self._current_p_attrs: dict[str, str] = {}
        self._current_p_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        if not self._help_depth:
            if tag == "div" and attr_map.get("id") == "section-help":
                self._help_depth = 1
            return

        self._help_depth += 1
        classes = set(attr_map.get("class", "").split())
        if tag == "div" and "support-help-card" in classes and not self._card_depth:
            self._card_depth = 1
            self._seen_card_p = False
            return

        if self._card_depth:
            self._card_depth += 1
            if tag == "p" and not self._seen_card_p:
                self._capturing_p = True
                self._current_p_attrs = attr_map
                self._current_p_text = []

    def handle_data(self, data: str) -> None:
        if self._capturing_p:
            self._current_p_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capturing_p and tag == "p":
            self.cards.append(
                (self._current_p_attrs, "".join(self._current_p_text).strip())
            )
            self._capturing_p = False
            self._seen_card_p = True

        if self._card_depth:
            self._card_depth -= 1
            if self._card_depth == 0:
                self._seen_card_p = False

        if self._help_depth:
            self._help_depth -= 1


def _support_help_cards(html: str) -> list[tuple[dict[str, str], str]]:
    parser = _SupportHelpCardParser()
    parser.feed(html)
    return parser.cards


def _sentence_start_words(value: str) -> list[str]:
    starts = [0]
    starts.extend(match.end() for match in re.finditer(r"[.!?]\s+", value))
    words = []
    for index in starts:
        while index < len(value) and not value[index].isalpha():
            index += 1
        match = re.match(r"[A-Za-z]+", value[index:])
        if match:
            words.append(match.group(0))
    return words


@pytest.fixture
def app(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1700000000000}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    from solstone.convey import create_app

    app = create_app(journal=str(journal))
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_support_index_serves_injected_spa_shell(client):
    response = client.get("/app/support/")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_support_static_literal_path_resolves(app):
    adapter = app.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/support/static/support.js", method="GET")

    assert endpoint


def test_support_help_card_bodies_start_sentences_lowercase():
    html = WORKSPACE_PATH.read_text(encoding="utf-8")
    cards = _support_help_cards(html)

    assert len(cards) >= 5
    errors = []
    for card_index, (_attrs, body) in enumerate(cards, start=1):
        for word in _sentence_start_words(body):
            if word == "I" or word in SUPPORT_SENTENCE_START_ALLOWLIST:
                continue
            if word[0] != word[0].lower():
                errors.append(f"card {card_index}: sentence starts with {word!r}")

    assert errors == []
