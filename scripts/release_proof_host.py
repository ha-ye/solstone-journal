#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider-neutral proof-host channels for release candidate install proofs."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.check_rust_release_manifest import (
    SHA256_RE,
    SOURCE_COMMIT_RE,
    Failure,
    canonical_json_bytes,
)
from scripts.release_digest import file_sha256_size
from scripts.release_install_smoke import (
    PROOF_TARGETS,
    candidate_file_entries,
    target_install_paths_from_ledger,
    validate_install_proof_bytes,
)
from scripts.release_public_evidence import validate_public_evidence_tree

Runner = Callable[..., subprocess.CompletedProcess[str]]
IdFactory = Callable[[], str]
FileCopier = Callable[[Path, Path], object]

TARGET_POLICY: Mapping[str, tuple[str, str]] = {
    "linux-x86_64-musl": ("Linux", "x86_64"),
    "linux-aarch64-musl": ("Linux", "aarch64"),
    "macos-arm64": ("Darwin", "arm64"),
}
TARGET_ENV_KEYS: Mapping[str, str] = {
    "linux-x86_64-musl": "RELEASE_PROOF_HOST_LINUX_X86_64_MUSL_CHANNEL",
    "linux-aarch64-musl": "RELEASE_PROOF_HOST_LINUX_AARCH64_MUSL_CHANNEL",
    "macos-arm64": "RELEASE_PROOF_HOST_MACOS_ARM64_CHANNEL",
}
REQUEST_KEYS = frozenset(
    (
        "schema_version",
        "cohort_id",
        "target",
        "version",
        "source_commit",
        "candidate_digest",
        "ledger_sha256",
        "core_lock_sha256",
        "candidate_files",
        "paths",
        "expected_host",
    )
)
REQUEST_FILE_KEYS = frozenset(("basename", "bytes", "sha256", "path"))
REQUEST_PATH_KEYS = frozenset(("candidate_dir", "ledger", "response", "proof"))
REQUEST_HOST_KEYS = frozenset(("os", "arch"))
RESPONSE_KEYS = frozenset(("schema_version", "cohort_id", "attestation", "proof"))
ATTESTATION_KEYS = frozenset(("os", "arch", "candidate_digest", "ledger_sha256"))
PROOF_FILE_KEYS = frozenset(("path", "sha256", "bytes"))


@dataclass(frozen=True)
class DirectoryIdentity:
    path: Path
    label: str
    st_dev: int
    st_ino: int
    mode: int


class ProofHostChannel(Protocol):
    def run_install_proof(
        self,
        *,
        target: str,
        version: str,
        source_commit: str,
        core_lock_sha256: str,
        candidate_digest: str,
        ledger_sha256: str,
        candidate_dir: Path,
        candidate_paths: Sequence[Path],
        ledger_payload: Mapping[str, Any],
        output_path: Path,
    ) -> Path: ...


