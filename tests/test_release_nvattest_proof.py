# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.release_nvattest_proof as proof
from scripts.check_rust_release_manifest import canonical_json_bytes
from scripts.release_install_smoke import SCRUBBED_COMMAND_ENV
from scripts.release_proof_host import TARGET_POLICY
from scripts.release_public_evidence import validate_public_evidence_tree
from solstone.think.providers.nvattest_authority import (
    TARGET_KEYS,
    NvattestTargetKey,
    authority_entry,
    authority_payload,
)
from solstone.think.providers.nvattest_install import SIDECAR_SCHEMA_VERSION
from tests.helpers.nvattest_fixtures import _write_payload_tarball

SOURCE_COMMIT = "a" * 40
CORE_LOCK = "b" * 64
CANDIDATE_DIGEST = "c" * 64
LEDGER_SHA = "d" * 64
CHALLENGE = "e" * 64
VERSION = "1.0.0"
RECORDED_AT = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "nvattest"
PROOF_TARGET_BY_KEY = {
    "linux-x86_64": "linux-x86_64-musl",
    "linux-aarch64": "linux-aarch64-musl",
    "macos-arm64": "macos-arm64",
}
SUPPORT_VERSIONS = {
    "anyio": "4.12.1",
    "certifi": "2026.1.4",
    "h11": "0.16.0",
    "httpcore": "1.0.9",
    "httpx": "0.28.1",
    "idna": "3.11",
    "sniffio": "1.3.1",
    "typing-extensions": "4.15.0",
}


@dataclass(frozen=True)
class SyntheticCase:
    target_key: NvattestTargetKey
    target: str
    candidate_dir: Path
    candidate_paths: tuple[Path, ...]
    support_paths: tuple[Path, ...]
    support_distributions: list[dict[str, Any]]
    archive_path: Path
    manifest_path: Path
    canonical_authority_bytes: bytes
    authority_target: Mapping[str, Any]


def test_production_companion_manifests_match_authority_and_validate() -> None:
    payload = authority_payload()
    for target_key in TARGET_KEYS:
        authority_target = payload["targets"][target_key]
        manifest_identity = authority_target["companion_manifest"]
        path = FIXTURE_DIR / manifest_identity["name"]
        data = path.read_bytes()

        assert hashlib.sha256(data).hexdigest() == manifest_identity["sha256"]
        assert (
            proof.validate_companion_manifest_bytes(
                data,
                target_key=target_key,
                authority_target=authority_target,
            )
            == []
        )


def test_manifest_member_order_differs_from_authority_but_validates() -> None:
    target_key = "linux-x86_64"
    authority_target = authority_payload()["targets"][target_key]
    data = (FIXTURE_DIR / authority_target["companion_manifest"]["name"]).read_bytes()
    manifest = json.loads(data)

    manifest_order = [member["path"] for member in manifest["archive_members"]]
    authority_order = [member["relpath"] for member in authority_target["inventory"]]

    assert manifest_order != authority_order
    assert (
        proof.validate_companion_manifest_bytes(
            data,
            target_key=target_key,
            authority_target=authority_target,
        )
        == []
    )


@pytest.mark.parametrize("target_key", TARGET_KEYS)
def test_synthetic_run_writes_canonical_public_receipt_for_target(
    tmp_path: Path,
    target_key: NvattestTargetKey,
) -> None:
    case = _synthetic_case(tmp_path, target_key)
    output = tmp_path / f"{target_key}.json"
    services = _synthetic_services(case, tmp_path)

    written = proof.run_nvattest_proof(
        target=case.target,
        version=VERSION,
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=CANDIDATE_DIGEST,
        ledger_sha256=LEDGER_SHA,
        challenge=CHALLENGE,
        candidate_dir=case.candidate_dir,
        candidate_paths=case.candidate_paths,
        support_wheel_paths=case.support_paths,
        output_path=output,
        services=services,
        canonical_authority_bytes=case.canonical_authority_bytes,
    )

    data = written.read_bytes()
    payload = json.loads(data)
    assert data == canonical_json_bytes(payload)
    assert validate_public_evidence_tree("nvattest_proof", payload) == []
    assert (
        proof.validate_nvattest_proof_bytes(
            data,
            expected_challenge=CHALLENGE,
            target=case.target,
            version=VERSION,
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=CANDIDATE_DIGEST,
            ledger_sha256=LEDGER_SHA,
            canonical_authority_bytes=case.canonical_authority_bytes,
            expected_support_distributions=case.support_distributions,
        )
        == []
    )
    text = data.decode("utf-8")
    for forbidden in ("/tmp", "/private", "/home", "site-packages", str(tmp_path)):
        assert forbidden not in text
    assert payload["smoke"]["argv"] == [
        f"{proof.NVATTEST_CACHE_ROOT}/bin/nvattest",
        "--help",
    ]
    assert payload["cache_install"]["wheel_install_command"]["argv"][-8:] == [
        f"{proof.SUPPORT}/{entry['filename']}" for entry in case.support_distributions
    ]


