# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Exhaustive journal config transaction side-effect path inventory for tests."""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from tests.helpers.journal_config import seed_journal_config


@dataclass
class JournalConfigEffectHarness:
    journal_path: Path
    monkeypatch: pytest.MonkeyPatch
    caplog: pytest.LogCaptureFixture
    capsys: pytest.CaptureFixture[str]
    effects: list[str] = field(default_factory=list)
    watched_env: dict[str, str | None] = field(default_factory=dict)
    before_bytes: bytes | None = None
    fail_replace: Callable[[Path, Path], None] | None = None

    @property
    def config_path(self) -> Path:
        return self.journal_path / "config" / "journal.json"

    def seed(self, config: dict[str, Any]) -> None:
        seed_journal_config(config, self.journal_path)
        self.before_bytes = self.config_path.read_bytes()
        if self.fail_replace is not None:
            self.monkeypatch.setattr(
                "solstone.think.journal_io.atomic.os.replace",
                self.fail_replace,
            )

    def watch_env(self, *names: str) -> None:
        for name in names:
            self.watched_env[name] = os.environ.get(name)

    def assert_watched_env_unchanged(self) -> None:
        for name, before in self.watched_env.items():
            assert os.environ.get(name) == before


@dataclass(frozen=True)
class JournalConfigEffectCase:
    path_id: str
    site: str
    effect_kind: tuple[str, ...]
    trigger: Callable[[JournalConfigEffectHarness], None]
    commit_failure_observable: str


def _base_config(**updates: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "setup": {"completed_at": 1700000000000},
        "identity": {"name": "Before", "preferred": "Before"},
        "journal": {"name": "Before"},
        "env": {},
        "providers": {
            "active": {"provider": "google", "model": "gemini-3.5-flash"},
            "key_validation": {},
            "local": {},
        },
        "retention": {"raw_media": "keep", "raw_media_days": None},
    }
    config.update(updates)
    return config


