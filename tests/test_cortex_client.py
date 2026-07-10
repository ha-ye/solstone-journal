# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for cortex_client module with Callosum."""

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from solstone.think.callosum import CallosumConnection, CallosumServer
from solstone.think.cortex_client import (
    CortexNotClaimed,
    CortexSpawnUnavailable,
    cortex_request,
    cortex_uses,
    get_use_end_state,
    get_use_log_status,
    read_use_provider_model,
    read_use_provider_model_reason,
    wait_for_uses,
)
from solstone.think.models import GPT_5
from solstone.think.utils import now_ms


@pytest.fixture
def callosum_server(monkeypatch):
    """Start a Callosum server for testing.

    Uses a short temp path in /tmp to avoid Unix socket path length limits
    (~104 chars on macOS). pytest's tmp_path creates paths that are too long.
    """
    # Create short temp dir to avoid Unix socket path length limits
    tmp_dir = tempfile.mkdtemp(dir="/tmp", prefix="callosum_")
    tmp_path = Path(tmp_dir)

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    (tmp_path / "talents").mkdir(parents=True, exist_ok=True)

    server = CallosumServer()
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()

    # Wait for server to be ready
    socket_path = tmp_path / "health" / "callosum.sock"
    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)
    else:
        pytest.fail("Callosum server did not start in time")

    yield tmp_path

    server.stop()
    server_thread.join(timeout=2)
    shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture
def callosum_listener(callosum_server):
    """Provide a CallosumConnection listener that collects received messages.

    Yields (messages, listener) where messages is a list that accumulates
    all broadcast messages received during the test.
    """
    messages = []

    def callback(msg):
        messages.append(msg)

    listener = CallosumConnection()
    listener.start(callback=callback)
    time.sleep(0.1)  # Allow connection to establish

    yield messages

    listener.stop()


def test_cortex_request_broadcasts_to_callosum(callosum_listener, monkeypatch):
    """Test that cortex_request broadcasts request to Callosum."""
    messages = callosum_listener
    monkeypatch.setattr(
        "solstone.think.cortex_client.get_use_log_status", lambda uid: "running"
    )

    # Create a request
    use_id = cortex_request(
        prompt="Test prompt",
        name="chat",
        provider="openai",
        config={"model": GPT_5},
    )

    time.sleep(0.2)

    # Verify broadcast was received
    assert len(messages) == 1
    msg = messages[0]
    assert msg["tract"] == "cortex"
    assert msg["event"] == "request"
    assert msg["prompt"] == "Test prompt"
    assert msg["name"] == "chat"
    assert msg["provider"] == "openai"
    assert msg["model"] == GPT_5
    assert msg["use_id"] == use_id
    assert "ts" in msg


def test_cortex_request_returns_agent_id(callosum_server, monkeypatch):
    """Test that cortex_request returns use_id string."""
    _ = callosum_server  # Needed for side effects only
    monkeypatch.setattr(
        "solstone.think.cortex_client.get_use_log_status", lambda uid: "running"
    )

    use_id = cortex_request(prompt="Test", name="chat", provider="openai")

    # Verify use_id is a string timestamp
    assert isinstance(use_id, str)
    assert use_id.isdigit()
    assert len(use_id) == 13  # Millisecond timestamp


def test_cortex_request_uses_explicit_use_id(callosum_listener, monkeypatch):
    messages = callosum_listener
    monkeypatch.setattr(
        "solstone.think.cortex_client.get_use_log_status", lambda uid: "running"
    )

    use_id = cortex_request(
        prompt="Test prompt",
        name="chat",
        provider="openai",
        use_id="1713629000000",
    )

    time.sleep(0.2)

    assert use_id == "1713629000000"
    assert messages[-1]["use_id"] == "1713629000000"


def test_cortex_request_unique_agent_ids(callosum_server, monkeypatch):
    """Test that cortex_request generates unique agent IDs."""
    _ = callosum_server  # Needed for side effects only
    monkeypatch.setattr(
        "solstone.think.cortex_client.get_use_log_status", lambda uid: "running"
    )

    agent_ids = []
    for i in range(3):
        use_id = cortex_request(prompt=f"Test {i}", name="chat", provider="openai")
        agent_ids.append(use_id)
        time.sleep(0.002)

    # All agent IDs should be unique
    assert len(set(agent_ids)) == 3


