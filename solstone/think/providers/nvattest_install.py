# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and locate NVIDIA nvattest runtime artifacts.

This module performs no network access at import time.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from solstone.think.journal_io import LockTimeout
from solstone.think.journal_io.locking import hold_lock
from solstone.think.providers.install_state import (
    canonical_fingerprint,
    fingerprint_sha256,
)
from solstone.think.providers.nvattest_authority import (
    NvattestTargetEntry,
    authority_entry,
    nvattest_target_key,
)
from solstone.think.providers.rfdetr_install import (
    RfdetrInstallError,
)
from solstone.think.providers.rfdetr_install import (
    _safe_extract_tarball as _rfdetr_safe_extract_tarball,
)
from solstone.think.utils import get_journal

SPP_NVATTEST_DIR_ENV = "SPP_NVATTEST_DIR"
SIDECAR_NAME = ".nvattest-install.json"
SIDECAR_SCHEMA_VERSION = 1
CA_BUNDLE_RELATIVE_PATH = Path("share") / "ca" / "ca-bundle.pem"
ENSURE_LOCK_TIMEOUT_S = 0.1
ENSURE_LOCK_POLL_INTERVAL_S = 0.02
DOWNLOADS_DIR_NAME = ".downloads"
EXTRACT_DIR_NAME = ".extract"
INSTALL_LOCK_SIDECAR_NAME = ".install.lock"
HOUSEKEEPING_NAMES = frozenset(
    {DOWNLOADS_DIR_NAME, EXTRACT_DIR_NAME, INSTALL_LOCK_SIDECAR_NAME}
)
PAYLOAD_TOP_LEVEL = ("bin", "lib", "share", "LICENSE")
EXECUTABLE_MASK = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

NvattestEnsureStatus = Literal[
    "already_installed",
    "installed",
    "install_in_flight",
    "install_failed",
    "platform_unsupported",
]


