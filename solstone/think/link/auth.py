# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""authorized_clients.json — the PL revocation ledger.

The spl-protocol-fixed core shape is unchanged: `fingerprint`, `device_label`,
`paired_at`, `instance_id`, and `role`. `device_label` is the home-assigned,
renameable label for the paired client. Solstone also stores local-only
`last_seen_at`, `network`, `client_label`, and browser-uplink fields for UX:

    {
      "fingerprint": "sha256:<hex>",
      "device_label": "Jer's iPhone",
      "paired_at": "2026-04-19T17:42:13Z",
      "instance_id": "<home_instance_id>",
      "role": "",
      "last_seen_at": "2026-04-19T18:03:12Z",  // optional; null/absent = never
      "network": "network",                    // optional; local display label source
      "client_label": "jer-laptop",            // optional; client self-name/hostname
      "kind": "cert",                          // cert | browser
      "pubkey_spki": "<hex>",                  // browser only
      "observer_handle": "<handle>"            // browser only
    }

Role-less linked systems are stored with `role: ""`; peers are stored with
`role: "peer"`. The peer role is provenance, not a behavioral authorization
role: it denotes a linked system whose pairing provisioned a journal-content
source, minted a journal-source record, and records the sender `instance_id`.
That provenance is durable in data via per-segment `sender_instance_id` /
`sender_fingerprint` and identity-derived source directories. Readers reload
the file on mtime change so an unpair action takes effect within ~500 ms of the
file write. Convey's pair and unpair routes own the pairing writer surface; the
secure listener updates `last_seen_at` and uses this ledger for TLS verification
and per-request authorization.

`last_seen_at`, `network`, `client_label`, `kind`, `pubkey_spki`, and
`observer_handle` are local-only — never transmitted externally.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from solstone.think.journal_io import hold_lock, write_json

MAX_DEVICE_LABEL_LEN = 80


class StrictAuthorizationError(RuntimeError):
    """Stable-code failure from the raw-preserving authorization path."""

    def __init__(self, code: str, *, mutated: bool = False) -> None:
        self.code = code
        self.mutated = mutated
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class StrictAuthorizationReceipt:
    fingerprint: str
    device_label: str
    paired_at: str
    expected_item_canonical: str

    def __repr__(self) -> str:
        return "StrictAuthorizationReceipt(<redacted>)"

    def __reduce__(self) -> object:
        raise TypeError("StrictAuthorizationReceipt is not serializable")

    def __copy__(self) -> object:
        raise TypeError("StrictAuthorizationReceipt is not copyable")

    def __deepcopy__(self, _memo: dict[int, object]) -> object:
        raise TypeError("StrictAuthorizationReceipt is not copyable")


def is_peer(role: str) -> bool:
    return role == "peer"


@dataclass(frozen=True)
class ClientEntry:
    fingerprint: str
    device_label: str
    paired_at: str
    instance_id: str
    role: str = ""
    last_seen_at: str | None = None
    network: str | None = None
    client_label: str = ""
    kind: str = "cert"
    pubkey_spki: str | None = None
    observer_handle: str | None = None


