# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tests import verify_speaker_discovery_clustering_differential as harness


def _seeded_discovery_matrix(
    rows: int, seed: int, cols: int = 256
) -> tuple[np.ndarray, int]:
    rng = np.random.default_rng(seed)
    marginal = 5
    n_groups = max(3, (rows - marginal) // 60)
    sizes = [(rows - marginal) // n_groups] * n_groups
    sizes[0] += (rows - marginal) - sum(sizes)
    sizes.append(marginal)
    centers = rng.normal(size=(len(sizes), cols))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)

    groups = []
    for index, size in enumerate(sizes):
        # Spread is total displacement from the unit-norm center. Divide by
        # sqrt(cols), or a 256-dim "tight" group becomes a broad overlapping
        # blob because every coordinate contributes variance.
        total_spread = 0.45 if index == len(sizes) - 1 else 0.12
        spread = total_spread / np.sqrt(cols)
        groups.append(centers[index] + rng.normal(scale=spread, size=(size, cols)))

    matrix = np.vstack(groups).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix.astype(np.float32), len(sizes)


def test_label_permutation_compares_equal() -> None:
    report = harness._compare_clustering(
        np.asarray([0, 0, 1, 1, -1], dtype=np.int64),
        np.asarray([8, 8, 3, 3, -1], dtype=np.int64),
        cols=2,
    )

    assert report["classification"] == harness.SHIP
    assert report["partition_equal_up_to_relabelling"] is True
    assert report["noise_to_clustered"] == []
    assert report["clustered_to_noise"] == []
    assert report["cluster_to_cluster_moves"] == []


def test_noise_flip_classifies_separately() -> None:
    left = np.asarray([0] * 500 + [1] * 500, dtype=np.int64)
    right = left.copy()
    right[0] = harness.NOISE

    report = harness._compare_clustering(left, right, cols=256)

    assert report["classification"] == harness.INVESTIGATE
    assert report["noise_to_clustered"] == []
    assert report["clustered_to_noise"] == [0]
    assert report["cluster_to_cluster_moves"] == []


def test_cluster_to_cluster_move_fails_structurally() -> None:
    report = harness._compare_clustering(
        np.asarray([0, 0, 1, 1], dtype=np.int64),
        np.asarray([0, 1, 1, 1], dtype=np.int64),
        cols=2,
    )

    assert report["classification"] == harness.TRAILS_THE_WAVE
    assert report["cluster_to_cluster_moves"]


def test_seeded_discovery_matrix_is_unit_normalized_npz_input(tmp_path: Path) -> None:
    matrix, expected_clusters = _seeded_discovery_matrix(200, seed=11)

    assert matrix.shape == (200, 256)
    assert matrix.dtype == np.float32
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-6)

    matrix_path = tmp_path / "matrix.npz"
    np.savez(matrix_path, embeddings=matrix)

    loaded = harness.load_matrix(matrix_path)
    labels = harness.run_sklearn(loaded)

    assert loaded.flags["C_CONTIGUOUS"]
    assert harness._cluster_count(labels) == expected_clusters
    assert harness._noise_count(labels) == 0


def test_refuses_in_repo_report_destination() -> None:
    with pytest.raises(harness.HarnessError):
        harness._refuse_repo_destination(harness.ROOT / "report.json")


def test_rust_bin_is_required() -> None:
    with pytest.raises(SystemExit) as exc:
        harness.parse_args(["matrix.npz"])

    assert exc.value.code == 2


def test_mode_defaults_to_production_path() -> None:
    args = harness.parse_args(["matrix.npz", "--rust-bin", "/tmp/helper"])

    assert args.mode == harness.MODE_PRODUCTION_PATH


def test_provenance_records_native_mode() -> None:
    report = harness._base_report(
        matrix_path=Path("matrix.npz"),
        rust_bin=Path("/tmp/helper"),
        mode=harness.MODE_DIRECT_BINARY,
    )

    assert report["provenance"]["inputs"]["mode"] == harness.MODE_DIRECT_BINARY
