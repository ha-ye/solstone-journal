# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI parity tests for entity trust-core routes."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from solstone.apps.entities import call as entity_call
from solstone.apps.entities.call import app as entities_app
from solstone.think.convey_client import ConveyClientError

runner = CliRunner()


@pytest.fixture
def request_spy(monkeypatch):
    calls: list[tuple[str, str, dict | None, dict | None]] = []

    def request(method, path, *, params=None, json_body=None):
        calls.append((method, path, params, json_body))
        if path == "/app/entities/api/ambiguities":
            return {
                "items": [
                    {
                        "ambiguity_id": "amb_one",
                        "original_query": "Sarah",
                        "status": "open",
                        "scope": {"kind": "journal"},
                        "observed_tier": 5,
                        "origins": [{"lane": "speaker.discovery"}],
                        "ranked_candidates": [
                            {"id": "sarah_lee", "name": "Sarah Lee", "score": 90}
                        ],
                    }
                ]
            }
        if path.endswith("/resolve"):
            return {
                "ambiguity": {
                    "ambiguity_id": "amb_one",
                    "resolved_at": "2026-07-14T12:00:00Z",
                },
                "entity": {"id": "sarah_lee", "name": "Sarah Lee"},
            }
        if path.endswith("/history"):
            return {
                "entity_id": "sarah_lee",
                "items": [
                    {
                        "seq": 1,
                        "kind": "create",
                        "ts": "2026-07-14T11:00:00Z",
                        "version_id": "vh_one",
                    }
                ],
            }
        if path.endswith("/restore"):
            return {
                "restored": True,
                "event": {"version_id": "vh_new", "kind": "restore"},
            }
        if path.endswith("/undo"):
            return {
                "undone": True,
                "merge_id": "em_one",
                "source_id": "source",
                "target_id": "target",
                "history_version_id": "vh_undo",
            }
        raise AssertionError(path)

    monkeypatch.setattr(entity_call, "_request", request)
    return calls


@pytest.mark.parametrize(
    "args",
    [
        ["undo-merge", "em_one"],
        ["resolve-ambiguity", "amb_one", "sarah_lee"],
        ["restore-version", "sarah_lee", "vh_one"],
    ],
)
def test_write_commands_require_yes_without_http(request_spy, args) -> None:
    result = runner.invoke(entities_app, args)
    assert result.exit_code == 1
    assert "without --yes" in result.output
    assert request_spy == []


def test_ambiguities_text_and_json_are_route_backed(request_spy) -> None:
    text = runner.invoke(entities_app, ["ambiguities", "--status", "open"])
    assert text.exit_code == 0
    assert "amb_one  Sarah  [open]" in text.output
    assert "from: speaker.discovery" in text.output
    assert "Sarah Lee (sarah_lee)" in text.output

    raw = runner.invoke(entities_app, ["ambiguities", "--json"])
    assert raw.exit_code == 0
    assert json.loads(raw.output)["items"][0]["ambiguity_id"] == "amb_one"
    assert request_spy[0][:2] == ("GET", "/app/entities/api/ambiguities")


def test_resolve_undo_history_and_restore_text_commands(request_spy) -> None:
    resolved = runner.invoke(
        entities_app,
        ["resolve-ambiguity", "amb_one", "sarah_lee", "--yes"],
    )
    assert resolved.exit_code == 0
    assert "Resolved amb_one to sarah_lee" in resolved.output

    undone = runner.invoke(entities_app, ["undo-merge", "em_one", "--yes"])
    assert undone.exit_code == 0
    assert "Undid em_one" in undone.output

    history = runner.invoke(entities_app, ["entity-history", "sarah_lee"])
    assert history.exit_code == 0
    assert "create" in history.output
    assert "vh_one" in history.output

    restored = runner.invoke(
        entities_app,
        ["restore-version", "sarah_lee", "vh_one", "--yes"],
    )
    assert restored.exit_code == 0
    assert "new history version vh_new" in restored.output

    assert [call[:2] for call in request_spy] == [
        ("POST", "/app/entities/api/ambiguities/amb_one/resolve"),
        ("POST", "/app/entities/api/merge/em_one/undo"),
        ("GET", "/app/entities/api/journal/entity/sarah_lee/history"),
        ("POST", "/app/entities/api/journal/entity/sarah_lee/restore"),
    ]


@pytest.mark.parametrize(
    "args, expected_key",
    [
        (["undo-merge", "em_one", "--yes", "--json"], "undone"),
        (
            [
                "resolve-ambiguity",
                "amb_one",
                "sarah_lee",
                "--yes",
                "--json",
            ],
            "ambiguity",
        ),
        (["entity-history", "sarah_lee", "--json"], "items"),
        (
            [
                "restore-version",
                "sarah_lee",
                "vh_one",
                "--yes",
                "--json",
            ],
            "restored",
        ),
    ],
)
def test_new_commands_json_emit_route_body(request_spy, args, expected_key) -> None:
    result = runner.invoke(entities_app, args)
    assert result.exit_code == 0
    assert expected_key in json.loads(result.output)


def test_repair_required_json_preserves_state_and_does_not_suggest_retry(
    monkeypatch,
) -> None:
    payload = {
        "error": "I couldn't complete that entity operation.",
        "reason_code": "entity_operation_failed",
        "detail": "rollback failed",
        "operation_state": "repair_required",
        "mutation_applied": True,
        "source_state": {"exists": True},
        "target_state": {"exists": True},
        "safe_remediation": "Inspect before retrying.",
    }

    def fail(*args, **kwargs):
        raise ConveyClientError(
            payload["error"],
            reason_code=payload["reason_code"],
            detail=payload["detail"],
            status=500,
            payload=payload,
        )

    monkeypatch.setattr(entity_call, "_request", fail)

    result = runner.invoke(
        entities_app,
        ["undo-merge", "em_repair", "--yes", "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["operation_state"] == "repair_required"
    assert "sol call entities undo-merge" not in result.stderr
