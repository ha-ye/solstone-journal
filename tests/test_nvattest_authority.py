# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib.util
import json
import platform
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from solstone.think.providers import nvattest_authority

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_nvattest_authority.py"
AUTHORITY_PATH = (
    REPO_ROOT / "solstone" / "think" / "providers" / "nvattest_authority.py"
)
MIRROR_PATH = (
    REPO_ROOT / "solstone" / "think" / "providers" / "nvattest_authority_v1.json"
)
INSTALL_PATH = REPO_ROOT / "solstone" / "think" / "providers" / "nvattest_install.py"
LEGACY_NVIDIA_SHA256 = (
    "3f10da6fca794b7e3025c6645447947ec8bc45bcfde5b5b1d23241c7115630db"
)
EXPECTED_TARGETS = {
    "linux-x86_64": {
        "artifact": {
            "name": "libnvat-linux-x86_64-1.2.2-sol.2-archive.tar.xz",
            "url": "https://updates.solstone.app/providers/nvattest/libnvat-linux-x86_64-1.2.2-sol.2-archive.tar.xz",
            "size_bytes": 7655628,
            "sha256": "3e2d207a3bb6eab9c47fc9cf65d7990f0d11541ff8691dce18e786e2ce9b26c7",
        },
        "companion_manifest": {
            "name": "libnvat-linux-x86_64-1.2.2-sol.2-archive.manifest.json",
            "url": "https://updates.solstone.app/providers/nvattest/libnvat-linux-x86_64-1.2.2-sol.2-archive.manifest.json",
            "sha256": "a013d18a74d89f3ee99a0a3648c582df08446a30f5b8f5bbb48929a08fad4a8f",
        },
    },
    "linux-aarch64": {
        "artifact": {
            "name": "libnvat-linux-aarch64-1.2.2-sol.2-archive.tar.xz",
            "url": "https://updates.solstone.app/providers/nvattest/libnvat-linux-aarch64-1.2.2-sol.2-archive.tar.xz",
            "size_bytes": 7423268,
            "sha256": "7a13a15192f5005a700dbe154da28cbc2a2b5f3f387113c8cd89a36083a2bd80",
        },
        "companion_manifest": {
            "name": "libnvat-linux-aarch64-1.2.2-sol.2-archive.manifest.json",
            "url": "https://updates.solstone.app/providers/nvattest/libnvat-linux-aarch64-1.2.2-sol.2-archive.manifest.json",
            "sha256": "0d46ffdf21a9c5a61370fbf77aed0c275dae93f3609511f4573515a7260ddc39",
        },
    },
    "macos-arm64": {
        "artifact": {
            "name": "libnvat-macos-arm64-1.2.2-sol.2-archive.tar.xz",
            "url": "https://updates.solstone.app/providers/nvattest/libnvat-macos-arm64-1.2.2-sol.2-archive.tar.xz",
            "size_bytes": 5848264,
            "sha256": "f2b01f60ee52f9c38c9e5e0f3cea6e8078a1efa165fad1f5c59994563120e365",
        },
        "companion_manifest": {
            "name": "libnvat-macos-arm64-1.2.2-sol.2-archive.manifest.json",
            "url": "https://updates.solstone.app/providers/nvattest/libnvat-macos-arm64-1.2.2-sol.2-archive.manifest.json",
            "sha256": "dca5342f87bba1244f8ee78848207410ae5ee8976df42fe10d804f1c99167094",
        },
    },
}


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_nvattest_authority", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload() -> dict:
    return deepcopy(nvattest_authority.authority_payload())


def _assert_rejected(payload: dict) -> None:
    with pytest.raises(ValueError):
        nvattest_authority.validate_authority_payload(payload)


def test_authority_payload_contains_exact_operator_literals() -> None:
    payload = nvattest_authority.authority_payload()
    assert payload["schema_version"] == 1
    targets = payload["targets"]
    assert set(targets) == set(EXPECTED_TARGETS)
    for target_key, expected in EXPECTED_TARGETS.items():
        target = targets[target_key]
        assert target["source"] == {
            "fork_commit": "1c95b1f5ae72b5f4282f7b6b96722f7c8d69f744",
            "upstream_base": "73c032ebff680ca6d2ba06f4006b511491b71ce9",
            "url_prefix": "https://updates.solstone.app/providers/nvattest/",
            "version": "1.2.2-sol.2",
        }
        assert target["artifact"] == expected["artifact"]
        assert target["companion_manifest"] == expected["companion_manifest"]


