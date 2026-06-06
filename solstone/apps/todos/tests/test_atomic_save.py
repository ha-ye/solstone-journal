# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Crash-safety and byte-format tests for ``TodoChecklist.save()``."""

from __future__ import annotations

import json
import pathlib

import pytest

from solstone.apps.todos import todo

# Long, non-ASCII marker: guarantees the torn write produces an invalid JSON
# tail and exercises ``ensure_ascii=False`` byte preservation.
MARKER = "CRASHTEST_MARKER_ζ_long_enough_to_guarantee_a_torn_json_line"

DAY = "20260101"
FACET = "personal"


def _set_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return todo.todo_file_path(DAY, FACET)


def _prepare(path, *, overwrite):
    """Return (prior_bytes_or_None, new_bytes, checklist) for a save under test.

    Does one clean reference save (capturing the exact new bytes), then resets
    the file to its prior state so the save-under-test can be applied.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prior_bytes = b'{"text": "keep me"}\n' if overwrite else None
    if prior_bytes is not None:
        path.write_bytes(prior_bytes)

    checklist = todo.TodoChecklist.load(DAY, FACET)
    checklist.append_entry(MARKER)  # clean reference save (writes new bytes)
    new_bytes = path.read_bytes()

    # Reset disk to prior state; the in-memory checklist keeps the marker item
    # (with fixed timestamps) so the save-under-test reproduces new_bytes exactly.
    if prior_bytes is not None:
        path.write_bytes(prior_bytes)
    else:
        path.unlink()
    return prior_bytes, new_bytes, checklist


@pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "new_file"])
def test_save_torn_direct_write_is_atomic(monkeypatch, tmp_path, overwrite):
    path = _set_journal(monkeypatch, tmp_path)
    prior_bytes, new_bytes, checklist = _prepare(path, overwrite=overwrite)

    real_write_text = pathlib.Path.write_text

    def torn_write_text(self, data, *args, **kwargs):
        # Only sabotage the checklist's own whole-file write; pass everything
        # else through untouched.
        if isinstance(data, str) and MARKER in data:
            with open(self, "w", encoding="utf-8") as handle:
                handle.write(data[:-3])  # truncated -> torn final JSON line
            raise OSError("simulated crash mid-write")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", torn_write_text)

    try:
        checklist.save()
    except OSError:
        pass  # the simulated crash (pre-fix path)

    # All-or-nothing: prior intact, or fully-new — never a torn partial.
    if path.exists():
        assert path.read_bytes() in (prior_bytes, new_bytes)
    else:
        assert not overwrite  # only the new-file case may be absent
    assert list(path.parent.glob(".tmp_*")) == []


@pytest.mark.parametrize("overwrite", [True, False], ids=["overwrite", "new_file"])
def test_save_interrupted_atomic_replace_preserves_prior(
    monkeypatch, tmp_path, overwrite
):
    path = _set_journal(monkeypatch, tmp_path)
    prior_bytes, _new_bytes, checklist = _prepare(path, overwrite=overwrite)

    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", boom)

    with pytest.raises(OSError):
        checklist.save()

    if overwrite:
        assert path.read_bytes() == prior_bytes
    else:
        assert not path.exists()
    assert list(path.parent.glob(".tmp_*")) == []


def test_save_preserves_exact_bytes(monkeypatch, tmp_path):
    path = _set_journal(monkeypatch, tmp_path)

    checklist = todo.TodoChecklist.load(DAY, FACET)
    checklist.append_entry("café ✅ \U0001f44d")  # accents + emoji
    raw = path.read_bytes()

    # Non-ASCII is stored literally as UTF-8, never \uXXXX-escaped.
    assert "café ✅ \U0001f44d".encode("utf-8") in raw
    assert b"\\u" not in raw
    # Round-trips back to the same text.
    record = json.loads(raw.decode("utf-8").splitlines()[0])
    assert record["text"] == "café ✅ \U0001f44d"

    # Empty checklist -> empty file.
    empty = todo.TodoChecklist(
        day=DAY,
        facet="work",
        path=todo.todo_file_path(DAY, "work"),
        items=[],
        exists=False,
    )
    empty.save()
    assert empty.path.read_bytes() == b""
