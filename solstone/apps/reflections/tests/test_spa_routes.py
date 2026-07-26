# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from solstone.apps.reflections import copy as reflections_copy
from solstone.convey import create_app

APP_ROOT = Path(__file__).resolve().parents[1]
ROUTES_PATH = APP_ROOT / "routes.py"
WORKSPACE_PATH = APP_ROOT / "workspace.html"


@pytest.fixture
def reflections_env(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1700000000000}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    app = create_app(journal=str(journal))
    app.config["TESTING"] = True

    class Env:
        def __init__(self) -> None:
            self.app = app
            self.client = app.test_client()
            self.journal = journal

    return Env()


def _seed_reflection(journal: Path, day: str = "20260308") -> None:
    target = journal / "reflections" / "weekly" / f"{day}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# reflection\n\nbody\n", encoding="utf-8")


def _render_template_call_functions() -> list[str]:
    tree = ast.parse(ROUTES_PATH.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    functions: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "render_template":
            continue
        parent = parents.get(node)
        while parent is not None and not isinstance(parent, ast.FunctionDef):
            parent = parents.get(parent)
        functions.append(parent.name if isinstance(parent, ast.FunctionDef) else "")
    return functions


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"function {name} is not closed")


def test_reflections_page_routes_serve_spa_shell(reflections_env):
    for path in (
        "/app/reflections/",
        "/app/reflections/sample",
        "/app/reflections/20260308",
        "/app/reflections/notaday",
    ):
        response = reflections_env.client.get(path)
        assert response.status_code == 200
        assert b'data-solstone-shell="spa"' in response.data


def test_reflections_workspace_is_served_verbatim(reflections_env):
    response = reflections_env.client.get("/app/reflections/workspace")

    assert response.status_code == 200
    assert response.data == WORKSPACE_PATH.read_bytes()


def test_reflections_api_routes_resolve(reflections_env):
    adapter = reflections_env.app.url_map.bind("localhost")

    expected = {
        "/app/reflections/api/state": "app:reflections.api_state",
        "/app/reflections/api/index": "app:reflections.api_index",
        "/app/reflections/api/grid": "app:reflections.api_grid",
        "/app/reflections/api/sample": "app:reflections.api_sample",
        "/app/reflections/api/20260308": "app:reflections.api_week",
        "/app/reflections/api/stats/202603": "app:reflections.api_stats",
    }
    for path, endpoint in expected.items():
        matched, _args = adapter.match(path, method="GET")
        assert matched == endpoint


def test_reflections_state_payload_shape(reflections_env, monkeypatch):
    _seed_reflection(reflections_env.journal)
    monkeypatch.setattr(
        "solstone.apps.reflections.routes.next_reflection_sunday",
        lambda journal, today, tz: "Sunday, March 15",
    )

    response = reflections_env.client.get("/app/reflections/api/state")
    data = response.get_json()

    assert response.status_code == 200
    assert set(data) == {"copy", "weeks"}
    assert data["weeks"] == [
        {
            "day": "20260308",
            "label": "Sunday March 8th",
            "url": "/app/reflections/20260308",
        }
    ]
    assert data["copy"]["empty_next"] == (
        "Your first reflection arrives on Sunday, March 15."
    )
    assert data["copy"]["populated_next_footer"] == "next reflection: Sunday, March 15"
    assert data["copy"]["grid_lede"] == reflections_copy.GRID_LEDE_ONE.format(
        count=1, month="March 2026"
    )


def test_reflections_empty_body_matches_approved_replacement():
    assert reflections_copy.EMPTY_BODY == (
        "Every Sunday, sol writes one reflection from the week you've just lived: "
        "the conversations, decisions, follow-ups, the people. A view of your week, "
        "in your journal, with sol's notes."
    )


def test_reflections_workspace_missing_detail_uses_copy_gated_empty_surface():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    render_empty = _function_source(source, "renderDetailEmpty")
    load = _function_source(source, "load")

    assert "window.SurfaceState.empty({" in render_empty
    assert "window.ConveyIcons.svg('calendar-days')" in render_empty
    assert "heading: copy.heading" in render_empty
    assert "desc: copy.desc" in render_empty
    assert "err?.status === 404 && err?.payload?.copy" in load
    assert "renderDetailEmpty(err.payload.copy);" in load
    assert "renderError(err, load);" in load
    assert load.index("renderDetailEmpty(err.payload.copy);") < load.index(
        "renderError(err, load);"
    )


