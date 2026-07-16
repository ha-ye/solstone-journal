# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider-aware fan-out sizing policy.

Governed local describe fan-out targets ``effective_procs * per_proc <=
2 * slots`` when integer flooring permits it. If operator-selected process
fan-out already exceeds that limit, per-proc fan-out floors at 1 and the
residual is logged once; local admission remains the real in-flight guard.
This is per invocation, and overlapping think/sense invocations are out of
scope here.
"""

from __future__ import annotations

import logging
import os

from solstone.think.models import is_local_provider_needed, resolve_provider
from solstone.think.providers.local_endpoint import resolve_local_endpoint
from solstone.think.providers.local_server import read_server_parallel_slots

_DEFAULT_DESCRIBE_PER_PROC_JOBS = 10
_CAP_LOGGED: set[str] = set()


def reset_default_cap_log_state() -> None:
    """Clear the per-process record of which cap lines have been logged."""
    _CAP_LOGGED.clear()


def _segment_work_uses_local() -> bool:
    """Return True when segment-pipeline work can resolve to the local provider."""
    return is_local_provider_needed()


def _describe_uses_local() -> bool:
    """Return True when screen-describe resolves to the local provider."""
    provider, _ = resolve_provider("generate")
    return provider == "local"


def _local_fanout_slots() -> int | None:
    endpoint = resolve_local_endpoint()
    if endpoint.is_bundled:
        return read_server_parallel_slots()
    return endpoint.parallel_slots


def cap_default_at_local_slots(formula: int, log_key: str) -> int:
    """Clamp a CPU-derived default to the local provider's client-side slots."""
    slots = _local_fanout_slots()
    if slots is None:
        return formula
    derived = min(formula, slots)
    if derived < formula and log_key not in _CAP_LOGGED:
        _CAP_LOGGED.add(log_key)
        logging.info(
            "%s capped provider=local slots=%d formula=%d derived=%d",
            log_key,
            slots,
            formula,
            derived,
        )
    return derived


def default_segment_workers() -> int:
    """Return the default segment-level worker count for repair mode.

    Capped at the local provider's client-side slot count when any
    segment-pipeline work can resolve to a governed local lane.
    """
    cpu_count = os.cpu_count() or 2
    formula = max(1, min(8, cpu_count // 2))
    if not _segment_work_uses_local():
        return formula
    return cap_default_at_local_slots(formula, "default_segment_workers")


def default_describe_jobs() -> int:
    """Return the default screen-describe worker count for repair mode.

    Capped at the local provider's client-side slot count when the describe
    path resolves to a governed local lane.
    """
    formula = max(1, min(4, (os.cpu_count() or 2) // 4))
    if not _describe_uses_local():
        return formula
    return cap_default_at_local_slots(formula, "default_describe_jobs")


def describe_per_proc_jobs(effective_procs: int) -> int:
    """Return the per-process describe request concurrency for an effective process fan-out.

    For governed local providers, size per-process concurrency so
    effective_procs * per_proc <= 2 * slots whenever integer flooring permits it.
    If effective_procs already exceeds 2 * slots, per_proc floors at 1; this leaves
    a residual product above the target, logs that residual once per key, and returns
    1 because per-invocation concurrency cannot be reduced further.

    This function sizes one describe invocation. It does not choose the number of
    describe processes, enforce local admission, or account for work outside the
    effective_procs supplied by the caller. Non-local providers and ungoverned
    confidential BYO endpoints keep the historical per-process default of 10.
    """
    if not _describe_uses_local():
        return _DEFAULT_DESCRIBE_PER_PROC_JOBS

    slots = _local_fanout_slots()
    if slots is None:
        return _DEFAULT_DESCRIBE_PER_PROC_JOBS

    per_proc = max(1, (2 * slots) // effective_procs)
    product = effective_procs * per_proc
    limit = 2 * slots
    if product > limit:
        log_key = f"describe_per_proc_jobs_residual:{slots}:{effective_procs}"
        if log_key not in _CAP_LOGGED:
            _CAP_LOGGED.add(log_key)
            logging.info(
                "%s residual provider=local slots=%d effective_procs=%d "
                "per_proc=%d product=%d limit=%d",
                "describe_per_proc_jobs",
                slots,
                effective_procs,
                per_proc,
                product,
                limit,
            )
    return per_proc
