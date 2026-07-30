# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Callosum-based talent process manager for solstone.

Cortex listens for talent requests via the Callosum message bus and manages
talent process lifecycle:
- Receives requests via Callosum (tract="cortex", event="request")
- Creates <talent>/<timestamp>_active.jsonl files to track active uses
- Spawns talent processes and captures their stdout events
- Broadcasts all talent events back to Callosum
- Renames to <talent>/<timestamp>.jsonl when complete

Talent files provide persistence and historical record, while Callosum provides
real-time event distribution to all interested services.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from solstone.think.callosum import CallosumConnection
from solstone.think.models import calc_agent_cost
from solstone.think.providers.brain_state import (
    inspect_brain_state,
    read_active_brain_fingerprint_sha256,
)
from solstone.think.runner import _atomic_symlink
from solstone.think.services.spp_attest.cadence import TPM_HEARTBEAT_INTERVAL
from solstone.think.talent import get_output_path
from solstone.think.talents import TALENT_EXECUTION_MODULE
from solstone.think.utils import get_journal, get_rev, now_ms

_CANCEL_REASON_CODE = "chat_watchdog_cancelled"


class TalentProcess:
    """Manages a running talent subprocess."""

    def __init__(self, use_id: str, process: subprocess.Popen, log_path: Path):
        self.use_id = use_id
        self.process = process
        self.log_path = log_path
        self.stop_event = threading.Event()
        self.timeout_timer = None  # For timeout support
        self.start_time = time.time()  # Track when agent started
        self.stderr_lines: list[str] = []
        try:
            self.process_group_id: int | None = os.getpgid(process.pid)
        except ProcessLookupError:
            self.process_group_id = None

    def is_running(self) -> bool:
        """Check if the agent process is still running."""
        return self.process.poll() is None and not self.stop_event.is_set()

    def stop(self) -> None:
        """Stop the agent process gracefully."""
        self.stop_event.set()

        # Cancel timeout timer if it exists
        if self.timeout_timer:
            self.timeout_timer.cancel()

        # First try SIGTERM for graceful shutdown. Signal the process group even
        # when the direct child has exited so live descendants release pipes.
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        self._signal_process_group(signal.SIGTERM)
        try:
            self.process.wait(timeout=10)  # Give more time for graceful shutdown
        except subprocess.TimeoutExpired:
            logging.getLogger(__name__).warning(
                f"Talent {self.use_id} didn't stop gracefully, killing"
            )
            self._signal_process_group(signal.SIGKILL)
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            self.process.wait()  # Ensure zombie is reaped

    def _signal_process_group(self, sig: int) -> None:
        pgid = self.process_group_id
        if pgid is None:
            try:
                pgid = os.getpgid(self.process.pid)
            except ProcessLookupError:
                return
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return


SPP_RENEWAL_ATTEMPT_BOUND_S = 120.0
SPP_REFRESH_OBSERVATION_BOUND_S = 300.0
SPP_RENEWAL_RETRY_DELAYS_S = (5.0, 10.0, 20.0, 40.0, 60.0)
SPP_RENEWAL_PROACTIVE_MARGIN_S = (
    SPP_RENEWAL_ATTEMPT_BOUND_S + SPP_RENEWAL_RETRY_DELAYS_S[0]
)
SPP_RENEWAL_ACK_TIMEOUT_S = 15.0
SPP_RENEWAL_MAX_WAIT_S = 60.0
assert SPP_RENEWAL_PROACTIVE_MARGIN_S < TPM_HEARTBEAT_INTERVAL.total_seconds() / 2


