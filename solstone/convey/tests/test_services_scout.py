# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import Mock

import pytest

import solstone.convey.services_scout as services_scout
from solstone.think.journal_config import write_journal_config
from solstone.think.services import portal_client
from solstone.think.services.portal_client import PollOutcome
from solstone.think.services.scout import (
    JournalNotInitializedError,
    provision_scout_handoff,
)

BLOCKED_COPY_TERMS = [
    "signed" + " in",
    "signing" + " in",
    "logged" + " in",
    "log" + " in",
    "sign" + " in",
    "your " + "account",
    "account " + "settings",
    "auth" + "enticate",
    "link" + "ed",
]
BLOCKED_COPY_RE = re.compile(
    "|".join(re.escape(term) for term in BLOCKED_COPY_TERMS),
    re.IGNORECASE,
)
GOOGLE_API_KEY_FIELD = "google" + "_api_key"
DISPATCH_TOKEN_FIELD = "dispatch" + "_token"


@pytest.fixture(autouse=True)
def scout_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[Mock]:
    with services_scout._REGISTRY_LOCK:
        services_scout._REGISTRY.clear()
    open_mock = Mock(return_value=True)
    monkeypatch.setattr(services_scout.webbrowser, "open", open_mock)
    yield open_mock
    with services_scout._REGISTRY_LOCK:
        services_scout._REGISTRY.clear()


def _payload(suffix: str = "one") -> dict[str, str]:
    return {
        GOOGLE_API_KEY_FIELD: f"google-{suffix}",
        DISPATCH_TOKEN_FIELD: f"dispatch-{suffix}",
        "account_id": f"acct-{suffix}",
        "created_at": "2026-05-24T00:00:00Z",
    }


def _read_config(journal) -> dict[str, Any]:
    return json.loads((journal / "config" / "journal.json").read_text())


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def _terminal_event(nonce_id: str) -> tuple[str, dict[str, Any]] | None:
    with services_scout._REGISTRY_LOCK:
        entry = services_scout._REGISTRY.get(nonce_id)
        return entry.terminal_event if entry is not None else None


def _next_chunk(response) -> str:
    chunk = next(iter(response.response))
    return chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)


def _parse_event(chunk: str) -> tuple[str, dict[str, Any]]:
    event_name = ""
    data = None
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event_name = line[len("event: ") :]
        if line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    if not event_name or data is None:
        raise AssertionError(f"No SSE event found in chunk: {chunk!r}")
    return event_name, data


def _next_event(response) -> tuple[str, dict[str, Any]]:
    return _parse_event(_next_chunk(response))


def _start(client):
    return client.post("/init/services/scout/start")


def _status(client, nonce_id: str):
    return client.get(
        f"/init/services/scout/status?nonce_id={nonce_id}",
        buffered=False,
    )


def _install_success_poll(monkeypatch: pytest.MonkeyPatch, suffix: str = "one") -> None:
    monkeypatch.setattr(
        portal_client,
        "poll_handoff_once",
        lambda *_args, **_kwargs: PollOutcome(kind="success", payload=_payload(suffix)),
    )


def test_start_returns_202_and_spawns_one_entry(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
    scout_registry: Mock,
) -> None:
    env = convey_env_setup_pending()
    monkeypatch.setattr(portal_client, "portal_base_url", lambda: "http://portal.test")
    _install_success_poll(monkeypatch)

    response = _start(env.client)

    assert response.status_code == 202
    data = response.get_json()
    assert set(data) == {"nonce_id", "subscribe_url", "portal_url"}
    assert data["subscribe_url"] == (
        f"/init/services/scout/status?nonce_id={data['nonce_id']}"
    )
    assert data["portal_url"].startswith("http://portal.test/enable/scout?nonce=")
    _wait_until(lambda: scout_registry.call_count == 1)
    assert scout_registry.call_args.args == (data["portal_url"],)
    _wait_until(lambda: _terminal_event(data["nonce_id"]) is not None)
    with services_scout._REGISTRY_LOCK:
        assert len(services_scout._REGISTRY) == 1


def test_start_returns_409_already_enabled(convey_env_setup_pending) -> None:
    env = convey_env_setup_pending()
    config = _read_config(env.journal)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "existing"
    config.setdefault("services", {})["scout"] = {"account_id": "acct"}
    write_journal_config(config)

    response = _start(env.client)

    assert response.status_code == 409
    assert response.get_json() == {"error": "already_enabled"}


def test_start_returns_409_manual_key_present(convey_env_setup_pending) -> None:
    env = convey_env_setup_pending()
    config = _read_config(env.journal)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual"
    config.pop("services", None)
    write_journal_config(config)

    response = _start(env.client)

    assert response.status_code == 409
    assert response.get_json() == {"error": "manual_key_present"}


def test_start_returns_404_after_setup_complete(convey_env) -> None:
    env = convey_env()

    response = _start(env.client)

    assert response.status_code == 404


