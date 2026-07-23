# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import scripts.release_publish as publisher
from scripts.release_candidate_driver import CandidateReport, DriverError
from scripts.transparency_core import failure

SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
TOKEN = "pypi-canary-token"
PROOF_TARGETS = (
    "linux-x86_64-musl",
    "linux-aarch64-musl",
    "macos-arm64",
)


def _sha(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _release_version() -> str:
    return publisher._release_version_from_expected(
        publisher.expected_package_names(include_models=False)
    )


def _models_version() -> str:
    return publisher._models_version_from_expected(
        publisher.expected_package_names(include_models=True)
    )


def _manifest_names() -> list[str]:
    return [
        f"{artifact}.rust-release-manifest.json"
        for artifact in publisher.rust_artifact_targets()
    ]


def _candidate(
    root: Path,
    *,
    include_models: bool = False,
    version: str | None = None,
    unknown_name: str | None = None,
) -> CandidateReport:
    candidate_version = version or _release_version()
    release_dir = root / "dist" / "release-candidate" / candidate_version
    evidence_dir = root / "target" / "release-evidence" / candidate_version
    release_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "proofs").mkdir(parents=True, exist_ok=True)

    names = [
        *publisher.expected_package_names(include_models=include_models),
        *_manifest_names(),
    ]
    if unknown_name is not None:
        names.append(unknown_name)
    for name in names:
        (release_dir / name).write_bytes(f"retained bytes for {name}\n".encode())

    files: list[dict[str, Any]] = []
    for path in sorted(release_dir.iterdir(), key=lambda item: item.name):
        digest, byte_count = _sha(path)
        files.append({"bytes": byte_count, "name": path.name, "sha256": digest})

    ledger = {
        "candidate": {
            "candidate_digest": "b" * 64,
            "file_count": len(files),
            "files": files,
            "manifest_file_count": len(_manifest_names()),
            "package_file_count": len(
                publisher.expected_package_names(include_models=include_models)
            ),
            "path": f"dist/release-candidate/{candidate_version}",
        },
        "models": {
            "decision": "include" if include_models else "exclude",
            "package_version": _models_version(),
        },
        "product": "solstone",
        "proofs": {"expected_targets": list(PROOF_TARGETS)},
        "source_commit": SOURCE_COMMIT,
        "version": candidate_version,
    }
    (evidence_dir / "ledger.json").write_text(
        json.dumps(ledger, sort_keys=True), encoding="utf-8"
    )

    proof_hashes: dict[str, str] = {}
    for target in PROOF_TARGETS:
        path = evidence_dir / "proofs" / f"{target}.json"
        path.write_text(
            json.dumps({"target": target, "version": candidate_version}),
            encoding="utf-8",
        )
        proof_hashes[target] = _sha(path)[0]

    return CandidateReport(
        heading="retained-candidate-valid",
        version=candidate_version,
        release_dir=release_dir,
        evidence_dir=evidence_dir,
        payload_files=len(files),
        candidate_digest="b" * 64,
        ledger_sha256=_sha(evidence_dir / "ledger.json")[0],
        proof_sha256=proof_hashes,
        bundle_digest="c" * 64,
    )


def _ledger(report: CandidateReport) -> dict[str, Any]:
    return json.loads((report.evidence_dir / "ledger.json").read_text())


def _write_ledger(report: CandidateReport, ledger: Mapping[str, Any]) -> None:
    (report.evidence_dir / "ledger.json").write_text(
        json.dumps(ledger, sort_keys=True), encoding="utf-8"
    )


def _config(
    root: Path,
    report: CandidateReport,
    *,
    mode: publisher.Mode = "test",
    token: str = TOKEN,
    ledger: Mapping[str, Any] | None = None,
) -> publisher.PublishConfig:
    retained_ledger = dict(_ledger(report) if ledger is None else ledger)
    return publisher.PublishConfig(
        mode=mode,
        root=root,
        release_dir=report.release_dir,
        version=report.version,
        source_commit=str(retained_ledger["source_commit"]),
        models_decision=retained_ledger["models"]["decision"],
        repository_url=(
            publisher.PRODUCTION_REPOSITORY_URL
            if mode == "production"
            else publisher.TEST_REPOSITORY_URL
        ),
        index_base_url=(
            publisher.PRODUCTION_INDEX_BASE_URL
            if mode == "production"
            else publisher.TEST_INDEX_BASE_URL
        ),
        pypi_token=token,
        retained_ledger=retained_ledger,
    )


