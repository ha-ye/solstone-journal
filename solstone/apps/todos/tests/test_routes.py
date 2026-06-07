# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solstone.apps.todos.todo import TodoChecklist
from solstone.convey import create_app
from solstone.think.call import call_app

runner = CliRunner()
STABLE_KEYS = (
    "text",
    "nudge",
    "created_at",
    "completed",
    "cancelled",
    "cancelled_reason",
    "moved_to",
)


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


def _write_todo(root: Path, facet: str, day: str, entry: dict) -> Path:
    _ensure_facet(root, facet)
    todos_dir = root / "facets" / facet / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)
    path = todos_dir / f"{day}.jsonl"
    path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _stable_items(path: Path) -> list[dict]:
    return [
        {key: item[key] for key in STABLE_KEYS if key in item}
        for item in _read_jsonl(path)
    ]


def _client_for_journal(journal: Path):
    app = create_app(journal=str(journal))
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session.permanent = True
    return client


def test_move_same_item_twice_via_route(tmp_path: Path, todos_client) -> None:
    day = "20260415"
    target_day = "20260416"
    facet = "personal"
    _write_todo(
        tmp_path,
        facet,
        day,
        {"text": "Move once", "created_at": 1704067200000},
    )

    client = todos_client()
    payload = {"target_day": target_day, "facet": facet, "index": 1}

    first = client.post(f"/app/todos/{day}/move", json=payload)
    target_path = tmp_path / "facets" / facet / "todos" / f"{target_day}.jsonl"
    assert first.status_code == 200
    assert [item["text"] for item in _read_jsonl(target_path)] == ["Move once"]

    second = client.post(f"/app/todos/{day}/move", json=payload)
    assert second.status_code == 409
    assert [item["text"] for item in _read_jsonl(target_path)] == ["Move once"]


def test_move_route_partial_failure_preserves_source(
    tmp_path: Path, todos_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = "20260417"
    target_day = "20260418"
    facet = "personal"
    source_path = _write_todo(
        tmp_path,
        facet,
        day,
        {"text": "Partial route", "created_at": 1704067200000},
    )

    def fail_cancel(
        self: TodoChecklist,
        line_number: int,
        cancelled_reason: str | None = None,
        moved_to: str | None = None,
    ):
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(TodoChecklist, "cancel_entry", fail_cancel)

    response = todos_client().post(
        f"/app/todos/{day}/move",
        json={"target_day": target_day, "facet": facet, "index": 1},
    )

    target_path = tmp_path / "facets" / facet / "todos" / f"{target_day}.jsonl"
    assert response.status_code == 409
    assert response.get_json()["reason_code"] == "operation_no_longer_available"
    assert _read_jsonl(source_path)[0].get("cancelled") is None
    assert _read_jsonl(target_path)[0]["text"] == "Partial route"


def test_edit_move_route_cli_byte_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = "20260419"
    entry = {
        "text": "Parity task",
        "nudge": "20260419T10:30",
        "created_at": 1704067200000,
    }

    route_journal = tmp_path / "route"
    _write_todo(route_journal, "work", day, entry)
    _ensure_facet(route_journal, "personal")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(route_journal))
    route_response = _client_for_journal(route_journal).post(
        f"/app/todos/{day}",
        data={
            "action": "edit",
            "facet": "work",
            "index": "1",
            "text": "Parity task #personal",
        },
    )
    assert route_response.status_code == 302

    cli_journal = tmp_path / "cli"
    _write_todo(cli_journal, "work", day, entry)
    _ensure_facet(cli_journal, "personal")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(cli_journal))
    cli_result = runner.invoke(
        call_app,
        [
            "todos",
            "move",
            "1",
            "--day",
            day,
            "--from",
            "work",
            "--to",
            "personal",
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output

    for facet in ("work", "personal"):
        relpath = Path("facets") / facet / "todos" / f"{day}.jsonl"
        assert _stable_items(route_journal / relpath) == _stable_items(
            cli_journal / relpath
        )
