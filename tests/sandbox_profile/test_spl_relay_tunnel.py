# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import json
import logging
import pickle
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedOK

from solstone.think.journal_io import write_json
from solstone.think.link import client as link_client
from solstone.think.link.ca import cert_fingerprint, generate_ca
from solstone.think.sandbox_profile import probe_contract, spl_readiness
from solstone.think.sandbox_profile import spl_relay_tunnel as probe
from tests.link.secure_listener_harness import SecureListenerHarness
from tests.sandbox_profile import RUN_ID, sandbox_journal, write_attempt_dir

CANARY_TOKEN = "device-token-canary"
CANARY_PRIVATE = "private-key-canary"
CANARIES = (CANARY_TOKEN, CANARY_PRIVATE)


@pytest.mark.asyncio
async def test_pass_path_binds_fingerprint_preserves_store_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    unrelated = _seed_unrelated_authorizations(journal)
    captured: dict[str, Any] = {}
    _install_fake_readiness(monkeypatch)
    _install_unreachable_path_canaries(monkeypatch)
    real_build_identity = probe._build_ephemeral_identity

    def build_identity_with_private_canary(
        journal_arg: Path,
        attempt_arg: Path,
    ) -> link_client.ClientIdentity:
        identity = real_build_identity(journal_arg, attempt_arg)
        return dataclasses.replace(identity, private_key_pem=CANARY_PRIVATE)

    async def fake_enroll(
        _relay_url: str,
        identity: link_client.ClientIdentity,
        *,
        timeout: float = 30.0,
    ) -> link_client.EnrolledDevice:
        captured["identity"] = identity
        captured["timeout"] = timeout
        return link_client.EnrolledDevice(device_token=CANARY_TOKEN, identity=identity)

    async def fake_dial(
        enrolled: link_client.EnrolledDevice,
        _deadline: float,
    ) -> FakeSession:
        captured["enrolled"] = enrolled
        return FakeSession(identity=enrolled.identity)

    monkeypatch.setattr(
        probe, "_build_ephemeral_identity", build_identity_with_private_canary
    )
    monkeypatch.setattr(link_client.Client, "enroll_device_async", fake_enroll)
    monkeypatch.setattr(probe, "_dial_with_deadline", fake_dial)
    caplog.set_level("DEBUG")

    lease, outcome = await probe.prove_spl_relay_tunnel(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert lease is not None
    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    assert outcome["checks"] == probe.SPL_CHECKS
    assert outcome["reason"] is None
    assert isinstance(outcome["duration_ms"], int) and outcome["duration_ms"] >= 0
    identity = captured["identity"]
    assert cert_fingerprint(identity.client_cert_pem) == identity.fingerprint
    assert (
        probe._attestation_device_fp(identity.home_attestation) == identity.fingerprint
    )
    assert identity.private_key_pem == CANARY_PRIVATE
    secrets = (*CANARIES, identity.fingerprint)
    auth_payload = _load_auth(journal)
    attempt_entry = next(
        item for item in auth_payload if item["device_label"].startswith("sandbox-spl-")
    )
    assert attempt_entry["fingerprint"] == identity.fingerprint
    assert _canonical(_without_attempt(auth_payload)) == _canonical(unrelated)
    outcome_json = json.dumps(outcome, sort_keys=True)
    for secret in secrets:
        assert secret not in outcome_json
        assert secret not in repr(lease)
        assert secret not in caplog.text
        assert secret not in " ".join(sys.argv)

    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError) as excinfo:
            operation(lease)
        assert excinfo.value.__cause__ is None
    receipt = lease._receipt
    assert receipt is not None
    for secret in secrets:
        assert secret not in repr(receipt)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError) as excinfo:
            operation(receipt)
        assert excinfo.value.__cause__ is None

    await lease.close()
    assert lease.is_closed
    assert _canonical(_load_auth(journal)) == _canonical(unrelated)
    names = " ".join(
        [task.get_name() for task in asyncio.all_tasks()]
        + [thread.name for thread in threading.enumerate()]
    )
    for secret in secrets:
        assert secret not in names
    _assert_canaries_absent_from_journal(journal, secrets)