class NvattestInstallError(RuntimeError):
    """nvattest artifact acquisition failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class NvattestEnsureResult:
    status: NvattestEnsureStatus
    nvattest_dir: Path | None = None
    reason_code: str | None = None
    detail: str | None = None


def cache_root(journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "cache" / "providers" / "nvattest"


def resolve_nvattest_dir(
    explicit_override: str | Path | None = None,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    """Resolve the nvattest directory from override, env, then journal cache."""

    if explicit_override is not None:
        return Path(explicit_override).expanduser()
    env_path = os.environ.get(SPP_NVATTEST_DIR_ENV)
    if env_path:
        return Path(env_path).expanduser()
    return cache_root(journal_path)


def ensure_nvattest_installed(
    *,
    explicit_override: str | Path | None = None,
    journal_path: str | Path | None = None,
    entry: NvattestTargetEntry | None = None,
    lock_timeout: float = ENSURE_LOCK_TIMEOUT_S,
) -> NvattestEnsureResult:
    """Ensure the journal-cache nvattest install is ready without blocking peers."""

    nvattest_dir = resolve_nvattest_dir(
        explicit_override,
        journal_path=journal_path,
    )
    if explicit_override is not None or os.environ.get(SPP_NVATTEST_DIR_ENV):
        # Override layout validation stays in nvgpu.binary so appraiser reasons
        # still traverse binary -> composite -> ratls instead of install plumbing.
        return NvattestEnsureResult(
            status="already_installed",
            nvattest_dir=nvattest_dir,
        )

    try:
        resolved_entry = entry or resolve_nvattest_authority_entry()
    except NvattestInstallError as exc:
        return NvattestEnsureResult(
            status="platform_unsupported",
            reason_code=exc.reason_code,
            detail=str(exc),
        )

    try:
        with hold_lock(
            _install_lock_path(journal_path),
            timeout=lock_timeout,
            poll_interval=ENSURE_LOCK_POLL_INTERVAL_S,
        ):
            if _installed(nvattest_dir, resolved_entry):
                return NvattestEnsureResult(
                    status="already_installed",
                    nvattest_dir=nvattest_dir,
                )
            try:
                installed = install_nvattest(
                    entry=resolved_entry,
                    journal_path=journal_path,
                )
            except NvattestInstallError as exc:
                return NvattestEnsureResult(
                    status="install_failed",
                    nvattest_dir=nvattest_dir,
                    reason_code=exc.reason_code,
                    detail=str(exc),
                )
            return NvattestEnsureResult(status="installed", nvattest_dir=installed)
    except LockTimeout:
        return NvattestEnsureResult(
            status="install_in_flight",
            nvattest_dir=nvattest_dir,
            reason_code="install-in-progress",
        )


def nvattest_cache_ready(
    *,
    explicit_override: str | Path | None = None,
    journal_path: str | Path | None = None,
    entry: NvattestTargetEntry | None = None,
) -> bool:
    """Return whether the cache install is quiescent and ready for a reader."""

    if explicit_override is not None or os.environ.get(SPP_NVATTEST_DIR_ENV):
        return True
    root = cache_root(journal_path)
    if not root.exists() or _install_lock_is_held(journal_path):
        return False
    try:
        resolved_entry = entry or resolve_nvattest_authority_entry()
    except NvattestInstallError:
        return False
    return _installed(root, resolved_entry)


def install_nvattest(
    *,
    force: bool = False,
    entry: NvattestTargetEntry | None = None,
    journal_path: str | Path | None = None,
) -> Path:
    """Download, verify, and install nvattest into the journal provider cache."""

    entry = entry or resolve_nvattest_authority_entry()
    root = cache_root(journal_path)
    if not force and _installed(root, entry):
        return root

    archive = _archive_path(entry, journal_path)
    extract_dir = root / EXTRACT_DIR_NAME
    raw_dir = extract_dir / "raw"
    payload_dir = extract_dir / "payload"
    try:
        _download_file(
            entry.artifact.url,
            archive,
            entry.artifact.size_bytes,
            entry.artifact.sha256,
        )
        shutil.rmtree(extract_dir, ignore_errors=True)
        _safe_extract_nvattest_tarball(archive, raw_dir)
        source = _find_extracted_root(raw_dir, entry)
        _materialize_payload_tree(source, payload_dir, entry)
        fingerprint = _tree_fingerprint_sha256(payload_dir, entry)
        _promote_payload_tree(
            payload_dir,
            root,
            _sidecar_json(entry, fingerprint),
        )
        return root
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        archive.unlink(missing_ok=True)


def resolve_nvattest_authority_entry() -> NvattestTargetEntry:
    target_key = nvattest_target_key()
    if target_key is None:
        raise NvattestInstallError(
            "platform_unsupported",
            "nvattest archive unsupported on this platform",
        )
    return authority_entry(target_key)


def _installed(root: Path, entry: NvattestTargetEntry) -> bool:
    try:
        data = json.loads((root / SIDECAR_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not _sidecar_matches(data, entry):
        return False
    try:
        return _tree_fingerprint_sha256(root, entry) == data["tree_fingerprint_sha256"]
    except (NvattestInstallError, OSError):
        return False


def _sidecar_matches(data: object, entry: NvattestTargetEntry) -> bool:
    if not isinstance(data, dict):
        return False
    if set(data) != {
        "artifact",
        "schema_version",
        "target_key",
        "tree_fingerprint_sha256",
        "version",
    }:
        return False
    return (
        data.get("schema_version") == SIDECAR_SCHEMA_VERSION
        and data.get("target_key") == entry.key
        and data.get("version") == entry.source.version
        and data.get("artifact") == entry.artifact.to_payload()
        and _is_sha256(data.get("tree_fingerprint_sha256"))
    )


def _archive_path(
    entry: NvattestTargetEntry,
    journal_path: str | Path | None = None,
) -> Path:
    return cache_root(journal_path) / DOWNLOADS_DIR_NAME / entry.artifact.name


def _install_lock_path(journal_path: str | Path | None = None) -> Path:
    return cache_root(journal_path) / ".install"


def _install_lock_is_held(journal_path: str | Path | None = None) -> bool:
    lock_path = _install_lock_path(journal_path).parent / INSTALL_LOCK_SIDECAR_NAME
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            return False
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(
    path: Path,
    expected_size_bytes: int,
    expected_sha256: str,
) -> None:
    if not path.is_file():
        raise NvattestInstallError("file_missing", f"nvattest asset missing: {path}")
    actual_size = path.stat().st_size
    if actual_size != expected_size_bytes:
        raise NvattestInstallError(
            "archive_size_mismatch",
            (
                f"size mismatch for {path.name}: expected {expected_size_bytes}, "
                f"got {actual_size}"
            ),
        )
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise NvattestInstallError(
            "sha256_mismatch",
            (
                f"sha256 mismatch for {path.name}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            ),
        )


def _tmp_path(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.tmp")


def _download_file(
    url: str,
    dest: Path,
    expected_size_bytes: int,
    expected_sha256: str,
) -> None:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(dest)
    dest.unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)
    try:
        with httpx.stream("GET", url, timeout=600.0, follow_redirects=True) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
        _verify_file(tmp, expected_size_bytes, expected_sha256)
        tmp.replace(dest)
    except NvattestInstallError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise NvattestInstallError(
            "download_failed",
            f"failed to download nvattest archive: {exc}",
        ) from exc


def _safe_extract_nvattest_tarball(tarball: Path, dest: Path) -> None:
    try:
        _rfdetr_safe_extract_tarball(tarball, dest)
    except RfdetrInstallError as exc:
        reason_code = getattr(exc, "reason_code", "archive_extract_failed")
        if reason_code == "archive_path_traversal":
            raise NvattestInstallError(reason_code, str(exc)) from exc
        raise NvattestInstallError("archive_extract_failed", str(exc)) from exc


def _find_extracted_root(extract_dir: Path, entry: NvattestTargetEntry) -> Path:
    candidates = [extract_dir]
    candidates.extend(
        path.parent.parent
        for path in extract_dir.rglob("nvattest")
        if path.is_file() and path.parent.name == "bin"
    )
    valid: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            _payload_member_facts(candidate, entry)
        except NvattestInstallError:
            continue
        valid.append(candidate)
    if len(valid) != 1:
        raise NvattestInstallError(
            "archive_layout_invalid",
            f"expected exactly one extracted nvattest payload, found {len(valid)}",
        )
    return valid[0]


def _materialize_payload_tree(
    source: Path,
    payload_dir: Path,
    entry: NvattestTargetEntry,
) -> None:
    shutil.rmtree(payload_dir, ignore_errors=True)
    payload_dir.mkdir(parents=True, exist_ok=True)
    for name in PAYLOAD_TOP_LEVEL:
        src = source / name
        dst = payload_dir / name
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            raise NvattestInstallError(
                "archive_layout_invalid",
                f"extracted archive missing payload member: {name}",
            )
    _payload_member_facts(payload_dir, entry)


def _tree_fingerprint_sha256(root: Path, entry: NvattestTargetEntry) -> str:
    fingerprint = {"members": _payload_member_facts(root, entry)}
    return fingerprint_sha256(canonical_fingerprint(fingerprint))


def _payload_member_facts(
    root: Path,
    entry: NvattestTargetEntry,
) -> list[dict[str, Any]]:
    expected = {member.relpath: member for member in entry.inventory}
    expected_dirs = _expected_payload_dirs(entry)
    observed_dirs: set[str] = set()
    observed: dict[str, dict[str, Any]] = {}
    for top_level in ("bin", "lib", "share"):
        _scan_payload_path(root / top_level, root, observed, observed_dirs)
    _scan_payload_path(root / "LICENSE", root, observed, observed_dirs)

    extra_dirs = observed_dirs - expected_dirs
    if extra_dirs:
        raise NvattestInstallError(
            "archive_layout_invalid",
            f"nvattest payload contains extra directories: {sorted(extra_dirs)}",
        )
    if set(observed) != set(expected):
        raise NvattestInstallError(
            "archive_layout_invalid",
            (
                "nvattest payload member set mismatch: "
                f"missing={sorted(set(expected) - set(observed))} "
                f"extra={sorted(set(observed) - set(expected))}"
            ),
        )
    for relpath, expected_member in expected.items():
        fact = observed[relpath]
        if fact["kind"] != expected_member.kind:
            raise NvattestInstallError(
                "archive_layout_invalid",
                f"nvattest payload wrong kind for {relpath}",
            )
        if fact["symlink_target"] != expected_member.symlink_target:
            raise NvattestInstallError(
                "archive_layout_invalid",
                f"nvattest payload wrong symlink target for {relpath}",
            )
        if fact["executable"] != expected_member.executable:
            raise NvattestInstallError(
                "archive_layout_invalid",
                f"nvattest payload wrong executable bit for {relpath}",
            )
    return [observed[relpath] for relpath in sorted(observed)]


def _expected_payload_dirs(entry: NvattestTargetEntry) -> set[str]:
    expected: set[str] = set()
    for member in entry.inventory:
        parts = Path(member.relpath).parts[:-1]
        for index in range(1, len(parts) + 1):
            expected.add(Path(*parts[:index]).as_posix())
    return expected


def _scan_payload_path(
    path: Path,
    root: Path,
    observed: dict[str, dict[str, Any]],
    observed_dirs: set[str],
) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    relpath = path.relative_to(root).as_posix()
    if stat.S_ISLNK(mode):
        observed[relpath] = {
            "content_sha256": None,
            "executable": False,
            "kind": "symlink",
            "relpath": relpath,
            "symlink_target": os.readlink(path),
        }
        return
    if stat.S_ISDIR(mode):
        observed_dirs.add(relpath)
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _scan_payload_path(child, root, observed, observed_dirs)
        return
    if stat.S_ISREG(mode):
        observed[relpath] = {
            "content_sha256": _sha256_file(path),
            "executable": bool(mode & EXECUTABLE_MASK),
            "kind": "regular",
            "relpath": relpath,
            "symlink_target": None,
        }
        return
    observed[relpath] = {
        "content_sha256": None,
        "executable": False,
        "kind": "other",
        "relpath": relpath,
        "symlink_target": None,
    }


def _promote_payload_tree(payload_dir: Path, root: Path, sidecar_json: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    aside = root / EXTRACT_DIR_NAME / "aside"
    shutil.rmtree(aside, ignore_errors=True)
    aside.mkdir(parents=True, exist_ok=True)
    moved_old: list[str] = []
    installed_new: list[str] = []
    committed = False
    try:
        for name in PAYLOAD_TOP_LEVEL:
            target = root / name
            if target.exists() or target.is_symlink():
                shutil.move(str(target), str(aside / name))
                moved_old.append(name)
        for name in PAYLOAD_TOP_LEVEL:
            shutil.move(str(payload_dir / name), str(root / name))
            installed_new.append(name)
        sidecar_tmp = root / EXTRACT_DIR_NAME / "sidecar.tmp"
        sidecar_tmp.write_text(sidecar_json, encoding="utf-8")
        sidecar_tmp.replace(root / SIDECAR_NAME)
        committed = True
    except Exception:
        if not committed:
            for name in reversed(installed_new):
                _remove_payload_path(root / name)
            for name in reversed(moved_old):
                shutil.move(str(aside / name), str(root / name))
        raise
    finally:
        if committed:
            shutil.rmtree(aside, ignore_errors=True)


def _remove_payload_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _sidecar_json(entry: NvattestTargetEntry, tree_fingerprint_sha256: str) -> str:
    return (
        json.dumps(
            {
                "artifact": entry.artifact.to_payload(),
                "schema_version": SIDECAR_SCHEMA_VERSION,
                "target_key": entry.key,
                "tree_fingerprint_sha256": tree_fingerprint_sha256,
                "version": entry.source.version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


__all__ = [
    "HOUSEKEEPING_NAMES",
    "NvattestEnsureResult",
    "NvattestEnsureStatus",
    "NvattestInstallError",
    "SIDECAR_NAME",
    "SPP_NVATTEST_DIR_ENV",
    "cache_root",
    "ensure_nvattest_installed",
    "install_nvattest",
    "nvattest_cache_ready",
    "resolve_nvattest_authority_entry",
    "resolve_nvattest_dir",
]
