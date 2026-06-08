# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from solstone.apps.todos import copy as todos_copy
from solstone.apps.todos.todo import (
    TodoChecklist,
    TodoEmptyTextError,
    TodoError,
    TodoItem,
    TodoMovePartialError,
    TodoNotMovableError,
    find_cross_facet_matches,
    format_nudge,
    get_facets_with_todos,
    get_todo_days_in_range,
    get_todos,
    parse_nudge,
    upcoming,
    validate_line_number,
)
from solstone.apps.utils import log_app_action
from solstone.convey import state
from solstone.convey.config import get_selected_facet
from solstone.convey.copy import CONVEY_RELOAD_HINT
from solstone.convey.reasons import (
    AGENT_UNAVAILABLE,
    INVALID_DAY,
    INVALID_MONTH,
    INVALID_REQUEST_VALUE,
    MISSING_REQUIRED_FIELD,
    OPERATION_NO_LONGER_AVAILABLE,
    TODO_BUSY,
    TODO_OPERATION_FAILED,
)
from solstone.convey.utils import (
    DATE_RE,
    created,
    error_response,
    format_date,
    respond_collection,
    success_response,
)
from solstone.think.facets import get_facets, log_call_action
from solstone.think.journal_io.errors import LockTimeout

VISIBLE_INCOMPLETE_BUDGET = 30
VISIBLE_COMPLETED_BUDGET = 5

todos_bp = Blueprint("app:todos", __name__, url_prefix="/app/todos")


@todos_bp.app_context_processor
def _inject_todos_copy() -> dict:
    return {"todos_copy": todos_copy}


def _compute_badge_counts(day: str, facet: str) -> dict:
    """Compute badge counts for a specific facet and total for today.

    Returns dict with 'facet_count' and 'total_count'.
    Excludes cancelled todos from counts.
    """
    today = date.today().strftime("%Y%m%d")

    # Get count for the specific facet (exclude cancelled)
    facet_todos = get_todos(day, facet)
    facet_count = 0
    if facet_todos:
        facet_count = sum(
            1 for t in facet_todos if not t.get("completed") and not t.get("cancelled")
        )

    # Get total count across all facets for today (for app icon badge)
    total_count = 0
    if day == today:
        try:
            facet_map = get_facets()
        except Exception:
            facet_map = {}

        for facet_name in facet_map.keys():
            todos = get_todos(today, facet_name)
            if todos:
                total_count += sum(
                    1
                    for t in todos
                    if not t.get("completed") and not t.get("cancelled")
                )

    return {"facet_count": facet_count, "total_count": total_count}


@todos_bp.route("/api/badge-count")
def badge_count():
    """Get total pending todo count for today across all facets."""
    today = date.today().strftime("%Y%m%d")
    total = 0

    try:
        facet_map = get_facets()
    except Exception:
        facet_map = {}

    for facet_name in facet_map.keys():
        facet_todos = get_todos(today, facet_name)
        if facet_todos:
            total += sum(
                1
                for todo in facet_todos
                if not todo.get("completed") and not todo.get("cancelled")
            )

    return jsonify({"count": total})


@todos_bp.route("/api/nudges")
def nudges_api():
    """Return todos with future nudges for today and tomorrow."""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y%m%d")
    now_str = now.strftime("%Y%m%dT%H:%M")

    nudges = []
    facets_dir = Path(state.journal_root) / "facets"
    if not facets_dir.is_dir():
        return jsonify({"nudges": []})

    for facet_dir in facets_dir.iterdir():
        if not facet_dir.is_dir():
            continue
        facet_name = facet_dir.name
        for day in [today, tomorrow]:
            checklist = TodoChecklist.load(day, facet_name)
            if not checklist.exists:
                continue
            for item in checklist.items:
                if (
                    item.nudge
                    and item.nudge > now_str
                    and not item.completed
                    and not item.cancelled
                    and not item.notified
                ):
                    nudges.append(
                        {
                            "facet": facet_name,
                            "day": day,
                            "index": item.index,
                            "text": item.text,
                            "nudge": item.nudge,
                        }
                    )

    return jsonify({"nudges": nudges})


