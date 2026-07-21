# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.check_release_preflight as preflight
import scripts.release_ledger as ledger
import scripts.release_tool_pins as pins
from scripts.release_advisory_policy import PolicyRun

SOURCE_COMMIT = "a" * 40


def _policy() -> PolicyRun:
    return PolicyRun(
        advisory_source_id="internal",
        db_commit="b" * 40,
        db_archive_sha256="c" * 64,
        advisory_acquired_at="2026-07-20T11:00:00Z",
        policy_checked_at="2026-07-20T12:00:00Z",
        result="pass",
    )


def _native(role: str, wheel_name: str, member_path: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "macos-native-record/v1",
        "source_commit": SOURCE_COMMIT,
        "core_lock_sha256": "d" * 64,
        "role": role,
        "target": {
            "triple": "aarch64-apple-darwin",
            "profile": "release",
            "features": [],
        },
        "wheel": {"name": wheel_name, "sha256": "e" * 64, "bytes": 12},
        "member": {"path": member_path, "sha256": "f" * 64, "bytes": 6},
        "tools": {
            "python": pins.PYTHON_MACOS_VERSION,
            "xcode": pins.MACOS_XCODE_PIN,
            "swift": pins.MACOS_SWIFT_PIN,
            "codesign": pins.MACOS_CODESIGN_PUBLIC_PIN,
            "notarytool": pins.MACOS_NOTARYTOOL_PIN,
        },
        "signing_mode": pins.MACOS_SIGNING_MODE,
        "signing": {
            "signer_pinned": True,
            "team_pinned": True,
            "hardened_runtime": True,
            "trusted_timestamp": True,
        },
        "notarization_status": "accepted",
    }


def _native_records() -> list[dict]:
    return [
        _native(
            "root",
            "solstone-1.2.3-py3-none-macosx_14_0_arm64.whl",
            "solstone/observe/transcribe/parakeet_helper/_bin/parakeet-helper",
        ),
        _native(
            "core",
            "solstone_core-1.2.3-py3-none-macosx_14_0_arm64.whl",
            "solstone_core-1.2.3.data/scripts/solstone-core",
        ),
    ]


def _candidate(root: Path) -> Path:
    candidate = root / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "a.whl").write_bytes(b"a")
    (candidate / "a.whl.rust-release-manifest.json").write_bytes(b"{}")
    return candidate


def _core_lock(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "Cargo.lock"
    path.write_text("lock\n", encoding="utf-8")
    return path


def _tool_evidence() -> dict[str, dict[str, str]]:
    return {
        lane: preflight.expected_lane_tool_evidence(lane)
        for lane in preflight.LANE_TOOL_KEYS
    }


def test_ledger_is_byte_deterministic_for_fixed_inputs(tmp_path: Path) -> None:
    first = ledger.write_ledger(
        evidence_root=tmp_path / "one" / "target" / "release-evidence",
        version="1.2.3",
        source_commit=SOURCE_COMMIT,
        release_dir=_candidate(tmp_path / "one"),
        core_lock_path=_core_lock(tmp_path / "one"),
        tool_evidence=_tool_evidence(),
        policy_run=_policy(),
        native_records=_native_records(),
    )
    second = ledger.write_ledger(
        evidence_root=tmp_path / "two" / "target" / "release-evidence",
        version="1.2.3",
        source_commit=SOURCE_COMMIT,
        release_dir=_candidate(tmp_path / "two"),
        core_lock_path=_core_lock(tmp_path / "two"),
        tool_evidence=_tool_evidence(),
        policy_run=_policy(),
        native_records=_native_records(),
    )

    assert first.read_bytes() == second.read_bytes()


def test_ledger_key_set_excludes_transport_and_bundle_state(tmp_path: Path) -> None:
    path = ledger.write_ledger(
        evidence_root=tmp_path / "target" / "release-evidence",
        version="1.2.3",
        source_commit=SOURCE_COMMIT,
        release_dir=_candidate(tmp_path),
        core_lock_path=_core_lock(tmp_path),
        tool_evidence=_tool_evidence(),
        policy_run=_policy(),
        native_records=_native_records(),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")

    assert set(payload) == ledger.TOP_LEVEL_KEYS
    assert set(payload["policy_run"]) == ledger.POLICY_RUN_KEYS
    assert "created_at" not in text
    assert "bundle_digest" not in text
    assert "github" not in text
    assert "db_url" not in text
    assert "url" not in payload["policy_run"]
    assert {"name", "sha256", "bytes"} == set(payload["candidate"]["files"][0])


def test_ledger_rejects_raw_signer_team_and_uuid_evidence(tmp_path: Path) -> None:
    records = _native_records()
    records[0]["tools"]["swift"] = pins.MACOS_TEAM_IDENTIFIER
    with pytest.raises(ledger.LedgerError):
        ledger.build_ledger(
            version="1.2.3",
            source_commit=SOURCE_COMMIT,
            release_dir=_candidate(tmp_path),
            core_lock_path=_core_lock(tmp_path),
            tool_evidence=_tool_evidence(),
            policy_run=_policy(),
            native_records=records,
        )

    records = _native_records()
    records[0]["wheel"]["name"] = "123e4567-e89b-12d3-a456-426614174000.whl"
    with pytest.raises(ledger.LedgerError):
        ledger.build_ledger(
            version="1.2.3",
            source_commit=SOURCE_COMMIT,
            release_dir=tmp_path / "candidate",
            core_lock_path=tmp_path / "Cargo.lock",
            tool_evidence=_tool_evidence(),
            policy_run=_policy(),
            native_records=records,
        )


def test_ledger_requires_exactly_two_native_records(tmp_path: Path) -> None:
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.build_ledger(
            version="1.2.3",
            source_commit=SOURCE_COMMIT,
            release_dir=_candidate(tmp_path),
            core_lock_path=_core_lock(tmp_path),
            tool_evidence=_tool_evidence(),
            policy_run=_policy(),
            native_records=_native_records()[:1],
        )

    assert exc.value.failures[0].error == "macOS native record set is incomplete"
