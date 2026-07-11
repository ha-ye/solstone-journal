# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import logging
import stat
import threading
from pathlib import Path

import pytest

from solstone.think.journal_config import write_journal_config
from solstone.think.services import spp as spp_module
from solstone.think.services.spp import (
    CREDENTIAL_FINGERPRINT_FIELD,
    DisableOutcome,
    JournalNotInitializedError,
    confidential_provenance,
    disable_confidential,
    is_confidential_enabled,
    provision_confidential_handoff,
)


def _payload(suffix: str = "one") -> dict[str, str]:
    return {
        "endpoint_url": f"https://spp-{suffix}.example.test/v1",
        "served_model_id": f"confidential-model-{suffix}",
        "credential": f"credential-{suffix}",
        "account_id": f"acct-{suffix}",
        "created_at": f"2026-05-24T00:00:0{len(suffix)}Z",
    }


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _read_config(journal: Path) -> dict:
    return json.loads(_config_path(journal).read_text("utf-8"))


def _write_config(journal: Path, config: dict) -> None:
    write_journal_config(config, journal)


def test_provision_confidential_handoff_round_trip_writes_single_state(
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["generate"]["model"] = "keep-generate-model"
    config["providers"]["cogitate"]["model"] = "keep-cogitate-model"
    config["providers"]["local"] = {
        "endpoint_url": "http://prior.test/v1",
        "served_model_id": "prior-model",
        "credential": "prior-credential",
    }
    _write_config(journal_copy, config)

    provision_confidential_handoff(_payload())

    saved = _read_config(journal_copy)
    assert saved["providers"]["local"] == {
        "endpoint_url": "https://spp-one.example.test",
        "served_model_id": "confidential-model-one",
        "credential": "credential-one",
    }
    assert saved["providers"]["generate"] == {
        "provider": "local",
        "tier": 2,
        "backup": "anthropic",
        "model": "keep-generate-model",
    }
    assert saved["providers"]["cogitate"] == {
        "provider": "local",
        "tier": 2,
        "backup": "anthropic",
        "model": "keep-cogitate-model",
    }
    block = saved["services"]["confidential"]
    assert set(block) == {
        "enabled_at",
        "account_id",
        "endpoint_url",
        "served_model_id",
        "credential_created_at",
        CREDENTIAL_FINGERPRINT_FIELD,
        "prior_generate_provider",
        "prior_cogitate_provider",
        "prior_local_endpoint",
    }
    assert block["account_id"] == "acct-one"
    assert block["endpoint_url"] == "https://spp-one.example.test"
    assert block["served_model_id"] == "confidential-model-one"
    assert block["credential_created_at"] == _payload()["created_at"]
    assert (
        block[CREDENTIAL_FINGERPRINT_FIELD]
        == hashlib.sha256(b"credential-one").hexdigest()
    )
    assert block["prior_generate_provider"] == "google"
    assert block["prior_cogitate_provider"] == "openai"
    assert block["prior_local_endpoint"] == {
        "endpoint_url": "http://prior.test/v1",
        "served_model_id": "prior-model",
        "credential": "prior-credential",
    }
    assert confidential_provenance() == block
    assert is_confidential_enabled() is True
    assert stat.S_IMODE(_config_path(journal_copy).stat().st_mode) == 0o600


def test_redact_handoff_redacts_credential_keeps_nonsecrets() -> None:
    payload = _payload()

    redacted = spp_module._redact_handoff(payload)

    assert redacted["credential"] == "***redacted***"
    assert redacted["endpoint_url"] == payload["endpoint_url"]
    assert redacted["served_model_id"] == payload["served_model_id"]
    assert redacted["account_id"] == payload["account_id"]
    assert "credential-one" not in redacted.values()


def test_provision_confidential_handoff_debug_log_redacts_payload(
    journal_copy: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    provision_confidential_handoff(_payload())

    assert "received confidential handoff payload" in caplog.text
    assert "***redacted***" in caplog.text
    assert "credential-one" not in caplog.text


@pytest.mark.parametrize("field", list(_payload().keys()))
def test_payload_validation_missing_field(journal_copy: Path, field: str) -> None:
    payload = _payload()
    payload.pop(field)

    with pytest.raises(
        ValueError, match=f"malformed handoff payload: missing field '{field}'"
    ):
        provision_confidential_handoff(payload)

    assert "confidential" not in _read_config(journal_copy).get("services", {})


@pytest.mark.parametrize("field", list(_payload().keys()))
@pytest.mark.parametrize("value", [None, 123, ""])
def test_payload_validation_non_empty_string(
    journal_copy: Path,
    field: str,
    value: object,
) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(
        ValueError,
        match=f"malformed handoff payload: field '{field}' must be a non-empty string",
    ):
        provision_confidential_handoff(payload)


@pytest.mark.parametrize("bad_url", ["spp.example.test", "ftp://spp.example.test"])
def test_payload_validation_rejects_non_http_endpoint(
    journal_copy: Path,
    bad_url: str,
) -> None:
    payload = _payload()
    payload["endpoint_url"] = bad_url

    with pytest.raises(ValueError, match="endpoint_url must be http"):
        provision_confidential_handoff(payload)

    assert "confidential" not in _read_config(journal_copy).get("services", {})


def test_disable_confidential_restores_prior_state_on_fingerprint_match(
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["local"] = {
        "endpoint_url": "http://prior.test",
        "served_model_id": "prior-model",
        "credential": "prior-credential",
    }
    _write_config(journal_copy, config)
    provision_confidential_handoff(_payload("disable"))

    outcome = disable_confidential()

    assert outcome == DisableOutcome(was_enabled=True, credential_preserved=False)
    saved = _read_config(journal_copy)
    assert saved["providers"]["generate"]["provider"] == "google"
    assert saved["providers"]["cogitate"]["provider"] == "openai"
    assert saved["providers"]["local"] == {
        "endpoint_url": "http://prior.test",
        "served_model_id": "prior-model",
        "credential": "prior-credential",
    }
    assert "confidential" not in saved["services"]
    assert is_confidential_enabled() is False


def test_disable_confidential_clears_local_block_when_no_prior_endpoint(
    journal_copy: Path,
) -> None:
    provision_confidential_handoff(_payload("clear"))

    outcome = disable_confidential()

    assert outcome == DisableOutcome(was_enabled=True, credential_preserved=False)
    saved = _read_config(journal_copy)
    assert saved["providers"]["local"] == {}
    assert "confidential" not in saved["services"]


def test_disable_confidential_preserves_local_endpoint_when_fingerprint_mismatches(
    journal_copy: Path,
) -> None:
    provision_confidential_handoff(_payload("manual"))
    config = _read_config(journal_copy)
    config["providers"]["local"] = {
        "endpoint_url": "https://manual.example.test",
        "served_model_id": "manual-model",
        "credential": "manual-credential",
    }
    _write_config(journal_copy, config)

    outcome = disable_confidential()

    assert outcome == DisableOutcome(was_enabled=True, credential_preserved=True)
    saved = _read_config(journal_copy)
    assert saved["providers"]["local"] == {
        "endpoint_url": "https://manual.example.test",
        "served_model_id": "manual-model",
        "credential": "manual-credential",
    }
    assert "confidential" not in saved["services"]


@pytest.mark.parametrize("credential_state", ["missing", "none"])
def test_disable_confidential_preserves_local_endpoint_when_current_credential_missing(
    journal_copy: Path,
    credential_state: str,
) -> None:
    provision_confidential_handoff(_payload("removed"))
    config = _read_config(journal_copy)
    local = dict(config["providers"]["local"])
    if credential_state == "missing":
        local.pop("credential", None)
    else:
        local["credential"] = None
    config["providers"]["local"] = local
    _write_config(journal_copy, config)

    outcome = disable_confidential()

    assert outcome == DisableOutcome(was_enabled=True, credential_preserved=True)
    saved = _read_config(journal_copy)
    assert saved["providers"]["generate"]["provider"] == "google"
    assert saved["providers"]["cogitate"]["provider"] == "openai"
    assert saved["providers"]["local"] == local
    assert "confidential" not in saved["services"]


def test_disable_confidential_when_not_enabled_returns_was_enabled_false(
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    config.setdefault("services", {}).pop("confidential", None)
    _write_config(journal_copy, config)

    outcome = disable_confidential()

    assert outcome == DisableOutcome(was_enabled=False, credential_preserved=False)


def test_reenable_after_disable_lands_cleanly(journal_copy: Path) -> None:
    provision_confidential_handoff(_payload("first"))
    disable_confidential()

    provision_confidential_handoff(_payload("second"))

    saved = _read_config(journal_copy)
    assert (
        saved["providers"]["local"]["endpoint_url"] == "https://spp-second.example.test"
    )
    assert saved["services"]["confidential"]["prior_generate_provider"] == "google"
    assert saved["services"]["confidential"]["prior_cogitate_provider"] == "openai"


def test_locked_parallel_writes_do_not_corrupt_config(journal_copy: Path) -> None:
    errors: list[BaseException] = []

    def write_payload(suffix: str) -> None:
        try:
            provision_confidential_handoff(_payload(suffix))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=write_payload, args=("alpha",)),
        threading.Thread(target=write_payload, args=("bravo",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    saved = _read_config(journal_copy)
    endpoint_url = saved["providers"]["local"]["endpoint_url"]
    assert endpoint_url in {
        "https://spp-alpha.example.test",
        "https://spp-bravo.example.test",
    }
    suffix = endpoint_url.removeprefix("https://spp-").removesuffix(".example.test")
    assert saved["providers"]["local"]["credential"] == f"credential-{suffix}"
    assert saved["services"]["confidential"]["account_id"] == f"acct-{suffix}"


def test_atomic_write_leaves_existing_config_on_replace_failure(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _config_path(journal_copy).read_bytes()

    def fail_replace(_tmp: Path, _path: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        provision_confidential_handoff(_payload("fail"))

    assert _config_path(journal_copy).read_bytes() == original


def test_provision_requires_initialized_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    with pytest.raises(JournalNotInitializedError):
        provision_confidential_handoff(_payload())
