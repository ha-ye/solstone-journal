# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OCI registry helpers used by CUDA runtime repacking."""

from __future__ import annotations

import hashlib
import re
import shutil
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

_GHCR_HOST = "ghcr.io"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class OciImageError(RuntimeError):
    """OCI registry or layer processing failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sha256(path: Path, expected: str) -> None:
    actual = _sha256_file(path)
    if actual != expected:
        raise OciImageError(
            "sha256_mismatch",
            f"sha256 mismatch for {path.name}: expected {expected}, got {actual}",
        )


def _fetch_token(client: httpx.Client, repo: str) -> str:
    url = (
        f"https://{_GHCR_HOST}/token?service={_GHCR_HOST}&scope=repository:{repo}:pull"
    )
    try:
        response = client.get(url)
        response.raise_for_status()
        data = response.json()
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("token response did not contain a token")
        return token
    except Exception as exc:
        raise OciImageError(
            "token_fetch_failed", f"failed to fetch OCI registry token: {exc}"
        ) from exc


def _fetch_image_manifest(
    client: httpx.Client,
    repo: str,
    digest: str,
    arch: str,
    token: str,
) -> dict[str, Any]:
    headers = _registry_headers(token)
    top = _fetch_manifest_json(client, repo, f"sha256:{digest}", headers)
    manifests = top.get("manifests")
    if isinstance(manifests, list):
        selected_digest = _select_arch_manifest(manifests, arch)
        return _fetch_manifest_json(client, repo, selected_digest, headers)
    if isinstance(top.get("layers"), list):
        return top
    raise OciImageError(
        "manifest_fetch_failed", "OCI manifest response was not an index or image"
    )


def _fetch_manifest_json(
    client: httpx.Client,
    repo: str,
    digest: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    url = f"https://{_GHCR_HOST}/v2/{repo}/manifests/{digest}"
    try:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("manifest JSON was not an object")
        return data
    except OciImageError:
        raise
    except Exception as exc:
        raise OciImageError(
            "manifest_fetch_failed", f"failed to fetch OCI manifest {digest}: {exc}"
        ) from exc


def _select_arch_manifest(manifests: list[Any], arch: str) -> str:
    for entry in manifests:
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform")
        if not isinstance(platform, dict):
            continue
        if platform.get("os") == "linux" and platform.get("architecture") == arch:
            digest = entry.get("digest")
            if _valid_digest_ref(digest):
                return digest
            raise OciImageError(
                "manifest_fetch_failed", "selected manifest had an invalid digest"
            )
    raise OciImageError("arch_unavailable", f"OCI image does not contain linux/{arch}")


def _layer_digests(manifest: dict[str, Any]) -> list[str]:
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        raise OciImageError("manifest_fetch_failed", "OCI image manifest lacks layers")
    digests: list[str] = []
    for layer in layers:
        if not isinstance(layer, dict) or not _valid_digest_ref(layer.get("digest")):
            raise OciImageError(
                "manifest_fetch_failed",
                "OCI image manifest has an invalid layer digest",
            )
        digests.append(layer["digest"])
    return digests


def _valid_digest_ref(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    return _DIGEST_RE.fullmatch(value.removeprefix("sha256:")) is not None


def _registry_headers(token: str) -> dict[str, str]:
    return {
        "Accept": _MANIFEST_ACCEPT,
        "Authorization": f"Bearer {token}",
    }


def _download_blob(
    client: httpx.Client,
    repo: str,
    digest_ref: str,
    dest: Path,
    token: str,
) -> None:
    expected = digest_ref.removeprefix("sha256:")
    url = f"https://{_GHCR_HOST}/v2/{repo}/blobs/{digest_ref}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    actual_digest = hashlib.sha256()
    try:
        with client.stream("GET", url, headers=_registry_headers(token)) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        actual_digest.update(chunk)
                        handle.write(chunk)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise OciImageError(
            "blob_fetch_failed", f"failed to fetch OCI blob {digest_ref}: {exc}"
        ) from exc

    actual = actual_digest.hexdigest()
    if actual != expected:
        tmp.unlink(missing_ok=True)
        raise OciImageError(
            "sha256_mismatch",
            f"sha256 mismatch for blob {digest_ref}: expected {expected}, got {actual}",
        )
    try:
        tmp.replace(dest)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise OciImageError(
            "blob_fetch_failed", f"failed to store OCI blob {digest_ref}: {exc}"
        ) from exc


def _extract_layer(layer: Path, rootfs: Path) -> None:
    rootfs.mkdir(parents=True, exist_ok=True)
    rootfs_resolved = rootfs.resolve()
    try:
        with tarfile.open(layer, "r:*") as archive:
            for member in archive.getmembers():
                target = _safe_member_target(rootfs, rootfs_resolved, member.name)
                basename = Path(member.name).name
                if basename == ".wh..wh..opq":
                    _clear_directory(target.parent)
                    continue
                if basename.startswith(".wh."):
                    _remove_path(target.parent / basename.removeprefix(".wh."))
                    continue
                _extract_member(archive, member, rootfs, target)
    except OciImageError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise OciImageError(
            "extract_failed", f"failed to extract OCI layer {layer.name}: {exc}"
        ) from exc


def _safe_member_target(rootfs: Path, rootfs_resolved: Path, member_name: str) -> Path:
    target = (rootfs / member_name).resolve()
    if target != rootfs_resolved and rootfs_resolved not in target.parents:
        raise OciImageError(
            "archive_path_traversal", f"Unsafe tar member path: {member_name}"
        )
    return target


def _extract_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    rootfs: Path,
    target: Path,
) -> None:
    if member.isdir():
        if target.exists() and not target.is_dir():
            _remove_path(target)
        target.mkdir(parents=True, exist_ok=True)
        return
    if target.exists() or target.is_symlink():
        _remove_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    archive.extract(member, rootfs, filter="fully_trusted")


def _clear_directory(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        _remove_path(child)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _resolved_under_root(path: Path, rootfs_resolved: Path) -> Path:
    resolved = path.resolve()
    if resolved != rootfs_resolved and rootfs_resolved not in resolved.parents:
        raise OciImageError(
            "archive_path_traversal", f"Resolved file escaped rootfs: {path}"
        )
    return resolved


__all__ = ["OciImageError"]
