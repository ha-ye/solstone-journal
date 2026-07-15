# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unit tests for the Callosum message bus.

These tests use mocks to test logic in isolation without real I/O.
"""

import logging
import queue
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from solstone.think.callosum import CallosumConnection, CallosumServer


@pytest.fixture
def journal_path(tmp_path, monkeypatch):
    """Set up a temporary journal path."""
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    yield journal


@pytest.fixture
def short_callosum_server(monkeypatch):
    """Start Callosum under a short /tmp path to avoid Unix socket path limits."""
    tmp_dir = tempfile.mkdtemp(dir="/tmp", prefix="callosum_")
    tmp_path = Path(tmp_dir)

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    server = CallosumServer()
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()

    socket_path = tmp_path / "health" / "callosum.sock"
    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)
    else:
        pytest.fail("Callosum server did not start in time")

    yield server

    server.stop()
    server_thread.join(timeout=2)
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_server_broadcast_validates_tract_field():
    """Test that messages without tract field are rejected."""
    server = CallosumServer()

    # Message without tract should be rejected and return False
    invalid_msg = {"event": "test"}
    result = server.broadcast(invalid_msg)

    assert result is False
    # Should not be queued
    assert server.broadcast_queue.qsize() == 0


def test_client_inbound_flood_receives_300kb_under_5s(short_callosum_server):
    server = short_callosum_server
    total_messages = 1024
    payload = "x" * 300
    received = 0
    lock = threading.Lock()
    all_received = threading.Event()

    def callback(message):
        nonlocal received
        if message.get("tract") != "flood":
            return
        with lock:
            received += 1
            if received == total_messages:
                all_received.set()

    client = CallosumConnection()
    client.start(callback=callback)
    try:
        for _ in range(50):
            if server.client_count() >= 1:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Callosum client did not connect in time")

        start = time.monotonic()
        for index in range(total_messages):
            assert server.broadcast(
                {
                    "tract": "flood",
                    "event": "chunk",
                    "index": index,
                    "payload": payload,
                }
            )

        finished = all_received.wait(timeout=5.0)
        elapsed = time.monotonic() - start

        with lock:
            count = received
        assert finished, (
            f"received {count}/{total_messages} callbacks in {elapsed:.2f}s"
        )
    finally:
        client.stop()


def test_client_emit_during_inbound_flood_reaches_server_under_2s(
    short_callosum_server,
):
    server = short_callosum_server
    inbound_started = threading.Event()
    outbound_received = threading.Event()
    stop_producer = threading.Event()
    inbound_count = 0
    lock = threading.Lock()

    def callback(message):
        nonlocal inbound_count
        if message.get("tract") == "flood":
            with lock:
                inbound_count += 1
                if inbound_count >= 20:
                    inbound_started.set()
        elif message.get("tract") == "probe" and message.get("event") == "mid_flood":
            outbound_received.set()

    client = CallosumConnection()
    client.start(callback=callback)

    def producer():
        index = 0
        while not stop_producer.is_set():
            server.broadcast(
                {
                    "tract": "flood",
                    "event": "chunk",
                    "index": index,
                    "payload": "x" * 300,
                }
            )
            index += 1
            time.sleep(0.001)

    producer_thread = threading.Thread(target=producer, daemon=True)
    try:
        for _ in range(50):
            if server.client_count() >= 1:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Callosum client did not connect in time")

        producer_thread.start()
        assert inbound_started.wait(timeout=2.0)

        start = time.monotonic()
        assert client.emit("probe", "mid_flood")
        assert outbound_received.wait(timeout=2.0)
        assert time.monotonic() - start < 2.0
    finally:
        stop_producer.set()
        producer_thread.join(timeout=2)
        client.stop()


def test_client_receives_during_outbound_flood(short_callosum_server):
    server = short_callosum_server
    inbound_received = threading.Event()
    stop_producer = threading.Event()
    inbound_count = 0
    lock = threading.Lock()

    def callback(message):
        nonlocal inbound_count
        if message.get("tract") != "inbound":
            return
        with lock:
            inbound_count += 1
            if inbound_count >= 20:
                inbound_received.set()

    client = CallosumConnection()
    client.start(callback=callback)

    def producer():
        index = 0
        while not stop_producer.is_set():
            try:
                client.send_queue.put_nowait(
                    {"tract": "outbound", "event": "flood", "index": index}
                )
                index += 1
            except queue.Full:
                time.sleep(0.001)

    producer_thread = threading.Thread(target=producer, daemon=True)
    try:
        for _ in range(50):
            if server.client_count() >= 1:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Callosum client did not connect in time")

        producer_thread.start()
        for index in range(20):
            assert server.broadcast(
                {"tract": "inbound", "event": "probe", "index": index}
            )

        assert inbound_received.wait(timeout=2.0)
    finally:
        stop_producer.set()
        producer_thread.join(timeout=2)
        client.stop()


def test_client_disconnected_idle_blocks_and_drops_emit(tmp_path, monkeypatch):
    client = CallosumConnection(socket_path=tmp_path / "missing.sock")
    original_wait = client.stop_event.wait
    wait_calls = 0
    block_reached = threading.Event()

    def counted_wait(timeout=None):
        nonlocal wait_calls
        wait_calls += 1
        block_reached.set()
        return original_wait(timeout)

    monkeypatch.setattr(client.stop_event, "wait", counted_wait)

    drop_logged = threading.Event()
    drop_message = "Dropping message (not connected): test/event"

    class _DropWatcher(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if drop_message in record.getMessage():
                drop_logged.set()

    logger = logging.getLogger("solstone.think.callosum")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    handler = _DropWatcher()
    logger.addHandler(handler)
    try:
        client.start()
        try:
            # Wait until the reconnect loop has entered its disconnected idle block
            # (first stop_event.wait), then emit while still disconnected.
            assert block_reached.wait(timeout=2.0)
            assert client.emit("test", "event")
            assert drop_logged.wait(timeout=2.0)
        finally:
            client.stop()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert 1 <= wait_calls <= 20


def test_client_does_not_join_partial_json_across_reconnect():
    client = CallosumConnection()
    delivered = []

    read1, write1 = socket.socketpair()
    read2, write2 = socket.socketpair()
    write1.sendall(b"xx")
    write2.sendall(b"x")

    sock1 = Mock()
    sock2 = Mock()
    sock1.fileno.return_value = read1.fileno()
    sock2.fileno.return_value = read2.fileno()

    first_calls = 0

    def recv_first(_n):
        nonlocal first_calls
        read1.recv(1)
        first_calls += 1
        if first_calls == 1:
            return b'{"tract":"split"'
        return b""

    def recv_second(_n):
        read2.recv(1)
        client.stop_event.set()
        return b',"event":"joined"}\n'

    sock1.recv.side_effect = recv_first
    sock2.recv.side_effect = recv_second

    times = iter([2.0, 4.0, 6.0, 8.0, 10.0])
    try:
        with (
            patch("solstone.think.callosum.socket.socket", side_effect=[sock1, sock2]),
            patch("solstone.think.callosum.time.time", side_effect=lambda: next(times)),
        ):
            client.start(callback=delivered.append)
            if client.thread is not None:
                client.thread.join(timeout=1.0)
        assert delivered == []
    finally:
        for handle in (read1, write1, read2, write2):
            handle.close()


def test_client_callback_exception_does_not_kill_thread(short_callosum_server, caplog):
    server = short_callosum_server
    delivered = []
    second_received = threading.Event()

    def callback(message):
        if message.get("tract") != "callback":
            return
        delivered.append(message["event"])
        if message["event"] == "first":
            raise RuntimeError("boom")
        if message["event"] == "second":
            second_received.set()

    client = CallosumConnection()
    client.start(callback=callback)
    try:
        for _ in range(50):
            if server.client_count() >= 1:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Callosum client did not connect in time")

        with caplog.at_level(logging.ERROR, logger="solstone.think.callosum"):
            assert server.broadcast({"tract": "callback", "event": "first"})
            assert server.broadcast({"tract": "callback", "event": "second"})
            assert second_received.wait(timeout=2.0)

        assert delivered == ["first", "second"]
        assert "Callback error: boom" in caplog.text
    finally:
        client.stop()


def test_server_broadcast_validates_event_field():
    """Test that messages without event field are rejected."""
    server = CallosumServer()

    # Message without event should be rejected and return False
    invalid_msg = {"tract": "test"}
    result = server.broadcast(invalid_msg)

    assert result is False
    # Should not be queued
    assert server.broadcast_queue.qsize() == 0


def test_server_broadcast_adds_timestamp():
    """Test that server adds timestamp if not present."""
    server = CallosumServer()

    # Valid message without timestamp
    msg = {"tract": "test", "event": "hello"}

    with patch("solstone.think.callosum.time.time", return_value=1234567.890):
        result = server.broadcast(msg)

    assert result is True
    # Message should be queued with timestamp added
    queued_msg = server.broadcast_queue.get_nowait()
    assert queued_msg["tract"] == "test"
    assert queued_msg["event"] == "hello"
    assert queued_msg["ts"] == 1234567890  # milliseconds


def test_server_broadcast_preserves_custom_timestamp():
    """Test that custom timestamp in message is preserved."""
    server = CallosumServer()

    custom_ts = 9999999999
    msg = {"tract": "test", "event": "hello", "ts": custom_ts}

    result = server.broadcast(msg)

    assert result is True
    # Should preserve custom timestamp
    queued_msg = server.broadcast_queue.get_nowait()
    assert queued_msg["ts"] == custom_ts


def test_server_broadcast_removes_dead_clients():
    """Test that _send_to_clients removes clients that fail to receive."""
    server = CallosumServer()

    # Create mock clients - one working, one dead
    working_client = Mock()
    dead_client = Mock()
    dead_client.sendall.side_effect = Exception("Connection broken")
    dead_client.settimeout = Mock()
    working_client.settimeout = Mock()

    server.clients = [working_client, dead_client]

    # Call _send_to_clients directly (the method used by _writer_loop)
    msg = {"tract": "test", "event": "hello", "ts": 12345}
    server._send_to_clients(msg)

    # Dead client should be removed
    assert working_client in server.clients
    assert dead_client not in server.clients
    assert len(server.clients) == 1

    # Dead client socket should be closed
    dead_client.close.assert_called_once()


def test_server_handle_client_warns_on_utf8_split(caplog):
    server = CallosumServer()
    survivor = Mock()
    bad_conn = Mock()
    bad_conn.settimeout = Mock()
    # First recv returns the first byte of a 3-byte char (invalid on its own);
    # decode raises UnicodeDecodeError. b"" is a safety net to end the loop.
    bad_conn.recv.side_effect = ["⚠️".encode("utf-8")[:1], b""]
    server.clients = [survivor]
    with caplog.at_level(logging.WARNING, logger="solstone.think.callosum"):
        server._handle_client(bad_conn)
    assert "utf-8 split" in caplog.text
    assert "[server]" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    # Offending client dropped + closed (existing finally cleanup); survivor kept.
    assert bad_conn not in server.clients
    assert survivor in server.clients
    bad_conn.close.assert_called_once()


def test_client_emit_returns_false_when_not_started():
    """Test that emit() returns False and logs warning if start() not called yet."""
    client = CallosumConnection()

    # emit() should return False and log when thread not started
    with patch("solstone.think.callosum.logger") as mock_logger:
        result = client.emit("test", "hello")
        assert result is False
        mock_logger.warning.assert_called_once()
        assert "Thread not running" in mock_logger.warning.call_args[0][0]


def test_client_emit_queues_message():
    """Test that emit() queues message when thread is running."""
    client = CallosumConnection()

    # Setup running thread
    mock_thread = Mock()
    mock_thread.is_alive.return_value = True
    client.thread = mock_thread

    result = client.emit("test", "hello", data="world", count=42)

    assert result is True
    # Message should be in queue
    assert client.send_queue.qsize() == 1
    msg = client.send_queue.get_nowait()
    assert msg["tract"] == "test"
    assert msg["event"] == "hello"
    assert msg["data"] == "world"
    assert msg["count"] == 42


def test_client_emit_returns_false_when_queue_full():
    """Test that emit() returns False when queue is full."""
    client = CallosumConnection()

    # Setup running thread
    mock_thread = Mock()
    mock_thread.is_alive.return_value = True
    client.thread = mock_thread

    # Fill the queue
    for i in range(1000):
        client.send_queue.put({"tract": "test", "event": f"msg{i}"})

    # Next emit should fail
    with patch("solstone.think.callosum.logger") as mock_logger:
        result = client.emit("test", "overflow")
        assert result is False
        mock_logger.warning.assert_called()
        assert "Queue full" in mock_logger.warning.call_args[0][0]


def test_client_run_loop_warns_on_utf8_split(caplog):
    client = CallosumConnection()
    mock_sock = Mock()
    read_sock, write_sock = socket.socketpair()
    write_sock.sendall(b"x")
    mock_sock.fileno.return_value = read_sock.fileno()

    def fake_recv(_n):
        client.stop_event.set()  # loop exits at next queue-drain
        return "⚠️".encode("utf-8")[:1]

    mock_sock.recv.side_effect = fake_recv
    try:
        with (
            patch("solstone.think.callosum.socket.socket", return_value=mock_sock),
            patch("solstone.think.callosum.time.time", return_value=2.0),
            caplog.at_level(logging.WARNING, logger="solstone.think.callosum"),
        ):
            client._run_loop()
    finally:
        read_sock.close()
        write_sock.close()
    assert "utf-8 split" in caplog.text
    assert "[client]" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    # Socket dropped/closed as part of the reset.
    mock_sock.close.assert_called()


def test_client_start_creates_thread():
    """Test that start() creates and starts background thread."""
    client = CallosumConnection()

    def callback(msg):
        pass

    client.start(callback=callback)

    assert client.thread is not None
    assert client.thread.is_alive()
    assert client.callback is callback

    # Cleanup
    client.stop()


def test_client_start_idempotent():
    """Test that calling start() multiple times is safe."""
    client = CallosumConnection()

    client.start()
    first_thread = client.thread

    # Call start again
    client.start()

    # Should still have same thread (not restarted)
    assert client.thread is first_thread

    # Cleanup
    client.stop()


def test_client_stop_stops_thread():
    """Test that stop() stops the background thread."""
    client = CallosumConnection()

    # Setup running thread
    mock_thread = Mock()
    mock_thread.is_alive.return_value = False
    client.thread = mock_thread

    client.stop()

    # Should set stop event and join thread
    assert client.stop_event.is_set()
    mock_thread.join.assert_called_once_with(timeout=0.5)


def test_server_socket_path_from_env(journal_path):
    """Test that server uses SOLSTONE_JOURNAL env var for socket path."""
    server = CallosumServer()

    expected_path = journal_path / "health" / "callosum.sock"
    assert server.socket_path == expected_path


def test_server_socket_path_custom():
    """Test that server accepts custom socket path."""
    custom_path = Path("/tmp/custom.sock")
    server = CallosumServer(socket_path=custom_path)

    assert server.socket_path == custom_path


def test_client_socket_path_from_env(journal_path):
    """Test that client uses SOLSTONE_JOURNAL env var for socket path."""
    client = CallosumConnection()

    expected_path = journal_path / "health" / "callosum.sock"
    assert client.socket_path == expected_path


def test_client_socket_path_custom():
    """Test that client accepts custom socket path."""
    custom_path = Path("/tmp/custom.sock")
    client = CallosumConnection(socket_path=custom_path)

    assert client.socket_path == custom_path


def test_callosum_send_empty_journal(tmp_path, monkeypatch):
    """Test that callosum_send() works with an empty journal directory."""
    from solstone.think.callosum import callosum_send

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    # No server listening at tmp_path, so send will fail gracefully
    result = callosum_send("test", "event", data="value")
    assert isinstance(result, bool)


def test_callosum_send_with_custom_path():
    """Test that callosum_send() accepts custom socket path."""
    from solstone.think.callosum import callosum_send

    # Use non-existent socket - should return False but not crash
    custom_path = Path("/tmp/nonexistent_callosum.sock")
    result = callosum_send("test", "event", socket_path=custom_path, data="value")

    # Should fail gracefully (no server listening)
    assert result is False


def test_callosum_send_classified_returns_exception_class_name(tmp_path):
    """Test classified send reports the swallowed exception class name."""
    from solstone.think.callosum import callosum_send_classified

    custom_path = tmp_path / "nonexistent_callosum.sock"
    result = callosum_send_classified(
        "test", "event", socket_path=custom_path, data="value"
    )

    assert result == "FileNotFoundError"


# --- CLI helper tests ---


class TestParseValue:
    """Tests for _parse_value auto-type detection."""

    def test_integer(self):
        from solstone.think.callosum import _parse_value

        assert _parse_value("42") == 42

    def test_float(self):
        from solstone.think.callosum import _parse_value

        assert _parse_value("3.14") == 3.14

    def test_boolean_true(self):
        from solstone.think.callosum import _parse_value

        assert _parse_value("true") is True

    def test_boolean_false(self):
        from solstone.think.callosum import _parse_value

        assert _parse_value("false") is False

    def test_null(self):
        from solstone.think.callosum import _parse_value

        assert _parse_value("null") is None

    def test_plain_string(self):
        from solstone.think.callosum import _parse_value

        assert _parse_value("hello") == "hello"

    def test_string_with_spaces(self):
        from solstone.think.callosum import _parse_value

        assert _parse_value("hello world") == "hello world"

    def test_json_array(self):
        from solstone.think.callosum import _parse_value

        assert _parse_value("[1,2,3]") == [1, 2, 3]


class TestParseKvFields:
    """Tests for _parse_kv_fields key=value parsing."""

    def test_basic_fields(self):
        from solstone.think.callosum import _parse_kv_fields

        result = _parse_kv_fields(["day=20250101", "count=5", "active=true"])
        assert result == {"day": 20250101, "count": 5, "active": True}

    def test_empty_list(self):
        from solstone.think.callosum import _parse_kv_fields

        assert _parse_kv_fields([]) == {}

    def test_value_with_equals(self):
        from solstone.think.callosum import _parse_kv_fields

        # Value containing '=' should keep everything after first '='
        result = _parse_kv_fields(["expr=a=b"])
        assert result == {"expr": "a=b"}

    def test_missing_equals_exits(self):
        from solstone.think.callosum import _parse_kv_fields

        with pytest.raises(SystemExit):
            _parse_kv_fields(["no_equals_here"])


class TestParseJsonMessage:
    """Tests for _parse_json_message validation."""

    def test_valid_json(self):
        from solstone.think.callosum import _parse_json_message

        result = _parse_json_message('{"tract":"test","event":"ping","data":1}')
        assert result == {"tract": "test", "event": "ping", "data": 1}

    def test_missing_tract(self):
        from solstone.think.callosum import _parse_json_message

        with pytest.raises(SystemExit):
            _parse_json_message('{"event":"ping"}')

    def test_missing_event(self):
        from solstone.think.callosum import _parse_json_message

        with pytest.raises(SystemExit):
            _parse_json_message('{"tract":"test"}')

    def test_invalid_json(self):
        from solstone.think.callosum import _parse_json_message

        with pytest.raises(SystemExit):
            _parse_json_message("not json")

    def test_json_array_rejected(self):
        from solstone.think.callosum import _parse_json_message

        with pytest.raises(SystemExit):
            _parse_json_message("[1,2,3]")


class TestCmdSendInputModes:
    """Tests for _cmd_send input mode detection."""

    def test_positional_mode(self):
        """Test tract event key=value positional syntax."""
        from types import SimpleNamespace

        from solstone.think.callosum import _cmd_send

        args = SimpleNamespace(args=["test", "ping", "data=42"])
        with patch(
            "solstone.think.callosum.callosum_send", return_value=True
        ) as mock_send:
            _cmd_send(args)
            mock_send.assert_called_once_with("test", "ping", data=42)

    def test_json_arg_mode(self):
        """Test JSON string argument mode."""
        from types import SimpleNamespace

        from solstone.think.callosum import _cmd_send

        args = SimpleNamespace(args=['{"tract":"test","event":"ping","n":1}'])
        with patch(
            "solstone.think.callosum.callosum_send", return_value=True
        ) as mock_send:
            _cmd_send(args)
            mock_send.assert_called_once_with("test", "ping", n=1)

    def test_stdin_mode(self, monkeypatch):
        """Test reading JSON from stdin."""
        import io
        from types import SimpleNamespace

        from solstone.think.callosum import _cmd_send

        args = SimpleNamespace(args=[])
        fake_stdin = io.StringIO('{"tract":"test","event":"ping"}')
        monkeypatch.setattr("solstone.think.callosum.sys.stdin", fake_stdin)

        with patch(
            "solstone.think.callosum.callosum_send", return_value=True
        ) as mock_send:
            _cmd_send(args)
            mock_send.assert_called_once_with("test", "ping")

    def test_too_few_positional_args_exits(self):
        """Test that a single positional arg (not JSON) exits with usage."""
        from types import SimpleNamespace

        from solstone.think.callosum import _cmd_send

        args = SimpleNamespace(args=["only_one"])
        with pytest.raises(SystemExit):
            _cmd_send(args)

    def test_send_failure_exits(self):
        """Test that failed send exits with code 1."""
        from types import SimpleNamespace

        from solstone.think.callosum import _cmd_send

        args = SimpleNamespace(args=["test", "ping"])
        with patch("solstone.think.callosum.callosum_send", return_value=False):
            with pytest.raises(SystemExit):
                _cmd_send(args)