def test_double_post_is_idempotent(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
    scout_registry: Mock,
) -> None:
    env = convey_env_setup_pending()
    release_poll = threading.Event()
    poll_entered = threading.Event()
    poll_calls = 0

    def fake_poll(*_args, **_kwargs):
        nonlocal poll_calls
        poll_calls += 1
        poll_entered.set()
        release_poll.wait(2)
        return PollOutcome(kind="success", payload=_payload())

    monkeypatch.setattr(portal_client, "poll_handoff_once", fake_poll)

    first = _start(env.client)
    second = _start(env.client)

    try:
        assert first.status_code == 202
        assert second.status_code == 202
        assert second.get_json() == first.get_json()
        _wait_until(lambda: scout_registry.call_count == 1)
        assert poll_entered.wait(1)
        assert poll_calls == 1
    finally:
        release_poll.set()
    nonce_id = first.get_json()["nonce_id"]
    _wait_until(lambda: _terminal_event(nonce_id) is not None)


def test_status_sse_happy_path(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()
    _install_success_poll(monkeypatch)
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]

    response = _status(env.client, nonce_id)
    try:
        assert response.status_code == 200
        assert response.content_type.startswith("text/event-stream")
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["X-Accel-Buffering"] == "no"
        name, data = _next_event(response)
        assert name == "subscribed"
        assert data["nonce_id"] == nonce_id
        assert "browser_open_attempted" in data
        assert "browser_open_succeeded" in data
        name, data = _next_event(response)
        assert name == "scout-enabled"
        assert data == {"account_id": "acct-one"}
        with pytest.raises(StopIteration):
            next(iter(response.response))
    finally:
        response.close()


def test_status_unknown_nonce_returns_404(convey_env_setup_pending) -> None:
    env = convey_env_setup_pending()

    response = _status(env.client, "missing")

    assert response.status_code == 404


def test_status_replays_terminal_event_for_late_subscriber(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()
    _install_success_poll(monkeypatch, "late")
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]
    _wait_until(lambda: _terminal_event(nonce_id) is not None)

    response = _status(env.client, nonce_id)
    try:
        assert _next_event(response)[0] == "subscribed"
        assert _next_event(response) == ("scout-enabled", {"account_id": "acct-late"})
        with pytest.raises(StopIteration):
            next(iter(response.response))
    finally:
        response.close()


def test_status_after_grace_returns_404(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()
    _install_success_poll(monkeypatch)
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]
    _wait_until(lambda: _terminal_event(nonce_id) is not None)
    with services_scout._REGISTRY_LOCK:
        entry = services_scout._REGISTRY[nonce_id]
        entry.cleanup_at_monotonic = -1

    response = _status(env.client, nonce_id)

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (
            PollOutcome(kind="failed", reason="consent_link_expired"),
            "consent_link_expired",
        ),
        (PollOutcome(kind="failed", reason="nonce_invalid"), "nonce_invalid"),
        (
            PollOutcome(
                kind="failed",
                reason="tls_verification_failed",
                detail="bad cert",
            ),
            "tls_verification_failed",
        ),
        (
            PollOutcome(kind="failed", reason="unexpected_payload", detail="bad json"),
            "unexpected_payload",
        ),
    ],
)
def test_status_failed_poll_mappings(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
    outcome: PollOutcome,
    reason: str,
) -> None:
    env = convey_env_setup_pending()
    monkeypatch.setattr(
        portal_client,
        "poll_handoff_once",
        lambda *_args, **_kwargs: outcome,
    )
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]

    response = _status(env.client, nonce_id)
    try:
        assert _next_event(response)[0] == "subscribed"
        name, data = _next_event(response)
        assert name == "failed"
        assert data["reason"] == reason
    finally:
        response.close()


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (ValueError("missing field"), "unexpected_payload"),
        (JournalNotInitializedError("missing config"), "journal_not_initialized"),
        (OSError("disk full"), "write_failed"),
    ],
)
def test_status_failed_provision_mappings(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    reason: str,
) -> None:
    env = convey_env_setup_pending()
    _install_success_poll(monkeypatch)

    def fail_provision(_payload):
        raise exc

    monkeypatch.setattr(services_scout, "provision_scout_handoff", fail_provision)
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]

    response = _status(env.client, nonce_id)
    try:
        assert _next_event(response)[0] == "subscribed"
        name, data = _next_event(response)
        assert name == "failed"
        assert data["reason"] == reason
    finally:
        response.close()


