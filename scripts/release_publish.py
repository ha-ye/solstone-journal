#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Publish an already-finalized solstone release candidate.

Production mode publishes retained PyPI artifacts, verifies PyPI's JSON index,
tags the retained source commit, and records a GitHub Release witness. Test mode
is a TestPyPI upload+verify rehearsal only; it does not validate changelog or
tag readiness, and it never invokes git or gh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_nvattest_authority import (
    render_nvattest_authority_json,  # noqa: E402
)
from scripts.check_rust_release_manifest import (  # noqa: E402
    CORE_UNSUPPORTED_TOMBSTONE_RECORD,
    Failure,
    expected_package_names,
    rust_artifact_targets,
    validate_core_unsupported_tombstone_record,
)
from scripts.release_archive_manifest import (  # noqa: E402
    assert_archives_semantically_identical,
)
from scripts.release_candidate_driver import (  # noqa: E402
    CandidateReport,
    DriverError,
    _retained_authority_binding,
)
from scripts.release_ledger import read_retained_ledger  # noqa: E402
from scripts.transparency_core import (  # noqa: E402
    fail_closed,
    failure,
    recover_candidate,
)
from scripts.transparency_head_log import WitnessStatus  # noqa: E402

LOG = logging.getLogger(__name__)

Mode = Literal["production", "test"]
IndexStatus = Literal["empty", "full", "divergent"]
UploadState = Literal["uploaded", "skipped-already-published"]
MODEL_PROJECT = "solstone-journal-models"

PRODUCTION_REPOSITORY_URL = "https://upload.pypi.org/legacy/"
TEST_REPOSITORY_URL = "https://test.pypi.org/legacy/"
PRODUCTION_INDEX_BASE_URL = "https://pypi.org"
TEST_INDEX_BASE_URL = "https://test.pypi.org"
DEFAULT_VERIFY_ATTEMPTS = 12
DEFAULT_VERIFY_SLEEP_SECONDS = 10.0
PYPI_TOKEN_ENV = {
    "production": "PYPI_TOKEN",
    "test": "TESTPYPI_TOKEN",
}

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
IndexClient = Callable[
    [str, Sequence["ProjectExpectation"]],
    Mapping[tuple[str, str], Mapping[str, Any] | None],
]
ArchiveDownloader = Callable[[str, Path, int], None]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class PublishConfig:
    mode: Mode
    root: Path
    release_dir: Path
    version: str
    source_commit: str
    models_decision: Literal["include", "exclude"]
    repository_url: str
    index_base_url: str
    pypi_token: str = field(repr=False)
    retained_ledger: Mapping[str, Any] = field(repr=False)

    @property
    def resume_target(self) -> str:
        target = (
            "publish-release" if self.mode == "production" else "publish-release-test"
        )
        return f"make {target} RELEASE_DIR={self.release_dir}"

    @classmethod
    def from_env(
        cls,
        *,
        root: Path,
        mode: Mode,
        env: Mapping[str, str],
    ) -> PublishConfig:
        raw_release_dir = env.get("RELEASE_DIR", "")
        if not raw_release_dir:
            raise DriverError(
                [
                    failure(
                        "release publish RELEASE_DIR is missing",
                        expected="RELEASE_DIR pointing at root/dist/release-candidate/<version>",
                        actual="<missing>",
                        repair=(
                            "make publish-release RELEASE_DIR=dist/release-candidate/<version>"
                        ),
                    )
                ]
            )
        version = Path(raw_release_dir).name
        supplied_release_dir = Path(raw_release_dir).resolve()
        derived_release_dir = (
            root / "dist" / "release-candidate" / str(version)
        ).resolve()
        if supplied_release_dir != derived_release_dir:
            raise DriverError(
                [
                    failure(
                        "release publish RELEASE_DIR does not match retained path",
                        expected=str(derived_release_dir),
                        actual=str(supplied_release_dir),
                        repair=(
                            "point RELEASE_DIR at root/dist/release-candidate/<version>"
                        ),
                    )
                ]
            )
        token_name = PYPI_TOKEN_ENV[mode]
        token = env.get(token_name, "")
        if not token:
            raise DriverError(
                [
                    failure(
                        "release publish environment is incomplete",
                        expected=token_name,
                        actual="missing",
                        repair=f"set {token_name} and retry",
                    )
                ]
            )
        ledger = _read_ledger_for_publish(
            root / "target" / "release-evidence" / version / "ledger.json"
        )
        source_commit = ledger.get("source_commit")
        if not isinstance(source_commit, str) or not source_commit:
            raise DriverError(
                [
                    failure(
                        "release publish retained ledger source_commit is invalid",
                        expected="non-empty retained source_commit",
                        actual=repr(source_commit),
                        repair="restore retained release evidence before publishing",
                    )
                ]
            )
        models = ledger.get("models")
        models_decision = (
            models.get("decision") if isinstance(models, Mapping) else None
        )
        if models_decision not in {"include", "exclude"}:
            raise DriverError(
                [
                    failure(
                        "release publish retained ledger models decision is invalid",
                        expected="include or exclude",
                        actual=repr(models_decision),
                        repair="restore retained release evidence before publishing",
                    )
                ]
            )
        return cls(
            mode=mode,
            root=root,
            release_dir=derived_release_dir,
            version=version,
            source_commit=source_commit,
            models_decision=models_decision,
            repository_url=(
                PRODUCTION_REPOSITORY_URL
                if mode == "production"
                else TEST_REPOSITORY_URL
            ),
            index_base_url=(
                PRODUCTION_INDEX_BASE_URL
                if mode == "production"
                else TEST_INDEX_BASE_URL
            ),
            pypi_token=token,
            retained_ledger=ledger,
        )