def _patch_recover(
    monkeypatch: pytest.MonkeyPatch,
    report: CandidateReport,
    *,
    heading: str = "retained-candidate-valid",
    rehash_payloads: bool = False,
) -> None:
    def recover(root: Path, *, version: str, source_commit: str) -> CandidateReport:
        assert root == report.release_dir.parents[2]
        assert version == report.version
        assert source_commit == SOURCE_COMMIT
        if rehash_payloads:
            ledger = _ledger(report)
            failures = []
            for item in ledger["candidate"]["files"]:
                path = report.release_dir / item["name"]
                digest, byte_count = _sha(path)
                if digest != item["sha256"] or byte_count != item["bytes"]:
                    failures.append(
                        failure(
                            "retained candidate payload hash mismatch",
                            expected=f"{item['name']} {item['sha256']}",
                            actual=f"{digest} bytes={byte_count}",
                            repair="rerun release recovery before publishing",
                        )
                    )
            if failures:
                raise DriverError(failures)
        return replace(report, heading=heading)

    monkeypatch.setattr(publisher, "recover_candidate", recover)


def _full_snapshot(
    projects: Sequence[publisher.ProjectExpectation],
) -> Mapping[tuple[str, str], Mapping[str, Any]]:
    return {
        (project.project, project.version): {
            "urls": [
                {"digests": {"sha256": digest}, "filename": filename}
                for filename, digest in sorted(project.files.items())
            ]
        }
        for project in projects
    }


def _empty_snapshot(
    projects: Sequence[publisher.ProjectExpectation],
) -> Mapping[tuple[str, str], None]:
    return {(project.project, project.version): None for project in projects}


def _divergent_snapshot(
    projects: Sequence[publisher.ProjectExpectation],
) -> Mapping[tuple[str, str], Mapping[str, Any] | None]:
    snapshot = dict(_full_snapshot(projects))
    first_project = projects[0]
    payload = dict(snapshot[(first_project.project, first_project.version)])
    urls = list(payload["urls"])
    urls[0] = {
        "digests": {"sha256": "0" * 64},
        "filename": urls[0]["filename"],
    }
    payload["urls"] = urls
    snapshot[(first_project.project, first_project.version)] = payload
    return snapshot


class RecordingIndex:
    def __init__(
        self,
        calls: list[str],
        snapshots: Sequence[
            Callable[
                [Sequence[publisher.ProjectExpectation]],
                Mapping[tuple[str, str], Any],
            ]
        ],
    ) -> None:
        self.calls = calls
        self.snapshots = list(snapshots)
        self.base_urls: list[str] = []
        self.projects: list[tuple[tuple[str, str], ...]] = []

    def __call__(
        self,
        base_url: str,
        projects: Sequence[publisher.ProjectExpectation],
    ) -> Mapping[tuple[str, str], Mapping[str, Any] | None]:
        self.calls.append("index")
        self.base_urls.append(base_url)
        self.projects.append(
            tuple((project.project, project.version) for project in projects)
        )
        snapshot = self.snapshots.pop(0) if self.snapshots else _empty_snapshot
        return snapshot(projects)


