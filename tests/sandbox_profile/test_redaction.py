# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys

from tests.sandbox_profile import (
    invoke,
    output_json,
    prepare_ok,
    sandbox_journal,
    scout_payload,
)


def test_payload_secret_absent_from_argv_output_stderr_logs_and_exception(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    secret = "recognizable-secret-google-key"
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)

    def fail_with_secret(_payload):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        "solstone.think.services.scout.provision_scout_handoff",
        fail_with_secret,
    )
    result = invoke(
        ["apply", "scout", "--json"], input_text=json.dumps(scout_payload(secret))
    )
    body = output_json(result)

    assert result.exit_code == 2
    assert body["error"]["code"] == "internal_error"
    assert secret not in result.output
    assert secret not in caplog.text
    assert secret not in " ".join(sys.argv)
    assert result.exception is None or secret not in str(result.exception)


def test_human_output_is_redacted_text_not_a_second_json_object(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)

    result = invoke(["status", "--human"])

    assert result.exit_code == 0
    assert result.output.startswith("action: status\n")
    assert not result.output.lstrip().startswith("{")