@dataclass(frozen=True)
class ArtifactEntry:
    name: str
    path: Path
    sha256: str
    bytes: int
    project: str
    version: str


@dataclass(frozen=True)
class ClassifiedArtifacts:
    uploads: tuple[ArtifactEntry, ...]

    @property
    def project_expectations(self) -> tuple["ProjectExpectation", ...]:
        return _project_expectations_for(self.uploads)


@dataclass(frozen=True)
class ProjectExpectation:
    project: str
    version: str
    files: Mapping[str, str]


def _project_expectations_for(
    entries: Sequence[ArtifactEntry],
) -> tuple[ProjectExpectation, ...]:
    grouped: dict[tuple[str, str], dict[str, str]] = {}
    for entry in entries:
        grouped.setdefault((entry.project, entry.version), {})[entry.name] = (
            entry.sha256
        )
    return tuple(
        ProjectExpectation(project=project, version=version, files=dict(files))
        for (project, version), files in sorted(grouped.items())
    )


@dataclass(frozen=True)
class ProjectIndexMatch:
    project: str
    version: str
    status: IndexStatus
    expected: Mapping[str, str]
    actual: Mapping[str, str]


@dataclass(frozen=True)
class ResolvedPublishSet:
    uploads: tuple[ArtifactEntry, ...]
    verify_expectations: tuple["ProjectExpectation", ...]
    reused_project: str
    reused_files: tuple[str, ...]
    reused_model_pin: "ProjectExpectation | None"


@dataclass(frozen=True)
class PublishResult:
    mode: Mode
    version: str
    source_commit: str
    upload_state: UploadState
    verified: bool
    tag_state: str
    witness_status: WitnessStatus
    uploaded_files: tuple[str, ...]
    projects: tuple[str, ...]
    reused_project: str
    reused_files: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "projects": list(self.projects),
            "reused_files": list(self.reused_files),
            "reused_project": self.reused_project,
            "source_commit": self.source_commit,
            "tag_state": self.tag_state,
            "upload_state": self.upload_state,
            "uploaded_files": list(self.uploaded_files),
            "verified": self.verified,
            "version": self.version,
            "witness_message": self.witness_status.message,
            "witness_status": self.witness_status.state,
        }


def _read_ledger_for_publish(path: Path) -> Mapping[str, Any]:
    try:
        return read_retained_ledger(path)
    except Exception as exc:
        fail_closed(
            "release publish retained ledger could not be read",
            expected="valid retained target/release-evidence/<version>/ledger.json",
            actual=type(exc).__name__,
            repair="restore retained release evidence before publishing",
        )


def _release_version_from_expected(names: Sequence[str]) -> str:
    for name in names:
        match = re.fullmatch(r"solstone-(?P<version>.+)\.tar\.gz", name)
        if match:
            return match.group("version")
    raise DriverError(
        [
            failure(
                "release publish canonical package helper did not emit root sdist",
                expected="solstone-<version>.tar.gz",
                actual=", ".join(names) or "<empty>",
                repair="repair scripts.check_rust_release_manifest.expected_package_names",
            )
        ]
    )


def _models_version_from_expected(names: Sequence[str]) -> str:
    for name in names:
        match = re.fullmatch(r"solstone_journal_models-(?P<version>.+)\.tar\.gz", name)
        if match:
            return match.group("version")
    raise DriverError(
        [
            failure(
                "release publish canonical package helper did not emit models sdist",
                expected="solstone_journal_models-<version>.tar.gz",
                actual=", ".join(names) or "<empty>",
                repair="repair scripts.check_rust_release_manifest.expected_package_names",
            )
        ]
    )


def _assert_checkout_versions_match(config: PublishConfig) -> None:
    names_without_models = expected_package_names(include_models=False)
    checkout_version = _release_version_from_expected(names_without_models)
    if checkout_version != config.version:
        raise DriverError(
            [
                failure(
                    "release publish checkout version does not match retained candidate",
                    expected=config.version,
                    actual=checkout_version,
                    repair=(
                        "run the publisher from the release checkout at "
                        f"{config.source_commit}"
                    ),
                )
            ]
        )
    models = config.retained_ledger.get("models")
    retained_models_version = (
        models.get("package_version") if isinstance(models, Mapping) else None
    )
    names_with_models = expected_package_names(include_models=True)
    checkout_models_version = _models_version_from_expected(names_with_models)
    if retained_models_version != checkout_models_version:
        raise DriverError(
            [
                failure(
                    "release publish checkout models version does not match retained candidate",
                    expected=str(retained_models_version),
                    actual=checkout_models_version,
                    repair=(
                        "run the publisher from the release checkout at "
                        f"{config.source_commit}"
                    ),
                )
            ]
        )


