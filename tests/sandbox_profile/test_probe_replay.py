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


def test_ok_terminal_is_retry_permitted(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    write_ledger(journal, complete_attempt_records())

    replay = replay_probe_ledger(journal)

    assert replay.retry_permitted is True
    assert replay.run_id == RUN_ID


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
