# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime as RealDateTime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import solstone.apps.todos.call as todos_call
import solstone.apps.todos.routes as todos_routes
import solstone.apps.todos.todo as todo_mod
import solstone.think.facets as facets_mod
from solstone.apps.todos.call import app
from solstone.apps.todos.todo import TodoChecklist, TodoItem
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_io import LockTimeout
from tests._baseline_harness import make_logged_in_test_client

FROZEN_DAY = "20260310"


class FixedDateTime(RealDateTime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 3, 10, 12, 0, tzinfo=tz)


@pytest.fixture
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner(journal: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.todos.call.get_client", lambda: client)
    return CliRunner()


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(todos_call, "datetime", FixedDateTime)
    monkeypatch.setattr(todos_routes, "datetime", FixedDateTime)
    monkeypatch.setattr(todo_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(facets_mod, "datetime", FixedDateTime)


def _ensure_facet(journal: Path, facet: str) -> None:
    facet_dir = journal / "facets" / facet
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
    journal: Path,
    facet: str,
    day: str,
    entries: list[dict] | None,
) -> Path:
    _ensure_facet(journal, facet)
    todos_dir = journal / "facets" / facet / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)
    path = todos_dir / f"{day}.jsonl"
    if entries is not None:
        path.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
    return path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _read_logs(journal: Path, facet: str, day: str) -> list[dict]:
    return _read_jsonl(journal / "facets" / facet / "logs" / f"{day}.jsonl")


def _lock_timeout(journal: Path) -> LockTimeout:
    return LockTimeout(journal / "lock", 0.01)


def test_list_single_facet_byte_exact(runner: CliRunner, journal: Path) -> None:
    _write_todos(
        journal,
        "personal",
        "20240101",
        [{"text": "Buy milk"}, {"text": "Walk dog", "completed": True}],
    )

    result = runner.invoke(app, ["list", "20240101", "--facet", "personal"])

    assert result.exit_code == 0
    assert result.stdout == "1: [ ] Buy milk\n2: [x] Walk dog\n"
    assert result.stderr == ""


def test_list_single_day_multi_facet_byte_exact(
    runner: CliRunner, journal: Path
) -> None:
    _write_todos(journal, "work", "20240101", [{"text": "Work task"}])
    _write_todos(journal, "home", "20240101", [{"text": "Home task"}])

    result = runner.invoke(app, ["list", "20240101"])

    assert result.exit_code == 0
    assert result.stdout == (
        "## home\n1: [ ] Home task\n\n## work\n1: [ ] Work task\n\n"
    )
    assert result.stderr == ""


def test_list_range_multi_facet_byte_exact(runner: CliRunner, journal: Path) -> None:
    _write_todos(journal, "work", "20240101", [{"text": "Work d1"}])
    _write_todos(journal, "work", "20240102", [{"text": "Work d2"}])
    _write_todos(journal, "home", "20240101", [{"text": "Home d1"}])

    result = runner.invoke(app, ["list", "20240101", "--to", "20240102"])

    assert result.exit_code == 0
    assert result.stdout == (
        "## home\n"
        "### 20240101\n"
        "1: [ ] Home d1\n"
        "\n"
        "## work\n"
        "### 20240101\n"
        "1: [ ] Work d1\n"
        "\n"
        "### 20240102\n"
        "1: [ ] Work d2\n"
        "\n"
    )
    assert result.stderr == ""


def test_list_empty_and_invalid_range(runner: CliRunner, journal: Path) -> None:
    _ensure_facet(journal, "personal")
    _write_todos(journal, "work", "20240101", [{"text": "Work task"}])

    empty = runner.invoke(app, ["list", "20240101", "--facet", "personal"])
    invalid = runner.invoke(
        app, ["list", "20240201", "--facet", "personal", "--to", "20240101"]
    )
    malformed_day = runner.invoke(app, ["list", "bad", "--facet", "personal"])
    malformed_to = runner.invoke(
        app, ["list", "20240101", "--facet", "work", "--to", "bad"]
    )

    assert empty.exit_code == 0
    assert empty.stdout == "No todos found for 20240101.\n"
    assert invalid.exit_code == 1
    assert (
        invalid.stderr == "Error: --to (20240101) must not be before day (20240201)\n"
    )
    assert malformed_day.exit_code == 0
    assert malformed_day.stdout == "No todos found for bad.\n"
    assert malformed_to.exit_code == 0
    assert malformed_to.stdout == "### 20240101\n1: [ ] Work task\n\n"