@todos_bp.route("/api/stats/<month>")
def api_stats(month: str):
    """Return todo counts per facet for a specific month.

    Args:
        month: YYYYMM format month string

    Returns:
        JSON dict mapping day (YYYYMMDD) to facet counts dict.
        Count is number of non-cancelled todos for that day.
    """
    import re

    if not re.fullmatch(r"\d{6}", month):
        return error_response(
            INVALID_MONTH,
            detail="Invalid month format, expected YYYYMM",
        )

    try:
        facet_map = get_facets()
    except Exception:
        facet_map = {}

    stats: dict[str, dict[str, int]] = {}
    journal_root = Path(state.journal_root)

    for facet_name in facet_map.keys():
        todos_dir = journal_root / "facets" / facet_name / "todos"
        if not todos_dir.exists():
            continue

        for todo_file in todos_dir.glob(f"{month}*.jsonl"):
            day = todo_file.stem
            if not DATE_RE.fullmatch(day):
                continue

            # Count non-cancelled todos in file
            facet_todos = get_todos(day, facet_name)
            if facet_todos:
                count = sum(1 for t in facet_todos if not t.get("cancelled"))
                if count > 0:
                    if day not in stats:
                        stats[day] = {}
                    stats[day][facet_name] = count

    return jsonify(stats)


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True) or {}
    return payload if isinstance(payload, dict) else {}


def _body_str(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    return str(value)


def _required_body_str(payload: dict[str, Any], name: str) -> tuple[str | None, Any]:
    value = _body_str(payload, name)
    if value is None:
        return None, error_response(
            MISSING_REQUIRED_FIELD, detail=f"{name} is required"
        )
    return value, None


def _body_line_number(payload: dict[str, Any]) -> tuple[int | None, Any]:
    try:
        return int(payload.get("line_number")), None
    except (TypeError, ValueError):
        return None, error_response(
            MISSING_REQUIRED_FIELD,
            detail="line_number is required",
        )


def _checklist_payload(day: str, facet: str) -> dict[str, Any]:
    checklist = TodoChecklist.load(day, facet)
    return {
        "day": day,
        "facet": facet,
        "exists": checklist.exists,
        "has_items": bool(checklist.items),
        "display": checklist.display(),
    }


def _due_nudge_items(
    facet: str | None,
) -> tuple[str, str, datetime, list[dict[str, Any]]]:
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    now_str = now.strftime("%Y%m%dT%H:%M")

    facets_dir = Path(state.journal_root) / "facets"
    if not facets_dir.is_dir():
        return today, now_str, now, []

    if facet is not None:
        facet_names = [facet]
    else:
        facet_names = [d.name for d in facets_dir.iterdir() if d.is_dir()]

    items: list[dict[str, Any]] = []
    for facet_name in facet_names:
        checklist = TodoChecklist.load(today, facet_name)
        if not checklist.exists:
            continue
        for item in checklist.items:
            if (
                item.nudge
                and item.nudge <= now_str
                and not item.notified
                and not item.completed
                and not item.cancelled
            ):
                items.append(
                    {
                        "day": today,
                        "facet": facet_name,
                        "index": item.index,
                        "text": item.text,
                        "nudge": item.nudge,
                        "nudge_display": format_nudge(item.nudge, now=now),
                        "display_line": item.display_line(),
                    }
                )
    return today, now_str, now, items


@todos_bp.route("/api/checklist")
def api_checklist():
    """Return rendered todo checklist data for the CLI."""
    day = request.args.get("day", "").strip()
    to = request.args.get("to")
    to = to.strip() if to is not None else None
    facet = request.args.get("facet")
    facet = facet.strip() if facet is not None and facet.strip() else None

    if to is not None and to < day:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail=f"--to ({to}) must not be before day ({day})",
        )

    if to is not None and to != day:
        facets = [facet] if facet else sorted(get_facets())
        sections = []
        for facet_name in facets:
            days = get_todo_days_in_range(facet_name, day, to)
            if not days:
                continue
            sections.append(
                {
                    "facet": facet_name,
                    "days": [
                        _checklist_payload(day_str, facet_name) for day_str in days
                    ],
                }
            )
        return jsonify(
            {
                "mode": "range",
                "day": day,
                "to": to,
                "facet": facet,
                "facet_count": len(facets),
                "sections": sections,
            }
        )

    facets = [facet] if facet else get_facets_with_todos(day)
    return jsonify(
        {
            "mode": "single",
            "day": day,
            "facet": facet,
            "facet_count": len(facets),
            "facets": [_checklist_payload(day, facet_name) for facet_name in facets],
        }
    )


@todos_bp.route("/api/upcoming")
def api_upcoming():
    """Return rendered upcoming todos for the CLI."""
    facet = request.args.get("facet")
    facet = facet.strip() if facet is not None and facet.strip() else None
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        return error_response(INVALID_REQUEST_VALUE, detail="limit must be an integer")
    return jsonify({"upcoming": upcoming(limit=limit, facet=facet)})


@todos_bp.route("/api/nudges-due")
def api_nudges_due():
    """Return due, unnotified todo nudges for the CLI."""
    facet = request.args.get("facet")
    facet = facet.strip() if facet is not None and facet.strip() else None
    _today, _now_str, _now, items = _due_nudge_items(facet)
    return respond_collection(items)


