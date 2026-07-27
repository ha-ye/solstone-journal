# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from solstone.apps.news import routes as news_routes
from solstone.convey import create_app

APP_ROOT = Path(__file__).resolve().parents[1]
ROUTES_PATH = APP_ROOT / "routes.py"
WORKSPACE_PATH = APP_ROOT / "workspace.html"
CONVEY_ICONS_JS = APP_ROOT.parents[1] / "convey" / "static" / "convey_icons.js"


@pytest.fixture
def news_env(tmp_path, monkeypatch):
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


def _seed_news(journal: Path) -> None:
    (journal / "chronicle" / "20260526").mkdir(parents=True)
    target = journal / "facets" / "verona" / "news" / "20260526.md"
    target.parent.mkdir(parents=True)
    target.write_text("# verona\n\nbody\n", encoding="utf-8")


def _seed_news_for(
    journal: Path, facet: str, day: str, body: str | None = None
) -> None:
    (journal / "chronicle" / day).mkdir(parents=True, exist_ok=True)
    target = journal / "facets" / facet / "news" / f"{day}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body or f"# {facet} {day}\n\nbody\n", encoding="utf-8")


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
    marker = f"function {name}("
    start = source.index(marker)
    open_paren = source.index("(", start)
    paren_depth = 0
    close_paren = -1
    for index in range(open_paren, len(source)):
        char = source[index]
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close_paren = index
                break
    assert close_paren != -1, f"function {name} has no closing parameter list"
    brace = source.index("{", close_paren)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"function {name} has no closing brace")


def test_news_page_routes_serve_spa_shell(news_env):
    for path in (
        "/app/news/",
        "/app/news/sample",
        "/app/news/verona/20260526",
        "/app/news/bad/notaday",
    ):
        response = news_env.client.get(path)
        assert response.status_code == 200
        assert b'data-solstone-shell="spa"' in response.data


def test_news_workspace_is_served_verbatim(news_env):
    response = news_env.client.get("/app/news/workspace")

    assert response.status_code == 200
    assert response.data == WORKSPACE_PATH.read_bytes()


def test_news_api_routes_resolve(news_env):
    adapter = news_env.app.url_map.bind("localhost")

    expected = {
        "/app/news/api/state": "app:news.api_state",
        "/app/news/api/sample": "app:news.api_sample",
        "/app/news/api/verona/20260526": "app:news.api_detail",
    }
    for path, endpoint in expected.items():
        matched, _args = adapter.match(path, method="GET")
        assert matched == endpoint


def test_news_state_payload_shape(news_env):
    _seed_news(news_env.journal)

    response = news_env.client.get("/app/news/api/state")
    data = response.get_json()

    assert response.status_code == 200
    assert set(data) == {"copy", "newsletters", "total_count"}
    assert data["total_count"] == 1
    assert data["newsletters"] == [
        {
            "facet": "verona",
            "day": "20260526",
            "label": "Tue May 26, 2026",
            "url": "/app/news/verona/20260526",
        }
    ]
    assert data["copy"]["empty_next"] == (
        "Your first newsletters arrive tomorrow morning."
    )
    assert data["copy"]["populated_next_footer"] == "next newsletters: tomorrow morning"
    assert data["copy"]["grid_lede"] == "1 newsletter since May 2026."


def test_news_routes_render_template_only_in_pdf_helper():
    assert _render_template_call_functions() == ["_render_newsletter_pdf"]


def test_news_state_orders_day_desc_then_facet_asc(news_env):
    _seed_news_for(news_env.journal, "zeta", "20260526")
    _seed_news_for(news_env.journal, "alpha", "20260526")
    _seed_news_for(news_env.journal, "beta", "20260525")

    response = news_env.client.get("/app/news/api/state")
    data = response.get_json()

    assert response.status_code == 200
    assert [(item["facet"], item["day"]) for item in data["newsletters"]] == [
        ("alpha", "20260526"),
        ("zeta", "20260526"),
        ("beta", "20260525"),
    ]


def test_news_state_bounds_to_newest_sixty_with_full_total(news_env):
    start = date(2026, 1, 1)
    days: list[str] = []
    for offset in range(65):
        day = (start + timedelta(days=offset)).strftime("%Y%m%d")
        days.append(day)
        _seed_news_for(news_env.journal, "verona", day)

    response = news_env.client.get("/app/news/api/state")
    data = response.get_json()

    assert response.status_code == 200
    assert data["total_count"] == 65
    assert len(data["newsletters"]) == 60
    assert [item["day"] for item in data["newsletters"]] == list(reversed(days[5:]))
    assert data["copy"]["grid_lede"] == "65 newsletters since January 2026."