@pytest.mark.asyncio
async def test_lease_close_succeeds_after_touch_last_seen_normalizes_unrelated_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, _attempt = _prepared_journal(tmp_path, monkeypatch)
    _seed_unrelated_authorizations(journal)
    store = probe.AuthorizedClients(journal / "link" / "authorized_clients.json")
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:" + "a" * 64,
        device_label="sandbox-spl-normalized",
        instance_id="instance",
    )
    lease = probe.RelayTunnelLease(
        session=FakeSession(),
        store=store,
        receipt=receipt,
    )

    assert store.touch_last_seen(receipt.fingerprint)
    normalized_before_close = _without_attempt(_load_auth(journal))
    assert all(
        "unmodeled" not in item and "future" not in item
        for item in normalized_before_close
    )

    await lease.close()

    after_close = _load_auth(journal)
    assert _canonical(after_close) == _canonical(normalized_before_close)
    assert {item["fingerprint"] for item in after_close} == {"sha256:one", "sha256:two"}
    assert lease.is_closed


@pytest.mark.asyncio
async def test_pin_override_during_readiness_refuses_before_write_or_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    calls: list[str] = []

    class MutatingObserver:
        def __enter__(self) -> MutatingObserver:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def wait_snapshot(self, *, deadline: float | None = None):
            monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://late.example")
            return spl_readiness.SplReadinessSnapshot(
                supervisor_ref="supervisor",
                spl_pid=1,
                spl_ref="spl",
                convey_pid=2,
                convey_ref="convey",
                spl_connection_state="connected",
                listen_generation=1,
                link_health_observed_at_monotonic=1.0,
                observed_relay_origin="https://link.solstone.app",
                secure_listener_bound_accepting=True,
                supervisor_observed_at_monotonic=1.0,
            )

        def reverify_before_authorization(self, _snapshot) -> None:
            return None

    async def enroll(*_args: object, **_kwargs: object) -> object:
        calls.append("enroll")
        raise AssertionError("enrollment must not be reached")

    async def dial(*_args: object, **_kwargs: object) -> object:
        calls.append("dial")
        raise AssertionError("dial must not be reached")

    monkeypatch.setattr(
        probe.spl_readiness,
        "open_spl_readiness_observer",
        lambda _journal: MutatingObserver(),
    )
    monkeypatch.setattr(link_client.Client, "enroll_device_async", enroll)
    monkeypatch.setattr(probe, "_dial_with_deadline", dial)

    lease, outcome = await probe.prove_spl_relay_tunnel(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert lease is None
    assert outcome["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    assert outcome["checks"] == ()
    assert calls == []
    assert not (journal / "link" / "authorized_clients.json").exists()


@pytest.mark.asyncio
async def test_env_pin_refuses_before_write_or_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://elsewhere.test")
    calls: list[str] = []
    monkeypatch.setattr(
        link_client.Client,
        "enroll_device_async",
        lambda *args, **kwargs: calls.append("enroll"),
    )
    monkeypatch.setattr(
        probe, "_dial_with_deadline", lambda *args, **kwargs: calls.append("dial")
    )

    lease, outcome = await probe.prove_spl_relay_tunnel(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert lease is None
    assert outcome["state"] == probe_contract.PROOF_STATE_FAILED
    assert outcome["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    assert outcome["checks"] == ()
    assert calls == []
    assert not (journal / "link" / "authorized_clients.json").exists()


@pytest.mark.asyncio
async def test_config_pin_refuses_before_write_or_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    write_json(
        journal / "config" / "journal.json",
        {"link": {"relay_url": "https://link.solstone.app"}},
    )
    calls: list[str] = []
    monkeypatch.setattr(
        link_client.Client,
        "enroll_device_async",
        lambda *args, **kwargs: calls.append("enroll"),
    )
    monkeypatch.setattr(
        probe, "_dial_with_deadline", lambda *args, **kwargs: calls.append("dial")
    )

    lease, outcome = await probe.prove_spl_relay_tunnel(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert lease is None
    assert outcome["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    assert outcome["checks"] == ()
    assert calls == []
    assert not (journal / "link" / "authorized_clients.json").exists()


@pytest.mark.asyncio
async def test_strict_store_rejection_at_create_maps_capability_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    auth_path = journal / "link" / "authorized_clients.json"
    write_json(
        auth_path,
        [
            {"fingerprint": "sha256:dup", "device_label": "one"},
            {"fingerprint": "sha256:dup", "device_label": "two"},
        ],
    )
    before = auth_path.read_text("utf-8")
    _install_fake_readiness(monkeypatch)

    lease, outcome = await probe.prove_spl_relay_tunnel(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert lease is None
    assert outcome["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    assert outcome["checks"] == ()
    assert auth_path.read_text("utf-8") == before


@pytest.mark.asyncio
async def test_work_deadline_is_exactly_sixty_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    deadlines: list[float | None] = []
    monkeypatch.setattr(probe.time, "monotonic", lambda: 100.0)

    class RefusingObserver:
        def __enter__(self) -> RefusingObserver:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def wait_snapshot(self, *, deadline: float | None = None) -> object:
            deadlines.append(deadline)
            raise spl_readiness.SplReadinessError("readiness_window_timeout")

    monkeypatch.setattr(
        probe.spl_readiness,
        "open_spl_readiness_observer",
        lambda _journal: RefusingObserver(),
    )

    lease, outcome = await probe.prove_spl_relay_tunnel(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert lease is None
    assert outcome["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    assert deadlines == [100.0 + probe.WORK_DEADLINE_SECONDS]


@pytest.mark.asyncio
async def test_enrollment_deadline_cap_clamp_and_boundary_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _dummy_identity()
    waits: list[float] = []
    enroll_timeouts: list[float] = []
    now = [100.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: now[0])

    async def fake_wait_for(coro, *, timeout: float):
        waits.append(timeout)
        return await coro

    async def fake_enroll(
        _relay_url: str,
        identity_arg: link_client.ClientIdentity,
        *,
        timeout: float = 30.0,
    ) -> link_client.EnrolledDevice:
        enroll_timeouts.append(timeout)
        return link_client.EnrolledDevice(device_token="token", identity=identity_arg)

    monkeypatch.setattr(probe.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(link_client.Client, "enroll_device_async", fake_enroll)

    await probe._enroll_with_deadline(identity, now[0] + 120.0)
    await probe._enroll_with_deadline(identity, now[0] + 12.5)
    now[0] = 130.0
    with pytest.raises(probe.SplRelayTunnelError) as excinfo:
        await probe._enroll_with_deadline(identity, now[0])

    assert waits == [probe.ENROLLMENT_DEADLINE_SECONDS, 12.5]
    assert enroll_timeouts == [probe.ENROLLMENT_DEADLINE_SECONDS, 12.5]
    assert excinfo.value.reason == probe_contract.REASON_DEADLINE_EXCEEDED


@pytest.mark.asyncio
async def test_cleanup_shield_is_independent_after_work_budget_expired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, _attempt = _prepared_journal(tmp_path, monkeypatch)
    store = probe.AuthorizedClients(journal / "link" / "authorized_clients.json")
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:" + "c" * 64,
        device_label="sandbox-spl-cleanup",
        instance_id="instance",
    )
    session = FakeSession()
    waits: list[float] = []
    monkeypatch.setattr(probe.time, "monotonic", lambda: 10_000.0)

    async def fake_wait_for(coro, *, timeout: float):
        waits.append(timeout)
        return await coro

    monkeypatch.setattr(probe.asyncio, "wait_for", fake_wait_for)

    assert await probe._cleanup_with_shield(
        session=session,
        store=store,
        receipt=receipt,
    )

    assert waits == [probe.CLEANUP_SHIELD_SECONDS]
    assert session.closed is True
    assert _load_auth(journal) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "setup", "reason", "checks"),
    [
        (
            "enrollment_http_error",
            lambda monkeypatch, tracker: _patch_enroll_error(
                monkeypatch,
                RuntimeError("POST https://link.solstone.app failed: HTTP 403"),
            ),
            probe_contract.REASON_REMOTE_REJECTED,
            tuple(),
        ),
        (
            "malformed_enrollment_response",
            lambda monkeypatch, tracker: _patch_enroll_error(
                monkeypatch, RuntimeError("missing string field device_token")
            ),
            probe_contract.REASON_RESPONSE_INVALID,
            tuple(),
        ),
        (
            "relay_upgrade_refusal",
            lambda monkeypatch, tracker: _patch_enroll_success_and_connect_error(
                monkeypatch, tracker, RuntimeError("HTTP 403 upgrade refused")
            ),
            probe_contract.REASON_REMOTE_REJECTED,
            tuple(probe.SPL_CHECKS[:1]),
        ),
        (
            "malformed_relay_upgrade",
            lambda monkeypatch, tracker: _patch_enroll_success_and_connect_error(
                monkeypatch, tracker, RuntimeError("malformed relay upgrade")
            ),
            probe_contract.REASON_RESPONSE_INVALID,
            tuple(probe.SPL_CHECKS[:1]),
        ),
        (
            "inner_tls_authorization_refusal",
            lambda monkeypatch, tracker: _patch_open_session_error(
                monkeypatch, tracker, RuntimeError("certificate rejected")
            ),
            probe_contract.REASON_REMOTE_REJECTED,
            tuple(probe.SPL_CHECKS[:2]),
        ),
        (
            "malformed_mux_success",
            lambda monkeypatch, tracker: _patch_open_session_result(
                monkeypatch, tracker, FakeSession(is_alive=False)
            ),
            probe_contract.REASON_RESPONSE_INVALID,
            tuple(probe.SPL_CHECKS[:2]),
        ),
        (
            "unexpected_local_failure",
            lambda monkeypatch, tracker: monkeypatch.setattr(
                probe,
                "_build_ephemeral_identity",
                lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
            ),
            probe_contract.REASON_INTERNAL_ERROR,
            tuple(),
        ),
    ],
)
async def test_failure_table_rows_and_non_transferred_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    setup,
    reason: str,
    checks: tuple[str, ...],
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    _install_fake_readiness(monkeypatch)
    tracker: dict[str, Any] = {"websockets": []}
    setup(monkeypatch, tracker)

    lease, outcome = await _assert_no_task_or_thread_leak(
        probe.prove_spl_relay_tunnel(
            journal,
            attempt_dir=attempt,
            cancel_requested=lambda: False,
        )
    )

    assert lease is None, name
    assert outcome["state"] == probe_contract.PROOF_STATE_FAILED
    assert outcome["reason"] == reason
    assert outcome["checks"] == checks
    assert _load_auth(journal) == []
    for ws in tracker["websockets"]:
        assert ws.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "setup", "checks"),
    [
        (
            "enrollment_timeout",
            lambda monkeypatch, tracker: _patch_enroll_error(
                monkeypatch, _NamedTimeoutException("request timed out")
            ),
            tuple(),
        ),
        (
            "relay_connect_timeout",
            lambda monkeypatch, tracker: _patch_enroll_success_and_connect_error(
                monkeypatch, tracker, asyncio.TimeoutError()
            ),
            tuple(probe.SPL_CHECKS[:1]),
        ),
        (
            "inner_tls_timeout",
            lambda monkeypatch, tracker: _patch_open_session_error(
                monkeypatch, tracker, asyncio.TimeoutError()
            ),
            tuple(probe.SPL_CHECKS[:2]),
        ),
    ],
)
async def test_deadline_exceeded_failure_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    setup,
    checks: tuple[str, ...],
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    _install_fake_readiness(monkeypatch)
    tracker: dict[str, Any] = {"websockets": []}
    setup(monkeypatch, tracker)

    lease, outcome = await _assert_no_task_or_thread_leak(
        probe.prove_spl_relay_tunnel(
            journal,
            attempt_dir=attempt,
            cancel_requested=lambda: False,
        )
    )

    assert lease is None, name
    assert outcome["reason"] == probe_contract.REASON_DEADLINE_EXCEEDED
    assert outcome["checks"] == checks
    assert _load_auth(journal) == []
    for ws in tracker["websockets"]:
        assert ws.closed is True


@pytest.mark.asyncio
async def test_cancel_after_authorization_cleans_then_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    _install_fake_readiness(monkeypatch)

    async def cancel_enroll(
        *_args: object, **_kwargs: object
    ) -> link_client.EnrolledDevice:
        raise asyncio.CancelledError

    monkeypatch.setattr(link_client.Client, "enroll_device_async", cancel_enroll)

    with pytest.raises(asyncio.CancelledError):
        await probe.prove_spl_relay_tunnel(
            journal,
            attempt_dir=attempt,
            cancel_requested=lambda: False,
        )

    assert _load_auth(journal) == []


@pytest.mark.asyncio
async def test_cancel_during_cleanup_shield_reports_ambiguous_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    _install_fake_readiness(monkeypatch)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cancel_enroll(
        *_args: object, **_kwargs: object
    ) -> link_client.EnrolledDevice:
        raise asyncio.CancelledError

    async def ambiguous_cleanup(**_kwargs: object) -> bool:
        cleanup_started.set()
        await release_cleanup.wait()
        return False

    monkeypatch.setattr(link_client.Client, "enroll_device_async", cancel_enroll)
    monkeypatch.setattr(
        probe, "_cleanup_transport_and_authorization", ambiguous_cleanup
    )

    task = asyncio.create_task(
        probe.prove_spl_relay_tunnel(
            journal,
            attempt_dir=attempt,
            cancel_requested=lambda: False,
        )
    )
    await cleanup_started.wait()
    task.cancel()
    release_cleanup.set()

    lease, outcome = await task

    assert lease is None
    assert outcome["reason"] == probe_contract.REASON_CLEANUP_UNVERIFIED
    assert outcome["checks"] == ()


@pytest.mark.asyncio
async def test_cancel_before_authorization_raises_without_cleanup_or_store_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        probe,
        "_cleanup_with_shield",
        lambda **_kwargs: cleanup_calls.append("cleanup"),
    )

    with pytest.raises(asyncio.CancelledError):
        await probe.prove_spl_relay_tunnel(
            journal,
            attempt_dir=attempt,
            cancel_requested=lambda: True,
        )

    assert cleanup_calls == []
    assert not (journal / "link" / "authorized_clients.json").exists()


@pytest.mark.asyncio
async def test_lease_close_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, _attempt = _prepared_journal(tmp_path, monkeypatch)
    store = probe.AuthorizedClients(journal / "link" / "authorized_clients.json")
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:" + "b" * 64,
        device_label="sandbox-spl-retry",
        instance_id="instance",
    )
    session = FakeSession()
    lease = probe.RelayTunnelLease(session=session, store=store, receipt=receipt)
    calls = 0
    real_remove = store.remove_attempt_client_strict

    def flaky_remove(receipt_arg) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise probe.StrictAuthorizationError("forced")
        real_remove(receipt_arg)

    monkeypatch.setattr(store, "remove_attempt_client_strict", flaky_remove)

    with pytest.raises(probe.RelayTunnelCloseError) as excinfo:
        await lease.close()

    assert excinfo.value.reason == probe_contract.REASON_CLEANUP_UNVERIFIED
    assert not lease.is_closed
    await lease.close()
    assert lease.is_closed
    await lease.close()


@pytest.mark.asyncio
async def test_lease_lifecycle_consumer_not_called_closes_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, _attempt = _prepared_journal(tmp_path, monkeypatch)
    store = probe.AuthorizedClients(journal / "link" / "authorized_clients.json")
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:" + "f" * 64,
        device_label="sandbox-spl-not-called",
        instance_id="instance",
    )
    session = FakeSession()
    lease = probe.RelayTunnelLease(session=session, store=store, receipt=receipt)

    await lease.close()

    assert lease.is_closed
    assert session.closed is True
    assert _load_auth(journal) == []


@pytest.mark.asyncio
async def test_lease_lifecycle_consumer_raised_still_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, _attempt = _prepared_journal(tmp_path, monkeypatch)
    store = probe.AuthorizedClients(journal / "link" / "authorized_clients.json")
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:" + "d" * 64,
        device_label="sandbox-spl-raised",
        instance_id="instance",
    )
    session = FakeSession()
    lease = probe.RelayTunnelLease(session=session, store=store, receipt=receipt)

    with pytest.raises(ValueError, match="consumer failed"):
        async with lease:
            raise ValueError("consumer failed")

    assert lease.is_closed
    assert session.closed is True
    assert _load_auth(journal) == []


@pytest.mark.asyncio
async def test_lease_double_close_after_success_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, _attempt = _prepared_journal(tmp_path, monkeypatch)
    store = probe.AuthorizedClients(journal / "link" / "authorized_clients.json")
    receipt = store.add_attempt_client_strict(
        fingerprint="sha256:" + "e" * 64,
        device_label="sandbox-spl-double-close",
        instance_id="instance",
    )
    session = FakeSession()
    lease = probe.RelayTunnelLease(session=session, store=store, receipt=receipt)

    await lease.close()
    await lease.close()

    assert lease.is_closed
    assert session.close_calls == 1
    assert _load_auth(journal) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "exception", "cancellation"])