@todos_bp.route("/api/add", methods=["POST"])
def api_add_todo():
    """Add a todo through the CLI-backing JSON API."""
    payload = _json_body()
    text, error = _required_body_str(payload, "text")
    if error is not None:
        return error
    day, error = _required_body_str(payload, "day")
    if error is not None:
        return error
    facet, error = _required_body_str(payload, "facet")
    if error is not None:
        return error
    assert text is not None and day is not None and facet is not None
    day = day.strip()
    facet = facet.strip()
    nudge = _body_str(payload, "nudge")
    force = bool(payload.get("force", False))

    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail=f"invalid day format '{day}'")

    parsed_nudge: str | None = None
    if nudge is not None:
        try:
            parsed_nudge = parse_nudge(nudge, day)
        except ValueError as exc:
            return error_response(INVALID_REQUEST_VALUE, detail=str(exc))

    if not force:
        matches = find_cross_facet_matches(text, day, exclude_facet=facet)
        if matches:
            return success_response({"status": "duplicate", "matches": matches})

    try:

        def _add(checklist: TodoChecklist) -> tuple[TodoChecklist, TodoItem]:
            item = checklist.append_entry(text, nudge=parsed_nudge)
            return checklist, item

        checklist, item = TodoChecklist.locked_modify(day, facet, _add)
    except TodoEmptyTextError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except LockTimeout:
        return error_response(TODO_BUSY)

    log_call_action(
        facet=facet,
        action="todo_add",
        params={"line_number": item.index, "text": item.text},
        day=day,
    )
    return created(
        {
            "status": "ok",
            "item": item.as_dict(),
            "display": checklist.display(),
        }
    )


@todos_bp.route("/api/done", methods=["POST"])
def api_done_todo():
    """Mark a todo done through the CLI-backing JSON API."""
    payload = _json_body()
    day, error = _required_body_str(payload, "day")
    if error is not None:
        return error
    facet, error = _required_body_str(payload, "facet")
    if error is not None:
        return error
    line_number, error = _body_line_number(payload)
    if error is not None:
        return error
    assert day is not None and facet is not None and line_number is not None
    day = day.strip()
    facet = facet.strip()

    try:

        def _done(checklist: TodoChecklist) -> tuple[TodoChecklist, TodoItem]:
            item = checklist.mark_done(line_number)
            return checklist, item

        checklist, item = TodoChecklist.locked_modify(day, facet, _done)
    except IndexError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except LockTimeout:
        return error_response(TODO_BUSY)

    log_call_action(
        facet=facet,
        action="todo_done",
        params={"line_number": line_number, "text": item.text},
        day=day,
    )
    return success_response(
        {
            "item": item.as_dict(),
            "display": checklist.display(),
        }
    )


@todos_bp.route("/api/cancel", methods=["POST"])
def api_cancel_todo():
    """Cancel a todo through the CLI-backing JSON API."""
    payload = _json_body()
    day, error = _required_body_str(payload, "day")
    if error is not None:
        return error
    facet, error = _required_body_str(payload, "facet")
    if error is not None:
        return error
    line_number, error = _body_line_number(payload)
    if error is not None:
        return error
    assert day is not None and facet is not None and line_number is not None
    day = day.strip()
    facet = facet.strip()

    try:

        def _cancel(checklist: TodoChecklist) -> tuple[TodoChecklist, TodoItem]:
            item = checklist.cancel_entry(line_number)
            return checklist, item

        checklist, item = TodoChecklist.locked_modify(day, facet, _cancel)
    except IndexError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except LockTimeout:
        return error_response(TODO_BUSY)

    log_call_action(
        facet=facet,
        action="todo_cancel",
        params={"line_number": line_number, "text": item.text},
        day=day,
    )
    return success_response(
        {
            "item": item.as_dict(),
            "display": checklist.display(),
        }
    )


