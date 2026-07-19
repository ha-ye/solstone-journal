# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest
import typer

from solstone.convey import create_app
from solstone.convey.reasons import CONFIG_BUSY
from solstone.think import journal_config
from solstone.think.journal_config import (
    JournalConfigMutation,
    ensure_journal_config,
    get_journal_config_path,
    mutate_journal_config,
    read_journal_config,
)
from solstone.think.journal_io import LockTimeout
from solstone.think.journal_io.locking import hold_lock
from solstone.think.utils import CorruptConfigError
from tests.helpers.journal_config import seed_journal_config
from tests.journal_config_transaction_effects import (
    JOURNAL_CONFIG_TRANSACTION_EFFECTS,
    JournalConfigEffectHarness,
)


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _replace_config(config: dict) -> None:
    def apply(draft: dict) -> JournalConfigMutation[None]:
        draft.clear()
        draft.update(config)
        return JournalConfigMutation(changed=True, value=None)

    mutate_journal_config(apply)


def test_mutate_journal_config_crash_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = _config_path(tmp_path)
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"identity": {"name": "Old"}}\n', encoding="utf-8")
    old = config_path.read_bytes()

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", boom)

    with pytest.raises(OSError):
        _replace_config({"identity": {"name": "New"}})

    assert config_path.read_bytes() == old
    assert list(config_path.parent.glob(".tmp_*")) == []


def test_mutate_journal_config_fsyncs_temp_and_parent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    calls = []
    real_fsync = os.fsync

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.fsync", spy)

    _replace_config({"identity": {"name": "Durable"}})

    assert len(calls) >= 2


