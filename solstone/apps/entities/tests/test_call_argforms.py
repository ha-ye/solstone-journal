# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI positional/option arg-form parity tests for entities commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from solstone.apps.entities.call import app as entities_app
from solstone.think.convey_client import ConveyClient
from solstone.think.indexer.journal import scan_journal
from tests._baseline_harness import make_test_client

runner = CliRunner()


@pytest.fixture
def indexed_entities_client(journal_copy, monkeypatch: pytest.MonkeyPatch) -> Path:
    scan_journal(str(journal_copy), full=True)

    def client() -> ConveyClient:
        return ConveyClient(
            session=make_test_client(journal_copy),
            base_url="",
        )

    monkeypatch.setattr("solstone.apps.entities.call.get_client", client)
    return journal_copy


def _assert_same_output(left_args: list[str], right_args: list[str]) -> None:
    left = runner.invoke(entities_app, left_args)
    right = runner.invoke(entities_app, right_args)

    assert left.exit_code == right.exit_code
    assert left.stdout == right.stdout


def test_list_positional_and_flag_forms_match(indexed_entities_client) -> None:
    _assert_same_output(["list", "montague"], ["list", "-f", "montague"])


def test_digest_positional_and_flag_forms_match(indexed_entities_client) -> None:
    _assert_same_output(
        ["digest", "montague", "--day", "20260306"],
        ["digest", "-f", "montague", "--day", "20260306"],
    )


def test_search_positional_and_flag_forms_match(indexed_entities_client) -> None:
    _assert_same_output(["search", "Romeo"], ["search", "-q", "Romeo"])
