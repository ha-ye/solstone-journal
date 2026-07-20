# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Export the verified observer-client contract bundle."""

from __future__ import annotations

import ctypes
import errno
import os
import re
import stat
import uuid
from pathlib import Path

from solstone.convey.contract.observer_bundle import (
    _WINDOWS_RESERVED_BASENAMES,
    BundleExportRefused,
    BundleSnapshot,
    BundleVerificationError,
    ObserverBundleError,
    _directory_entry_snapshot,
    _entry_snapshot_identity,
    _open_dir_at_no_follow,
    _open_parent_dir_no_follow,
    _repo_root,
    _stat_identity,
    stale_bundle_paths,
)
from solstone.convey.contract.observer_bundle_verification import (
    verify_bundle_directory,
    verify_bundle_fd,
    verify_committed_bundle,
)

_RENAME_NOREPLACE = 1


def export_bundle(destination: Path, root: Path | None = None) -> Path:
    """Export the committed observer-client bundle to a new destination."""

    repo_root = _repo_root(root)
    stale = stale_bundle_paths(repo_root)
    if stale:
        raise _export_refused(
            "committed bundle is stale: " + ", ".join(str(path) for path in stale),
            "run make openapi before exporting",
        )
    try:
        source_snapshot = verify_committed_bundle(repo_root)
    except ObserverBundleError as exc:
        raise _export_refused(
            str(exc),
            "repair the bundle manifest and rerun make openapi",
        ) from exc
    return _publish_verified_bundle(
        Path(destination),
        repo_root,
        source_snapshot,
    )


def publish_bundle_directory(
    source_dir: Path,
    destination: Path,
    root: Path | None = None,
) -> Path:
    """Publish a verified bundle directory to a new destination."""

    repo_root = _repo_root(root)
    source_snapshot = verify_bundle_directory(source_dir)
    return _publish_verified_bundle(
        Path(destination),
        repo_root,
        source_snapshot,
    )


def _publish_verified_bundle(
    destination: Path,
    repo_root: Path,
    source_snapshot: BundleSnapshot,
) -> Path:
    target = _resolve_export_destination(repo_root, Path(destination))
    parent_fd, destination_name = _open_destination_parent(target)
    stage_name, stage_fd, stage_stat = _create_stage_dir(target, parent_fd)
    finalized = False
    try:
        _populate_bundle_stage(stage_fd, source_snapshot)
        staged_snapshot = _verify_bundle_stage(stage_fd)
        if staged_snapshot.files != source_snapshot.files:
            raise _export_refused(
                "staged bundle bytes do not match the committed bundle",
                "repair the bundle manifest and rerun make openapi",
            )
        _finalize_bundle_publish(parent_fd, stage_name, destination_name, target)
        finalized = True
    except BundleVerificationError as exc:
        _cleanup_stage(parent_fd, stage_name, stage_stat)
        raise _export_refused(
            str(exc),
            "repair the bundle manifest and rerun make openapi",
        ) from exc
    except Exception:
        if not finalized:
            _cleanup_stage(parent_fd, stage_name, stage_stat)
        raise
    finally:
        os.close(stage_fd)
        os.close(parent_fd)
    return target


def _populate_bundle_stage(stage_fd: int, snapshot: BundleSnapshot) -> None:
    """Populate a fresh stage directory from verified source bundle files."""

    for rel_path in sorted(snapshot.files):
        _write_stage_file(stage_fd, rel_path, snapshot.files[rel_path])


def _verify_bundle_stage(stage_fd: int) -> BundleSnapshot:
    """Verify a populated staging directory before final publish."""

    return verify_bundle_fd(stage_fd, "export-stage")


def _open_destination_parent(destination: Path) -> tuple[int, bytes]:
    try:
        parent_fd, destination_name = _open_parent_dir_no_follow(destination)
    except ObserverBundleError as exc:
        raise _export_refused(
            str(exc),
            "choose a destination whose parent components are normal directories",
        ) from exc
    try:
        os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return parent_fd, destination_name
    except OSError:
        os.close(parent_fd)
        raise
    os.close(parent_fd)
    raise _export_refused(
        f"destination already exists: {destination}",
        "choose a new empty destination path",
    )


def _create_stage_dir(
    target: Path, parent_fd: int
) -> tuple[bytes, int, os.stat_result]:
    for _attempt in range(100):
        name = os.fsencode(f".{target.name}.staging.{uuid.uuid4().hex}")
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise _export_refused(
                f"staging directory cannot be created beside destination: {target}",
                "choose a writable local Linux destination parent",
            ) from exc
        stage_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        stage_fd = _open_dir_at_no_follow(parent_fd, name, stage_stat)
        return name, stage_fd, stage_stat
    raise _export_refused(
        f"staging directory name collision beside destination: {target}",
        "retry export with the same destination",
    )