def _upload_runner(
    calls: list[str],
    *,
    fail: bool = False,
    token: str = TOKEN,
) -> publisher.ProcessRunner:
    def run(
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append("upload")
        assert cwd
        assert capture_output is True
        assert text is True
        assert check is False
        assert env["TWINE_USERNAME"] == "__token__"
        assert env["TWINE_PASSWORD"] == token
        assert token not in " ".join(argv)
        if fail:
            return subprocess.CompletedProcess(
                list(argv),
                1,
                stdout="",
                stderr=f"upload failed for {token}",
            )
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    return run


class RecordingGit:
    def __init__(
        self,
        calls: list[str],
        *,
        source_exists: bool = True,
        changelog_present: bool = True,
        remote_commit: str | None = None,
        local_commit: str | None = None,
        push_returncode: int = 0,
    ) -> None:
        self.calls = calls
        self.source_exists = source_exists
        self.changelog_present = changelog_present
        self.remote_commit = remote_commit
        self.local_commit = local_commit
        self.push_returncode = push_returncode

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd
        assert capture_output is True
        assert text is True
        assert check is False
        if argv[:4] == ["git", "rev-parse", "--verify", "--quiet"]:
            target = argv[4]
            if target.endswith("^{commit}"):
                self.calls.append("source-check")
                return subprocess.CompletedProcess(
                    list(argv),
                    0 if self.source_exists else 1,
                    stdout=SOURCE_COMMIT,
                    stderr="",
                )
            self.calls.append("local-tag-check")
            stdout = f"{self.local_commit}\n" if self.local_commit is not None else ""
            return subprocess.CompletedProcess(
                list(argv),
                0 if self.local_commit is not None else 1,
                stdout=stdout,
                stderr="",
            )
        if argv[:2] == ["git", "show"]:
            self.calls.append("changelog")
            text_out = (
                f"## [{_release_version()}] - 2026-07-22\n\nReleased.\n"
                if self.changelog_present
                else "## [0.0.0] - 2026-01-01\n\nOld.\n"
            )
            return subprocess.CompletedProcess(
                list(argv), 0, stdout=text_out, stderr=""
            )
        if argv[:3] == ["git", "ls-remote", "--tags"]:
            self.calls.append("tag-check")
            stdout = ""
            if self.remote_commit is not None:
                tag_ref = argv[-2].removeprefix("refs/tags/")
                stdout = f"{self.remote_commit}\trefs/tags/{tag_ref}^{{}}\n"
            return subprocess.CompletedProcess(list(argv), 0, stdout=stdout, stderr="")
        if argv[:2] == ["git", "tag"]:
            self.calls.append("tag")
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")
        if argv[:2] == ["git", "push"]:
            self.calls.append("push")
            return subprocess.CompletedProcess(
                list(argv),
                self.push_returncode,
                stdout="",
                stderr="push failed" if self.push_returncode else "",
            )
        raise AssertionError(f"unexpected git invocation: {argv!r}")


def _gh_runner(calls: list[str], *, fail: bool = False) -> publisher.ProcessRunner:
    def run(
        argv: Sequence[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append("witness")
        assert cwd
        assert argv[:3] == ["gh", "release", "create"]
        assert capture_output is True
        assert text is True
        assert check is False
        return subprocess.CompletedProcess(
            list(argv),
            1 if fail else 0,
            stdout="",
            stderr="gh failed" if fail else "",
        )

    return run


def _forbidden_runner(label: str, calls: list[str]) -> publisher.ProcessRunner:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(label)
        raise AssertionError(f"{label} seam must not be invoked")

    return run


def _run_publish(
    config: publisher.PublishConfig,
    *,
    calls: list[str],
    index: RecordingIndex,
    upload_runner: publisher.ProcessRunner | None = None,
    git_runner: publisher.ProcessRunner | None = None,
    gh_runner: publisher.ProcessRunner | None = None,
    max_verify_attempts: int = 1,
) -> publisher.PublishResult:
    return publisher.publish_release(
        config=config,
        index_client=index,
        upload_runner=upload_runner or _upload_runner(calls),
        git_runner=git_runner or _forbidden_runner("git", calls),
        gh_runner=gh_runner or _forbidden_runner("witness", calls),
        sleep=lambda _seconds: None,
        max_verify_attempts=max_verify_attempts,
        verify_sleep_seconds=0,
    )


def _first_failure(error: DriverError) -> str:
    return error.failures[0].error


def test_publish_config_repr_does_not_expose_secret(tmp_path: Path) -> None:
    report = _candidate(tmp_path)
    config = _config(tmp_path, report, token=TOKEN)

    assert TOKEN not in repr(config)


def test_config_requires_release_dir_before_ledger(tmp_path: Path) -> None:
    with pytest.raises(DriverError) as excinfo:
        publisher.PublishConfig.from_env(root=tmp_path, mode="test", env={})

    assert _first_failure(excinfo.value) == "release publish RELEASE_DIR is missing"


def test_config_requires_mode_token_before_ledger(tmp_path: Path) -> None:
    version = _release_version()
    release_dir = tmp_path / "dist" / "release-candidate" / version

    with pytest.raises(DriverError) as excinfo:
        publisher.PublishConfig.from_env(
            root=tmp_path,
            mode="test",
            env={"RELEASE_DIR": str(release_dir)},
        )

    assert _first_failure(excinfo.value) == "release publish environment is incomplete"


def test_config_wraps_malformed_ledger(tmp_path: Path) -> None:
    version = _release_version()
    release_dir = tmp_path / "dist" / "release-candidate" / version
    ledger_path = tmp_path / "target" / "release-evidence" / version / "ledger.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(DriverError) as excinfo:
        publisher.PublishConfig.from_env(
            root=tmp_path,
            mode="test",
            env={"RELEASE_DIR": str(release_dir), "TESTPYPI_TOKEN": TOKEN},
        )

    assert _first_failure(excinfo.value) == (
        "release publish retained ledger could not be read"
    )


def test_test_mode_clean_upload_verify_never_invokes_git_or_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    result = _run_publish(
        _config(tmp_path, report, mode="test"),
        calls=calls,
        index=index,
    )

    assert calls == ["index", "upload", "index"]
    assert index.base_urls == [publisher.TEST_INDEX_BASE_URL] * 2
    assert result.tag_state == "test-skipped"
    assert result.witness_status.state == "test-skipped"


def test_production_clean_path_orders_upload_verify_tag_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    result = _run_publish(
        _config(tmp_path, report, mode="production"),
        calls=calls,
        index=index,
        git_runner=RecordingGit(calls),
        gh_runner=_gh_runner(calls),
    )

    assert calls == [
        "source-check",
        "changelog",
        "index",
        "upload",
        "index",
        "tag-check",
        "local-tag-check",
        "tag",
        "push",
        "witness",
    ]
    assert index.base_urls == [publisher.PRODUCTION_INDEX_BASE_URL] * 2
    assert result.upload_state == "uploaded"
    assert result.tag_state == "created-and-pushed"
    assert result.witness_status.state == "created"


def test_byte_divergence_from_recover_prevents_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    first_payload = next(report.release_dir.glob("*.whl"))
    first_payload.write_bytes(b"mutated after ledger\n")
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(_config(tmp_path, report), calls=calls, index=index)

    assert _first_failure(excinfo.value) == "retained candidate payload hash mismatch"
    assert calls == []


def test_recover_heading_must_be_retained_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, heading="not-ready")
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(_config(tmp_path, report), calls=calls, index=index)

    assert _first_failure(excinfo.value) == "release publish recover verdict is invalid"
    assert calls == []


def test_unknown_asset_class_prevents_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path, unknown_name="release-notes.txt")
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(_config(tmp_path, report), calls=calls, index=index)

    assert _first_failure(excinfo.value) == (
        "release publish candidate artifact set is not canonical"
    )
    assert calls == []


def test_models_decision_gate_requires_models_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path, include_models=False)
    ledger = _ledger(report)
    ledger["models"]["decision"] = "include"
    _write_ledger(report, ledger)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(
            _config(tmp_path, report, ledger=ledger),
            calls=calls,
            index=index,
        )

    assert _first_failure(excinfo.value) == (
        "release publish candidate artifact set is not canonical"
    )
    assert "missing uploads" in excinfo.value.failures[0].actual
    assert calls == []