def test_cortex_request_raises_when_callosum_unavailable(tmp_path, monkeypatch):
    """Test cortex_request classifies Callosum send failures."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(
        "solstone.think.cortex_client.callosum_send_classified",
        lambda *a, **kw: "FileNotFoundError",
    )

    with pytest.raises(CortexSpawnUnavailable) as excinfo:
        cortex_request(prompt="Test", name="chat", provider="openai")

    assert excinfo.value.detail == "FileNotFoundError"


def test_cortex_request_empty_journal(tmp_path, monkeypatch):
    """Test cortex_request works with an empty journal directory."""
    monkeypatch.setattr(
        "solstone.think.cortex_client.callosum_send_classified", lambda *a, **kw: ""
    )
    monkeypatch.setattr(
        "solstone.think.cortex_client.get_use_log_status", lambda uid: "running"
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    use_id = cortex_request("test", "chat", "openai")
    assert use_id is not None
    assert len(use_id) > 0


def _install_fake_claim_clock(monkeypatch, poll_interval=0.01, windows=(0.02,) * 3):
    """Fake the claim loop's clock so schedules run without real sleeps.

    Returns the cortex_client module and the mutable fake-monotonic clock, so
    tests can read elapsed time and timestamp individual sends.
    """
    import solstone.think.cortex_client as cc

    now = {"value": 0.0}
    monkeypatch.setattr(cc.time, "monotonic", lambda: now["value"])

    def fake_sleep(seconds):
        now["value"] += seconds

    monkeypatch.setattr(cc.time, "sleep", fake_sleep)
    monkeypatch.setattr(cc, "_CLAIM_POLL_INTERVAL_S", poll_interval)
    monkeypatch.setattr(cc, "_DEFAULT_CLAIM_WINDOWS", tuple(windows))
    return cc, now


def test_cortex_request_returns_when_claim_appears_after_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    cc, _ = _install_fake_claim_clock(monkeypatch)
    monkeypatch.setattr(cc, "callosum_send_classified", lambda *a, **kw: "")
    statuses = iter(["not_found", "running"])

    def fake_status(use_id):
        return next(statuses, "running")

    monkeypatch.setattr(cc, "get_use_log_status", fake_status)

    use_id = cortex_request("test", "chat", "openai", use_id="1713629000001")

    assert use_id == "1713629000001"


def test_cortex_request_rebroadcast_reuses_same_use_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    cc, _ = _install_fake_claim_clock(monkeypatch)
    send_calls = []

    def fake_send(*args, **kwargs):
        send_calls.append(kwargs)
        return ""

    monkeypatch.setattr(cc, "callosum_send_classified", fake_send)
    monkeypatch.setattr(
        cc,
        "get_use_log_status",
        lambda use_id: "running" if len(send_calls) >= 2 else "not_found",
    )

    use_id = cortex_request("test", "chat", "openai", use_id="1713629000002")

    assert use_id == "1713629000002"
    assert len(send_calls) >= 2
    assert {call["use_id"] for call in send_calls} == {"1713629000002"}


def test_cortex_request_raises_when_not_claimed(tmp_path, monkeypatch):
    """Default schedule fast-fails: three sends, one window each, then raises."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    cc, clock = _install_fake_claim_clock(monkeypatch)
    send_calls = []

    def fake_send(*args, **kwargs):
        send_calls.append(kwargs)
        return ""

    monkeypatch.setattr(cc, "callosum_send_classified", fake_send)
    monkeypatch.setattr(cc, "get_use_log_status", lambda use_id: "not_found")

    with pytest.raises(CortexNotClaimed) as excinfo:
        cortex_request("test", "chat", "openai", use_id="1713629000003")

    assert excinfo.value.use_id == "1713629000003"
    assert len(send_calls) == 3
    assert "after 3 broadcasts" in excinfo.value.detail
    assert clock["value"] == pytest.approx(sum(cc._DEFAULT_CLAIM_WINDOWS), abs=1e-6)


