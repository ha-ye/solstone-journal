# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared helpers for sandbox profile tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from solstone.think.sandbox_profile import cli, manifest

RUN_ID = "86d9eb6c-d64e-4ae5-b29e-524ddf57a013"
OTHER_RUN_ID = "11111111-1111-4111-8111-111111111111"


def write_marker(
    journal: Path,
    *,
    run_id: str = RUN_ID,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    marker_payload = {
        "kind": manifest.MARKER_KIND,
        "contract_version": manifest.CONTRACT_VERSION,
        "profile": manifest.PROFILE,
        "run_id": run_id,
        "journal_path": str(journal.resolve()),
    }
    if payload is not None:
        marker_payload = payload
    journal.mkdir(parents=True, exist_ok=True)
    (journal / ".solstone-sandbox.json").write_text(
        json.dumps(marker_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    return marker_payload


def sandbox_journal(tmp_path: Path, monkeypatch, *, run_id: str = RUN_ID) -> Path:
    journal = tmp_path / "journal"
    write_marker(journal, run_id=run_id)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal.resolve()))
    return journal


def invoke(args: list[str], *, input_text: str | None = None):
    return CliRunner().invoke(
        cli.app,
        args,
        input=input_text,
        catch_exceptions=False,
    )


def output_json(result) -> dict[str, Any]:
    return json.loads(result.output)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text("utf-8"))


def scout_payload(secret: str = "fake-google-key") -> dict[str, str]:
    return {
        "google_api_key": secret,
        "dispatch_token": "dispatch-token",
        "account_id": "acct-scout",
        "created_at": "2026-01-01T00:00:00Z",
    }


def spl_payload() -> dict[str, object]:
    return {
        "service": "spl",
        "state": "approved",
        "approved_at": "2026-01-01T00:00:00Z",
    }


def spb_payload(journal: Path, *, instance_id: str | None = None) -> dict[str, str]:
    state = read_json(journal / "link" / "state.json")
    return {
        "broker_endpoint": "https://broker.example.invalid",
        "account_id": "acct-backup",
        "instance_id": instance_id or str(state["instance_id"]),
        "bucket": "sandbox-bucket",
        "prefix": "sandbox-prefix",
        "broker_token": "broker-token",
    }


def spp_payload(*, endpoint_url: str = "http://127.0.0.1:9100") -> dict[str, str]:
    return {
        "endpoint_url": endpoint_url,
        "served_model_id": "synthetic-model",
        "credential": "spp-secret",
        "account_id": "acct-spp",
        "created_at": "2026-01-01T00:00:00Z",
    }


def prepare_ok(journal: Path) -> dict[str, Any]:
    result = invoke(["prepare", "--json"])
    assert result.exit_code == 0, result.output
    assert (journal / "health" / "sandbox-profile" / "intent.json").exists()
    return output_json(result)
