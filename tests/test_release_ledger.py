# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import scripts.check_release_preflight as preflight
import scripts.check_rust_release_manifest as checker
import scripts.release_ledger as ledger
import scripts.release_tool_pins as pins
from scripts.check_rust_release_manifest import canonical_json_bytes
from scripts.release_advisory_policy import PolicyRun

SOURCE_COMMIT = "a" * 40
MALFORMED_DB_COMMIT_CASES = (
    ("short-39", "b" * 39),
    ("short-63", "b" * 63),
    ("long-41", "b" * 41),
    ("long-65", "b" * 65),
    ("uppercase", "B" * 40),
    ("non-hex", "g" * 40),
    ("empty", ""),
    ("whitespace", " " + "b" * 40),
    ("extra-line", "b" * 40 + "\nunexpected"),
)
MALFORMED_ARCHIVE_DIGEST_CASES = (
    ("short", "c" * 63),
    ("uppercase", "C" * 64),
    ("non-hex", "g" * 64),
    ("empty", ""),
    ("extra-line", "c" * 64 + "\nunexpected"),
)
MALFORMED_DB_COMMITS = tuple(
    pytest.param(value, id=name) for name, value in MALFORMED_DB_COMMIT_CASES
)
MALFORMED_ARCHIVE_DIGESTS = tuple(
    pytest.param(value, id=name) for name, value in MALFORMED_ARCHIVE_DIGEST_CASES
)