def test_mutate_journal_config_file_mode_is_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    _replace_config({"identity": {"name": "Private"}})

    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_mutate_journal_config_applies_mode_before_replace(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    captured = {}
    real_replace = os.replace

    def spy(src, dst):
        captured["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        return real_replace(src, dst)

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", spy)

    _replace_config({"identity": {"name": "Private"}})

    assert captured["mode"] == 0o600
    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_mutate_journal_config_serializes_utf8_without_ascii_escapes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config = {"identity": {"name": "Renée"}}

    _replace_config(config)

    actual = _config_path(tmp_path).read_bytes()
    expected = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    assert actual == expected
    assert "Renée".encode("utf-8") in actual
    assert b"Ren\\u00e9e" not in actual


def test_falsey_mutator_value_still_persists_changed_config(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    seed_journal_config({"identity": {"name": "Before"}}, tmp_path)

    def apply(draft: dict) -> JournalConfigMutation[str]:
        draft["identity"]["name"] = ""
        return JournalConfigMutation(changed=True, value="")

    result = mutate_journal_config(apply)

    assert result.value == ""
    assert result.changed is True
    assert result.written is True
    assert read_journal_config(tmp_path)["identity"]["name"] == ""


def test_truthy_mutator_value_does_not_force_unchanged_write(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = seed_journal_config({"identity": {"name": "Stable"}}, tmp_path)
    before = config_path.read_bytes()
    before_mtime = config_path.stat().st_mtime_ns

    result = mutate_journal_config(
        lambda draft: JournalConfigMutation(changed=False, value={"ok": True})
    )

    assert result.value == {"ok": True}
    assert result.changed is False
    assert result.written is False
    assert config_path.read_bytes() == before
    assert config_path.stat().st_mtime_ns == before_mtime


def test_mutator_raise_leaves_prior_doc_valid_and_lock_released(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = seed_journal_config({"identity": {"name": "Before"}}, tmp_path)
    before = config_path.read_bytes()

    def fail(draft: dict) -> JournalConfigMutation[None]:
        draft["identity"]["name"] = "Partial"
        raise RuntimeError("mutator failed")

    with pytest.raises(RuntimeError, match="mutator failed"):
        mutate_journal_config(fail)

    assert config_path.read_bytes() == before
    assert list(config_path.parent.glob(".tmp_*")) == []

    mutate_journal_config(
        lambda draft: (
            draft["identity"].update({"name": "After"})
            or JournalConfigMutation(changed=True, value=None)
        )
    )
    assert read_journal_config(tmp_path)["identity"]["name"] == "After"


def test_missing_file_materialization_does_not_clobber_racing_initializer(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = get_journal_config_path(tmp_path)
    observed: dict[str, object] = {}

    def worker() -> None:
        try:
            observed["config"] = ensure_journal_config(tmp_path)
        except BaseException as exc:  # pragma: no cover - surfaced below
            observed["error"] = exc

    with hold_lock(config_path, timeout=1, mode=0o600):
        thread = Thread(target=worker)
        thread.start()
        seed_journal_config({"identity": {"name": "Racing Init"}}, tmp_path)

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert "error" not in observed
    assert observed["config"] == {"identity": {"name": "Racing Init"}}
    assert read_journal_config(tmp_path) == {"identity": {"name": "Racing Init"}}


def test_malformed_existing_doc_raises_and_is_not_replaced(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = _config_path(tmp_path)
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b"{ invalid json }")
    before = config_path.read_bytes()

    with pytest.raises(CorruptConfigError):
        mutate_journal_config(
            lambda draft: JournalConfigMutation(changed=True, value=None)
        )

    assert config_path.read_bytes() == before


def test_lock_timeout_runs_no_mutator_and_commits_nothing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    seed_journal_config({"identity": {"name": "Before"}}, tmp_path)
    timeout = LockTimeout(path=Path("busy.lock"), timeout=0.01)
    calls: list[str] = []

    @contextmanager
    def busy_lock(_journal_path=None):
        raise timeout
        yield

    monkeypatch.setattr(journal_config, "_hold_config_lock", busy_lock)

    def apply(draft: dict) -> JournalConfigMutation[None]:
        calls.append("mutator")
        draft["identity"]["name"] = "After"
        return JournalConfigMutation(changed=True, value=None)

    with pytest.raises(LockTimeout) as exc_info:
        mutate_journal_config(apply)

    assert exc_info.value is timeout
    assert calls == []
    assert read_journal_config(tmp_path)["identity"]["name"] == "Before"


def test_settings_lock_timeout_returns_503_config_busy_without_effects(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    seed_journal_config(
        {
            "setup": {"completed_at": 1},
            "journal": {"name": "Before"},
            "env": {},
        },
        tmp_path,
    )
    import solstone.apps.settings.routes as settings_routes

    logs: list[object] = []
    timeout = LockTimeout(path=Path("busy.lock"), timeout=0.01)
    monkeypatch.setattr(
        settings_routes,
        "mutate_journal_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(timeout),
    )
    monkeypatch.setattr(settings_routes, "log_app_action", logs.append)

    app = create_app(str(tmp_path))
    app.config["TESTING"] = True
    response = app.test_client().put(
        "/app/settings/api/config",
        json={"section": "journal", "data": {"name": "After"}},
    )

    assert response.status_code == 503
    assert response.get_json()["reason_code"] == CONFIG_BUSY.code
    assert logs == []
    assert read_journal_config(tmp_path)["journal"]["name"] == "Before"


def test_tools_call_lock_timeout_exits_nonzero_with_retry_message(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    seed_journal_config({"retention": {"raw_media": "keep"}}, tmp_path)
    import solstone.think.tools.call as call_module

    timeout = LockTimeout(path=Path("busy.lock"), timeout=0.01)
    monkeypatch.setattr(
        call_module,
        "mutate_journal_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(timeout),
    )

    with pytest.raises(typer.Exit) as exc_info:
        call_module.config(mode="days", days=3, stream=None, clear=False)

    assert exc_info.value.exit_code == 1
    assert "Journal config is busy; try again." in capsys.readouterr().err
    assert read_journal_config(tmp_path)["retention"]["raw_media"] == "keep"


@pytest.mark.parametrize(
    "case",
    JOURNAL_CONFIG_TRANSACTION_EFFECTS,
    ids=lambda case: case.path_id,
)
def test_effect_inventory_entries_are_commit_failure_observables(case) -> None:
    assert case.path_id
    assert case.site.startswith("solstone/")
    assert case.effect_kind
    assert callable(case.trigger)
    assert case.commit_failure_observable


@pytest.mark.parametrize(
    "case",
    JOURNAL_CONFIG_TRANSACTION_EFFECTS,
    ids=lambda case: case.path_id,
)
def test_effect_inventory_forced_commit_failure_runs_no_secondary_effect(
    case, tmp_path, monkeypatch, caplog, capsys
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_replace = os.replace

    def fail_replace(_src, _dst) -> None:
        raise OSError(f"forced commit failure for {case.path_id}")

    harness = JournalConfigEffectHarness(
        journal_path=tmp_path,
        monkeypatch=monkeypatch,
        caplog=caplog,
        capsys=capsys,
        fail_replace=fail_replace,
    )

    case.trigger(harness)

    assert harness.before_bytes is not None
    assert harness.config_path.read_bytes() == harness.before_bytes
    harness.assert_watched_env_unchanged()
    assert harness.effects == []

    monkeypatch.setattr(
        "solstone.think.journal_io.atomic.os.replace",
        original_replace,
    )
    mutate_journal_config(
        lambda draft: (
            (draft.setdefault("effect_probe", {}) or draft["effect_probe"]).update(
                {case.path_id: True}
            )
            or JournalConfigMutation(changed=True, value=None)
        )
    )
    assert read_journal_config(tmp_path)["effect_probe"][case.path_id] is True