def test_news_grid_payload_counts_coverage_and_watermark(news_env, monkeypatch):
    _seed_news_for(news_env.journal, "alpha", "20260105")
    _seed_news_for(news_env.journal, "zeta", "20260105")
    _seed_news_for(news_env.journal, "beta", "20260201")
    monkeypatch.setattr(news_routes, "_today", lambda: date(2026, 7, 17))
    calls: list[tuple[dict[str, int], str | None, dict[str, object]]] = []
    real_builder = news_routes.build_day_grid_payload

    def spy(counts, watermark, **kwargs):
        calls.append((dict(counts), watermark, dict(kwargs)))
        return real_builder(counts, watermark, **kwargs)

    monkeypatch.setattr(news_routes, "build_day_grid_payload", spy)

    response = news_env.client.get("/app/news/api/grid")
    data = response.get_json()

    assert response.status_code == 200
    assert calls == [
        (
            {"20260201": 1, "20260105": 2},
            "20260201",
            {"coverage": {"start": "20260105", "end": "20260717"}},
        )
    ]
    assert data == {
        "coverage": {"start": "20260105", "end": "20260717"},
        "days": {"20260105": 2, "20260201": 1},
        "pending": {},
    }


def test_news_grid_empty_journal(news_env):
    response = news_env.client.get("/app/news/api/grid")

    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "days": {}, "pending": {}}


def test_news_index_uses_date_nav_index(news_env, monkeypatch):
    _seed_news_for(news_env.journal, "alpha", "20260526")
    _seed_news_for(news_env.journal, "zeta", "20260526")
    _seed_news_for(news_env.journal, "beta", "20260601")
    calls: list[dict[str, int]] = []

    def spy(counts):
        calls.append(dict(counts))
        return {"coverage": {"start": "20260526", "end": "20260601"}, "months": {}}

    monkeypatch.setattr(news_routes, "build_date_nav_index", spy)

    response = news_env.client.get("/app/news/api/index")

    assert response.status_code == 200
    assert calls == [{"20260601": 1, "20260526": 2}]
    assert response.get_json() == {
        "coverage": {"start": "20260526", "end": "20260601"},
        "months": {},
    }


def test_news_index_empty_journal(news_env):
    response = news_env.client.get("/app/news/api/index")

    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "months": {}}


def test_news_stats_month_counts_and_index_cross_check(news_env):
    _seed_news_for(news_env.journal, "alpha", "20260526")
    _seed_news_for(news_env.journal, "zeta", "20260526")
    _seed_news_for(news_env.journal, "beta", "20260601")

    may_response = news_env.client.get("/app/news/api/stats/202605")
    empty_response = news_env.client.get("/app/news/api/stats/202604")
    invalid_response = news_env.client.get("/app/news/api/stats/2026aa")
    index = news_env.client.get("/app/news/api/index").get_json()

    assert may_response.status_code == 200
    assert may_response.get_json() == {"20260526": 2}
    assert empty_response.status_code == 200
    assert empty_response.get_json() == {}
    assert invalid_response.status_code == 400
    invalid = invalid_response.get_json()
    assert invalid["reason_code"] == "invalid_month"
    assert invalid["detail"] == "Invalid month format, expected YYYYMM"
    for month, total in index["months"].items():
        stats = news_env.client.get(f"/app/news/api/stats/{month}").get_json()
        assert sum(stats.values()) == total


def test_news_day_page_and_api(news_env):
    _seed_news_for(news_env.journal, "zeta", "20260526")
    _seed_news_for(news_env.journal, "alpha", "20260526")

    page_response = news_env.client.get("/app/news/20260526")
    garbage_response = news_env.client.get("/app/news/garbage")
    api_response = news_env.client.get("/app/news/api/day/20260526")
    empty_response = news_env.client.get("/app/news/api/day/20260527")
    invalid_response = news_env.client.get("/app/news/api/day/garbage")

    assert page_response.status_code == 200
    assert b'data-solstone-shell="spa"' in page_response.data
    assert garbage_response.status_code == 404
    assert garbage_response.get_json()["reason_code"] == "invalid_day"
    assert api_response.status_code == 200
    data = api_response.get_json()
    assert data["day"] == "20260526"
    assert data["date_label"] == "Tue May 26, 2026"
    assert [
        (item["facet"], item["label"], item["url"]) for item in data["newsletters"]
    ] == [
        ("alpha", "Tue May 26, 2026", "/app/news/alpha/20260526"),
        ("zeta", "Tue May 26, 2026", "/app/news/zeta/20260526"),
    ]
    assert empty_response.status_code == 200
    empty = empty_response.get_json()
    assert empty["empty"] is True
    assert "reason_code" not in empty
    assert invalid_response.status_code == 404
    assert invalid_response.get_json()["reason_code"] == "invalid_day"


