# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import scripts.check_release_preflight as preflight
import scripts.check_rust_release_manifest as checker
import scripts.release_candidate_driver as driver
import scripts.release_ledger as ledger
import scripts.release_tool_pins as pins
from scripts.build_nvattest_authority import render_nvattest_authority_json
from scripts.check_rust_release_manifest import canonical_json_bytes
from scripts.check_wheel_contents import (
    CORE_SCRIPT_NAMES,
    SPEAKERS_ANALYZE_RUNTIME_INSTALL_DIR,
    SPEAKERS_ANALYZE_SCRIPT_NAMES,
    SPEAKERS_ANALYZE_TARGETS,
)
from scripts.release_advisory_policy import PolicyRun
from scripts.release_nvattest_proof import SUPPORT_DISTRIBUTION_NAMES
from scripts.release_public_evidence import validate_public_evidence_tree

SOURCE_COMMIT = "a" * 40
CONTRACT_DOC = "docs/release-evidence-contract.md"
RETAINED_LEDGER_V1_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "release_evidence"
    / "retained-1.0.17"
    / "ledger.json"
)
RETAINED_LEDGER_V1_SHA256 = (
    "9aafdf7fa3bedfd943c7a26b9a6338e25d87d735ad144ecd4b0bf7c801ad5cc7"
)
EXPECTED_LEDGER_SCHEMA_V1_TOP_LEVEL_KEYS = frozenset(
    (
        "schema_version",
        "kind",
        "product",
        "version",
        "source_commit",
        "candidate",
        "models",
        "core_lock_sha256",
        "rust_targets",
        "tool_evidence",
        "native_members",
        "dependency_policy",
        "policy_run",
        "native_summary",
        "proofs",
        "redaction",
    )
)
EXPECTED_LEDGER_SCHEMA_V1_MODELS_KEYS = frozenset(("decision", "package_version"))
EXPECTED_LEDGER_SCHEMA_V1_NVATTEST_KEYS = None
EXPECTED_LEDGER_SCHEMA_V1_POLICY_RUN_KEYS = frozenset(
    (
        "advisory_source_id",
        "db_snapshot_basename",
        "db_commit",
        "db_archive_sha256",
        "advisory_count",
        "advisory_acquired_at",
        "db_commit_timestamp",
        "policy_checked_at",
        "result",
    )
)
CANONICAL_LEDGER_V2_FIXTURE = (
    Path(__file__).parent / "fixtures" / "release_evidence" / "canonical-ledger-v2.json"
)
CANONICAL_LEDGER_V2_SHA256 = (
    "3686dadcf4e9549495d3ceaf39ace59592dc16305b71eede0a3768e140c835f0"
)
EXPECTED_LEDGER_SCHEMA_V2_TOP_LEVEL_KEYS = frozenset(
    (
        "schema_version",
        "kind",
        "product",
        "version",
        "source_commit",
        "candidate",
        "models",
        "core_lock_sha256",
        "rust_targets",
        "tool_evidence",
        "native_members",
        "dependency_policy",
        "policy_run",
        "native_summary",
        "proofs",
        "nvattest",
        "redaction",
    )
)
EXPECTED_LEDGER_SCHEMA_V2_MODELS_KEYS = frozenset(("decision", "package_version"))
EXPECTED_LEDGER_SCHEMA_V2_NVATTEST_KEYS = frozenset(
    ("challenge", "authority_sha256", "authority", "support_distributions")
)
EXPECTED_LEDGER_SCHEMA_V2_POLICY_RUN_KEYS = frozenset(
    (
        "advisory_source_id",
        "db_snapshot_basename",
        "db_commit",
        "db_archive_sha256",
        "advisory_count",
        "advisory_acquired_at",
        "db_commit_timestamp",
        "policy_checked_at",
        "result",
    )
)
TOP_LEVEL_KEY_SET_INVALID = "retained ledger top-level key set is invalid"
MODELS_KEY_SET_INVALID = "retained ledger models key set is invalid"
NVATTEST_KEY_SET_INVALID = "retained ledger nvattest key set is invalid"
POLICY_RUN_KEY_SET_INVALID = "retained ledger policy_run key set is invalid"
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
    if role == "root":
        members = {
            "parakeet-helper": {
                "path": member_path,
                "sha256": "f" * 64,
                "bytes": 6,
            }
        }
    elif role == "core":
        members = {
            name: {
                "path": f"solstone_core-1.2.3.data/scripts/{name}",
                "sha256": "f" * 64,
                "bytes": 6,
            }
            for name in CORE_SCRIPT_NAMES
        }
    else:
        dylib_name = SPEAKERS_ANALYZE_TARGETS["macos-arm64"].runtime_staged_name
        members = {
            SPEAKERS_ANALYZE_SCRIPT_NAMES[0]: {
                "path": member_path,
                "sha256": "f" * 64,
                "bytes": 6,
            },
            dylib_name: {
                "path": (
                    "solstone_core_speakers_analyze-1.2.3.data/"
                    f"{SPEAKERS_ANALYZE_RUNTIME_INSTALL_DIR.as_posix()}/{dylib_name}"
                ),
                "sha256": "f" * 64,
                "bytes": 6,
            },
        }
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
        "members": members,
        "unsigned_members": {name: "f" * 64 for name in members},
        "tools": {
            "python": pins.PYTHON_MACOS_VERSION,
            "xcode": pins.MACOS_XCODE_PIN,
            "swift": pins.MACOS_SWIFT_FIXTURE_BANNER,
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
    speakers_wheel = next(
        name
        for name in checker.expected_package_names(include_models=False)
        if name.startswith("solstone_core_speakers_analyze-")
        and "macosx_14_0_arm64" in name
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
        _native(
            "speakers-analyze",
            speakers_wheel,
            "solstone_core_speakers_analyze-1.2.3.data/scripts/solstone-core-speakers-analyze",
        ),
    ]


def _candidate(root: Path) -> Path:
    candidate = root / "candidate"
    candidate.mkdir(parents=True)
    checker.write_inert_packages(candidate, include_models=False)
    for artifact, (lane, _target) in checker.rust_artifact_targets().items():
        if lane == "source":
            continue
        with zipfile.ZipFile(candidate / artifact, "w") as wheel:
            version = artifact.removesuffix(".whl").split("-")[1]
            for name in CORE_SCRIPT_NAMES:
                info = zipfile.ZipInfo(f"solstone_core-{version}.data/scripts/{name}")
                info.create_system = 3
                info.external_attr = 0o755 << 16
                wheel.writestr(info, f"{lane} {name} native member".encode("utf-8"))
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
    speakers_wheel = next(
        name
        for name in checker.expected_package_names(include_models=False)
        if name.startswith("solstone_core_speakers_analyze-")
        and "macosx_14_0_arm64" in name
    )
    speakers_version = speakers_wheel.removesuffix(".whl").split("-")[1]
    speakers_prefix = f"solstone_core_speakers_analyze-{speakers_version}.data"
    dylib_name = SPEAKERS_ANALYZE_TARGETS["macos-arm64"].runtime_staged_name
    with zipfile.ZipFile(candidate / speakers_wheel, "w") as wheel:
        for member in (
            f"{speakers_prefix}/scripts/{SPEAKERS_ANALYZE_SCRIPT_NAMES[0]}",
            (
                f"{speakers_prefix}/"
                f"{SPEAKERS_ANALYZE_RUNTIME_INSTALL_DIR.as_posix()}/{dylib_name}"
            ),
        ):
            info = zipfile.ZipInfo(member)
            info.create_system = 3
            info.external_attr = 0o755 << 16
            wheel.writestr(info, b"native")
    return candidate


def _core_lock(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "Cargo.lock"
    path.write_text("lock\n", encoding="utf-8")
    return path


def _tool_evidence() -> dict[str, dict[str, str]]:
    return {
        lane: pins.fixture_lane_tool_evidence(lane) for lane in preflight.LANE_TOOL_KEYS
    }


def _models(decision: str = "exclude") -> dict[str, str]:
    return {"decision": decision, "package_version": "1.0.0"}


def _nvattest(challenge: str | None = None) -> dict[str, object]:
    authority_bytes = render_nvattest_authority_json().encode("utf-8")
    return {
        "authority": json.loads(authority_bytes.decode("utf-8")),
        "authority_sha256": hashlib.sha256(authority_bytes).hexdigest(),
        "challenge": challenge or hashlib.sha256(b"challenge").hexdigest(),
        "support_distributions": [
            {
                "bytes": len(name.encode("utf-8")),
                "filename": f"{name.replace('-', '_')}-0.0.{index}-py3-none-any.whl",
                "name": name,
                "sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                "version": f"0.0.{index}",
            }
            for index, name in enumerate(sorted(SUPPORT_DISTRIBUTION_NAMES), start=1)
        ],
    }


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
        nvattest=_nvattest(),
    )


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(
            _contains_key(child, key) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False


def test_unsigned_members_from_native_records_do_not_reach_retained_surfaces(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    native_records = _native_records()
    for record in native_records:
        record["unsigned_members"] = {
            name: str(member["sha256"]) for name, member in record["members"].items()
        }

    payload = ledger.build_ledger(
        version="1.2.3",
        source_commit=SOURCE_COMMIT,
        release_dir=candidate,
        core_lock_path=_core_lock(tmp_path),
        tool_evidence=_tool_evidence(),
        policy_run=_policy(),
        native_records=native_records,
        models=_models(),
        nvattest=_nvattest(),
    )

    assert not _contains_key(payload, "unsigned_members")
    assert not any(
        "unsigned_members" in name
        for name in driver._expected_payload_file_names(include_models=False)
    )
    for wheel_path in candidate.glob("*.whl"):
        with zipfile.ZipFile(wheel_path) as wheel:
            assert not any("unsigned_members" in name for name in wheel.namelist())


def _fixture_payload() -> dict:
    return json.loads(CANONICAL_LEDGER_V2_FIXTURE.read_text(encoding="utf-8"))


def _v1_fixture_payload() -> dict:
    return json.loads(RETAINED_LEDGER_V1_FIXTURE.read_text(encoding="utf-8"))


def _v2_fixture_payload() -> dict:
    return json.loads(CANONICAL_LEDGER_V2_FIXTURE.read_text(encoding="utf-8"))


def _failure_text(failure: checker.Failure) -> str:
    return "\n".join((failure.error, failure.expected, failure.actual, failure.repair))


def _schema_with_extra_key(
    schema_version: int, extra_key: str
) -> ledger.RetainedLedgerSchema:
    schema = ledger.LEDGER_SCHEMA_REGISTRY[schema_version]
    return ledger.RetainedLedgerSchema(
        top_level_keys=schema.top_level_keys | {extra_key},
        models_keys=schema.models_keys,
        nvattest_keys=schema.nvattest_keys,
        policy_run_keys=schema.policy_run_keys,
    )


def _mutate_retained_policy_run(path: Path, field: str, value: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["policy_run"][field] = value
    path.write_bytes(canonical_json_bytes(payload))


@pytest.mark.parametrize(
    ("schema_version", "expected_shape"),
    (
        (
            1,
            {
                "top_level": EXPECTED_LEDGER_SCHEMA_V1_TOP_LEVEL_KEYS,
                "models": EXPECTED_LEDGER_SCHEMA_V1_MODELS_KEYS,
                "nvattest": EXPECTED_LEDGER_SCHEMA_V1_NVATTEST_KEYS,
                "policy_run": EXPECTED_LEDGER_SCHEMA_V1_POLICY_RUN_KEYS,
            },
        ),
        (
            2,
            {
                "top_level": EXPECTED_LEDGER_SCHEMA_V2_TOP_LEVEL_KEYS,
                "models": EXPECTED_LEDGER_SCHEMA_V2_MODELS_KEYS,
                "nvattest": EXPECTED_LEDGER_SCHEMA_V2_NVATTEST_KEYS,
                "policy_run": EXPECTED_LEDGER_SCHEMA_V2_POLICY_RUN_KEYS,
            },
        ),
    ),
)
def test_registered_ledger_schema_matches_literal_shape(
    schema_version: int, expected_shape: dict[str, object]
) -> None:
    schema = ledger.LEDGER_SCHEMA_REGISTRY[schema_version]
    actual_shape = {
        "top_level": schema.top_level_keys,
        "models": schema.models_keys,
        "nvattest": schema.nvattest_keys,
        "policy_run": schema.policy_run_keys,
    }
    assert actual_shape == expected_shape, (
        f"Registered retained ledger schema version {schema_version} changed. "
        f"Append a new schema version and keep the old one registered; see {CONTRACT_DOC}."
    )


def test_registered_ledger_schema_nvattest_presence_is_self_consistent() -> None:
    for schema in ledger.LEDGER_SCHEMA_REGISTRY.values():
        declares_nvattest = ledger.retained_ledger_schema_declares_nvattest(schema)
        assert (schema.nvattest_keys is None) == (not declares_nvattest)


def test_consumer_completeness_gate_is_proper_subset_for_all_registered_versions() -> (
    None
):
    for schema in ledger.LEDGER_SCHEMA_REGISTRY.values():
        assert ledger.RETAINED_LEDGER_CONSUMER_TOP_LEVEL_KEYS < schema.top_level_keys


def test_writer_stamps_current_schema_version_constant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    synthetic_version = 99
    monkeypatch.setitem(
        ledger.LEDGER_SCHEMA_REGISTRY,
        synthetic_version,
        ledger.LEDGER_SCHEMA_REGISTRY[ledger.CURRENT_LEDGER_SCHEMA_VERSION],
    )
    monkeypatch.setattr(ledger, "CURRENT_LEDGER_SCHEMA_VERSION", synthetic_version)

    path = _ledger_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == synthetic_version


def test_release_ledger_has_no_bare_top_level_keys_symbol() -> None:
    source = Path(ledger.__file__).read_text(encoding="utf-8")

    assert not hasattr(ledger, "TOP_LEVEL_KEYS")
    assert "\nTOP_LEVEL_KEYS =" not in source
    assert "set(payload) != TOP_LEVEL_KEYS" not in source
    assert "set(payload) != schema.top_level_keys" in source


@pytest.mark.parametrize(
    "fixture",
    (RETAINED_LEDGER_V1_FIXTURE, CANONICAL_LEDGER_V2_FIXTURE),
)
def test_frozen_ledger_fixture_validates_with_reader_and_public_evidence(
    fixture: Path,
) -> None:
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    failures = ledger.validate_retained_ledger(payload)

    assert failures == [], (
        "Frozen retained ledger fixture no longer validates: current code now rejects "
        f"a ledger it previously accepted. Read {CONTRACT_DOC} options 1 and 2 before "
        "changing fixture bytes. Failures: "
        f"{[_failure_text(failure) for failure in failures]}"
    )
    try:
        readback = ledger.read_retained_ledger(fixture)
    except ledger.LedgerError as exc:
        pytest.fail(
            "Frozen retained ledger fixture no longer validates through the file "
            "reader: current code now rejects a ledger it previously accepted. Read "
            f"{CONTRACT_DOC} options 1 and 2 before changing fixture bytes. Failures: "
            f"{[_failure_text(failure) for failure in exc.failures]}"
        )
    public_failures = validate_public_evidence_tree("ledger", payload)

    assert readback == payload
    assert public_failures == [], (
        "Frozen retained ledger fixture is not public-evidence clean; choose fixture "
        f"bytes that pass the public evidence validator before pinning. {CONTRACT_DOC}. "
        f"Failures: {[_failure_text(failure) for failure in public_failures]}"
    )


@pytest.mark.parametrize(
    ("fixture", "expected_sha256"),
    (
        (RETAINED_LEDGER_V1_FIXTURE, RETAINED_LEDGER_V1_SHA256),
        (CANONICAL_LEDGER_V2_FIXTURE, CANONICAL_LEDGER_V2_SHA256),
    ),
)
def test_frozen_ledger_fixture_sha256_is_pinned(
    fixture: Path, expected_sha256: str
) -> None:
    actual = hashlib.sha256(fixture.read_bytes()).hexdigest()

    assert actual == expected_sha256, (
        f"restore {fixture}; never "
        "regenerate the frozen retained ledger fixture to make a test pass. See "
        f"{CONTRACT_DOC}."
    )


def test_retained_ledger_rejects_extra_top_level_key() -> None:
    payload = copy.deepcopy(_fixture_payload())
    payload["shape_marker"] = "shape marker"

    failures = ledger.validate_retained_ledger(payload)

    assert failures[0].error == TOP_LEVEL_KEY_SET_INVALID


def test_retained_ledger_rejects_missing_top_level_key() -> None:
    payload = copy.deepcopy(_fixture_payload())
    del payload["redaction"]

    failures = ledger.validate_retained_ledger(payload)

    assert failures[0].error == TOP_LEVEL_KEY_SET_INVALID


@pytest.mark.parametrize(
    ("section", "removed_key", "added_key", "expected_error"),
    (
        ("models", "decision", "shape_marker", MODELS_KEY_SET_INVALID),
        ("nvattest", "challenge", "shape_marker", NVATTEST_KEY_SET_INVALID),
        ("policy_run", "result", "shape_marker", POLICY_RUN_KEY_SET_INVALID),
    ),
)
def test_retained_ledger_rejects_registered_subkey_shape_mutations(
    section: str, removed_key: str, added_key: str, expected_error: str
) -> None:
    payload = copy.deepcopy(_fixture_payload())
    del payload[section][removed_key]
    payload[section][added_key] = "shape marker"

    failures = ledger.validate_retained_ledger(payload)

    assert failures[0].error == expected_error


def test_retained_ledger_rejects_missing_schema_version() -> None:
    payload = copy.deepcopy(_fixture_payload())
    del payload["schema_version"]

    failures = ledger.validate_retained_ledger(payload)

    assert len(failures) == 1
    assert failures[0].error == "retained ledger schema_version is missing"
    assert "schema_version" in _failure_text(failures[0])


@pytest.mark.parametrize("value", (True, "1", 0, -1))
def test_retained_ledger_rejects_malformed_schema_version(value: object) -> None:
    payload = copy.deepcopy(_fixture_payload())
    payload["schema_version"] = value

    failures = ledger.validate_retained_ledger(payload)

    assert len(failures) == 1
    assert (
        failures[0].error == f"retained ledger schema_version is malformed: {value!r}"
    )
    assert repr(value) in _failure_text(failures[0])


def test_retained_ledger_rejects_unregistered_schema_version_alone() -> None:
    payload = copy.deepcopy(_fixture_payload())
    payload["schema_version"] = 999

    failures = ledger.validate_retained_ledger(payload)

    assert len(failures) == 1
    assert failures[0].error == "retained ledger schema_version 999 is not registered"
    assert "999" in _failure_text(failures[0])
    assert "1, 2" in failures[0].expected


def test_retained_ledger_accepts_registered_synthetic_future_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_version = 99
    extra_key = "shape_marker_future"
    monkeypatch.setitem(
        ledger.LEDGER_SCHEMA_REGISTRY,
        synthetic_version,
        _schema_with_extra_key(ledger.CURRENT_LEDGER_SCHEMA_VERSION, extra_key),
    )
    assert extra_key in ledger.LEDGER_SCHEMA_REGISTRY[synthetic_version].top_level_keys

    payload = copy.deepcopy(_fixture_payload())
    payload["schema_version"] = synthetic_version
    payload[extra_key] = "shape marker future"
    assert ledger.validate_retained_ledger(payload) == []

    missing_payload = copy.deepcopy(payload)
    del missing_payload[extra_key]
    missing_failures = ledger.validate_retained_ledger(missing_payload)
    assert missing_failures[0].error == TOP_LEVEL_KEY_SET_INVALID
    assert all("not registered" not in failure.error for failure in missing_failures)

    current_extra_payload = copy.deepcopy(_fixture_payload())
    current_extra_payload[extra_key] = "shape marker future"
    current_extra_failures = ledger.validate_retained_ledger(current_extra_payload)
    assert current_extra_failures[0].error == TOP_LEVEL_KEY_SET_INVALID


def test_consumer_completeness_gate_fails_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absent_key = "future_consumer_key"
    monkeypatch.setattr(
        ledger,
        "RETAINED_LEDGER_CONSUMER_TOP_LEVEL_KEYS",
        ledger.RETAINED_LEDGER_CONSUMER_TOP_LEVEL_KEYS | {absent_key},
    )

    failures = ledger.validate_retained_ledger(_fixture_payload())

    assert len(failures) == 1
    assert failures[0].error == (
        "retained ledger schema_version 2 omits current consumer top-level key "
        f"{absent_key}; see {CONTRACT_DOC}"
    )
    assert "schema_version" in _failure_text(failures[0])
    assert absent_key in _failure_text(failures[0])
    assert CONTRACT_DOC in _failure_text(failures[0])
    assert "key set is invalid" not in _failure_text(failures[0])


def test_consumer_completeness_gate_is_noop_for_registered_versions() -> None:
    assert set(ledger.LEDGER_SCHEMA_REGISTRY) == {1, 2}

    assert ledger.validate_retained_ledger(_v1_fixture_payload()) == []
    assert ledger.validate_retained_ledger(_v2_fixture_payload()) == []


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
        nvattest=_nvattest(),
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
        nvattest=_nvattest(),
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
        nvattest=_nvattest(),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    schema = ledger.LEDGER_SCHEMA_REGISTRY[payload["schema_version"]]

    assert set(payload) == schema.top_level_keys
    assert set(payload["policy_run"]) == schema.policy_run_keys
    assert "created_at" not in text
    assert "bundle_digest" not in text
    assert "github" not in text
    assert "db_url" not in text
    assert "url" not in payload["policy_run"]
    assert payload["models"] == _models()
    assert {"name", "sha256", "bytes"} == set(payload["candidate"]["files"][0])
    assert set(payload["tool_evidence"]) == set(preflight.LANE_TOOL_KEYS)
    assert payload["tool_evidence"]["source"]["uv"] == pins.UV_LINUX_FIXTURE_BANNER
    assert set(payload["native_members"]) == set(ledger.PROOF_TARGETS)
    assert set(payload["native_members"]["macos-arm64"]) == {
        *ledger.CORE_SCRIPT_NAMES,
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
            nvattest=_nvattest(),
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
            nvattest=_nvattest(),
        )


def test_ledger_requires_exactly_three_native_records(tmp_path: Path) -> None:
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
            nvattest=_nvattest(),
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
            nvattest=_nvattest(),
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


def test_freshly_written_ledger_round_trips_through_reader(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    payload = ledger.read_retained_ledger(path)

    assert payload["schema_version"] == ledger.CURRENT_LEDGER_SCHEMA_VERSION
    assert (
        set(payload)
        == ledger.LEDGER_SCHEMA_REGISTRY[
            ledger.CURRENT_LEDGER_SCHEMA_VERSION
        ].top_level_keys
    )


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