@todos_bp.route("/api/move", methods=["POST"])
def api_move_todo():
    """Move an open todo across facets through the CLI-backing JSON API."""
    payload = _json_body()
    day, error = _required_body_str(payload, "day")
    if error is not None:
        return error
    from_facet, error = _required_body_str(payload, "from_facet")
    if error is not None:
        return error
    to_facet, error = _required_body_str(payload, "to_facet")
    if error is not None:
        return error
    line_number, error = _body_line_number(payload)
    if error is not None:
        return error
    assert (
        day is not None
        and from_facet is not None
        and to_facet is not None
        and line_number is not None
    )
    day = day.strip()
    from_facet = from_facet.strip()
    to_facet = to_facet.strip()
    consent = bool(payload.get("consent", False))

    for facet_name in (from_facet, to_facet):
        if not (Path(state.journal_root) / "facets" / facet_name).is_dir():
            return error_response(INVALID_REQUEST_VALUE, detail=facet_name)

    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError:
        return error_response(
            INVALID_DAY,
            detail=f"Invalid day format '{day}', expected YYYYMMDD.",
        )

    if from_facet == to_facet:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="source and destination facet are the same.",
        )

    try:
        source_checklist = TodoChecklist.load(day, from_facet)
        if not source_checklist.exists:
            raise FileNotFoundError()
        validate_line_number(line_number, len(source_checklist.items))
        item = source_checklist.items[line_number - 1]
        if item.completed:
            raise TodoError("Cannot move a completed todo.")
        if item.cancelled:
            raise TodoError("Cannot move an already cancelled todo.")
    except FileNotFoundError:
        return error_response(
            OPERATION_NO_LONGER_AVAILABLE,
            detail=f"No todos found for day {day} in facet '{from_facet}'.",
        )
    except IndexError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except TodoError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))

    try:
        new_item, cancelled = TodoChecklist.move_entry(
            day, from_facet, line_number, day, to_facet
        )
    except (IndexError, TodoNotMovableError) as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except LockTimeout:
        return error_response(TODO_BUSY)
    except TodoMovePartialError:
        return error_response(
            TODO_OPERATION_FAILED,
            detail=(
                f"Item was appended to '{to_facet}' but could not cancel source "
                f"in '{from_facet}'. Cancel it manually with: sol call todos "
                f"cancel {line_number} --day {day} --facet {from_facet}"
            ),
        )

    params_out: dict[str, object] = {
        "moved_from": from_facet,
        "moved_to": to_facet,
        "line_number": line_number,
        "text": cancelled.text,
    }
    params_in: dict[str, object] = {
        "moved_from": from_facet,
        "moved_to": to_facet,
        "line_number": new_item.index,
        "text": new_item.text,
    }
    if consent:
        params_out["consent"] = True
        params_in["consent"] = True
    log_call_action(facet=from_facet, action="todo_move_out", params=params_out)
    log_call_action(facet=to_facet, action="todo_move_in", params=params_in)
    return success_response(
        {
            "new_item": new_item.as_dict(),
            "cancelled": cancelled.as_dict(),
        }
    )


@todos_bp.route("/api/dispatch-nudges", methods=["POST"])
def api_dispatch_nudges():
    """Mark due todo nudges as notified for the CLI."""
    payload = _json_body()
    facet = _body_str(payload, "facet")
    facet = facet.strip() if facet is not None and facet.strip() else None
    today, now_str, _now, due_items = _due_nudge_items(facet)

    facet_names = sorted({item["facet"] for item in due_items})
    dispatched: list[dict[str, str]] = []
    for facet_name in facet_names:

        def _mark(checklist: TodoChecklist) -> list[str]:
            texts: list[str] = []
            for item in checklist.items:
                if (
                    item.nudge
                    and item.nudge <= now_str
                    and not item.notified
                    and not item.completed
                    and not item.cancelled
                ):
                    item.notified = True
                    texts.append(item.text)
            if texts:
                checklist.save()
            return texts

        try:
            texts = TodoChecklist.locked_modify(today, str(facet_name), _mark)
        except LockTimeout:
            continue
        for text in texts:
            dispatched.append({"facet": str(facet_name), "text": text})

    return success_response(
        {
            "day": today,
            "items": dispatched,
            "total": len(dispatched),
        }
    )


def _todo_path(day: str, facet: str) -> Path:
    return Path(state.journal_root) / "facets" / facet / "todos" / f"{day}.jsonl"


@todos_bp.route("/")
def todos_page() -> str:
    today = date.today().strftime("%Y%m%d")
    return redirect(url_for("app:todos.todos_day", day=today))


