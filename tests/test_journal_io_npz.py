# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Acceptance tests for solstone.think.journal_io.npz."""

from __future__ import annotations

import ast
import fcntl
import json
import multiprocessing as mp
from pathlib import Path

import numpy as np
import pytest

from solstone.think.journal_io.errors import LockTimeout, MalformedDataError
from solstone.think.journal_io.locking import hold_lock as real_hold_lock
from solstone.think.journal_io.npz import load_npz, save_npz, update_npz, write_npz

REPO_ROOT = Path(__file__).resolve().parents[1]
VOICEPRINT_KEYS = ("embeddings", "metadata")


def _voiceprint_arrays(ids: list[int]) -> dict[str, np.ndarray]:
    embeddings = np.asarray(
        [[float(item)] + [0.0] * 255 for item in ids],
        dtype=np.float32,
    )
    metadata = np.asarray(
        [json.dumps({"id": item}) for item in ids],
        dtype=str,
    )
    return {"embeddings": embeddings, "metadata": metadata}


def _append_voiceprint(path: Path, item: int) -> None:
    def transform(current: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        if current:
            embeddings = current["embeddings"]
            metadata = current["metadata"]
        else:
            embeddings = np.empty((0, 256), dtype=np.float32)
            metadata = np.asarray([], dtype=str)
        row = np.asarray([[float(item)] + [0.0] * 255], dtype=np.float32)
        meta = np.asarray([json.dumps({"id": item})], dtype=str)
        return {
            "embeddings": np.vstack([embeddings, row]),
            "metadata": np.append(metadata, meta),
        }

    update_npz(path, transform, expected_keys=VOICEPRINT_KEYS)


def _append_worker(path_str: str, item: int) -> None:
    _append_voiceprint(Path(path_str), item)


def _assert_voiceprint_pairing(path: Path, expected_ids: set[int]) -> None:
    loaded = load_npz(path)
    assert loaded is not None
    embeddings = loaded["embeddings"]
    metadata = loaded["metadata"]
    assert embeddings.shape == (len(expected_ids), 256)
    assert metadata.shape == (len(expected_ids),)
    observed: dict[int, float] = {}
    for emb, meta_raw in zip(embeddings, metadata, strict=True):
        meta = json.loads(str(meta_raw))
        observed[int(meta["id"])] = float(emb[0])
    assert set(observed) == expected_ids
    for item in expected_ids:
        assert observed[item] == pytest.approx(float(item))


def test_load_npz_missing_and_old_writer_round_trip(tmp_path) -> None:
    path = tmp_path / "voiceprints.npz"
    assert load_npz(path) is None

    old_arrays = _voiceprint_arrays([1, 2])
    np.savez_compressed(path, **old_arrays)

    loaded = load_npz(path)
    assert loaded is not None
    assert set(loaded) == {"embeddings", "metadata"}
    assert loaded["embeddings"].dtype == np.float32
    assert loaded["metadata"].dtype.kind == "U"
    np.testing.assert_array_equal(loaded["embeddings"], old_arrays["embeddings"])
    np.testing.assert_array_equal(loaded["metadata"], old_arrays["metadata"])


def test_save_npz_owner_centroid_schema_old_reader_compatible(tmp_path) -> None:
    path = tmp_path / "owner_centroid.npz"
    arrays = {
        "centroid": np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        "cluster_size": np.array(42, dtype=np.int32),
        "threshold": np.array(0.85, dtype=np.float32),
        "last_refreshed_at": np.array("2026-03-19T12:00:00Z"),
    }

    save_npz(
        path,
        arrays,
        expected_keys=("centroid", "cluster_size", "threshold", "last_refreshed_at"),
    )

    with np.load(path, allow_pickle=False) as data:
        assert set(data.files) == set(arrays)
        assert data["centroid"].dtype == np.float32
        assert data["cluster_size"].dtype == np.int32
        assert data["threshold"].dtype == np.float32
        np.testing.assert_array_equal(data["centroid"], arrays["centroid"])
        assert int(np.asarray(data["cluster_size"]).item()) == 42
        assert float(np.asarray(data["threshold"]).item()) == pytest.approx(0.85)
        assert str(np.asarray(data["last_refreshed_at"]).item()).endswith("Z")


def test_save_npz_owner_candidate_schema_old_reader_compatible(tmp_path) -> None:
    path = tmp_path / "owner_candidate.npz"
    arrays = {
        "centroid": np.asarray([0.0, 1.0, 0.0], dtype=np.float32),
        "cluster_size": np.array(52, dtype=np.int32),
        "threshold": np.array(0.85, dtype=np.float32),
        "version": np.array("2026-03-19T12:00:00Z"),
    }

    save_npz(
        path,
        arrays,
        expected_keys=("centroid", "cluster_size", "threshold", "version"),
    )

    with np.load(path, allow_pickle=False) as data:
        assert set(data.files) == set(arrays)
        assert data["centroid"].dtype == np.float32
        assert data["cluster_size"].dtype == np.int32
        assert data["threshold"].dtype == np.float32
        np.testing.assert_array_equal(data["centroid"], arrays["centroid"])
        assert int(np.asarray(data["cluster_size"]).item()) == 52
        assert str(np.asarray(data["version"]).item()).endswith("Z")


def test_write_npz_round_trip(tmp_path) -> None:
    path = tmp_path / "voiceprints.npz"
    arrays = _voiceprint_arrays([1, 2])

    write_npz(path, arrays, expected_keys=VOICEPRINT_KEYS)

    loaded = load_npz(path)
    assert loaded is not None
    assert set(loaded) == set(arrays)
    for key, expected in arrays.items():
        np.testing.assert_array_equal(loaded[key], expected)


def test_write_npz_reload_verify_missing_key_raises_malformed(tmp_path) -> None:
    path = tmp_path / "broken.npz"

    with pytest.raises(MalformedDataError) as error:
        write_npz(
            path,
            {"embeddings": np.empty((0, 256), dtype=np.float32)},
            expected_keys=VOICEPRINT_KEYS,
        )

    assert error.value.path == path


def test_write_npz_leaves_no_lock_sidecar(tmp_path) -> None:
    path = tmp_path / "voiceprints.npz"

    write_npz(path, _voiceprint_arrays([1]), expected_keys=VOICEPRINT_KEYS)

    assert path.exists()
    assert not (path.parent / f"{path.name}.lock").exists()
    assert list(path.parent.glob("*.lock")) == []
    assert list(path.parent.glob(".tmp_*")) == []
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]


