# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.think.sandbox_profile import probe_contract, probe_records
from solstone.think.sandbox_profile.probe_replay import replay_probe_ledger
from tests.sandbox_profile import (
    ATTEMPT_ID,
    complete_attempt_records,
    write_attempt_dir,
    write_ledger,
)


def _assert_stale(journal) -> None:
    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        replay_probe_ledger(journal)
    assert excinfo.value.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT


def test_replay_accepts_arbitrary_field_order(tmp_path) -> None:
    journal = tmp_path / "journal"
    records = complete_attempt_records()
    write_attempt_dir(journal)
    path = probe_contract.probe_ledger_path(journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(dict(reversed(list(record.items())))) for record in records
        )
        + "\n",
        encoding="utf-8",
    )

    replay = replay_probe_ledger(journal)
    assert replay.attempt_count == 1


def test_replay_rejects_missing_final_lf(tmp_path) -> None:
    journal = tmp_path / "journal"
    records = complete_attempt_records()
    write_attempt_dir(journal)
    path = write_ledger(journal, records)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

    _assert_stale(journal)


def test_replay_rejects_blank_lines(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    path = write_ledger(journal, complete_attempt_records())
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    _assert_stale(journal)


def test_replay_rejects_duplicate_fields(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    records = complete_attempt_records()
    path = write_ledger(journal, records)
    duplicate_line = (
        path.read_text(encoding="utf-8")
        .splitlines()[0]
        .replace("{", '{"run_id":"x",', 1)
    )
    path.write_text(duplicate_line + "\n", encoding="utf-8")

    _assert_stale(journal)


def test_replay_rejects_unknown_fields(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    records = complete_attempt_records()
    records[0]["unknown"] = True
    write_ledger(journal, records)

    _assert_stale(journal)


def test_replay_rejects_retired_discriminator_field(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    records = complete_attempt_records()
    retired_key = "record_kind"
    records[0][retired_key] = records[0].pop("type")
    write_ledger(journal, records)

    _assert_stale(journal)


def test_replay_rejects_old_attempt_terminal_reason_field(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    records = complete_attempt_records()
    records[-1]["reason"] = records[-1]["terminal_reason"]
    write_ledger(journal, records)

    _assert_stale(journal)


def test_replay_rejects_old_attempt_terminal_duration_field(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    records = complete_attempt_records()
    records[-1]["duration_ms"] = 1
    write_ledger(journal, records)

    _assert_stale(journal)


def test_replay_rejects_missing_type(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    records = complete_attempt_records()
    del records[0]["type"]
    write_ledger(journal, records)

    _assert_stale(journal)


def test_replay_rejects_missing_terminal_reason(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    records = complete_attempt_records()
    del records[-1]["terminal_reason"]
    write_ledger(journal, records)

    _assert_stale(journal)


def test_replay_rejects_mixed_old_and_new_rows(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    records = complete_attempt_records()
    retired_key = "record_kind"
    records[1][retired_key] = records[1].pop("type")
    write_ledger(journal, records)

    _assert_stale(journal)


def test_replay_rejects_trailing_bytes_invalid_utf8_and_invalid_json(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal)
    path = write_ledger(journal, complete_attempt_records())
    path.write_text(path.read_text(encoding="utf-8").replace("\n", " \n", 1))
    _assert_stale(journal)

    path.write_bytes(b"\xff\n")
    _assert_stale(journal)

    path.write_text("{\n", encoding="utf-8")
    _assert_stale(journal)


def test_replay_rejects_non_object_json(tmp_path) -> None:
    journal = tmp_path / "journal"
    write_attempt_dir(journal, ATTEMPT_ID)
    path = probe_contract.probe_ledger_path(journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]\n", encoding="utf-8")

    _assert_stale(journal)

