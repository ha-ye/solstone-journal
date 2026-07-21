# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Active-brain status and bounded refresh CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, cast

from solstone.think.brain_health import brain_age, brain_reason_text
from solstone.think.journal_config import read_journal_config
from solstone.think.models import (
    AttestationFailedError,
    AttestationNotVerifiedError,
    AttestationStaleError,
    generate_with_result,
)
from solstone.think.providers import get_provider_module
from solstone.think.providers.brain_state import (
    BRAIN_AGGREGATE_STATES,
    BRAIN_EVIDENCE_REASON_CODES,
    BRAIN_REASON_TO_AGGREGATE,
    COMPONENT_ORDER,
    DEFAULT_READY_EVIDENCE_TTL,
    PROVIDER_ENV_BY_NAME,
    BrainEvidenceComponent,
    BrainProbeOutcome,
    BrainStateConflictError,
    BrainStateInspection,
    BrainStateRecord,
    abandon_brain_refresh,
    begin_brain_refresh,
    brain_state_path,
    build_active_brain_fingerprint,
    finish_brain_refresh,
    inspect_brain_state,
    probe_brain_refresh_lease_held,
    runtime_phase_reason,
)
from solstone.think.providers.runtime_health import (
    REASON_CODES as RUNTIME_REASON_CODES,
)
from solstone.think.providers.runtime_health import (
    RUNTIME_PHASES,
    inspect_runtime_health,
)
from solstone.think.providers.shared import (
    CANNED_COGITATE_PROBE_PROMPT,
    CANNED_GENERATE_MAX_OUTPUT_TOKENS,
    CANNED_GENERATE_NUM_RETRIES,
    CANNED_GENERATE_PROMPT,
    CANNED_GENERATE_THINKING_BUDGET,
    CANNED_GENERATE_TIMEOUT_S,
    GenerateResult,
    classify_canned_generate,
    classify_provider_error,
)
from solstone.think.utils import require_solstone, setup_cli

LOG = logging.getLogger("solstone.think.brain_cli")

RefreshOutcome = Literal["busy", "stale_expected_fingerprint", "lost_fence"]
_REFRESH_EXIT_3: frozenset[str] = frozenset(
    {"busy", "stale_expected_fingerprint", "lost_fence"}
)
# The bundled-runtime fingerprint field is content-only; HMAC affects secret-bearing
# fingerprint components, not this read-only fence value.
_FENCE_DUMMY_HMAC_KEY = b"\x00" * 32
_BYO_REASON_MAP = {
    "local_endpoint_unreachable": "endpoint_unreachable",
    "local_endpoint_contract_failed": "endpoint_contract_failed",
}
_DIAGNOSTIC_COGITATE_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class BrainView:
    aggregate_state: str
    reason_code: str | None
    active_lane: str | None
    active_provider: str | None
    active_model: str | None
    fingerprint_sha256: str | None
    failing_component: str | None
    observed_at: str | None
    expires_at: str | None
    path: str | None
    checked_age: str | None = None
    refresh_outcome: RefreshOutcome | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def brain_exit_code(
    *,
    aggregate_state: str | None = None,
    refresh_outcome: str | None = None,
) -> int:
    if refresh_outcome is not None:
        if refresh_outcome not in _REFRESH_EXIT_3:
            raise ValueError(f"unknown refresh outcome: {refresh_outcome}")
        return 3
    if aggregate_state not in BRAIN_AGGREGATE_STATES:
        raise ValueError(f"unknown brain aggregate state: {aggregate_state}")
    if aggregate_state == "ready":
        return 0
    if aggregate_state in {"blocked", "unhealthy"}:
        return 1
    return 2


def _evidence_view(
    record: BrainStateRecord | None,
) -> tuple[str | None, str | None, str | None]:
    if record is None:
        return None, None, None
    ready_component: BrainEvidenceComponent | None = None
    for component_name in COMPONENT_ORDER:
        component = record["evidence"].get(component_name)
        if component is None:
            continue
        if component["status"] != "ok":
            return (
                component_name,
                component.get("observed_at"),
                component.get("expires_at"),
            )
        if ready_component is None:
            ready_component = component
    if ready_component is not None:
        return (
            None,
            ready_component.get("observed_at"),
            ready_component.get("expires_at"),
        )
    return None, None, None


