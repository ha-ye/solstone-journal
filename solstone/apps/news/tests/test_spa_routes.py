# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from solstone.convey import create_app

APP_ROOT = Path(__file__).resolve().parents[1]
ROUTES_PATH = APP_ROOT / "routes.py"
WORKSPACE_PATH = APP_ROOT / "workspace.html"


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
    assert set(data) == {"copy", "newsletters"}
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


def test_news_routes_render_template_only_in_pdf_helper():
    assert _render_template_call_functions() == ["_render_newsletter_pdf"]
