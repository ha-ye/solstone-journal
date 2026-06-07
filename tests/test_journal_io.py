# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Acceptance tests for solstone.think.journal_io."""

import fcntl
import importlib
import json
import logging
import multiprocessing as mp
import os
import stat
import sys
from pathlib import Path

import pytest

from solstone.think.journal_io.append import append_jsonl
from solstone.think.journal_io.atomic import (
    atomic_replace,
    install_file,
    write_json,
    write_jsonl,
    write_text,
)
from solstone.think.journal_io.errors import (
    LockTimeout,
    MalformedDataError,
    PathEscapeError,
)
from solstone.think.journal_io.locking import hold_lock
from solstone.think.journal_io.paths import contained_path
from solstone.think.journal_io.readers import (
    MalformedPolicy,
    read_json,
    read_jsonl,
)


def _journal_io_increment_worker(target_str: str, iterations: int) -> None:
    target = Path(target_str)
    for _ in range(iterations):
        with hold_lock(target, timeout=30):
            value = read_json(target, default=0)
            write_json(target, value + 1)


def _run_increment_processes(target: Path) -> None:
    process_count = 4
    iterations = 25
    ctx = mp.get_context("spawn")
    processes = [
        ctx.Process(
            target=_journal_io_increment_worker,
            args=(str(target), iterations),
        )
        for _ in range(process_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(45)
    for process in processes:
        assert not process.is_alive()
        assert process.exitcode == 0
    assert read_json(target) == process_count * iterations


def test_atomic_replace_crash_safe(tmp_path, monkeypatch) -> None:
    """§6.1: atomic_replace preserves the old file and cleans temps on failure."""
    path = tmp_path / "data.txt"
    path.write_bytes(b"OLD")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", boom)

    with pytest.raises(OSError):
        atomic_replace(path, "NEW")

    assert path.read_bytes() == b"OLD"
    assert list(path.parent.glob(".tmp_*")) == []


def test_install_file_installs_streamed_temp(tmp_path) -> None:
    dest = tmp_path / "audio.opus"
    temp = tmp_path / "incoming.part"
    temp.write_bytes(b"streamed-bytes")

    install_file(temp, dest)

    assert dest.read_bytes() == b"streamed-bytes"
    assert not temp.exists()
    assert list(tmp_path.glob(".tmp_*")) == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["audio.opus"]


def test_install_file_crash_safe(tmp_path, monkeypatch) -> None:
    dest = tmp_path / "audio.opus"
    dest.write_bytes(b"OLD")
    temp = tmp_path / "incoming.part"
    temp.write_bytes(b"NEW")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", boom)

    with pytest.raises(OSError):
        install_file(temp, dest)

    assert dest.read_bytes() == b"OLD"
    assert not temp.exists()


def test_install_file_applies_mode_before_replace(tmp_path, monkeypatch) -> None:
    dest = tmp_path / "secret.opus"
    temp = tmp_path / "incoming.part"
    temp.write_bytes(b"secret")
    captured = {}
    real_replace = os.replace

    def spy(src, dst):
        captured["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        return real_replace(src, dst)

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", spy)

    install_file(temp, dest, mode=0o600)

    assert captured["mode"] == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_install_file_never_reads_content_into_memory(tmp_path, monkeypatch) -> None:
    dest = tmp_path / "audio.opus"
    temp = tmp_path / "incoming.part"
    temp.write_bytes(b"streamed-bytes")

    def fail_read(*args, **kwargs):
        raise AssertionError("install_file must not read content")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.read", fail_read)

    install_file(temp, dest)

    assert dest.read_bytes() == b"streamed-bytes"


def test_parent_dir_fsync_degraded_policy(tmp_path, monkeypatch, caplog) -> None:
    """§6.2: parent-dir fsync failure degrades by warning after write success."""
    path = tmp_path / "data.txt"
    real_open = os.open

    def fake_open(p, flags, *args, **kwargs):
        if flags & os.O_DIRECTORY:
            raise OSError("no dir fsync")
        return real_open(p, flags, *args, **kwargs)

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.open", fake_open)

    with caplog.at_level(logging.WARNING, logger="solstone.think.journal_io.atomic"):
        atomic_replace(path, "data")

    assert path.read_text(encoding="utf-8") == "data"
    assert any("degraded" in record.message for record in caplog.records)


def test_atomic_replace_applies_mode_before_replace(tmp_path, monkeypatch) -> None:
    """Atomic mode bonus: mode is set on the temp before replacement."""
    path = tmp_path / "secret.txt"
    captured = {}
    real_replace = os.replace

    def spy(src, dst):
        captured["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        return real_replace(src, dst)

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", spy)

    atomic_replace(path, "secret", mode=0o600)

    assert captured["mode"] == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_wrappers_are_thin(tmp_path, monkeypatch) -> None:
    """Atomic wrapper bonus: JSON, text, and JSONL wrappers delegate thinly."""
    import solstone.think.journal_io.atomic as atomic_module

    calls = []

    def spy(path, data, *, mode=None):
        calls.append((path, data, mode))

    monkeypatch.setattr(atomic_module, "atomic_replace", spy)

    json_path = tmp_path / "data.json"
    text_path = tmp_path / "data.txt"
    jsonl_path = tmp_path / "data.jsonl"

    write_json(json_path, {"a": 1})
    write_text(text_path, "hello", mode=0o644)
    write_jsonl(jsonl_path, [{"a": 1}, {"b": 2}], mode=0o600)

    assert calls == [
        (json_path, json.dumps({"a": 1}, indent=2, sort_keys=False) + "\n", None),
        (text_path, "hello", 0o644),
        (
            jsonl_path,
            json.dumps({"a": 1}) + "\n" + json.dumps({"b": 2}) + "\n",
            0o600,
        ),
    ]


def test_malformed_policy_json_and_jsonl(tmp_path, caplog) -> None:
    """§6.5: JSON and JSONL malformed handling supports raise/skip/warn."""
    good_json = tmp_path / "good.json"
    bad_json = tmp_path / "bad.json"
    jsonl_path = tmp_path / "data.jsonl"
    sentinel = object()

    good_json.write_text(json.dumps({"ok": True}), encoding="utf-8")
    bad_json.write_text("{bad", encoding="utf-8")
    jsonl_path.write_text('{"i": 1}\n{bad\n{"i": 2}\n', encoding="utf-8")

    for policy in MalformedPolicy:
        assert read_json(good_json, on_error=policy) == {"ok": True}

    with pytest.raises(MalformedDataError) as json_error:
        read_json(bad_json)
    assert json_error.value.path == bad_json

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="solstone.think.journal_io.readers"):
        assert (
            read_json(bad_json, on_error=MalformedPolicy.SKIP, default=sentinel)
            is sentinel
        )
    assert caplog.records == []

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="solstone.think.journal_io.readers"):
        assert (
            read_json(
                bad_json, on_error=MalformedPolicy.WARN_AND_SKIP, default=sentinel
            )
            is sentinel
        )
    assert any(record.levelno == logging.WARNING for record in caplog.records)

    with pytest.raises(MalformedDataError) as jsonl_error:
        read_jsonl(jsonl_path)
    assert jsonl_error.value.path == jsonl_path
    assert jsonl_error.value.lineno == 2

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="solstone.think.journal_io.readers"):
        assert read_jsonl(jsonl_path, on_error=MalformedPolicy.SKIP) == [
            {"i": 1},
            {"i": 2},
        ]
    assert caplog.records == []

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="solstone.think.journal_io.readers"):
        assert read_jsonl(jsonl_path, on_error=MalformedPolicy.WARN_AND_SKIP) == [
            {"i": 1},
            {"i": 2},
        ]
    assert any("line 2" in record.message for record in caplog.records)