def test_command_text_normalization_fails_closed_on_prefix_collision() -> None:
    env_root = Path("/tmp/abc")
    candidate_dir = Path("/tmp/candidate")
    cache_root = env_root / "journal" / "cache" / "providers" / "nvattest"
    collision = "/tmp/abcdef/x"
    child_path = "/tmp/abc/bin/python"
    result = proof.CommandResult(
        argv=(child_path,),
        exit_code=0,
        stdout=f"normalized {child_path}\nleaked {collision}",
        stderr=f"leaked {collision}",
        env=SCRUBBED_COMMAND_ENV,
    )

    payload = proof._command_payload(
        result,
        env_root=env_root,
        candidate_dir=candidate_dir,
        cache_root=cache_root,
        site_roots=(),
    )

    assert payload["argv"] == [f"{proof.ENVROOT}/bin/python"]
    assert f"{proof.ENVROOT}/bin/python" in payload["stdout"]
    assert "ENVROOTdef" not in payload["stdout"]
    assert collision in payload["stdout"]
    assert collision in payload["stderr"]
    assert {
        failure.error
        for failure in validate_public_evidence_tree("nvattest_proof", payload)
    } == {
        "nvattest_proof.stderr contains disallowed content",
        "nvattest_proof.stdout contains disallowed content",
    }


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("challenge", lambda receipt: receipt.update({"challenge": "0" * 64})),
        ("target", lambda receipt: receipt.update({"target": "macos-arm64"})),
        ("version", lambda receipt: receipt.update({"version": "9.9.9"})),
        (
            "source_commit",
            lambda receipt: receipt.update({"source_commit": "f" * 40}),
        ),
        (
            "candidate_digest",
            lambda receipt: receipt.update({"candidate_digest": "1" * 64}),
        ),
        (
            "core_lock_sha256",
            lambda receipt: receipt.update({"core_lock_sha256": "2" * 64}),
        ),
        ("ledger_sha256", lambda receipt: receipt.update({"ledger_sha256": "3" * 64})),
        (
            "authority_digest",
            lambda receipt: receipt["installed_authority"].update({"sha256": "4" * 64}),
        ),
        (
            "archive_hash",
            lambda receipt: receipt["archive_fetch"].update({"sha256": "5" * 64}),
        ),
        (
            "manifest_hash",
            lambda receipt: receipt["manifest_fetch"].update({"sha256": "6" * 64}),
        ),
        (
            "smoke_argv",
            lambda receipt: receipt["smoke"].update({"argv": ["nvattest", "--help"]}),
        ),
        ("smoke_exit", lambda receipt: receipt["smoke"].update({"exit_code": 1})),
    ],
)
def test_validator_rejects_bound_field_mutations(
    tmp_path: Path,
    label: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    del label
    case = _synthetic_case(tmp_path, "linux-x86_64")
    receipt = _synthetic_receipt(case, tmp_path)

    mutate(receipt)

    assert proof.validate_nvattest_proof(
        receipt,
        expected_challenge=CHALLENGE,
        target=case.target,
        version=VERSION,
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=CANDIDATE_DIGEST,
        ledger_sha256=LEDGER_SHA,
        canonical_authority_payload=json.loads(case.canonical_authority_bytes),
        canonical_authority_sha256=hashlib.sha256(
            case.canonical_authority_bytes
        ).hexdigest(),
        expected_support_distributions=case.support_distributions,
    )


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("missing", lambda entries: entries.pop()),
        ("extra", lambda entries: entries.append({**entries[0], "name": "bogus"})),
        ("duplicate", lambda entries: entries.append(dict(entries[0]))),
        ("reordered", lambda entries: entries.reverse()),
        ("mismatched", lambda entries: entries[0].update({"sha256": "0" * 64})),
    ],
)
def test_validator_rejects_support_declaration_mutations(
    tmp_path: Path,
    label: str,
    mutate: Callable[[list[dict[str, Any]]], None],
) -> None:
    del label
    case = _synthetic_case(tmp_path, "linux-x86_64")
    receipt = _synthetic_receipt(case, tmp_path)
    mutated = [dict(entry) for entry in receipt["support_distributions"]]
    mutate(mutated)
    receipt["support_distributions"] = mutated

    failures = proof.validate_nvattest_proof(
        receipt,
        expected_challenge=CHALLENGE,
        target=case.target,
        version=VERSION,
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=CANDIDATE_DIGEST,
        ledger_sha256=LEDGER_SHA,
        canonical_authority_payload=json.loads(case.canonical_authority_bytes),
        canonical_authority_sha256=hashlib.sha256(
            case.canonical_authority_bytes
        ).hexdigest(),
        expected_support_distributions=case.support_distributions,
    )
    assert failures