def test_checkout_version_mismatch_refuses_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path, version="0.0.0")
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(_config(tmp_path, report), calls=calls, index=index)

    assert _first_failure(excinfo.value) == (
        "release publish checkout version does not match retained candidate"
    )
    assert calls == []


def test_already_published_skips_upload_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_full_snapshot, _full_snapshot])

    result = _run_publish(_config(tmp_path, report), calls=calls, index=index)

    assert calls == ["index", "index"]
    assert result.upload_state == "skipped-already-published"
    assert result.verified is True


def test_already_published_digest_divergence_refuses_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_divergent_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(_config(tmp_path, report), calls=calls, index=index)

    assert _first_failure(excinfo.value) == "release publish package index is divergent"
    assert calls == ["index"]


def test_upload_failure_is_classified_and_redacts_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(
            _config(tmp_path, report, token=TOKEN),
            calls=calls,
            index=index,
            upload_runner=_upload_runner(calls, fail=True, token=TOKEN),
        )

    caplog.set_level(logging.ERROR)
    publisher._print_failures(excinfo.value)
    captured = capsys.readouterr()
    assert _first_failure(excinfo.value) == "release publish upload failed"
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in excinfo.value.failures[0].actual
    assert TOKEN not in caplog.text
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert calls == ["index", "upload"]


def test_verify_timeout_stops_before_tag_and_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _empty_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(
            _config(tmp_path, report, mode="production"),
            calls=calls,
            index=index,
            git_runner=RecordingGit(calls),
            gh_runner=_gh_runner(calls),
        )

    assert _first_failure(excinfo.value) == (
        "release publish package index verification timed out"
    )
    assert calls == ["source-check", "changelog", "index", "upload", "index"]
    assert "tag" not in calls
    assert "push" not in calls
    assert "witness" not in calls


