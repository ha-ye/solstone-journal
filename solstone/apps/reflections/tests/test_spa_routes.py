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


def _seed_reflection(journal: Path) -> None:
    target = journal / "reflections" / "weekly" / "20260308.md"
    target.parent.mkdir(parents=True)
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