def _finalize_bundle_publish(
    parent_fd: int,
    stage_name: bytes,
    destination_name: bytes,
    destination: Path,
) -> None:
    """Publish a verified stage on Linux local filesystems without replacement."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise _export_refused(
            "Linux renameat2(RENAME_NOREPLACE) is unavailable",
            "export on a Linux local filesystem with renameat2 support",
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        stage_name,
        parent_fd,
        destination_name,
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise _export_refused(
            f"destination appeared before final rename: {destination}",
            "choose a new empty destination path and rerun export",
        )
    if err in {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        errno.EXDEV,
    }:
        raise _export_refused(
            "filesystem does not support Linux renameat2(RENAME_NOREPLACE) "
            f"for local sibling publish: {destination.parent}",
            "export on a supported local Linux filesystem; NFS and remote "
            "filesystems are outside this atomic no-replace guarantee",
        )
    raise OSError(err, os.strerror(err), str(destination))


def _resolve_export_destination(repo_root: Path, destination: Path) -> Path:
    target = destination if destination.is_absolute() else repo_root / destination
    _validate_export_destination_text(target)
    return target


def _validate_export_destination_text(target: Path) -> None:
    raw = str(target)
    if "\\" in raw:
        raise _export_refused(
            f"destination contains a backslash: {target}",
            "choose a normal POSIX destination path",
        )
    if re.search(r"(^|/)[A-Za-z]:", raw):
        raise _export_refused(
            f"destination contains a Windows drive prefix: {target}",
            "choose a destination without drive-letter syntax",
        )
    parts = [part for part in target.parts if part not in {target.anchor, ""}]
    if not parts:
        raise _export_refused(
            f"destination has no path component: {target}",
            "choose a named destination directory",
        )
    for part in parts:
        _validate_export_destination_component(part, target)


def _validate_export_destination_component(component: str, target: Path) -> None:
    if component in {".", ".."}:
        raise _export_refused(
            f"destination has unsafe component: {target}",
            "choose a destination without . or .. components",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in component):
        raise _export_refused(
            f"destination contains a control character: {target!r}",
            "choose a printable destination path",
        )
    if ":" in component:
        raise _export_refused(
            f"destination contains a colon: {target}",
            "choose a destination without Windows-unsafe characters",
        )
    if any(char in component for char in "*?[]"):
        raise _export_refused(
            f"destination contains wildcard characters: {target}",
            "choose a literal destination path",
        )
    if component.endswith("."):
        raise _export_refused(
            f"destination component has trailing dot: {target}",
            "choose a Windows-safe destination component",
        )
    if component.endswith(" "):
        raise _export_refused(
            f"destination component has trailing space: {target}",
            "choose a Windows-safe destination component",
        )
    basename = component.split(".", 1)[0].upper()
    if basename in _WINDOWS_RESERVED_BASENAMES:
        raise _export_refused(
            f"destination uses Windows-reserved device name: {target}",
            "choose a Windows-safe destination component",
        )


def _write_stage_file(stage_fd: int, rel_path: str, payload: bytes) -> None:
    parts = [os.fsencode(part) for part in rel_path.split("/")]
    parent_fd = os.dup(stage_fd)
    try:
        for directory in parts[:-1]:
            child_fd = _ensure_stage_directory(parent_fd, directory)
            os.close(parent_fd)
            parent_fd = child_fd
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(parts[-1], flags, 0o600, dir_fd=parent_fd)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(file_fd, view)
                view = view[written:]
        finally:
            os.close(file_fd)
    finally:
        os.close(parent_fd)


def _ensure_stage_directory(parent_fd: int, name: bytes) -> int:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(entry_stat.st_mode):
        raise _export_refused(
            f"staging path component is not a directory: {os.fsdecode(name)}",
            "retry export with a fresh destination",
        )
    return _open_dir_at_no_follow(parent_fd, name, entry_stat)


def _cleanup_stage(
    parent_fd: int, stage_name: bytes, expected_stat: os.stat_result
) -> None:
    try:
        current_stat = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _node_identity(current_stat) != _node_identity(expected_stat):
        return
    stage_fd = _open_dir_at_no_follow(parent_fd, stage_name, current_stat)
    try:
        _remove_stage_contents(stage_fd)
    finally:
        os.close(stage_fd)
    try:
        current_stat = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _node_identity(current_stat) != _node_identity(expected_stat):
        return
    os.rmdir(stage_name, dir_fd=parent_fd)


def _remove_stage_contents(dir_fd: int) -> None:
    before_entries = _directory_entry_snapshot(dir_fd)
    for name, entry_stat in sorted(before_entries, key=lambda item: item[0]):
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_dir_at_no_follow(dir_fd, name, entry_stat)
            try:
                _remove_stage_contents(child_fd)
            finally:
                os.close(child_fd)
            current_stat = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if _node_identity(current_stat) == _node_identity(entry_stat):
                os.rmdir(name, dir_fd=dir_fd)
            continue
        if stat.S_ISREG(entry_stat.st_mode):
            current_stat = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if _stat_identity(current_stat) == _stat_identity(entry_stat):
                os.unlink(name, dir_fd=dir_fd)
            continue
        raise _export_refused(
            f"staging path is not a regular file or directory: {os.fsdecode(name)}",
            "retry export with a fresh destination",
        )
    after_entries = _directory_entry_snapshot(dir_fd)
    if _entry_snapshot_identity(after_entries) != ():
        raise _export_refused(
            "staging directory changed during cleanup",
            "inspect and remove the staging directory manually",
        )


def _node_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode)


def _export_refused(reason: str, recovery: str) -> BundleExportRefused:
    return BundleExportRefused(
        f"observer client contract export refused: {reason}. Recovery: {recovery}."
    )
