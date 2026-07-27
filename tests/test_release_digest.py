# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib

import scripts.release_digest as digest
from scripts.check_rust_release_manifest import canonical_json_bytes


def test_candidate_digest_uses_basename_sorted_two_space_lf_stream(tmp_path) -> None:
    (tmp_path / "z").mkdir()
    (tmp_path / "z" / "a.txt").write_bytes(b"")
    (tmp_path / "b.txt").write_bytes(b"abc")
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "c.txt").write_bytes(b"")

    expected_stream = (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        "  0  a.txt\n"
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        "  3  b.txt\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        "  0  c.txt\n"
    )
    path_sorted_stream = (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        "  3  b.txt\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        "  0  c.txt\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        "  0  a.txt\n"
    )

    assert expected_stream != path_sorted_stream
    assert (
        digest.candidate_digest(tmp_path)
        == hashlib.sha256(expected_stream.encode("ascii")).hexdigest()
    )
    assert digest.candidate_digest(tmp_path) == (
        "f9c21327effe2299f897f3638b41e6d936714d0a6e9aa3781ee577fa2949355e"
    )


def test_bundle_digest_excludes_self_hash_and_is_canonical() -> None:
    candidate = "f9c21327effe2299f897f3638b41e6d936714d0a6e9aa3781ee577fa2949355e"
    ledger = "1" * 64
    proof_hashes = {
        "macos-arm64": "4" * 64,
        "linux-x86_64-musl": "3" * 64,
        "linux-aarch64-musl": "2" * 64,
    }
    nvattest_hashes = {
        "macos-arm64": "7" * 64,
        "linux-x86_64-musl": "6" * 64,
        "linux-aarch64-musl": "5" * 64,
    }
    expected_payload = {
        "candidate_digest": candidate,
        "ledger_sha256": ledger,
        "nvattest_sha256": {
            target: nvattest_hashes[target] for target in sorted(nvattest_hashes)
        },
        "proof_sha256": {
            target: proof_hashes[target] for target in sorted(proof_hashes)
        },
    }
    expected = hashlib.sha256(canonical_json_bytes(expected_payload)).hexdigest()

    assert (
        digest.bundle_digest(candidate, ledger, proof_hashes, nvattest_hashes)
        == expected
    )

    with_extra_self_hash = dict(proof_hashes)
    with_extra_self_hash["bundle_digest"] = "5" * 64
    assert (
        digest.bundle_digest(candidate, ledger, with_extra_self_hash, nvattest_hashes)
        != expected
    )