def test_missing_and_empty_bypass_malformed_policy(tmp_path, caplog) -> None:
    """§6.6: missing and zero-byte files bypass malformed-policy handling."""
    missing_json = tmp_path / "missing.json"
    empty_json = tmp_path / "empty.json"
    missing_jsonl = tmp_path / "missing.jsonl"
    empty_jsonl = tmp_path / "empty.jsonl"
    malformed_json = tmp_path / "malformed.json"
    sentinel = object()

    empty_json.write_text("", encoding="utf-8")
    empty_jsonl.write_text("", encoding="utf-8")
    malformed_json.write_text("{bad", encoding="utf-8")

    assert read_json(missing_json, default=sentinel) is sentinel
    assert read_json(empty_json, default=sentinel) is sentinel
    assert read_jsonl(missing_jsonl) == []
    assert read_jsonl(empty_jsonl) == []

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="solstone.think.journal_io.readers"):
        assert (
            read_json(
                missing_json, on_error=MalformedPolicy.WARN_AND_SKIP, default=sentinel
            )
            is sentinel
        )
        assert (
            read_json(
                empty_json, on_error=MalformedPolicy.WARN_AND_SKIP, default=sentinel
            )
            is sentinel
        )
        assert read_jsonl(missing_jsonl, on_error=MalformedPolicy.WARN_AND_SKIP) == []
        assert read_jsonl(empty_jsonl, on_error=MalformedPolicy.WARN_AND_SKIP) == []
    assert caplog.records == []

    with pytest.raises(MalformedDataError):
        read_json(malformed_json)