@todos_bp.route("/<day>", methods=["GET", "POST"])
def todos_day(day: str):  # type: ignore[override]
    if not DATE_RE.fullmatch(day):
        return error_response(
            INVALID_DAY,
            status=404,
            detail="Day must be in YYYYMMDD format.",
        )

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add":
            text = request.form.get("text", "").strip()
            is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
            error_message = None

            if not text:
                error_message = "Cannot add an empty todo"
            else:
                # Extract facet from hashtag (e.g., "#work" -> "work")
                import re

                facet_match = re.search(r"#([a-z][a-z0-9_-]*)", text, re.IGNORECASE)
                if facet_match:
                    facet = facet_match.group(1).lower()
                    # Remove the hashtag from the text
                    text = re.sub(
                        r"\s*#" + re.escape(facet_match.group(1)) + r"\b",
                        "",
                        text,
                        count=1,
                        flags=re.IGNORECASE,
                    ).strip()

                    # Validate facet exists
                    try:
                        facet_map = get_facets()
                    except Exception:
                        facet_map = {}

                    if facet not in facet_map:
                        error_message = f"Facet #{facet} does not exist"
                else:
                    # Use selected facet as default, fall back to personal
                    selected = get_selected_facet()
                    facet = selected if selected else "personal"

                if not error_message and not text:
                    error_message = "Cannot add an empty todo"

                if not error_message:
                    try:

                        def _add(cl: TodoChecklist) -> TodoItem:
                            return cl.append_entry(text)

                        item = TodoChecklist.locked_modify(day, facet, _add)

                        log_app_action(
                            app="todos",
                            facet=facet,
                            action="todo_add",
                            params={"text": item.text, "line_number": item.index},
                            day=day,
                        )

                        # If AJAX request, return JSON with new todo data
                        if is_ajax:
                            counts = _compute_badge_counts(day, facet)
                            return jsonify(
                                {
                                    "status": "ok",
                                    "todo": {
                                        "facet": facet,
                                        "index": item.index,
                                        "text": item.text,
                                        "nudge": item.nudge,
                                        "nudge_display": format_nudge(item.nudge)
                                        if item.nudge
                                        else None,
                                        "completed": False,
                                    },
                                    **counts,
                                }
                            )
                    except (TodoEmptyTextError, RuntimeError) as exc:
                        current_app.logger.debug(
                            "Failed to append todo for %s/%s: %s", facet, day, exc
                        )
                        error_message = "Unable to add todo right now"

            # Handle errors
            if error_message:
                if is_ajax:
                    return (
                        jsonify({"status": "error", "message": error_message}),
                        400,
                    )
                flash(error_message, "error")

            return redirect(url_for("app:todos.todos_day", day=day))

        # Get facet and index for other actions
        facet = request.form.get("facet", "personal")
        index_str = request.form.get("index")

        try:
            index = int(index_str) if index_str else None
        except ValueError:
            index = None

        if not index:
            flash("Missing todo index", "error")
            return redirect(url_for("app:todos.todos_day", day=day))

        try:
            checklist = TodoChecklist.load(day, facet)
        except RuntimeError as exc:
            current_app.logger.debug(
                "Failed to load checklist for %s/%s: %s", facet, day, exc
            )
            flash(f"Todo list changed, {CONVEY_RELOAD_HINT}", "error")
            return redirect(url_for("app:todos.todos_day", day=day))

        try:
            if action == "complete":

                def _complete(cl: TodoChecklist) -> TodoItem:
                    return cl.mark_done(index)

                item = TodoChecklist.locked_modify(day, facet, _complete)
                log_app_action(
                    app="todos",
                    facet=facet,
                    action="todo_complete",
                    params={"line_number": index, "text": item.text},
                    day=day,
                )
            elif action == "uncomplete":

                def _uncomplete(cl: TodoChecklist) -> TodoItem:
                    return cl.mark_undone(index)

                item = TodoChecklist.locked_modify(day, facet, _uncomplete)
                log_app_action(
                    app="todos",
                    facet=facet,
                    action="todo_uncomplete",
                    params={"line_number": index, "text": item.text},
                    day=day,
                )
            elif action == "cancel":

                def _cancel(cl: TodoChecklist) -> TodoItem:
                    return cl.cancel_entry(index)

                item = TodoChecklist.locked_modify(day, facet, _cancel)
                log_app_action(
                    app="todos",
                    facet=facet,
                    action="todo_cancel",
                    params={"line_number": index, "text": item.text},
                    day=day,
                )
            elif action == "edit":
                import re

                text = request.form.get("text", "").strip()

                # Check if text contains a facet hashtag
                facet_match = re.search(r"#([a-z][a-z0-9_-]*)", text, re.IGNORECASE)
                if facet_match:
                    new_facet = facet_match.group(1).lower()
                    # Remove the hashtag from the text
                    text = re.sub(
                        r"\s*#" + re.escape(facet_match.group(1)) + r"\b",
                        "",
                        text,
                        count=1,
                        flags=re.IGNORECASE,
                    ).strip()

                    # Validate new facet exists
                    try:
                        facet_map = get_facets()
                    except Exception:
                        facet_map = {}

                    if new_facet not in facet_map:
                        flash(f"Facet #{new_facet} does not exist", "error")
                        return redirect(url_for("app:todos.todos_day", day=day))

                    # If facet changed, move the todo (cancel source, add to target)
                    if new_facet != facet:
                        _created, cancelled = TodoChecklist.move_entry(
                            day, facet, index, day, new_facet, text=text
                        )

                        log_app_action(
                            app="todos",
                            facet=facet,
                            action="todo_edit",
                            params={
                                "line_number": index,
                                "old_text": cancelled.text,
                                "new_text": text,
                                "old_facet": facet,
                                "new_facet": new_facet,
                            },
                            day=day,
                        )

                        return redirect(url_for("app:todos.todos_day", day=day))

                # No facet change, just update text
                old_text = checklist.get_item(index).text

                def _update(cl: TodoChecklist) -> TodoItem:
                    return cl.update_entry_text(index, text)

                TodoChecklist.locked_modify(day, facet, _update)
                log_app_action(
                    app="todos",
                    facet=facet,
                    action="todo_edit",
                    params={
                        "line_number": index,
                        "old_text": old_text,
                        "new_text": text,
                    },
                    day=day,
                )
            else:
                flash("Unknown action", "error")
                return redirect(url_for("app:todos.todos_day", day=day))
        except TodoEmptyTextError:
            flash("Cannot update todo to empty text", "error")
        except IndexError:
            flash(f"Todo list changed, {CONVEY_RELOAD_HINT}", "error")
        except TodoNotMovableError:
            flash(f"Todo list changed, {CONVEY_RELOAD_HINT}", "error")
        except TodoMovePartialError:
            flash(f"Todo list changed, {CONVEY_RELOAD_HINT}", "error")
        except LockTimeout:
            flash(f"Todo list changed, {CONVEY_RELOAD_HINT}", "error")

        # If AJAX request, return JSON with updated counts
        if (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.accept_mimetypes.accept_json
        ):
            counts = _compute_badge_counts(day, facet)
            return jsonify({"status": "ok", **counts})

        return redirect(url_for("app:todos.todos_day", day=day))

    # Load todos from all facets
    try:
        facet_map = get_facets()
    except Exception as exc:  # pragma: no cover - metadata is optional
        current_app.logger.debug("Failed to load facet metadata: %s", exc)
        facet_map = {}

    # Collect todos from each facet (excluding cancelled, including empty facets)
    todos_by_facet = {}
    visible_incomplete_by_facet = {}
    visible_completed_by_facet = {}
    facet_totals = {}
    for facet_name in facet_map.keys():
        facet_todos = get_todos(day, facet_name)
        if facet_todos:
            # Filter out cancelled todos and add facet info
            facet_todos = [t for t in facet_todos if not t.get("cancelled")]
            facet_todos.sort(key=lambda t: t.get("completed", False))
            for todo in facet_todos:
                todo["facet"] = facet_name
        else:
            facet_todos = []
        incomplete_list = [t for t in facet_todos if not t.get("completed")]
        completed_list = [t for t in facet_todos if t.get("completed")]
        incomplete_total = len(incomplete_list)
        completed_total = len(completed_list)
        visible_incomplete = incomplete_list[:VISIBLE_INCOMPLETE_BUDGET]
        visible_completed = completed_list[-VISIBLE_COMPLETED_BUDGET:]

        facet_totals[facet_name] = {
            "incomplete_total": incomplete_total,
            "completed_total": completed_total,
            "incomplete_hidden": max(0, incomplete_total - VISIBLE_INCOMPLETE_BUDGET),
            "completed_hidden": max(0, completed_total - VISIBLE_COMPLETED_BUDGET),
        }
        visible_incomplete_by_facet[facet_name] = visible_incomplete
        visible_completed_by_facet[facet_name] = visible_completed
        todos_by_facet[facet_name] = visible_incomplete + visible_completed

    # Sort facets for initial page load:
    # 1. Facets with incomplete items first, sorted by incomplete count (descending)
    # 2. Fully completed facets next, sorted alphabetically
    # 3. Empty facets last, sorted alphabetically
    def facet_sort_key(item):
        facet_name, _facet_todos = item
        totals = facet_totals[facet_name]
        incomplete_count = totals["incomplete_total"]
        total_count = totals["incomplete_total"] + totals["completed_total"]
        has_no_todos = total_count == 0
        all_complete = incomplete_count == 0 and total_count > 0
        # Return tuple: (has_no_todos, all_complete, -incomplete_count, facet_name)
        # has_no_todos=False sorts before has_no_todos=True (empty facets last)
        # all_complete=False sorts before all_complete=True (incomplete before complete)
        # -incomplete_count sorts higher counts first
        # facet_name for alphabetical tie-breaking
        return (has_no_todos, all_complete, -incomplete_count, facet_name)

    sorted_todos_by_facet = dict(sorted(todos_by_facet.items(), key=facet_sort_key))

    today_day = date.today().strftime("%Y%m%d")

    # Compute facet counts for facet pill badges
    facet_counts = {}
    for facet_name, totals in facet_totals.items():
        pending = totals["incomplete_total"]
        if pending > 0:
            facet_counts[facet_name] = pending

    return render_template(
        "app.html",
        title=format_date(day),
        today_day=today_day,
        todos_by_facet=sorted_todos_by_facet,
        visible_incomplete_by_facet=visible_incomplete_by_facet,
        visible_completed_by_facet=visible_completed_by_facet,
        facet_map=facet_map,
        facet_totals=facet_totals,
        facet_counts=facet_counts,
        format_nudge=format_nudge,
    )


