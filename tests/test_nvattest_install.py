# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from solstone.think.journal_io import LockTimeout
from solstone.think.providers import nvattest_install
from solstone.think.providers.nvattest_authority import (
    TARGET_KEYS,
    NvattestArtifactIdentity,
    NvattestTargetEntry,
    NvattestTargetKey,
    authority_entry,
)

PAYLOAD_TOP_LEVEL = ("bin", "lib", "share", "LICENSE")
SIDECAR_KEYS = {
    "artifact",
    "schema_version",
    "target_key",
    "tree_fingerprint_sha256",
    "version",
}


@dataclass(frozen=True, slots=True)
class FixtureArchive:
    archive_path: Path
    entry: NvattestTargetEntry


class DownloadSentinel(RuntimeError):
    pass


@pytest.mark.parametrize("target_key", TARGET_KEYS)
def test_install_requests_exact_authority_artifact_identity_for_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_key: NvattestTargetKey,
) -> None:
    entry = authority_entry(target_key)
    calls: list[tuple[str, int, str]] = []

    def capture_download(
        url: str,
        _dest: Path,
        expected_size_bytes: int,
        expected_sha256: str,
    ) -> None:
        calls.append((url, expected_size_bytes, expected_sha256))
        raise DownloadSentinel

    monkeypatch.setattr(nvattest_install, "_download_file", capture_download)

    with pytest.raises(DownloadSentinel):
        nvattest_install.install_nvattest(entry=entry, journal_path=tmp_path)

    assert calls == [
        (entry.artifact.url, entry.artifact.size_bytes, entry.artifact.sha256)
    ]


@pytest.mark.parametrize("target_key", TARGET_KEYS)
def test_install_materializes_authority_layout_and_sidecar_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_key: NvattestTargetKey,
) -> None:
    fixture = _fixture_archive(tmp_path, target_key=target_key)

    root, calls = _install_from_fixture(tmp_path, monkeypatch, fixture)

    assert root == nvattest_install.cache_root(tmp_path)
    assert len(calls) == 1
    _assert_payload_layout(root, fixture.entry)
    _assert_sidecar_binding(root, fixture.entry)
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )


def test_install_accepts_single_wrapped_authority_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture_archive(tmp_path, target_key="linux-x86_64", wrapped=True)

    root, _calls = _install_from_fixture(tmp_path, monkeypatch, fixture)

    _assert_payload_layout(root, fixture.entry)
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )


def test_install_is_download_free_noop_only_when_sidecar_and_fingerprint_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture_archive(tmp_path, target_key="linux-x86_64")
    root, calls = _install_from_fixture(tmp_path, monkeypatch, fixture)
    assert len(calls) == 1

    monkeypatch.setattr(
        nvattest_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not run"),
    )

    assert (
        nvattest_install.install_nvattest(
            entry=fixture.entry,
            journal_path=tmp_path,
        )
        == root
    )


@pytest.mark.parametrize(
    ("case_name", "mutate"),
    [
        ("bin content", lambda root, entry: _append(root / "bin" / "nvattest")),
        ("lib content", lambda root, entry: _append(root / _runtime_lib(entry))),
        (
            "bundled ca",
            lambda root, entry: _append(root / "share" / "ca" / "ca-bundle.pem"),
        ),
        ("license", lambda root, entry: _append(root / "LICENSE")),
        (
            "third party notices",
            lambda root, entry: _append(root / "share" / "THIRD_PARTY_NOTICES.md"),
        ),
        ("file mode", lambda root, entry: (root / "bin" / "nvattest").chmod(0o644)),
        (
            "symlink target",
            lambda root, entry: _replace_symlink(
                root / _first_symlink(entry),
                "wrong-target",
            ),
        ),
        ("missing member", lambda root, entry: (root / "LICENSE").unlink()),
        (
            "extra owned member",
            lambda root, entry: (root / "share" / "extra.txt").write_text(
                "extra\n",
                encoding="utf-8",
            ),
        ),
        (
            "regular where symlink expected",
            lambda root, entry: _replace_with_file(root / _first_symlink(entry)),
        ),
        (
            "symlink where regular expected",
            lambda root, entry: _replace_with_symlink(root / _runtime_lib(entry)),
        ),
    ],
)
def test_readiness_rejects_payload_mutation_and_retry_reinstalls_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    mutate: Callable[[Path, NvattestTargetEntry], None],
) -> None:
    del case_name
    fixture = _fixture_archive(tmp_path, target_key="linux-x86_64")
    root, _calls = _install_from_fixture(tmp_path, monkeypatch, fixture)

    mutate(root, fixture.entry)

    assert not nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )
    root, calls = _install_from_fixture(tmp_path, monkeypatch, fixture)
    assert len(calls) == 1
    _assert_payload_layout(root, fixture.entry)
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )


