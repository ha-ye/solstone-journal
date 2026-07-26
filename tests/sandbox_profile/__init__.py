# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared helpers for sandbox profile tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from solstone.think.sandbox_profile import cli, manifest, probe_contract, probe_records

RUN_ID = "86d9eb6c-d64e-4ae5-b29e-524ddf57a013"
OTHER_RUN_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
THIRD_ATTEMPT_ID = "44444444-4444-4444-8444-444444444444"
FIXED_TS = "2026-01-01T00:00:00.000Z"


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


def write_attempt_dir(
    journal: Path, attempt_id: str = ATTEMPT_ID, *, mode: int = 0o700
) -> Path:
    path = probe_contract.probe_attempts_parent_path(journal) / attempt_id
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def write_ledger(journal: Path, records: list[dict[str, object]]) -> Path:
    path = probe_contract.probe_ledger_path(journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for record in records
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def start_record(
    *,
    run_id: str = RUN_ID,
    attempt_id: str = ATTEMPT_ID,
    selected: tuple[str, ...] | None = None,
    execution_order: tuple[str, ...] | None = None,
) -> dict[str, object]:
    selected = selected or probe_contract.CAPABILITY_ORDER[:1]
    execution_order = execution_order or selected
    return probe_records.build_attempt_started_record(
        run_id=run_id,
        attempt_id=attempt_id,
        selected=selected,
        execution_order=execution_order,
        started_at=FIXED_TS,
    ).to_json_obj()


def proof_record(
    *,
    run_id: str = RUN_ID,
    attempt_id: str = ATTEMPT_ID,
    proof: str | None = None,
    state: str = probe_contract.PROOF_STATE_PASSED,
    checks: tuple[str, ...] | None = None,
    reason: str | None = None,
    duration_ms: int | None = 1,
) -> dict[str, object]:
    proof = proof or probe_contract.CAPABILITY_ORDER[0]
    if checks is None:
        checks = (
            probe_contract.PROOF_CHECKS[proof]
            if state == probe_contract.PROOF_STATE_PASSED
            else ()
        )
    return probe_records.build_proof_terminal_record(
        run_id=run_id,
        attempt_id=attempt_id,
        proof=proof,
        state=state,
        checks=checks,
        reason=reason,
        duration_ms=duration_ms,
        finished_at=FIXED_TS,
    ).to_json_obj()


def terminal_record(
    *,
    run_id: str = RUN_ID,
    attempt_id: str = ATTEMPT_ID,
    proofs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    proof_records = [
        probe_records.validate_proof_terminal_payload(proof)
        for proof in (proofs or [proof_record()])
    ]
    return probe_records.build_attempt_terminal_record(
        run_id=run_id,
        attempt_id=attempt_id,
        proofs=proof_records,
        finished_at=FIXED_TS,
    ).to_json_obj()


def complete_attempt_records(
    *,
    run_id: str = RUN_ID,
    attempt_id: str = ATTEMPT_ID,
    selected: tuple[str, ...] | None = None,
    execution_order: tuple[str, ...] | None = None,
    proof_overrides: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    selected = selected or probe_contract.CAPABILITY_ORDER[:1]
    execution_order = execution_order or selected
    records: list[dict[str, object]] = [
        start_record(
            run_id=run_id,
            attempt_id=attempt_id,
            selected=selected,
            execution_order=execution_order,
        )
    ]
    proofs: list[dict[str, object]] = []
    for proof in execution_order:
        override = (proof_overrides or {}).get(proof, {})
        proof_payload = proof_record(
            run_id=run_id,
            attempt_id=attempt_id,
            proof=proof,
            state=override.get("state", probe_contract.PROOF_STATE_PASSED),  # type: ignore[arg-type]
            checks=override.get("checks"),  # type: ignore[arg-type]
            reason=override.get("reason"),  # type: ignore[arg-type]
            duration_ms=override.get("duration_ms", 1),  # type: ignore[arg-type]
        )
        proofs.append(proof_payload)
        records.append(proof_payload)
    records.append(
        terminal_record(
            run_id=run_id,
            attempt_id=attempt_id,
            proofs=proofs,
        )
    )
    return records


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