def _view_from_inspection(
    inspection: BrainStateInspection,
    now: datetime,
) -> BrainView:
    projection = inspection["projection"]
    failing_component, observed_at, expires_at = _evidence_view(inspection["record"])
    return BrainView(
        aggregate_state=projection["aggregate_state"],
        reason_code=projection["reason_code"],
        active_lane=projection["active_lane"],
        active_provider=projection["active_provider"],
        active_model=projection["active_model"],
        fingerprint_sha256=projection["fingerprint_sha256"],
        failing_component=failing_component,
        observed_at=observed_at,
        expires_at=expires_at,
        path=inspection["path"],
        checked_age=brain_age(now, observed_at)[1],
    )


def _transient_view(
    outcome: RefreshOutcome,
    *,
    active_lane: str | None = None,
    active_provider: str | None = None,
    active_model: str | None = None,
    fingerprint_sha256: str | None = None,
) -> BrainView:
    return BrainView(
        aggregate_state="checking" if outcome == "busy" else "unknown",
        reason_code=outcome,
        active_lane=active_lane,
        active_provider=active_provider,
        active_model=active_model,
        fingerprint_sha256=fingerprint_sha256,
        failing_component=None,
        observed_at=None,
        expires_at=None,
        path=str(brain_state_path()),
        refresh_outcome=outcome,
    )


def _identity_text(view: BrainView) -> str:
    if view.active_provider and view.active_model:
        return f"{view.active_lane or 'unknown'} {view.active_provider}/{view.active_model}"
    if view.active_lane:
        return view.active_lane
    return "unknown"


def render(view: BrainView, *, json_output: bool) -> None:
    payload = {
        "aggregate_state": view.aggregate_state,
        "reason_code": view.reason_code,
        "lane": view.active_lane,
        "provider": view.active_provider,
        "model": view.active_model,
        "fingerprint_sha256": view.fingerprint_sha256,
        "failing_component": view.failing_component,
        "observed_at": view.observed_at,
        "expires_at": view.expires_at,
        "path": view.path,
    }
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return

    if view.refresh_outcome == "busy":
        print("Brain busy: check already running")
        return
    if view.aggregate_state == "ready":
        suffix = f", checked {view.checked_age} ago" if view.checked_age else ""
        print(f"Brain ready: {_identity_text(view)}{suffix}")
        return
    label = view.aggregate_state
    reason = brain_reason_text(view.reason_code)
    component = f" ({view.failing_component})" if view.failing_component else ""
    print(f"Brain {label}: {reason}{component}")


def _ok_component(
    now: datetime, *, expires_at: datetime | None = None
) -> BrainEvidenceComponent:
    expiry = expires_at or now + DEFAULT_READY_EVIDENCE_TTL
    return {
        "status": "ok",
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "expires_at": expiry.astimezone(timezone.utc).isoformat(),
    }


def _failed_component(
    now: datetime,
    reason_code: str,
    *,
    diagnostic: Mapping[str, Any] | None = None,
) -> BrainEvidenceComponent:
    aggregate = BRAIN_REASON_TO_AGGREGATE[reason_code]
    status = {
        "blocked": "blocked",
        "unhealthy": "failed",
        "unknown": "unknown",
    }.get(aggregate)
    if status is None:
        raise ValueError(f"reason is not valid evidence: {reason_code}")
    component: BrainEvidenceComponent = {
        "status": cast(Any, status),
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "reason_code": cast(Any, reason_code),
    }
    if diagnostic:
        component["diagnostic"] = dict(diagnostic)
    return component


def _not_attempted_component(now: datetime, reason_code: str) -> BrainEvidenceComponent:
    return {
        "status": "not_attempted",
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "reason_code": cast(Any, reason_code),
    }


def _config_env_value(config: Mapping[str, Any], env_key: str) -> str:
    env_block = config.get("env")
    if isinstance(env_block, Mapping):
        value = env_block.get(env_key)
        if value:
            return str(value)
    return os.environ.get(env_key, "")


def _current_fingerprint_result(config: Mapping[str, Any]) -> dict[str, Any]:
    return build_active_brain_fingerprint(config, hmac_key=_FENCE_DUMMY_HMAC_KEY)


def _current_bundled_runtime_fingerprint(config: Mapping[str, Any]) -> str | None:
    try:
        fingerprint = _current_fingerprint_result(config)
    except Exception:
        return None
    if not fingerprint or fingerprint.get("active_lane") != "bundled":
        return None
    value = fingerprint.get("bundled_runtime_fingerprint_sha256")
    return value if isinstance(value, str) else None


