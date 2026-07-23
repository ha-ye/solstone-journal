# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from tests.native_sol.run_python_parity import PARITY_DIR, load_vectors, run_vector


@pytest.mark.parametrize(
    "vector",
    load_vectors(sorted(PARITY_DIR.glob("*.jsonl"))) if PARITY_DIR.exists() else [],
    ids=lambda vector: vector["id"],
)
def test_python_parity_vectors(vector: dict[str, object]) -> None:
    run_vector(vector)