def _client(harness: JournalConfigEffectHarness):
    harness.monkeypatch.setenv("SOLSTONE_JOURNAL", str(harness.journal_path))
    harness.monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    harness.monkeypatch.setenv("SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES", "1")
    from solstone.convey import create_app

    app = create_app(str(harness.journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _expect_exception(call: Callable[[], Any]) -> None:
    with pytest.raises(OSError, match="forced commit failure"):
        call()


def _expect_http_failure(response: Any) -> None:
    assert response.status_code >= 500
    payload = response.get_json(silent=True)
    if isinstance(payload, dict):
        assert payload.get("success") is not True


def _patch_settings_action(harness: JournalConfigEffectHarness) -> None:
    module = importlib.import_module("solstone.apps.settings.routes")
    harness.monkeypatch.setattr(
        module,
        "log_app_action",
        lambda **_kwargs: harness.effects.append("owner_action"),
    )


def _patch_thinking_action(harness: JournalConfigEffectHarness) -> None:
    module = importlib.import_module("solstone.apps.thinking.routes")
    harness.monkeypatch.setattr(
        module,
        "log_app_action",
        lambda **_kwargs: harness.effects.append("owner_action"),
    )


def _settings_config_trigger(
    section: str,
    data: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    watch_env: tuple[str, ...] = (),
) -> Callable[[JournalConfigEffectHarness], None]:
    def trigger(harness: JournalConfigEffectHarness) -> None:
        harness.seed(config or _base_config())
        _patch_settings_action(harness)
        harness.watch_env(*watch_env)
        if "PLAUD_ACCESS_TOKEN" in data:
            plaud = importlib.import_module("solstone.think.importers.plaud")
            harness.monkeypatch.setattr(
                plaud,
                "validate_token",
                lambda token: {"valid": True, "account": token},
            )
        response = _client(harness).put(
            "/app/settings/api/config",
            json={"section": section, "data": data},
        )
        _expect_http_failure(response)

    return trigger


def _settings_route_trigger(
    path: str,
    payload: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> Callable[[JournalConfigEffectHarness], None]:
    def trigger(harness: JournalConfigEffectHarness) -> None:
        harness.seed(config or _base_config())
        _patch_settings_action(harness)
        response = _client(harness).put(path, json=payload)
        _expect_http_failure(response)

    return trigger


def _thinking_keys_trigger(
    payload: dict[str, Any],
    *,
    config: dict[str, Any],
    watch_env: tuple[str, ...],
) -> Callable[[JournalConfigEffectHarness], None]:
    def trigger(harness: JournalConfigEffectHarness) -> None:
        harness.seed(config)
        _patch_thinking_action(harness)
        harness.watch_env(*watch_env)
        module = importlib.import_module("solstone.apps.thinking.routes")
        harness.monkeypatch.setattr(
            module,
            "validate_key",
            lambda provider, key: {"valid": True, "provider": provider, "key": key},
        )
        response = _client(harness).put("/app/thinking/api/keys", json=payload)
        _expect_http_failure(response)

    return trigger


def _thinking_route_trigger(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
    extra_patch: Callable[[JournalConfigEffectHarness], None] | None = None,
) -> Callable[[JournalConfigEffectHarness], None]:
    def trigger(harness: JournalConfigEffectHarness) -> None:
        harness.seed(config or _base_config())
        _patch_thinking_action(harness)
        if extra_patch is not None:
            extra_patch(harness)
        client = _client(harness)
        response = getattr(client, method)(path, json=payload)
        _expect_http_failure(response)

    return trigger


def _tools_retention_trigger(
    *,
    stream: str | None,
    mode: str | None,
    days: int | None,
    clear: bool,
    config: dict[str, Any],
) -> Callable[[JournalConfigEffectHarness], None]:
    def trigger(harness: JournalConfigEffectHarness) -> None:
        harness.seed(config)
        module = importlib.import_module("solstone.think.tools.call")
        harness.monkeypatch.setattr(
            module,
            "log_call_action",
            lambda **_kwargs: harness.effects.append("owner_action"),
        )
        _expect_exception(
            lambda: module.config(mode=mode, days=days, stream=stream, clear=clear)
        )

    return trigger


def _import_resolve_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config(identity={"name": "Before"}))
    module = importlib.import_module("solstone.apps.import.resolve")
    state_dir = harness.journal_path / "imports" / "pending"
    diff_path = state_dir / "config" / "diff.json"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(
        json.dumps(
            {
                "identity.name": {
                    "source": "After",
                    "target": "Before",
                    "category": "preference",
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _expect_exception(
        lambda: module.resolve_config(state_dir, "identity.name", "apply")
    )
    assert diff_path.exists()
    assert not (state_dir / "config" / "log.jsonl").exists()


def _root_finalize_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config(convey={"allow_network_access": True}))
    establish = importlib.import_module("solstone.think.link.establish")
    harness.monkeypatch.setattr(establish, "is_committed", lambda: True)
    root = importlib.import_module("solstone.convey.root")
    harness.monkeypatch.setattr(
        root,
        "locked_modify_convey_config",
        lambda *_args, **_kwargs: harness.effects.append("post_commit_operation"),
    )
    harness.monkeypatch.setattr(
        root,
        "start_secure_listener",
        lambda *_args, **_kwargs: harness.effects.append("post_commit_operation"),
    )
    _expect_exception(
        lambda: _client(harness).post(
            "/init/finalize",
            json={"name": "After", "preferred": "After", "retention_mode": "keep"},
        )
    )


def _observer_maint_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config(observe={"remote": {"enabled": True}}))
    module = importlib.import_module(
        "solstone.apps.observer.maint.000_migrate_remote_to_observer"
    )
    harness.monkeypatch.setattr(sys, "argv", ["observer-maint"])
    _expect_exception(module.main)
    assert "Config updated:" not in harness.capsys.readouterr().out


def _settings_maint_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config(pairing={"host_url": "https://home.example"}))
    module = importlib.import_module(
        "solstone.apps.settings.maint.008_migrate_pairing_home_address"
    )
    harness.monkeypatch.setattr(sys, "argv", ["settings-maint"])
    _expect_exception(module.main)
    assert (
        "Migrated pairing home address config." not in harness.capsys.readouterr().out
    )


def _thinking_maint_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(
        _base_config(
            providers={
                "google_vertex": {"project_id": "p"},
                "key_validation": {"google_vertex": {"valid": True}},
            }
        )
    )
    credentials = harness.journal_path / ".config" / "vertex-credentials.json"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("{}", encoding="utf-8")
    module = importlib.import_module(
        "solstone.apps.thinking.maint.000_unify_provider_config"
    )
    harness.monkeypatch.setattr(sys, "argv", ["thinking-maint"])
    _expect_exception(module.main)
    assert credentials.exists()
    assert "Unified thinking provider config" not in harness.capsys.readouterr().out


def _spp_payload() -> dict[str, str]:
    return {
        "account_id": "acct",
        "endpoint_url": "https://local.example/v1",
        "served_model_id": "model",
        "credential": "credential",
        "created_at": "2026-07-01T00:00:00+00:00",
    }


def _spp_provision_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config())
    module = importlib.import_module("solstone.think.services.spp")
    with harness.caplog.at_level(logging.DEBUG, logger=module.log.name):
        _expect_exception(lambda: module.provision_confidential_handoff(_spp_payload()))
    assert "provisioned confidential service" not in harness.caplog.text


def _spp_disable_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(
        _base_config(
            providers={
                "active": {"provider": "local", "model": "gemini-3.5-flash"},
                "local": {
                    "endpoint_url": "https://local.example/v1",
                    "served_model_id": "model",
                    "credential": "credential",
                },
            },
            services={
                "confidential": {
                    "credential_fingerprint_sha256": (
                        "b001dffcc72b09c258cfdabe7ab055042d987ecc2f14b566a8a"
                        "57f5d546b8e2b"
                    ),
                    "prior_active": {"provider": "google", "model": "gemini"},
                    "prior_local_endpoint": {},
                }
            },
        )
    )
    module = importlib.import_module("solstone.think.services.spp")
    harness.monkeypatch.setattr(
        module,
        "delete_attestation_state",
        lambda: harness.effects.append("post_commit_operation"),
    )
    with harness.caplog.at_level(logging.DEBUG, logger=module.log.name):
        _expect_exception(module.disable_confidential)
    assert "disabled confidential service" not in harness.caplog.text


def _spl_enable_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config(link={"posture": "direct"}))
    module = importlib.import_module("solstone.think.services.spl")
    harness.monkeypatch.setattr(
        module,
        "LinkState",
        SimpleNamespace(
            load_or_create=lambda: SimpleNamespace(instance_id="i", home_label="h")
        ),
    )
    harness.monkeypatch.setattr(
        module,
        "load_or_generate_ca",
        lambda *_args, **_kwargs: SimpleNamespace(pubkey_spki_pem="pem"),
    )
    harness.monkeypatch.setattr(
        module, "enroll_home", lambda *_args, **_kwargs: "token"
    )
    harness.monkeypatch.setattr(
        module,
        "save_service_token",
        lambda _token: harness.effects.append("post_commit_operation"),
    )
    with harness.caplog.at_level(logging.DEBUG, logger=module.log.name):
        _expect_exception(module.enable_spl)
    assert "enabled sol private link" not in harness.caplog.text


def _spl_disable_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config(link={"posture": "spl"}))
    module = importlib.import_module("solstone.think.services.spl")
    with harness.caplog.at_level(logging.DEBUG, logger=module.log.name):
        _expect_exception(module.disable_spl)
    assert "disabled sol private link" not in harness.caplog.text


