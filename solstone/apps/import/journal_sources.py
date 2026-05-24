# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import threading
from functools import wraps
from pathlib import Path
from typing import Any

from flask import abort, g, request

from solstone.apps.utils import get_app_storage_path
from solstone.convey import state
from solstone.think.entities.core import atomic_write
from solstone.think.utils import now_ms

logger = logging.getLogger(__name__)

KEY_BYTES = 32
STATE_AREAS = ("segments", "entities", "facets", "imports", "config")
FINGERPRINT_RE = re.compile(r"^sha256:([a-f0-9]{64})$")
_PEER_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,256}$")


def is_valid_journal_source_name(name: str) -> bool:
    return (
        bool(name) and name not in {".", ".."} and "/" not in name and "\\" not in name
    )


def generate_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode().rstrip("=")


def _fingerprint_hex(fingerprint: str) -> str:
    match = FINGERPRINT_RE.fullmatch(fingerprint)
    if match is None:
        raise ValueError("journal source fingerprint must be sha256:<64 hex chars>")
    return match.group(1)


def journal_source_state_prefix(source: dict[str, Any]) -> str:
    if source.get("pair_mode") == "pl":
        fingerprint = source.get("fingerprint")
        if not isinstance(fingerprint, str):
            raise ValueError("PL journal source record must include fingerprint")
        return _fingerprint_hex(fingerprint)[:16]

    key = source.get("key")
    if not isinstance(key, str) or len(key) < 8:
        raise ValueError("DL journal source record must include key")
    return key[:8]


def _journal_source_filename(source: dict[str, Any]) -> str:
    if source.get("pair_mode") == "pl":
        return f"{journal_source_state_prefix(source)}.json"

    name = source.get("name")
    if not isinstance(name, str) or not is_valid_journal_source_name(name):
        raise ValueError("DL journal source record must include a valid name")
    return f"{name}.json"


def _persistable_source(source: dict[str, Any]) -> dict[str, Any]:
    clean = dict(source)
    clean.pop("filename_prefix", None)
    return clean


def _augment_source(source: dict[str, Any], filename_prefix: str | None = None) -> dict:
    augmented = dict(source)
    augmented["filename_prefix"] = filename_prefix or journal_source_state_prefix(
        augmented
    )
    return augmented


def _validate_journal_source_record(record: dict[str, Any], path: Path) -> dict | None:
    clean = _persistable_source(record)
    key = clean.get("key")
    fingerprint = clean.get("fingerprint")
    peer_instance_id = clean.get("peer_instance_id")
    has_key = isinstance(key, str) and bool(key)
    has_fingerprint = isinstance(fingerprint, str) and bool(fingerprint)
    if has_key == has_fingerprint:
        logger.warning("Skipping invalid journal source record %s", path)
        return None

    if peer_instance_id is not None:
        if not isinstance(peer_instance_id, str) or not _PEER_INSTANCE_ID_RE.fullmatch(
            peer_instance_id
        ):
            logger.warning(
                "Skipping invalid journal source record %s: bad peer_instance_id",
                path,
            )
            return None
        if clean.get("pair_mode") != "pl":
            logger.warning(
                "Skipping invalid journal source record %s: peer_instance_id only valid for pl records",
                path,
            )
            return None

    pair_mode = clean.get("pair_mode")
    if has_fingerprint:
        if pair_mode != "pl":
            logger.warning("Skipping invalid journal source record %s", path)
            return None
    elif pair_mode is not None:
        logger.warning("Skipping invalid journal source record %s", path)
        return None

    try:
        prefix = journal_source_state_prefix(clean)
        _journal_source_filename(clean)
    except ValueError as exc:
        logger.warning("Skipping invalid journal source record %s: %s", path, exc)
        return None
    return _augment_source(clean, prefix)


def get_journal_sources_dir() -> Path:
    return get_app_storage_path("import", "journal_sources", ensure_exists=True)