class ProofHostError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _run(
    runner: Runner,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _key_set_failures(
    label: str,
    payload: Mapping[str, object],
    expected: frozenset[str],
) -> list[Failure]:
    actual = frozenset(str(key) for key in payload)
    if actual == expected:
        return []
    return [
        _failure(
            f"{label} key set is invalid",
            expected=", ".join(sorted(expected)),
            actual=", ".join(sorted(actual)) or "<empty>",
            repair="bash scripts/release.sh --candidate",
        )
    ]


def _validate_cohort_id(cohort_id: str) -> None:
    if (
        len(cohort_id) != 32
        or cohort_id.lower() != cohort_id
        or any(char not in "0123456789abcdef" for char in cohort_id)
    ):
        raise ProofHostError(
            [
                _failure(
                    "proof-host cohort id is invalid",
                    expected="32 lowercase hexadecimal characters",
                    actual=cohort_id,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )


def _safe_basename(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if not value or value in {".", ".."}:
        return None
    if Path(value).name != value or "/" in value or "\\" in value:
        return None
    return value


def _capture_directory_identity(path: Path, *, label: str) -> DirectoryIdentity:
    try:
        current = path.lstat()
    except OSError as exc:
        raise ProofHostError(
            [
                _failure(
                    f"proof-host {label} directory identity could not be captured",
                    expected="owned non-symlink directory",
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        ) from None
    if stat.S_ISLNK(current.st_mode):
        actual = "symlink"
    elif not stat.S_ISDIR(current.st_mode):
        actual = "non-directory"
    else:
        return DirectoryIdentity(
            path=path,
            label=label,
            st_dev=current.st_dev,
            st_ino=current.st_ino,
            mode=current.st_mode,
        )
    raise ProofHostError(
        [
            _failure(
                f"proof-host {label} directory identity could not be captured",
                expected="owned non-symlink directory",
                actual=actual,
                repair="bash scripts/release.sh --candidate",
            )
        ]
    )


def _directory_identity_failure(identity: DirectoryIdentity) -> Failure | None:
    try:
        current = identity.path.lstat()
    except OSError as exc:
        return _failure(
            f"proof-host {identity.label} directory identity changed",
            expected="same owned non-symlink directory",
            actual=type(exc).__name__,
            repair="bash scripts/release.sh --candidate",
        )
    if stat.S_ISLNK(current.st_mode):
        actual = "symlink"
    elif not stat.S_ISDIR(current.st_mode):
        actual = "non-directory"
    elif (
        current.st_dev != identity.st_dev
        or current.st_ino != identity.st_ino
        or current.st_mode != identity.mode
    ):
        actual = "different directory"
    else:
        return None
    return _failure(
        f"proof-host {identity.label} directory identity changed",
        expected="same owned non-symlink directory",
        actual=actual,
        repair="bash scripts/release.sh --candidate",
    )


def _validate_directory_identities(
    identities: Sequence[DirectoryIdentity | None],
) -> None:
    failures = [
        failure
        for identity in identities
        if identity is not None
        if (failure := _directory_identity_failure(identity)) is not None
    ]
    if failures:
        raise ProofHostError(failures)


def _validate_regular_file(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ProofHostError(
            [
                _failure(
                    f"proof-host {label} is not a regular file",
                    expected="regular non-symlink file",
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        ) from None
    if stat.S_ISLNK(mode):
        actual = "symlink"
    elif not stat.S_ISREG(mode):
        actual = "non-regular"
    else:
        return
    raise ProofHostError(
        [
            _failure(
                f"proof-host {label} is not a regular file",
                expected="regular non-symlink file",
                actual=actual,
                repair="bash scripts/release.sh --candidate",
            )
        ]
    )


def _validate_fresh_directory_path(path: Path, *, label: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink():
        actual = "symlink"
    elif not path.is_dir():
        actual = "non-directory"
    elif any(path.iterdir()):
        actual = "pre-existing entries"
    else:
        actual = "empty directory"
    raise ProofHostError(
        [
            _failure(
                f"proof-host {label} directory is unsafe",
                expected="fresh directory path",
                actual=actual,
                repair="bash scripts/release.sh --candidate",
            )
        ]
    )


def _combine_failures(
    primary: ProofHostError | None, cleanup: ProofHostError | None
) -> ProofHostError | None:
    failures: list[Failure] = []
    if primary is not None:
        failures.extend(primary.failures)
    if cleanup is not None:
        failures.extend(cleanup.failures)
    return ProofHostError(failures) if failures else None


def _validate_scalars(
    *,
    target: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
) -> None:
    failures: list[Failure] = []
    if target not in TARGET_POLICY:
        failures.append(
            _failure(
                "proof-host target is invalid",
                expected=", ".join(PROOF_TARGETS),
                actual=target,
                repair="bash scripts/release.sh --candidate",
            )
        )
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        failures.append(
            _failure(
                "proof-host source commit is invalid",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=source_commit,
                repair="bash scripts/release.sh --candidate",
            )
        )
    for label, value in (
        ("core lock", core_lock_sha256),
        ("candidate digest", candidate_digest),
        ("ledger sha256", ledger_sha256),
    ):
        if not SHA256_RE.fullmatch(value):
            failures.append(
                _failure(
                    f"proof-host {label} is invalid",
                    expected="lowercase SHA-256",
                    actual=value,
                    repair="bash scripts/release.sh --candidate",
                )
            )
    if failures:
        raise ProofHostError(failures)


def _json_object(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProofHostError(
            [
                _failure(
                    "proof-host response is not valid JSON",
                    expected="JSON object",
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        ) from None
    if not isinstance(payload, Mapping):
        raise ProofHostError(
            [
                _failure(
                    "proof-host response is not an object",
                    expected="JSON object",
                    actual=type(payload).__name__,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    return payload


class ExternalProofHostChannel:
    """External command adapter; command and credentials live outside source."""

    def __init__(
        self,
        target: str,
        command: Sequence[str],
        *,
        runner: Runner = subprocess.run,
        cohort_id_factory: IdFactory | None = None,
        file_copier: FileCopier = shutil.copyfile,
    ) -> None:
        if target not in TARGET_POLICY:
            raise ValueError("proof target is invalid")
        if not command:
            raise ValueError("proof-host command is required")
        self._target = target
        self._command = tuple(command)
        self._runner = runner
        self._cohort_id_factory = cohort_id_factory or (lambda: uuid.uuid4().hex)
        self._file_copier = file_copier

    @classmethod
    def from_env(
        cls,
        target: str,
        env: Mapping[str, str],
        *,
        runner: Runner = subprocess.run,
        cohort_id_factory: IdFactory | None = None,
        file_copier: FileCopier = shutil.copyfile,
    ) -> ExternalProofHostChannel:
        env_key = TARGET_ENV_KEYS[target]
        try:
            command = shlex.split(env.get(env_key, ""))
        except ValueError as exc:
            raise ProofHostError(
                [
                    _failure(
                        "proof-host channel configuration is invalid",
                        expected="shell-style command tokens",
                        actual=str(exc),
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            ) from None
        if not command:
            raise ProofHostError(
                [
                    _failure(
                        "proof-host channel is not configured",
                        expected=f"{env_key} command",
                        actual="<missing>",
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            )
        return cls(
            target,
            command,
            runner=runner,
            cohort_id_factory=cohort_id_factory,
            file_copier=file_copier,
        )

    def _cleanup_remote(self, *, cohort_id: str, ledger_sha256: str) -> None:
        try:
            result = _run(
                self._runner,
                [*self._command, "cleanup", cohort_id, ledger_sha256],
            )
        except BaseException as exc:
            raise ProofHostError(
                [
                    _failure(
                        "proof-host remote cleanup failed",
                        expected="external proof-host cleanup completed",
                        actual=type(exc).__name__,
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            ) from None
        if result.returncode != 0:
            raise ProofHostError(
                [
                    _failure(
                        "proof-host remote cleanup failed",
                        expected="external proof-host cleanup exit 0",
                        actual=result.stderr.strip()
                        or result.stdout.strip()
                        or f"exit {result.returncode}",
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            )

    def _cleanup_local(
        self,
        targets: Sequence[tuple[Path, DirectoryIdentity | None]],
    ) -> ProofHostError | None:
        failures: list[Failure] = []
        preserved: list[Path] = []
        for path, identity in targets:
            try:
                if any(path in preserved_path.parents for preserved_path in preserved):
                    failures.append(
                        _failure(
                            "proof-host local cleanup failed",
                            expected="owned proof-host transients removed",
                            actual="preserved descendant residue",
                            repair="bash scripts/release.sh --candidate",
                        )
                    )
                    continue
                if identity is not None:
                    identity_failure = _directory_identity_failure(identity)
                    if identity_failure is not None:
                        preserved.append(path)
                        failures.append(
                            _failure(
                                "proof-host local cleanup failed",
                                expected="owned proof-host transients removed",
                                actual=f"{identity.label} residue",
                                repair="bash scripts/release.sh --candidate",
                            )
                        )
                        continue
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
            except BaseException as exc:
                preserved.append(path)
                failures.append(
                    _failure(
                        "proof-host local cleanup failed",
                        expected="owned proof-host transients removed",
                        actual=type(exc).__name__,
                        repair="bash scripts/release.sh --candidate",
                    )
                )
        return ProofHostError(failures) if failures else None

    def _request_payload(
        self,
        *,
        cohort_id: str,
        target: str,
        version: str,
        source_commit: str,
        core_lock_sha256: str,
        candidate_digest: str,
        ledger_sha256: str,
        install_paths: Sequence[Path],
    ) -> dict[str, object]:
        os_name, arch = TARGET_POLICY[target]
        files = [
            {
                **entry,
                "path": f"candidate/{entry['basename']}",
            }
            for entry in candidate_file_entries(install_paths)
        ]
        payload: dict[str, object] = {
            "schema_version": 1,
            "cohort_id": cohort_id,
            "target": target,
            "version": version,
            "source_commit": source_commit,
            "candidate_digest": candidate_digest,
            "ledger_sha256": ledger_sha256,
            "core_lock_sha256": core_lock_sha256,
            "candidate_files": files,
            "paths": {
                "candidate_dir": "candidate",
                "ledger": "ledger.json",
                "response": "response.json",
                "proof": "output/proof.json",
            },
            "expected_host": {"os": os_name, "arch": arch},
        }
        if set(payload) != REQUEST_KEYS:
            raise AssertionError("proof-host request key set drifted")
        for entry in files:
            if set(entry) != REQUEST_FILE_KEYS:
                raise AssertionError("proof-host request file key set drifted")
        if set(payload["paths"]) != REQUEST_PATH_KEYS:  # type: ignore[arg-type]
            raise AssertionError("proof-host request path key set drifted")
        if set(payload["expected_host"]) != REQUEST_HOST_KEYS:  # type: ignore[arg-type]
            raise AssertionError("proof-host request host key set drifted")
        return payload

    def _validate_response(
        self,
        payload: Mapping[str, object],
        *,
        cohort_id: str,
        target: str,
        candidate_digest: str,
        ledger_sha256: str,
    ) -> Mapping[str, object]:
        failures = _key_set_failures("proof-host response", payload, RESPONSE_KEYS)
        if failures:
            raise ProofHostError(failures)
        if payload.get("schema_version") != 1:
            raise ProofHostError(
                [
                    _failure(
                        "proof-host response schema version is wrong",
                        expected="1",
                        actual=repr(payload.get("schema_version")),
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            )
        if payload.get("cohort_id") != cohort_id:
            raise ProofHostError(
                [
                    _failure(
                        "proof-host response cohort id is wrong",
                        expected=cohort_id,
                        actual=repr(payload.get("cohort_id")),
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            )
        attestation = payload.get("attestation")
        if not isinstance(attestation, Mapping):
            raise ProofHostError(
                [
                    _failure(
                        "proof-host response attestation is invalid",
                        expected="attestation object",
                        actual=type(attestation).__name__,
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            )
        failures = _key_set_failures(
            "proof-host response attestation",
            attestation,
            ATTESTATION_KEYS,
        )
        if failures:
            raise ProofHostError(failures)
        expected_os, expected_arch = TARGET_POLICY[target]
        expected = {
            "os": expected_os,
            "arch": expected_arch,
            "candidate_digest": candidate_digest,
            "ledger_sha256": ledger_sha256,
        }
        failures = [
            _failure(
                f"proof-host response attestation {key} is wrong",
                expected=repr(expected_value),
                actual=repr(attestation.get(key)),
                repair="bash scripts/release.sh --candidate",
            )
            for key, expected_value in expected.items()
            if attestation.get(key) != expected_value
        ]
        if failures:
            raise ProofHostError(failures)
        proof = payload.get("proof")
        if not isinstance(proof, Mapping):
            raise ProofHostError(
                [
                    _failure(
                        "proof-host response proof descriptor is invalid",
                        expected="proof file descriptor",
                        actual=type(proof).__name__,
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            )
        failures = _key_set_failures(
            "proof-host response proof descriptor",
            proof,
            PROOF_FILE_KEYS,
        )
        if failures:
            raise ProofHostError(failures)
        return proof

    def run_install_proof(
        self,
        *,
        target: str,
        version: str,
        source_commit: str,
        core_lock_sha256: str,
        candidate_digest: str,
        ledger_sha256: str,
        candidate_dir: Path,
        candidate_paths: Sequence[Path],
        ledger_payload: Mapping[str, Any],
        output_path: Path,
    ) -> Path:
        if target != self._target:
            raise ProofHostError(
                [
                    _failure(
                        "proof-host channel target mismatch",
                        expected=self._target,
                        actual=target,
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            )
        _ = candidate_paths
        _validate_scalars(
            target=target,
            source_commit=source_commit,
            core_lock_sha256=core_lock_sha256,
            candidate_digest=candidate_digest,
            ledger_sha256=ledger_sha256,
        )
        install_paths = target_install_paths_from_ledger(
            ledger_payload,
            target=target,
            candidate_dir=candidate_dir,
        )
        cohort_id = self._cohort_id_factory()
        _validate_cohort_id(cohort_id)
        request_dir = output_path.parent / f".{target}.proof-request-{cohort_id}"
        request_candidate_dir = request_dir / "candidate"
        request_output_dir = request_dir / "output"
        response_path = request_dir / "response.json"
        request_path = request_dir / "request.json"
        proof_path = request_dir / "output" / "proof.json"
        request_identity: DirectoryIdentity | None = None
        candidate_identity: DirectoryIdentity | None = None
        output_identity: DirectoryIdentity | None = None
        remote_started = False
        primary: ProofHostError | None = None
        completed = False
        try:
            _validate_fresh_directory_path(request_dir, label="request")
            if output_path.exists() or output_path.is_symlink():
                raise ProofHostError(
                    [
                        _failure(
                            "proof-host output proof already exists",
                            expected="fresh proof output path",
                            actual=output_path.name,
                            repair="bash scripts/release.sh --candidate",
                        )
                    ]
                )
            request_candidate_dir.mkdir(parents=True)
            request_output_dir.mkdir()
            request_identity = _capture_directory_identity(request_dir, label="request")
            candidate_identity = _capture_directory_identity(
                request_candidate_dir, label="candidate"
            )
            output_identity = _capture_directory_identity(
                request_output_dir, label="output"
            )
            for path in install_paths:
                if _safe_basename(path.name) is None:
                    raise ProofHostError(
                        [
                            _failure(
                                "proof-host candidate filename is unsafe",
                                expected="safe candidate wheel basename",
                                actual=path.name,
                                repair="bash scripts/release.sh --candidate",
                            )
                        ]
                    )
                _validate_regular_file(path, label="candidate wheel")
                source_sha256, source_bytes = file_sha256_size(path)
                _validate_directory_identities(
                    (request_identity, candidate_identity, output_identity)
                )
                self._file_copier(path, request_candidate_dir / path.name)
                _validate_directory_identities(
                    (request_identity, candidate_identity, output_identity)
                )
                _validate_regular_file(
                    request_candidate_dir / path.name,
                    label="request candidate wheel",
                )
                copied_sha256, copied_bytes = file_sha256_size(
                    request_candidate_dir / path.name
                )
                _validate_directory_identities(
                    (request_identity, candidate_identity, output_identity)
                )
                if copied_sha256 != source_sha256 or copied_bytes != source_bytes:
                    raise ProofHostError(
                        [
                            _failure(
                                "proof-host copied candidate wheel changed bytes",
                                expected=f"{source_sha256}/{source_bytes}",
                                actual=f"{copied_sha256}/{copied_bytes}",
                                repair="bash scripts/release.sh --candidate",
                            )
                        ]
                    )
            (request_dir / "ledger.json").write_bytes(
                canonical_json_bytes(ledger_payload)
            )
            request_payload = self._request_payload(
                cohort_id=cohort_id,
                target=target,
                version=version,
                source_commit=source_commit,
                core_lock_sha256=core_lock_sha256,
                candidate_digest=candidate_digest,
                ledger_sha256=ledger_sha256,
                install_paths=install_paths,
            )
            request_path.write_bytes(canonical_json_bytes(request_payload))
            public_failures = validate_public_evidence_tree(
                "proof_host.request",
                request_payload,
            )
            if public_failures:
                raise ProofHostError(public_failures)
            remote_started = True
            result = _run(
                self._runner,
                [*self._command, "prove", "request.json"],
                cwd=request_dir,
            )
            if result.returncode != 0:
                raise ProofHostError(
                    [
                        _failure(
                            "proof-host proof command failed",
                            expected="external proof-host command exit 0",
                            actual=result.stderr.strip()
                            or result.stdout.strip()
                            or f"exit {result.returncode}",
                            repair="bash scripts/release.sh --candidate",
                        )
                    ]
                )
            _validate_directory_identities(
                (request_identity, candidate_identity, output_identity)
            )
            _validate_regular_file(response_path, label="response")
            response = _json_object(response_path)
            proof_descriptor = self._validate_response(
                response,
                cohort_id=cohort_id,
                target=target,
                candidate_digest=candidate_digest,
                ledger_sha256=ledger_sha256,
            )
            proof_name = proof_descriptor.get("path")
            if proof_name != "output/proof.json":
                raise ProofHostError(
                    [
                        _failure(
                            "proof-host proof path is invalid",
                            expected="output/proof.json",
                            actual=repr(proof_name),
                            repair="bash scripts/release.sh --candidate",
                        )
                    ]
                )
            _validate_directory_identities(
                (request_identity, candidate_identity, output_identity)
            )
            _validate_regular_file(proof_path, label="proof")
            proof_sha256, proof_bytes_count = file_sha256_size(proof_path)
            if proof_descriptor.get("sha256") != proof_sha256:
                raise ProofHostError(
                    [
                        _failure(
                            "proof-host proof SHA-256 is wrong",
                            expected=proof_sha256,
                            actual=repr(proof_descriptor.get("sha256")),
                            repair="bash scripts/release.sh --candidate",
                        )
                    ]
                )
            if proof_descriptor.get("bytes") != proof_bytes_count:
                raise ProofHostError(
                    [
                        _failure(
                            "proof-host proof byte count is wrong",
                            expected=str(proof_bytes_count),
                            actual=repr(proof_descriptor.get("bytes")),
                            repair="bash scripts/release.sh --candidate",
                        )
                    ]
                )
            _validate_directory_identities(
                (request_identity, candidate_identity, output_identity)
            )
            proof_bytes = proof_path.read_bytes()
            _validate_directory_identities(
                (request_identity, candidate_identity, output_identity)
            )
            proof_failures = validate_install_proof_bytes(
                proof_bytes,
                target=target,
                version=version,
                source_commit=source_commit,
                core_lock_sha256=core_lock_sha256,
                candidate_digest=candidate_digest,
                ledger_sha256=ledger_sha256,
                candidate_dir=candidate_dir,
                ledger_payload=ledger_payload,
            )
            if proof_failures:
                raise ProofHostError(proof_failures)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = output_path.with_name(f".{output_path.name}.tmp")
            try:
                temp_path.write_bytes(proof_bytes)
                os.rename(temp_path, output_path)
            finally:
                temp_path.unlink(missing_ok=True)
            completed = True
            return output_path
        except BaseException as exc:
            if isinstance(exc, ProofHostError):
                primary = exc
            else:
                primary = ProofHostError(
                    [
                        _failure(
                            "proof-host operation failed",
                            expected="proof-host request completed",
                            actual=type(exc).__name__,
                            repair="bash scripts/release.sh --candidate",
                        )
                    ]
                )
            raise primary
        finally:
            cleanup_error: ProofHostError | None = None
            if remote_started:
                try:
                    self._cleanup_remote(
                        cohort_id=cohort_id,
                        ledger_sha256=ledger_sha256,
                    )
                except ProofHostError as exc:
                    cleanup_error = exc
            local_cleanup = self._cleanup_local(
                (
                    (request_candidate_dir, candidate_identity),
                    (request_output_dir, output_identity),
                    (request_dir, request_identity),
                )
            )
            cleanup_error = _combine_failures(cleanup_error, local_cleanup)
            if cleanup_error is not None:
                combined = _combine_failures(primary, cleanup_error)
                assert combined is not None
                if completed:
                    raise combined
                raise combined


def proof_channels_from_env(
    env: Mapping[str, str],
    *,
    runner: Runner = subprocess.run,
) -> dict[str, ProofHostChannel]:
    return {
        target: ExternalProofHostChannel.from_env(target, env, runner=runner)
        for target in PROOF_TARGETS
    }


def run_install_proof_with_channels(
    channels: Mapping[str, ProofHostChannel],
    **kwargs: Any,
) -> Path:
    target = str(kwargs.get("target", ""))
    channel = channels.get(target)
    if channel is None:
        raise ProofHostError(
            [
                _failure(
                    "proof-host channel is missing",
                    expected=f"{target} configured proof-host channel",
                    actual="<missing>",
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    return channel.run_install_proof(**kwargs)