async def test_proof_log_filter_attaches_only_specific_logger_and_detaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    journal, attempt = _prepared_journal(tmp_path, monkeypatch)
    _install_fake_readiness(monkeypatch)
    logger = logging.getLogger(link_client.LOG.name)
    probe_logger = logging.getLogger(probe.__name__)
    before_filters = list(logger.filters)
    seen_filters: list[list[logging.Filter]] = []

    async def fake_enroll(
        _relay_url: str,
        identity: link_client.ClientIdentity,
        *,
        timeout: float = 30.0,
    ) -> link_client.EnrolledDevice:
        seen_filters.append(list(logger.filters))
        assert probe_logger.filters == []
        if mode == "exception":
            raise RuntimeError("POST failed: HTTP 403")
        if mode == "cancellation":
            raise asyncio.CancelledError
        return link_client.EnrolledDevice(device_token=CANARY_TOKEN, identity=identity)

    async def fake_dial(
        _enrolled: link_client.EnrolledDevice,
        _deadline: float,
    ) -> FakeSession:
        return FakeSession()

    monkeypatch.setattr(link_client.Client, "enroll_device_async", fake_enroll)
    monkeypatch.setattr(probe, "_dial_with_deadline", fake_dial)

    if mode == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await probe.prove_spl_relay_tunnel(
                journal,
                attempt_dir=attempt,
                cancel_requested=lambda: False,
            )
    else:
        lease, _outcome = await probe.prove_spl_relay_tunnel(
            journal,
            attempt_dir=attempt,
            cancel_requested=lambda: False,
        )
        if lease is not None:
            await lease.close()

    assert seen_filters and len(seen_filters[0]) == len(before_filters) + 1
    assert logger.filters == before_filters
    assert probe_logger.filters == []

    caplog.set_level(logging.INFO, logger=link_client.LOG.name)
    link_client.LOG.info("client %s: enrolling device token", "sha256:normal")
    assert "sha256:normal" in caplog.text


