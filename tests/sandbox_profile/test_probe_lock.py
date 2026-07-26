# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.sandbox_profile import probe_contract, probe_records
from solstone.think.sandbox_profile.probe_slot import acquire_probe_slot
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests.sandbox_profile import RUN_ID


def test_acquire_probe_slot_is_single_process_nonblocking(tmp_path) -> None:
    journal = tmp_path / "journal"
    first = acquire_probe_slot(journal, run_id=RUN_ID)
    assert first.owned is True

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        acquire_probe_slot(journal, run_id=RUN_ID)

    assert excinfo.value.code == probe_contract.STABLE_ERROR_PROBE_ACTIVE
    first.release()


def test_probe_active_does_not_mutate_repository_inventory(tmp_path) -> None:
    journal = tmp_path / "journal"
    first = acquire_probe_slot(journal, run_id=RUN_ID)
    before = repository_inventory(Path.cwd())
    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        acquire_probe_slot(journal, run_id=RUN_ID)
    after = repository_inventory(Path.cwd())
    first.release()

    assert excinfo.value.code == probe_contract.STABLE_ERROR_PROBE_ACTIVE
    assert_inventory_unchanged(before, after)