def test_readiness_rejects_sidecar_target_mismatch_and_retry_reinstalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture_archive(tmp_path, target_key="linux-x86_64")
    root, _calls = _install_from_fixture(tmp_path, monkeypatch, fixture)
    sidecar_path = root / nvattest_install.SIDECAR_NAME
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["target_key"] = "linux-aarch64"
    sidecar_path.write_text(
        json.dumps(sidecar, sort_keys=True) + "\n", encoding="utf-8"
    )

    assert not nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )
    _install_from_fixture(tmp_path, monkeypatch, fixture)
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )


def test_readiness_rejects_old_sidecar_shape_and_retry_reinstalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture_archive(tmp_path, target_key="linux-x86_64")
    root, _calls = _install_from_fixture(tmp_path, monkeypatch, fixture)
    (root / nvattest_install.SIDECAR_NAME).write_text(
        json.dumps(
            {
                "archive_sha256": fixture.entry.artifact.sha256,
                "version": fixture.entry.source.version,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    assert not nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )
    _install_from_fixture(tmp_path, monkeypatch, fixture)
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )


def test_housekeeping_changes_do_not_affect_fingerprint_and_foreign_paths_survive_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture_archive(tmp_path, target_key="linux-x86_64")
    root, _calls = _install_from_fixture(tmp_path, monkeypatch, fixture)
    baseline_fingerprint = nvattest_install._tree_fingerprint_sha256(
        root, fixture.entry
    )

    (root / ".downloads").mkdir(exist_ok=True)
    (root / ".downloads" / "note").write_text("housekeeping\n", encoding="utf-8")
    (root / ".extract").mkdir(exist_ok=True)
    (root / ".extract" / "note").write_text("housekeeping\n", encoding="utf-8")
    (root / ".install.lock").write_text("housekeeping\n", encoding="utf-8")
    foreign = root / "operator-note.txt"
    foreign.write_text("keep me\n", encoding="utf-8")

    assert nvattest_install._tree_fingerprint_sha256(root, fixture.entry) == (
        baseline_fingerprint
    )
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=fixture.entry,
    )

    replacement = _fixture_archive(
        tmp_path,
        target_key="linux-x86_64",
        label="replacement",
    )
    _install_from_fixture(tmp_path, monkeypatch, replacement, force=True)

    assert foreign.read_text(encoding="utf-8") == "keep me\n"
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=replacement.entry,
    )


def test_readiness_is_pure_read_on_not_ready_cache(tmp_path: Path) -> None:
    root = nvattest_install.cache_root(tmp_path)
    root.mkdir(parents=True)
    before = _snapshot_tree(root)

    assert not nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=_fixture_entry(tmp_path, authority_entry("linux-x86_64")),
    )

    assert _snapshot_tree(root) == before
    assert not (root / ".install.lock").exists()


@pytest.mark.parametrize(
    ("failure_point", "install_attempt"),
    [
        ("before download", "before_download"),
        ("after download", "after_download"),
        ("after verification", "after_verification"),
        ("after extraction", "after_extraction"),
        ("after layout validation", "after_layout_validation"),
        ("after fingerprinting", "after_fingerprinting"),
        ("during final promotion", "during_final_promotion"),
    ],
)
def test_failed_install_never_accepts_mixed_tree_and_preserves_prior_ready_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    install_attempt: str,
) -> None:
    del failure_point
    ready = _fixture_archive(tmp_path, target_key="linux-x86_64", label="ready")
    root, _calls = _install_from_fixture(tmp_path, monkeypatch, ready)
    before = _snapshot_install(root)
    replacement = _fixture_archive(
        tmp_path,
        target_key="linux-x86_64",
        label="replacement",
    )
    _inject_failure(monkeypatch, install_attempt, replacement)

    with pytest.raises(
        (NvattestInstallErrorProxy, nvattest_install.NvattestInstallError)
    ):
        nvattest_install.install_nvattest(
            force=True,
            entry=replacement.entry,
            journal_path=tmp_path,
        )

    assert _snapshot_install(root) == before
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=ready.entry,
    )
    assert not nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=replacement.entry,
    )