def test_cortex_request_patient_windows_are_non_decreasing(tmp_path, monkeypatch):
    """An explicit schedule drives send count, gaps, and total budget."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    cc, clock = _install_fake_claim_clock(monkeypatch, poll_interval=0.001)
    windows = (0.01, 0.02, 0.04, 0.08, 0.15)
    send_times = []

    def fake_send(*args, **kwargs):
        send_times.append(clock["value"])
        return ""

    monkeypatch.setattr(cc, "callosum_send_classified", fake_send)
    monkeypatch.setattr(cc, "get_use_log_status", lambda use_id: "not_found")

    with pytest.raises(CortexNotClaimed) as excinfo:
        cortex_request(
            "test", "chat", "openai", use_id="1713629000005", claim_windows=windows
        )

    assert len(send_times) == len(windows)
    assert "after 5 broadcasts" in excinfo.value.detail

    gaps = [b - a for a, b in zip(send_times, send_times[1:])]
    assert gaps == pytest.approx(list(windows[:-1]), abs=1e-6)
    assert gaps == sorted(gaps), "rebroadcast windows must be non-decreasing"
    assert clock["value"] == pytest.approx(sum(windows), abs=1e-6)


def test_cortex_request_raises_spawn_unavailable_on_failed_rebroadcast(
    tmp_path, monkeypatch
):
    """A send failure on a rebroadcast is CortexSpawnUnavailable, not NotClaimed."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    cc, _ = _install_fake_claim_clock(monkeypatch)
    send_calls = []

    def fake_send(*args, **kwargs):
        send_calls.append(kwargs)
        return "FileNotFoundError" if len(send_calls) >= 2 else ""

    monkeypatch.setattr(cc, "callosum_send_classified", fake_send)
    monkeypatch.setattr(cc, "get_use_log_status", lambda use_id: "not_found")

    with pytest.raises(CortexSpawnUnavailable) as excinfo:
        cortex_request("test", "chat", "openai", use_id="1713629000006")

    assert excinfo.value.detail == "FileNotFoundError"
    assert len(send_calls) == 2


def test_dispatch_cortex_request_patient_schedule_claims_after_default_would_fail(
    tmp_path, monkeypatch
):
    """The orchestrator survives a claim that lands past the fast-fail budget."""
    import solstone.think.thinking as think

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    default_windows = (0.01, 0.01, 0.01)  # budget 0.03
    patient_windows = (0.01, 0.02, 0.04, 0.08, 0.15)  # budget 0.30
    cc, clock = _install_fake_claim_clock(
        monkeypatch, poll_interval=0.001, windows=default_windows
    )
    monkeypatch.setattr(think, "PATIENT_CLAIM_WINDOWS", patient_windows)
    monkeypatch.setattr(cc, "callosum_send_classified", lambda *a, **kw: "")

    # Claim lands after the default budget expires but well inside the patient one.
    claim_at = 0.05
    monkeypatch.setattr(
        cc,
        "get_use_log_status",
        lambda use_id: "running" if clock["value"] >= claim_at else "not_found",
    )

    with pytest.raises(CortexNotClaimed):
        cortex_request("test", "chat", "openai", use_id="1713629000007")

    clock["value"] = 0.0
    result = think._dispatch_cortex_request(
        prompt="test", name="chat", provider="openai", use_id="1713629000008"
    )

    assert result == "1713629000008"


