# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from solstone.think.providers import oci_image


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_ref(data: bytes) -> str:
    return f"sha256:{_sha256_bytes(data)}"


def _layer_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _write_layer(path: Path, entries: dict[str, bytes]) -> None:
    path.write_bytes(_layer_bytes(entries))


class _Response:
    def __init__(self, data: bytes, *, status_error: Exception | None = None) -> None:
        self._data = data
        self._status_error = status_error

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    def iter_bytes(self) -> list[bytes]:
        return [self._data]


class _Client:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def stream(self, method: str, url: str, *, headers: dict[str, str]) -> _Response:
        self.calls.append((method, url, headers))
        return _Response(self.data)


def test_retained_repack_import_surface_and_owner_apis_absent() -> None:
    for name in (
        "_MANIFEST_ACCEPT",
        "_download_blob",
        "_extract_layer",
        "_fetch_token",
        "_layer_digests",
        "_registry_headers",
        "_resolved_under_root",
        "_select_arch_manifest",
        "_sha256_file",
        "_valid_digest_ref",
        "_verify_sha256",
        "OciImageError",
    ):
        assert hasattr(oci_image, name)

    for retired in (
        "OciInstallRecord",
        "OciInstallResult",
        "OciSignaturePolicy",
        "SIDECAR_NAME",
        "pull_and_install",
        "verify_image_signature",
        "verify_sidecar_install",
    ):
        assert not hasattr(oci_image, retired)


def test_select_arch_manifest_and_layer_digests_validate_digest_refs() -> None:
    manifest_ref = "sha256:" + "a" * 64
    arm_ref = "sha256:" + "b" * 64
    manifests: list[dict[str, Any]] = [
        {
            "digest": manifest_ref,
            "platform": {"os": "linux", "architecture": "amd64"},
        },
        {
            "digest": arm_ref,
            "platform": {"os": "linux", "architecture": "arm64"},
        },
    ]

    assert oci_image._select_arch_manifest(manifests, "arm64") == arm_ref
    assert oci_image._layer_digests({"layers": [{"digest": manifest_ref}]}) == [
        manifest_ref
    ]

    with pytest.raises(oci_image.OciImageError) as exc_info:
        oci_image._select_arch_manifest(
            [{"digest": "sha256:nothex", "platform": manifests[0]["platform"]}],
            "amd64",
        )
    assert exc_info.value.reason_code == "manifest_fetch_failed"

    with pytest.raises(oci_image.OciImageError) as exc_info:
        oci_image._layer_digests({"layers": [{"digest": "sha256:nothex"}]})
    assert exc_info.value.reason_code == "manifest_fetch_failed"


def test_download_blob_writes_verified_blob(tmp_path: Path) -> None:
    data = b"blob"
    client = _Client(data)
    dest = tmp_path / "blob.tar"

    oci_image._download_blob(client, "acme/tool", _digest_ref(data), dest, "token")

    assert dest.read_bytes() == data
    assert client.calls == [
        (
            "GET",
            f"https://ghcr.io/v2/acme/tool/blobs/{_digest_ref(data)}",
            oci_image._registry_headers("token"),
        )
    ]


def test_download_blob_rejects_sha256_mismatch(tmp_path: Path) -> None:
    client = _Client(b"actual")
    dest = tmp_path / "blob.tar"

    with pytest.raises(oci_image.OciImageError) as exc_info:
        oci_image._download_blob(client, "acme/tool", "sha256:" + "0" * 64, dest, "t")

    assert exc_info.value.reason_code == "sha256_mismatch"
    assert not dest.exists()
    assert not (tmp_path / "blob.tar.tmp").exists()


def test_ac5_path_traversal_is_rejected(tmp_path: Path) -> None:
    layer = tmp_path / "layer.tar.gz"
    _write_layer(layer, {"../escape": b"bad"})
    rootfs = tmp_path / "rootfs"

    with pytest.raises(oci_image.OciImageError) as exc_info:
        oci_image._extract_layer(layer, rootfs)

    assert exc_info.value.reason_code == "archive_path_traversal"
    assert not (tmp_path / "escape").exists()


def test_corrupt_layer_raises_extract_failed(tmp_path: Path) -> None:
    layer = tmp_path / "layer.tar.gz"
    layer.write_bytes(b"this is not a tarball")

    with pytest.raises(oci_image.OciImageError) as exc_info:
        oci_image._extract_layer(layer, tmp_path / "rootfs")

    assert exc_info.value.reason_code == "extract_failed"


def test_whiteout_and_opaque_directory_remove_earlier_files(tmp_path: Path) -> None:
    layer_one = tmp_path / "one.tar.gz"
    _write_layer(layer_one, {"bin/tool": b"tool", "app/old": b"old"})
    layer_two = tmp_path / "two.tar.gz"
    _write_layer(
        layer_two,
        {
            "bin/.wh.tool": b"",
            "app/.wh..wh..opq": b"",
            "app/new": b"new",
        },
    )
    rootfs = tmp_path / "rootfs"

    oci_image._extract_layer(layer_one, rootfs)
    oci_image._extract_layer(layer_two, rootfs)

    assert not (rootfs / "bin" / "tool").exists()
    assert not (rootfs / "app" / "old").exists()
    assert (rootfs / "app" / "new").read_bytes() == b"new"