@pytest.mark.parametrize(
    ("case_name", "archive_factory", "reason_code"),
    [
        (
            "wrong length",
            lambda tmp_path, entry: _raw_download_fixture(
                tmp_path,
                entry,
                data=b"not an archive",
                expected_size_delta=1,
            ),
            "archive_size_mismatch",
        ),
        (
            "wrong sha256",
            lambda tmp_path, entry: _raw_download_fixture(
                tmp_path,
                entry,
                data=b"not an archive",
                expected_sha256="0" * 64,
            ),
            "sha256_mismatch",
        ),
        (
            "path traversal",
            lambda tmp_path, entry: _path_traversal_archive(tmp_path, entry),
            "archive_path_traversal",
        ),
        (
            "invalid wrapped layout",
            lambda tmp_path, entry: _duplicate_wrapped_archive(tmp_path, entry),
            "archive_layout_invalid",
        ),
        (
            "invalid flat layout",
            lambda tmp_path, entry: _invalid_flat_archive(tmp_path, entry),
            "archive_layout_invalid",
        ),
        (
            "missing bundled ca",
            lambda tmp_path, entry: _fixture_archive(
                tmp_path,
                target_key=entry.key,
                label="missing-ca",
                omitted={"share/ca/ca-bundle.pem"},
            ),
            "archive_layout_invalid",
        ),
        (
            "missing executable",
            lambda tmp_path, entry: _fixture_archive(
                tmp_path,
                target_key=entry.key,
                label="missing-exec",
                executable_overrides={"bin/nvattest": False},
            ),
            "archive_layout_invalid",
        ),
    ],
)
def test_typed_failures_clean_partial_state_and_leave_prior_ready_install_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    archive_factory: Callable[[Path, NvattestTargetEntry], FixtureArchive],
    reason_code: str,
) -> None:
    del case_name
    ready = _fixture_archive(tmp_path, target_key="linux-x86_64", label="ready")
    root, _calls = _install_from_fixture(tmp_path, monkeypatch, ready)
    before = _snapshot_install(root)
    failing = archive_factory(tmp_path, ready.entry)
    _install_download_from_fixture(monkeypatch, failing)

    with pytest.raises(nvattest_install.NvattestInstallError) as exc_info:
        nvattest_install.install_nvattest(
            force=True,
            entry=failing.entry,
            journal_path=tmp_path,
        )

    assert exc_info.value.reason_code == reason_code
    assert _snapshot_install(root) == before
    assert not (root / ".extract").exists()
    assert not (root / ".downloads" / failing.entry.artifact.name).exists()
    assert not (root / ".downloads" / f"{failing.entry.artifact.name}.tmp").exists()
    assert nvattest_install.nvattest_cache_ready(
        journal_path=tmp_path,
        entry=ready.entry,
    )


def test_ensure_nvattest_unsupported_platform_does_not_touch_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nvattest_install, "nvattest_target_key", lambda: None)

    result = nvattest_install.ensure_nvattest_installed(journal_path=tmp_path)

    assert result.status == "platform_unsupported"
    assert result.reason_code == "platform_unsupported"
    assert not nvattest_install.cache_root(tmp_path).exists()


def test_ensure_nvattest_lock_timeout_is_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def fake_hold_lock(path: Path, *, timeout: float, **_kwargs) -> Iterator[None]:
        raise LockTimeout(path, timeout)
        yield

    monkeypatch.setattr(nvattest_install, "hold_lock", fake_hold_lock)

    result = nvattest_install.ensure_nvattest_installed(journal_path=tmp_path)

    assert result.status == "install_in_flight"
    assert result.reason_code == "install-in-progress"


def test_ensure_nvattest_override_skips_cache_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "override"
    monkeypatch.setenv(nvattest_install.SPP_NVATTEST_DIR_ENV, str(override))
    monkeypatch.setattr(
        nvattest_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not run"),
    )

    result = nvattest_install.ensure_nvattest_installed(journal_path=tmp_path)

    assert result.status == "already_installed"
    assert result.nvattest_dir == override
    assert not nvattest_install.cache_root(tmp_path).exists()


class NvattestInstallErrorProxy(RuntimeError):
    pass