def test_cortex_request_immediate_claim_does_not_sleep(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.cortex_client as cc

    monkeypatch.setattr(cc, "callosum_send_classified", lambda *a, **kw: "")
    monkeypatch.setattr(cc, "get_use_log_status", lambda use_id: "running")

    def fail_sleep(seconds):
        raise AssertionError("sleep should not be called")

    monkeypatch.setattr(cc.time, "sleep", fail_sleep)

    use_id = cortex_request("test", "chat", "openai", use_id="1713629000004")

    assert use_id == "1713629000004"


# Tests for cortex_uses remain mostly unchanged as they read from files


def test_cortex_agents_empty(tmp_path, monkeypatch):
    """Test cortex_uses with no agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    result = cortex_uses()

    assert result["uses"] == []
    assert result["pagination"]["total"] == 0
    assert result["pagination"]["has_more"] is False
    assert result["live_count"] == 0
    assert result["historical_count"] == 0


def test_cortex_agents_with_active(tmp_path, monkeypatch):
    """Test cortex_uses with active (running) agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()

    # Create active agent files
    ts1 = now_ms()
    ts2 = ts1 + 1000

    unified_dir = talents_dir / "chat"
    tester_dir = talents_dir / "tester"
    unified_dir.mkdir()
    tester_dir.mkdir()

    active_file1 = unified_dir / f"{ts1}_active.jsonl"
    with open(active_file1, "w") as f:
        json.dump(
            {
                "event": "request",
                "ts": ts1,
                "prompt": "Task 1",
                "name": "chat",
                "provider": "openai",
            },
            f,
        )
        f.write("\n")

    active_file2 = tester_dir / f"{ts2}_active.jsonl"
    with open(active_file2, "w") as f:
        json.dump(
            {
                "event": "request",
                "ts": ts2,
                "prompt": "Task 2",
                "name": "tester",
                "provider": "google",
            },
            f,
        )
        f.write("\n")

    result = cortex_uses()

    assert len(result["uses"]) == 2
    assert result["live_count"] == 2
    assert result["historical_count"] == 0


def test_cortex_agents_with_completed(tmp_path, monkeypatch):
    """Test cortex_uses with completed (historical) agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()

    # Create completed agent files
    ts1 = now_ms()
    reviewer_dir = talents_dir / "reviewer"
    reviewer_dir.mkdir()

    completed_file1 = reviewer_dir / f"{ts1}.jsonl"
    with open(completed_file1, "w") as f:
        json.dump(
            {
                "event": "request",
                "ts": ts1,
                "prompt": "Old task",
                "name": "reviewer",
                "provider": "anthropic",
            },
            f,
        )
        f.write("\n")
        json.dump({"event": "finish", "ts": ts1 + 100, "result": "Done"}, f)
        f.write("\n")

    result = cortex_uses()

    assert len(result["uses"]) == 1
    assert result["live_count"] == 0
    assert result["historical_count"] == 1
    assert result["uses"][0]["status"] == "completed"


def test_cortex_agents_pagination(tmp_path, monkeypatch):
    """Test cortex_uses pagination."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()

    # Create multiple agents
    base_ts = now_ms()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    for i in range(5):
        ts = base_ts + (i * 1000)
        file = unified_dir / f"{ts}.jsonl"
        with open(file, "w") as f:
            json.dump(
                {
                    "event": "request",
                    "ts": ts,
                    "prompt": f"Task {i}",
                    "name": "chat",
                },
                f,
            )
            f.write("\n")

    # Test limit
    result = cortex_uses(limit=2)
    assert len(result["uses"]) == 2
    assert result["pagination"]["limit"] == 2
    assert result["pagination"]["total"] == 5
    assert result["pagination"]["has_more"] is True


def test_cortex_agents_empty_journal(tmp_path, monkeypatch):
    """Test cortex_uses works with an empty journal directory."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    result = cortex_uses()
    assert "uses" in result
    assert "pagination" in result
    assert isinstance(result["uses"], list)


def test_get_agent_log_status_completed(tmp_path, monkeypatch):
    """Test get_use_log_status returns 'completed' for finished agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    use_id = "1234567890123"
    (unified_dir / f"{use_id}.jsonl").write_text('{"event": "finish"}\n')

    assert get_use_log_status(use_id) == "completed"


def test_get_agent_log_status_running(tmp_path, monkeypatch):
    """Test get_use_log_status returns 'running' for active agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    use_id = "1234567890123"
    (unified_dir / f"{use_id}_active.jsonl").write_text('{"event": "start"}\n')

    assert get_use_log_status(use_id) == "running"


def test_read_use_provider_model_reads_active_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents" / "chat"
    talents_dir.mkdir(parents=True)

    use_id = "1234567890123"
    (talents_dir / f"{use_id}_active.jsonl").write_text(
        json.dumps({"event": "request", "provider": "openai", "model": "wrong"})
        + "\n"
        + json.dumps(
            {
                "event": "start",
                "provider": "anthropic",
                "model": "claude-opus-4-1",
            }
        )
        + "\n"
        + json.dumps({"event": "error", "reason_code": "provider_key_missing"})
        + "\n",
        encoding="utf-8",
    )

    assert read_use_provider_model(use_id) == ("anthropic", "claude-opus-4-1")
    assert read_use_provider_model_reason(use_id) == (
        "anthropic",
        "claude-opus-4-1",
        "provider_key_missing",
    )


def test_get_agent_log_status_not_found(tmp_path, monkeypatch):
    """Test get_use_log_status returns 'not_found' for missing agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    (tmp_path / "talents").mkdir()

    assert get_use_log_status("nonexistent") == "not_found"


def test_get_agent_log_status_prefers_completed(tmp_path, monkeypatch):
    """Test get_use_log_status returns 'completed' when both files exist."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    # Edge case: both files exist (shouldn't happen, but check precedence)
    use_id = "1234567890123"
    (unified_dir / f"{use_id}.jsonl").write_text('{"event": "finish"}\n')
    (unified_dir / f"{use_id}_active.jsonl").write_text('{"event": "start"}\n')

    assert get_use_log_status(use_id) == "completed"


def test_get_agent_end_state_finish(tmp_path, monkeypatch):
    """Test get_use_end_state returns 'finish' for successful agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    use_id = "1234567890123"
    (unified_dir / f"{use_id}.jsonl").write_text(
        '{"event": "request", "prompt": "hello"}\n'
        '{"event": "finish", "result": "done"}\n'
    )

    assert get_use_end_state(use_id) == "finish"


def test_get_agent_end_state_error(tmp_path, monkeypatch):
    """Test get_use_end_state returns 'error' for failed agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    use_id = "1234567890123"
    (unified_dir / f"{use_id}.jsonl").write_text(
        '{"event": "request", "prompt": "hello"}\n'
        '{"event": "error", "error": "something went wrong"}\n'
    )

    assert get_use_end_state(use_id) == "error"


def test_get_agent_end_state_no_output_maps_to_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    use_id = "1234567890123"
    (unified_dir / f"{use_id}.jsonl").write_text(
        json.dumps({"event": "request", "prompt": "hello"})
        + "\n"
        + json.dumps(
            {
                "event": "error",
                "error": "no_output: expects-final cogitate run finished without "
                "emitting a final result",
                "reason_code": "no_output",
                "terminal": True,
            }
        )
        + "\n"
    )

    assert get_use_end_state(use_id) == "error"


@pytest.mark.parametrize(
    "reason_code",
    ["token_budget_exceeded", "max_turns_exhausted", "agent_stuck"],
)
def test_force_finish_error_surfaces_budget_reason_code(
    tmp_path,
    monkeypatch,
    reason_code,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    use_id = "1234567890123"
    (unified_dir / f"{use_id}.jsonl").write_text(
        json.dumps({"event": "request", "prompt": "hello"})
        + "\n"
        + json.dumps(
            {
                "event": "start",
                "provider": "openai",
                "model": GPT_5,
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "error",
                "error": f"{reason_code}: force-finished",
                "reason_code": reason_code,
                "terminal": True,
            }
        )
        + "\n"
    )

    assert get_use_end_state(use_id) == "error"
    assert read_use_provider_model_reason(use_id) == ("openai", GPT_5, reason_code)


def test_get_agent_end_state_finish_after_nonterminal_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    use_id = "1234567890123"
    (unified_dir / f"{use_id}.jsonl").write_text(
        json.dumps({"event": "request", "prompt": "hello"})
        + "\n"
        + json.dumps(
            {
                "event": "start",
                "provider": "openai",
                "model": GPT_5,
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "error",
                "error": "recoverable agent error",
                "terminal": False,
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "finish",
                "result": "done",
                "usage": {"total_tokens": 10},
            }
        )
        + "\n"
    )

    assert get_use_end_state(use_id) == "finish"


def test_get_agent_end_state_running(tmp_path, monkeypatch):
    """Test get_use_end_state returns 'running' for active agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()

    use_id = "1234567890123"
    (unified_dir / f"{use_id}_active.jsonl").write_text(
        '{"event": "request", "prompt": "hello"}\n'
    )

    assert get_use_end_state(use_id) == "running"


def test_get_agent_end_state_unknown(tmp_path, monkeypatch):
    """Test get_use_end_state returns 'unknown' for missing agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    (tmp_path / "talents").mkdir()

    assert get_use_end_state("nonexistent") == "unknown"


# Tests for wait_for_uses


def test_wait_for_agents_already_complete(tmp_path, monkeypatch):
    """Test wait_for_uses returns immediately if agents already completed."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    # Create completed agents
    agent_ids = ["1000", "2000"]
    for use_id in agent_ids:
        (unified_dir / f"{use_id}.jsonl").write_text('{"event": "finish"}\n')

    completed, timed_out = wait_for_uses(agent_ids, timeout=1)

    assert set(completed.keys()) == set(agent_ids)
    assert all(v == "finish" for v in completed.values())
    assert timed_out == []


def test_wait_for_agents_event_completion(callosum_server):
    """Test wait_for_uses completes when finish event is received."""
    tmp_path = callosum_server
    talents_dir = tmp_path / "talents"
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir(exist_ok=True)

    use_id = "1234567890123"

    # Start wait in background thread
    result = {"completed": None, "timed_out": None}

    def wait_thread():
        result["completed"], result["timed_out"] = wait_for_uses([use_id], timeout=5)

    waiter = threading.Thread(target=wait_thread)
    waiter.start()

    # Give the waiter time to set up listener
    time.sleep(0.2)

    # Create the completed file and emit finish event
    (unified_dir / f"{use_id}.jsonl").write_text('{"event": "finish"}\n')

    # Emit finish event via Callosum
    client = CallosumConnection()
    client.start()
    time.sleep(0.1)
    client.emit("cortex", "finish", use_id=use_id, result="done")
    time.sleep(0.2)
    client.stop()

    waiter.join(timeout=3)

    assert result["completed"] == {use_id: "finish"}
    assert result["timed_out"] == []


def test_wait_for_agents_error_event(callosum_server):
    """Test wait_for_uses completes on error event too."""
    tmp_path = callosum_server
    talents_dir = tmp_path / "talents"
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir(exist_ok=True)

    use_id = "1234567890124"

    result = {"completed": None, "timed_out": None}

    def wait_thread():
        result["completed"], result["timed_out"] = wait_for_uses([use_id], timeout=5)

    waiter = threading.Thread(target=wait_thread)
    waiter.start()
    time.sleep(0.2)

    # Create completed file and emit error event
    (unified_dir / f"{use_id}.jsonl").write_text('{"event": "error"}\n')

    client = CallosumConnection()
    client.start()
    time.sleep(0.1)
    client.emit("cortex", "error", use_id=use_id, error="something failed")
    time.sleep(0.2)
    client.stop()

    waiter.join(timeout=3)

    assert result["completed"] == {use_id: "error"}
    assert result["timed_out"] == []


def test_wait_for_agents_initial_file_check(tmp_path, monkeypatch):
    """Test wait_for_uses finds already-completed agents via initial file check."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890125"

    # Agent already completed before we start waiting
    (unified_dir / f"{use_id}.jsonl").write_text('{"event": "finish"}\n')

    completed, timed_out = wait_for_uses([use_id], timeout=1)

    # Should find via initial file check
    assert completed == {use_id: "finish"}
    assert timed_out == []


def test_wait_for_agents_timeout_actual(tmp_path, monkeypatch):
    """Test wait_for_uses times out for agents that never complete."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890126"
    # Create active file (not completed)
    (unified_dir / f"{use_id}_active.jsonl").write_text('{"event": "start"}\n')

    completed, timed_out = wait_for_uses([use_id], timeout=1)

    assert completed == {}
    assert timed_out == [use_id]


def test_wait_for_agents_partial(callosum_server):
    """Test wait_for_uses with some completing and some timing out."""
    tmp_path = callosum_server
    talents_dir = tmp_path / "talents"
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir(exist_ok=True)

    completing_agent = "1111"
    timeout_agent = "2222"

    # Create active file for timeout agent
    (unified_dir / f"{timeout_agent}_active.jsonl").write_text('{"event": "start"}\n')

    result = {"completed": None, "timed_out": None}

    def wait_thread():
        result["completed"], result["timed_out"] = wait_for_uses(
            [completing_agent, timeout_agent], timeout=1
        )

    waiter = threading.Thread(target=wait_thread)
    waiter.start()
    time.sleep(0.2)

    # Complete one agent
    (unified_dir / f"{completing_agent}.jsonl").write_text('{"event": "finish"}\n')

    client = CallosumConnection()
    client.start()
    time.sleep(0.1)
    client.emit("cortex", "finish", use_id=completing_agent, result="done")
    time.sleep(0.1)
    client.stop()

    waiter.join(timeout=5)

    assert result["completed"] == {completing_agent: "finish"}
    assert result["timed_out"] == [timeout_agent]


def test_wait_for_agents_missed_event_recovery(tmp_path, monkeypatch, caplog):
    """Test that missed events are recovered via final file check with INFO log."""
    import logging

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890127"

    # Start with active file
    (unified_dir / f"{use_id}_active.jsonl").write_text('{"event": "start"}\n')

    result = {"completed": None, "timed_out": None}

    def wait_and_complete():
        # Wait a bit then "complete" the agent by renaming file
        time.sleep(0.3)
        (unified_dir / f"{use_id}.jsonl").write_text('{"event": "finish"}\n')
        (unified_dir / f"{use_id}_active.jsonl").unlink()

    completer = threading.Thread(target=wait_and_complete)
    completer.start()

    with caplog.at_level(logging.INFO):
        result["completed"], result["timed_out"] = wait_for_uses([use_id], timeout=1)

    completer.join()

    # Should recover via final file check
    assert result["completed"] == {use_id: "finish"}
    assert result["timed_out"] == []

    # Should log about missed event
    assert any(
        "completion event not received but use completed" in record.message
        for record in caplog.records
    )


def test_wait_for_uses_renamed_terminal_via_polling(tmp_path, monkeypatch):
    """Test polling recovers a use renamed terminal without Callosum."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("solstone.think.cortex_client._POLL_INTERVAL_S", 0.02)
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890128"
    active_file = unified_dir / f"{use_id}_active.jsonl"
    final_file = unified_dir / f"{use_id}.jsonl"
    active_file.write_text('{"event": "start"}\n')

    def complete_use():
        time.sleep(0.05)
        final_file.write_text('{"event": "finish"}\n')
        active_file.unlink()

    completer = threading.Thread(target=complete_use, daemon=True)
    completer.start()

    started_at = time.monotonic()
    completed, timed_out = wait_for_uses([use_id], timeout=5)
    elapsed = time.monotonic() - started_at
    completer.join(timeout=5)

    assert completed == {use_id: "finish"}
    assert timed_out == []
    assert elapsed < 2.0


def test_wait_for_uses_terminal_before_rename_via_polling(tmp_path, monkeypatch):
    """Test polling recovers terminal events appended to the active log."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("solstone.think.cortex_client._POLL_INTERVAL_S", 0.02)
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890129"
    active_file = unified_dir / f"{use_id}_active.jsonl"
    active_file.write_text('{"event": "start"}\n')

    result = {"completed": None, "timed_out": None}

    def wait_thread():
        result["completed"], result["timed_out"] = wait_for_uses([use_id], timeout=5)

    waiter = threading.Thread(target=wait_thread, daemon=True)
    waiter.start()
    time.sleep(0.05)

    with open(active_file, "a") as f:
        f.write('{"event": "finish"}\n')

    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert result["completed"] == {use_id: "finish"}
    assert result["timed_out"] == []


def test_wait_for_uses_timeout_none_returns_on_terminal(tmp_path, monkeypatch):
    """Test timeout=None returns when polling sees a terminal log."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("solstone.think.cortex_client._POLL_INTERVAL_S", 0.02)
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890130"
    active_file = unified_dir / f"{use_id}_active.jsonl"
    final_file = unified_dir / f"{use_id}.jsonl"
    active_file.write_text('{"event": "start"}\n')

    result = {"completed": None, "timed_out": None}

    def wait_thread():
        result["completed"], result["timed_out"] = wait_for_uses([use_id], timeout=None)

    waiter = threading.Thread(target=wait_thread, daemon=True)
    waiter.start()
    time.sleep(0.05)

    final_file.write_text('{"event": "finish"}\n')
    active_file.unlink()

    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert result["completed"] == {use_id: "finish"}
    assert result["timed_out"] == []


def test_wait_for_uses_timeout_none_no_deadline_and_no_busy_spin(
    tmp_path,
    monkeypatch,
):
    """Test timeout=None polls at the bounded interval until terminal."""
    import solstone.think.cortex_client as cc

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(cc, "_POLL_INTERVAL_S", 0.05)
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890131"
    active_file = unified_dir / f"{use_id}_active.jsonl"
    final_file = unified_dir / f"{use_id}.jsonl"
    active_file.write_text('{"event": "start"}\n')

    real = cc.get_use_end_state
    calls = []

    def spy(uid):
        calls.append(uid)
        return real(uid)

    monkeypatch.setattr(cc, "get_use_end_state", spy)

    result = {"completed": None, "timed_out": None}

    def wait_thread():
        result["completed"], result["timed_out"] = wait_for_uses([use_id], timeout=None)

    waiter = threading.Thread(target=wait_thread, daemon=True)
    waiter.start()
    time.sleep(0.3)

    calls_before_completion = len(calls)
    assert calls_before_completion <= 20

    final_file.write_text('{"event": "finish"}\n')
    active_file.unlink()
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert result["completed"] == {use_id: "finish"}
    assert result["timed_out"] == []


def test_wait_for_uses_unreadable_log_returns_unknown(tmp_path, monkeypatch):
    """Test get_use_end_state tolerates unreadable logs."""
    import solstone.think.cortex_client as cc

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890132"
    (unified_dir / f"{use_id}_active.jsonl").write_text('{"event": "start"}\n')

    def unreadable(_uid):
        raise PermissionError("boom")

    monkeypatch.setattr(cc, "read_use_events", unreadable)

    assert get_use_end_state(use_id) == "unknown"


def test_wait_for_uses_unreadable_sibling_does_not_block_terminal_sibling(
    tmp_path,
    monkeypatch,
):
    """Test an unreadable pending use does not block a terminal sibling."""
    import solstone.think.cortex_client as cc

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(cc, "_POLL_INTERVAL_S", 0.02)
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    terminal_use = "1234567890133"
    unreadable_use = "1234567890134"
    (unified_dir / f"{terminal_use}_active.jsonl").write_text('{"event": "start"}\n')
    (unified_dir / f"{unreadable_use}_active.jsonl").write_text('{"event": "start"}\n')
    (unified_dir / f"{terminal_use}.jsonl").write_text('{"event": "finish"}\n')

    real = cc.read_use_events

    def spy(uid):
        if uid == unreadable_use:
            raise OSError("unreadable")
        return real(uid)

    monkeypatch.setattr(cc, "read_use_events", spy)

    completed, timed_out = wait_for_uses([terminal_use, unreadable_use], timeout=1)

    assert completed == {terminal_use: "finish"}
    assert timed_out == [unreadable_use]


def test_wait_for_uses_transient_read_error_then_recovery(tmp_path, monkeypatch):
    """Test a transient read error stays pending and recovers later."""
    import solstone.think.cortex_client as cc

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(cc, "_POLL_INTERVAL_S", 0.02)
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890135"
    (unified_dir / f"{use_id}_active.jsonl").write_text('{"event": "start"}\n')

    n = {"c": 0}

    def spy(_uid):
        n["c"] += 1
        if n["c"] == 1:
            raise OSError("race")
        return [{"event": "start"}, {"event": "finish"}]

    monkeypatch.setattr(cc, "read_use_events", spy)

    completed, timed_out = wait_for_uses([use_id], timeout=2)

    assert completed == {use_id: "finish"}
    assert timed_out == []


def test_wait_for_uses_malformed_line_before_terminal(tmp_path, monkeypatch):
    """Test polling skips malformed JSON and still recovers terminal events."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("solstone.think.cortex_client._POLL_INTERVAL_S", 0.02)
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890136"
    active_file = unified_dir / f"{use_id}_active.jsonl"
    active_file.write_text('{"event": "start"}\n')

    result = {"completed": None, "timed_out": None}

    def wait_thread():
        result["completed"], result["timed_out"] = wait_for_uses([use_id], timeout=5)

    waiter = threading.Thread(target=wait_thread, daemon=True)
    waiter.start()
    time.sleep(0.05)

    with open(active_file, "a") as f:
        f.write("not valid json\n")
        f.write('{"event": "finish"}\n')

    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert result["completed"] == {use_id: "finish"}
    assert result["timed_out"] == []


def test_wait_for_uses_recovery_logs_exactly_once(tmp_path, monkeypatch, caplog):
    """Test missed-event recovery logs once, not once per poll."""
    import logging

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("solstone.think.cortex_client._POLL_INTERVAL_S", 0.05)
    talents_dir = tmp_path / "talents"
    talents_dir.mkdir()
    unified_dir = talents_dir / "chat"
    unified_dir.mkdir()
    (tmp_path / "health").mkdir()

    use_id = "1234567890137"
    active_file = unified_dir / f"{use_id}_active.jsonl"
    final_file = unified_dir / f"{use_id}.jsonl"
    active_file.write_text('{"event": "start"}\n')

    def complete_use():
        time.sleep(0.2)
        final_file.write_text('{"event": "finish"}\n')
        active_file.unlink()

    completer = threading.Thread(target=complete_use, daemon=True)
    completer.start()

    with caplog.at_level(logging.INFO):
        completed, timed_out = wait_for_uses([use_id], timeout=2)

    completer.join(timeout=5)

    matching_records = [
        record
        for record in caplog.records
        if "completion event not received but use completed" in record.message
    ]
    assert completed == {use_id: "finish"}
    assert timed_out == []
    assert len(matching_records) == 1