def test_list_defaults_from_env_and_today(
    runner: CliRunner,
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: None,
) -> None:
    _write_todos(journal, "personal", "20240101", [{"text": "Env task"}])
    _write_todos(journal, "personal", FROZEN_DAY, [{"text": "Today task"}])

    monkeypatch.setenv("SOL_DAY", "20240101")
    env_result = runner.invoke(app, ["list", "--facet", "personal"])
    monkeypatch.delenv("SOL_DAY", raising=False)
    today_result = runner.invoke(app, ["list", "--facet", "personal"])

    assert env_result.exit_code == 0
    assert env_result.stdout == "1: [ ] Env task\n"
    assert today_result.exit_code == 0
    assert today_result.stdout == "1: [ ] Today task\n"


def test_list_day_option_matches_positional_and_conflicts(
    runner: CliRunner, journal: Path
) -> None:
    _write_todos(journal, "personal", "20240101", [{"text": "Option task"}])

    positional = runner.invoke(app, ["list", "20240101", "--facet", "personal"])
    option = runner.invoke(app, ["list", "--day", "20240101", "--facet", "personal"])
    conflict = runner.invoke(
        app, ["list", "20240101", "--day", "20240102", "--facet", "personal"]
    )

    assert positional.exit_code == 0
    assert option.exit_code == 0
    assert option.stdout == positional.stdout
    assert conflict.exit_code == 1
    assert conflict.stderr == (
        "Error: conflicting day given as argument (20240101) and --day (20240102).\n"
    )


def test_add_success_and_call_action_log(
    runner: CliRunner, journal: Path, frozen_clock: None
) -> None:
    _ensure_facet(journal, "personal")

    result = runner.invoke(
        app, ["add", "Ship feature", "--day", "29991231", "--facet", "personal"]
    )

    assert result.exit_code == 0
    assert result.stdout == "1: [ ] Ship feature\n"
    entries = _read_logs(journal, "personal", "29991231")
    assert len(entries) == 1
    assert entries[0]["source"] == "call"
    assert entries[0]["actor"] == "agent"
    assert entries[0]["action"] == "todo_add"
    assert entries[0]["params"] == {"line_number": 1, "text": "Ship feature"}