@pytest.mark.parametrize(
    ("label", "paths_mutator"),
    [
        ("missing", lambda paths, extra: paths[:-1]),
        ("extra", lambda paths, extra: (*paths, extra)),
        ("duplicate", lambda paths, extra: (*paths, paths[0])),
    ],
)
def test_support_wheel_input_rejects_missing_extra_and_duplicate_sets(
    tmp_path: Path,
    label: str,
    paths_mutator: Callable[[tuple[Path, ...], Path], Sequence[Path]],
) -> None:
    del label
    support_dir = tmp_path / "support"
    support_dir.mkdir()
    support_paths = _write_support_wheels(support_dir)
    extra = _write_metadata_wheel(
        support_dir / "urllib3-2.0.0-py3-none-any.whl",
        name="urllib3",
        version="2.0.0",
    )

    with pytest.raises(proof.NvattestProofError):
        proof.support_distribution_entries(paths_mutator(support_paths, extra))


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("schema", lambda manifest: manifest.update({"schema_version": 1})),
        ("version", lambda manifest: manifest["release"].update({"version": "bad"})),
        ("target", lambda manifest: manifest["target"].update({"id": "bad"})),
        ("fork", lambda manifest: manifest["source"].update({"commit": "bad"})),
        (
            "upstream",
            lambda manifest: manifest["source"].update({"upstream_base_commit": "bad"}),
        ),
        (
            "artifact",
            lambda manifest: manifest["artifact"].update({"sha256": "0" * 64}),
        ),
        (
            "inventory",
            lambda manifest: manifest["archive_members"].pop(),
        ),
    ],
)
def test_companion_manifest_semantic_validator_rejects_mutations(
    tmp_path: Path,
    label: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    del label
    case = _synthetic_case(tmp_path, "linux-x86_64")
    manifest = json.loads(case.manifest_path.read_bytes())
    mutate(manifest)

    failures = proof.validate_companion_manifest_bytes(
        canonical_json_bytes(manifest),
        target_key=case.target_key,
        authority_target=case.authority_target,
    )
    assert failures


@pytest.mark.parametrize(
    ("target", "host", "expected"),
    [
        (
            "linux-x86_64-musl",
            proof.HostObservation(os="Linux", arch="x86_64"),
            "linux-x86_64",
        ),
        (
            "linux-aarch64-musl",
            proof.HostObservation(os="Linux", arch="arm64"),
            "linux-aarch64",
        ),
        (
            "macos-arm64",
            proof.HostObservation(os="Darwin", arch="arm64"),
            "macos-arm64",
        ),
    ],
)
def test_host_policy_derives_target_key_without_second_table(
    target: str,
    host: proof.HostObservation,
    expected: str,
) -> None:
    assert proof._target_key_from_policy(target, host) == expected


@pytest.mark.parametrize(
    "host",
    [
        proof.HostObservation(os="Darwin", arch="x86_64"),
        proof.HostObservation(os="Linux", arch="armv7"),
    ],
)
def test_spoofed_or_near_miss_host_fails_before_reach(
    tmp_path: Path,
    host: proof.HostObservation,
) -> None:
    calls: list[str] = []
    services = proof.NvattestProofServices(
        create_environment=lambda _target: calls.append("environment") or tmp_path,
        install_wheels=lambda *_args: (
            calls.append("install") or _command_result(("python",))
        ),
        fetch=lambda *_args: (
            calls.append("fetch") or pytest.fail("fetch should not run")
        ),
        run_package_install=lambda *_args: (
            calls.append("driver") or pytest.fail("driver should not run")
        ),
        integrity_recheck=lambda *_args: {},
        run_smoke=lambda _path: pytest.fail("smoke should not run"),
        clock=lambda: RECORDED_AT,
        cleanup=lambda _path: calls.append("cleanup"),
        observe_host=lambda: host,
    )

    with pytest.raises(proof.NvattestProofError) as exc_info:
        proof.run_nvattest_proof(
            target="macos-arm64",
            version=VERSION,
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=CANDIDATE_DIGEST,
            ledger_sha256=LEDGER_SHA,
            challenge=CHALLENGE,
            candidate_dir=tmp_path,
            candidate_paths=(),
            support_wheel_paths=(),
            output_path=tmp_path / "proof.json",
            services=services,
        )

    assert [failure.error for failure in exc_info.value.failures] == ["host-validation"]
    assert calls == []


def test_receipt_validator_names_policy_negative_cases(tmp_path: Path) -> None:
    case = _synthetic_case(tmp_path, "linux-x86_64")
    receipt = _synthetic_receipt(case, tmp_path)

    mutations = [
        lambda data: data["cache_install"]["wheel_install_command"]["argv"].append(
            "--find-links"
        ),
        lambda data: data["cache_install"]["wheel_install_command"]["argv"].append(
            "--index-url"
        ),
        lambda data: data["installed_package"].update(
            {
                "module_origin": "/home/jer/source/solstone/think/providers/nvattest_install.py"
            }
        ),
        lambda data: data["smoke"].update({"argv": ["nvattest", "--help"]}),
        lambda data: data["support_distributions"].pop(),
    ]
    for mutate in mutations:
        mutated = copy.deepcopy(receipt)
        mutate(mutated)
        assert proof.validate_nvattest_proof(
            mutated,
            expected_challenge=CHALLENGE,
            target=case.target,
            version=VERSION,
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=CANDIDATE_DIGEST,
            ledger_sha256=LEDGER_SHA,
            canonical_authority_payload=json.loads(case.canonical_authority_bytes),
            canonical_authority_sha256=hashlib.sha256(
                case.canonical_authority_bytes
            ).hexdigest(),
            expected_support_distributions=case.support_distributions,
        )


def _synthetic_receipt(case: SyntheticCase, tmp_path: Path) -> dict[str, Any]:
    output = tmp_path / f"{case.target_key}-receipt.json"
    proof.run_nvattest_proof(
        target=case.target,
        version=VERSION,
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=CANDIDATE_DIGEST,
        ledger_sha256=LEDGER_SHA,
        challenge=CHALLENGE,
        candidate_dir=case.candidate_dir,
        candidate_paths=case.candidate_paths,
        support_wheel_paths=case.support_paths,
        output_path=output,
        services=_synthetic_services(case, tmp_path),
        canonical_authority_bytes=case.canonical_authority_bytes,
    )
    return json.loads(output.read_bytes())


def _synthetic_case(tmp_path: Path, target_key: NvattestTargetKey) -> SyntheticCase:
    target = PROOF_TARGET_BY_KEY[target_key]
    candidate_dir = tmp_path / f"candidate-{target_key}"
    candidate_dir.mkdir()
    candidate_paths = (
        _write_metadata_wheel(
            candidate_dir / "solstone-1.0.0-py3-none-any.whl",
            name="solstone",
            version=VERSION,
        ),
    )
    support_dir = tmp_path / f"support-{target_key}"
    support_dir.mkdir()
    support_paths = _write_support_wheels(support_dir)
    support_distributions = proof.support_distribution_entries(support_paths)

    entry = authority_entry(target_key)
    archive_path = tmp_path / f"{target_key}.tar.xz"
    _write_payload_tarball(
        archive_path,
        entry,
        roots=("",),
        label=target_key,
        omitted=set(),
        executable_overrides={},
    )
    archive_bytes = archive_path.read_bytes()
    payload = copy.deepcopy(authority_payload())
    authority_target = payload["targets"][target_key]
    authority_target["artifact"]["size_bytes"] = len(archive_bytes)
    authority_target["artifact"]["sha256"] = hashlib.sha256(archive_bytes).hexdigest()
    manifest = _synthetic_manifest(target_key, authority_target)
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_path = tmp_path / authority_target["companion_manifest"]["name"]
    manifest_path.write_bytes(manifest_bytes)
    authority_target["companion_manifest"]["sha256"] = hashlib.sha256(
        manifest_bytes
    ).hexdigest()
    manifest_path.write_bytes(
        canonical_json_bytes(_synthetic_manifest(target_key, authority_target))
    )
    canonical_authority_bytes = (
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    return SyntheticCase(
        target_key=target_key,
        target=target,
        candidate_dir=candidate_dir,
        candidate_paths=candidate_paths,
        support_paths=support_paths,
        support_distributions=support_distributions,
        archive_path=archive_path,
        manifest_path=manifest_path,
        canonical_authority_bytes=canonical_authority_bytes,
        authority_target=authority_target,
    )


def _synthetic_manifest(
    target_key: NvattestTargetKey,
    authority_target: Mapping[str, Any],
) -> dict[str, Any]:
    source = authority_target["source"]
    artifact = authority_target["artifact"]
    inventory = authority_target["inventory"]
    symlinks = [member for member in inventory if member["kind"] == "symlink"]
    regular = [
        member
        for member in inventory
        if member["kind"] == "regular" and member["relpath"] != "bin/nvattest"
    ]
    member_order = [
        inventory[0],
        *sorted(symlinks, key=lambda item: item["relpath"]),
        *regular,
    ]
    return {
        "archive_members": [
            {
                "kind": member["kind"],
                "link_target": member["symlink_target"],
                "path": member["relpath"],
            }
            for member in member_order
        ],
        "artifact": {
            "name": artifact["name"],
            "sha256": artifact["sha256"],
            "size": artifact["size_bytes"],
        },
        "build_inputs": {"ignored": True},
        "build_tools": {"ignored": True},
        "dependency_pins": [],
        "release": {"sol_revision": "synthetic", "version": source["version"]},
        "schema_version": 2,
        "source": {
            "commit": source["fork_commit"],
            "sol_series_commits": [],
            "source_date_epoch": 0,
            "upstream_base_commit": source["upstream_base"],
        },
        "target": {
            "abi": "synthetic",
            "architecture": TARGET_POLICY[PROOF_TARGET_BY_KEY[target_key]][1],
            "binary_format": "synthetic",
            "id": target_key,
        },
    }


def _synthetic_services(
    case: SyntheticCase,
    tmp_path: Path,
) -> proof.NvattestProofServices:
    env_root = tmp_path / f"env-{case.target_key}"
    policy_os, policy_arch = TARGET_POLICY[case.target]

    def create_environment(_target: str) -> Path:
        (env_root / "bin").mkdir(parents=True)
        python = env_root / "bin" / "python"
        python.write_text("#!/bin/sh\nprintf 'solstone==1.0.0\\n'\n", encoding="utf-8")
        python.chmod(0o755)
        return env_root

    def install_wheels(
        env_python: Path,
        candidate_wheels: Sequence[Path],
        support_wheels: Sequence[Path],
    ) -> proof.CommandResult:
        assert tuple(candidate_wheels) == case.candidate_paths
        assert tuple(support_wheels) == case.support_paths
        site_root = _site_root(env_root)
        authority_path = (
            site_root
            / "solstone"
            / "think"
            / "providers"
            / "nvattest_authority_v1.json"
        )
        authority_path.parent.mkdir(parents=True)
        authority_path.write_bytes(case.canonical_authority_bytes)
        (site_root / "solstone-1.0.0.dist-info").mkdir()
        return _command_result(
            (
                str(env_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                *(str(path) for path in candidate_wheels),
                *(str(path) for path in case.support_paths),
            )
        )

    def fetch(label: str, url: str, dest: Path) -> proof.FetchObservation:
        source = case.archive_path if label == "archive" else case.manifest_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        sha256, size_bytes = proof.file_sha256_size(dest)
        return proof.FetchObservation(
            label=label,
            url=url,
            path=dest,
            sha256=sha256,
            size_bytes=size_bytes,
        )

    def run_package_install(
        env_python: Path,
        driver_path: Path,
        target_key: str,
        journal_path: Path,
    ) -> proof.DriverObservation:
        assert target_key == case.target_key
        payload = _driver_payload(case, env_root, journal_path)
        return proof.DriverObservation(
            command=_command_result(
                (
                    str(env_python),
                    str(driver_path),
                    "--target-key",
                    target_key,
                    "--journal-path",
                    str(journal_path),
                ),
                stdout=json.dumps(payload, sort_keys=True),
            ),
            payload=payload,
        )

    def run_smoke(nvattest_bin: Path) -> proof.CommandResult:
        return _command_result((str(nvattest_bin), "--help"), stdout="usage\n")

    return proof.NvattestProofServices(
        create_environment=create_environment,
        install_wheels=install_wheels,
        fetch=fetch,
        run_package_install=run_package_install,
        integrity_recheck=lambda _journal, _target, _fetches, driver: {
            "members": driver.payload["members"],
            "sidecar": driver.payload["sidecar"],
            "sidecar_path": driver.payload["sidecar_path"],
            "sidecar_sha256": driver.payload["sidecar_sha256"],
            "sidecar_size_bytes": driver.payload["sidecar_size_bytes"],
            "tree_fingerprint_sha256": driver.payload["tree_fingerprint_sha256"],
        },
        run_smoke=run_smoke,
        clock=lambda: RECORDED_AT,
        cleanup=lambda path: shutil.rmtree(path),
        observe_host=lambda: proof.HostObservation(os=policy_os, arch=policy_arch),
    )


def _driver_payload(
    case: SyntheticCase,
    env_root: Path,
    journal_path: Path,
) -> dict[str, Any]:
    site_root = _site_root(env_root)
    authority_path = (
        site_root / "solstone" / "think" / "providers" / "nvattest_authority_v1.json"
    )
    cache_root = journal_path / "cache" / "providers" / "nvattest"
    sidecar_path = cache_root / ".nvattest-install.json"
    fingerprint = "f" * 64
    sidecar = {
        "artifact": dict(case.authority_target["artifact"]),
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "target_key": case.target_key,
        "tree_fingerprint_sha256": fingerprint,
        "version": case.authority_target["source"]["version"],
    }
    sidecar_bytes = canonical_json_bytes(sidecar)
    return {
        "authority_module_file": str(
            site_root / "solstone" / "think" / "providers" / "nvattest_authority.py"
        ),
        "authority_origin": str(
            site_root / "solstone" / "think" / "providers" / "nvattest_authority.py"
        ),
        "authority_path": str(authority_path),
        "authority_sha256": hashlib.sha256(case.canonical_authority_bytes).hexdigest(),
        "authority_size_bytes": len(case.canonical_authority_bytes),
        "cache_root": str(cache_root),
        "dist_info": [
            {
                "dist_info_path": str(site_root / "solstone-1.0.0.dist-info"),
                "name": "solstone",
                "version": VERSION,
            }
        ],
        "journal_path": str(journal_path),
        "members": _member_facts(case.authority_target),
        "module_file": str(
            site_root / "solstone" / "think" / "providers" / "nvattest_install.py"
        ),
        "module_origin": str(
            site_root / "solstone" / "think" / "providers" / "nvattest_install.py"
        ),
        "sidecar": sidecar,
        "sidecar_path": str(sidecar_path),
        "sidecar_sha256": hashlib.sha256(sidecar_bytes).hexdigest(),
        "sidecar_size_bytes": len(sidecar_bytes),
        "site_packages": [str(site_root)],
        "solstone_journal_present": False,
        "spp_nvattest_dir_present": False,
        "tree_fingerprint_sha256": fingerprint,
    }


def _member_facts(authority_target: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "content_sha256": (
                hashlib.sha256(member["relpath"].encode("utf-8")).hexdigest()
                if member["kind"] == "regular"
                else None
            ),
            "executable": member["executable"],
            "kind": member["kind"],
            "relpath": member["relpath"],
            "symlink_target": member["symlink_target"],
        }
        for member in sorted(
            authority_target["inventory"],
            key=lambda item: item["relpath"],
        )
    ]


def _site_root(env_root: Path) -> Path:
    return env_root / "lib" / "python3.13" / "site-packages"


def _write_support_wheels(path: Path) -> tuple[Path, ...]:
    return tuple(
        _write_metadata_wheel(
            path / f"{name.replace('-', '_')}-{version}-py3-none-any.whl",
            name=name,
            version=version,
        )
        for name, version in sorted(SUPPORT_VERSIONS.items())
    )


def _write_metadata_wheel(path: Path, *, name: str, version: str) -> Path:
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(
            f"{dist_info}/METADATA",
            f"Name: {name}\nVersion: {version}\n",
        )
    return path


def _command_result(
    argv: Sequence[str],
    *,
    stdout: str = "",
    exit_code: int = 0,
) -> proof.CommandResult:
    return proof.CommandResult(
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        env=SCRUBBED_COMMAND_ENV,
    )
