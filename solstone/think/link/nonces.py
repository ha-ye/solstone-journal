# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pair-nonce store — shared between the CLI pair flow and convey's pair route.

`sol call link pair` mints a nonce and writes it to disk; convey's
`POST /link/pair` reads on every incoming pair request, garbage-collects
expired entries, and enforces single-use semantics. The file is the IPC
channel between the two processes — simple, durable across crashes, no
extra port.

Consumers treat the file as opaque and call only into the methods here.
Atomic replaces guard against partial writes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from solstone.think.journal_io import hold_lock, write_json

NONCE_TTL_SECONDS = 300  # 5 min per the spl pairing spec.


@dataclass(frozen=True)
class Nonce:
    value: str
    device_label: str
    issued_at: int
    expires_at: int
    used: bool
    role: str = ""


class NonceStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def add(
        self,
        nonce: str,
        device_label: str,
        *,
        role: str = "",
        now: int | None = None,
        ttl: int = NONCE_TTL_SECONDS,
    ) -> Nonce:
        ts = now if now is not None else int(time.time())
        entry = Nonce(
            value=nonce,
            device_label=device_label,
            issued_at=ts,
            expires_at=ts + ttl,
            used=False,
            role=role,
        )
        with hold_lock(self._path):
            entries = self._read()
            self._gc_locked(entries, ts)
            entries[nonce] = entry
            self._write(entries)
        return entry

    def consume(self, value: str, *, now: int | None = None) -> Nonce | None:
        """Mark a nonce used if valid. Single-use enforced atomically."""
        ts = now if now is not None else int(time.time())
        with hold_lock(self._path):
            entries = self._read()
            self._gc_locked(entries, ts)
            entry = entries.get(value)
            if entry is None:
                return None
            if entry.used or entry.expires_at <= ts:
                return None
            entry = Nonce(
                value=entry.value,
                device_label=entry.device_label,
                issued_at=entry.issued_at,
                expires_at=entry.expires_at,
                used=True,
                role=entry.role,
            )
            entries[value] = entry
            self._write(entries)
            return entry

    def peek(self, value: str) -> Nonce | None:
        entries = self._read()
        return entries.get(value)

    def snapshot(self) -> list[Nonce]:
        return list(self._read().values())

    def gc(self, *, now: int | None = None) -> int:
        """Remove expired entries. Returns count removed."""
        ts = now if now is not None else int(time.time())
        with hold_lock(self._path):
            entries = self._read()
            before = len(entries)
            self._gc_locked(entries, ts)
            if len(entries) != before:
                self._write(entries)
            return before - len(entries)

    def _read(self) -> dict[str, Nonce]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        out: dict[str, Nonce] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                val = item.get("value")
                if not isinstance(val, str):
                    continue
                out[val] = Nonce(
                    value=val,
                    device_label=str(item.get("device_label", "")),
                    issued_at=int(item.get("issued_at", 0)),
                    expires_at=int(item.get("expires_at", 0)),
                    used=bool(item.get("used", False)),
                    role=item.get("role") if isinstance(item.get("role"), str) else "",
                )
        return out

    def _write(self, entries: dict[str, Nonce]) -> None:
        payload = [
            {
                "value": e.value,
                "device_label": e.device_label,
                "issued_at": e.issued_at,
                "expires_at": e.expires_at,
                "used": e.used,
                "role": e.role,
            }
            for e in entries.values()
        ]
        write_json(self._path, payload)

    def _gc_locked(self, entries: dict[str, Nonce], now: int) -> None:
        to_drop = [k for k, e in entries.items() if e.used or e.expires_at <= now]
        for k in to_drop:
            del entries[k]
