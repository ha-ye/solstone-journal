# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import threading

from solstone.think.talents import JSONEventWriter


class _BrokenPipeStdout:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        raise BrokenPipeError()


class _CollectingStdout:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None


def test_json_event_writer_emit_broken_pipe_keeps_sidecar(monkeypatch, tmp_path):
    stdout = _BrokenPipeStdout()
    monkeypatch.setattr("sys.stdout", stdout)
    sidecar = tmp_path / "events.jsonl"
    writer = JSONEventWriter(str(sidecar))

    try:
        writer.emit({"event": "error", "error": "pipe closed"})
        assert writer._pipe_dead is True
        first_write_count = len(stdout.writes)

        writer.emit({"event": "finish", "result": "sidecar only"})
        assert len(stdout.writes) == first_write_count
    finally:
        writer.close()

    rows = [json.loads(line) for line in sidecar.read_text().splitlines()]
    assert rows == [
        {"event": "error", "error": "pipe closed"},
        {"event": "finish", "result": "sidecar only"},
    ]


def test_json_event_writer_concurrent_emit_lines_are_valid_json(monkeypatch, tmp_path):
    stdout = _CollectingStdout()
    monkeypatch.setattr("sys.stdout", stdout)
    sidecar = tmp_path / "events.jsonl"
    writer = JSONEventWriter(str(sidecar))
    thread_count = 8
    iterations = 25

    def emit_many(thread_index: int) -> None:
        for iteration in range(iterations):
            writer.emit(
                {
                    "event": "progress",
                    "thread": thread_index,
                    "iteration": iteration,
                }
            )

    threads = [
        threading.Thread(target=emit_many, args=(thread_index,))
        for thread_index in range(thread_count)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
    finally:
        writer.close()

    expected_count = thread_count * iterations
    stdout_rows = [json.loads(line) for line in "".join(stdout.chunks).splitlines()]
    sidecar_rows = [
        json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()
    ]
    assert len(stdout_rows) == expected_count
    assert len(sidecar_rows) == expected_count
    assert {(row["thread"], row["iteration"]) for row in stdout_rows} == {
        (thread_index, iteration)
        for thread_index in range(thread_count)
        for iteration in range(iterations)
    }