def test_authority_payload_contains_exact_inventories() -> None:
    payload = nvattest_authority.authority_payload()
    linux_inventory = [
        {
            "executable": True,
            "kind": "regular",
            "relpath": "bin/nvattest",
            "symlink_target": None,
        },
        {
            "executable": True,
            "kind": "regular",
            "relpath": "lib/libnvat.so.1.2.2",
            "symlink_target": None,
        },
        {
            "executable": False,
            "kind": "symlink",
            "relpath": "lib/libnvat.so.1",
            "symlink_target": "libnvat.so.1.2.2",
        },
        {
            "executable": False,
            "kind": "symlink",
            "relpath": "lib/libnvat.so",
            "symlink_target": "libnvat.so.1",
        },
        {
            "executable": False,
            "kind": "regular",
            "relpath": "LICENSE",
            "symlink_target": None,
        },
        {
            "executable": False,
            "kind": "regular",
            "relpath": "share/ca/ca-bundle.pem",
            "symlink_target": None,
        },
        {
            "executable": False,
            "kind": "regular",
            "relpath": "share/THIRD_PARTY_NOTICES.md",
            "symlink_target": None,
        },
    ]
    macos_inventory = [
        {**linux_inventory[0]},
        {
            "executable": True,
            "kind": "regular",
            "relpath": "lib/libnvat.1.2.2.dylib",
            "symlink_target": None,
        },
        {
            "executable": False,
            "kind": "symlink",
            "relpath": "lib/libnvat.1.dylib",
            "symlink_target": "libnvat.1.2.2.dylib",
        },
        {
            "executable": False,
            "kind": "symlink",
            "relpath": "lib/libnvat.dylib",
            "symlink_target": "libnvat.1.dylib",
        },
        *linux_inventory[4:],
    ]

    assert payload["targets"]["linux-x86_64"]["inventory"] == linux_inventory
    assert payload["targets"]["linux-aarch64"]["inventory"] == linux_inventory
    assert payload["targets"]["macos-arm64"]["inventory"] == macos_inventory


def test_authority_json_matches_constants() -> None:
    actual = json.loads(MIRROR_PATH.read_text(encoding="utf-8"))
    assert actual == nvattest_authority.authority_payload()


def test_builder_check_is_bidirectional(monkeypatch, tmp_path, capsys) -> None:
    builder = _load_builder()
    artifact = (
        tmp_path / "solstone" / "think" / "providers" / "nvattest_authority_v1.json"
    )
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "ARTIFACT_PATH", artifact)

    builder.write_outputs()
    assert builder.check_outputs() == 0

    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("{", "[", 1),
        encoding="utf-8",
    )
    assert builder.check_outputs() == 1
    captured = capsys.readouterr()
    assert (
        "nvattest authority is stale: "
        "solstone/think/providers/nvattest_authority_v1.json. "
        "Run: make nvattest-authority"
    ) in captured.err


def test_authority_rejects_missing_target() -> None:
    payload = _payload()
    payload["targets"].pop("macos-arm64")
    _assert_rejected(payload)


def test_authority_rejects_extra_target() -> None:
    payload = _payload()
    payload["targets"]["linux-ppc64le"] = deepcopy(payload["targets"]["linux-x86_64"])
    _assert_rejected(payload)


def test_authority_rejects_missing_inventory_member() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["inventory"].pop()
    _assert_rejected(payload)


def test_authority_rejects_extra_inventory_member() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["inventory"].append(
        {
            "executable": False,
            "kind": "regular",
            "relpath": "share/extra.txt",
            "symlink_target": None,
        }
    )
    _assert_rejected(payload)


def test_authority_rejects_duplicate_artifact_identity() -> None:
    payload = _payload()
    payload["targets"]["linux-aarch64"]["artifact"] = deepcopy(
        payload["targets"]["linux-x86_64"]["artifact"]
    )
    _assert_rejected(payload)


def test_authority_rejects_duplicate_manifest_identity() -> None:
    payload = _payload()
    payload["targets"]["linux-aarch64"]["companion_manifest"] = deepcopy(
        payload["targets"]["linux-x86_64"]["companion_manifest"]
    )
    _assert_rejected(payload)


def test_authority_rejects_non_https_url() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["artifact"]["url"] = payload["targets"][
        "linux-x86_64"
    ]["artifact"]["url"].replace("https://", "http://")
    _assert_rejected(payload)