def test_add_appends_and_accepts_nudge(
    runner: CliRunner, journal: Path, frozen_clock: None
) -> None:
    _write_todos(journal, "personal", FROZEN_DAY, [{"text": "First"}])

    result = runner.invoke(
        app,
        [
            "add",
            "Test nudge",
            "--nudge",
            "15:00",
            "--day",
            FROZEN_DAY,
            "--facet",
            "personal",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "1: [ ] First\n2: [ ] Test nudge (nudge 15:00)\n"
    saved = _read_jsonl(
        journal / "facets" / "personal" / "todos" / f"{FROZEN_DAY}.jsonl"
    )
    assert saved[1]["nudge"] == "20260310T15:00"


def test_add_dedup_and_force(runner: CliRunner, journal: Path) -> None:
    _write_todos(journal, "work", "20240102", [{"text": "Draft Q1 plan"}])
    _ensure_facet(journal, "personal")

    duplicate = runner.invoke(
        app,
        ["add", "Draft Q1 plan", "--day", "20240102", "--facet", "personal"],
    )
    forced = runner.invoke(
        app,
        [
            "add",
            "Draft Q1 plan",
            "--day",
            "20240102",
            "--facet",
            "personal",
            "--force",
        ],
    )

    assert duplicate.exit_code == 1
    assert duplicate.stdout == ""
    assert duplicate.stderr == (
        "Duplicate detected for: Draft Q1 plan\n"
        "  [100%] work/20240102 line 1: Draft Q1 plan\n"
        "Use --force to add anyway.\n"
    )
    assert forced.exit_code == 0
    assert forced.stdout == "1: [ ] Draft Q1 plan\n"


def test_add_error_branches(
    runner: CliRunner,
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_facet(journal, "personal")

    invalid_day = runner.invoke(
        app, ["add", "Task", "--day", "bad", "--facet", "personal"]
    )
    bad_nudge = runner.invoke(
        app,
        [
            "add",
            "Task",
            "--day",
            "20240101",
            "--facet",
            "personal",
            "--nudge",
            "tomorrow nope",
        ],
    )
    empty = runner.invoke(
        app, ["add", "   ", "--day", "20240101", "--facet", "personal"]
    )
    monkeypatch.delenv("SOL_DAY", raising=False)
    monkeypatch.delenv("SOL_FACET", raising=False)
    missing_day = runner.invoke(app, ["add", "Task", "--facet", "personal"])
    missing_facet = runner.invoke(app, ["add", "Task", "--day", "20240101"])

    assert invalid_day.exit_code == 1
    assert invalid_day.stderr == "Error: invalid day format 'bad'\n"
    assert bad_nudge.exit_code == 1
    assert bad_nudge.stderr == "Error: invalid nudge time after 'tomorrow': nope\n"
    assert empty.exit_code == 1
    assert empty.stderr == "Error: todo text cannot be empty\n"
    assert missing_day.exit_code == 1
    assert missing_day.stderr == (
        "Error: day is required (pass as argument or set SOL_DAY).\n"
    )
    assert missing_facet.exit_code == 1
    assert missing_facet.stderr == (
        "Error: facet is required (pass as argument or set SOL_FACET).\n"
    )


@pytest.mark.parametrize("verb", ["done", "cancel"])
def test_done_cancel_success_and_logs(
    runner: CliRunner, journal: Path, verb: str
) -> None:
    _write_todos(journal, "personal", "20240101", [{"text": "Buy milk"}])

    result = runner.invoke(app, [verb, "1", "--day", "20240101", "--facet", "personal"])

    assert result.exit_code == 0
    if verb == "done":
        assert result.stdout == "1: [x] Buy milk\n"
        action = "todo_done"
    else:
        assert result.stdout == "1: ~~[cancelled] Buy milk~~\n"
        action = "todo_cancel"
    entries = _read_logs(journal, "personal", "20240101")
    assert entries[-1]["source"] == "call"
    assert entries[-1]["actor"] == "agent"
    assert entries[-1]["action"] == action
    assert entries[-1]["params"] == {"line_number": 1, "text": "Buy milk"}


@pytest.mark.parametrize("verb", ["done", "cancel"])
def test_done_cancel_errors(
    runner: CliRunner,
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    _write_todos(journal, "personal", "20240101", [{"text": "Only one"}])
    bad_line = runner.invoke(
        app, [verb, "5", "--day", "20240101", "--facet", "personal"]
    )

    missing = runner.invoke(
        app, [verb, "1", "--day", "20240102", "--facet", "personal"]
    )

    def busy(cls, day: str, facet: str, modify_fn):
        raise _lock_timeout(journal)

    monkeypatch.setattr(TodoChecklist, "locked_modify", classmethod(busy))
    locked = runner.invoke(app, [verb, "1", "--day", "20240101", "--facet", "personal"])

    assert bad_line.exit_code == 1
    assert bad_line.stderr == "Error: line number 5 is out of range (1..1)\n"
    assert missing.exit_code == 1
    assert missing.stderr == "Error: line number 1 is out of range (1..0)\n"
    assert locked.exit_code == 1
    assert locked.stderr == "Error: todo list is busy, try again.\n"


def test_terminal_state_cli_errors_leave_jsonl_unchanged(
    runner: CliRunner, journal: Path
) -> None:
    completed_path = _write_todos(
        journal, "personal", "20240101", [{"text": "Done", "completed": True}]
    )
    completed_before = _read_jsonl(completed_path)
    completed_bytes = completed_path.read_bytes()

    cancel_completed = runner.invoke(
        app, ["cancel", "1", "--day", "20240101", "--facet", "personal"]
    )

    assert cancel_completed.exit_code == 1
    assert cancel_completed.stderr == "Error: Cannot cancel a completed todo.\n"
    assert _read_jsonl(completed_path) == completed_before
    assert completed_path.read_bytes() == completed_bytes

    cancelled_path = _write_todos(
        journal, "personal", "20240102", [{"text": "Cancelled", "cancelled": True}]
    )
    cancelled_before = _read_jsonl(cancelled_path)
    cancelled_bytes = cancelled_path.read_bytes()

    done_cancelled = runner.invoke(
        app, ["done", "1", "--day", "20240102", "--facet", "personal"]
    )

    assert done_cancelled.exit_code == 1
    assert done_cancelled.stderr == "Error: Cannot complete a cancelled todo.\n"
    assert _read_jsonl(cancelled_path) == cancelled_before
    assert cancelled_path.read_bytes() == cancelled_bytes


def test_done_rejects_move_tombstone_without_writing(
    runner: CliRunner, journal: Path
) -> None:
    source_path = _write_todos(journal, "work", "20240101", [{"text": "Move me"}])
    _ensure_facet(journal, "personal")
    move_result = runner.invoke(
        app,
        [
            "move",
            "1",
            "--day",
            "20240101",
            "--from",
            "work",
            "--to",
            "personal",
        ],
    )
    assert move_result.exit_code == 0
    tombstone_before = _read_jsonl(source_path)
    tombstone_bytes = source_path.read_bytes()

    done_tombstone = runner.invoke(
        app, ["done", "1", "--day", "20240101", "--facet", "work"]
    )

    assert done_tombstone.exit_code == 1
    assert done_tombstone.stderr == "Error: Cannot complete a cancelled todo.\n"
    assert _read_jsonl(source_path) == tombstone_before
    assert source_path.read_bytes() == tombstone_bytes


def test_move_success_nudge_consent_and_logs(
    runner: CliRunner, journal: Path, frozen_clock: None
) -> None:
    _write_todos(
        journal,
        "work",
        "20240101",
        [{"text": "Ship feature", "nudge": "20240101T09:00", "created_at": 1234}],
    )
    _write_todos(journal, "personal", "20240101", [{"text": "Existing"}])

    result = runner.invoke(
        app,
        [
            "move",
            "1",
            "--day",
            "20240101",
            "--from",
            "work",
            "--to",
            "personal",
            "--consent",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "Moved todo 1 ('Ship feature') from 'work' to 'personal'.\n"
    source = _read_jsonl(journal / "facets" / "work" / "todos" / "20240101.jsonl")
    dest = _read_jsonl(journal / "facets" / "personal" / "todos" / "20240101.jsonl")
    assert source[0]["cancelled"] is True
    assert source[0]["cancelled_reason"] == "moved_to_facet"
    assert source[0]["moved_to"] == "personal"
    assert dest[0]["text"] == "Existing"
    assert dest[1]["text"] == "Ship feature"
    assert dest[1]["nudge"] == "20240101T09:00"
    assert dest[1]["created_at"] == 1234

    out_log = _read_logs(journal, "work", FROZEN_DAY)[0]
    in_log = _read_logs(journal, "personal", FROZEN_DAY)[0]
    assert out_log["source"] == "call"
    assert out_log["actor"] == "agent"
    assert out_log["action"] == "todo_move_out"
    assert out_log["params"] == {
        "moved_from": "work",
        "moved_to": "personal",
        "line_number": 1,
        "text": "Ship feature",
        "consent": True,
    }
    assert in_log["action"] == "todo_move_in"
    assert in_log["params"] == {
        "moved_from": "work",
        "moved_to": "personal",
        "line_number": 2,
        "text": "Ship feature",
        "consent": True,
    }


@pytest.mark.parametrize(
    ("argv", "stderr"),
    [
        (
            ["move", "1", "--day", "bad", "--from", "work", "--to", "personal"],
            "Error: Invalid day format 'bad', expected YYYYMMDD.\n",
        ),
        (
            ["move", "1", "--day", "20240101", "--from", "work", "--to", "work"],
            "Error: source and destination facet are the same.\n",
        ),
    ],
)
def test_move_route_ordered_guards(
    runner: CliRunner, journal: Path, argv: list[str], stderr: str
) -> None:
    _write_todos(journal, "work", "20240101", [{"text": "Ship feature"}])
    _ensure_facet(journal, "personal")

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert result.stderr == stderr


def test_move_facet_error_precedes_bad_day(runner: CliRunner, journal: Path) -> None:
    _ensure_facet(journal, "work")

    result = runner.invoke(
        app,
        [
            "move",
            "1",
            "--day",
            "BADDAY",
            "--from",
            "nonexistent",
            "--to",
            "work",
        ],
    )

    assert result.exit_code == 1
    assert result.stderr == "Error: Facet 'nonexistent' (--from) does not exist.\n"


@pytest.mark.parametrize(
    ("from_facet", "to_facet", "stderr"),
    [
        ("missing", "personal", "Error: Facet 'missing' (--from) does not exist.\n"),
        ("work", "missing", "Error: Facet 'missing' (--to) does not exist.\n"),
    ],
)
def test_move_missing_facet_errors(
    runner: CliRunner,
    journal: Path,
    from_facet: str,
    to_facet: str,
    stderr: str,
) -> None:
    _write_todos(journal, "work", "20240101", [{"text": "Ship feature"}])
    _ensure_facet(journal, "personal")

    result = runner.invoke(
        app,
        [
            "move",
            "1",
            "--day",
            "20240101",
            "--from",
            from_facet,
            "--to",
            to_facet,
        ],
    )

    assert result.exit_code == 1
    assert result.stderr == stderr


@pytest.mark.parametrize(
    ("entries", "line", "stderr"),
    [
        (
            [{"text": "Ship feature", "completed": True}],
            "1",
            "Error: Cannot move a completed todo.\n",
        ),
        (
            [{"text": "Ship feature", "cancelled": True}],
            "1",
            "Error: Cannot move an already cancelled todo.\n",
        ),
        (
            [{"text": "Ship feature"}],
            "5",
            "Error: line number 5 is out of range (1..1)\n",
        ),
    ],
)
def test_move_state_guards(
    runner: CliRunner,
    journal: Path,
    entries: list[dict],
    line: str,
    stderr: str,
) -> None:
    _write_todos(journal, "work", "20240101", entries)
    _ensure_facet(journal, "personal")

    result = runner.invoke(
        app,
        [
            "move",
            line,
            "--day",
            "20240101",
            "--from",
            "work",
            "--to",
            "personal",
        ],
    )

    assert result.exit_code == 1
    assert result.stderr == stderr


def test_move_missing_source_partial_and_busy(
    runner: CliRunner, journal: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_facet(journal, "work")
    _ensure_facet(journal, "personal")
    missing = runner.invoke(
        app,
        [
            "move",
            "1",
            "--day",
            "20240101",
            "--from",
            "work",
            "--to",
            "personal",
        ],
    )
    assert missing.exit_code == 1
    assert missing.stderr == "Error: No todos found for day 20240101 in facet 'work'.\n"

    _write_todos(journal, "work", "20240101", [{"text": "Partial"}])
    original_apply_cancel = TodoChecklist._apply_cancel

    def fail_cancel(
        self: TodoChecklist,
        item: TodoItem,
        *,
        cancelled_reason: str | None = None,
        moved_to: str | None = None,
    ):
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(TodoChecklist, "_apply_cancel", fail_cancel)
    partial = runner.invoke(
        app,
        [
            "move",
            "1",
            "--day",
            "20240101",
            "--from",
            "work",
            "--to",
            "personal",
        ],
    )
    assert partial.exit_code == 1
    assert partial.stderr == (
        "Warning: Item was appended to 'personal' but could not cancel source "
        "in 'work'. Cancel it manually with: sol call todos cancel 1 --day "
        "20240101 --facet work\n"
    )

    monkeypatch.setattr(TodoChecklist, "_apply_cancel", original_apply_cancel)
    _write_todos(journal, "work", "20240102", [{"text": "Busy"}])
    _ensure_facet(journal, "personal")

    def busy_move(cls, *args, **kwargs):
        raise _lock_timeout(journal)

    monkeypatch.setattr(TodoChecklist, "move_entry", classmethod(busy_move))
    busy = runner.invoke(
        app,
        [
            "move",
            "1",
            "--day",
            "20240102",
            "--from",
            "work",
            "--to",
            "personal",
        ],
    )
    assert busy.exit_code == 1
    assert busy.stderr == "Error: todo list is busy, try again.\n"


def test_upcoming_success_filter_and_empty(
    runner: CliRunner, journal: Path, frozen_clock: None
) -> None:
    _write_todos(journal, "work", "29991231", [{"text": "Future task"}])
    _write_todos(journal, "home", "20200101", [{"text": "Past task"}])

    future = runner.invoke(app, ["upcoming", "--facet", "work"])
    empty = runner.invoke(app, ["upcoming", "--facet", "home"])

    assert future.exit_code == 0
    assert future.stdout == "### Work: 29991231\n[ ] Future task\n"
    assert empty.exit_code == 0
    assert empty.stdout == "No upcoming todos.\n"


def test_list_nudges_due_human_json_empty_and_readonly(
    runner: CliRunner, journal: Path, frozen_clock: None
) -> None:
    work_path = _write_todos(
        journal,
        "work",
        FROZEN_DAY,
        [{"text": "Work ping", "nudge": "20260310T08:00"}],
    )
    _write_todos(
        journal,
        "home",
        FROZEN_DAY,
        [{"text": "Home ping", "nudge": "20260310T09:00"}],
    )
    before = work_path.read_text(encoding="utf-8")

    human = runner.invoke(app, ["list-nudges-due", "--facet", "work"])
    json_result = runner.invoke(app, ["list-nudges-due", "--json"])

    assert human.exit_code == 0
    assert human.stdout == "1: [ ] Work ping (4h ago)\n"
    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout) == [
        {
            "day": FROZEN_DAY,
            "facet": "work",
            "index": 1,
            "text": "Work ping",
            "nudge": "20260310T08:00",
            "nudge_display": "4h ago",
        },
        {
            "day": FROZEN_DAY,
            "facet": "home",
            "index": 1,
            "text": "Home ping",
            "nudge": "20260310T09:00",
            "nudge_display": "3h ago",
        },
    ]
    assert work_path.read_text(encoding="utf-8") == before

    empty_human = runner.invoke(app, ["list-nudges-due", "--facet", "personal"])
    empty_json = runner.invoke(
        app, ["list-nudges-due", "--facet", "personal", "--json"]
    )
    assert empty_human.exit_code == 0
    assert empty_human.stdout == "No nudges due.\n"
    assert empty_json.exit_code == 0
    assert json.loads(empty_json.stdout) == []


def test_list_nudges_due_multi_facet_human(
    runner: CliRunner, journal: Path, frozen_clock: None
) -> None:
    _write_todos(
        journal,
        "work",
        FROZEN_DAY,
        [{"text": "Work ping", "nudge": "20260310T08:00"}],
    )
    _write_todos(
        journal,
        "home",
        FROZEN_DAY,
        [{"text": "Home ping", "nudge": "20260310T09:00"}],
    )

    result = runner.invoke(app, ["list-nudges-due"])

    assert result.exit_code == 0
    assert result.stdout == (
        "## work\n1: [ ] Work ping (4h ago)\n\n## home\n1: [ ] Home ping (3h ago)\n\n"
    )


def test_dispatch_nudges_marks_subprocess_and_idempotency(
    runner: CliRunner,
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: None,
) -> None:
    todo_path = _write_todos(
        journal,
        "work",
        FROZEN_DAY,
        [{"text": "Follow up", "nudge": "20260310T09:00"}],
    )
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return None

    monkeypatch.setattr(todos_call.subprocess, "run", fake_run)

    first = runner.invoke(app, ["dispatch-nudges", "--facet", "work"])
    second = runner.invoke(app, ["dispatch-nudges", "--facet", "work"])

    assert first.exit_code == 0
    assert first.stdout == "dispatched 1 nudge(s)\n"
    assert second.exit_code == 0
    assert second.stdout == "dispatched 0 nudge(s)\n"
    assert calls == [
        (
            [
                "sol",
                "notify",
                "Follow up",
                "--title",
                "Todo Reminder",
                "--icon",
                "✅",
                "--app",
                "todos",
                "--facet",
                "work",
                "--action",
                "/app/todos/20260310",
            ],
            {"check": False, "capture_output": True},
        )
    ]
    assert _read_jsonl(todo_path)[0]["notified"] is True


def test_dispatch_nudges_nothing_due_and_busy_skip(
    runner: CliRunner,
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: None,
) -> None:
    _write_todos(journal, "empty", FROZEN_DAY, [])
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        todos_call.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    nothing = runner.invoke(app, ["dispatch-nudges", "--facet", "empty"])
    assert nothing.exit_code == 0
    assert nothing.stdout == "dispatched 0 nudge(s)\n"
    assert calls == []

    _write_todos(
        journal,
        "work",
        FROZEN_DAY,
        [{"text": "Busy ping", "nudge": "20260310T08:00"}],
    )
    _write_todos(
        journal,
        "home",
        FROZEN_DAY,
        [{"text": "Home ping", "nudge": "20260310T09:00"}],
    )
    original = TodoChecklist.locked_modify

    def flaky(cls, day: str, facet: str, modify_fn):
        if facet == "work":
            raise _lock_timeout(journal)
        return original(day, facet, modify_fn)

    monkeypatch.setattr(TodoChecklist, "locked_modify", classmethod(flaky))
    skip = runner.invoke(app, ["dispatch-nudges"])

    assert skip.exit_code == 0
    assert skip.stdout == "dispatched 1 nudge(s)\n"
    assert calls == [
        (
            [
                "sol",
                "notify",
                "Home ping",
                "--title",
                "Todo Reminder",
                "--icon",
                "✅",
                "--app",
                "todos",
                "--facet",
                "home",
                "--action",
                "/app/todos/20260310",
            ],
            {"check": False, "capture_output": True},
        )
    ]
    work = _read_jsonl(journal / "facets" / "work" / "todos" / f"{FROZEN_DAY}.jsonl")
    home = _read_jsonl(journal / "facets" / "home" / "todos" / f"{FROZEN_DAY}.jsonl")
    assert "notified" not in work[0]
    assert home[0]["notified"] is True
