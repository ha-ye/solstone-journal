# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.think.sandbox_profile import probe_contract, probe_records
from solstone.think.sandbox_profile.probe_replay import replay_probe_ledger
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests.sandbox_profile import (
    complete_attempt_records,
    write_attempt_dir,
    write_ledger,
)


def _attempt_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def test_byte_size_bound_precedes_read_and_malformed_content(tmp_path) -> None:
    journal = tmp_path / "journal"
    path = probe_contract.probe_ledger_path(journal)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{" + (b"x" * probe_contract.MAX_LEDGER_BYTES))

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        replay_probe_ledger(journal)

    assert excinfo.value.code == probe_contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED


def test_malformed_framing_precedes_attempt_count_bound(tmp_path) -> None:
    journal = tmp_path / "journal"
    path = probe_contract.probe_ledger_path(journal)
    path.parent.mkdir(parents=True)
    start = {
        "record_kind": probe_contract.RECORD_KIND_ATTEMPT_STARTED,
        "attempt_id": _attempt_id(0),
    }
    lines = [
        json.dumps({**start, "attempt_id": _attempt_id(index)})
        for index in range(probe_contract.MAX_ATTEMPTS)
    ]
    path.write_text("\n".join(lines) + "\n{", encoding="utf-8")

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        replay_probe_ledger(journal)

    assert excinfo.value.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT


def test_attempt_count_bound_precedes_semantic_validation(tmp_path) -> None:
    journal = tmp_path / "journal"
    records: list[dict[str, object]] = []
    for index in range(probe_contract.MAX_ATTEMPTS):
        attempt_id = _attempt_id(index)
        write_attempt_dir(journal, attempt_id)
        records.extend(complete_attempt_records(attempt_id=attempt_id))
    records[0]["unknown"] = True
    write_ledger(journal, records)

    before = repository_inventory(Path.cwd())
    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        replay_probe_ledger(journal)
    after = repository_inventory(Path.cwd())

    assert excinfo.value.code == probe_contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED
    assert_inventory_unchanged(before, after)