@todos_bp.route("/<day>/overflow/<facet>/<section>", methods=["GET"])
def todos_overflow(day: str, facet: str, section: str):
    """Return todo row HTML past the visible budget."""
    if not DATE_RE.fullmatch(day):
        return "", 404

    if section not in ("incomplete", "completed"):
        return "", 400

    facet_map = get_facets()
    if facet not in facet_map:
        return "", 404

    facet_todos = get_todos(day, facet) or []
    facet_todos = [todo for todo in facet_todos if not todo.get("cancelled")]
    facet_todos.sort(key=lambda todo: todo.get("completed", False))
    for todo in facet_todos:
        todo["facet"] = facet

    incomplete_list = [todo for todo in facet_todos if not todo.get("completed")]
    completed_list = [todo for todo in facet_todos if todo.get("completed")]
    if section == "incomplete":
        hidden = incomplete_list[VISIBLE_INCOMPLETE_BUDGET:]
    else:
        hidden = completed_list[:-VISIBLE_COMPLETED_BUDGET]

    return "\n".join(
        render_template(
            "todos/_row.html",
            item=item,
            facet_name=facet,
            day=day,
            format_nudge=format_nudge,
        )
        for item in hidden
    )


@todos_bp.route("/<day>/move", methods=["POST"])
def move_todo(day: str):  # type: ignore[override]
    """Move a todo to a different day by cancelling source and adding to target."""
    if not DATE_RE.fullmatch(day):
        return error_response(
            INVALID_DAY,
            status=404,
            detail="Day must be in YYYYMMDD format.",
        )

    payload = request.get_json(silent=True) or {}
    target_day = (payload.get("target_day") or "").strip()
    facet = (payload.get("facet") or "personal").strip()
    index_value = payload.get("index")

    if not DATE_RE.fullmatch(target_day):
        return error_response(INVALID_DAY, detail="Please pick a valid target day.")

    try:
        index = int(index_value)
    except (TypeError, ValueError):
        return error_response(MISSING_REQUIRED_FIELD, detail="Missing todo index.")

    if index <= 0:
        return error_response(
            INVALID_REQUEST_VALUE, detail="Todo index must be positive."
        )

    if target_day == day:
        return jsonify(
            {
                "status": "noop",
                "message": "Todo is already on that day.",
                "redirect": url_for("app:todos.todos_day", day=day),
            }
        )

    # L9: replaying an already-moved item returns 409 without appending again.
    try:
        _created, cancelled = TodoChecklist.move_entry(
            day, facet, index, target_day, facet
        )
    except (IndexError, TodoNotMovableError, TodoMovePartialError, LockTimeout) as exc:
        current_app.logger.debug(
            "Failed to move todo %s from %s/%s to %s/%s: %s",
            index,
            facet,
            day,
            facet,
            target_day,
            exc,
        )
        return error_response(
            OPERATION_NO_LONGER_AVAILABLE,
            status=409,
            detail=f"Todo list changed, {CONVEY_RELOAD_HINT}",
        )
    except TodoEmptyTextError as exc:
        current_app.logger.debug("Failed to append todo to %s: %s", target_day, exc)
        return error_response(
            TODO_OPERATION_FAILED,
            status=400,
            detail="Unable to move todo to the selected day.",
        )

    log_app_action(
        app="todos",
        facet=facet,
        action="todo_move",
        params={
            "source_day": day,
            "target_day": target_day,
            "line_number": index,
            "text": cancelled.text,
            "completed": cancelled.completed,
        },
        day=day,
    )

    redirect_url = url_for("app:todos.todos_day", day=target_day)
    counts = _compute_badge_counts(day, facet)
    return jsonify(
        {
            "status": "ok",
            "redirect": redirect_url,
            "target_day": target_day,
            **counts,
        }
    )


