# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import logging
from pathlib import Path

import pytest


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    yield

    for handler in list(root.handlers):
        if handler not in original_handlers:
            handler.flush()
            handler.close()
    root.handlers = original_handlers
    root.setLevel(original_level)


def _flush_root_handlers() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_rotating_supervisor_logging_bounds_file_count_and_total_bytes(
    tmp_path, restore_root_logging
):
    mod = importlib.import_module("solstone.think.supervisor")
    log_path = tmp_path / "health" / "supervisor.log"
    log_path.parent.mkdir()
    max_bytes = 2000
    backup_count = 2

    mod._configure_supervisor_logging(
        log_path,
        logging.INFO,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )

    for index in range(300):
        logging.info("rotation line %03d", index)
    _flush_root_handlers()

    log_files = list(log_path.parent.glob("supervisor.log*"))
    total_bytes = sum(path.stat().st_size for path in log_files)

    assert len(log_files) <= backup_count + 1
    assert total_bytes <= (backup_count + 1) * max_bytes


def test_configure_supervisor_logging_preserves_format_and_level(
    tmp_path, restore_root_logging
):
    from solstone.think.logs_cli import parse_log_line

    mod = importlib.import_module("solstone.think.supervisor")
    log_path = tmp_path / "health" / "supervisor.log"
    log_path.parent.mkdir()

    mod._configure_supervisor_logging(
        log_path,
        logging.DEBUG,
        max_bytes=2000,
        backup_count=1,
    )
    logging.debug("format sentinel")
    _flush_root_handlers()

    assert logging.getLogger().level == logging.DEBUG
    line = log_path.read_text(encoding="utf-8").splitlines()[0]
    parsed = parse_log_line(line)
    assert parsed is not None
    assert parsed.service == "supervisor"
    assert parsed.stream == "log"
    assert parsed.message == "DEBUG format sentinel"


def test_compact_log_if_oversized_keeps_tail_complete_lines(tmp_path):
    from solstone.think.logs_cli import parse_log_line

    mod = importlib.import_module("solstone.think.supervisor")
    log_path = tmp_path / "health" / "supervisor.log"
    log_path.parent.mkdir()
    max_bytes = 500
    lines = [
        f"2026-02-09T10:00:{index:02d} [supervisor:log] INFO line {index:02d}"
        for index in range(40)
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mod._compact_log_if_oversized(log_path, max_bytes)

    assert log_path.stat().st_size < max_bytes
    surviving_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert any("line 39" in line for line in surviving_lines)
    assert all("line 00" not in line for line in surviving_lines)
    assert surviving_lines
    for line in surviving_lines:
        assert parse_log_line(line) is not None


def test_compact_log_if_oversized_noops_under_cap_and_missing(tmp_path):
    mod = importlib.import_module("solstone.think.supervisor")
    log_path = tmp_path / "health" / "supervisor.log"
    log_path.parent.mkdir()
    original = b"2026-02-09T10:00:00 [supervisor:log] INFO under cap\n"
    log_path.write_bytes(original)

    mod._compact_log_if_oversized(log_path, max_bytes=2000)

    assert log_path.read_bytes() == original

    missing_path = tmp_path / "health" / "missing.log"
    mod._compact_log_if_oversized(missing_path, max_bytes=2000)

    assert not missing_path.exists()
    assert not missing_path.with_name(missing_path.name + ".compact").exists()


def test_compact_log_if_oversized_warns_and_continues_on_oserror(
    tmp_path, monkeypatch, caplog
):
    mod = importlib.import_module("solstone.think.supervisor")
    log_path = tmp_path / "health" / "supervisor.log"
    log_path.parent.mkdir()
    original = b"x" * 3000
    log_path.write_bytes(original)
    original_open = Path.open

    def fail_read(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == log_path and mode == "rb":
            raise OSError("locked")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_read)

    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        mod._compact_log_if_oversized(log_path, max_bytes=2000)

    with original_open(log_path, "rb") as handle:
        assert handle.read() == original
    assert "Could not compact oversized supervisor log" in caplog.text


def test_sandbox_redirect_uses_service_log_not_supervisor_log():
    makefile = Path(__file__).resolve().parents[1] / "Makefile"
    text = makefile.read_text(encoding="utf-8")
    start = text.index("sandbox: .installed")
    end = text.index("sandbox-stop:", start)
    sandbox_block = text[start:end]

    assert '> "$$SANDBOX_JOURNAL/health/service.log" 2>&1 &' in sandbox_block
    assert '> "$$SANDBOX_JOURNAL/health/supervisor.log" 2>&1 &' not in sandbox_block
