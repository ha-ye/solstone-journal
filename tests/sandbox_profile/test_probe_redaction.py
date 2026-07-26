# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys

import pytest

from solstone.think.sandbox_profile import (
    probe_contract,
    probe_durability,
    probe_records,
)
from tests.sandbox_profile import start_record


def test_write_error_secret_absent_from_exception_argv_and_logs(
    tmp_path, monkeypatch, caplog
) -> None:
    secret = "recognizable-secret-probe-token"

    def fail_open(_path):
        raise OSError(secret)

    monkeypatch.setattr(probe_durability, "_open_append", fail_open)

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        probe_durability.append_jsonl_strict(tmp_path / "ledger.jsonl", start_record())

    assert excinfo.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
    assert secret not in str(excinfo.value)
    assert secret not in caplog.text
    assert secret not in " ".join(sys.argv)
    assert excinfo.value.__cause__ is None


def test_operation_error_optional_fields_reject_free_text() -> None:
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.ProbeOperationError(
            probe_contract.STABLE_ERROR_INTERNAL_ERROR,
            attempt_id="recognizable-secret-probe-token",
        )
    with pytest.raises(ValueError):
        probe_records.ProbeOperationError(
            probe_contract.STABLE_ERROR_INTERNAL_ERROR,
            record_kind="recognizable-secret-probe-token",
        )
    with pytest.raises(ValueError):
        probe_records.ProbeOperationError(
            probe_contract.STABLE_ERROR_INTERNAL_ERROR,
            proof="recognizable-secret-probe-token",
        )