def test_status_failed_internal_error(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()

    def raise_internal(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(portal_client, "poll_handoff_once", raise_internal)
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]

    response = _status(env.client, nonce_id)
    try:
        assert _next_event(response)[0] == "subscribed"
        name, data = _next_event(response)
        assert name == "failed"
        assert data["reason"] == "internal_error"
    finally:
        response.close()


def test_transient_portal_unreachable_retries_to_success(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()
    outcomes = [
        PollOutcome(
            kind="failed",
            reason="portal_unreachable",
            detail="temporary",
        ),
        PollOutcome(kind="success", payload=_payload("retry")),
    ]

    def fake_poll(*_args, **_kwargs):
        return outcomes.pop(0)

    monkeypatch.setattr(portal_client, "poll_handoff_once", fake_poll)
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]

    response = _status(env.client, nonce_id)
    try:
        assert _next_event(response)[0] == "subscribed"
        name, data = _next_event(response)
        assert name == "scout-enabled"
        assert data == {"account_id": "acct-retry"}
    finally:
        response.close()


def test_portal_unreachable_paces_retries(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()
    backoff = 0.05
    sleep_mock = Mock()
    outcomes = [
        PollOutcome(
            kind="failed",
            reason="portal_unreachable",
            detail="connection refused",
        ),
        PollOutcome(kind="success", payload=_payload("paced")),
    ]
    poll_calls: list[int] = []

    def fake_poll(*_args, **_kwargs):
        poll_calls.append(len(poll_calls) + 1)
        return outcomes.pop(0)

    monkeypatch.setattr(services_scout, "TRANSIENT_RETRY_BACKOFF_SECONDS", backoff)
    monkeypatch.setattr(services_scout.time, "sleep", sleep_mock)
    monkeypatch.setattr(portal_client, "poll_handoff_once", fake_poll)

    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]

    response = _status(env.client, nonce_id)
    try:
        assert _next_event(response)[0] == "subscribed"
        name, data = _next_event(response)
        assert name == "scout-enabled"
        assert data == {"account_id": "acct-paced"}
    finally:
        response.close()

    assert poll_calls == [1, 2]
    sleep_mock.assert_called_once_with(backoff)
    config = _read_config(env.journal)
    assert config["services"]["scout"]["account_id"] == "acct-paced"


def test_timeout_event(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()
    monkeypatch.setattr(services_scout, "WALL_CLOCK_BUDGET_SECONDS", 0)
    monkeypatch.setattr(
        portal_client,
        "poll_handoff_once",
        lambda *_args, **_kwargs: pytest.fail("poll should not run after timeout"),
    )
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]

    response = _status(env.client, nonce_id)
    try:
        assert _next_event(response)[0] == "subscribed"
        name, data = _next_event(response)
        assert name == "timeout"
        assert isinstance(data["elapsed_ms"], int)
    finally:
        response.close()


def test_direct_provision_restart_recovery(convey_env_setup_pending) -> None:
    env = convey_env_setup_pending()
    provision_scout_handoff(_payload("direct"))

    response = _start(env.client)

    assert response.status_code == 409
    assert response.get_json() == {"error": "already_enabled"}


def test_services_portal_url_env_override(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
    scout_registry: Mock,
) -> None:
    env = convey_env_setup_pending()
    monkeypatch.setenv("SERVICES_PORTAL_URL", "http://test.example")
    seen_poll: dict[str, str] = {}

    def fake_poll(base_url: str, nonce: str, **_kwargs):
        seen_poll["base_url"] = base_url
        seen_poll["nonce"] = nonce
        return PollOutcome(kind="success", payload=_payload())

    monkeypatch.setattr(portal_client, "poll_handoff_once", fake_poll)

    response = _start(env.client)

    assert response.status_code == 202
    data = response.get_json()
    assert data["portal_url"].startswith("http://test.example/enable/scout?nonce=")
    _wait_until(lambda: _terminal_event(data["nonce_id"]) is not None)
    assert scout_registry.call_args.args == (data["portal_url"],)
    assert seen_poll["base_url"] == "http://test.example"
    assert data["portal_url"].endswith(f"?nonce={seen_poll['nonce']}")


def test_response_copy_avoids_blocked_brand_terms(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()
    _install_success_poll(monkeypatch, "brand")
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]
    response = _status(env.client, nonce_id)
    chunks = [start_response.get_data(as_text=True)]
    try:
        chunks.append(_next_chunk(response))
        chunks.append(_next_chunk(response))
    finally:
        response.close()

    combined = "".join(chunks)
    assert not BLOCKED_COPY_RE.search(combined)


def test_responses_do_not_leak_handoff_secrets(
    convey_env_setup_pending,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = convey_env_setup_pending()
    payload = _payload("secret")
    payload[GOOGLE_API_KEY_FIELD] = "SENTINEL-GAPI"
    payload[DISPATCH_TOKEN_FIELD] = "SENTINEL-DT"
    monkeypatch.setattr(
        portal_client,
        "poll_handoff_once",
        lambda *_args, **_kwargs: PollOutcome(kind="success", payload=payload),
    )
    start_response = _start(env.client)
    nonce_id = start_response.get_json()["nonce_id"]
    response = _status(env.client, nonce_id)
    chunks = [start_response.get_data(as_text=True)]
    try:
        chunks.append(_next_chunk(response))
        chunks.append(_next_chunk(response))
    finally:
        response.close()

    combined = "".join(chunks)
    assert "SENTINEL-GAPI" not in combined
    assert "SENTINEL-DT" not in combined