def _project_from_filename(
    name: str, *, release_version: str, models_version: str
) -> tuple[str, str]:
    stem = name.removesuffix(".tar.gz") if name.endswith(".tar.gz") else name
    distribution = stem.split("-", 1)[0]
    project = distribution.replace("_", "-")
    version = (
        models_version if project == "solstone-journal-models" else release_version
    )
    return project, version


def classify_candidate_artifacts(config: PublishConfig) -> ClassifiedArtifacts:
    _assert_checkout_versions_match(config)
    include_models = config.models_decision == "include"
    expected_upload_names = tuple(expected_package_names(include_models=include_models))
    expected_upload_set = set(expected_upload_names)
    expected_manifest_set = {
        f"{artifact}.rust-release-manifest.json" for artifact in rust_artifact_targets()
    }
    models = config.retained_ledger.get("models")
    models_version = (
        str(models["package_version"]) if isinstance(models, Mapping) else ""
    )
    candidate = config.retained_ledger.get("candidate")
    files = candidate.get("files") if isinstance(candidate, Mapping) else None
    if not isinstance(files, list):
        raise DriverError(
            [
                failure(
                    "release publish retained candidate file list is invalid",
                    expected="candidate.files list",
                    actual=type(files).__name__,
                    repair="restore retained release evidence before publishing",
                )
            ]
        )
    entries: dict[str, Mapping[str, Any]] = {}
    invalid_entries: list[str] = []
    duplicate_names: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping):
            invalid_entries.append(repr(item))
            continue
        name = item.get("name")
        sha256 = item.get("sha256")
        byte_count = item.get("bytes")
        if (
            not isinstance(name, str)
            or not isinstance(sha256, str)
            or not isinstance(byte_count, int)
        ):
            invalid_entries.append(repr(item))
            continue
        if name in entries:
            duplicate_names.add(name)
        entries[name] = item
    actual_names = set(entries)
    unknown = sorted(actual_names - expected_upload_set - expected_manifest_set)
    missing_uploads = sorted(expected_upload_set - actual_names)
    missing_manifests = sorted(expected_manifest_set - actual_names)
    problems: list[str] = []
    if invalid_entries:
        problems.append(f"invalid entries: {', '.join(invalid_entries)}")
    if duplicate_names:
        problems.append(f"duplicate names: {', '.join(sorted(duplicate_names))}")
    if unknown:
        problems.append(f"unknown files: {', '.join(unknown)}")
    if missing_uploads:
        problems.append(f"missing uploads: {', '.join(missing_uploads)}")
    if missing_manifests:
        problems.append(f"missing manifests: {', '.join(missing_manifests)}")
    if problems:
        raise DriverError(
            [
                failure(
                    "release publish candidate artifact set is not canonical",
                    expected=(
                        "canonical PyPI artifacts plus Rust companion manifests for "
                        f"models decision {config.models_decision}"
                    ),
                    actual="; ".join(problems),
                    repair="restore the retained release candidate or cut the next version",
                )
            ]
        )
    uploads: list[ArtifactEntry] = []
    for name in expected_upload_names:
        item = entries[name]
        project, project_version = _project_from_filename(
            name,
            release_version=config.version,
            models_version=models_version,
        )
        uploads.append(
            ArtifactEntry(
                name=name,
                path=config.release_dir / name,
                sha256=str(item["sha256"]),
                bytes=int(item["bytes"]),
                project=project,
                version=project_version,
            )
        )
    return ClassifiedArtifacts(uploads=tuple(uploads))


def _verify_recover(config: PublishConfig) -> CandidateReport:
    report = recover_candidate(
        config.root,
        version=config.version,
        source_commit=config.source_commit,
    )
    if report.heading != "retained-candidate-valid":
        raise DriverError(
            [
                failure(
                    "release publish recover verdict is invalid",
                    expected="retained-candidate-valid",
                    actual=report.heading,
                    repair=(
                        "bash scripts/release.sh --recover "
                        f"{config.version} {config.source_commit}"
                    ),
                )
            ]
        )
    return report


def _assert_nvattest_authority_matches_checkout(
    config: PublishConfig,
    report: CandidateReport,
) -> None:
    authority_bytes, failures = _retained_authority_binding(
        release_dir=report.release_dir,
        ledger=config.retained_ledger,
    )
    if authority_bytes is None and not failures:
        failures.append(
            failure(
                "release publish retained nvattest authority is missing",
                expected="retained candidate root wheel authority bytes",
                actual="<missing>",
                repair=(
                    "bash scripts/release.sh --recover "
                    f"{config.version} {config.source_commit}"
                ),
            )
        )
    if failures:
        raise DriverError(failures)

    checkout_authority_bytes = render_nvattest_authority_json().encode("utf-8")
    if checkout_authority_bytes != authority_bytes:
        raise DriverError(
            [
                failure(
                    (
                        "release publish checkout nvattest authority does not match "
                        "retained candidate"
                    ),
                    expected=hashlib.sha256(authority_bytes).hexdigest(),
                    actual=hashlib.sha256(checkout_authority_bytes).hexdigest(),
                    repair=(
                        "run the publisher from the release checkout at "
                        f"{config.source_commit}"
                    ),
                )
            ]
        )


