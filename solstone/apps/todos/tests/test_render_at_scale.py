# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from solstone.apps.todos import copy as todos_copy
from solstone.convey import create_app

CHECK_EMOJI = "\u2705"
STAR_KEYCAP = "*\ufe0f\u20e3"
ZERO_KEYCAP = "0\ufe0f\u20e3"


class TodosHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.todo_rows: list[dict] = []
        self.overflow_buttons: list[dict] = []
        self.facet_sections: list[dict] = []
        self.empty_states: list[dict] = []
        self._current_button: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        classes = attr_map.get("class", "").split()

        if tag == "li" and "todo-row" in classes and "data-todo-row" in attr_map:
            self.todo_rows.append({"classes": classes, "attrs": attr_map})
        elif tag == "button" and "todo-overflow-btn" in classes:
            button = {"attrs": attr_map, "text": ""}
            self.overflow_buttons.append(button)
            self._current_button = button
        elif tag == "div" and "facet-section" in classes:
            self.facet_sections.append({"classes": classes, "attrs": attr_map})
        elif tag == "div" and "todo-facet-empty" in classes:
            self.empty_states.append({"classes": classes, "attrs": attr_map})

    def handle_data(self, data: str) -> None:
        if self._current_button is not None:
            self._current_button["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            self._current_button = None


def _parse(html: str) -> TodosHTMLParser:
    parser = TodosHTMLParser()
    parser.feed(html)
    return parser


@pytest.fixture
def todos_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    def _create():
        app = create_app(journal=str(tmp_path))
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session.permanent = True
        return client

    return _create


def _ensure_facet(root: Path, facet: str) -> None:
    facet_dir = root / "facets" / facet
    facet_dir.mkdir(parents=True, exist_ok=True)
    (facet_dir / "facet.json").write_text(
        json.dumps(
            {
                "title": facet.title(),
                "description": f"{facet} facet",
                "color": "#6b7280",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_todos(
    root: Path,
    facet: str,
    day: str,
    *,
    incomplete: int = 0,
    completed: int = 0,
) -> None:
    _ensure_facet(root, facet)
    todos_dir = root / "facets" / facet / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {"text": f"Open task {index}", "created_at": 1704067200000 + index}
        for index in range(1, incomplete + 1)
    ]
    entries.extend(
        {
            "text": f"Done task {index}",
            "completed": True,
            "created_at": 1704077200000 + index,
        }
        for index in range(1, completed + 1)
    )
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    (todos_dir / f"{day}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _get_page(client, day: str) -> tuple[str, TodosHTMLParser]:
    response = client.get(f"/app/todos/{day}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    return html, _parse(html)


def test_initial_render_caps_at_budget_per_facet(tmp_path: Path, todos_client) -> None:
    day = "20260415"
    _write_todos(tmp_path, "personal", day, incomplete=50, completed=10)
    _html, parser = _get_page(todos_client(), day)

    incomplete_rows = [
        row for row in parser.todo_rows if "completed" not in row["classes"]
    ]
    completed_rows = [row for row in parser.todo_rows if "completed" in row["classes"]]
    assert len(incomplete_rows) == 30
    assert len(completed_rows) == 5


def test_small_facet_renders_all_no_overflow(tmp_path: Path, todos_client) -> None:
    day = "20260416"
    _write_todos(tmp_path, "personal", day, incomplete=10, completed=3)
    _html, parser = _get_page(todos_client(), day)

    assert len(parser.todo_rows) == 13
    assert parser.overflow_buttons == []


def test_overflow_button_exact_text_and_aria(tmp_path: Path, todos_client) -> None:
    client = todos_client()

    _write_todos(tmp_path, "work", "20260417", incomplete=50)
    _html, parser = _get_page(client, "20260417")
    incomplete_button = parser.overflow_buttons[0]
    assert incomplete_button["attrs"]["data-section"] == "incomplete"
    assert incomplete_button["text"].strip() == "Show 20 more"
    assert incomplete_button["attrs"]["aria-label"] == "Show 20 more todos in work"

    _write_todos(tmp_path, "work", "20260418", incomplete=31)
    _html, parser = _get_page(client, "20260418")
    assert parser.overflow_buttons[0]["text"].strip() == "Show 1 more"

    _write_todos(tmp_path, "work", "20260419", completed=10)
    _html, parser = _get_page(client, "20260419")
    completed_button = parser.overflow_buttons[0]
    assert completed_button["attrs"]["data-section"] == "completed"
    assert completed_button["text"].strip() == "Show 5 more completed"
    assert (
        completed_button["attrs"]["aria-label"] == "Show 5 more completed todos in work"
    )

    _write_todos(tmp_path, "work", "20260420", incomplete=30, completed=5)
    _html, parser = _get_page(client, "20260420")
    assert parser.overflow_buttons == []


def test_facet_totals_in_data_attributes(tmp_path: Path, todos_client) -> None:
    day = "20260421"
    _write_todos(tmp_path, "personal", day, incomplete=50, completed=10)
    _html, parser = _get_page(todos_client(), day)

    section = parser.facet_sections[0]
    assert section["attrs"]["data-incomplete-total"] == "50"
    assert section["attrs"]["data-completed-total"] == "10"


def test_emoji_and_pending_text_use_totals(tmp_path: Path, todos_client) -> None:
    client = todos_client()

    _write_todos(tmp_path, "personal", "20260422", incomplete=50)
    html, parser = _get_page(client, "20260422")
    assert STAR_KEYCAP in html
    assert "0 of 50 done" in html
    assert "collapsed" not in parser.facet_sections[0]["classes"]

    _write_todos(tmp_path, "personal", "20260423", completed=35)
    html, parser = _get_page(client, "20260423")
    assert CHECK_EMOJI in html
    assert "all done" in html
    assert "collapsed" in parser.facet_sections[0]["classes"]

    _ensure_facet(tmp_path, "personal")
    html, parser = _get_page(client, "20260424")
    assert ZERO_KEYCAP in html
    assert parser.empty_states
    assert "collapsed" in parser.facet_sections[0]["classes"]


def test_overflow_route_returns_fragment_of_li_rows(
    tmp_path: Path, todos_client
) -> None:
    day = "20260425"
    _write_todos(tmp_path, "personal", day, incomplete=50)
    response = todos_client().get(f"/app/todos/{day}/overflow/personal/incomplete")
    body = response.get_data(as_text=True)
    parser = _parse(body)

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert len(parser.todo_rows) == 20
    assert "<ul" not in body
    assert "<script" not in body
    assert "<style" not in body


def test_overflow_route_completed_section(tmp_path: Path, todos_client) -> None:
    day = "20260426"
    _write_todos(tmp_path, "personal", day, completed=10)
    response = todos_client().get(f"/app/todos/{day}/overflow/personal/completed")
    parser = _parse(response.get_data(as_text=True))

    assert response.status_code == 200
    assert len(parser.todo_rows) == 5


def test_overflow_route_empty_when_at_or_under_budget(
    tmp_path: Path, todos_client
) -> None:
    day = "20260427"
    _write_todos(tmp_path, "personal", day, incomplete=10)
    response = todos_client().get(f"/app/todos/{day}/overflow/personal/incomplete")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == ""


def test_overflow_route_validates_inputs(tmp_path: Path, todos_client) -> None:
    day = "20260428"
    _write_todos(tmp_path, "personal", day, incomplete=50)
    client = todos_client()

    assert (
        client.get("/app/todos/notadate/overflow/personal/incomplete").status_code
        == 404
    )
    assert client.get(f"/app/todos/{day}/overflow/personal/bogus").status_code == 400
    assert (
        client.get(f"/app/todos/{day}/overflow/unknown/incomplete").status_code == 404
    )


def test_copy_constants_used_in_template(tmp_path: Path, todos_client) -> None:
    day = "20260429"
    _write_todos(tmp_path, "personal", day, incomplete=50)
    response = todos_client().get(f"/app/todos/{day}")

    expected = todos_copy.SHOW_MORE_INCOMPLETE.format(count=20)
    assert expected.encode() in response.data


def test_count_stability_under_add_assertion_level(
    tmp_path: Path, todos_client
) -> None:
    day = "20260430"
    _write_todos(tmp_path, "personal", day, incomplete=50)
    _html, parser = _get_page(todos_client(), day)

    section = parser.facet_sections[0]
    incomplete_rows = [
        row for row in parser.todo_rows if "completed" not in row["classes"]
    ]
    assert section["attrs"]["data-incomplete-total"] == "50"
    assert parser.overflow_buttons[0]["text"].strip() == "Show 20 more"
    assert len(incomplete_rows) == 30