def test_update_npz_append_preserves_embedding_metadata_pairing(tmp_path) -> None:
    path = tmp_path / "voiceprints.npz"

    _append_voiceprint(path, 7)
    _append_voiceprint(path, 3)
    _append_voiceprint(path, 11)

    _assert_voiceprint_pairing(path, {3, 7, 11})


def test_update_npz_multiprocess_append_preserves_pairing(tmp_path) -> None:
    path = tmp_path / "voiceprints.npz"
    process_count = 8
    ctx = mp.get_context("spawn")
    processes = [
        ctx.Process(target=_append_worker, args=(str(path), item))
        for item in range(process_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(45)
    for process in processes:
        assert not process.is_alive()
        assert process.exitcode == 0

    _assert_voiceprint_pairing(path, set(range(process_count)))
    assert (path.parent / f"{path.name}.lock").exists()


def test_save_npz_failed_replace_leaves_prior_file_intact(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "voiceprints.npz"
    save_npz(path, _voiceprint_arrays([1]), expected_keys=VOICEPRINT_KEYS)
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", boom)

    with pytest.raises(OSError):
        save_npz(path, _voiceprint_arrays([2]), expected_keys=VOICEPRINT_KEYS)

    assert path.read_bytes() == before
    assert list(path.parent.glob(".tmp_*")) == []
    _assert_voiceprint_pairing(path, {1})


def test_new_write_loads_old_and_old_write_loads_new(tmp_path) -> None:
    new_path = tmp_path / "new.npz"
    old_path = tmp_path / "old.npz"

    save_npz(new_path, _voiceprint_arrays([4]), expected_keys=VOICEPRINT_KEYS)
    with np.load(new_path, allow_pickle=False) as data:
        assert set(data.files) == {"embeddings", "metadata"}
        assert float(data["embeddings"][0][0]) == pytest.approx(4.0)
        assert json.loads(str(data["metadata"][0])) == {"id": 4}

    old_arrays = _voiceprint_arrays([9])
    np.savez_compressed(old_path, **old_arrays)
    loaded = load_npz(old_path)
    assert loaded is not None
    np.testing.assert_array_equal(loaded["embeddings"], old_arrays["embeddings"])
    np.testing.assert_array_equal(loaded["metadata"], old_arrays["metadata"])


def test_save_npz_lock_timeout_raises_typed_error(tmp_path, monkeypatch) -> None:
    import solstone.think.journal_io.npz as npz_module

    path = tmp_path / "voiceprints.npz"
    lock_path = path.parent / f"{path.name}.lock"
    lock_file = open(lock_path, "w")

    def short_hold_lock(target: Path):
        return real_hold_lock(target, timeout=0.1, poll_interval=0.01)

    monkeypatch.setattr(npz_module, "hold_lock", short_hold_lock)
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    try:
        with pytest.raises(LockTimeout) as error:
            save_npz(path, _voiceprint_arrays([1]), expected_keys=VOICEPRINT_KEYS)
        assert error.value.path == path
        assert error.value.timeout == 0.1
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def test_update_npz_empty_transform_unlinks_file(tmp_path) -> None:
    path = tmp_path / "voiceprints.npz"
    save_npz(path, _voiceprint_arrays([1]), expected_keys=VOICEPRINT_KEYS)

    update_npz(path, lambda _current: {}, expected_keys=VOICEPRINT_KEYS)

    assert not path.exists()


def test_update_npz_none_transform_leaves_file_unchanged(tmp_path) -> None:
    path = tmp_path / "voiceprints.npz"
    save_npz(path, _voiceprint_arrays([1]), expected_keys=VOICEPRINT_KEYS)
    before = path.read_bytes()

    update_npz(path, lambda _current: None, expected_keys=VOICEPRINT_KEYS)

    assert path.read_bytes() == before
    _assert_voiceprint_pairing(path, {1})


def test_save_npz_reload_verify_missing_key_raises_malformed(tmp_path) -> None:
    path = tmp_path / "broken.npz"

    with pytest.raises(MalformedDataError) as error:
        save_npz(
            path,
            {"embeddings": np.empty((0, 256), dtype=np.float32)},
            expected_keys=VOICEPRINT_KEYS,
        )

    assert error.value.path == path


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base:
            return f"{base}.{node.attr}"
    return None


def test_non_test_np_load_calls_pin_allow_pickle_false() -> None:
    failures: list[str] = []
    excluded = Path("solstone/think/journal_io/npz.py")
    for path in sorted((REPO_ROOT / "solstone").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel == excluded or "tests" in rel.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _dotted_name(node.func) not in {"np.load", "numpy.load"}:
                continue
            allow_pickle_false = any(
                keyword.arg == "allow_pickle"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in node.keywords
            )
            if not allow_pickle_false:
                failures.append(f"{rel.as_posix()}:{node.lineno}")

    assert failures == []