def _scout_payload() -> dict[str, str]:
    return {
        "google_api_key": "google-key",
        "dispatch_token": "dispatch",
        "account_id": "acct",
        "created_at": "2026-07-01T00:00:00+00:00",
    }


def _scout_provision_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config())
    module = importlib.import_module("solstone.think.services.scout")
    with harness.caplog.at_level(logging.DEBUG, logger=module.log.name):
        _expect_exception(lambda: module.provision_scout_handoff(_scout_payload()))
    assert "provisioned scout service" not in harness.caplog.text


def _scout_pending_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(_base_config())
    module = importlib.import_module("solstone.think.services.scout")
    with harness.caplog.at_level(logging.DEBUG, logger=module.log.name):
        _expect_exception(
            lambda: module.record_scout_pending("acct", "now", "dispatch")
        )
    assert "recorded pending scout marker" not in harness.caplog.text


def _scout_disable_trigger(harness: JournalConfigEffectHarness) -> None:
    harness.seed(
        _base_config(
            env={"GOOGLE_API_KEY": "google-key"},
            services={
                "scout": {
                    "account_id": "acct",
                    "key_fingerprint_sha256": (
                        "fc88e0ac64d2f3363d7281c688618a380635ca7453c1d460e54dcf15637e3d77"
                    ),
                }
            },
        )
    )
    module = importlib.import_module("solstone.think.services.scout")
    with harness.caplog.at_level(logging.DEBUG, logger=module.log.name):
        _expect_exception(module.disable_scout)
    assert "disabled scout service" not in harness.caplog.text


def _patch_local_endpoint(harness: JournalConfigEffectHarness) -> None:
    module = importlib.import_module("solstone.apps.thinking.routes")
    harness.monkeypatch.setattr(
        module,
        "resolve_local_endpoint",
        lambda: SimpleNamespace(
            is_bundled=False, base_url="https://old/v1", served_model_id="old"
        ),
    )