def _expected_fingerprint_matches(expected: str) -> bool:
    try:
        config = read_journal_config(None)
    except Exception:
        return False
    return _current_bundled_runtime_fingerprint(config) == expected


def _runtime_diagnostic(
    reason_code: str,
    *,
    phase: str | None,
    runtime_reason: str | None = None,
) -> dict[str, str]:
    diagnostic: dict[str, str] = {}
    if phase in RUNTIME_PHASES:
        diagnostic["phase"] = str(phase)
    if (
        runtime_reason in RUNTIME_REASON_CODES
        and reason_code != "local_runtime_fingerprint_mismatch"
    ):
        diagnostic["runtime_reason"] = str(runtime_reason)
    return diagnostic


def _bundled_prerequisite(
    config: Mapping[str, Any],
    now: datetime,
) -> tuple[BrainEvidenceComponent, str | None]:
    expected_runtime = _current_bundled_runtime_fingerprint(config)
    inspection = inspect_runtime_health("local")
    if inspection["status"] == "corrupt":
        return _failed_component(
            now, "local_runtime_state_invalid"
        ), "local_runtime_state_invalid"
    if inspection["status"] == "unavailable":
        return (
            _failed_component(now, "local_runtime_state_unavailable"),
            "local_runtime_state_unavailable",
        )
    record = inspection["record"]
    if not isinstance(record, Mapping):
        return (
            _failed_component(now, "local_runtime_state_unavailable"),
            "local_runtime_state_unavailable",
        )
    phase = record.get("phase")
    if not isinstance(phase, str) or phase not in RUNTIME_PHASES:
        return (
            _failed_component(now, "local_runtime_state_invalid"),
            "local_runtime_state_invalid",
        )
    runtime_reason = record.get("reason_code")
    reason = runtime_phase_reason(cast(Any, phase))
    if reason is not None:
        diagnostic = _runtime_diagnostic(
            reason,
            phase=phase,
            runtime_reason=runtime_reason if isinstance(runtime_reason, str) else None,
        )
        return _failed_component(now, reason, diagnostic=diagnostic), reason
    desired = record.get("desired_fingerprint_sha256")
    if not isinstance(desired, str):
        return (
            _failed_component(
                now,
                "local_runtime_state_invalid",
                diagnostic=_runtime_diagnostic(
                    "local_runtime_state_invalid",
                    phase=phase,
                    runtime_reason=runtime_reason
                    if isinstance(runtime_reason, str)
                    else None,
                ),
            ),
            "local_runtime_state_invalid",
        )
    if expected_runtime is None or desired != expected_runtime:
        return (
            _failed_component(
                now,
                "local_runtime_fingerprint_mismatch",
                diagnostic=_runtime_diagnostic(
                    "local_runtime_fingerprint_mismatch",
                    phase=phase,
                ),
            ),
            "local_runtime_fingerprint_mismatch",
        )
    return _ok_component(now), None


def _spp_prerequisite(now: datetime) -> tuple[BrainEvidenceComponent, str | None]:
    from solstone.think.services import spp
    from solstone.think.services.spp_transport import recheck_confidential_attestation

    try:
        recheck_confidential_attestation()
    except AttestationStaleError:
        return _failed_component(now, "attestation_expired"), "attestation_expired"
    except AttestationFailedError:
        return _failed_component(now, "attestation_rejected"), "attestation_rejected"
    except AttestationNotVerifiedError:
        return (
            _failed_component(now, "attestation_not_verified"),
            "attestation_not_verified",
        )

    state = spp.get_attestation_state()
    if state.failure is not None:
        reason = (
            "attestation_not_verified"
            if state.failure.kind == "unreachable"
            else "attestation_rejected"
        )
        return _failed_component(now, reason), reason
    if state.session is None:
        return _failed_component(
            now, "attestation_not_verified"
        ), "attestation_not_verified"
    if state.session.status(now) != "verified":
        return _failed_component(now, "attestation_expired"), "attestation_expired"
    expires_at = min(
        state.session.tpm_heartbeat_due_at,
        state.session.gpu_reattest_due_at,
        state.session.session_cap_at,
    )
    return _ok_component(now, expires_at=expires_at), None