def test_news_detail_empty_and_malformed_paths(news_env):
    _seed_news_for(news_env.journal, "alpha", "20260526")

    existing_response = news_env.client.get("/app/news/api/alpha/20260526")
    missing_response = news_env.client.get("/app/news/api/zeta/20260526")
    malformed_response = news_env.client.get("/app/news/api/alpha/notaday")

    assert existing_response.status_code == 200
    existing = existing_response.get_json()
    assert "empty" not in existing
    assert existing["raw_url"] == "/app/news/alpha/20260526/raw"
    assert existing["pdf_url"] == "/app/news/alpha/20260526/pdf"
    assert existing["debug_link_url"] == "/app/sol/20260526/talents/facet_newsletter"
    assert missing_response.status_code == 200
    missing = missing_response.get_json()
    assert missing["empty"] is True
    assert missing["day_url"] == "/app/news/20260526"
    assert "reason_code" not in missing
    assert malformed_response.status_code == 404
    assert malformed_response.get_json()["reason_code"] == "file_not_found"


def test_news_workspace_day_render_source_hooks():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    day_slice = _function_source(source, "renderDay")

    assert "renderDetailEmpty" not in day_slice
    assert "copy.subtitle" in day_slice
    assert "copy.empty_body" in day_slice
    assert "copy.title" not in day_slice
    assert "copy.empty_title" not in day_slice


def test_news_workspace_index_empty_uses_surface_state_slots():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    render_index = _function_source(source, "renderIndex")
    empty_call = render_index[
        render_index.index("window.SurfaceState.empty(") : render_index.index(
            "        : `",
            render_index.index("window.SurfaceState.empty("),
        )
    ]

    assert "window.SurfaceState.empty(" in render_index
    assert ": (window.SurfaceState" in render_index
    assert re.search(r"heading:\s*copy\.empty_next\b", render_index)
    assert re.search(r"desc:\s*copy\.empty_body\b", render_index)
    assert "copy.empty_until_then" in render_index
    assert "copy.sample_url" in render_index
    assert "copy.sample_link_label" in render_index
    assert "escapeHtml(copy.empty_next)" not in empty_call
    assert "escapeHtml(copy.empty_body)" not in empty_call
    assert '<a href="${escapeHtml(copy.sample_url)}"' in empty_call


def test_news_workspace_day_empty_uses_surface_state_desc_slot():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    render_day = _function_source(source, "renderDay")
    empty_call = render_day[
        render_day.index("window.SurfaceState.empty(") : render_day.index(
            "        : `",
            render_day.index("window.SurfaceState.empty("),
        )
    ]

    assert "window.SurfaceState.empty(" in render_day
    assert "? (window.SurfaceState" in render_day
    assert re.search(r"desc:\s*copy\.empty_body\b", render_day)
    assert "escapeHtml(copy.empty_body)" not in empty_call
    assert "escapeHtml(copy.empty_next)" not in empty_call


def test_news_workspace_empty_state_icon_is_registered():
    assert '"mailbox":' in CONVEY_ICONS_JS.read_text(encoding="utf-8")


def test_news_workspace_detail_empty_source_hooks():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    detail_empty_slice = _function_source(source, "renderDetailEmpty")

    assert "copy.empty_title" in detail_empty_slice
    assert "copy.empty_body" in detail_empty_slice


def test_news_workspace_day_axis_source_hooks(news_env):
    source = WORKSPACE_PATH.read_text(encoding="utf-8")

    assert "data-date-nav" in source
    assert "data-date-nav-heading" in source
    assert "mode: 'day'" in source
    assert "/app/news/api/day/" in source
    assert "if (payload.empty)" in source
    assert "context.mode !== 'detail'" in source
    assert "context.mode !== 'sample'" not in source
    assert "minSpanDays: 70, minActiveDays: 14" in source
    assert "fetch('/app/news/api/grid'" in source
    assert "hideNewsGrid(card, host, legend, unit);" in source
