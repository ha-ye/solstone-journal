# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Move legacy journal-config credentials into the local secret boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.importers import local_secrets
from solstone.think.journal_config import (
    hold_config_lock,
    read_journal_config,
    write_journal_config,
)
from solstone.think.utils import get_journal


@dataclass(frozen=True, slots=True)
class ConfigSecretMapping:
    path: tuple[str, ...]
    integration: str
    secret_name: str
    owner: str
    true_secret: bool
    note: str

    @property
    def path_text(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True, slots=True)
class ConfigSecretMove:
    path: str
    integration: str
    secret_name: str
    owner: str
    true_secret: bool
    note: str
    action: str


@dataclass(frozen=True, slots=True)
class ConfigSecretsMigrationResult:
    journal_path: Path
    applied: bool
    moves: tuple[ConfigSecretMove, ...]


CONFIG_SECRET_MAPPINGS: tuple[ConfigSecretMapping, ...] = (
    ConfigSecretMapping(
        path=("convey", "secret"),
        integration="convey",
        secret_name="secret",
        owner="legacy Convey access gate",
        true_secret=True,
        note="Legacy random secret; no current runtime consumer found.",
    ),
    ConfigSecretMapping(
        path=("convey", "password_hash"),
        integration="convey",
        secret_name="password_hash",
        owner="legacy Convey access gate",
        true_secret=True,
        note="Credential verifier; treat as sensitive even though it is hashed.",
    ),
    ConfigSecretMapping(
        path=("env", "GOOGLE_API_KEY"),
        integration="google",
        secret_name="GOOGLE_API_KEY",
        owner="Thinking provider / Gemini transcription / Scout",
        true_secret=True,
        note="AI Studio Gemini API key.",
    ),
    ConfigSecretMapping(
        path=("env", "OPENAI_API_KEY"),
        integration="openai",
        secret_name="OPENAI_API_KEY",
        owner="Thinking provider / voice",
        true_secret=True,
        note="OpenAI API key.",
    ),
    ConfigSecretMapping(
        path=("env", "ANTHROPIC_API_KEY"),
        integration="anthropic",
        secret_name="ANTHROPIC_API_KEY",
        owner="Thinking provider",
        true_secret=True,
        note="Anthropic API key.",
    ),
    ConfigSecretMapping(
        path=("env", "REVAI_ACCESS_TOKEN"),
        integration="revai",
        secret_name="REVAI_ACCESS_TOKEN",
        owner="Rev.ai transcription backend",
        true_secret=True,
        note="Rev.ai access token.",
    ),
    ConfigSecretMapping(
        path=("env", "PLAUD_ACCESS_TOKEN"),
        integration="plaud",
        secret_name="PLAUD_ACCESS_TOKEN",
        owner="Plaud sync importer",
        true_secret=True,
        note="Plaud API access token.",
    ),
    ConfigSecretMapping(
        path=("voice", "openai_api_key"),
        integration="voice",
        secret_name="openai_api_key",
        owner="Voice realtime sideband",
        true_secret=True,
        note="Legacy voice-specific OpenAI key.",
    ),
)

_SECRET_KEY_TERMS = (
    "secret",
    "token",
    "api_key",
    "apikey",
    "password",
    "credential",
    "private_key",
    "access_key",
)
_BENIGN_KEY_PATHS = frozenset(
    {
        "providers.key_validation",
        "services.scout.key_fingerprint",
        "oura.client_id",
    }
)


def migrate_config_secrets(
    *,
    journal_path: str | Path | None = None,
    apply: bool = False,
) -> ConfigSecretsMigrationResult:
    """Dry-run or apply the one-time replicated-config secret migration."""

    journal = Path(journal_path or get_journal()).resolve()
    with hold_config_lock(journal):
        config = read_journal_config(journal)
        moves = _plan_moves(config)
        if not apply:
            return ConfigSecretsMigrationResult(
                journal_path=journal,
                applied=False,
                moves=tuple(moves),
            )

        for mapping in _present_mappings(config):
            value = _get_path(config, mapping.path)
            if not isinstance(value, str) or not value:
                continue
            if mapping.path[0] == "env":
                local_secrets.save_env_secret(
                    mapping.secret_name,
                    value,
                    journal_path=journal,
                )
                verified = local_secrets.load_env_secret(
                    mapping.secret_name,
                    journal_path=journal,
                    include_process=False,
                )
            else:
                local_secrets.save_secret(
                    mapping.integration,
                    mapping.secret_name,
                    value,
                    journal_path=journal,
                )
                verified = local_secrets.load_secret(
                    mapping.integration,
                    mapping.secret_name,
                    journal_path=journal,
                )
            if verified != value:
                raise RuntimeError(
                    f"local secret verification failed for {mapping.path_text}"
                )
            _remove_path(config, mapping.path)

        write_journal_config(config, journal)
        return ConfigSecretsMigrationResult(
            journal_path=journal,
            applied=True,
            moves=tuple(
                ConfigSecretMove(
                    path=move.path,
                    integration=move.integration,
                    secret_name=move.secret_name,
                    owner=move.owner,
                    true_secret=move.true_secret,
                    note=move.note,
                    action="moved",
                )
                for move in moves
            ),
        )


def replicated_secret_paths(config: dict[str, Any]) -> list[str]:
    """Return credential-shaped paths still present in replicated config."""

    found: list[str] = []
    for path, value in _walk_leaf_values(config):
        path_text = ".".join(path)
        if path_text in _BENIGN_KEY_PATHS:
            continue
        lower = path_text.lower()
        if any(term in lower for term in _SECRET_KEY_TERMS) and bool(value):
            found.append(path_text)
    return found


def _plan_moves(config: dict[str, Any]) -> list[ConfigSecretMove]:
    return [
        ConfigSecretMove(
            path=mapping.path_text,
            integration=mapping.integration,
            secret_name=mapping.secret_name,
            owner=mapping.owner,
            true_secret=mapping.true_secret,
            note=mapping.note,
            action="would_move",
        )
        for mapping in _present_mappings(config)
    ]


def _present_mappings(config: dict[str, Any]) -> list[ConfigSecretMapping]:
    present: list[ConfigSecretMapping] = []
    for mapping in CONFIG_SECRET_MAPPINGS:
        value = _get_path(config, mapping.path)
        if isinstance(value, str) and value:
            present.append(mapping)
    return present


def _get_path(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _remove_path(config: dict[str, Any], path: tuple[str, ...]) -> None:
    current: Any = config
    for key in path[:-1]:
        if not isinstance(current, dict):
            return
        current = current.get(key)
    if isinstance(current, dict):
        current.pop(path[-1], None)


def _walk_leaf_values(
    value: Any, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Any]]:
    if isinstance(value, dict):
        leaves: list[tuple[tuple[str, ...], Any]] = []
        for key, nested in value.items():
            leaves.extend(_walk_leaf_values(nested, path + (str(key),)))
        return leaves
    if isinstance(value, list):
        leaves = []
        for index, nested in enumerate(value):
            leaves.extend(_walk_leaf_values(nested, path + (f"[{index}]",)))
        return leaves
    return [(path, value)]


__all__ = [
    "CONFIG_SECRET_MAPPINGS",
    "ConfigSecretMapping",
    "ConfigSecretMove",
    "ConfigSecretsMigrationResult",
    "migrate_config_secrets",
    "replicated_secret_paths",
]
