# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared utilities for the observer app.

Provides common helpers for observer metadata management and sync history
that are used by both routes.py and events.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from solstone.apps.observer.auth_rejection_log import (
    record_auth_rejection,
    sweep_auth_rejection_bursts,
)
from solstone.apps.utils import get_app_storage_path, log_app_action
from solstone.convey.reasons import (
    AUTH_KEY_INVALID,
    AUTH_REQUIRED,
    FEATURE_UNAVAILABLE,
    PL_REVOKED,
    Reason,
)
from solstone.convey.utils import error_response
from solstone.observe.protocol import OBSERVER_HANDLE_HEADER
from solstone.think.contract.journal import ContractIssue
from solstone.think.journal_io import atomic_replace, hold_lock, write_bytes_exclusive
from solstone.think.media import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from solstone.think.segment_files import (
    INGEST_MANIFEST_NAME,
    RESERVED_SEGMENT_FILENAMES,
)
from solstone.think.utils import day_path, now_ms

logger = logging.getLogger(__name__)

INGEST_MANIFEST_SCHEMA_VERSION = 1
MAX_INGEST_SEGMENT_ATTEMPTS = 100
DISPOSITION_WRITTEN = "written"
DISPOSITION_ALREADY_HELD = "already_held"
DISPOSITION_RECEIVED_NOT_WRITTEN = "received_not_written"
_MEDIA_CONTENT_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
DEVICE_BINDING_FIELD = "device_binding"
DEVICE_BINDING_KIND_CERT = "cert"
DEVICE_BINDING_KIND_BROWSER = "browser"
DEVICE_BINDING_KINDS = {DEVICE_BINDING_KIND_CERT, DEVICE_BINDING_KIND_BROWSER}


@dataclass(frozen=True)
class ObserverIdentityRejection:
    reason: Reason
    detail: str
    attempted_prefix: str | None