JOURNAL_CONFIG_TRANSACTION_EFFECTS: tuple[JournalConfigEffectCase, ...] = (
    JournalConfigEffectCase(
        "settings.update_config.identity",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        _settings_config_trigger("identity", {"name": "After"}),
        "No settings identity_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.journal",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        _settings_config_trigger("journal", {"name": "After"}),
        "No settings journal_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.transcribe",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        _settings_config_trigger("transcribe", {"backend": "parakeet-cpp"}),
        "No settings transcribe_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.support",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        _settings_config_trigger("support", {"enabled": True}),
        "No settings support_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.agent",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        _settings_config_trigger("agent", {"name": "helper"}),
        "No settings agent_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.processing",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        _settings_config_trigger(
            "processing",
            {
                "mode": "deferred",
                "gate": {"time_window": {"start": "01:00", "end": "05:00"}},
            },
        ),
        "No settings processing_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.env.set",
        "solstone/apps/settings/routes.py:update_config",
        ("process_env", "owner_action"),
        _settings_config_trigger(
            "env",
            {"PLAUD_ACCESS_TOKEN": "plaud-token"},
            watch_env=("PLAUD_ACCESS_TOKEN",),
        ),
        "PLAUD_ACCESS_TOKEN unchanged; no settings env_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.env.clear",
        "solstone/apps/settings/routes.py:update_config",
        ("process_env", "owner_action"),
        _settings_config_trigger(
            "env",
            {"PLAUD_ACCESS_TOKEN": ""},
            config=_base_config(env={"PLAUD_ACCESS_TOKEN": "plaud-token"}),
            watch_env=("PLAUD_ACCESS_TOKEN",),
        ),
        "PLAUD_ACCESS_TOKEN unchanged; no settings env_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_vision.changed",
        "solstone/apps/settings/routes.py:update_vision",
        ("owner_action",),
        _settings_route_trigger("/app/settings/api/vision", {"max_extractions": 42}),
        "No settings vision_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_observe.changed",
        "solstone/apps/settings/routes.py:update_observe",
        ("owner_action",),
        _settings_route_trigger(
            "/app/settings/api/observe",
            {"tmux": {"enabled": False}},
            config=_base_config(observe={"tmux": {"enabled": True}}),
        ),
        "No settings observe_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_storage.changed",
        "solstone/apps/settings/routes.py:update_storage",
        ("owner_action",),
        _settings_route_trigger(
            "/app/settings/api/storage",
            {"raw_media": "days", "raw_media_days": 14},
        ),
        "No settings retention_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.keys.set",
        "solstone/apps/thinking/routes.py:update_key",
        ("process_env", "owner_action"),
        _thinking_keys_trigger(
            {"env_var": "GOOGLE_API_KEY", "value": "google-key"},
            config=_base_config(env={}),
            watch_env=("GOOGLE_API_KEY",),
        ),
        "Provider env var unchanged; no thinking env_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.keys.clear",
        "solstone/apps/thinking/routes.py:update_key",
        ("process_env", "owner_action"),
        _thinking_keys_trigger(
            {"env_var": "GOOGLE_API_KEY", "value": ""},
            config=_base_config(env={"GOOGLE_API_KEY": "google-key"}),
            watch_env=("GOOGLE_API_KEY",),
        ),
        "Provider env var unchanged; no thinking env_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.update_local_endpoint.changed",
        "solstone/apps/thinking/routes.py:update_local_endpoint",
        ("owner_action",),
        _thinking_route_trigger(
            "post",
            "/app/thinking/api/local/endpoint",
            {
                "endpoint_url": "https://local.example/v1",
                "served_model_id": "model",
                "credential": "secret",
            },
        ),
        "No thinking local_endpoint_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.clear_local_endpoint.changed",
        "solstone/apps/thinking/routes.py:clear_local_endpoint",
        ("owner_action",),
        _thinking_route_trigger(
            "delete",
            "/app/thinking/api/local/endpoint",
            config=_base_config(
                providers={
                    "active": {"provider": "local", "model": "gemini-3.5-flash"},
                    "local": {
                        "endpoint_url": "https://local.example/v1",
                        "served_model_id": "model",
                        "credential": "secret",
                    },
                }
            ),
            extra_patch=_patch_local_endpoint,
        ),
        "No thinking local_endpoint_clear action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.update_providers.changed",
        "solstone/apps/thinking/routes.py:update_providers",
        ("owner_action",),
        _thinking_route_trigger(
            "put",
            "/app/thinking/api/providers",
            {"lane": "byo", "provider": "openai", "model": "gpt-5"},
        ),
        "No thinking providers_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.update_generators.changed",
        "solstone/apps/thinking/routes.py:update_generators",
        ("owner_action",),
        _thinking_route_trigger(
            "put",
            "/app/thinking/api/generators",
            {"summary": {"disabled": True}},
        ),
        "No thinking generators_update action log entry.",
    ),
    JournalConfigEffectCase(
        "tools.retention_config.clear",
        "solstone/think/tools/call.py:retention_config",
        ("owner_action",),
        _tools_retention_trigger(
            stream="desktop",
            mode=None,
            days=None,
            clear=True,
            config=_base_config(
                retention={
                    "per_stream": {
                        "desktop": {"raw_media": "days", "raw_media_days": 7}
                    }
                }
            ),
        ),
        "No call retention_config action log entry for clear.",
    ),
    JournalConfigEffectCase(
        "tools.retention_config.stream",
        "solstone/think/tools/call.py:retention_config",
        ("owner_action",),
        _tools_retention_trigger(
            stream="desktop",
            mode="days",
            days=7,
            clear=False,
            config=_base_config(),
        ),
        "No call retention_config action log entry for stream override.",
    ),
    JournalConfigEffectCase(
        "tools.retention_config.default",
        "solstone/think/tools/call.py:retention_config",
        ("owner_action",),
        _tools_retention_trigger(
            stream=None,
            mode="processed",
            days=None,
            clear=False,
            config=_base_config(),
        ),
        "No call retention_config action log entry for default retention.",
    ),
    JournalConfigEffectCase(
        "import.resolve_config.apply",
        "solstone/apps/import/resolve.py:apply_config_field",
        ("resolution_log", "post_commit_operation"),
        _import_resolve_trigger,
        "Diff files unchanged; no config_field_applied resolution log row.",
    ),
    JournalConfigEffectCase(
        "root.init_finalize",
        "solstone/convey/root.py:init_finalize",
        ("post_commit_operation",),
        _root_finalize_trigger,
        "No app-navigation seed; no secure listener start; no success response.",
    ),
    JournalConfigEffectCase(
        "observer_maint.remote_to_observer.changed",
        "solstone/apps/observer/maint/000_migrate_remote_to_observer.py:main",
        ("success_print",),
        _observer_maint_trigger,
        'No final "Config updated: yes" success summary.',
    ),
    JournalConfigEffectCase(
        "settings_maint.pairing_home_address.changed",
        "solstone/apps/settings/maint/008_migrate_pairing_home_address.py:run",
        ("success_print",),
        _settings_maint_trigger,
        'No "Migrated pairing home address config." print.',
    ),
    JournalConfigEffectCase(
        "thinking_maint.unify_provider_config.changed",
        "solstone/apps/thinking/maint/000_unify_provider_config.py:main",
        ("success_print",),
        _thinking_maint_trigger,
        'No "Unified thinking provider config..." print.',
    ),
    JournalConfigEffectCase(
        "spp.provision_confidential",
        "solstone/think/services/spp.py:provision_confidential_handoff",
        ("success_log",),
        _spp_provision_trigger,
        'No "provisioned confidential service" debug log.',
    ),
    JournalConfigEffectCase(
        "spp.disable_confidential.enabled",
        "solstone/think/services/spp.py:disable_confidential",
        ("success_log", "post_commit_operation"),
        _spp_disable_trigger,
        'No "disabled confidential service" debug log; attestation state not cleared.',
    ),
    JournalConfigEffectCase(
        "spl.enable",
        "solstone/think/services/spl.py:enable_spl",
        ("pre_commit_side_effect", "success_log"),
        _spl_enable_trigger,
        'No service token write before commit; no "enabled sol private link" log.',
    ),
    JournalConfigEffectCase(
        "spl.disable.enabled",
        "solstone/think/services/spl.py:disable_spl",
        ("success_log",),
        _spl_disable_trigger,
        'No "disabled sol private link" debug log.',
    ),
    JournalConfigEffectCase(
        "scout.provision",
        "solstone/think/services/scout.py:provision_scout_handoff",
        ("success_log",),
        _scout_provision_trigger,
        'No "provisioned scout service..." debug log.',
    ),
    JournalConfigEffectCase(
        "scout.record_pending",
        "solstone/think/services/scout.py:record_scout_pending",
        ("success_log",),
        _scout_pending_trigger,
        'No "recorded pending scout marker..." debug log.',
    ),
    JournalConfigEffectCase(
        "scout.disable.enabled",
        "solstone/think/services/scout.py:disable_scout",
        ("success_log",),
        _scout_disable_trigger,
        'No "disabled scout service" debug log.',
    ),
)

assert len(JOURNAL_CONFIG_TRANSACTION_EFFECTS) == 32