def _policy() -> PolicyRun:
    return PolicyRun(
        advisory_source_id="internal",
        db_snapshot_basename="advisory-db-fixture00000000",
        db_commit="b" * 40,
        db_archive_sha256="c" * 64,
        advisory_count=1,
        advisory_acquired_at="2026-07-20T11:00:00Z",
        db_commit_timestamp="2026-07-19T12:00:00Z",
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
    root_wheel = next(
        name
        for name in checker.expected_package_names(include_models=False)
        if name.startswith("solstone-") and "macosx_14_0_arm64" in name
    )
    core_wheel = next(
        name
        for name in checker.expected_package_names(include_models=False)
        if name.startswith("solstone_core-") and "macosx_14_0_arm64" in name
    )
    return [
        _native(
            "root",
            root_wheel,
            "solstone/observe/transcribe/parakeet_helper/_bin/parakeet-helper",
        ),
        _native(
            "core",
            core_wheel,
            "solstone_core-1.2.3.data/scripts/solstone-core",
        ),
    ]


def _candidate(root: Path) -> Path:
    candidate = root / "candidate"
    candidate.mkdir(parents=True)
    checker.write_inert_packages(candidate, include_models=False)
    for artifact, (lane, _target) in checker.rust_artifact_targets().items():
        if lane == "source":
            continue
        info = zipfile.ZipInfo(
            f"{artifact.removesuffix('.whl')}.data/scripts/solstone-core"
        )
        info.create_system = 3
        info.external_attr = 0o755 << 16
        with zipfile.ZipFile(candidate / artifact, "w") as wheel:
            wheel.writestr(info, f"{lane} native member".encode("utf-8"))
    root_wheel = next(
        name
        for name in checker.expected_package_names(include_models=False)
        if name.startswith("solstone-") and "macosx_14_0_arm64" in name
    )
    info = zipfile.ZipInfo(
        "solstone/observe/transcribe/parakeet_helper/_bin/parakeet-helper"
    )
    info.create_system = 3
    info.external_attr = 0o755 << 16
    with zipfile.ZipFile(candidate / root_wheel, "w") as wheel:
        wheel.writestr(info, b"macos helper")
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


def _models(decision: str = "exclude") -> dict[str, str]:
    return {"decision": decision, "package_version": "1.0.0"}


def _ledger_path(root: Path) -> Path:
    return ledger.write_ledger(
        evidence_root=root / "target" / "release-evidence",
        version="1.2.3",
        source_commit=SOURCE_COMMIT,
        release_dir=_candidate(root),
        core_lock_path=_core_lock(root),
        tool_evidence=_tool_evidence(),
        policy_run=_policy(),
        native_records=_native_records(),
        models=_models(),
    )


def _mutate_retained_policy_run(path: Path, field: str, value: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["policy_run"][field] = value
    path.write_bytes(canonical_json_bytes(payload))


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
        models=_models(),
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
        models=_models(),
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
        models=_models(),
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
    assert payload["models"] == _models()
    assert {"name", "sha256", "bytes"} == set(payload["candidate"]["files"][0])
    assert set(payload["tool_evidence"]) == set(preflight.LANE_TOOL_KEYS)
    assert set(payload["native_members"]) == set(ledger.PROOF_TARGETS)
    assert set(payload["native_members"]["macos-arm64"]) == {
        "solstone-core",
        "parakeet-helper",
    }


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
            models=_models(),
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
            models=_models(),
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
            models=_models(),
        )

    assert exc.value.failures[0].error == "macOS native record set is incomplete"


@pytest.mark.parametrize("lane", tuple(preflight.LANE_TOOL_KEYS))
def test_ledger_requires_full_tool_cohort_per_lane(tmp_path: Path, lane: str) -> None:
    tools = _tool_evidence()
    removed = next(iter(preflight.LANE_TOOL_KEYS[lane]))
    del tools[lane][removed]

    with pytest.raises(ledger.LedgerError) as exc:
        ledger.build_ledger(
            version="1.2.3",
            source_commit=SOURCE_COMMIT,
            release_dir=_candidate(tmp_path),
            core_lock_path=_core_lock(tmp_path),
            tool_evidence=tools,
            policy_run=_policy(),
            native_records=_native_records(),
            models=_models(),
        )

    assert any("tool" in failure.error for failure in exc.value.failures)


@pytest.mark.parametrize(
    ("target", "member", "mutation"),
    [
        ("linux-x86_64-musl", "solstone-core", "missing"),
        ("linux-aarch64-musl", "solstone-core", "invalid-sha256"),
        ("macos-arm64", "parakeet-helper", "invalid-bytes"),
    ],
)
def test_retained_ledger_rejects_native_member_map_mutations(
    tmp_path: Path, target: str, member: str, mutation: str
) -> None:
    path = _ledger_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing":
        del payload["native_members"][target][member]
    elif mutation == "invalid-sha256":
        payload["native_members"][target][member]["sha256"] = "ABC"
    elif mutation == "invalid-bytes":
        payload["native_members"][target][member]["bytes"] = -1
    else:
        raise AssertionError(mutation)
    path.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(ledger.LedgerError) as exc:
        ledger.read_retained_ledger(path)

    assert any("native member" in failure.error for failure in exc.value.failures)


@pytest.mark.parametrize("value", MALFORMED_DB_COMMITS)
def test_retained_ledger_rejects_malformed_db_commit(
    tmp_path: Path,
    value: str,
) -> None:
    path = _ledger_path(tmp_path)
    _mutate_retained_policy_run(path, "db_commit", value)

    with pytest.raises(ledger.LedgerError) as exc:
        ledger.read_retained_ledger(path)

    assert exc.value.failures[0].error == "ledger.policy_run.db_commit is invalid"
    assert exc.value.failures[0].expected == "exactly 40 or 64 lowercase hex characters"
    if value.startswith("B"):
        assert exc.value.failures[0].actual == value


def test_retained_ledger_accepts_sha256_db_commit(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    _mutate_retained_policy_run(path, "db_commit", "b" * 64)

    payload = ledger.read_retained_ledger(path)

    assert payload["policy_run"]["db_commit"] == "b" * 64


@pytest.mark.parametrize("value", MALFORMED_ARCHIVE_DIGESTS)
def test_retained_ledger_rejects_malformed_archive_digest(
    tmp_path: Path,
    value: str,
) -> None:
    path = _ledger_path(tmp_path)
    _mutate_retained_policy_run(path, "db_archive_sha256", value)

    with pytest.raises(ledger.LedgerError) as exc:
        ledger.read_retained_ledger(path)

    assert (
        exc.value.failures[0].error == "ledger.policy_run.db_archive_sha256 is invalid"
    )
    assert exc.value.failures[0].expected == "exactly 64 lowercase hex characters"
    if value.startswith("C"):
        assert exc.value.failures[0].actual == value