def _verify_core_unsupported_tombstone_prerequisite(config: PublishConfig) -> None:
    if config.mode != "production":
        return
    path = (
        config.root
        / "target"
        / "release-evidence"
        / config.version
        / CORE_UNSUPPORTED_TOMBSTONE_RECORD
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DriverError(
            [
                failure(
                    "core unsupported-platform tombstone prerequisite is missing",
                    expected=str(path),
                    actual="missing",
                    repair=(
                        "publish and verify solstone-core-unsupported-platform before "
                        "publishing solstone"
                    ),
                )
            ]
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError(
            [
                failure(
                    "core unsupported-platform tombstone prerequisite could not be read",
                    expected="valid tombstone publication verification JSON",
                    actual=type(exc).__name__,
                    repair=(
                        "publish and verify solstone-core-unsupported-platform before "
                        "publishing solstone"
                    ),
                )
            ]
        ) from None
    failures = validate_core_unsupported_tombstone_record(
        payload,
        version=config.version,
    )
    if failures:
        raise DriverError(failures)


def _ensure_source_commit_exists(config: PublishConfig, runner: ProcessRunner) -> None:
    result = runner(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            f"{config.source_commit}^{{commit}}",
        ],
        cwd=config.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DriverError(
            [
                failure(
                    "release source commit is not present in repository",
                    expected=f"git commit {config.source_commit}",
                    actual="missing",
                    repair=f"fetch {config.source_commit} before publishing",
                )
            ]
        )


def extract_changelog_block(text: str, version: str) -> str:
    target = f"## [{version}]"
    lines = text.splitlines()
    selected: list[str] = []
    seen = False
    for line in lines:
        if line.startswith("## [") and seen:
            break
        if line.startswith(target):
            seen = True
        if seen:
            selected.append(line)
    return "\n".join(selected).strip() + ("\n" if selected else "")


def _read_changelog_block(config: PublishConfig, runner: ProcessRunner) -> str:
    result = runner(
        ["git", "show", f"{config.source_commit}:CHANGELOG.md"],
        cwd=config.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DriverError(
            [
                failure(
                    "release changelog could not be read at source commit",
                    expected=f"{config.source_commit}:CHANGELOG.md",
                    actual=(
                        result.stderr or result.stdout or str(result.returncode)
                    ).strip(),
                    repair=(
                        "publish only from a source commit that contains CHANGELOG.md"
                    ),
                )
            ]
        )
    block = extract_changelog_block(result.stdout, config.version)
    if not block.strip():
        raise DriverError(
            [
                failure(
                    "release changelog block is missing at source commit",
                    expected=f"## [{config.version}] in {config.source_commit}:CHANGELOG.md",
                    actual="missing",
                    repair=(
                        "add the changelog block before cutting the release candidate"
                    ),
                )
            ]
        )
    return block


def default_index_client(
    base_url: str, projects: Sequence[ProjectExpectation]
) -> Mapping[tuple[str, str], Mapping[str, Any] | None]:
    import requests

    responses: dict[tuple[str, str], Mapping[str, Any] | None] = {}
    for project in projects:
        url = f"{base_url.rstrip('/')}/pypi/{project.project}/{project.version}/json"
        try:
            response = requests.get(url, timeout=10)
        except requests.RequestException as exc:
            raise DriverError(
                [
                    failure(
                        "release publish index request failed",
                        expected=url,
                        actual=type(exc).__name__,
                        repair="retry after the package index is reachable",
                    )
                ]
            ) from None
        if response.status_code == 404:
            responses[(project.project, project.version)] = None
            continue
        if response.status_code != 200:
            raise DriverError(
                [
                    failure(
                        "release publish index request failed",
                        expected=f"{url} HTTP 200 or 404",
                        actual=f"HTTP {response.status_code}",
                        repair="retry after the package index is healthy",
                    )
                ]
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DriverError(
                [
                    failure(
                        "release publish index response is not JSON",
                        expected=url,
                        actual=type(exc).__name__,
                        repair="retry after the package index is healthy",
                    )
                ]
            ) from None
        if not isinstance(payload, Mapping):
            raise DriverError(
                [
                    failure(
                        "release publish index response is not an object",
                        expected=url,
                        actual=type(payload).__name__,
                        repair="retry after the package index is healthy",
                    )
                ]
            )
        responses[(project.project, project.version)] = payload
    return responses


def default_archive_downloader(url: str, destination: Path, max_bytes: int) -> None:
    import requests

    written = 0
    try:
        with requests.get(url, stream=True, timeout=(30, 45)) as response:
            if response.status_code != 200:
                raise DriverError(
                    [
                        failure(
                            "release publish archive download failed",
                            expected=f"{url} HTTP 200",
                            actual=f"HTTP {response.status_code}",
                            repair="retry after the package index files are reachable",
                        )
                    ]
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        destination.unlink(missing_ok=True)
                        raise DriverError(
                            [
                                failure(
                                    "release publish archive download exceeded retained size ceiling",
                                    expected=f"<= {max_bytes} bytes",
                                    actual=f"{url} wrote {written} bytes",
                                    repair="audit the package index; cut the next version if bytes differ",
                                )
                            ]
                        )
                    handle.write(chunk)
    except requests.RequestException as exc:
        destination.unlink(missing_ok=True)
        raise DriverError(
            [
                failure(
                    "release publish archive download failed",
                    expected=url,
                    actual=type(exc).__name__,
                    repair="retry after the package index files are reachable",
                )
            ]
        ) from None
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise DriverError(
            [
                failure(
                    "release publish archive download failed",
                    expected=f"write downloaded archive to {destination}",
                    actual=type(exc).__name__,
                    repair="retry after local temporary storage is healthy",
                )
            ]
        ) from None


def _remote_files_from_payload(payload: Mapping[str, Any] | None) -> Mapping[str, str]:
    if payload is None:
        return {}
    urls = payload.get("urls")
    if not isinstance(urls, list):
        return {"<invalid-urls>": ""}
    files: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, Mapping):
            files["<invalid-entry>"] = ""
            continue
        filename = item.get("filename")
        digests = item.get("digests")
        sha256 = digests.get("sha256") if isinstance(digests, Mapping) else None
        if not isinstance(filename, str) or not isinstance(sha256, str):
            files[str(filename)] = ""
            continue
        files[filename] = sha256
    return files


def _remote_file_urls_from_payload(
    payload: Mapping[str, Any] | None,
) -> Mapping[str, str]:
    if payload is None:
        return {}
    urls = payload.get("urls")
    if not isinstance(urls, list):
        return {}
    files: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, Mapping):
            continue
        filename = item.get("filename")
        url = item.get("url")
        if isinstance(filename, str) and isinstance(url, str) and url:
            files[filename] = url
    return files


def match_project_index(
    expectation: ProjectExpectation, payload: Mapping[str, Any] | None
) -> ProjectIndexMatch:
    actual = dict(_remote_files_from_payload(payload))
    expected = dict(expectation.files)
    if not actual:
        status: IndexStatus = "empty"
    elif actual == expected:
        status = "full"
    else:
        status = "divergent"
    return ProjectIndexMatch(
        project=expectation.project,
        version=expectation.version,
        status=status,
        expected=expected,
        actual=actual,
    )


def _index_matches(
    *,
    config: PublishConfig,
    expectations: Sequence[ProjectExpectation],
    index_client: IndexClient,
) -> tuple[ProjectIndexMatch, ...]:
    responses = _index_responses(
        config=config,
        expectations=expectations,
        index_client=index_client,
    )
    return _matches_from_responses(expectations=expectations, responses=responses)


def _index_responses(
    *,
    config: PublishConfig,
    expectations: Sequence[ProjectExpectation],
    index_client: IndexClient,
) -> Mapping[tuple[str, str], Mapping[str, Any] | None]:
    return index_client(config.index_base_url, expectations)


def _matches_from_responses(
    *,
    expectations: Sequence[ProjectExpectation],
    responses: Mapping[tuple[str, str], Mapping[str, Any] | None],
) -> tuple[ProjectIndexMatch, ...]:
    return tuple(
        match_project_index(
            expectation,
            responses.get((expectation.project, expectation.version)),
        )
        for expectation in expectations
    )


def _describe_index_matches(matches: Sequence[ProjectIndexMatch]) -> str:
    parts: list[str] = []
    for match in matches:
        parts.append(
            (
                f"{match.project}=={match.version} status={match.status} "
                f"expected={dict(sorted(match.expected.items()))} "
                f"actual={dict(sorted(match.actual.items()))}"
            )
        )
    return "; ".join(parts)


def _pre_upload_index_state(matches: Sequence[ProjectIndexMatch]) -> str:
    statuses = {match.status for match in matches}
    if statuses == {"empty"}:
        return "clean"
    if statuses == {"full"}:
        return "already_published"
    return "divergent"


def _assert_pre_upload_index_clean_or_published(
    matches: Sequence[ProjectIndexMatch],
) -> str:
    state = _pre_upload_index_state(matches)
    if state == "divergent":
        raise DriverError(
            [
                failure(
                    "release publish package index is divergent",
                    expected="empty project versions or exact retained file digests",
                    actual=_describe_index_matches(matches),
                    repair="audit the package index; cut the next version if bytes differ",
                )
            ]
        )
    return state


def _all_index_matches_full(matches: Sequence[ProjectIndexMatch]) -> bool:
    return all(match.status == "full" for match in matches)


def _redact_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "<redacted>")


def _run_twine_upload(
    config: PublishConfig,
    uploads: Sequence[ArtifactEntry],
    *,
    upload_runner: ProcessRunner,
) -> None:
    argv = [
        "twine",
        "upload",
        "--repository-url",
        config.repository_url,
        *(str(entry.path) for entry in uploads),
    ]
    env = dict(os.environ)
    env["TWINE_USERNAME"] = "__token__"
    env["TWINE_PASSWORD"] = config.pypi_token
    result = upload_runner(
        argv,
        cwd=config.root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        actual = (result.stderr or result.stdout or str(result.returncode)).strip()
        raise DriverError(
            [
                failure(
                    "release publish upload failed",
                    expected="twine upload exit 0",
                    actual=_redact_secret(actual, config.pypi_token),
                    repair=config.resume_target,
                )
            ]
        )


def _verify_uploaded_index(
    *,
    config: PublishConfig,
    expectations: Sequence[ProjectExpectation],
    index_client: IndexClient,
    sleep: Sleeper,
    max_attempts: int,
    sleep_seconds: float,
) -> None:
    last_matches: tuple[ProjectIndexMatch, ...] = ()
    for attempt in range(max_attempts):
        last_matches = _index_matches(
            config=config,
            expectations=expectations,
            index_client=index_client,
        )
        if _all_index_matches_full(last_matches):
            return
        if attempt < max_attempts - 1:
            sleep(sleep_seconds)
    raise DriverError(
        [
            failure(
                "release publish package index verification timed out",
                expected="all train retained files visible with matching SHA-256",
                actual=_describe_index_matches(last_matches),
                repair=config.resume_target,
            )
        ]
    )


def _models_package_version(config: PublishConfig) -> str:
    models = config.retained_ledger.get("models")
    version = models.get("package_version") if isinstance(models, Mapping) else None
    return str(version) if isinstance(version, str) else ""


def _model_key(config: PublishConfig) -> tuple[str, str]:
    return (MODEL_PROJECT, _models_package_version(config))


def _entry_by_name(entries: Sequence[ArtifactEntry]) -> Mapping[str, ArtifactEntry]:
    return {entry.name: entry for entry in entries}


def _resolve_publish_set(
    *,
    config: PublishConfig,
    classified: ClassifiedArtifacts,
    pre_upload_matches: Sequence[ProjectIndexMatch],
    pre_upload_responses: Mapping[tuple[str, str], Mapping[str, Any] | None],
    archive_downloader: ArchiveDownloader,
) -> tuple[ResolvedPublishSet, str]:
    full_expectations = classified.project_expectations
    full_set = ResolvedPublishSet(
        uploads=classified.uploads,
        verify_expectations=full_expectations,
        reused_project="",
        reused_files=(),
        reused_model_pin=None,
    )
    model_key = _model_key(config)
    model_matches = [
        match
        for match in pre_upload_matches
        if (match.project, match.version) == model_key
    ]
    if not model_matches or model_matches[0].status == "empty":
        return (
            full_set,
            _assert_pre_upload_index_clean_or_published(pre_upload_matches),
        )

    model_match = model_matches[0]
    _assert_model_file_set_exact(model_match)
    if dict(model_match.actual) != dict(model_match.expected):
        _download_and_compare_reused_models(
            classified=classified,
            model_match=model_match,
            payload=pre_upload_responses.get(model_key),
            archive_downloader=archive_downloader,
        )
    model_names = tuple(sorted(model_match.expected))
    train_matches = tuple(
        match
        for match in pre_upload_matches
        if (match.project, match.version) != model_key
    )
    train_state = _assert_pre_upload_index_clean_or_published(train_matches)
    train_uploads = tuple(
        entry
        for entry in classified.uploads
        if not (entry.project == MODEL_PROJECT and entry.version == model_key[1])
    )
    return (
        ResolvedPublishSet(
            uploads=train_uploads,
            verify_expectations=_project_expectations_for(train_uploads),
            reused_project=f"{model_match.project}=={model_match.version}",
            reused_files=model_names,
            reused_model_pin=ProjectExpectation(
                project=model_match.project,
                version=model_match.version,
                files=dict(model_match.actual),
            ),
        ),
        train_state,
    )


def _assert_model_file_set_exact(model_match: ProjectIndexMatch) -> None:
    expected = set(model_match.expected)
    actual = set(model_match.actual)
    if len(expected) != 2 or actual != expected:
        raise DriverError(
            [
                failure(
                    "release publish model project index file set is invalid",
                    expected=", ".join(sorted(expected)),
                    actual=", ".join(sorted(actual)) or "<empty>",
                    repair="audit the package index; cut the next version if the published set differs",
                )
            ]
        )


def _download_and_compare_reused_models(
    *,
    classified: ClassifiedArtifacts,
    model_match: ProjectIndexMatch,
    payload: Mapping[str, Any] | None,
    archive_downloader: ArchiveDownloader,
) -> None:
    urls = _remote_file_urls_from_payload(payload)
    entries = _entry_by_name(classified.uploads)
    missing_urls = sorted(name for name in model_match.expected if name not in urls)
    if missing_urls:
        raise DriverError(
            [
                failure(
                    "release publish model project index file URL is missing",
                    expected="download URL for each reused model archive",
                    actual=", ".join(missing_urls),
                    repair="retry after the package index exposes archive URLs",
                )
            ]
        )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name in sorted(model_match.expected):
            retained = entries[name]
            destination = tmp_path / name
            archive_downloader(urls[name], destination, retained.bytes * 2)
            downloaded_sha256 = _sha256_file(destination)
            expected_sha256 = model_match.actual[name]
            if downloaded_sha256 != expected_sha256:
                raise DriverError(
                    [
                        failure(
                            "release publish downloaded model archive digest mismatch",
                            expected=f"{name} {expected_sha256}",
                            actual=f"{downloaded_sha256} bytes={destination.stat().st_size}",
                            repair="retry after the package index files are consistent",
                        )
                    ]
                )
            assert_archives_semantically_identical(
                retained.path,
                destination,
                retained_label=f"retained {name}",
                published_label=f"published {name}",
            )


def _assert_reused_model_project_still_pinned(
    *,
    config: PublishConfig,
    pin: ProjectExpectation | None,
    index_client: IndexClient,
) -> None:
    if pin is None:
        return
    matches = _index_matches(
        config=config,
        expectations=(pin,),
        index_client=index_client,
    )
    if len(matches) == 1 and matches[0].status == "full":
        return
    raise DriverError(
        [
            failure(
                "release publish reused model index changed after train artifacts may already have uploaded",
                expected=str(dict(sorted(pin.files.items()))),
                actual=_describe_index_matches(matches),
                repair=config.resume_target,
            )
        ]
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(256 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _remote_tag_commit(
    config: PublishConfig, runner: ProcessRunner, tag_name: str
) -> str | None:
    result = runner(
        [
            "git",
            "ls-remote",
            "--tags",
            "origin",
            f"refs/tags/{tag_name}",
            f"refs/tags/{tag_name}^{{}}",
        ],
        cwd=config.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DriverError(
            [
                failure(
                    "release git remote tag check failed",
                    expected=f"readable origin refs/tags/{tag_name}",
                    actual=(
                        result.stderr or result.stdout or str(result.returncode)
                    ).strip(),
                    repair="retry after git remote access is healthy",
                )
            ]
        )
    direct: str | None = None
    peeled: str | None = None
    for line in result.stdout.splitlines():
        commit, _sep, ref = line.partition("\t")
        if ref == f"refs/tags/{tag_name}":
            direct = commit
        elif ref == f"refs/tags/{tag_name}^{{}}":
            peeled = commit
    return peeled or direct


def _local_tag_commit(
    config: PublishConfig, runner: ProcessRunner, tag_name: str
) -> str | None:
    result = runner(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag_name}^{{}}"],
        cwd=config.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _run_git_checked(
    config: PublishConfig,
    runner: ProcessRunner,
    argv: Sequence[str],
    *,
    error: str,
    expected: str,
    repair: str,
) -> None:
    result = runner(
        list(argv),
        cwd=config.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DriverError(
            [
                failure(
                    error,
                    expected=expected,
                    actual=(
                        result.stderr or result.stdout or str(result.returncode)
                    ).strip(),
                    repair=repair,
                )
            ]
        )


def publish_git_tag(config: PublishConfig, *, git_runner: ProcessRunner) -> str:
    tag_name = f"v{config.version}"
    remote_commit = _remote_tag_commit(config, git_runner, tag_name)
    if remote_commit is not None:
        if remote_commit == config.source_commit:
            LOG.info(
                "release tag %s already exists at retained source commit", tag_name
            )
            return "remote-already-correct"
        raise DriverError(
            [
                failure(
                    "release git tag points at a different commit",
                    expected=config.source_commit,
                    actual=remote_commit,
                    repair="cut the next version; a published tag is immutable",
                )
            ]
        )
    local_commit = _local_tag_commit(config, git_runner, tag_name)
    if local_commit is not None and local_commit != config.source_commit:
        raise DriverError(
            [
                failure(
                    "release local git tag points at a different commit",
                    expected=config.source_commit,
                    actual=local_commit,
                    repair=f"delete or repair local tag {tag_name} before publishing",
                )
            ]
        )
    if local_commit is None:
        _run_git_checked(
            config,
            git_runner,
            [
                "git",
                "tag",
                "-a",
                tag_name,
                config.source_commit,
                "-m",
                f"solstone {config.version}",
            ],
            error="release git tag creation failed",
            expected=f"annotated {tag_name} at {config.source_commit}",
            repair=config.resume_target,
        )
        tag_state = "created-and-pushed"
    else:
        tag_state = "pushed-existing-local"
    _run_git_checked(
        config,
        git_runner,
        ["git", "push", "origin", f"refs/tags/{tag_name}"],
        error="release git tag push failed",
        expected=f"pushed refs/tags/{tag_name}",
        repair=config.resume_target,
    )
    return tag_state


def record_github_release_witness(
    *,
    config: PublishConfig,
    classified: ClassifiedArtifacts,
    changelog_block: str,
    gh_runner: ProcessRunner,
) -> WitnessStatus:
    tag_name = f"v{config.version}"
    with tempfile.TemporaryDirectory() as tmp:
        notes_path = Path(tmp) / "release-notes.md"
        notes_path.write_text(changelog_block, encoding="utf-8")
        argv = [
            "gh",
            "release",
            "create",
            tag_name,
            "--title",
            f"solstone {config.version}",
            "--notes-file",
            str(notes_path),
            *(str(entry.path) for entry in classified.uploads),
        ]
        try:
            result = gh_runner(
                argv,
                cwd=config.root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return _witness_gap(config, tag_name, type(exc).__name__)
    if result.returncode == 0:
        return WitnessStatus(
            state="created",
            message=f"GitHub Release witness created for {tag_name}",
        )
    actual = (result.stderr or result.stdout or str(result.returncode)).strip()
    return _witness_gap(config, tag_name, actual)


def _witness_gap(config: PublishConfig, tag_name: str, actual: str) -> WitnessStatus:
    message = (
        f"GitHub Release witness failed for {tag_name}; rerun: {config.resume_target}"
    )
    LOG.error("%s\n  actual: %s", message, actual)
    return WitnessStatus(state="witness-gap", message=message)


def publish_release(
    *,
    config: PublishConfig,
    index_client: IndexClient = default_index_client,
    archive_downloader: ArchiveDownloader = default_archive_downloader,
    upload_runner: ProcessRunner = subprocess.run,
    git_runner: ProcessRunner = subprocess.run,
    gh_runner: ProcessRunner = subprocess.run,
    sleep: Sleeper = time.sleep,
    max_verify_attempts: int = DEFAULT_VERIFY_ATTEMPTS,
    verify_sleep_seconds: float = DEFAULT_VERIFY_SLEEP_SECONDS,
) -> PublishResult:
    report = _verify_recover(config)
    _assert_nvattest_authority_matches_checkout(config, report)
    if config.mode == "production":
        _ensure_source_commit_exists(config, git_runner)
        _verify_core_unsupported_tombstone_prerequisite(config)
    classified = classify_candidate_artifacts(config)
    changelog_block = ""
    if config.mode == "production":
        changelog_block = _read_changelog_block(config, git_runner)
    full_expectations = classified.project_expectations
    pre_upload_responses = _index_responses(
        config=config,
        expectations=full_expectations,
        index_client=index_client,
    )
    pre_upload = _matches_from_responses(
        expectations=full_expectations,
        responses=pre_upload_responses,
    )
    publish_set, index_state = _resolve_publish_set(
        config=config,
        classified=classified,
        pre_upload_matches=pre_upload,
        pre_upload_responses=pre_upload_responses,
        archive_downloader=archive_downloader,
    )
    if index_state == "clean":
        _run_twine_upload(config, publish_set.uploads, upload_runner=upload_runner)
        upload_state: UploadState = "uploaded"
    else:
        LOG.info("release artifacts are already published with retained digests")
        upload_state = "skipped-already-published"
    _verify_uploaded_index(
        config=config,
        expectations=publish_set.verify_expectations,
        index_client=index_client,
        sleep=sleep,
        max_attempts=max_verify_attempts,
        sleep_seconds=verify_sleep_seconds,
    )
    _assert_reused_model_project_still_pinned(
        config=config,
        pin=publish_set.reused_model_pin,
        index_client=index_client,
    )
    if config.mode == "test":
        tag_state = "test-skipped"
        witness = WitnessStatus(
            state="test-skipped",
            message="test mode does not tag or create a GitHub Release witness",
        )
    else:
        tag_state = publish_git_tag(config, git_runner=git_runner)
        witness = record_github_release_witness(
            config=config,
            classified=classified,
            changelog_block=changelog_block,
            gh_runner=gh_runner,
        )
    return PublishResult(
        mode=config.mode,
        version=config.version,
        source_commit=config.source_commit,
        upload_state=upload_state,
        verified=True,
        tag_state=tag_state,
        witness_status=witness,
        uploaded_files=tuple(entry.name for entry in publish_set.uploads),
        projects=tuple(
            f"{project.project}=={project.version}"
            for project in publish_set.verify_expectations
        ),
        reused_project=publish_set.reused_project,
        reused_files=publish_set.reused_files,
    )


def _config_from_args(
    args: argparse.Namespace, env: Mapping[str, str]
) -> PublishConfig:
    return PublishConfig.from_env(
        root=Path(args.root).resolve(),
        mode=args.mode,
        env=env,
    )


def _print_failures(error: DriverError) -> None:
    for item in error.failures:
        _print_failure(item)


def _print_failure(item: Failure) -> None:
    LOG.error(
        "%s\n  expected: %s\n  actual: %s\n  repair: %s",
        item.error,
        item.expected,
        item.actual,
        item.repair,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Publish an already-finalized retained solstone release candidate. "
            "Test mode is a TestPyPI upload+verify rehearsal only; it does not "
            "validate changelog or tag readiness."
        )
    )
    parser.add_argument("--mode", choices=("production", "test"), required=True)
    parser.add_argument("--root", default=".")
    return parser


def main(
    argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    runtime_env = dict(os.environ if env is None else env)
    try:
        config = _config_from_args(args, runtime_env)
        result = publish_release(config=config)
    except DriverError as exc:
        _print_failures(exc)
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