def _fixture_archive(
    tmp_path: Path,
    *,
    target_key: NvattestTargetKey,
    label: str = "fixture",
    wrapped: bool = False,
    omitted: set[str] | None = None,
    executable_overrides: dict[str, bool] | None = None,
) -> FixtureArchive:
    entry = authority_entry(target_key)
    archive_path = tmp_path / f"{label}-{target_key}.tar.xz"
    roots = (label,) if wrapped else ("",)
    _write_payload_tarball(
        archive_path,
        entry,
        roots=roots,
        label=label,
        omitted=omitted or set(),
        executable_overrides=executable_overrides or {},
    )
    return FixtureArchive(
        archive_path=archive_path,
        entry=_fixture_entry(tmp_path, entry, archive_path=archive_path),
    )


def _fixture_entry(
    tmp_path: Path,
    entry: NvattestTargetEntry,
    *,
    archive_path: Path | None = None,
) -> NvattestTargetEntry:
    if archive_path is None:
        archive_path = tmp_path / f"empty-{entry.key}.tar.xz"
        with tarfile.open(archive_path, "w:xz"):
            pass
    archive_bytes = archive_path.read_bytes()
    artifact = replace(
        entry.artifact,
        name=archive_path.name,
        url=f"https://example.invalid/{archive_path.name}",
        size_bytes=len(archive_bytes),
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
    )
    return replace(entry, artifact=artifact)


def _install_from_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: FixtureArchive,
    *,
    force: bool = False,
) -> tuple[Path, list[tuple[str, Path, int, str]]]:
    calls = _install_download_from_fixture(monkeypatch, fixture)
    installed = nvattest_install.install_nvattest(
        force=force,
        entry=fixture.entry,
        journal_path=tmp_path,
    )
    return installed, calls


def _install_download_from_fixture(
    monkeypatch: pytest.MonkeyPatch,
    fixture: FixtureArchive,
) -> list[tuple[str, Path, int, str]]:
    calls: list[tuple[str, Path, int, str]] = []

    def fake_download(
        url: str,
        dest: Path,
        expected_size_bytes: int,
        expected_sha256: str,
    ) -> None:
        calls.append((url, dest, expected_size_bytes, expected_sha256))
        assert (url, expected_size_bytes, expected_sha256) == (
            fixture.entry.artifact.url,
            fixture.entry.artifact.size_bytes,
            fixture.entry.artifact.sha256,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture.archive_path, dest)
        nvattest_install._verify_file(dest, expected_size_bytes, expected_sha256)

    monkeypatch.setattr(nvattest_install, "_download_file", fake_download)
    return calls


def _write_payload_tarball(
    archive_path: Path,
    entry: NvattestTargetEntry,
    *,
    roots: tuple[str, ...],
    label: str,
    omitted: set[str],
    executable_overrides: dict[str, bool],
) -> None:
    with tarfile.open(archive_path, "w:xz") as archive:
        for root in roots:
            prefix = f"{root}/" if root else ""
            if root:
                _add_dir(archive, root)
            dirs = _payload_dirs(entry, omitted)
            for directory in sorted(dirs):
                _add_dir(archive, f"{prefix}{directory}")
            for member in entry.inventory:
                if member.relpath in omitted:
                    continue
                path = f"{prefix}{member.relpath}"
                if member.kind == "symlink":
                    assert member.symlink_target is not None
                    _add_symlink(archive, path, member.symlink_target)
                    continue
                executable = executable_overrides.get(
                    member.relpath,
                    member.executable,
                )
                _add_file(
                    archive,
                    path,
                    _member_content(member.relpath, label),
                    mode=0o755 if executable else 0o644,
                )


def _payload_dirs(entry: NvattestTargetEntry, omitted: set[str]) -> set[str]:
    dirs: set[str] = set()
    for member in entry.inventory:
        if member.relpath in omitted:
            continue
        parts = Path(member.relpath).parts[:-1]
        for index in range(1, len(parts) + 1):
            dirs.add(Path(*parts[:index]).as_posix())
    return dirs


def _raw_download_fixture(
    tmp_path: Path,
    entry: NvattestTargetEntry,
    *,
    data: bytes,
    expected_size_delta: int = 0,
    expected_sha256: str | None = None,
) -> FixtureArchive:
    archive_path = tmp_path / f"raw-{entry.key}.tar.xz"
    archive_path.write_bytes(data)
    artifact = NvattestArtifactIdentity(
        name=archive_path.name,
        url=f"https://example.invalid/{archive_path.name}",
        size_bytes=len(data) + expected_size_delta,
        sha256=expected_sha256 or hashlib.sha256(data).hexdigest(),
    )
    return FixtureArchive(
        archive_path=archive_path,
        entry=replace(entry, artifact=artifact),
    )


