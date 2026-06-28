# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Onboarding journal identity establishment.

This module owns the staged candidate CA lifecycle, regeneration, crash-safe
lock-in, and self-certifying instance_id derivation. It performs no raw journal
I/O: content writes delegate to ca.py and paths.LinkState.save. Its only
journal_io primitive is hold_lock; its only direct filesystem mutation is
best-effort staging cleanup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from solstone.think.journal_io import hold_lock
from solstone.think.link.ca import (
    LoadedCa,
    ca_is_present,
    generate_ca,
    load_ca,
    load_or_generate_ca,
    promote_ca,
)
from solstone.think.link.mark import Mark, jid_from_spki, mark_from_spki
from solstone.think.link.paths import LinkState, ca_dir, link_root, staging_dir
from solstone.think.utils import now_ms


def _identity_lock_path() -> Path:
    return link_root() / "identity"


def _spki_der(ca: LoadedCa) -> bytes:
    return ca.cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _valid_staging_or_none() -> LoadedCa | None:
    try:
        return load_ca(staging_dir())
    except (OSError, ValueError):
        return None


def _regenerate_staging() -> LoadedCa:
    return generate_ca(staging_dir())


def is_committed() -> bool:
    """Return whether the permanent journal identity CA is committed."""
    return ca_is_present(ca_dir())


def _normalize_instance_id_against_ca(state: LinkState) -> LinkState:
    if not is_committed():
        return state

    ca = load_ca(ca_dir())
    spki = _spki_der(ca)
    derived = str(jid_from_spki(spki))
    if state.instance_id == derived:
        return state

    rewritten = LinkState(
        instance_id=derived,
        home_label=state.home_label,
        locked_at=state.locked_at if state.locked_at is not None else now_ms(),
    )
    rewritten.save()
    if str(jid_from_spki(spki)) != rewritten.instance_id:
        raise RuntimeError(
            "link identity normalization produced a non-deterministic instance_id"
        )
    return rewritten


def committed_mark() -> Mark:
    """Return the mark derived from the committed permanent CA."""
    return mark_from_spki(_spki_der(load_ca(ca_dir())))


def current_candidate_mark() -> Mark:
    """Return the current staged candidate mark, regenerating invalid staging."""
    with hold_lock(_identity_lock_path()):
        candidate = _valid_staging_or_none() or _regenerate_staging()
        return mark_from_spki(_spki_der(candidate))


def regenerate_candidate() -> Mark:
    """Generate a fresh staged candidate and return its mark."""
    with hold_lock(_identity_lock_path()):
        return mark_from_spki(_spki_der(_regenerate_staging()))


def lock_in() -> Mark:
    """Commit the staged candidate CA and persist a locked state when needed."""
    with hold_lock(_identity_lock_path()):
        if not is_committed():
            # Ensure promote_ca can reload a valid staged CA from disk.
            _valid_staging_or_none() or _regenerate_staging()
            promote_ca(staging_dir(), ca_dir())

        ca = load_ca(ca_dir())
        spki = _spki_der(ca)
        jid = str(jid_from_spki(spki))
        existing = LinkState.load()
        if existing is None or not existing.instance_id:
            LinkState(
                instance_id=jid,
                home_label="solstone",
                locked_at=now_ms(),
            ).save()
        else:
            _normalize_instance_id_against_ca(existing)

        shutil.rmtree(staging_dir(), ignore_errors=True)
        return mark_from_spki(spki)


def create_link_state(default_label: str = "solstone") -> LinkState:
    """Create the lazy LinkState under the identity lock."""
    with hold_lock(_identity_lock_path()):
        existing = LinkState.load(default_label=default_label)
        if existing is not None:
            return _normalize_instance_id_against_ca(existing)

        ca = load_or_generate_ca(ca_dir())
        jid = str(jid_from_spki(_spki_der(ca)))
        state = LinkState(instance_id=jid, home_label=default_label)
        state.save()
        return state