@todos_bp.route("/<day>/generate", methods=["POST"])
def generate_todos(day: str):  # type: ignore[override]
    if not DATE_RE.fullmatch(day):
        return error_response(
            INVALID_DAY,
            status=404,
            detail="Day must be in YYYYMMDD format.",
        )

    payload = request.get_json(silent=True) or {}
    facet = (payload.get("facet") or "personal").strip()

    day_date = datetime.strptime(day, "%Y%m%d")
    yesterday = (day_date - timedelta(days=1)).strftime("%Y%m%d")
    yesterday_path = _todo_path(yesterday, facet)

    yesterday_content = ""
    if yesterday_path.exists():
        try:
            yesterday_content = yesterday_path.read_text(encoding="utf-8")
        except OSError:
            yesterday_content = ""

    prompt = f"""Generate a TODO checklist for {day_date.strftime("%Y-%m-%d")} in the {facet} facet.

Current date/time: {datetime.now().strftime("%Y-%m-%d %H:%M")}
Target day: {day_date.strftime("%Y-%m-%d")}
Target facet: {facet}
Target file: facets/{facet}/todos/{day}.jsonl

Yesterday's todos content:
{yesterday_content if yesterday_content else "(No todos recorded yesterday)"}

Write the generated checklist to facets/{facet}/todos/{day}.jsonl"""

    try:
        from solstone.convey.utils import spawn_agent

        use_id = spawn_agent(
            prompt=prompt,
            name="todos:todo",
            provider="openai",
            config={},
        )
    except Exception as exc:  # pragma: no cover - network/agent failure
        return error_response(
            AGENT_UNAVAILABLE,
            status=500,
            detail=f"Failed to spawn agent: {exc}",
        )

    if use_id is None:
        return error_response(
            AGENT_UNAVAILABLE,
            detail="Failed to connect to agent service",
        )

    if not hasattr(state, "todo_generation_agents"):
        state.todo_generation_agents = {}
    state.todo_generation_agents[day] = use_id

    return jsonify({"use_id": use_id, "status": "started"})