def _lane_prerequisite(
    lane: str,
    provider: str,
    config: Mapping[str, Any],
    now: datetime,
) -> tuple[BrainEvidenceComponent, str | None]:
    if lane == "bundled":
        return _bundled_prerequisite(config, now)
    if lane == "byo-cloud":
        env_key = PROVIDER_ENV_BY_NAME.get(provider)
        if env_key and not _config_env_value(config, env_key):
            return _failed_component(
                now, "provider_key_missing"
            ), "provider_key_missing"
    if lane == "spp":
        return _spp_prerequisite(now)
    return _ok_component(now), None


def _component_reason_for_exception(
    exc: BaseException,
    *,
    provider: str,
    lane: str,
    component: str,
) -> str:
    if lane == "byo-endpoint":
        from solstone.think.providers.local_endpoint import classify_byo_cogitate_error

        endpoint_reason = classify_byo_cogitate_error(exc)
        mapped = _BYO_REASON_MAP.get(endpoint_reason or "")
        if mapped:
            return mapped
    reason = classify_provider_error(exc, provider)
    allowed = BRAIN_EVIDENCE_REASON_CODES[component]
    return reason if reason in allowed else "probe_internal_error"


def _generate_component(
    *,
    provider: str,
    lane: str,
    now: datetime,
) -> BrainEvidenceComponent:
    try:
        result = generate_with_result(
            CANNED_GENERATE_PROMPT,
            "health.brain.generate",
            temperature=0,
            max_output_tokens=CANNED_GENERATE_MAX_OUTPUT_TOKENS,
            system_instruction=None,
            json_output=False,
            thinking_budget=CANNED_GENERATE_THINKING_BUDGET,
            timeout_s=CANNED_GENERATE_TIMEOUT_S,
            num_retries=CANNED_GENERATE_NUM_RETRIES,
        )
    except Exception as exc:
        reason = _component_reason_for_exception(
            exc,
            provider=provider,
            lane=lane,
            component="generate",
        )
        return _failed_component(now, reason)
    verdict = classify_canned_generate(cast(GenerateResult, result))
    if verdict == "pass":
        return _ok_component(now)
    reason = (
        "probe_output_starved" if verdict == "starved" else "provider_response_invalid"
    )
    return _failed_component(now, reason)


async def _run_cogitate_with_timeout(module: Any, config: dict[str, Any]) -> str | None:
    return await asyncio.wait_for(
        module.run_cogitate(config=config, on_event=None),
        timeout=_DIAGNOSTIC_COGITATE_TIMEOUT_S,
    )


def _cogitate_component(
    *,
    provider: str,
    model: str,
    lane: str,
    now: datetime,
) -> BrainEvidenceComponent:
    config = {
        "diagnostic": True,
        "prompt": CANNED_COGITATE_PROBE_PROMPT,
        "provider": provider,
        "model": model,
        "max_turns": 2,
        "max_run_cost_usd": 0.05,
        "timeout_seconds": 60,
    }
    try:
        module = get_provider_module(provider)
        result = asyncio.run(_run_cogitate_with_timeout(module, config))
    except asyncio.TimeoutError:
        return _failed_component(now, "chat_timeout")
    except Exception as exc:
        reason = _component_reason_for_exception(
            exc,
            provider=provider,
            lane=lane,
            component="cogitate",
        )
        return _failed_component(now, reason)
    if isinstance(result, str) and result.strip():
        return _ok_component(now)
    return _failed_component(now, "cogitate_terminal_error")


def _probe_outcome(
    *,
    lane: str,
    provider: str,
    model: str,
    config: Mapping[str, Any],
    now: datetime,
) -> BrainProbeOutcome:
    configuration = _ok_component(now)
    lane_prerequisites, prerequisite_reason = _lane_prerequisite(
        lane,
        provider,
        config,
        now,
    )
    if prerequisite_reason is not None:
        return {
            "configuration": configuration,
            "lane_prerequisites": lane_prerequisites,
            "generate": _not_attempted_component(now, prerequisite_reason),
            "cogitate": _not_attempted_component(now, prerequisite_reason),
        }
    return {
        "configuration": configuration,
        "lane_prerequisites": lane_prerequisites,
        "generate": _generate_component(provider=provider, lane=lane, now=now),
        "cogitate": _cogitate_component(
            provider=provider,
            model=model,
            lane=lane,
            now=now,
        ),
    }