def test_append_durable_record_boundary(tmp_path, monkeypatch) -> None:
    """§6.7: append writers fsync each complete record and preserve boundaries."""
    path = tmp_path / "events.jsonl"
    calls = []
    real_fsync = os.fsync

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr("solstone.think.journal_io.append.os.fsync", spy)

    append_jsonl(path, {"i": 1})
    append_jsonl(path, {"i": 2})

    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert [json.loads(line) for line in lines] == [{"i": 1}, {"i": 2}]
    assert content.endswith("\n")
    assert len(lines) == 2
    assert len(calls) >= 2


def test_contained_path_rejects_escape_accepts_legit(tmp_path) -> None:
    """§6.8: contained_path accepts contained paths and rejects escapes."""
    journal = tmp_path
    root_real = Path(os.path.realpath(str(journal)))

    accepted = contained_path(str(journal), "facets/work/facet.json")
    assert accepted.is_relative_to(root_real)

    with pytest.raises(ValueError):
        contained_path(str(journal), "../escape")
    with pytest.raises(ValueError):
        contained_path(str(journal), str(tmp_path / "absolute"))

    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    os.symlink(outside, journal / "out")

    with pytest.raises(PathEscapeError):
        contained_path(str(journal), "out/secret.txt")

    internal = journal / "real"
    internal.mkdir()
    os.symlink(internal, journal / "alias")

    alias_path = contained_path(str(journal), "alias/file.txt")
    assert alias_path.is_relative_to(root_real)


def test_core_imports_no_numpy(monkeypatch) -> None:
    """§6.9: journal_io core imports do not load numpy."""
    monkeypatch.delitem(sys.modules, "numpy", raising=False)
    for module_name in list(sys.modules):
        if module_name.startswith("solstone.think.journal_io"):
            monkeypatch.delitem(sys.modules, module_name, raising=False)

    importlib.import_module("solstone.think.journal_io")
    for module_name in (
        "solstone.think.journal_io.atomic",
        "solstone.think.journal_io.readers",
        "solstone.think.journal_io.append",
        "solstone.think.journal_io.locking",
        "solstone.think.journal_io.paths",
    ):
        importlib.import_module(module_name)

    assert "numpy" not in sys.modules


def test_hold_lock_times_out_with_typed_error(tmp_path) -> None:
    """§6.4: hold_lock times out with LockTimeout attributes under contention."""
    target = tmp_path / "data.json"
    lock_path = tmp_path / "data.json.lock"
    manual_lock = open(lock_path, "w")
    fcntl.flock(manual_lock, fcntl.LOCK_EX)
    try:
        with pytest.raises(LockTimeout) as error:
            with hold_lock(target, timeout=0.2):
                pass
        assert error.value.path == target
        assert error.value.timeout == 0.2
    finally:
        fcntl.flock(manual_lock, fcntl.LOCK_UN)
        manual_lock.close()


def test_locked_rmw_cross_process_cold_start(tmp_path) -> None:
    """§6.3: locked RMW preserves increments across real child processes."""
    cold_target = tmp_path / "cold" / "counter.json"
    assert not cold_target.exists()
    assert not (cold_target.parent / f"{cold_target.name}.lock").exists()

    _run_increment_processes(cold_target)

    warm_target = tmp_path / "warm" / "counter.json"
    write_json(warm_target, 0)
    (warm_target.parent / f"{warm_target.name}.lock").touch()

    _run_increment_processes(warm_target)