@todos_bp.route("/<day>/generation-status")
def todo_generation_status(day: str):  # type: ignore[override]
    if not DATE_RE.fullmatch(day):
        return error_response(
            INVALID_DAY,
            status=404,
            detail="Day must be in YYYYMMDD format.",
        )

    facet = request.args.get("facet", "personal")
    use_id = request.args.get("use_id")
    if not use_id and hasattr(state, "todo_generation_agents"):
        use_id = state.todo_generation_agents.get(day)

    if not use_id:
        return jsonify({"status": "none", "use_id": None})

    from solstone.think.cortex_client import cortex_uses

    todo_path = _todo_path(day, facet)

    talents_dir = Path(state.journal_root) / "talents"
    use_file = next(talents_dir.glob(f"*/{use_id}.jsonl"), None)

    if use_file and use_file.exists():
        if todo_path.exists():
            if (
                hasattr(state, "todo_generation_agents")
                and day in state.todo_generation_agents
            ):
                del state.todo_generation_agents[day]
            return jsonify(
                {"status": "finished", "use_id": use_id, "todo_created": True}
            )
        return jsonify({"status": "finished", "use_id": use_id, "todo_created": False})

    try:
        response = cortex_uses(limit=100, offset=0)
        if response:
            uses = response.get("uses", [])
            for use in uses:
                if use.get("id") == use_id:
                    return jsonify({"status": "running", "use_id": use_id})
            return jsonify({"status": "unknown", "use_id": use_id})
    except Exception:  # pragma: no cover - external call failure
        pass

    return jsonify({"status": "unknown", "use_id": use_id})


@todos_bp.route("/<day>/generate-weekly/<facet>", methods=["POST"])
def generate_weekly_todos(day: str, facet: str):  # type: ignore[override]
    """Spawn todo_weekly agent for a specific facet."""
    if not DATE_RE.fullmatch(day):
        return error_response(
            INVALID_DAY,
            status=404,
            detail="Day must be in YYYYMMDD format.",
        )

    day_date = datetime.strptime(day, "%Y%m%d")

    prompt = f"""Review the past week and generate high-impact todos for {facet} facet.

Current date/time: {datetime.now().strftime("%Y-%m-%d %H:%M")}
Target day: {day_date.strftime("%Y-%m-%d")}
Target facet: {facet}
Target file: facets/{facet}/todos/{day}.jsonl

Focus on surfacing the most important unfinished work from the past 7 days."""

    try:
        from solstone.convey.utils import spawn_agent

        use_id = spawn_agent(
            prompt=prompt,
            name="todos:weekly",
            provider="openai",
            config={},
        )
    except Exception as exc:  # pragma: no cover - network/agent failure
        return error_response(
            AGENT_UNAVAILABLE,
            status=500,
            detail=f"Failed to spawn agent: {exc}",
        )

    if use_id is None:
        return error_response(
            AGENT_UNAVAILABLE,
            detail="Failed to connect to agent service",
        )

    return jsonify({"use_id": use_id, "status": "started"})