def test_reflections_workspace_non_copy_errors_fall_through_to_retry_error_surface():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    render_error = _function_source(source, "renderError")
    load = _function_source(source, "load")

    assert "window.SurfaceState.error({" in render_error
    assert "retry: true" in render_error
    assert re.search(
        r"if \(err\?\.status === 404 && err\?\.payload\?\.copy\) \{"
        r"[\s\S]*?renderDetailEmpty\(err\.payload\.copy\);"
        r"[\s\S]*?return;"
        r"[\s\S]*?\}"
        r"\s*renderError\(err, load\);",
        load,
    )


def test_reflections_index_payload_shape(reflections_env):
    _seed_reflection(reflections_env.journal, "20260308")
    _seed_reflection(reflections_env.journal, "20260405")

    response = reflections_env.client.get("/app/reflections/api/index")
    data = response.get_json()

    assert response.status_code == 200
    assert data == {
        "coverage": {"start": "20260308", "end": "20260405"},
        "months": {"202603": 1, "202604": 1},
    }


def test_reflections_grid_payload_normalizes_weeks_and_uses_owner_today(
    reflections_env, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 10, 12, 0, tzinfo=tz)

    _seed_reflection(reflections_env.journal, "20260308")
    _seed_reflection(reflections_env.journal, "20260310")
    _seed_reflection(reflections_env.journal, "20260405")
    monkeypatch.setattr(
        "solstone.apps.reflections.routes.get_owner_timezone",
        lambda: ZoneInfo("UTC"),
    )
    monkeypatch.setattr("solstone.apps.reflections.routes.datetime", FrozenDateTime)

    response = reflections_env.client.get("/app/reflections/api/grid")
    data = response.get_json()

    assert response.status_code == 200
    assert data == {
        "coverage": {"start": "20260308", "end": "20260410"},
        "days": {"20260308": 1, "20260405": 1},
        "pending": {},
    }


def test_reflections_state_lede_count_matches_grid_days(reflections_env, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 10, 12, 0, tzinfo=tz)

    _seed_reflection(reflections_env.journal, "20260308")
    _seed_reflection(reflections_env.journal, "20260310")
    _seed_reflection(reflections_env.journal, "20260322")
    monkeypatch.setattr(
        "solstone.apps.reflections.routes.get_owner_timezone",
        lambda: ZoneInfo("UTC"),
    )
    monkeypatch.setattr("solstone.apps.reflections.routes.datetime", FrozenDateTime)

    state_response = reflections_env.client.get("/app/reflections/api/state")
    grid_response = reflections_env.client.get("/app/reflections/api/grid")
    state_data = state_response.get_json()
    grid_data = grid_response.get_json()

    assert state_response.status_code == 200
    assert grid_response.status_code == 200
    expected_count = len(grid_data["days"])
    coverage_start = grid_data["coverage"]["start"]
    month = datetime.strptime(coverage_start, "%Y%m%d").strftime("%B %Y")
    assert expected_count > 1
    assert expected_count < len(state_data["weeks"])
    assert state_data["copy"]["grid_lede"] == reflections_copy.GRID_LEDE_OTHER.format(
        count=expected_count, month=month
    )


def test_reflections_grid_empty_journal_returns_empty_maps(reflections_env):
    response = reflections_env.client.get("/app/reflections/api/grid")

    assert response.status_code == 200
    assert response.get_json() == {
        "coverage": None,
        "days": {},
        "pending": {},
    }


def test_reflections_detail_payload_canonicalizes(reflections_env):
    _seed_reflection(reflections_env.journal)

    response = reflections_env.client.get("/app/reflections/api/20260310")
    data = response.get_json()

    assert response.status_code == 200
    assert data["day"] == "20260308"
    assert data["week_label"] == "Sunday March 8th"
    assert data["raw_url"] == "/app/reflections/20260308/raw"
    assert data["pdf_url"] == "/app/reflections/20260308/pdf"


def test_reflections_routes_render_template_only_in_pdf_helper():
    assert _render_template_call_functions() == ["_render_reflection_pdf"]


def test_week_arrow_targets_stay_sundays(reflections_env):
    from solstone.apps.reflections.routes import _canonical_week_day

    start = datetime.strptime("20260308", "%Y%m%d")
    for offset in range(-28, 35, 7):
        target = (start + timedelta(days=offset)).strftime("%Y%m%d")
        assert _canonical_week_day(target) == target