class SppRenewalController:
    """Unattended SPP prerequisite renewal controller for Cortex."""

    def __init__(
        self,
        *,
        callosum: CallosumConnection,
        stop_event: threading.Event,
        logger: logging.Logger,
        clock: Callable[[], datetime],
        wait: Callable[[float], bool],
        journal_path: Path,
    ) -> None:
        self.callosum = callosum
        self.stop_event = stop_event
        self.logger = logger
        self.clock = clock
        self.wait = wait
        self.journal_path = journal_path
        self._pending_ref: str | None = None
        self._pending_action: str | None = None
        self._pending_fingerprint: str | None = None
        self._pending_expect_fingerprint_absent = False
        self._pending_observed_at: datetime | None = None
        self._pending_expires_at: datetime | None = None
        self._ack_deadline: datetime | None = None
        self._running_ref: str | None = None
        self._running_action: str | None = None
        self._running_fingerprint: str | None = None
        self._running_expect_fingerprint_absent = False
        self._running_observed_at: datetime | None = None
        self._running_expires_at: datetime | None = None
        self._running_deadline: datetime | None = None
        self._successor_after_ref: str | None = None
        self._successor_deadline: datetime | None = None
        self._retry_index = 0
        self._retry_after: datetime | None = None
        self._last_mode: str | None = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                delay = self.step()
            except Exception as exc:
                now = self._now()
                self._handle_step_exception(exc, now)
                delay = self._seconds_until(
                    self._retry_after, now, default=SPP_RENEWAL_RETRY_DELAYS_S[0]
                )
            self.wait(max(0.0, min(delay, SPP_RENEWAL_MAX_WAIT_S)))

    def step(self) -> float:
        now = self._now()
        try:
            return self._step(now)
        except Exception as exc:
            self._handle_step_exception(exc, now)
            return self._seconds_until(
                self._retry_after, now, default=SPP_RENEWAL_RETRY_DELAYS_S[0]
            )

    def _step(self, now: datetime) -> float:
        if self._spp_disabled(now):
            self._clear_demand()
            if self._last_mode != "disabled":
                self._log("disabled", reason="non_spp_lane")
            self._last_mode = "disabled"
            return 30.0
        if self._pending_ref is not None:
            if self._ack_deadline is not None and now >= self._ack_deadline:
                self._log("failed", reason="start_ack_timeout", ref=self._pending_ref)
                self._clear_pending()
                self._schedule_retry(now)
            return self._seconds_until(self._ack_deadline, now, default=5.0)
        if self._running_ref is not None:
            if self._running_deadline is not None and now >= self._running_deadline:
                self._log(
                    "stale", reason="running_observation_timeout", ref=self._running_ref
                )
                self._clear_running()
                self._schedule_retry(now)
                return self._seconds_until(
                    self._retry_after, now, default=SPP_RENEWAL_RETRY_DELAYS_S[0]
                )
            return self._seconds_until(self._running_deadline, now, default=5.0)
        if self._successor_after_ref is not None:
            if self._successor_deadline is not None and now < self._successor_deadline:
                return self._seconds_until(
                    self._successor_deadline,
                    now,
                    default=5.0,
                )
            self._log(
                "stale",
                reason="successor_observation_timeout",
                active_ref=self._successor_after_ref,
            )
            self._clear_successor()
        if self._retry_after is not None:
            if now < self._retry_after:
                return self._seconds_until(self._retry_after, now, default=5.0)
            self._retry_after = None

        plan = self._plan(now)
        if plan["action"] == "disabled":
            self._clear_demand()
            if self._last_mode != "disabled":
                self._log("disabled", reason=plan.get("reason"))
            self._last_mode = "disabled"
            return 30.0
        self._last_mode = plan["action"]
        if plan["action"] == "checking":
            return 5.0
        if plan["action"] == "wait":
            return float(plan["delay"])
        if plan["action"] in {"renew", "refresh"}:
            if self._send_request(plan):
                return SPP_RENEWAL_ACK_TIMEOUT_S
            return self._seconds_until(
                self._retry_after,
                now,
                default=SPP_RENEWAL_RETRY_DELAYS_S[0],
            )
        return 30.0

    def _spp_disabled(self, now: datetime) -> bool:
        inspection = inspect_brain_state(now, journal_path=self.journal_path)
        return inspection["projection"]["active_lane"] != "spp"

    def handle_supervisor_message(self, message: dict[str, Any]) -> None:
        if message.get("tract") != "supervisor":
            return
        event = message.get("event")
        ref = message.get("ref")
        now = self._now()
        if event == "started" and ref == self._pending_ref:
            self._running_ref = self._pending_ref
            self._running_action = self._pending_action
            self._running_fingerprint = self._pending_fingerprint
            self._running_expect_fingerprint_absent = (
                self._pending_expect_fingerprint_absent
            )
            self._running_observed_at = self._pending_observed_at
            self._running_expires_at = self._pending_expires_at
            self._running_deadline = datetime.fromtimestamp(
                now.timestamp() + self._observation_bound(self._pending_action),
                tz=timezone.utc,
            )
            self._log("in_flight", ref=self._running_ref, action=self._running_action)
            self._clear_pending(keep_retry=True)
            return
        if event == "skipped" and ref == self._pending_ref:
            active_ref = message.get("active_ref")
            self._log(
                "in_flight",
                reason=str(message.get("reason") or "skipped"),
                ref=ref,
                active_ref=str(active_ref) if active_ref else None,
            )
            self._successor_after_ref = str(active_ref) if active_ref else None
            self._successor_deadline = (
                datetime.fromtimestamp(
                    now.timestamp() + SPP_REFRESH_OBSERVATION_BOUND_S,
                    tz=timezone.utc,
                )
                if self._successor_after_ref is not None
                else None
            )
            self._clear_pending(keep_retry=True)
            if self._successor_after_ref is None:
                self._schedule_retry(now)
            return
        if event == "stopped":
            if ref == self._running_ref:
                self._verify_running_result(self._exit_code(message), now)
                return
            if ref == self._successor_after_ref:
                self._clear_successor()

    def _plan(self, now: datetime) -> dict[str, Any]:
        inspection = inspect_brain_state(now, journal_path=self.journal_path)
        projection = inspection["projection"]
        if projection["active_lane"] != "spp":
            return {"action": "disabled", "reason": "non_spp_lane"}
        if projection["aggregate_state"] == "checking":
            return {"action": "checking"}

        fingerprint = read_active_brain_fingerprint_sha256(
            journal_path=self.journal_path
        )
        if fingerprint is None:
            return {
                "action": "refresh",
                "fingerprint": None,
                "expect_fingerprint_absent": True,
            }
        record = inspection["record"]
        component = None
        if record is not None:
            component = record["evidence"].get("lane_prerequisites")
        observed_at = None
        expires_at = None
        if isinstance(component, dict):
            observed_at = self._parse_time(component.get("observed_at"))
            expires_at = self._parse_time(component.get("expires_at"))
        if (
            projection["aggregate_state"] != "ready"
            or not isinstance(record, dict)
            or record.get("fingerprint_sha256") != fingerprint
            or not isinstance(component, dict)
            or component.get("status") != "ok"
        ):
            return {
                "action": "refresh",
                "fingerprint": fingerprint,
                "observed_at": observed_at,
                "expires_at": expires_at,
            }

        if observed_at is None or expires_at is None:
            return {
                "action": "refresh",
                "fingerprint": fingerprint,
                "observed_at": observed_at,
                "expires_at": expires_at,
            }
        if now >= expires_at:
            return {
                "action": "refresh",
                "fingerprint": fingerprint,
                "observed_at": observed_at,
                "expires_at": expires_at,
            }
        renew_at = expires_at.timestamp() - SPP_RENEWAL_PROACTIVE_MARGIN_S
        delay = renew_at - now.timestamp()
        if delay > 0:
            self._log("scheduled", delay_s=round(delay, 3))
            return {"action": "wait", "delay": delay}
        return {
            "action": "renew",
            "fingerprint": fingerprint,
            "observed_at": observed_at,
            "expires_at": expires_at,
        }

    def _send_request(self, plan: dict[str, Any]) -> bool:
        action = str(plan["action"])
        fingerprint = plan.get("fingerprint")
        ref = f"spp-renewal-{uuid.uuid4().hex}"
        if action == "renew":
            cmd = [
                "journal",
                "brain",
                "renew-prerequisites",
                "--json",
                "--expected-fingerprint",
                str(fingerprint),
            ]
        else:
            expect_absent = bool(plan.get("expect_fingerprint_absent"))
            if fingerprint is None and not expect_absent:
                self._log("failed", reason="fingerprint_unavailable", action=action)
                self._schedule_retry(self._now())
                return False
            cmd = ["journal", "brain", "refresh", "--json"]
            if expect_absent:
                cmd.append("--expect-active-fingerprint-absent")
            else:
                cmd.extend(
                    [
                        "--expected-fingerprint",
                        str(fingerprint),
                        "--expected-active-fingerprint",
                    ]
                )
        now = self._now()
        try:
            self.callosum.emit(
                "supervisor",
                "request",
                cmd=cmd,
                ref=ref,
                scheduler_name="spp-renewal",
            )
        except Exception as exc:
            self._log("failed", reason=type(exc).__name__, action=action)
            self._schedule_retry(now)
            return False
        self._pending_ref = ref
        self._pending_action = action
        self._pending_fingerprint = str(fingerprint) if fingerprint else None
        self._pending_expect_fingerprint_absent = bool(
            plan.get("expect_fingerprint_absent")
        )
        self._pending_observed_at = plan.get("observed_at")
        self._pending_expires_at = plan.get("expires_at")
        self._ack_deadline = datetime.fromtimestamp(
            now.timestamp() + SPP_RENEWAL_ACK_TIMEOUT_S, tz=timezone.utc
        )
        self._log("in_flight", ref=ref, action=action)
        return True

    def _verify_running_result(self, exit_code: int, now: datetime) -> None:
        action = self._running_action
        fingerprint = self._running_fingerprint
        expect_absent = self._running_expect_fingerprint_absent
        previous_observed = self._running_observed_at
        previous_expires = self._running_expires_at
        ref = self._running_ref
        self._clear_running()
        if action == "renew" and self._persisted_spp_prerequisite_verified(
            fingerprint,
            previous_observed,
            previous_expires,
            now,
            require_ready=False,
        ):
            self._retry_index = 0
            self._retry_after = None
            self._log("verified", ref=ref)
            return
        if (
            action == "refresh"
            and exit_code == 0
            and (
                self._persisted_spp_prerequisite_verified(
                    fingerprint,
                    previous_observed,
                    previous_expires,
                    now,
                    require_ready=True,
                )
                if not expect_absent
                else self._persisted_spp_absence_bootstrap_verified(
                    previous_observed,
                    previous_expires,
                    now,
                )
            )
        ):
            self._retry_index = 0
            self._retry_after = None
            self._log("verified", ref=ref, action="refresh")
            return
        self._log("failed", ref=ref, action=action, exit_code=exit_code)
        self._schedule_retry(now)

    def _persisted_spp_prerequisite_verified(
        self,
        fingerprint: str | None,
        previous_observed: datetime | None,
        previous_expires: datetime | None,
        now: datetime,
        *,
        require_ready: bool,
    ) -> bool:
        if fingerprint is None:
            return False
        try:
            active_fingerprint = read_active_brain_fingerprint_sha256(
                journal_path=self.journal_path
            )
            inspection = inspect_brain_state(now, journal_path=self.journal_path)
        except Exception:
            return False
        if active_fingerprint != fingerprint:
            return False
        projection = inspection["projection"]
        if projection["active_lane"] != "spp":
            return False
        if require_ready and projection["aggregate_state"] != "ready":
            return False
        record = inspection["record"]
        if record is None or record["active_lane"] != "spp":
            return False
        if record["fingerprint_sha256"] != fingerprint:
            return False
        component = record["evidence"].get("lane_prerequisites")
        if not isinstance(component, dict) or component.get("status") != "ok":
            return False
        observed_at = self._parse_time(component.get("observed_at"))
        expires_at = self._parse_time(component.get("expires_at"))
        return (
            observed_at is not None
            and expires_at is not None
            and (previous_observed is None or observed_at > previous_observed)
            and (previous_expires is None or expires_at > previous_expires)
        )

    def _persisted_spp_absence_bootstrap_verified(
        self,
        previous_observed: datetime | None,
        previous_expires: datetime | None,
        now: datetime,
    ) -> bool:
        try:
            active_fingerprint = read_active_brain_fingerprint_sha256(
                journal_path=self.journal_path
            )
            inspection = inspect_brain_state(now, journal_path=self.journal_path)
        except Exception:
            return False
        if active_fingerprint is None:
            return False
        projection = inspection["projection"]
        if (
            projection["active_lane"] != "spp"
            or projection["aggregate_state"] != "ready"
        ):
            return False
        record = inspection["record"]
        if record is None or record["active_lane"] != "spp":
            return False
        if record["fingerprint_sha256"] != active_fingerprint:
            return False
        component = record["evidence"].get("lane_prerequisites")
        if not isinstance(component, dict) or component.get("status") != "ok":
            return False
        observed_at = self._parse_time(component.get("observed_at"))
        expires_at = self._parse_time(component.get("expires_at"))
        return (
            observed_at is not None
            and expires_at is not None
            and (previous_observed is None or observed_at > previous_observed)
            and (previous_expires is None or expires_at > previous_expires)
        )

    def _schedule_retry(self, now: datetime) -> None:
        delay = SPP_RENEWAL_RETRY_DELAYS_S[
            min(self._retry_index, len(SPP_RENEWAL_RETRY_DELAYS_S) - 1)
        ]
        self._retry_index += 1
        self._retry_after = datetime.fromtimestamp(
            now.timestamp() + delay, tz=timezone.utc
        )
        self._log("retrying", delay_s=delay)

    def _handle_step_exception(self, exc: Exception, now: datetime) -> None:
        self._log("failed", reason=type(exc).__name__)
        self._clear_pending(keep_retry=True)
        self._clear_running()
        self._clear_successor()
        self._schedule_retry(now)

    def _observation_bound(self, action: str | None) -> float:
        if action == "refresh":
            return SPP_REFRESH_OBSERVATION_BOUND_S
        return SPP_RENEWAL_ATTEMPT_BOUND_S

    def _exit_code(self, message: dict[str, Any]) -> int:
        value = message.get("exit_code")
        if value is None:
            return -1
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _clear_pending(self, *, keep_retry: bool = False) -> None:
        self._pending_ref = None
        self._pending_action = None
        self._pending_fingerprint = None
        self._pending_expect_fingerprint_absent = False
        self._pending_observed_at = None
        self._pending_expires_at = None
        self._ack_deadline = None
        if not keep_retry:
            self._retry_after = None

    def _clear_running(self) -> None:
        self._running_ref = None
        self._running_action = None
        self._running_fingerprint = None
        self._running_expect_fingerprint_absent = False
        self._running_observed_at = None
        self._running_expires_at = None
        self._running_deadline = None

    def _clear_successor(self) -> None:
        self._successor_after_ref = None
        self._successor_deadline = None

    def _clear_demand(self) -> None:
        self._clear_pending()
        self._clear_running()
        self._clear_successor()
        self._retry_index = 0
        self._retry_after = None

    def _now(self) -> datetime:
        return self.clock().astimezone(timezone.utc)

    def _seconds_until(
        self, deadline: datetime | None, now: datetime, *, default: float
    ) -> float:
        if deadline is None:
            return default
        return max(0.0, deadline.timestamp() - now.timestamp())

    def _parse_time(self, value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            return None

    def _log(self, event: str, **fields: object) -> None:
        safe = " ".join(
            f"{key}={value}"
            for key, value in sorted(fields.items())
            if value is not None
        )
        suffix = f" {safe}" if safe else ""
        self.logger.info("event=spp_renewal_%s%s", event, suffix)


class CortexService:
    """Callosum-based talent process manager."""

    def __init__(
        self,
        journal_path: Optional[str] = None,
        *,
        clock: Callable[[], datetime] | None = None,
        wait: Callable[[float], bool] | None = None,
    ):
        self.journal_path = Path(journal_path or get_journal())
        self.talents_dir = self.journal_path / "talents"
        self.talents_dir.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger(__name__)
        self.running_uses: Dict[str, TalentProcess] = {}
        self.use_requests: Dict[str, Dict[str, Any]] = {}  # Store use requests
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.shutdown_requested = threading.Event()
        self.spawn_queue: queue.Queue = queue.Queue()
        self.cancel_queue: queue.Queue = queue.Queue()
        self._pending_spawns: int = 0
        self._spawn_worker: threading.Thread | None = None
        self._cancel_worker: threading.Thread | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._wait = wait or self.stop_event.wait
        self._spp_renewal_controller: SppRenewalController | None = None
        self._spp_renewal_worker: threading.Thread | None = None

        # Callosum connection for receiving requests and broadcasting events
        self.callosum = CallosumConnection(defaults={"rev": get_rev()})

    def _create_error_event(
        self,
        use_id: str,
        error: str,
        trace: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create standardized error event."""
        event = {
            "event": "error",
            "ts": now_ms(),
            "use_id": use_id,
            "error": error,
        }
        if trace:
            event["trace"] = trace
        if exit_code is not None:
            event["exit_code"] = exit_code
        return event

    def _claim_finalize(self, use_id: str) -> bool:
        """Atomically claim finalization rights for a tracked talent use."""
        with self.lock:
            if use_id not in self.running_uses:
                return False
            del self.running_uses[use_id]
            return True

    def _clear_request(self, use_id: str) -> None:
        """Clear stored request metadata after completion."""
        with self.lock:
            self.use_requests.pop(use_id, None)

    def _append_use_event(
        self, use_id: str, active_path: Path, event: Dict[str, Any]
    ) -> bool:
        """Append a use event without recreating a completed active log."""
        line = json.dumps(event) + "\n"
        try:
            fd = os.open(active_path, os.O_WRONLY | os.O_APPEND)
        except FileNotFoundError:
            completed_path = active_path.parent / f"{use_id}.jsonl"
            if completed_path.exists():
                self.logger.info(
                    "Dropping late %s event for completed use %s",
                    event.get("event", "?"),
                    use_id,
                )
            else:
                self.logger.warning(
                    "Use log missing for %s; dropping %s event",
                    use_id,
                    event.get("event", "?"),
                )
            return False
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _abort_spawn(
        self,
        use_id: str,
        file_path: Path,
        process: subprocess.Popen | None,
        error_message: str,
    ) -> None:
        """Abort a spawn failure and complete the use as an error."""
        if process is not None:
            # Spawn aborts only reap the direct child; group cleanup belongs to stop().
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        with self.lock:
            self.running_uses.pop(use_id, None)

        self._write_error_and_complete(file_path, error_message)

        with self.lock:
            self.use_requests.pop(use_id, None)

    def _recover_orphaned_uses(self, active_files: list) -> None:
        """Recover orphaned active talent files from a previous crash.

        Appends an error event to each file and renames to completed.
        """
        for file_path in active_files:
            use_id = file_path.stem.replace("_active", "")
            try:
                error_event = self._create_error_event(
                    use_id, "Recovered: Cortex restarted while talent was running"
                )
                with open(file_path, "a") as f:
                    f.write(json.dumps(error_event) + "\n")

                completed_path = file_path.parent / f"{use_id}.jsonl"
                file_path.rename(completed_path)
                self.logger.warning(f"Recovered orphaned talent: {use_id}")
            except Exception as e:
                self.logger.error(f"Failed to recover talent {use_id}: {e}")

    def start(self) -> None:
        """Start listening for talent requests via Callosum."""
        # Recover any orphaned active files from previous crash
        active_files = list(self.talents_dir.glob("*/*_active.jsonl"))
        if active_files:
            self.logger.warning(
                f"Found {len(active_files)} orphaned talent use(s), recovering..."
            )
            self._recover_orphaned_uses(active_files)

        # Connect to Callosum to receive requests
        try:
            self.callosum.start(callback=self._handle_callosum_message)
            self.logger.info("Connected to Callosum message bus")
            if self._should_request_brain_refresh():
                self.callosum.emit(
                    "supervisor", "request", cmd=["journal", "brain", "refresh"]
                )
                self.logger.info("Requested brain health refresh via supervisor")
        except Exception as e:
            self.logger.error(f"Failed to connect to Callosum: {e}")
            sys.exit(1)

        # Start status emission thread
        threading.Thread(
            target=self._emit_periodic_status,
            name="cortex-status",
            daemon=True,
        ).start()
        self._spawn_worker = threading.Thread(
            target=self._run_spawn_worker,
            name="cortex-spawn-worker",
            daemon=True,
        )
        self._spawn_worker.start()
        self._cancel_worker = threading.Thread(
            target=self._run_cancel_worker,
            name="cortex-cancel-worker",
            daemon=True,
        )
        self._cancel_worker.start()
        self._start_spp_renewal_controller()

        self.logger.info("Cortex service started, listening for talent requests")

        while True:
            try:
                while not self.stop_event.is_set():
                    time.sleep(1)
                    # Exit when idle during shutdown
                    if self.shutdown_requested.is_set():
                        if self._is_idle():
                            self.logger.info(
                                "No talent uses running, exiting gracefully"
                            )
                            return
                break
            except KeyboardInterrupt:
                self.logger.info("Shutdown requested, will exit when idle")
                self.shutdown_requested.set()

    def _should_request_brain_refresh(self) -> bool:
        try:
            inspection = inspect_brain_state(
                self._clock(), journal_path=self.journal_path
            )
        except Exception:
            return True
        projection = inspection["projection"]
        if projection["active_lane"] == "spp":
            return False
        if projection["aggregate_state"] in {"checking", "ready"}:
            return False
        return not projection["runtime_transition_in_progress"]

    def _start_spp_renewal_controller(self) -> None:
        self._spp_renewal_controller = SppRenewalController(
            callosum=self.callosum,
            stop_event=self.stop_event,
            logger=self.logger,
            clock=self._clock,
            wait=self._wait,
            journal_path=self.journal_path,
        )
        self._spp_renewal_worker = threading.Thread(
            target=self._spp_renewal_controller.run,
            name="cortex-spp-renewal",
            daemon=True,
        )
        self._spp_renewal_worker.start()

    def _handle_callosum_message(self, message: Dict[str, Any]) -> None:
        """Handle incoming Callosum messages (callback)."""
        if self._spp_renewal_controller is not None:
            self._spp_renewal_controller.handle_supervisor_message(message)
        # Filter for cortex tract and inbound work events.
        event = message.get("event")
        if message.get("tract") != "cortex" or event not in {"request", "cancel"}:
            return

        # Handle the request
        try:
            if event == "request":
                self._handle_request(message)
            else:
                self._handle_cancel(message)
        except Exception as e:
            self.logger.exception(f"Error handling {event}: {e}")

    def _handle_cancel(self, message: Dict[str, Any]) -> None:
        """Queue cancellation for a running talent use without blocking callosum."""
        use_id = message.get("use_id")
        if not isinstance(use_id, str) or not use_id:
            self.logger.debug("Ignoring cortex cancel without use_id")
            return
        reason_code = message.get("reason_code")
        if not isinstance(reason_code, str) or not reason_code:
            reason_code = _CANCEL_REASON_CODE
        self.cancel_queue.put({"use_id": use_id, "reason_code": reason_code})

    def _handle_request(self, request: Dict[str, Any]) -> None:
        """Handle a new talent request from Callosum.

        Cortex is a minimal process manager - it only handles:
        - File lifecycle (<talent>/<id>_active.jsonl -> <talent>/<id>.jsonl)
        - Process spawning and monitoring
        - Event relay to Callosum

        All config loading, validation, and hydration is done by solstone.think.talents.
        Cortex only resolves talent cwd early so the child process starts in
        the correct working directory.
        """
        use_id = request.get("use_id")
        if not use_id:
            self.logger.error("Received request without use_id")
            return

        # Skip if this use is already being processed
        with self.lock:
            if use_id in self.running_uses:
                self.logger.debug(f"Talent use {use_id} already running, skipping")
                return

        # Create _active.jsonl file (exclusive creation to prevent race conditions)
        name = request["name"]
        safe_name = name.replace(":", "--")
        talent_subdir = self.talents_dir / safe_name
        talent_subdir.mkdir(parents=True, exist_ok=True)
        file_path = talent_subdir / f"{use_id}_active.jsonl"
        if file_path.exists():
            self.logger.debug(f"Talent use {use_id} already claimed by another process")
            return

        try:
            with open(file_path, "x") as f:  # 'x' mode fails if file exists
                f.write(json.dumps(request) + "\n")
        except FileExistsError:
            return

        self.logger.info(f"Processing talent request: {use_id}")

        with self.lock:
            self.use_requests[use_id] = request
            self._pending_spawns += 1
        self.spawn_queue.put(
            {"use_id": use_id, "file_path": file_path, "request": request}
        )

    def _run_spawn_worker(self) -> None:
        """Drain the spawn queue on a single dedicated thread (FIFO)."""
        while not self.stop_event.is_set():
            try:
                item = self.spawn_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            use_id = item["use_id"]
            file_path = item["file_path"]
            request = item["request"]
            try:
                self._spawn_subprocess(
                    use_id,
                    file_path,
                    request,
                    [sys.executable, "-m", TALENT_EXECUTION_MODULE],
                    "talent",
                )
            except Exception as e:
                # `_spawn_subprocess` handles its own failures via `_abort_spawn`;
                # this backstop only fires on a truly unexpected escape. Terminalize
                # THIS use and keep the loop alive.
                self.logger.exception(f"Spawn worker error for {use_id}: {e}")
                self._abort_spawn(use_id, file_path, None, f"Spawn worker error: {e}")
            finally:
                with self.lock:
                    self._pending_spawns -= 1
                self.spawn_queue.task_done()

    def _run_cancel_worker(self) -> None:
        """Drain the cancel queue on a single dedicated thread (FIFO)."""
        while not self.stop_event.is_set():
            try:
                item = self.cancel_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._cancel_talent_use(
                    str(item.get("use_id") or ""),
                    str(item.get("reason_code") or _CANCEL_REASON_CODE),
                )
            except Exception as e:
                self.logger.exception(f"Cancel worker error: {e}")
            finally:
                self.cancel_queue.task_done()

    def _is_idle(self) -> bool:
        """True when nothing is running, queued, or mid-spawn on the worker."""
        with self.lock:
            return not self.running_uses and self._pending_spawns == 0

    def _spawn_subprocess(
        self,
        use_id: str,
        file_path: Path,
        config: Dict[str, Any],
        cmd: list[str],
        process_type: str,
    ) -> None:
        """Spawn a subprocess and monitor its output.

        Args:
            use_id: Unique identifier for this process
            file_path: Path to the JSONL log file
            config: Configuration dict to pass via NDJSON stdin
            cmd: Command to run (e.g., [sys.executable, "-m", TALENT_EXECUTION_MODULE])
            process_type: Label for logging ("talent")
        """
        try:
            process: subprocess.Popen | None = None

            # Store the config for later use - thread safe
            with self.lock:
                self.use_requests[use_id] = config

            # Pass the full config through as NDJSON
            ndjson_input = json.dumps(config)

            # Prepare environment
            env = os.environ.copy()

            # Promote top-level config keys to environment so tools can read
            # them as defaults (e.g., sol call entities search uses SOL_FACET).
            # Explicit env overrides below take precedence.
            if config.get("facet"):
                env["SOL_FACET"] = str(config["facet"])
            if config.get("day"):
                env["SOL_DAY"] = str(config["day"])

            # Apply explicit env overrides (from thinking.py etc.) — these win
            env_overrides = config.get("env")
            if env_overrides and isinstance(env_overrides, dict):
                env.update({k: str(v) for k, v in env_overrides.items()})

            # Spawn the subprocess
            self.logger.info(f"Spawning {process_type} {use_id}: {cmd}")
            self.logger.debug(f"NDJSON input: {ndjson_input}")
            subprocess_cwd = None
            talent_meta: dict[str, Any] | None = None
            if process_type == "talent":
                from solstone.think.talent import get_talent

                talent_key = str(config["name"])
                talent_meta = get_talent(talent_key)
                with self.lock:
                    if use_id in self.use_requests:
                        self.use_requests[use_id]["type"] = talent_meta.get("type")
                if talent_meta.get("type") == "cogitate":
                    # Resolve here because prepare_config() runs inside solstone.think.talents.
                    cwd_value = talent_meta.get("cwd")
                    if cwd_value == "journal":
                        try:
                            subprocess_cwd = str(Path(get_journal()))
                        except Exception as exc:
                            raise RuntimeError(
                                f"Cannot resolve cwd for talent '{talent_key}'"
                            ) from exc

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                bufsize=1,
                cwd=subprocess_cwd,
                process_group=0,
            )

            # Send input and close stdin
            process.stdin.write(ndjson_input + "\n")
            process.stdin.close()

            # Track the running process
            agent = TalentProcess(use_id, process, file_path)
            with self.lock:
                self.running_uses[use_id] = agent

            # Set up timeout (default to 10 minutes if not specified)
            timeout_seconds = config.get("timeout_seconds")
            if timeout_seconds is None and process_type == "talent" and talent_meta:
                timeout_seconds = talent_meta.get("timeout_seconds")
            if timeout_seconds is None:
                timeout_seconds = 600
            agent.timeout_timer = threading.Timer(
                timeout_seconds,
                lambda: self._timeout_talent(use_id, agent, timeout_seconds),
            )
            agent.timeout_timer.start()

            # Start monitoring threads
            threading.Thread(
                target=self._monitor_stdout, args=(agent,), daemon=True
            ).start()

            threading.Thread(
                target=self._monitor_stderr, args=(agent,), daemon=True
            ).start()

            self.logger.info(
                f"{process_type.capitalize()} {use_id} spawned successfully "
                f"(PID: {process.pid})"
            )

        except Exception as e:
            self.logger.exception(f"Failed to spawn {process_type} {use_id}: {e}")
            self._abort_spawn(
                use_id,
                file_path,
                process,
                f"Failed to spawn {process_type}: {e}",
            )

    def _timeout_talent(
        self, use_id: str, agent: TalentProcess, timeout_seconds: int
    ) -> None:
        """Handle talent timeout."""
        if not self._claim_finalize(use_id):
            return

        self.logger.warning(
            f"Talent {use_id} timed out after {timeout_seconds} seconds"
        )
        error_event = self._create_error_event(
            use_id, f"Talent timed out after {timeout_seconds} seconds"
        )
        self._append_use_event(use_id, agent.log_path, error_event)

        # Broadcast to callosum so wait_for_uses detects immediately
        try:
            event_copy = error_event.copy()
            event_type = event_copy.pop("event", "error")
            self.callosum.emit("cortex", event_type, **event_copy)
        except Exception:
            pass

        agent.stop()
        self._complete_use_file(use_id, agent.log_path)
        self._clear_request(use_id)

    def _cancel_talent_use(self, use_id: str, reason_code: str) -> None:
        """Terminalize and stop a running talent use cancelled by a caller."""
        if not use_id:
            return
        with self.lock:
            agent = self.running_uses.get(use_id)
        if agent is None or not self._claim_finalize(use_id):
            return

        self.logger.warning("Talent %s cancelled by cortex cancel event", use_id)
        error_event = self._create_error_event(
            use_id,
            "Talent cancelled by chat watchdog",
        )
        error_event["reason_code"] = reason_code
        self._append_use_event(use_id, agent.log_path, error_event)

        # Broadcast to callosum so wait_for_uses detects immediately
        try:
            event_copy = error_event.copy()
            event_type = event_copy.pop("event", "error")
            self.callosum.emit("cortex", event_type, **event_copy)
        except Exception:
            pass

        agent.stop()
        self._complete_use_file(use_id, agent.log_path)
        self._clear_request(use_id)

    def _monitor_stdout(self, agent: TalentProcess) -> None:
        """Monitor talent stdout and append events to the JSONL file."""
        if not agent.process.stdout:
            return

        try:
            with agent.process.stdout:
                for line in agent.process.stdout:
                    if not line:
                        continue

                    line = line.strip()
                    if not line:
                        continue

                    try:
                        # Parse JSON event
                        event = json.loads(line)

                        # Ensure event has timestamp and use_id
                        if "ts" not in event:
                            event["ts"] = now_ms()
                        if "use_id" not in event:
                            event["use_id"] = agent.use_id

                        # Inject agent name for WebSocket consumers
                        with self.lock:
                            _req = self.use_requests.get(agent.use_id)
                        if _req and "name" not in event:
                            event["name"] = _req.get("name", "")
                        if _req and "day" not in event:
                            event["day"] = _req.get("day", "")

                        # Append to JSONL file
                        self._append_use_event(agent.use_id, agent.log_path, event)

                        # Broadcast event to Callosum
                        try:
                            event_copy = event.copy()
                            event_type = event_copy.pop("event", "unknown")
                            self.callosum.emit("cortex", event_type, **event_copy)
                        except Exception as e:
                            self.logger.info(
                                f"Failed to broadcast event to Callosum: {e}"
                            )

                        # Handle start event
                        if event.get("event") == "start":
                            # Capture model and provider for status reporting
                            with self.lock:
                                if agent.use_id in self.use_requests:
                                    model = event.get("model")
                                    if model:
                                        self.use_requests[agent.use_id]["model"] = model
                                    provider = event.get("provider")
                                    if provider:
                                        self.use_requests[agent.use_id]["provider"] = (
                                            provider
                                        )

                        # Handle finish or terminal error event
                        terminal_error = event.get("event") == "error" and event.get(
                            "terminal", True
                        )
                        if event.get("event") == "finish" or terminal_error:
                            # Get original request (thread-safe access)
                            with self.lock:
                                original_request = self.use_requests.get(agent.use_id)

                            # Log token usage if available
                            usage_data = event.get("usage")
                            if (
                                usage_data
                                and original_request
                                and original_request.get("type") == "cogitate"
                            ):
                                try:
                                    from solstone.think.models import log_token_usage
                                    from solstone.think.talent import key_to_context

                                    model = usage_data.get(
                                        "model_version"
                                    ) or original_request.get("model", "unknown")
                                    name = original_request.get("name", "unknown")
                                    context = key_to_context(name)

                                    # Extract segment from env if set (flat merge puts env at top level)
                                    env_config = original_request.get("env", {})
                                    segment = (
                                        env_config.get("SOL_SEGMENT")
                                        if env_config
                                        else None
                                    )

                                    log_token_usage(
                                        model=model,
                                        usage=usage_data,
                                        context=context,
                                        segment=segment,
                                        type="cogitate",
                                    )
                                except Exception as e:
                                    self.logger.warning(
                                        f"Failed to log token usage for talent {agent.use_id}: {e}"
                                    )

                            # Break to trigger cleanup
                            break

                    except json.JSONDecodeError:
                        # Non-JSON output becomes info event
                        info_event = {
                            "event": "info",
                            "ts": now_ms(),
                            "message": line,
                            "use_id": agent.use_id,
                        }
                        self._append_use_event(agent.use_id, agent.log_path, info_event)

        except Exception as e:
            self.logger.error(f"Error monitoring stdout for agent {agent.use_id}: {e}")
        finally:
            # Wait for process to fully exit (reaps zombie)
            exit_code = agent.process.wait()
            self.logger.info(f"Talent {agent.use_id} exited with code {exit_code}")

            if agent.timeout_timer:
                agent.timeout_timer.cancel()

            if not self._claim_finalize(agent.use_id):
                return

            # Check if finish event was emitted
            has_finish = self._has_finish_event(agent.log_path)

            if not has_finish:
                # Write error event if no finish using standardized format
                error_event = self._create_error_event(
                    agent.use_id,
                    f"Talent exited with code {exit_code} without finish event",
                    trace="\n".join(agent.stderr_lines) or None,
                    exit_code=exit_code,
                )
                self._append_use_event(agent.use_id, agent.log_path, error_event)

            # Complete the file (rename from _active.jsonl to .jsonl)
            self._complete_use_file(agent.use_id, agent.log_path)
            self._clear_request(agent.use_id)

    def _monitor_stderr(self, agent: TalentProcess) -> None:
        """Monitor talent stderr for errors."""
        if not agent.process.stderr:
            return

        try:
            with agent.process.stderr:
                for line in agent.process.stderr:
                    if not line:
                        continue
                    stripped = line.strip()
                    if stripped:
                        agent.stderr_lines.append(stripped)
                        # Pass through to cortex stderr with talent prefix for traceability
                        print(
                            f"[talent:{agent.use_id}:stderr] {stripped}",
                            file=sys.stderr,
                            flush=True,
                        )

        except Exception as e:
            self.logger.error(f"Error monitoring stderr for agent {agent.use_id}: {e}")

    def _has_finish_event(self, file_path: Path) -> bool:
        """Check if the JSONL file contains a finish or terminal error event."""
        try:
            with open(file_path, "r") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                        terminal_error = event.get("event") == "error" and event.get(
                            "terminal", True
                        )
                        if event.get("event") == "finish" or terminal_error:
                            return True
                    except json.JSONDecodeError as exc:
                        self.logger.warning(
                            "Malformed event in %s while scanning for finish: %s",
                            file_path,
                            exc,
                        )
                        continue
        except FileNotFoundError:
            self.logger.debug("Use log disappeared before finish scan: %s", file_path)
        except OSError as exc:
            self.logger.warning(
                "Failed to scan %s for finish events: %s", file_path, exc
            )
        return False

    def _complete_use_file(self, use_id: str, file_path: Path) -> None:
        """Complete a talent use by renaming the file from _active.jsonl to .jsonl."""
        try:
            completed_path = file_path.parent / f"{use_id}.jsonl"
            file_path.rename(completed_path)
            self.logger.info(f"Completed talent use {use_id}: {completed_path}")

            # Create convenience symlink: {name}.log -> {name}/{use_id}.jsonl
            request = self.use_requests.get(use_id)
            if request:
                name = request.get("name")
                if name:
                    safe_name = name.replace(":", "--")
                    link_path = self.talents_dir / f"{safe_name}.log"
                    _atomic_symlink(link_path, f"{safe_name}/{use_id}.jsonl")
                    self.logger.debug(
                        f"Symlinked {safe_name}.log -> {safe_name}/{use_id}.jsonl"
                    )

                    # Append summary to day index
                    self._append_day_index(use_id, request, completed_path)
                else:
                    self.logger.debug(
                        f"No name in request for {use_id}, skipping symlink"
                    )
        except Exception as e:
            self.logger.error(f"Failed to complete talent file {use_id}: {e}")

    def _summarize_output_file(self, request: Dict[str, Any]) -> str | None:
        """Return the API-facing output path if it exists at completion time."""
        if not request.get("output"):
            return None

        try:
            if request.get("output_path"):
                out_path = Path(request["output_path"])
            else:
                req_day = request.get("day")
                if not req_day:
                    return None
                day_dir = self.talents_dir.parent / req_day
                req_env = request.get("env") or {}
                out_path = get_output_path(
                    day_dir,
                    request["name"],
                    segment=request.get("segment"),
                    output_format=request.get("output"),
                    facet=request.get("facet"),
                    stream=req_env.get("SOL_STREAM"),
                )

            if not out_path.exists():
                return None

            req_day = request.get("day")
            day_dir = self.talents_dir.parent / req_day if req_day else None
            if day_dir and out_path.is_relative_to(day_dir):
                return str(out_path.relative_to(day_dir))
            return str(out_path.relative_to(self.talents_dir.parent))
        except (OSError, ValueError, KeyError):
            return None

    def _append_day_index(
        self, use_id: str, request: Dict[str, Any], completed_path: Path
    ) -> None:
        """Append talent-use summary to the day index file."""
        try:
            # Determine day from request or use_id timestamp
            day = request.get("day")
            if not day:
                from datetime import datetime

                ts_seconds = int(use_id) / 1000
                day = datetime.fromtimestamp(ts_seconds).strftime("%Y%m%d")

            start_ts = request.get("ts", 0)

            thinking_count = 0
            tool_count = 0
            finish_usage = None
            degraded = None
            error_message = None
            reason_code = None
            model = None
            runtime_seconds = None
            status = "completed"
            try:
                with open(completed_path, "r") as f:
                    lines = f.readlines()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        event_type = event.get("event")
                        if event_type == "thinking":
                            thinking_count += 1
                        elif event_type == "tool_start":
                            tool_count += 1
                        elif event_type == "start":
                            model = event.get("model")

                        if event_type == "finish":
                            status = "completed"
                            finish_usage = event.get("usage")
                            degraded = event.get("degraded")
                            end_ts = event.get("ts", 0)
                            if end_ts and start_ts:
                                runtime_seconds = round((end_ts - start_ts) / 1000.0, 1)
                        if event_type == "error":
                            status = "error"
                            msg = event.get("error", "")
                            error_message = msg[:200] if msg else None
                            reason_code = event.get("reason_code")
                            end_ts = event.get("ts", 0)
                            if end_ts and start_ts:
                                runtime_seconds = round((end_ts - start_ts) / 1000.0, 1)
                    except json.JSONDecodeError:
                        continue
            except Exception:
                pass

            summary = {
                "use_id": use_id,
                "name": request["name"],
                "day": day,
                "facet": request.get("facet"),
                "ts": start_ts,
                "status": status,
                "runtime_seconds": runtime_seconds,
                "provider": request.get("provider"),
                "model": model,
                "schedule": request.get("schedule"),
                "thinking_count": thinking_count,
                "tool_count": tool_count,
                "cost": calc_agent_cost(model, finish_usage),
                "error_message": error_message if status == "error" else None,
                "reason_code": reason_code if status == "error" else None,
                "degraded": degraded,
                "output_file": self._summarize_output_file(request),
                "prompt": request.get("prompt", ""),
            }

            day_index_path = self.talents_dir / f"{day}.jsonl"
            with open(day_index_path, "a") as f:
                f.write(json.dumps(summary) + "\n")
                f.flush()

        except Exception as e:
            self.logger.error(f"Failed to append day index for {use_id}: {e}")

    def _write_error_and_complete(self, file_path: Path, error_message: str) -> None:
        """Write an error event to the file and mark it as complete."""
        try:
            use_id = file_path.stem.replace("_active", "")
            error_event = self._create_error_event(use_id, error_message)
            self._append_use_event(use_id, file_path, error_event)

            # Complete the file
            self._complete_use_file(use_id, file_path)
        except Exception as e:
            self.logger.error(f"Failed to write error and complete: {e}")

    def stop(self) -> None:
        """Stop the Cortex service."""
        self.stop_event.set()

        if self.callosum:
            self.callosum.stop()

        if self._spp_renewal_worker is not None:
            self._spp_renewal_worker.join(timeout=2.0)

        # Let the spawn worker finish its current item and exit (~2s bound; the
        # worker's 0.5s get-timeout guarantees it observes stop_event promptly).
        if self._spawn_worker is not None:
            self._spawn_worker.join(timeout=2.0)

        # Let the cancel worker finish its current item and exit (~2s bound; the
        # worker's 0.5s get-timeout guarantees it observes stop_event promptly).
        if self._cancel_worker is not None:
            self._cancel_worker.join(timeout=2.0)

        # Terminalize any claims still queued. The worker has exited, so no other
        # thread pulls these concurrently.
        while True:
            try:
                item = self.spawn_queue.get_nowait()
            except queue.Empty:
                break
            self._abort_spawn(
                item["use_id"], item["file_path"], None, "Cortex stopped before spawn"
            )
            with self.lock:
                self._pending_spawns -= 1

        # Stop all running talent uses
        with self.lock:
            for agent in self.running_uses.values():
                agent.stop()

    def _emit_periodic_status(self) -> None:
        """Emit status events every 5 seconds (runs in background thread)."""
        while not self.stop_event.is_set():
            try:
                self._emit_status_once()
            except Exception as e:
                self.logger.debug(f"Status emission failed: {e}")

            time.sleep(5)

    def _emit_status_once(self) -> None:
        with self.lock:
            uses = [
                {
                    "use_id": use_id,
                    "name": self.use_requests.get(use_id, {}).get("name", "unknown"),
                    "provider": self.use_requests.get(use_id, {}).get(
                        "provider", "unknown"
                    ),
                    "elapsed_seconds": int(time.time() - agent_proc.start_time),
                }
                for use_id, agent_proc in self.running_uses.items()
            ]
        queue_depth = self.spawn_queue.qsize()
        # Emit when work is running OR queued, so a backlog with nothing yet
        # spawned is still observable (the point of surfacing depth).
        if uses or queue_depth > 0:
            self.callosum.emit(
                "cortex",
                "status",
                running_uses=len(uses),
                uses=uses,
                queue_depth=queue_depth,
            )

    def get_status(self) -> Dict[str, Any]:
        """Get service status information."""
        with self.lock:
            return {
                "running_uses": len(self.running_uses),
                "use_ids": list(self.running_uses.keys()),
            }


def main() -> None:
    """CLI entry point for the Cortex service."""
    import argparse

    from solstone.think.utils import require_solstone, setup_cli

    parser = argparse.ArgumentParser(description="solstone Cortex Talent Manager")
    args = setup_cli(parser)
    require_solstone()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO if not args.verbose else logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Start the service
    cortex = CortexService()
    _install_sigterm_handler(cortex)

    try:
        cortex.start()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Shutting down Cortex service")
        cortex.stop()


def _install_sigterm_handler(cortex: CortexService) -> None:
    def handle_sigterm(_signum, _frame) -> None:
        logging.getLogger(__name__).info("SIGTERM received, shutting down Cortex")
        cortex.stop()

    signal.signal(signal.SIGTERM, handle_sigterm)


if __name__ == "__main__":
    main()
