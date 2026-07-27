# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Differential harness for unknown-speaker discovery clustering."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import sklearn
from sklearn.cluster import HDBSCAN

from solstone.apps.speakers.discovery import MIN_CLUSTER_SIZE, MIN_SAMPLES
from solstone.think.utils import get_rev

ROOT = Path(__file__).resolve().parent.parent
REPORT_SCHEMA = "solstone-speaker-discovery-clustering-differential-report"
SCHEMA_VERSION = 1

REQUEST_SCHEMA = "solstone-speaker-discovery-cluster-request-v1"
RESPONSE_SCHEMA = "solstone-speaker-discovery-cluster-response-v1"
PAYLOAD_FORMAT = "raw-f32le-row-major-v1"
DTYPE_F32LE = "float32-le"
RUST_COMMAND = "discovery-cluster"

NOISE = -1

SHIP = "ship"
INVESTIGATE = "investigate"
TRAILS_THE_WAVE = "trails-the-wave"
HARNESS_ERROR = "harness-error"


class HarnessError(RuntimeError):
    """Raised when the harness cannot produce a trustworthy comparison."""


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _render_report(report: dict[str, Any]) -> str:
    return json.dumps(report, default=_json_default, indent=2, sort_keys=True) + "\n"


def _refuse_repo_destination(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or resolved.is_relative_to(ROOT):
        raise HarnessError(
            f"speaker differential refuses in-repo destination: {resolved}"
        )
    return resolved


def _provenance(*, matrix_path: Path | None, rust_bin: Path | None) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "harness": {
            "name": "tests.verify_speaker_discovery_clustering_differential",
            "repo_commit": get_rev(),
            "schema_version": SCHEMA_VERSION,
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "versions": {
            "numpy": np.__version__,
            "sklearn": sklearn.__version__,
        },
        "inputs": {
            "matrix_path": str(matrix_path) if matrix_path is not None else None,
            "rust_bin": str(rust_bin) if rust_bin is not None else None,
        },
    }


def _base_report(
    *, matrix_path: Path | None = None, rust_bin: Path | None = None
) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "classification": HARNESS_ERROR,
        "failure": None,
        "provenance": _provenance(matrix_path=matrix_path, rust_bin=rust_bin),
        "parameters": {
            "min_cluster_size": MIN_CLUSTER_SIZE,
            "min_samples": MIN_SAMPLES,
        },
        "rows": None,
        "cols": None,
        "sklearn": {
            "cluster_count": None,
            "noise_count": None,
        },
        "rust": {
            "cluster_count": None,
            "noise_count": None,
        },
        "partition_equal_up_to_relabelling": None,
        "noise_to_clustered": [],
        "clustered_to_noise": [],
        "cluster_to_cluster_moves": [],
    }


def load_matrix(path: Path) -> np.ndarray:
    if path.suffix != ".npz":
        raise HarnessError("matrix input must be a .npz file")
    with np.load(path) as payload:
        if "embeddings" not in payload:
            raise HarnessError("matrix .npz must contain an embeddings array")
        matrix = payload["embeddings"]
    if matrix.ndim != 2:
        raise HarnessError(f"embeddings must be 2-D, got shape {matrix.shape}")
    if matrix.dtype != np.float32:
        raise HarnessError(f"embeddings dtype must be float32, got {matrix.dtype}")
    if not np.isfinite(matrix).all():
        raise HarnessError("embeddings contain non-finite values")
    return np.ascontiguousarray(matrix)


def run_sklearn(matrix: np.ndarray) -> np.ndarray:
    clusterer = HDBSCAN(
        min_cluster_size=MIN_CLUSTER_SIZE,
        min_samples=MIN_SAMPLES,
        metric="euclidean",
        cluster_selection_method="eom",
    )
    return clusterer.fit_predict(matrix).astype(np.int64, copy=False)


