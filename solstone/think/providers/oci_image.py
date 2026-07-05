# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Acquire selected files from pinned OCI images.

This module performs no network access at import time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    import httpx

LOG = logging.getLogger(__name__)

SIDECAR_NAME = ".oci-install.json"
_GHCR_HOST = "ghcr.io"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_COSIGN_TIMEOUT_SECONDS = 60.0
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class OciImageError(RuntimeError):
    """OCI image acquisition failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class OciInstallRecord:
    image_ref: str
    arch: str
    files: dict[str, str]

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "arch": self.arch,
                    "files": self.files,
                    "image_ref": self.image_ref,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    @classmethod
    def from_json(cls, text: str) -> OciInstallRecord:
        data = json.loads(text)
        files = data["files"]
        if not isinstance(files, dict):
            raise ValueError("OCI install record files must be an object")
        return cls(
            image_ref=str(data["image_ref"]),
            arch=str(data["arch"]),
            files={str(name): str(digest) for name, digest in files.items()},
        )


@dataclass(frozen=True)
class OciInstallResult:
    target_dir: Path
    files: dict[str, str]
    already_present: bool


@dataclass(frozen=True, kw_only=True)
class OciSignaturePolicy:
    certificate_identity: str | None = None
    certificate_identity_regexp: str | None = None
    oidc_issuer: str

    def __post_init__(self) -> None:
        has_identity = bool(self.certificate_identity)
        has_regexp = bool(self.certificate_identity_regexp)
        if has_identity == has_regexp:
            raise ValueError(
                "exactly one of certificate_identity or "
                "certificate_identity_regexp is required"
            )
        if not self.oidc_issuer:
            raise ValueError("oidc_issuer is required")


def pull_and_install(
    image_ref: str,
    arch: str,
    wanted_files: Sequence[str],
    target_dir: Path,
    *,
    client: httpx.Client | None = None,
    policy: OciSignaturePolicy | None = None,
    verifier: Callable[[str, OciSignaturePolicy], None] | None = None,
) -> OciInstallResult:
    repo, digest = _parse_image_ref(image_ref)
    wanted = _validate_wanted_files(wanted_files)
    target_dir = Path(target_dir)

    offline = _offline_result(image_ref, arch, wanted, target_dir)
    if offline is not None:
        return offline

    if policy is not None:
        (verifier or verify_image_signature)(image_ref, policy)

    import httpx

    created_client = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=600.0)

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(dir=target_dir.parent))
    try:
        token = _fetch_token(client, repo)
        manifest = _fetch_image_manifest(client, repo, digest, arch, token)
        rootfs = work_root / "rootfs"
        blobs_dir = work_root / "blobs"
        rootfs.mkdir()
        blobs_dir.mkdir()
        for index, layer_digest in enumerate(_layer_digests(manifest), start=1):
            blob_path = blobs_dir / f"layer-{index}.tar"
            _download_blob(client, repo, layer_digest, blob_path, token)
            _extract_layer(blob_path, rootfs)
        selected = _find_wanted_files(rootfs, wanted)
        files = _publish_install(image_ref, arch, selected, target_dir)
        return OciInstallResult(
            target_dir=target_dir,
            files=files,
            already_present=False,
        )
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        if created_client:
            client.close()


def verify_image_signature(image_ref: str, policy: OciSignaturePolicy) -> None:
    command = ["cosign", "verify", image_ref]
    if policy.certificate_identity is not None:
        command.extend(["--certificate-identity", policy.certificate_identity])
    elif policy.certificate_identity_regexp is not None:
        command.extend(
            ["--certificate-identity-regexp", policy.certificate_identity_regexp]
        )
    command.extend(["--certificate-oidc-issuer", policy.oidc_issuer])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_COSIGN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OciImageError(
            "signature_verify_failed",
            f"cosign verify timed out after {_COSIGN_TIMEOUT_SECONDS:g}s",
        ) from exc
    except OSError as exc:
        raise OciImageError(
            "cosign_missing", f"cosign verify could not start: {exc}"
        ) from exc

    if completed.returncode == 0:
        return

    detail = (
        (completed.stderr or "").strip()
        or (completed.stdout or "").strip()
        or f"exited with status {completed.returncode}"
    )
    raise OciImageError(
        "signature_verify_failed", f"cosign verify failed for {image_ref}: {detail}"
    )


def _parse_image_ref(image_ref: str) -> tuple[str, str]:
    if "@" not in image_ref:
        raise OciImageError(
            "invalid_image_ref", f"OCI image ref must be pinned by digest: {image_ref}"
        )
    repo_part, digest_part = image_ref.split("@", 1)
    if repo_part.startswith(f"{_GHCR_HOST}/"):
        repo = repo_part[len(_GHCR_HOST) + 1 :]
    else:
        first = repo_part.split("/", 1)[0]
        if "." in first or ":" in first or first == "localhost":
            raise OciImageError(
                "invalid_image_ref", f"OCI image ref must use ghcr.io: {image_ref}"
            )
        repo = repo_part
    if not repo:
        raise OciImageError(
            "invalid_image_ref", f"OCI image ref must include a repository: {image_ref}"
        )
    if not digest_part.startswith("sha256:"):
        raise OciImageError(
            "invalid_image_ref", f"OCI image ref must use a sha256 digest: {image_ref}"
        )
    digest = digest_part.removeprefix("sha256:")
    if not _DIGEST_RE.fullmatch(digest):
        raise OciImageError(
            "invalid_image_ref",
            f"OCI image ref digest must be 64 lowercase hex chars: {image_ref}",
        )
    return repo, digest


def _validate_wanted_files(wanted_files: Sequence[str]) -> list[str]:
    wanted: list[str] = []
    seen: set[str] = set()
    for name in wanted_files:
        if (
            not name
            or name == "."
            or name == SIDECAR_NAME
            or "/" in name
            or ".." in name
        ):
            raise OciImageError(
                "invalid_wanted_file", f"wanted file must be a basename: {name!r}"
            )
        if name not in seen:
            wanted.append(name)
            seen.add(name)
    return wanted


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


def _offline_result(
    image_ref: str,
    arch: str,
    wanted: Sequence[str],
    target_dir: Path,
) -> OciInstallResult | None:
    record = _verify_sidecar(image_ref, arch, wanted, target_dir)
    if record is None:
        return None
    return OciInstallResult(
        target_dir=target_dir,
        files=dict(record.files),
        already_present=True,
    )


def _verify_sidecar(
    image_ref: str,
    arch: str,
    wanted: Sequence[str],
    target_dir: Path,
) -> OciInstallRecord | None:
    sidecar = target_dir / SIDECAR_NAME
    try:
        record = OciInstallRecord.from_json(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return None
    if record.image_ref != image_ref or record.arch != arch:
        return None
    for name in wanted:
        expected = record.files.get(name)
        if expected is None:
            return None
        path = target_dir / name
        if not path.is_file():
            return None
        try:
            _verify_sha256(path, expected)
        except OciImageError:
            return None
    return record


def verify_sidecar_install(
    image_ref: str,
    arch: str,
    wanted_files: Sequence[str],
    target_dir: Path,
) -> bool:
    wanted = _validate_wanted_files(wanted_files)
    return _verify_sidecar(image_ref, arch, wanted, Path(target_dir)) is not None


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


def _find_wanted_files(rootfs: Path, wanted: Sequence[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    rootfs_resolved = rootfs.resolve()
    for name in wanted:
        found[name] = _find_wanted_file(rootfs, rootfs_resolved, name)
    return found


def _find_wanted_file(rootfs: Path, rootfs_resolved: Path, name: str) -> Path:
    direct = rootfs / name
    if direct.is_file():
        return _resolved_under_root(direct, rootfs_resolved)
    matches = [path for path in rootfs.rglob(name) if path.is_file()]
    if not matches:
        raise OciImageError(
            "wanted_file_missing", f"Extracted image did not contain {name}"
        )
    matches.sort(key=lambda path: (len(path.relative_to(rootfs).parts), str(path)))
    return _resolved_under_root(matches[0], rootfs_resolved)


def _resolved_under_root(path: Path, rootfs_resolved: Path) -> Path:
    resolved = path.resolve()
    if resolved != rootfs_resolved and rootfs_resolved not in resolved.parents:
        raise OciImageError(
            "archive_path_traversal", f"Resolved file escaped rootfs: {path}"
        )
    return resolved


def _publish_install(
    image_ref: str,
    arch: str,
    selected: dict[str, Path],
    target_dir: Path,
) -> dict[str, str]:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target_dir.parent))
    aside: Path | None = None
    published = False
    try:
        files: dict[str, str] = {}
        for name, source in selected.items():
            dest = staging / name
            shutil.copy2(source, dest)
            files[name] = _sha256_file(dest)
        record = OciInstallRecord(image_ref=image_ref, arch=arch, files=files)
        (staging / SIDECAR_NAME).write_text(record.to_json(), encoding="utf-8")

        if target_dir.exists():
            aside = Path(tempfile.mkdtemp(dir=target_dir.parent))
            target_dir.replace(aside)
        try:
            staging.replace(target_dir)
            published = True
        except Exception:
            _restore_aside(aside, target_dir)
            raise

        if aside is not None:
            try:
                shutil.rmtree(aside)
            except OSError:
                LOG.warning(
                    "failed to remove previous OCI install target", exc_info=True
                )
            aside = None
        return files
    except Exception as exc:
        if not published:
            _restore_aside(aside, target_dir)
        raise OciImageError(
            "install_failed", f"failed to install OCI image files: {exc}"
        ) from exc
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if not published and aside is not None and aside.exists():
            shutil.rmtree(aside, ignore_errors=True)


def _restore_aside(aside: Path | None, target_dir: Path) -> None:
    if aside is None or not aside.exists() or target_dir.exists():
        return
    try:
        aside.replace(target_dir)
    except Exception:
        LOG.exception("failed to restore previous OCI install target")
        raise


__all__ = [
    "OciImageError",
    "OciInstallRecord",
    "OciInstallResult",
    "OciSignaturePolicy",
    "SIDECAR_NAME",
    "pull_and_install",
    "verify_image_signature",
    "verify_sidecar_install",
]
