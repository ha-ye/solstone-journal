# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.sandbox_profile import probe_contract, probe_records
from solstone.think.sandbox_profile.probe_replay import replay_probe_ledger
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests.sandbox_profile import (
    ATTEMPT_ID,
    RUN_ID,
    complete_attempt_records,
    terminal_record,
    write_attempt_dir,
    write_ledger,
)


def _assert_error(journal: Path, code: str) -> None:
    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        replay_probe_ledger(journal)
    assert excinfo.value.code == code


def test_empty_ledger_is_retry_permitted(tmp_path) -> None:
    replay = replay_probe_ledger(tmp_path / "journal")

    assert replay.retry_permitted is True
    assert replay.attempt_count == 0
    assert replay.ledger_size_bytes == 0
    assert replay.ledger_identity is None


def test_ok_terminal_is_retry_permitted(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    write_ledger(journal, complete_attempt_records())

    before = repository_inventory(Path.cwd())
    replay = replay_probe_ledger(journal)
    after = repository_inventory(Path.cwd())

    assert replay.retry_permitted is True
    assert replay.run_id == RUN_ID
    ledger_stat = probe_contract.probe_ledger_path(journal).stat()
    assert replay.ledger_size_bytes == ledger_stat.st_size
    assert replay.ledger_identity == (ledger_stat.st_dev, ledger_stat.st_ino)
    assert_inventory_unchanged(before, after)


def test_degraded_proof_failed_with_verified_cleanup_is_retry_permitted(
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    proof = probe_contract.CAPABILITY_ORDER[0]
    write_attempt_dir(journal)
    write_ledger(
        journal,
        complete_attempt_records(
            proof_overrides={
                proof: {
                    "state": probe_contract.PROOF_STATE_FAILED,
                    "reason": probe_contract.PROOF_SPECIFIC_REASONS[proof][0],
                }
            }
        ),
    )

    replay = replay_probe_ledger(journal)

    assert replay.retry_permitted is True


def test_cleanup_unverified_cancelled_and_internal_terminals_are_stale(
    tmp_path,
) -> None:
    proof = probe_contract.CAPABILITY_ORDER[0]
    cases = [
        {
            "state": probe_contract.PROOF_STATE_FAILED,
            "reason": probe_contract.REASON_CLEANUP_UNVERIFIED,
        },
        {
            "state": probe_contract.PROOF_STATE_FAILED,
            "reason": probe_contract.REASON_CANCELLED,
        },
        {
            "state": probe_contract.PROOF_STATE_FAILED,
            "reason": probe_contract.REASON_INTERNAL_ERROR,
        },
    ]
    for index, override in enumerate(cases):
        journal = tmp_path / f"journal-{index}"
        write_attempt_dir(journal)
        write_ledger(
            journal, complete_attempt_records(proof_overrides={proof: override})
        )

        _assert_error(journal, probe_contract.STABLE_ERROR_STALE_ATTEMPT)


def test_incomplete_attempt_is_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    write_ledger(journal, complete_attempt_records()[:1])

    _assert_error(journal, probe_contract.STABLE_ERROR_STALE_ATTEMPT)


def test_orphan_attempt_directory_is_stale_and_does_not_mutate_repo(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)

    before = repository_inventory(Path.cwd())
    _assert_error(journal, probe_contract.STABLE_ERROR_STALE_ATTEMPT)
    after = repository_inventory(Path.cwd())

    assert_inventory_unchanged(before, after)


def test_missing_attempt_directory_is_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_ledger(journal, complete_attempt_records())

    _assert_error(journal, probe_contract.STABLE_ERROR_STALE_ATTEMPT)


def test_wrong_attempt_directory_mode_is_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal, mode=0o755)
    write_ledger(journal, complete_attempt_records())

    _assert_error(journal, probe_contract.STABLE_ERROR_STALE_ATTEMPT)


def test_attempt_directory_symlink_is_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    target = tmp_path / "target"
    target.mkdir()
    parent = probe_contract.probe_attempts_parent_path(journal)
    parent.mkdir(parents=True)
    (parent / ATTEMPT_ID).symlink_to(target, target_is_directory=True)
    write_ledger(journal, complete_attempt_records())

    _assert_error(journal, probe_contract.STABLE_ERROR_STALE_ATTEMPT)


@pytest.mark.parametrize(
    "later_override",
    [
        {
            "state": probe_contract.PROOF_STATE_PASSED,
            "checks": None,
            "reason": None,
            "duration_ms": 1,
        },
        {
            "state": probe_contract.PROOF_STATE_FAILED,
            "checks": (),
            "reason": probe_contract.REASON_INTERNAL_ERROR,
            "duration_ms": 1,
        },
        {
            "state": probe_contract.PROOF_STATE_NOT_RUN,
            "checks": (),
            "reason": probe_contract.REASON_DEPENDENCY_FAILED,
            "duration_ms": None,
        },
    ],
)
def test_cancelled_terminal_requires_exact_contiguous_not_run_suffix(
    tmp_path,
    later_override,
) -> None:
    journal = tmp_path / "journal"
    selected = probe_contract.CAPABILITY_ORDER[:2]
    records = complete_attempt_records(selected=selected)
    first_proof = records[1]
    later_proof = records[2]
    first_proof["state"] = probe_contract.PROOF_STATE_FAILED
    first_proof["checks"] = []
    first_proof["reason"] = probe_contract.REASON_CANCELLED
    first_proof["duration_ms"] = 1
    later_proof["state"] = later_override["state"]
    later_proof["checks"] = (
        list(probe_contract.PROOF_CHECKS[selected[1]])
        if later_override["checks"] is None
        else list(later_override["checks"])
    )
    later_proof["reason"] = later_override["reason"]
    later_proof["duration_ms"] = later_override["duration_ms"]
    records[-1] = {
        **terminal_record(proofs=[first_proof]),
        "attempt_id": ATTEMPT_ID,
        "state": probe_contract.ATTEMPT_STATE_CANCELLED,
        "terminal_reason": probe_contract.REASON_CANCELLED,
    }
    write_attempt_dir(journal)
    write_ledger(journal, records)

    _assert_error(journal, probe_contract.STABLE_ERROR_STALE_ATTEMPT)