def run_rust(
    matrix: np.ndarray, rust_bin: Path, *, temp_parent: Path | None = None
) -> np.ndarray:
    with tempfile.TemporaryDirectory(
        prefix="solstone-speaker-discovery-", dir=temp_parent
    ) as temp_dir:
        payload_path = Path(temp_dir) / "embeddings.f32"
        matrix.astype("<f4", copy=False).tofile(payload_path)
        request = {
            "schema": REQUEST_SCHEMA,
            "embeddings_f32le_path": str(payload_path),
            "payload_format": PAYLOAD_FORMAT,
            "dtype": DTYPE_F32LE,
            "shape": [int(matrix.shape[0]), int(matrix.shape[1])],
            "min_cluster_size": MIN_CLUSTER_SIZE,
            "min_samples": MIN_SAMPLES,
        }
        completed = subprocess.run(
            [str(rust_bin), RUST_COMMAND],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            check=False,
        )
    if completed.returncode != 0:
        raise HarnessError(
            "rust clustering command failed "
            f"exit={completed.returncode} stderr={completed.stderr.strip()!r}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"rust stdout is not JSON: {exc}") from exc
    if response.get("schema") != RESPONSE_SCHEMA:
        raise HarnessError(f"rust response schema mismatch: {response.get('schema')!r}")
    labels = np.asarray(response.get("labels"), dtype=np.int64)
    if labels.shape != (matrix.shape[0],):
        raise HarnessError(
            f"rust labels shape {labels.shape} does not match rows={matrix.shape[0]}"
        )
    return labels


def _cluster_count(labels: Sequence[int]) -> int:
    return len({int(label) for label in labels if int(label) != NOISE})


def _noise_count(labels: Sequence[int]) -> int:
    return sum(1 for label in labels if int(label) == NOISE)


def _partition(labels: Sequence[int]) -> set[frozenset[int]]:
    groups: dict[int, set[int]] = {}
    for index, label in enumerate(labels):
        label = int(label)
        if label == NOISE:
            continue
        groups.setdefault(label, set()).add(index)
    return {frozenset(members) for members in groups.values()}


def _partition_without(labels: Sequence[int], ignored: set[int]) -> set[frozenset[int]]:
    groups = set()
    for members in _partition(labels):
        remaining = frozenset(index for index in members if index not in ignored)
        if remaining:
            groups.add(remaining)
    return groups


def _index_members(
    labels: Sequence[int], *, ignored: set[int] | None = None
) -> dict[int, frozenset[int]]:
    ignored = ignored or set()
    by_label: dict[int, set[int]] = {}
    for index, label in enumerate(labels):
        if index in ignored:
            continue
        label = int(label)
        if label == NOISE:
            continue
        by_label.setdefault(label, set()).add(index)
    out: dict[int, frozenset[int]] = {}
    for members in by_label.values():
        frozen = frozenset(members)
        for index in members:
            out[index] = frozen
    return out


def _compare_clustering(
    sklearn_labels: Sequence[int],
    rust_labels: Sequence[int],
    *,
    cols: int | None = None,
) -> dict[str, Any]:
    if len(sklearn_labels) != len(rust_labels):
        raise HarnessError(
            f"label length mismatch: sklearn={len(sklearn_labels)} rust={len(rust_labels)}"
        )
    rows = len(sklearn_labels)
    sklearn_labels = [int(label) for label in sklearn_labels]
    rust_labels = [int(label) for label in rust_labels]
    noise_to_clustered = [
        index
        for index, (left, right) in enumerate(
            zip(sklearn_labels, rust_labels, strict=True)
        )
        if left == NOISE and right != NOISE
    ]
    clustered_to_noise = [
        index
        for index, (left, right) in enumerate(
            zip(sklearn_labels, rust_labels, strict=True)
        )
        if left != NOISE and right == NOISE
    ]
    ignored_noise_flips = set(noise_to_clustered) | set(clustered_to_noise)
    sklearn_members = _index_members(sklearn_labels, ignored=ignored_noise_flips)
    rust_members = _index_members(rust_labels, ignored=ignored_noise_flips)
    cluster_to_cluster_moves = [
        index
        for index in range(rows)
        if sklearn_labels[index] != NOISE
        and rust_labels[index] != NOISE
        and index not in ignored_noise_flips
        and sklearn_members[index] != rust_members[index]
    ]
    partition_equal = _partition(sklearn_labels) == _partition(rust_labels)
    cluster_counts_match_after_noise_flips = _partition_without(
        sklearn_labels, ignored_noise_flips
    ) == _partition_without(rust_labels, ignored_noise_flips)

    classification = _classify(
        rows=rows,
        partition_equal=partition_equal,
        sklearn_cluster_count=_cluster_count(sklearn_labels),
        rust_cluster_count=_cluster_count(rust_labels),
        noise_flip_count=len(ignored_noise_flips),
        cluster_to_cluster_move_count=len(cluster_to_cluster_moves),
        cluster_counts_match_after_noise_flips=cluster_counts_match_after_noise_flips,
    )
    return {
        "classification": classification,
        "rows": rows,
        "cols": cols,
        "sklearn": {
            "cluster_count": _cluster_count(sklearn_labels),
            "noise_count": _noise_count(sklearn_labels),
        },
        "rust": {
            "cluster_count": _cluster_count(rust_labels),
            "noise_count": _noise_count(rust_labels),
        },
        "partition_equal_up_to_relabelling": partition_equal,
        "noise_to_clustered": noise_to_clustered,
        "clustered_to_noise": clustered_to_noise,
        "cluster_to_cluster_moves": cluster_to_cluster_moves,
        "cluster_counts_match_after_noise_flips": cluster_counts_match_after_noise_flips,
    }


def _classify(
    *,
    rows: int,
    partition_equal: bool,
    sklearn_cluster_count: int,
    rust_cluster_count: int,
    noise_flip_count: int,
    cluster_to_cluster_move_count: int,
    cluster_counts_match_after_noise_flips: bool,
) -> str:
    if (
        partition_equal
        and noise_flip_count == 0
        and cluster_to_cluster_move_count == 0
        and sklearn_cluster_count == rust_cluster_count
    ):
        return SHIP

    # A noise-boundary flip is a point sitting on the stability margin where
    # f64 summation order can legitimately move it, whereas a cluster-to-cluster
    # move means the two implementations built different structure. Those are
    # different findings with different consequences, and averaging them into
    # one percentage would hide the one that matters.
    noise_flips_within_threshold = (
        rows > 0 and noise_flip_count * 1000 <= rows and noise_flip_count <= 3
    )
    cluster_count_mismatch_explained = (
        sklearn_cluster_count == rust_cluster_count
        or cluster_counts_match_after_noise_flips
    )
    if (
        cluster_to_cluster_move_count == 0
        and noise_flip_count > 0
        and noise_flips_within_threshold
        and cluster_count_mismatch_explained
    ):
        return INVESTIGATE

    return TRAILS_THE_WAVE


def compare_matrix(
    matrix: np.ndarray, rust_bin: Path, *, temp_parent: Path | None = None
) -> dict[str, Any]:
    sklearn_labels = run_sklearn(matrix)
    rust_labels = run_rust(matrix, rust_bin, temp_parent=temp_parent)
    return _compare_clustering(sklearn_labels, rust_labels, cols=int(matrix.shape[1]))


def compare_matrix_file(matrix_path: Path, rust_bin: Path) -> dict[str, Any]:
    report = _base_report(matrix_path=matrix_path, rust_bin=rust_bin)
    try:
        matrix = load_matrix(matrix_path)
        report.update(
            compare_matrix(
                matrix,
                rust_bin,
                temp_parent=_temp_parent_for_matrix(matrix_path),
            )
        )
    except Exception as exc:
        report["classification"] = HARNESS_ERROR
        report["failure"] = {"class": HARNESS_ERROR, "message": str(exc)}
    return report


def _temp_parent_for_matrix(matrix_path: Path) -> Path | None:
    resolved = matrix_path.expanduser().resolve()
    if resolved == ROOT or resolved.is_relative_to(ROOT):
        return None
    return resolved.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix_path", help="Input .npz containing embeddings")
    parser.add_argument(
        "--rust-bin", required=True, help="Path to Rust analyzer binary"
    )
    parser.add_argument(
        "--report", help="JSON report destination outside the repository"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    requested_report_path = Path(args.report).resolve() if args.report else None
    report_path: Path | None = None
    try:
        if requested_report_path is not None:
            report_path = _refuse_repo_destination(requested_report_path)
        matrix_path = Path(args.matrix_path)
        rust_bin = Path(args.rust_bin)
        report = compare_matrix_file(matrix_path, rust_bin)
    except Exception as exc:
        report = _base_report()
        report["failure"] = {"class": HARNESS_ERROR, "message": str(exc)}
        report["classification"] = HARNESS_ERROR

    rendered = _render_report(report)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report.get("classification") == SHIP else 1


if __name__ == "__main__":
    raise SystemExit(main())