def test_production_source_commit_missing_refuses_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(
            _config(tmp_path, report, mode="production"),
            calls=calls,
            index=index,
            git_runner=RecordingGit(calls, source_exists=False),
            gh_runner=_gh_runner(calls),
        )

    assert _first_failure(excinfo.value) == (
        "release source commit is not present in repository"
    )
    assert calls == ["source-check"]


def test_production_missing_changelog_refuses_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_empty_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(
            _config(tmp_path, report, mode="production"),
            calls=calls,
            index=index,
            git_runner=RecordingGit(calls, changelog_present=False),
            gh_runner=_gh_runner(calls),
        )

    assert _first_failure(excinfo.value) == (
        "release changelog block is missing at source commit"
    )
    assert calls == ["source-check", "changelog"]


def test_remote_tag_at_same_commit_skips_push_but_records_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_full_snapshot, _full_snapshot])

    result = _run_publish(
        _config(tmp_path, report, mode="production"),
        calls=calls,
        index=index,
        git_runner=RecordingGit(calls, remote_commit=SOURCE_COMMIT),
        gh_runner=_gh_runner(calls),
    )

    assert calls == [
        "source-check",
        "changelog",
        "index",
        "index",
        "tag-check",
        "witness",
    ]
    assert result.upload_state == "skipped-already-published"
    assert result.tag_state == "remote-already-correct"


def test_remote_tag_at_different_commit_refuses_without_push_or_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_full_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(
            _config(tmp_path, report, mode="production"),
            calls=calls,
            index=index,
            git_runner=RecordingGit(calls, remote_commit=OTHER_COMMIT),
            gh_runner=_gh_runner(calls),
        )

    assert _first_failure(excinfo.value) == (
        "release git tag points at a different commit"
    )
    assert calls == ["source-check", "changelog", "index", "index", "tag-check"]


def test_local_tag_at_different_commit_refuses_without_push_or_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_full_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(
            _config(tmp_path, report, mode="production"),
            calls=calls,
            index=index,
            git_runner=RecordingGit(calls, local_commit=OTHER_COMMIT),
            gh_runner=_gh_runner(calls),
        )

    assert _first_failure(excinfo.value) == (
        "release local git tag points at a different commit"
    )
    assert calls == [
        "source-check",
        "changelog",
        "index",
        "index",
        "tag-check",
        "local-tag-check",
    ]


def test_tag_push_failure_names_resume_and_skips_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_full_snapshot, _full_snapshot])

    with pytest.raises(DriverError) as excinfo:
        _run_publish(
            _config(tmp_path, report, mode="production"),
            calls=calls,
            index=index,
            git_runner=RecordingGit(calls, push_returncode=1),
            gh_runner=_gh_runner(calls),
        )

    assert _first_failure(excinfo.value) == "release git tag push failed"
    assert "make publish-release RELEASE_DIR=" in excinfo.value.failures[0].repair
    assert calls == [
        "source-check",
        "changelog",
        "index",
        "index",
        "tag-check",
        "local-tag-check",
        "tag",
        "push",
    ]


def test_witness_failure_records_gap_and_exits_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_full_snapshot, _full_snapshot])
    caplog.set_level(logging.ERROR)

    result = _run_publish(
        _config(tmp_path, report, mode="production"),
        calls=calls,
        index=index,
        git_runner=RecordingGit(calls),
        gh_runner=_gh_runner(calls, fail=True),
    )

    assert result.witness_status.state == "witness-gap"
    assert f"make publish-release RELEASE_DIR={report.release_dir}" in (
        result.witness_status.message
    )
    assert "GitHub Release witness failed" in caplog.text
    assert calls[-1] == "witness"


def test_missing_gh_records_gap_and_exits_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    report = _candidate(tmp_path)
    _patch_recover(monkeypatch, report, rehash_payloads=True)
    calls: list[str] = []
    index = RecordingIndex(calls, [_full_snapshot, _full_snapshot])
    caplog.set_level(logging.ERROR)

    def missing_gh(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append("witness")
        raise FileNotFoundError("gh")

    result = _run_publish(
        _config(tmp_path, report, mode="production"),
        calls=calls,
        index=index,
        git_runner=RecordingGit(calls),
        gh_runner=missing_gh,
    )

    assert result.witness_status.state == "witness-gap"
    assert "FileNotFoundError" in caplog.text
    assert calls[-1] == "witness"