def _is_sha256_device(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    hex_part = value.removeprefix("sha256:")
    return len(hex_part) == 64 and all(char in "0123456789abcdef" for char in hex_part)


def _normalize_device_binding(raw: object) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("device_binding must be an object")
    device = raw.get("device")
    kind = raw.get("kind")
    if not isinstance(device, str) or not _is_sha256_device(device):
        raise ValueError("device_binding.device must be sha256:<64 lowercase hex>")
    if kind not in DEVICE_BINDING_KINDS:
        raise ValueError("device_binding.kind must be cert or browser")
    return {"device": device, "kind": kind}


def observer_device_binding(record: dict[str, Any]) -> dict[str, str] | None:
    """Return a valid device binding from a loaded observer record, if present."""
    try:
        return _normalize_device_binding(record.get(DEVICE_BINDING_FIELD))
    except ValueError:
        return None


def observer_device_binding_kind(record: dict[str, Any]) -> str | None:
    binding = observer_device_binding(record)
    return None if binding is None else binding["kind"]


def get_observers_dir(*, ensure_exists: bool = True) -> Path:
    """Get the observers storage directory."""
    return get_app_storage_path("observer", "observers", ensure_exists=ensure_exists)


def get_hist_dir(key_prefix: str, ensure_exists: bool = True) -> Path:
    """Get the history directory for an observer.

    Args:
        key_prefix: Observer filename prefix
        ensure_exists: Create directory if it doesn't exist (default: True)

    Returns:
        Path to apps/observer/observers/<key_prefix>/hist/
    """
    return get_app_storage_path(
        "observer", "observers", key_prefix, "hist", ensure_exists=ensure_exists
    )


def observer_filename_prefix(record: dict[str, Any]) -> str:
    key = record.get("key")
    if isinstance(key, str) and key:
        return key[:8]
    raise ValueError("observer record must include key")


def sanitize_validation_summary(
    issues: list[ContractIssue], *, max_issues: int = 3, max_chars: int = 240
) -> str:
    """Return a bounded, content-safe one-line contract validation summary."""
    parts = []
    for issue in issues[:max_issues]:
        path = issue.path[:80]
        desc = _safe_issue_descriptor(issue.message)[:100]
        parts.append(f"{path}: {desc}")
    if len(issues) > max_issues:
        parts.append(f"(+{len(issues) - max_issues} more)")
    return "; ".join(parts)[:max_chars]


def _safe_issue_descriptor(message: str) -> str:
    if message.endswith("is a required property"):
        return message
    if message.startswith("Additional properties are not allowed"):
        return "Additional properties are not allowed"

    markers = (
        " is not of type ",
        " is not one of ",
        " does not match ",
        " is too long",
        " is too short",
        " is less than ",
        " is greater than ",
        " is not a ",
        " is not valid under ",
    )
    for marker in markers:
        index = message.find(marker)
        if index >= 0:
            return "value" + message[index:]
    return "invalid"


def record_ingest_rejection(
    observer: dict,
    *,
    reason_code: str,
    segment: str,
    stream: str,
    version: str | None,
    issues: list[ContractIssue],
) -> None:
    """Record an active ingest rejection on an observer record."""
    health = observer.setdefault("health", {})
    existing = health.get("ingest_rejection")
    now = now_ms()
    if isinstance(existing, dict):
        first_ts = existing["first_ts"]
        active_count = existing.get("active_count", 1) + 1
    else:
        first_ts = now
        active_count = 1

    health["ingest_rejection"] = {
        "reason_code": reason_code,
        "first_ts": first_ts,
        "latest_ts": now,
        "active_count": active_count,
        "segment": segment,
        "stream": stream,
        "version": version,
        "summary": sanitize_validation_summary(issues),
    }


def clear_ingest_rejection(observer: dict) -> bool:
    """Clear an active ingest rejection from an observer record."""
    health = observer.get("health")
    if isinstance(health, dict) and "ingest_rejection" in health:
        del health["ingest_rejection"]
        return True
    return False


def record_status_beacon(observer: dict, data: dict) -> None:
    """Record a sanitized observer status beacon on an observer record."""
    observer["last_seen"] = now_ms()
    beacon = {
        "name": _coerce_beacon_str(data.get("name"), 120),
        "stream_type": _coerce_beacon_str(data.get("stream_type"), 120),
        "version": _coerce_beacon_str(data.get("version"), 120),
        "uptime": _coerce_beacon_int(data.get("uptime")),
        "last_successful_sync": _coerce_beacon_int(data.get("last_successful_sync")),
        "pending_queue_depth": _coerce_beacon_int(data.get("pending_queue_depth")),
        "recent_error_count": _coerce_beacon_int(data.get("recent_error_count")),
        "last_error_reason": _coerce_beacon_str(data.get("last_error_reason"), 200),
    }
    if all(value is None for value in beacon.values()):
        return

    observer.setdefault("health", {})["beacon"] = {
        "received_at": now_ms(),
        **beacon,
    }


def _coerce_beacon_str(value: Any, max_len: int) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return str(value).strip()[:max_len]


def _coerce_beacon_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return max(0, int(value))
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        if not math.isfinite(parsed):
            return None
        return max(0, int(parsed))
    return None


def get_active_ingest_rejection(record: dict) -> dict | None:
    health = record.get("health")
    rej = health.get("ingest_rejection") if isinstance(health, dict) else None
    return rej if isinstance(rej, dict) else None


def get_health_beacon(record: dict) -> dict | None:
    health = record.get("health")
    beacon = health.get("beacon") if isinstance(health, dict) else None
    return beacon if isinstance(beacon, dict) else None


def _usable_observer_stamp(value: Any, *, current_ms: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > current_ms:
        return None
    return value


def get_delivery_divergence(
    record: dict, *, now_ms: int, reachable_within_ms: int
) -> dict | None:
    """Return delivery freshness facts for a reachable observer record.

    This is read-only and never mutates ``record``. Every unusable stamp resolves
    to ``None`` rather than an exception. The caller supplies the reachability
    window so this helper does not own a second freshness threshold.
    """
    last_seen = _usable_observer_stamp(record.get("last_seen"), current_ms=now_ms)
    if last_seen is None:
        return None
    last_segment_received_at = _usable_observer_stamp(
        record.get("last_segment_received_at"), current_ms=now_ms
    )
    if last_segment_received_at is None:
        return None

    last_seen_age_ms = now_ms - last_seen
    if last_seen_age_ms >= reachable_within_ms:
        return None

    return {
        "name": record.get("name", "unknown"),
        "last_seen_age_ms": last_seen_age_ms,
        "last_segment_received_age_ms": now_ms - last_segment_received_at,
    }


def _observer_filename(record: dict[str, Any]) -> str:
    return f"{observer_filename_prefix(record)}.json"


def _persistable_record(record: dict[str, Any]) -> dict[str, Any]:
    clean = dict(record)
    clean.pop("filename_prefix", None)
    return clean


def _augment_record(record: dict[str, Any], filename_prefix: str | None = None) -> dict:
    augmented = dict(record)
    augmented["filename_prefix"] = filename_prefix or observer_filename_prefix(record)
    return augmented


def _validate_observer_record(record: dict[str, Any], path: Path) -> dict | None:
    key = record.get("key")
    if not isinstance(key, str) or not key:
        logger.warning("Skipping invalid observer record %s", path)
        return None
    fingerprint = record.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        # Attribution is handle-only; a stray legacy fingerprint-keyed PL record
        # is simply not loaded. Production has none, so no migration is needed.
        logger.warning("Skipping fingerprint-keyed observer record %s", path)
        return None
    try:
        prefix = observer_filename_prefix(record)
        binding = _normalize_device_binding(record.get(DEVICE_BINDING_FIELD))
    except ValueError as exc:
        logger.warning("Skipping invalid observer record %s: %s", path, exc)
        return None
    clean = dict(record)
    if binding is not None:
        clean[DEVICE_BINDING_FIELD] = binding
    return _augment_record(clean, prefix)


class ObserverRegistry:
    _instance: ObserverRegistry | None = None
    _instance_lock = threading.Lock()

    def __init__(self, observers_dir: Path) -> None:
        self._observers_dir = observers_dir
        self._lock = threading.Lock()
        self._mtime_ns = -1
        self._by_key: dict[str, dict] = {}
        self._by_prefix: dict[str, dict] = {}
        self._records: list[dict] = []

    @classmethod
    def singleton(cls) -> ObserverRegistry:
        observers_dir = get_observers_dir(ensure_exists=False)
        with cls._instance_lock:
            if cls._instance is None or cls._instance._observers_dir != observers_dir:
                cls._instance = cls(observers_dir)
            return cls._instance

    def invalidate(self) -> None:
        with self._lock:
            self._mtime_ns = -1

    def _current_mtime_ns(self) -> int:
        try:
            current = self._observers_dir.stat().st_mtime_ns
        except FileNotFoundError:
            return 0
        for observer_path in self._observers_dir.glob("*.json"):
            try:
                current = max(current, observer_path.stat().st_mtime_ns)
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

    def by_prefix(self, prefix: str) -> dict | None:
        self.reload_if_stale()
        with self._lock:
            record = self._by_prefix.get(prefix)
            return dict(record) if record is not None else None

    def by_name(self, name: str) -> dict | None:
        self.reload_if_stale()
        with self._lock:
            for record in self._records:
                if record.get("name") == name:
                    return dict(record)
        return None

    def all(self) -> list[dict]:
        self.reload_if_stale()
        with self._lock:
            return [dict(record) for record in self._records]

    def _reload_locked(self, current_mtime: int) -> None:
        by_key: dict[str, dict] = {}
        by_prefix: dict[str, dict] = {}
        records: list[dict] = []
        for observer_path in self._observers_dir.glob("*.json"):
            try:
                with open(observer_path, encoding="utf-8") as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Skipping unreadable observer record %s: %s", observer_path, exc
                )
                continue
            if not isinstance(raw, dict):
                logger.warning("Skipping invalid observer record %s", observer_path)
                continue
            record = _validate_observer_record(raw, observer_path)
            if record is None:
                continue
            prefix = record["filename_prefix"]
            if observer_path.name != f"{prefix}.json":
                logger.warning(
                    "Skipping observer record with mismatched filename %s",
                    observer_path,
                )
                continue
            key = record.get("key")
            if isinstance(key, str) and key:
                by_key[key] = record
            by_prefix[prefix] = record
            records.append(record)
        records.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        self._by_key = by_key
        self._by_prefix = by_prefix
        self._records = records
        self._mtime_ns = current_mtime


def load_observer(key: str) -> dict | None:
    """Load observer metadata by handle."""
    return ObserverRegistry.singleton().by_key(key)


def save_observer(data: dict) -> bool:
    """Save observer metadata.

    Args:
        data: Observer metadata dict (must contain 'key' field)

    Returns:
        True if saved successfully, False otherwise
    """
    observers_dir = get_observers_dir()
    try:
        clean = _persistable_record(data)
        observer_path = observers_dir / _observer_filename(clean)
        atomic_replace(observer_path, json.dumps(clean, indent=2))
        os.chmod(observer_path, 0o600)
        ObserverRegistry.singleton().invalidate()
        return True
    except (OSError, ValueError):
        return False


def _find_observer(identifier: str) -> dict | None:
    """Find an observer by name or filename prefix."""
    observer = find_observer_by_name(identifier)
    if observer:
        return observer

    observers_dir = get_observers_dir(ensure_exists=False)
    observer_path = observers_dir / f"{identifier}.json"
    if observer_path.exists():
        try:
            with open(observer_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
        else:
            if isinstance(raw, dict):
                return _validate_observer_record(raw, observer_path)

    return None


def revoke_observer_record(identifier: str) -> dict:
    """Revoke an observer registration and return the mutated record."""
    observer = _find_observer(identifier)
    if not observer:
        raise ValueError(f"observer not found: {identifier}")

    if observer.get("revoked", False):
        raise ValueError(f"observer already revoked: {observer.get('name')}")

    name = observer.get("name", "")
    key_prefix = observer_filename_prefix(observer)
    observer["revoked"] = True
    observer["revoked_at"] = now_ms()

    if not save_observer(observer):
        raise RuntimeError("failed to save observer")

    log_app_action(
        app="observer",
        facet=None,
        action="observer_revoke",
        params={"name": name, "key_prefix": key_prefix},
    )
    return observer


class ObserverRevokeError(RuntimeError):
    def __init__(self, message: str, revoked: list[dict]) -> None:
        super().__init__(message)
        self.revoked = revoked


def revoke_observers_bound_to_device(device: str) -> list[dict]:
    """Revoke all observer records bound to a paired-device identity."""
    revoked: list[dict] = []
    revoked_at = now_ms()
    for observer in list_observers():
        binding = observer_device_binding(observer)
        if (
            binding is None
            or binding.get("device") != device
            or observer.get("revoked", False)
        ):
            continue
        observer["revoked"] = True
        observer["revoked_at"] = revoked_at
        if not save_observer(observer):
            raise ObserverRevokeError("failed to save observer", revoked)
        key_prefix = observer_filename_prefix(observer)
        log_app_action(
            app="observer",
            facet=None,
            action="observer_revoke",
            params={"name": observer.get("name", ""), "key_prefix": key_prefix},
        )
        revoked.append(observer)
    return revoked


def list_observers() -> list[dict]:
    """List all registered observers.

    Returns:
        List of observer metadata dicts, sorted by created_at descending
    """
    return ObserverRegistry.singleton().all()


def find_observer_by_name(name: str) -> dict | None:
    """Find observer metadata by name.

    Args:
        name: Observer name to search for

    Returns:
        Observer metadata dict if found, None otherwise
    """
    return ObserverRegistry.singleton().by_name(name)


def find_oldest_unrevoked_by_name(name: str) -> dict | None:
    """Return the oldest unrevoked observer record for a stream name.

    The survivor rule shared by idempotent register and `observer reconcile`:
    filter to unrevoked records whose name matches, then take the minimum
    created_at. Returns None when no unrevoked record exists for the name.
    """
    candidates = [
        record
        for record in list_observers()
        if record.get("name") == name and not record.get("revoked", False)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda record: record.get("created_at", 0))


def _get_auth_key() -> str | None:
    from flask import request

    handle = request.headers.get(OBSERVER_HANDLE_HEADER, "").strip()
    if handle:
        return handle
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        bearer = auth[7:].strip()
        if bearer:
            return bearer
    return None


def _disabled_observer_rejection(observer: dict) -> ObserverIdentityRejection | None:
    if observer.get("revoked", False):
        return ObserverIdentityRejection(
            PL_REVOKED,
            "Observer revoked",
            observer["filename_prefix"],
        )
    if not observer.get("enabled", True):
        return ObserverIdentityRejection(
            FEATURE_UNAVAILABLE,
            "Observer disabled",
            observer["filename_prefix"],
        )
    return None


def _rejection_response(rejection: ObserverIdentityRejection) -> tuple[Any, int]:
    return error_response(rejection.reason, detail=rejection.detail)


def _resolve_identity() -> tuple[
    dict | None, str | None, ObserverIdentityRejection | None
]:
    from flask import g

    from solstone.think.link.auth import AuthorizedClients
    from solstone.think.link.paths import authorized_clients_path

    handle = _get_auth_key()
    if not handle:
        return (
            None,
            None,
            ObserverIdentityRejection(
                AUTH_REQUIRED,
                "Authorization required",
                None,
            ),
        )

    attempted_prefix = handle[:8]
    observer = load_observer(handle)
    if observer is None:
        return (
            None,
            None,
            ObserverIdentityRejection(
                AUTH_KEY_INVALID,
                "Invalid key",
                attempted_prefix,
            ),
        )

    rejection = _disabled_observer_rejection(observer)
    if rejection is not None:
        return None, None, rejection

    binding = observer_device_binding(observer)
    if binding is None:
        return observer, observer["filename_prefix"], None

    entry = AuthorizedClients(authorized_clients_path()).get(binding["device"])
    if entry is None or entry.kind != binding["kind"]:
        return (
            None,
            None,
            ObserverIdentityRejection(
                PL_REVOKED,
                "Paired device revoked",
                observer["filename_prefix"],
            ),
        )

    if binding["kind"] == DEVICE_BINDING_KIND_CERT:
        identity = getattr(g, "identity", None)
        if (
            getattr(identity, "mode", None) not in {"pl-direct", "pl-via-spl"}
            or getattr(identity, "fingerprint", None) != binding["device"]
        ):
            return (
                None,
                None,
                ObserverIdentityRejection(
                    PL_REVOKED,
                    "Paired device revoked",
                    observer["filename_prefix"],
                ),
            )
    elif entry.observer_handle != handle:
        return (
            None,
            None,
            ObserverIdentityRejection(
                PL_REVOKED,
                "Paired device revoked",
                observer["filename_prefix"],
            ),
        )

    return observer, observer["filename_prefix"], None


def resolve_observer_identity():
    """Resolve an observer by its handle (X-Solstone-Observer, else Bearer)."""
    observer, key_prefix, rejection = _resolve_identity()
    if rejection is not None:
        return None, None, _rejection_response(rejection)
    return observer, key_prefix, None


def resolve_ingest_identity(surface: str) -> tuple[dict | None, str | None, Any]:
    """Resolve observer identity for data-bearing ingest surfaces and account rejects."""
    sweep_auth_rejection_bursts()
    observer, key_prefix, rejection = _resolve_identity()
    if rejection is not None:
        record_auth_rejection(
            surface=surface,
            reason=rejection.reason,
            attempted_prefix=rejection.attempted_prefix,
        )
        return None, None, _rejection_response(rejection)
    return observer, key_prefix, None


def append_history_record(key_prefix: str, day: str, record: dict) -> None:
    """Append a record to the sync history file.

    Args:
        key_prefix: Observer filename prefix
        day: Day string (YYYYMMDD)
        record: Record to append (will be JSON-serialized)
    """
    hist_dir = get_hist_dir(key_prefix)
    hist_path = hist_dir / f"{day}.jsonl"
    with open(hist_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_history(key_prefix: str, day: str) -> list[dict]:
    """Load sync history for an observer on a given day.

    Args:
        key_prefix: Observer filename prefix
        day: Day string (YYYYMMDD)

    Returns:
        List of history records, empty if file doesn't exist
    """
    hist_dir = get_hist_dir(key_prefix, ensure_exists=False)
    hist_path = hist_dir / f"{day}.jsonl"
    if not hist_path.exists():
        return []

    records = []
    try:
        with open(hist_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load sync history {hist_path}: {e}")
    return records


def has_history_for_stream(stream: str) -> bool:
    """True if any observer sync-history row references this stream. Read-only."""
    for observer in list_observers():
        prefix = observer["filename_prefix"]
        hist_dir = get_hist_dir(prefix, ensure_exists=False)
        if not hist_dir.exists():
            continue
        for hist_path in hist_dir.glob("*.jsonl"):
            try:
                with open(hist_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and json.loads(line).get("stream") == stream:
                            return True
            except (json.JSONDecodeError, OSError):
                continue
    return False


def prune_history_by_stream(stream: str) -> int:
    """Remove observer sync-history rows for a stream across all prefixes.

    Returns the total number of rows removed. Idempotent.
    """
    total = 0
    for observer in list_observers():
        prefix = observer["filename_prefix"]
        hist_dir = get_hist_dir(prefix, ensure_exists=False)
        if not hist_dir.exists():
            continue

        for hist_path in sorted(hist_dir.glob("*.jsonl")):
            rows = []
            try:
                with open(hist_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows.append(json.loads(line))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load sync history %s: %s", hist_path, exc)
                continue

            keep = [row for row in rows if row.get("stream") != stream]
            removed = len(rows) - len(keep)
            if not removed:
                continue

            total += removed
            content = "".join(
                json.dumps(row, ensure_ascii=False) + "\n" for row in keep
            )
            atomic_replace(hist_path, content)

    return total


def increment_stat(key_prefix: str, stat_name: str) -> None:
    """Increment a stat counter for an observer.

    Args:
        key_prefix: Observer filename prefix
        stat_name: Name of the stat to increment (e.g., 'segments_observed')
    """
    observers_dir = get_observers_dir()
    observer_path = observers_dir / f"{key_prefix}.json"
    if not observer_path.exists():
        return

    try:
        with open(observer_path) as f:
            data = json.load(f)

        data["stats"][stat_name] = data["stats"].get(stat_name, 0) + 1

        atomic_replace(observer_path, json.dumps(data, indent=2))
        os.chmod(observer_path, 0o600)
        ObserverRegistry.singleton().invalidate()
    except (json.JSONDecodeError, OSError, KeyError) as e:
        logger.warning(f"Failed to update {stat_name} for {key_prefix}: {e}")


@dataclass(frozen=True)
class IngestFile:
    submitted: str
    written: str
    content: bytes
    sha256: str

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def is_reserved(self) -> bool:
        return self.written in RESERVED_SEGMENT_FILENAMES

    @property
    def is_media(self) -> bool:
        return Path(self.written).suffix.lower() in _MEDIA_CONTENT_EXTENSIONS


@dataclass(frozen=True)
class ContentIdentityFile:
    name: str
    sha256: str
    size: int
    evidence: str

    @property
    def is_media(self) -> bool:
        return Path(self.name).suffix.lower() in _MEDIA_CONTENT_EXTENSIONS

    @property
    def is_terminal_proof_only(self) -> bool:
        return self.evidence == "terminal_proof"


@dataclass(frozen=True)
class ContentIdentityIssue:
    file: str
    resolution: str


@dataclass(frozen=True)
class IngestPlan:
    status: str
    segment: str
    requested_segment: str
    stream: str
    day: str
    files: list[IngestFile]
    records: list[dict[str, Any]]
    write_files: list[IngestFile]
    held_files: list[IngestFile]
    conflict_files: list[str]
    existing_segment: str | None = None
    segment_original: str | None = None
    created_segment: bool = False


@dataclass(frozen=True)
class IngestApplyResult:
    files_written: list[str]
    files_already_held: list[str]
    bytes_written: int
    reenter_resolution: bool = False


def resolve_ingest_plan(
    *,
    day: str,
    stream: str,
    requested_segment: str,
    files: list[IngestFile],
) -> IngestPlan:
    """Resolve an observer upload against disk without writing anything."""
    stream_dir = day_path(day, create=False) / stream
    candidates = _candidate_dirs(stream_dir, requested_segment)
    content_files = content_identity_from_uploads(files)

    for candidate_segment, candidate_dir in candidates:
        evaluation = _evaluate_candidate(candidate_dir, files, content_files)
        if evaluation is None:
            continue
        return _plan_for_candidate(
            day=day,
            stream=stream,
            requested_segment=requested_segment,
            segment=candidate_segment,
            files=files,
            content_files=content_files,
            evaluation=evaluation,
        )

    segment = _first_available_segment(stream_dir, requested_segment)
    if segment is None:
        return IngestPlan(
            status="storage_failed",
            segment=requested_segment,
            requested_segment=requested_segment,
            stream=stream,
            day=day,
            files=files,
            records=_records_for_files(
                files, disposition=DISPOSITION_RECEIVED_NOT_WRITTEN
            ),
            write_files=[],
            held_files=[],
            conflict_files=[],
        )

    records = []
    write_files = []
    for file in files:
        if file.is_reserved:
            records.append(_file_record(file, DISPOSITION_RECEIVED_NOT_WRITTEN))
        else:
            records.append(_file_record(file, DISPOSITION_WRITTEN))
            write_files.append(file)

    return IngestPlan(
        status="ok" if segment == requested_segment else "collision",
        segment=segment,
        requested_segment=requested_segment,
        stream=stream,
        day=day,
        files=files,
        records=records,
        write_files=write_files,
        held_files=[],
        conflict_files=[],
        segment_original=requested_segment if segment != requested_segment else None,
        created_segment=True,
    )


def save_ingest_plan(plan: IngestPlan, *, allow_reentry: bool) -> IngestApplyResult:
    """Apply a resolved ingest plan, writing client bytes create-exclusively."""
    if plan.status == "duplicate":
        segment_dir = day_path(plan.day, create=False) / plan.stream / plan.segment
        if plan.held_files and not _held_files_are_current(
            segment_dir, plan.held_files
        ):
            if allow_reentry:
                return IngestApplyResult(
                    files_written=[],
                    files_already_held=[],
                    bytes_written=0,
                    reenter_resolution=True,
                )
            raise FileNotFoundError("held ingest files changed before response")
        if plan.held_files:
            write_ingest_manifest(
                segment_dir,
                requested_segment=plan.requested_segment,
                files=plan.held_files,
            )
        return IngestApplyResult(
            files_written=[], files_already_held=[], bytes_written=0
        )

    if plan.status not in {"ok", "collision"}:
        return IngestApplyResult(
            files_written=[], files_already_held=[], bytes_written=0
        )

    segment_dir = day_path(plan.day) / plan.stream / plan.segment
    segment_dir.mkdir(parents=True, exist_ok=True)

    files_written: list[str] = []
    files_already_held: list[str] = []
    bytes_written = 0
    for file in plan.write_files:
        target = segment_dir / file.written
        try:
            write_bytes_exclusive(target, file.content)
        except FileExistsError:
            try:
                existing_sha = _file_sha256(target)
            except OSError:
                existing_sha = ""
            if existing_sha == file.sha256:
                files_already_held.append(file.written)
                continue
            if allow_reentry:
                return IngestApplyResult(
                    files_written=files_written,
                    files_already_held=files_already_held,
                    bytes_written=bytes_written,
                    reenter_resolution=True,
                )
            raise

        files_written.append(file.written)
        bytes_written += file.size

    # This closes the practical planner-to-response gap for held files, but it
    # is not a full filesystem lock: a delete after this check can still race
    # the history append/HTTP response. Closing that final gap needs a broader
    # segment lock that observer ingest does not currently own.
    if plan.held_files and not _held_files_are_current(segment_dir, plan.held_files):
        if allow_reentry:
            return IngestApplyResult(
                files_written=files_written,
                files_already_held=files_already_held,
                bytes_written=bytes_written,
                reenter_resolution=True,
            )
        raise FileNotFoundError("held ingest files changed before response")

    if plan.write_files or plan.held_files:
        write_ingest_manifest(
            segment_dir,
            requested_segment=plan.requested_segment,
            files=[*plan.held_files, *plan.write_files],
        )
    return IngestApplyResult(
        files_written=files_written,
        files_already_held=files_already_held,
        bytes_written=bytes_written,
    )


def write_ingest_manifest(
    segment_dir: Path, *, requested_segment: str, files: list[IngestFile]
) -> None:
    """Write the journal-authored ingest manifest for a segment."""
    manifest_path = segment_dir / INGEST_MANIFEST_NAME
    with hold_lock(manifest_path):
        existing = _read_ingest_manifest(manifest_path)
        manifest_files = dict(existing.get("files", {})) if existing else {}
        for file in files:
            if file.is_reserved:
                continue
            manifest_files[file.written] = {"sha256": file.sha256, "size": file.size}
        manifest = {
            "schema_version": INGEST_MANIFEST_SCHEMA_VERSION,
            "requested_segment": requested_segment,
            "files": manifest_files,
        }
        atomic_replace(
            manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )


def pruned_segments(records: list[dict]) -> set[str]:
    """Return segments whose latest history record is a prune record."""
    latest: dict[str, str | None] = {}
    for record in records:
        segment = record.get("segment")
        if not isinstance(segment, str) or not segment:
            continue
        record_type = record.get("type")
        latest[segment] = record_type if isinstance(record_type, str) else None
    return {
        segment for segment, record_type in latest.items() if record_type == "pruned"
    }


def content_identity_from_uploads(files: list[IngestFile]) -> list[IngestFile]:
    """Return the upload files that define observer content identity."""
    non_reserved = [file for file in files if not file.is_reserved]
    media_files = [file for file in non_reserved if file.is_media]
    return media_files or non_reserved


def content_identity_from_segment(
    segment_dir: Path,
) -> tuple[dict[str, ContentIdentityFile], ContentIdentityIssue | None]:
    """Return a segment's content identity, or a structured refusal.

    Valid ingest manifests are authoritative. Legacy manifest-less directories
    can establish identity only from media files present on disk.
    """
    manifest_files = _manifest_files(segment_dir)
    if manifest_files:
        identity: dict[str, ContentIdentityFile] = {}
        for name, entry in manifest_files.items():
            if not isinstance(name, str) or name in RESERVED_SEGMENT_FILENAMES:
                continue
            manifest_name_issue = _manifest_content_name_issue(name)
            if manifest_name_issue is not None:
                return {}, manifest_name_issue
            if not isinstance(entry, dict):
                return {}, ContentIdentityIssue(
                    name,
                    "repair ingest.json: this content entry is not a sha256/size object",
                )
            sha256 = entry.get("sha256")
            size = entry.get("size")
            if (
                not isinstance(sha256, str)
                or isinstance(size, bool)
                or not isinstance(size, int)
            ):
                return {}, ContentIdentityIssue(
                    name,
                    "repair ingest.json: this content entry needs string sha256 and integer size",
                )
            target = segment_dir / name
            if target.exists():
                try:
                    disk_sha = _file_sha256(target)
                    disk_size = target.stat().st_size
                except OSError:
                    return {}, ContentIdentityIssue(
                        name,
                        "restore a readable content file before pruning",
                    )
                if disk_sha != sha256 or disk_size != size:
                    return {}, ContentIdentityIssue(
                        name,
                        "restore bytes matching ingest.json or repair the corrupt manifest before pruning",
                    )
                evidence = "present"
            elif _has_terminal_processing_proof(target, size):
                evidence = "terminal_proof"
            else:
                return {}, ContentIdentityIssue(
                    name,
                    "restore the content file or its terminal processing proof before pruning",
                )
            identity[name] = ContentIdentityFile(name, sha256, size, evidence)
        if identity:
            return identity, None
        return {}, ContentIdentityIssue(
            INGEST_MANIFEST_NAME,
            "repair ingest.json so it names at least one non-reserved content file",
        )

    media_paths = [
        path
        for path in sorted(segment_dir.iterdir())
        if path.is_file()
        and path.name not in RESERVED_SEGMENT_FILENAMES
        and path.suffix.lower() in _MEDIA_CONTENT_EXTENSIONS
    ]
    if not media_paths:
        return {}, ContentIdentityIssue(
            INGEST_MANIFEST_NAME,
            "restore a valid ingest.json with content files or restore media files before pruning",
        )

    identity = {}
    for path in media_paths:
        try:
            identity[path.name] = ContentIdentityFile(
                path.name, _file_sha256(path), path.stat().st_size, "present"
            )
        except OSError:
            return {}, ContentIdentityIssue(
                path.name,
                "restore readable media bytes before pruning",
            )
    return identity, None


def _manifest_content_name_issue(name: str) -> ContentIdentityIssue | None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or len(path.parts) != 1
        or path.parts[0] in {"", ".", ".."}
    ):
        return ContentIdentityIssue(
            name or INGEST_MANIFEST_NAME,
            "repair ingest.json: content file names must be plain in-segment names",
        )
    return None


def content_identity_key(identity: dict[str, ContentIdentityFile]) -> tuple:
    """Return the stable duplicate-group key for content identity."""
    return tuple(
        sorted((item.name, item.sha256, item.size) for item in identity.values())
    )


def is_structural_derived_file(
    rel_name: str, content_files: dict[str, ContentIdentityFile]
) -> bool:
    """Return True for recognized journal-derived per-segment outputs."""
    path = Path(rel_name)
    if path.parts and path.parts[0] == "talents":
        return True
    if "/" in rel_name:
        return False
    if rel_name in {"events.jsonl", "timeline.json"}:
        return True
    if path.suffix.lower() not in {".jsonl", ".npz"}:
        return False
    sidecar_stem = path.stem
    for content in content_files.values():
        if not content.is_media:
            continue
        if Path(content.name).stem == sidecar_stem:
            return True
    return False


def _candidate_dirs(stream_dir: Path, requested_segment: str) -> list[tuple[str, Path]]:
    if not stream_dir.exists():
        return []
    start = requested_segment.split("_", 1)[0]
    candidates = [
        child.name
        for child in stream_dir.iterdir()
        if child.is_dir() and child.name.startswith(f"{start}_")
    ]
    candidates.sort()
    if requested_segment in candidates:
        candidates.remove(requested_segment)
        candidates.insert(0, requested_segment)
    return [(segment, stream_dir / segment) for segment in candidates]


def _evaluate_candidate(
    segment_dir: Path, files: list[IngestFile], content_files: list[IngestFile]
) -> dict[str, Any] | None:
    content_names = {file.written for file in content_files}
    manifest_files = _manifest_files(segment_dir)
    missing_content: list[IngestFile] = []
    content_conflicts: list[IngestFile] = []
    sidecar_conflicts: list[IngestFile] = []
    new_files: list[IngestFile] = []
    held_files: list[IngestFile] = []

    for file in files:
        if file.is_reserved:
            continue
        target = segment_dir / file.written
        held = False
        if target.exists():
            try:
                held = _file_sha256(target) == file.sha256
            except OSError:
                held = False
            if not held:
                if file.written in content_names:
                    content_conflicts.append(file)
                else:
                    sidecar_conflicts.append(file)
                continue
        else:
            manifest_entry = manifest_files.get(file.written)
            if _is_held_by_processing_proof(segment_dir, manifest_files, file):
                held = True
            elif manifest_entry is not None and not _manifest_entry_matches(
                manifest_entry, file
            ):
                if file.written in content_names:
                    content_conflicts.append(file)
                else:
                    new_files.append(file)
                continue
            elif file.written in content_names:
                missing_content.append(file)
                continue
            else:
                new_files.append(file)
                continue

        if held:
            held_files.append(file)

    if content_conflicts:
        return None
    if not content_files and not held_files:
        return None

    return {
        "missing_content": missing_content,
        "sidecar_conflicts": sidecar_conflicts,
        "new_files": new_files,
        "held_files": held_files,
    }


def _plan_for_candidate(
    *,
    day: str,
    stream: str,
    requested_segment: str,
    segment: str,
    files: list[IngestFile],
    content_files: list[IngestFile],
    evaluation: dict[str, Any],
) -> IngestPlan:
    records = []
    write_files: list[IngestFile] = []
    held_files: list[IngestFile] = list(evaluation["held_files"])
    conflict_files = [file.written for file in evaluation["sidecar_conflicts"]]
    missing_content = list(evaluation["missing_content"])
    new_files = list(evaluation["new_files"])
    has_media_identity = any(file.is_media for file in content_files)

    for file in files:
        if file.is_reserved:
            records.append(_file_record(file, DISPOSITION_RECEIVED_NOT_WRITTEN))
        elif file in held_files:
            records.append(_file_record(file, DISPOSITION_ALREADY_HELD))
        elif file in missing_content:
            records.append(_file_record(file, DISPOSITION_WRITTEN))
            write_files.append(file)
        elif file in new_files:
            if conflict_files and has_media_identity:
                records.append(_file_record(file, DISPOSITION_RECEIVED_NOT_WRITTEN))
            else:
                records.append(_file_record(file, DISPOSITION_WRITTEN))
                write_files.append(file)
        else:
            records.append(_file_record(file, DISPOSITION_RECEIVED_NOT_WRITTEN))

    if missing_content:
        return IngestPlan(
            status="ok",
            segment=segment,
            requested_segment=requested_segment,
            stream=stream,
            day=day,
            files=files,
            records=records,
            write_files=write_files,
            held_files=held_files,
            conflict_files=[],
            existing_segment=segment,
        )

    if conflict_files and has_media_identity:
        return IngestPlan(
            status="conflict",
            segment=segment,
            requested_segment=requested_segment,
            stream=stream,
            day=day,
            files=files,
            records=records,
            write_files=[],
            held_files=held_files,
            conflict_files=conflict_files,
            existing_segment=segment,
        )

    if write_files:
        return IngestPlan(
            status="ok",
            segment=segment,
            requested_segment=requested_segment,
            stream=stream,
            day=day,
            files=files,
            records=records,
            write_files=write_files,
            held_files=held_files,
            conflict_files=[],
            existing_segment=segment,
        )

    return IngestPlan(
        status="duplicate",
        segment=segment,
        requested_segment=requested_segment,
        stream=stream,
        day=day,
        files=files,
        records=records,
        write_files=[],
        held_files=held_files,
        conflict_files=[],
        existing_segment=segment,
    )


def _first_available_segment(stream_dir: Path, requested_segment: str) -> str | None:
    start, duration_text = requested_segment.split("_", 1)
    try:
        duration = int(duration_text)
    except ValueError:
        return None
    for offset in range(MAX_INGEST_SEGMENT_ATTEMPTS):
        candidate = requested_segment if offset == 0 else f"{start}_{duration + offset}"
        if not (stream_dir / candidate).exists():
            return candidate
    return None


def _records_for_files(
    files: list[IngestFile], *, disposition: str
) -> list[dict[str, Any]]:
    return [_file_record(file, disposition) for file in files]


def _file_record(file: IngestFile, disposition: str) -> dict[str, Any]:
    return {
        "submitted": file.submitted,
        "written": file.written,
        "size": file.size,
        "sha256": file.sha256,
        "disposition": disposition,
    }


def _read_ingest_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    schema_version = raw.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != INGEST_MANIFEST_SCHEMA_VERSION
    ):
        return {}
    files = raw.get("files")
    if not isinstance(files, dict):
        return {}
    if not all(isinstance(entry, dict) for entry in files.values()):
        return {}
    return raw


def _manifest_files(segment_dir: Path) -> dict[str, Any]:
    manifest = _read_ingest_manifest(segment_dir / INGEST_MANIFEST_NAME)
    files = manifest.get("files")
    return files if isinstance(files, dict) else {}


def _manifest_entry_matches(entry: object, file: IngestFile) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("sha256") == file.sha256
        and entry.get("size") == file.size
    )


def _is_held_by_processing_proof(
    segment_dir: Path, manifest_files: dict[str, Any], file: IngestFile
) -> bool:
    """Return whether terminal processing proof holds an absent ingest file."""
    entry = manifest_files.get(file.written)
    # Legacy segments predate manifests, so input_size is the only available
    # strength there; when a manifest entry exists it must match by hash.
    if entry is not None and not _manifest_entry_matches(entry, file):
        return False
    return _has_terminal_processing_proof(segment_dir / file.written, file.size)


def _held_files_are_current(segment_dir: Path, files: list[IngestFile]) -> bool:
    manifest_files = _manifest_files(segment_dir)
    for file in files:
        target = segment_dir / file.written
        if target.exists():
            try:
                if _file_sha256(target) == file.sha256:
                    continue
            except OSError:
                pass
        if _is_held_by_processing_proof(segment_dir, manifest_files, file):
            continue
        return False
    return True


def _has_terminal_processing_proof(path: Path, size: int) -> bool:
    from solstone.apps.observer.processing_proof import has_terminal_processing_proof

    return has_terminal_processing_proof(path, size)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