class AuthorizedClients:
    """In-memory view of authorized_clients.json with mtime-based reload."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, ClientEntry] = {}
        self._mtime_ns = 0
        if path.exists():
            self._reload_locked()

    @property
    def path(self) -> Path:
        return self._path

    def reload_if_stale(self) -> bool:
        """Re-read the file if its mtime changed. Returns True if reloaded."""
        with self._lock:
            try:
                current = self._path.stat().st_mtime_ns
            except FileNotFoundError:
                if self._entries:
                    self._entries = {}
                    self._mtime_ns = 0
                    return True
                return False
            if current == self._mtime_ns:
                return False
            self._reload_locked()
            return True

    def is_authorized(self, fingerprint: str) -> bool:
        self.reload_if_stale()
        with self._lock:
            return fingerprint in self._entries

    def add(
        self,
        fingerprint: str,
        device_label: str,
        instance_id: str,
        *,
        role: str = "",
        paired_at: str | None = None,
        network: str | None = None,
        client_label: str = "",
    ) -> None:
        paired_at = paired_at or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = ClientEntry(
            fingerprint=fingerprint,
            device_label=device_label,
            paired_at=paired_at,
            instance_id=instance_id,
            role=role,
            last_seen_at=None,
            network=network,
            client_label=client_label,
        )
        with self._lock:
            with hold_lock(self._path):
                current = self._load_file_locked()
                current[fingerprint] = entry
                self._write(current)
                self._entries = current

    def add_browser(
        self,
        *,
        fingerprint: str,
        device_label: str,
        instance_id: str,
        pubkey_spki: str,
        observer_handle: str,
        paired_at: str | None = None,
        network: str | None = None,
    ) -> ClientEntry:
        paired_at = paired_at or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = ClientEntry(
            fingerprint=fingerprint,
            device_label=device_label,
            paired_at=paired_at,
            instance_id=instance_id,
            role="",
            last_seen_at=None,
            network=network,
            client_label="",
            kind="browser",
            pubkey_spki=pubkey_spki,
            observer_handle=observer_handle,
        )
        with self._lock:
            with hold_lock(self._path):
                current = self._load_file_locked()
                current[fingerprint] = entry
                self._write(current)
                self._entries = current
        return entry

    def remove(self, fingerprint: str) -> bool:
        with self._lock:
            with hold_lock(self._path):
                current = self._load_file_locked()
                if fingerprint not in current:
                    return False
                del current[fingerprint]
                self._write(current)
                self._entries = current
                return True

    def add_attempt_client_strict(
        self,
        *,
        fingerprint: str,
        device_label: str,
        instance_id: str,
        paired_at: str | None = None,
        role: str = "",
        network: str | None = None,
        client_label: str = "",
    ) -> StrictAuthorizationReceipt:
        """Append one proof-scoped cert authorization, preserving raw unrelated entries.

        This path is intentionally stricter than add(): malformed or ambiguous
        stores are rejected instead of normalized, and unrelated raw dict items
        are compared before/after the write. On verified success this instance's
        modeled cache is reloaded from the new file. On ambiguous failure the
        cache is marked stale so normal readers will reload on next use.
        """

        paired_at = paired_at or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _validate_strict_new_entry(
            fingerprint=fingerprint,
            device_label=device_label,
            instance_id=instance_id,
            role=role,
            paired_at=paired_at,
        )
        item: dict[str, Any] = {
            "fingerprint": fingerprint,
            "device_label": device_label,
            "paired_at": paired_at,
            "instance_id": instance_id,
            "role": role,
            "kind": "cert",
        }
        if network:
            item["network"] = network
        if client_label:
            item["client_label"] = client_label

        with self._lock:
            with hold_lock(self._path):
                items, identity = self._read_raw_list_strict(missing_ok=True)
                _reject_strict_collisions(items, fingerprint, device_label)
                before_unrelated = _canonical_items(items)
                expected = [*items, item]
                self._assert_identity_unchanged(identity)
                try:
                    write_json(self._path, expected)
                    after, _after_identity = self._read_raw_list_strict(
                        missing_ok=False
                    )
                except StrictAuthorizationError:
                    self._mark_cache_stale_locked()
                    raise
                except OSError:
                    self._mark_cache_stale_locked()
                    raise StrictAuthorizationError(
                        "write_failed", mutated=True
                    ) from None
                after_unrelated = _canonical_items(
                    entry for entry in after if entry.get("fingerprint") != fingerprint
                )
                if after_unrelated != before_unrelated or _canonical_items(
                    after
                ) != _canonical_items(expected):
                    self._mark_cache_stale_locked()
                    raise StrictAuthorizationError(
                        "post_write_verification_failed",
                        mutated=True,
                    )
                self._reload_locked()
                return StrictAuthorizationReceipt(
                    fingerprint=fingerprint,
                    device_label=device_label,
                    paired_at=paired_at,
                    expected_item_canonical=_canonical_item(item),
                )

    def remove_attempt_client_strict(
        self,
        receipt: StrictAuthorizationReceipt,
    ) -> None:
        """Remove a proof-scoped authorization and verify unrelated raw entries."""

        with self._lock:
            with hold_lock(self._path):
                items, identity = self._read_raw_list_strict(missing_ok=False)
                own_index = _find_strict_fingerprint(items, receipt.fingerprint)
                if own_index is None:
                    raise StrictAuthorizationError("missing_attempt_entry")
                remaining = [
                    item for index, item in enumerate(items) if index != own_index
                ]
                expected_unrelated = _canonical_items(remaining)
                self._assert_identity_unchanged(identity)
                try:
                    write_json(self._path, remaining)
                    after, _after_identity = self._read_raw_list_strict(missing_ok=True)
                except StrictAuthorizationError:
                    self._mark_cache_stale_locked()
                    raise
                except OSError:
                    self._mark_cache_stale_locked()
                    raise StrictAuthorizationError(
                        "write_failed", mutated=True
                    ) from None
                if _find_strict_fingerprint(after, receipt.fingerprint) is not None:
                    self._mark_cache_stale_locked()
                    raise StrictAuthorizationError(
                        "absence_unverified",
                        mutated=True,
                    )
                if _canonical_items(after) != expected_unrelated:
                    self._mark_cache_stale_locked()
                    raise StrictAuthorizationError(
                        "unrelated_entries_changed",
                        mutated=True,
                    )
                self._reload_locked()

    def verify_attempt_client_absent_strict(self, fingerprint: str) -> None:
        items, _identity = self._read_raw_list_strict(missing_ok=True)
        if _find_strict_fingerprint(items, fingerprint) is not None:
            raise StrictAuthorizationError("attempt_entry_present")

    def touch_last_seen(
        self, fingerprint: str, *, now: dt.datetime | None = None
    ) -> bool:
        """Update last_seen_at for a paired device. Returns False if not paired."""
        ts = (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            with hold_lock(self._path):
                current = self._load_file_locked()
                existing = current.get(fingerprint)
                if existing is None:
                    return False
                current[fingerprint] = replace(existing, last_seen_at=ts)
                self._write(current)
                self._entries = current
                return True

    def update_label(self, fingerprint: str, label: str) -> bool:
        """Update device_label for a paired device. Returns False if not paired."""
        normalized = label.strip()
        if not normalized:
            raise ValueError("label must not be empty")
        if len(normalized) > MAX_DEVICE_LABEL_LEN:
            raise ValueError("label too long")
        with self._lock:
            with hold_lock(self._path):
                current = self._load_file_locked()
                existing = current.get(fingerprint)
                if existing is None:
                    return False
                current[fingerprint] = replace(existing, device_label=normalized)
                self._write(current)
                self._entries = current
                return True

    def snapshot(self) -> list[ClientEntry]:
        self.reload_if_stale()
        with self._lock:
            return list(self._entries.values())

    def get(self, fingerprint: str) -> ClientEntry | None:
        self.reload_if_stale()
        with self._lock:
            return self._entries.get(fingerprint)

    def find_by_label(self, label: str) -> ClientEntry | None:
        self.reload_if_stale()
        with self._lock:
            for entry in self._entries.values():
                if label and entry.device_label == label:
                    return entry
        return None

    def _reload_locked(self) -> None:
        entries = self._load_file_locked()
        self._entries = entries
        try:
            self._mtime_ns = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            self._mtime_ns = 0

    def _load_file_locked(self) -> dict[str, ClientEntry]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            # Unreadable authorized_clients.json means no clients are authorized. There is no last-good authorization cache.
            return {}
        out: dict[str, ClientEntry] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                fp = item.get("fingerprint")
                if not isinstance(fp, str):
                    continue
                last_seen = item.get("last_seen_at")
                network = item.get("network")
                client_label = item.get("client_label")
                kind = item.get("kind")
                pubkey_spki = item.get("pubkey_spki")
                observer_handle = item.get("observer_handle")
                out[fp] = ClientEntry(
                    fingerprint=fp,
                    device_label=str(item.get("device_label", "")),
                    paired_at=str(item.get("paired_at", "")),
                    instance_id=str(item.get("instance_id", "")),
                    role=item.get("role") if isinstance(item.get("role"), str) else "",
                    last_seen_at=last_seen if isinstance(last_seen, str) else None,
                    network=network if isinstance(network, str) else None,
                    client_label=client_label if isinstance(client_label, str) else "",
                    kind=kind if isinstance(kind, str) and kind else "cert",
                    pubkey_spki=pubkey_spki if isinstance(pubkey_spki, str) else None,
                    observer_handle=(
                        observer_handle if isinstance(observer_handle, str) else None
                    ),
                )
        return out

    def _read_raw_list_strict(
        self,
        *,
        missing_ok: bool,
    ) -> tuple[list[dict[str, Any]], tuple[int, int, int, int] | None]:
        identity = _file_identity(self._path)
        if identity is None:
            if missing_ok:
                return [], None
            raise StrictAuthorizationError("missing_file")
        try:
            raw_text = self._path.read_text("utf-8")
        except OSError:
            raise StrictAuthorizationError("unreadable_file") from None
        if _file_identity(self._path) != identity:
            raise StrictAuthorizationError("concurrent_replacement")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            raise StrictAuthorizationError("malformed_json") from None
        if not isinstance(raw, list):
            raise StrictAuthorizationError("non_list_root")
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise StrictAuthorizationError("non_dict_item")
            fingerprint = item.get("fingerprint")
            if not isinstance(fingerprint, str):
                raise StrictAuthorizationError("non_string_fingerprint")
            if fingerprint in seen:
                raise StrictAuthorizationError("duplicate_fingerprint")
            seen.add(fingerprint)
            items.append(item)
        return items, identity

    def _assert_identity_unchanged(
        self,
        identity: tuple[int, int, int, int] | None,
    ) -> None:
        if _file_identity(self._path) != identity:
            raise StrictAuthorizationError("concurrent_replacement")

    def _mark_cache_stale_locked(self) -> None:
        self._mtime_ns = -1

    def _write(self, entries: dict[str, ClientEntry]) -> None:
        payload = [
            {
                "fingerprint": e.fingerprint,
                "device_label": e.device_label,
                "paired_at": e.paired_at,
                "instance_id": e.instance_id,
                "role": e.role,
                "kind": e.kind,
                **({"last_seen_at": e.last_seen_at} if e.last_seen_at else {}),
                **({"network": e.network} if e.network else {}),
                **({"client_label": e.client_label} if e.client_label else {}),
                **({"pubkey_spki": e.pubkey_spki} if e.pubkey_spki else {}),
                **({"observer_handle": e.observer_handle} if e.observer_handle else {}),
            }
            for e in entries.values()
        ]
        write_json(self._path, payload)


def _validate_strict_new_entry(
    *,
    fingerprint: str,
    device_label: str,
    instance_id: str,
    role: str,
    paired_at: str,
) -> None:
    if not isinstance(fingerprint, str) or not fingerprint:
        raise StrictAuthorizationError("invalid_fingerprint")
    if not isinstance(device_label, str) or not device_label:
        raise StrictAuthorizationError("invalid_device_label")
    if len(device_label) > MAX_DEVICE_LABEL_LEN:
        raise StrictAuthorizationError("device_label_too_long")
    if not isinstance(instance_id, str) or not instance_id:
        raise StrictAuthorizationError("invalid_instance_id")
    if not isinstance(role, str):
        raise StrictAuthorizationError("invalid_role")
    if not isinstance(paired_at, str) or not paired_at:
        raise StrictAuthorizationError("invalid_paired_at")


def _reject_strict_collisions(
    items: list[dict[str, Any]],
    fingerprint: str,
    device_label: str,
) -> None:
    for item in items:
        if item.get("fingerprint") == fingerprint:
            raise StrictAuthorizationError("fingerprint_collision")
        if item.get("device_label") == device_label:
            raise StrictAuthorizationError("label_collision")


def _find_strict_fingerprint(
    items: list[dict[str, Any]],
    fingerprint: str,
) -> int | None:
    for index, item in enumerate(items):
        if item.get("fingerprint") == fingerprint:
            return index
    return None


def _canonical_items(items: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(_canonical_item(item) for item in items)


def _canonical_item(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"))


def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StrictAuthorizationError("unreadable_file") from None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