def test_authority_rejects_url_not_equal_to_prefix_plus_name() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["artifact"]["url"] = (
        "https://updates.solstone.app/providers/nvattest/other.tar.xz"
    )
    _assert_rejected(payload)


@pytest.mark.parametrize(
    "bad_hash",
    [
        "0" * 63,
        "A" + "0" * 63,
        "g" + "0" * 63,
    ],
)
def test_authority_rejects_malformed_hashes(bad_hash: str) -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["artifact"]["sha256"] = bad_hash
    _assert_rejected(payload)


@pytest.mark.parametrize("bad_size", [0, -1])
def test_authority_rejects_nonpositive_sizes(bad_size: int) -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["artifact"]["size_bytes"] = bad_size
    _assert_rejected(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "1.2.2-sol.1"),
        ("fork_commit", "0" * 40),
        ("upstream_base", "1" * 40),
    ],
)
def test_authority_rejects_source_drift(field: str, value: str) -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["source"][field] = value
    _assert_rejected(payload)


def test_authority_rejects_absolute_member_relpath() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["inventory"][0]["relpath"] = "/bin/nvattest"
    _assert_rejected(payload)


def test_authority_rejects_parent_member_relpath() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["inventory"][0]["relpath"] = "bin/../nvattest"
    _assert_rejected(payload)


def test_authority_rejects_absolute_symlink_target() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["inventory"][2]["symlink_target"] = (
        "/tmp/libnvat.so"
    )
    _assert_rejected(payload)


def test_authority_rejects_parent_symlink_target() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["inventory"][2]["symlink_target"] = (
        "../libnvat.so"
    )
    _assert_rejected(payload)


def test_authority_rejects_name_target_disagreement() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["artifact"]["name"] = (
        "libnvat-linux-aarch64-1.2.2-sol.2-archive.tar.xz"
    )
    _assert_rejected(payload)


def test_authority_rejects_linux_target_with_macos_inventory() -> None:
    payload = _payload()
    payload["targets"]["linux-x86_64"]["inventory"] = deepcopy(
        payload["targets"]["macos-arm64"]["inventory"]
    )
    _assert_rejected(payload)


@pytest.mark.parametrize(
    ("os_name", "arch", "expected"),
    [
        ("linux", "x86_64", "linux-x86_64"),
        ("linux", "amd64", "linux-x86_64"),
        ("linux", "x64", "linux-x86_64"),
        ("linux", "aarch64", "linux-aarch64"),
        ("linux", "arm64", "linux-aarch64"),
        ("darwin", "arm64", "macos-arm64"),
    ],
)
def test_target_resolver_accepts_supported_aliases(
    os_name: str,
    arch: str,
    expected: str,
) -> None:
    assert nvattest_authority.nvattest_target_key(os_name, arch) == expected


@pytest.mark.parametrize(
    ("os_name", "arch"),
    [
        ("macos", "arm64"),
        ("Darwin", "arm64"),
        ("linux", "armv8"),
        ("linux", "arm64-v8a"),
        ("linux", "x86"),
        ("linux", "i386"),
        ("darwin", "aarch64"),
    ],
)
def test_target_resolver_rejects_named_near_misses(os_name: str, arch: str) -> None:
    assert nvattest_authority.nvattest_target_key(os_name, arch) is None


def test_target_resolver_default_host_uses_python_platform_values() -> None:
    os_name = "linux" if sys.platform.startswith("linux") else sys.platform
    arch = platform.machine().lower()
    expected = None
    if os_name == "linux" and arch in {"x86_64", "amd64", "x64"}:
        expected = "linux-x86_64"
    if os_name == "linux" and arch in {"aarch64", "arm64"}:
        expected = "linux-aarch64"
    if os_name == "darwin" and arch == "arm64":
        expected = "macos-arm64"

    assert nvattest_authority.nvattest_target_key() == expected
    if expected is not None:
        assert expected in nvattest_authority.TARGET_KEYS


def test_nvattest_sources_do_not_point_to_legacy_nvidia_artifact() -> None:
    source = "\n".join(
        [
            AUTHORITY_PATH.read_text(encoding="utf-8"),
            INSTALL_PATH.read_text(encoding="utf-8"),
        ]
    )
    assert "developer.download.nvidia.com" not in source
    assert LEGACY_NVIDIA_SHA256 not in source