def _path_traversal_archive(
    tmp_path: Path,
    entry: NvattestTargetEntry,
) -> FixtureArchive:
    archive_path = tmp_path / f"path-traversal-{entry.key}.tar.xz"
    with tarfile.open(archive_path, "w:xz") as archive:
        _add_dir(archive, "payload")
        _add_symlink(archive, "payload/lib", "../../outside")
    return FixtureArchive(
        archive_path=archive_path,
        entry=_fixture_entry(tmp_path, entry, archive_path=archive_path),
    )


def _duplicate_wrapped_archive(
    tmp_path: Path,
    entry: NvattestTargetEntry,
) -> FixtureArchive:
    archive_path = tmp_path / f"duplicate-wrapped-{entry.key}.tar.xz"
    _write_payload_tarball(
        archive_path,
        entry,
        roots=("one", "two"),
        label="duplicate",
        omitted=set(),
        executable_overrides={},
    )
    return FixtureArchive(
        archive_path=archive_path,
        entry=_fixture_entry(tmp_path, entry, archive_path=archive_path),
    )


def _invalid_flat_archive(
    tmp_path: Path,
    entry: NvattestTargetEntry,
) -> FixtureArchive:
    archive_path = tmp_path / f"invalid-flat-{entry.key}.tar.xz"
    with tarfile.open(archive_path, "w:xz") as archive:
        _add_file(archive, "nvattest", b"binary\n", mode=0o755)
        _add_file(archive, "libnvat", b"library\n", mode=0o644)
    return FixtureArchive(
        archive_path=archive_path,
        entry=_fixture_entry(tmp_path, entry, archive_path=archive_path),
    )


def _inject_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    fixture: FixtureArchive,
) -> None:
    if failure_point == "before_download":

        def fail_before_download(*_args: object, **_kwargs: object) -> None:
            raise nvattest_install.NvattestInstallError("download_failed", "before")

        monkeypatch.setattr(nvattest_install, "_download_file", fail_before_download)
        return

    if failure_point == "after_download":

        def fail_after_download(
            _url: str,
            dest: Path,
            _expected_size_bytes: int,
            _expected_sha256: str,
        ) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fixture.archive_path, dest)
            raise nvattest_install.NvattestInstallError("download_failed", "after")

        monkeypatch.setattr(nvattest_install, "_download_file", fail_after_download)
        return

    _install_download_from_fixture(monkeypatch, fixture)

    if failure_point == "after_verification":
        monkeypatch.setattr(
            nvattest_install,
            "_safe_extract_nvattest_tarball",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                nvattest_install.NvattestInstallError("archive_extract_failed", "after")
            ),
        )
        return

    if failure_point == "after_extraction":
        real_find = nvattest_install._find_extracted_root

        def fail_after_extraction(raw_dir: Path, entry: NvattestTargetEntry) -> Path:
            real_find(raw_dir, entry)
            raise nvattest_install.NvattestInstallError(
                "archive_layout_invalid",
                "after extraction",
            )

        monkeypatch.setattr(
            nvattest_install, "_find_extracted_root", fail_after_extraction
        )
        return

    if failure_point == "after_layout_validation":
        monkeypatch.setattr(
            nvattest_install,
            "_materialize_payload_tree",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                nvattest_install.NvattestInstallError(
                    "archive_layout_invalid",
                    "after layout",
                )
            ),
        )
        return

    if failure_point == "after_fingerprinting":
        real_promote = nvattest_install._promote_payload_tree

        def fail_after_fingerprinting(
            payload_dir: Path,
            root: Path,
            sidecar_json: str,
        ) -> None:
            assert payload_dir.exists()
            assert root.exists()
            assert sidecar_json
            raise nvattest_install.NvattestInstallError(
                "archive_layout_invalid",
                "after fingerprint",
            )

        monkeypatch.setattr(
            nvattest_install, "_promote_payload_tree", fail_after_fingerprinting
        )
        assert real_promote is not None
        return

    if failure_point == "during_final_promotion":
        original_replace = Path.replace

        def fail_sidecar_commit(path: Path, target: Path) -> Path:
            if path.name == "sidecar.tmp":
                raise NvattestInstallErrorProxy("sidecar commit failed")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_sidecar_commit)
        return

    raise AssertionError(f"unknown failure point: {failure_point}")