class JournalSourceRegistry:
    _instance: JournalSourceRegistry | None = None
    _instance_lock = threading.Lock()

    def __init__(self, sources_dir: Path) -> None:
        self._sources_dir = sources_dir
        self._lock = threading.Lock()
        self._mtime_ns = -1
        self._by_key: dict[str, dict] = {}
        self._by_fingerprint: dict[str, dict] = {}
        self._by_name: dict[str, dict] = {}
        self._records: list[dict] = []

    @classmethod
    def singleton(cls) -> JournalSourceRegistry:
        sources_dir = get_journal_sources_dir()
        with cls._instance_lock:
            if cls._instance is None or cls._instance._sources_dir != sources_dir:
                cls._instance = cls(sources_dir)
            return cls._instance

    def invalidate(self) -> None:
        with self._lock:
            self._mtime_ns = -1

    def _current_mtime_ns(self) -> int:
        try:
            current = self._sources_dir.stat().st_mtime_ns
        except FileNotFoundError:
            return 0
        for source_path in self._sources_dir.glob("*.json"):
            try:
                current = max(current, source_path.stat().st_mtime_ns)
            except FileNotFoundError:
                continue
        return current

    def reload_if_stale(self) -> None:
        current_mtime = self._current_mtime_ns()
        with self._lock:
            if current_mtime == self._mtime_ns:
                return
            self._reload_locked(current_mtime)

    def by_key(self, key: str) -> dict | None:
        self.reload_if_stale()
        with self._lock:
            record = self._by_key.get(key)
            return dict(record) if record is not None else None

    def by_fingerprint(self, fingerprint: str) -> dict | None:
        self.reload_if_stale()
        with self._lock:
            record = self._by_fingerprint.get(fingerprint)
            return dict(record) if record is not None else None

    def by_name(self, name: str) -> dict | None:
        self.reload_if_stale()
        with self._lock:
            record = self._by_name.get(name)
            return dict(record) if record is not None else None

    def all(self) -> list[dict]:
        self.reload_if_stale()
        with self._lock:
            return [dict(record) for record in self._records]

    def _reload_locked(self, current_mtime: int) -> None:
        by_key: dict[str, dict] = {}
        by_fingerprint: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        records: list[dict] = []
        for source_path in self._sources_dir.glob("*.json"):
            try:
                with open(source_path, encoding="utf-8") as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Skipping unreadable journal source %s: %s", source_path, exc
                )
                continue
            if not isinstance(raw, dict):
                logger.warning("Skipping invalid journal source record %s", source_path)
                continue
            record = _validate_journal_source_record(raw, source_path)
            if record is None:
                continue
            try:
                expected_filename = _journal_source_filename(record)
            except ValueError as exc:
                logger.warning(
                    "Skipping invalid journal source record %s: %s", source_path, exc
                )
                continue
            if source_path.name != expected_filename:
                logger.warning(
                    "Skipping journal source record with mismatched filename %s",
                    source_path,
                )
                continue
            key = record.get("key")
            if isinstance(key, str) and key:
                by_key[key] = record
            fingerprint = record.get("fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                by_fingerprint[fingerprint] = record
            name = record.get("name")
            if isinstance(name, str) and name:
                by_name[name] = record
            records.append(record)
        records.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        self._by_key = by_key
        self._by_fingerprint = by_fingerprint
        self._by_name = by_name
        self._records = records
        self._mtime_ns = current_mtime


def load_journal_source(key: str) -> dict | None:
    return JournalSourceRegistry.singleton().by_key(key)


def load_journal_source_by_fingerprint(fingerprint: str) -> dict | None:
    return JournalSourceRegistry.singleton().by_fingerprint(fingerprint)


def save_journal_source(data: dict) -> bool:
    clean = _persistable_source(data)
    try:
        if _validate_journal_source_record(clean, Path("<journal_source>")) is None:
            return False
        source_path = get_journal_sources_dir() / _journal_source_filename(clean)
        atomic_write(source_path, json.dumps(clean, indent=2))
        os.chmod(source_path, 0o600)
        JournalSourceRegistry.singleton().invalidate()
        return True
    except (OSError, ValueError):
        return False


def list_journal_sources() -> list[dict]:
    return JournalSourceRegistry.singleton().all()


def find_journal_source_by_name(name: str) -> dict | None:
    if not is_valid_journal_source_name(name):
        return None
    return JournalSourceRegistry.singleton().by_name(name)


def mint_pl_journal_source_record(
    fingerprint: str,
    device_label: str,
    paired_at: str,
    peer_instance_id: str | None = None,
) -> Path:
    prefix = _fingerprint_hex(fingerprint)[:16]
    sources_dir = get_journal_sources_dir()
    source_path = sources_dir / f"{prefix}.json"
    if source_path.exists():
        raise FileExistsError(source_path)
    record = {
        "pair_mode": "pl",
        "fingerprint": fingerprint,
        "device_label": device_label,
        "paired_at": paired_at,
        "created_at": now_ms(),
        "enabled": True,
        "revoked": False,
        "revoked_at": None,
        "stats": {
            "segments_received": 0,
            "entities_received": 0,
            "facets_received": 0,
            "imports_received": 0,
            "config_received": 0,
        },
    }
    if peer_instance_id is not None:
        record["peer_instance_id"] = peer_instance_id
    atomic_write(source_path, json.dumps(record, indent=2))
    os.chmod(source_path, 0o600)
    JournalSourceRegistry.singleton().invalidate()
    return source_path


def create_state_directory(journal_root: Path, key_prefix: str) -> Path:
    state_dir = journal_root / "imports" / key_prefix
    state_dir.mkdir(parents=True, exist_ok=True)
    source_path = state_dir / "source.json"
    if not source_path.exists():
        source_path.write_text("{}", encoding="utf-8")
    for area in STATE_AREAS:
        area_dir = state_dir / area
        area_dir.mkdir(parents=True, exist_ok=True)
        state_path = area_dir / "state.json"
        if not state_path.exists():
            state_path.write_text("{}", encoding="utf-8")
    return state_dir


def get_state_directory(key_prefix: str) -> Path:
    return Path(state.journal_root) / "imports" / key_prefix


def require_journal_source(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        identity = getattr(g, "identity", None)
        identity_mode = getattr(identity, "mode", None)
        if identity_mode in {"pl-direct", "pl-via-spl"}:
            fingerprint = getattr(identity, "fingerprint", None)
            if not isinstance(fingerprint, str) or not fingerprint:
                abort(401, description="Missing or invalid authentication")
            source = load_journal_source_by_fingerprint(fingerprint)
            if not source:
                abort(401, description="Invalid PL identity")
            if source.get("revoked"):
                abort(403, description="Journal source has been revoked")
            if source.get("enabled") is False:
                abort(403, description="Journal source is disabled")

            g.journal_source = source
            return f(*args, **kwargs)

        auth = request.headers.get("Authorization", "")
        token = None
        if auth.startswith("Bearer "):
            bearer = auth[7:].strip()
            if bearer:
                token = bearer

        if not token:
            abort(401, description="Missing or invalid authentication")

        source = load_journal_source(token)
        if not source:
            abort(401, description="Invalid API key")
        if source.get("revoked"):
            abort(403, description="API key has been revoked")

        g.journal_source = source
        return f(*args, **kwargs)

    return wrapped