def _run_status(args: argparse.Namespace) -> int:
    now = _now()
    inspection = inspect_brain_state(now)
    view = _view_from_inspection(inspection, now)
    render(view, json_output=args.json)
    return brain_exit_code(aggregate_state=view.aggregate_state)


def _render_stale_expected(args: argparse.Namespace) -> int:
    view = _transient_view("stale_expected_fingerprint")
    render(view, json_output=args.json)
    return brain_exit_code(refresh_outcome="stale_expected_fingerprint")


def _run_refresh(args: argparse.Namespace) -> int:
    now = _now()
    expected = args.expected_fingerprint
    if expected and not _expected_fingerprint_matches(expected):
        return _render_stale_expected(args)

    inspection = inspect_brain_state(now)
    view = _view_from_inspection(inspection, now)
    if view.reason_code == "configuration_invalid":
        render(view, json_output=args.json)
        return brain_exit_code(aggregate_state=view.aggregate_state)
    if expected and view.aggregate_state == "ready":
        render(view, json_output=args.json)
        return brain_exit_code(aggregate_state=view.aggregate_state)

    permit = begin_brain_refresh(now, run_id=uuid.uuid4().hex)
    if permit is None:
        busy = False
        try:
            busy = probe_brain_refresh_lease_held()
        except OSError:
            busy = False
        if busy:
            busy_view = _transient_view(
                "busy",
                active_lane=view.active_lane,
                active_provider=view.active_provider,
                active_model=view.active_model,
                fingerprint_sha256=view.fingerprint_sha256,
            )
            render(busy_view, json_output=args.json)
            return brain_exit_code(refresh_outcome="busy")
        inspection = inspect_brain_state(now)
        view = _view_from_inspection(inspection, now)
        render(view, json_output=args.json)
        return brain_exit_code(aggregate_state=view.aggregate_state)

    checking_inspection = inspect_brain_state(now)
    checking_view = _view_from_inspection(checking_inspection, now)
    lane = checking_view.active_lane
    provider = checking_view.active_provider
    model = checking_view.active_model
    if not lane or not provider or not model:
        try:
            record = abandon_brain_refresh(permit, "probe_internal_error", now)
        except BrainStateConflictError:
            lost = _transient_view("lost_fence")
            render(lost, json_output=args.json)
            return brain_exit_code(refresh_outcome="lost_fence")
        committed = inspect_brain_state(now)
        view = _view_from_inspection(committed, now)
        render(view, json_output=args.json)
        return brain_exit_code(aggregate_state=record["aggregate_state"])

    try:
        config = read_journal_config(None)
        outcome = _probe_outcome(
            lane=lane,
            provider=provider,
            model=model,
            config=config,
            now=now,
        )
        record = finish_brain_refresh(permit, outcome, now)
    except BrainStateConflictError:
        lost = _transient_view("lost_fence")
        render(lost, json_output=args.json)
        return brain_exit_code(refresh_outcome="lost_fence")
    except Exception:
        LOG.exception("brain refresh probe failed")
        try:
            record = abandon_brain_refresh(permit, "probe_internal_error", now)
        except BrainStateConflictError:
            lost = _transient_view("lost_fence")
            render(lost, json_output=args.json)
            return brain_exit_code(refresh_outcome="lost_fence")

    inspection = inspect_brain_state(now)
    view = _view_from_inspection(inspection, now)
    render(view, json_output=args.json)
    return brain_exit_code(aggregate_state=record["aggregate_state"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="journal brain", description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand")

    status_parser = subparsers.add_parser("status", help="Show active-brain status")
    status_parser.add_argument("--json", action="store_true", help="Output JSON")

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Run one bounded active-brain check",
    )
    refresh_parser.add_argument("--json", action="store_true", help="Output JSON")
    refresh_parser.add_argument(
        "--expected-fingerprint",
        help="Only refresh if the bundled runtime fingerprint still matches",
    )
    return parser


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.subcommand == "status":
        return _run_status(args)
    if args.subcommand == "refresh":
        return _run_refresh(args)
    parser.print_help()
    return 2


def main() -> None:
    parser = build_parser()
    args = setup_cli(parser)
    require_solstone()
    raise SystemExit(_dispatch(args, parser))


if __name__ == "__main__":
    main()