def _assert_payload_layout(root: Path, entry: NvattestTargetEntry) -> None:
    assert {path.name for path in root.iterdir()} >= {
        *PAYLOAD_TOP_LEVEL,
        nvattest_install.SIDECAR_NAME,
    }
    observed = {
        fact["relpath"]: fact
        for fact in nvattest_install._payload_member_facts(root, entry)
    }
    assert set(observed) == {member.relpath for member in entry.inventory}
    for member in entry.inventory:
        path = root / member.relpath
        fact = observed[member.relpath]
        assert fact["kind"] == member.kind
        assert fact["symlink_target"] == member.symlink_target
        assert fact["executable"] == member.executable
        if member.kind == "symlink":
            assert path.is_symlink()
            assert path.readlink() == Path(member.symlink_target or "")
        else:
            assert path.is_file()
            assert not path.is_symlink()


def _assert_sidecar_binding(root: Path, entry: NvattestTargetEntry) -> None:
    sidecar = json.loads(
        (root / nvattest_install.SIDECAR_NAME).read_text(encoding="utf-8")
    )
    assert set(sidecar) == SIDECAR_KEYS
    assert sidecar["schema_version"] == nvattest_install.SIDECAR_SCHEMA_VERSION
    assert sidecar["target_key"] == entry.key
    assert sidecar["version"] == entry.source.version
    assert sidecar["artifact"] == entry.artifact.to_payload()
    assert sidecar["tree_fingerprint_sha256"] == (
        nvattest_install._tree_fingerprint_sha256(root, entry)
    )
    assert "fingerprint_json" not in sidecar
    assert "companion_manifest" not in sidecar


def _snapshot_install(root: Path) -> dict[str, tuple[object, ...]]:
    paths = [root / name for name in PAYLOAD_TOP_LEVEL]
    paths.append(root / nvattest_install.SIDECAR_NAME)
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in paths:
        _snapshot_path(path, root, snapshot)
    return snapshot


def _snapshot_tree(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    _snapshot_path(root, root.parent, snapshot)
    return snapshot


def _snapshot_path(
    path: Path,
    root: Path,
    snapshot: dict[str, tuple[object, ...]],
) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    relpath = path.relative_to(root).as_posix()
    if path.is_symlink():
        snapshot[relpath] = ("symlink", os.readlink(path))
        return
    if path.is_dir():
        snapshot[relpath] = ("dir", mode & 0o777)
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _snapshot_path(child, root, snapshot)
        return
    snapshot[relpath] = (
        "file",
        mode & 0o777,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _runtime_lib(entry: NvattestTargetEntry) -> Path:
    for member in entry.inventory:
        if member.kind == "regular" and member.relpath.startswith("lib/"):
            return Path(member.relpath)
    raise AssertionError("missing regular lib member")


def _first_symlink(entry: NvattestTargetEntry) -> Path:
    for member in entry.inventory:
        if member.kind == "symlink":
            return Path(member.relpath)
    raise AssertionError("missing symlink member")


def _append(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(b"mutation\n")


def _replace_symlink(path: Path, target: str) -> None:
    path.unlink()
    path.symlink_to(target)


def _replace_with_file(path: Path) -> None:
    path.unlink()
    path.write_text("wrong kind\n", encoding="utf-8")


def _replace_with_symlink(path: Path) -> None:
    path.unlink()
    path.symlink_to("wrong-kind-target")


def _member_content(relpath: str, label: str) -> bytes:
    return f"{label}:{relpath}\n".encode("utf-8")


def _add_dir(archive: tarfile.TarFile, name: str) -> None:
    info = _tarinfo(name, tarfile.DIRTYPE, 0o755)
    archive.addfile(info)


def _add_file(
    archive: tarfile.TarFile,
    name: str,
    data: bytes,
    *,
    mode: int,
) -> None:
    info = _tarinfo(name, tarfile.REGTYPE, mode)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def _add_symlink(archive: tarfile.TarFile, name: str, linkname: str) -> None:
    info = _tarinfo(name, tarfile.SYMTYPE, 0o777)
    info.linkname = linkname
    archive.addfile(info)


def _tarinfo(name: str, member_type: bytes, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info