@pytest.mark.asyncio
async def test_offline_real_inner_tls_and_mux_with_relay_boundary_doubled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await SecureListenerHarness.start(tmp_path, monkeypatch)
    try:
        import solstone.convey.root as convey_root
        import solstone.convey.secure_listener.runtime as secure_runtime

        monkeypatch.setattr(
            secure_runtime,
            "get_authorized_clients",
            lambda: harness.authorized,
        )
        monkeypatch.setattr(
            convey_root,
            "get_authorized_clients",
            lambda: harness.authorized,
        )
        journal = harness.journal
        _write_state_and_marker(journal, monkeypatch)
        attempt = write_attempt_dir(journal)
        _install_fake_readiness(monkeypatch)

        async def fake_enroll(
            _relay_url: str,
            identity: link_client.ClientIdentity,
            *,
            timeout: float = 30.0,
        ) -> link_client.EnrolledDevice:
            return link_client.EnrolledDevice(
                device_token="offline-token", identity=identity
            )

        class TcpBackedWebSocket:
            def __init__(
                self,
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                self._reader = reader
                self._writer = writer
                self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
                self._pump = asyncio.create_task(
                    self._pump_reader(),
                    name="spl-proof-test-ws-pump",
                )
                self.closed = False

            async def _pump_reader(self) -> None:
                try:
                    while True:
                        data = await self._reader.read(65536)
                        if not data:
                            break
                        self._inbound.put_nowait(data)
                finally:
                    self._inbound.put_nowait(None)

            async def send(self, data: bytes) -> None:
                self._writer.write(data)
                await self._writer.drain()

            async def recv(self) -> bytes:
                data = await self._inbound.get()
                if data is None:
                    raise ConnectionClosedOK(None, None)
                return data

            async def close(self) -> None:
                self.closed = True
                self._writer.close()
                with contextlib.suppress(Exception):
                    await self._writer.wait_closed()
                self._pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pump

        connect_calls: list[str] = []

        async def fake_connect(
            url: str, *, max_size: int | None = None
        ) -> TcpBackedWebSocket:
            connect_calls.append(url)
            assert max_size is None
            reader, writer = await asyncio.open_connection(harness.host, harness.port)
            return TcpBackedWebSocket(reader, writer)

        monkeypatch.setattr(link_client.Client, "enroll_device_async", fake_enroll)
        monkeypatch.setattr(probe.websockets, "connect", fake_connect)

        lease, outcome = await probe.prove_spl_relay_tunnel(
            journal,
            attempt_dir=attempt,
            cancel_requested=lambda: False,
        )

        assert lease is not None
        assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
        assert connect_calls == [
            "wss://link.solstone.app/session/dial?instance=instance&token=offline-token"
        ]
        status, _headers, body = await lease.request("GET", "/app/network/api/status")
        assert status == 200
        assert json.loads(body.decode("utf-8"))["posture"] == "spl"
        await lease.close()
    finally:
        await harness.close()


def _prepared_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    journal = sandbox_journal(tmp_path, monkeypatch, run_id=RUN_ID)
    _write_state_and_marker(journal, monkeypatch)
    generate_ca(journal / "link" / "ca")
    return journal, write_attempt_dir(journal)


def _write_state_and_marker(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    write_json(journal / "config" / "journal.json", {"link": {"posture": "spl"}})
    write_json(
        journal / "link" / "state.json",
        {"instance_id": "instance", "home_label": "home"},
    )


def _install_fake_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeObserver:
        def __enter__(self) -> FakeObserver:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def wait_snapshot(self, *, deadline: float | None = None):
            return spl_readiness.SplReadinessSnapshot(
                supervisor_ref="supervisor",
                spl_pid=1,
                spl_ref="spl",
                convey_pid=2,
                convey_ref="convey",
                spl_connection_state="connected",
                listen_generation=1,
                link_health_observed_at_monotonic=1.0,
                observed_relay_origin="https://link.solstone.app",
                secure_listener_bound_accepting=True,
                supervisor_observed_at_monotonic=1.0,
            )

        def reverify_before_authorization(self, _snapshot) -> None:
            return None

    monkeypatch.setattr(
        probe.spl_readiness,
        "open_spl_readiness_observer",
        lambda _journal: FakeObserver(),
    )


def _install_unreachable_path_canaries(monkeypatch: pytest.MonkeyPatch) -> None:
    from solstone.think.link import dialer, interface_watcher

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unreachable path entered")

    monkeypatch.setattr(link_client.Client, "dial_direct", fail)
    monkeypatch.setattr(dialer, "open_tunnel", fail)
    monkeypatch.setattr(dialer, "_dial_relay", fail)
    monkeypatch.setattr(dialer, "TunnelClient", fail)
    monkeypatch.setattr(interface_watcher.InterfaceWatcher, "start", fail)


class FakeSession:
    def __init__(
        self,
        *,
        is_alive: bool = True,
        identity: link_client.ClientIdentity | None = None,
    ) -> None:
        self.is_alive = is_alive
        self.closed = False
        self.close_calls = 0
        self.websocket: FakeWebSocket | None = None
        self._identity = identity

    async def request(
        self,
        _method: str,
        _path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | link_client.BodySource = b"",
    ) -> tuple[int, dict[str, str], bytes]:
        return 200, {}, b"{}"

    async def stream_request(
        self,
        _method: str,
        _path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | link_client.BodySource = b"",
    ) -> tuple[int, dict[str, str], bytes, object]:
        return 200, {}, b"{}", object()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self.websocket is not None:
            await self.websocket.close()


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _NamedTimeoutException(Exception):
    pass


_NamedTimeoutException.__name__ = "TimeoutException"


def _dummy_identity() -> link_client.ClientIdentity:
    return link_client.ClientIdentity(
        private_key_pem="private",
        client_cert_pem="cert",
        ca_chain_pem="ca",
        fingerprint="sha256:dummy",
        home_instance_id="instance",
        home_label="home",
        home_attestation="attestation",
        local_endpoints=(),
    )


async def _assert_no_task_or_thread_leak(coro):
    current = asyncio.current_task()
    before_tasks = {
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    }
    before_threads = {thread.ident for thread in threading.enumerate()}
    result = await coro
    await asyncio.sleep(0)
    after_tasks = {
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    }
    after_threads = {thread.ident for thread in threading.enumerate()}
    assert after_tasks <= before_tasks
    assert after_threads == before_threads
    return result


def _patch_enroll_error(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
) -> None:
    async def fake_enroll(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(link_client.Client, "enroll_device_async", fake_enroll)


def _patch_enroll_success_and_connect_error(
    monkeypatch: pytest.MonkeyPatch,
    tracker: dict[str, Any],
    exc: Exception,
) -> None:
    _patch_enroll_success(monkeypatch)

    async def fake_connect(*_args: object, **_kwargs: object) -> object:
        tracker.setdefault("connect_attempts", 0)
        tracker["connect_attempts"] += 1
        raise exc

    monkeypatch.setattr(probe.websockets, "connect", fake_connect)


def _patch_open_session_error(
    monkeypatch: pytest.MonkeyPatch,
    tracker: dict[str, Any],
    exc: Exception,
) -> None:
    _patch_enroll_success(monkeypatch)
    ws = FakeWebSocket()
    tracker.setdefault("websockets", []).append(ws)

    async def fake_connect(*_args: object, **_kwargs: object) -> FakeWebSocket:
        return ws

    async def fake_open(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(probe.websockets, "connect", fake_connect)
    monkeypatch.setattr(link_client, "_open_tunnel_session", fake_open)


def _patch_open_session_result(
    monkeypatch: pytest.MonkeyPatch,
    tracker: dict[str, Any],
    session: FakeSession,
) -> None:
    _patch_enroll_success(monkeypatch)
    ws = FakeWebSocket()
    tracker.setdefault("websockets", []).append(ws)
    session.websocket = ws
    tracker["session"] = session

    async def fake_connect(*_args: object, **_kwargs: object) -> FakeWebSocket:
        return ws

    async def fake_open(*_args: object, **_kwargs: object) -> FakeSession:
        return session

    monkeypatch.setattr(probe.websockets, "connect", fake_connect)
    monkeypatch.setattr(link_client, "_open_tunnel_session", fake_open)


def _patch_enroll_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_enroll(
        _relay_url: str,
        identity: link_client.ClientIdentity,
        *,
        timeout: float = 30.0,
    ) -> link_client.EnrolledDevice:
        return link_client.EnrolledDevice(device_token="token", identity=identity)

    monkeypatch.setattr(link_client.Client, "enroll_device_async", fake_enroll)


def _seed_unrelated_authorizations(journal: Path) -> list[dict[str, object]]:
    payload = [
        {
            "fingerprint": "sha256:one",
            "device_label": "one",
            "paired_at": "2026-01-01T00:00:00Z",
            "instance_id": "instance",
            "role": "",
            "unmodeled": {"keep": True},
        },
        {
            "fingerprint": "sha256:two",
            "device_label": "two",
            "paired_at": "2026-01-01T00:00:00Z",
            "instance_id": "instance",
            "role": "",
            "future": "value",
        },
    ]
    write_json(journal / "link" / "authorized_clients.json", payload)
    return payload


def _load_auth(journal: Path) -> list[dict[str, object]]:
    path = journal / "link" / "authorized_clients.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text("utf-8"))
    assert isinstance(payload, list)
    return payload


def _without_attempt(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        item
        for item in items
        if not str(item.get("device_label", "")).startswith("sandbox-spl-")
    ]


def _canonical(items: list[dict[str, object]]) -> tuple[str, ...]:
    return tuple(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in items
    )


def _assert_canaries_absent_from_journal(
    journal: Path,
    canaries: tuple[str, ...] = CANARIES,
) -> None:
    for path in journal.rglob("*"):
        for canary in canaries:
            assert canary not in path.name
        if path.is_file():
            data = path.read_text("utf-8", errors="ignore")
            for canary in canaries:
                assert canary not in data
