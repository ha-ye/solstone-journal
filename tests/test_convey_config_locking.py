# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import multiprocessing
import os
import time
import traceback
from pathlib import Path
from queue import Empty
from typing import Any

import pytest


def _write_convey(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _seed_cross_writer_journal(journal: Path) -> Path:
    config_path = journal / "config" / "convey.json"
    _write_convey(
        config_path,
        {
            "facets": {"order": ["a", "b"], "selected": "a"},
            "apps": {"order": ["x"]},
        },
    )
    facet_dir = journal / "facets" / "b"
    facet_dir.mkdir(parents=True, exist_ok=True)
    (facet_dir / "facet.json").write_text(
        json.dumps(
            {
                "title": "B",
                "description": "",
                "color": "#667eea",
                "emoji": "📦",
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _delete_facet_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    delay: float,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        barrier.wait(timeout=5)
        if delay:
            time.sleep(delay)

        from solstone.think.facets import delete_facet

        delete_facet("b")
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _convey_config_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    delay: float,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        barrier.wait(timeout=5)
        if delay:
            time.sleep(delay)

        import solstone.convey.state as convey_state
        from solstone.convey.config import locked_modify_convey_config

        convey_state.journal_root = journal_path

        def _transform(config: dict[str, Any]) -> dict[str, Any]:
            config.setdefault("apps", {})["order"] = ["x", "y"]
            return config

        locked_modify_convey_config(_transform)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _drain_errors(errors: Any) -> list[str]:
    found = []
    while True:
        try:
            found.append(errors.get_nowait())
        except Empty:
            return found


def _join_processes(processes: list[Any], errors: Any) -> None:
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    error_text = "\n".join(_drain_errors(errors))
    assert all(not process.is_alive() for process in processes), error_text
    assert all(process.exitcode == 0 for process in processes), error_text


def test_convey_config_atomic_failure_preserves_existing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "journal"
    config_path = journal / "config" / "convey.json"
    original = {
        "facets": {"order": ["a"], "selected": "a"},
        "apps": {"order": ["x"]},
    }
    _write_convey(config_path, original)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    import solstone.convey.state as convey_state
    from solstone.convey.config import locked_modify_convey_config

    convey_state.journal_root = str(journal)
    original_bytes = config_path.read_bytes()

    def fail_replace(_src: str, _dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", fail_replace)

    with pytest.raises(OSError):
        locked_modify_convey_config(lambda config: {**config, "apps": {"order": ["z"]}})

    assert config_path.read_bytes() == original_bytes
    assert list(config_path.parent.glob(".tmp_*")) == []


@pytest.mark.parametrize(
    ("delete_delay", "convey_delay"),
    [(0.0, 0.2), (0.2, 0.0)],
)
def test_convey_config_lock_shared_with_think_facet_delete(
    tmp_path: Path,
    delete_delay: float,
    convey_delay: float,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / f"journal-{delete_delay}-{convey_delay}"
    config_path = _seed_cross_writer_journal(journal)
    barrier = ctx.Barrier(2)
    errors = ctx.Queue()
    processes = [
        ctx.Process(
            target=_delete_facet_worker,
            args=(str(journal), barrier, errors, delete_delay),
        ),
        ctx.Process(
            target=_convey_config_worker,
            args=(str(journal), barrier, errors, convey_delay),
        ),
    ]

    _join_processes(processes, errors)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["facets"]["order"] == ["a"]
    assert data["apps"]["order"] == ["x", "y"]
    assert not (journal / "facets" / "b").exists()
