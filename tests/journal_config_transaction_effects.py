# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Exhaustive journal config transaction side-effect path inventory for tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JournalConfigEffectCase:
    path_id: str
    site: str
    effect_kind: tuple[str, ...]
    trigger: str
    commit_failure_observable: str


JOURNAL_CONFIG_TRANSACTION_EFFECTS: tuple[JournalConfigEffectCase, ...] = (
    JournalConfigEffectCase(
        "settings.update_config.identity",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        "identity section changed",
        "No settings identity_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.journal",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        "journal section changed",
        "No settings journal_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.transcribe",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        "transcribe section changed",
        "No settings transcribe_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.support",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        "support section changed",
        "No settings support_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.agent",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        "agent section changed",
        "No settings agent_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.processing",
        "solstone/apps/settings/routes.py:update_config",
        ("owner_action",),
        "processing section changed",
        "No settings processing_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.env.set",
        "solstone/apps/settings/routes.py:update_config",
        ("process_env", "owner_action"),
        "set PLAUD_ACCESS_TOKEN",
        "PLAUD_ACCESS_TOKEN unchanged; no settings env_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_config.env.clear",
        "solstone/apps/settings/routes.py:update_config",
        ("process_env", "owner_action"),
        "clear PLAUD_ACCESS_TOKEN",
        "PLAUD_ACCESS_TOKEN unchanged; no settings env_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_vision.changed",
        "solstone/apps/settings/routes.py:update_vision",
        ("owner_action",),
        "vision settings changed",
        "No settings vision_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_observe.changed",
        "solstone/apps/settings/routes.py:update_observe",
        ("owner_action",),
        "observe settings changed",
        "No settings observe_update action log entry.",
    ),
    JournalConfigEffectCase(
        "settings.update_storage.changed",
        "solstone/apps/settings/routes.py:update_storage",
        ("owner_action",),
        "retention settings changed",
        "No settings retention_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.keys.set",
        "solstone/apps/thinking/routes.py:update_key",
        ("process_env", "owner_action"),
        "set provider env var",
        "Provider env var unchanged; no thinking env_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.keys.clear",
        "solstone/apps/thinking/routes.py:update_key",
        ("process_env", "owner_action"),
        "clear provider env var",
        "Provider env var unchanged; no thinking env_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.update_local_endpoint.changed",
        "solstone/apps/thinking/routes.py:update_local_endpoint",
        ("owner_action",),
        "local endpoint changed",
        "No thinking local_endpoint_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.clear_local_endpoint.changed",
        "solstone/apps/thinking/routes.py:clear_local_endpoint",
        ("owner_action",),
        "local endpoint cleared",
        "No thinking local_endpoint_clear action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.update_providers.changed",
        "solstone/apps/thinking/routes.py:update_providers",
        ("owner_action",),
        "provider selection changed",
        "No thinking providers_update action log entry.",
    ),
    JournalConfigEffectCase(
        "thinking.update_generators.changed",
        "solstone/apps/thinking/routes.py:update_generators",
        ("owner_action",),
        "generator settings changed",
        "No thinking generators_update action log entry.",
    ),
    JournalConfigEffectCase(
        "tools.retention_config.clear",
        "solstone/think/tools/call.py:retention_config",
        ("owner_action",),
        "clear per-stream retention",
        "No call retention_config action log entry for clear.",
    ),
    JournalConfigEffectCase(
        "tools.retention_config.stream",
        "solstone/think/tools/call.py:retention_config",
        ("owner_action",),
        "set per-stream retention",
        "No call retention_config action log entry for stream override.",
    ),
    JournalConfigEffectCase(
        "tools.retention_config.default",
        "solstone/think/tools/call.py:retention_config",
        ("owner_action",),
        "set default retention",
        "No call retention_config action log entry for default retention.",
    ),
    JournalConfigEffectCase(
        "import.resolve_config.apply",
        "solstone/apps/import/resolve.py:apply_config_field",
        ("resolution_log", "post_commit_operation"),
        "apply config-field resolution",
        "Diff files unchanged; no config_field_applied resolution log row.",
    ),
    JournalConfigEffectCase(
        "root.init_finalize",
        "solstone/convey/root.py:init_finalize",
        ("post_commit_operation",),
        "finalize onboarding",
        "No app-navigation seed; no secure listener start; no success response.",
    ),
    JournalConfigEffectCase(
        "observer_maint.remote_to_observer.changed",
        "solstone/apps/observer/maint/000_migrate_remote_to_observer.py:main",
        ("success_print",),
        "migration changes config",
        'No final "Config updated: yes" success summary.',
    ),
    JournalConfigEffectCase(
        "settings_maint.pairing_home_address.changed",
        "solstone/apps/settings/maint/008_migrate_pairing_home_address.py:run",
        ("success_print",),
        "migration changes config",
        'No "Migrated pairing home address config." print.',
    ),
    JournalConfigEffectCase(
        "thinking_maint.unify_provider_config.changed",
        "solstone/apps/thinking/maint/000_unify_provider_config.py:main",
        ("success_print",),
        "migration changes config",
        'No "Unified thinking provider config..." print.',
    ),
    JournalConfigEffectCase(
        "spp.provision_confidential",
        "solstone/think/services/spp.py:provision_confidential_handoff",
        ("success_log",),
        "provision confidential handoff",
        'No "provisioned confidential service" debug log.',
    ),
    JournalConfigEffectCase(
        "spp.disable_confidential.enabled",
        "solstone/think/services/spp.py:disable_confidential",
        ("success_log", "post_commit_operation"),
        "disable enabled confidential service",
        'No "disabled confidential service" debug log; attestation state not cleared.',
    ),
    JournalConfigEffectCase(
        "spl.enable",
        "solstone/think/services/spl.py:enable_spl",
        ("pre_commit_side_effect", "success_log"),
        "enable sol private link",
        'No service token write before commit; no "enabled sol private link" log.',
    ),
    JournalConfigEffectCase(
        "spl.disable.enabled",
        "solstone/think/services/spl.py:disable_spl",
        ("success_log",),
        "disable enabled sol private link",
        'No "disabled sol private link" debug log.',
    ),
    JournalConfigEffectCase(
        "scout.provision",
        "solstone/think/services/scout.py:provision_scout_handoff",
        ("success_log",),
        "provision scout handoff",
        'No "provisioned scout service..." debug log.',
    ),
    JournalConfigEffectCase(
        "scout.record_pending",
        "solstone/think/services/scout.py:record_scout_pending",
        ("success_log",),
        "record pending scout approval",
        'No "recorded pending scout marker..." debug log.',
    ),
    JournalConfigEffectCase(
        "scout.disable.enabled",
        "solstone/think/services/scout.py:disable_scout",
        ("success_log",),
        "disable enabled scout service",
        'No "disabled scout service" debug log.',
    ),
)

assert len(JOURNAL_CONFIG_TRANSACTION_EFFECTS) == 32
