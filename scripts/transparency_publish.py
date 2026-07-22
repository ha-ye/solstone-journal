#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Publish solstone release transparency ledger entries."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_candidate_driver import DriverError
from scripts.transparency_core import (
    DEFAULT_BASE_URL,
    ENTRY_OBJECT_NAME,
    ENTRY_SIGNATURE_NAME,
    HEAD_LOG,
    LATEST_OBJECT_NAME,
    LATEST_SIGNATURE_NAME,
    LEDGER_OBJECT_NAME,
    PRODUCT,
    PUBLIC_TRUST_ANCHOR_PATH,
    STAGING_ROOT,
    ZERO_SHA256,
    EntryRecord,
    LatestRecord,
    atomic_write,
    build_latest_pointer,
    build_ledger_entry,
    canonical_json_bytes,
    collect_candidate_parts,
    encoded_stage_key,
    entry_trusted_comment,
    fail_closed,
    failure,
    format_published_utc,
    latest_key,
    latest_signature_key,
    latest_trusted_comment,
    ledger_jsonl_bytes,
    ledger_key,
    parse_latest_bytes,
    parse_ledger_entry_bytes,
    parse_ledger_jsonl,
    parse_published_utc,
    plus_14_days,
    public_url,
    recover_candidate,
    sha256_bytes,
    snapshot_candidate,
    validate_entry_chain,
    validate_entry_trusted_comment,
    validate_latest_trusted_comment,
    version_object_key,
    version_prefix,
)
from scripts.transparency_head_log import (
    HeadLogRow,
    WitnessStatus,
    append_head_row,
    git_witness_status,
    highest_seq,
)
from scripts.transparency_signing import (
    LocalMinisignSigner,
    TransparencySigner,
)
from scripts.transparency_transport import (
    CurlTransparencyTransport,
    HttpResult,
    TransparencyTransport,
)

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
MUTABLE_CACHE = "no-cache"
JSON_CONTENT_TYPE = "application/json"
SIG_CONTENT_TYPE = "application/octet-stream"
TEXT_CONTENT_TYPE = "application/jsonl"
PAYLOAD_DIR_NAME = "payload"
ARTIFACT_DIR_NAME = "artifacts"
STAGING_MANIFEST_NAME = "staging-manifest.txt"


ArchiveRunner = Callable[[Path, str], str]
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishConfig:
    root: Path
    version: str
    source_commit: str
    base_url: str
    s3_endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    minisign_key: Path
    minisign_pub: Path
    archive_channel: str
    genesis: str | None
    product: str = PRODUCT

    @classmethod
    def from_env(
        cls,
        *,
        root: Path,
        version: str,
        source_commit: str,
        env: Mapping[str, str],
    ) -> PublishConfig:
        missing = [
            name
            for name in (
                "TRANSPARENCY_S3_ENDPOINT",
                "TRANSPARENCY_BUCKET",
                "TRANSPARENCY_S3_ACCESS_KEY_ID",
                "TRANSPARENCY_S3_SECRET_ACCESS_KEY",
                "TRANSPARENCY_MINISIGN_KEY",
                "TRANSPARENCY_MINISIGN_PUB",
                "TRANSPARENCY_ARCHIVE_CHANNEL",
            )
            if not env.get(name)
        ]
        if missing:
            raise DriverError(
                [
                    failure(
                        "transparency publish environment is incomplete",
                        expected=", ".join(missing),
                        actual="missing",
                        repair="set the required TRANSPARENCY_* environment variables and retry",
                    )
                ]
            )
        return cls(
            root=root,
            version=version,
            source_commit=source_commit,
            base_url=env.get("TRANSPARENCY_BASE_URL", DEFAULT_BASE_URL),
            s3_endpoint=env["TRANSPARENCY_S3_ENDPOINT"],
            bucket=env["TRANSPARENCY_BUCKET"],
            access_key_id=env["TRANSPARENCY_S3_ACCESS_KEY_ID"],
            secret_access_key=env["TRANSPARENCY_S3_SECRET_ACCESS_KEY"],
            minisign_key=Path(env["TRANSPARENCY_MINISIGN_KEY"]),
            minisign_pub=Path(env["TRANSPARENCY_MINISIGN_PUB"]),
            archive_channel=env["TRANSPARENCY_ARCHIVE_CHANNEL"],
            genesis=env.get("TRANSPARENCY_GENESIS"),
        )


@dataclass(frozen=True)
class ChainState:
    entries: tuple[EntryRecord, ...]
    pointer: LatestRecord | None
    pointer_signature: bytes | None
    pointer_signature_etag: str | None
    next_seq: int
    prev_sha256: str
    prev_version: str
    tip_published_utc: str | None
    derived_ledger_jsonl: bytes

    @property
    def is_genesis(self) -> bool:
        return self.pointer is None


@dataclass(frozen=True)
class StagedPublish:
    path: Path
    product: str
    version: str
    seq: int
    source_commit: str
    entry_sha256: str
    published_utc: str
    valid_until: str
    staging_manifest_sha256: str

    @property
    def payload_dir(self) -> Path:
        return self.path / PAYLOAD_DIR_NAME

    @property
    def version_dir(self) -> Path:
        return self.payload_dir / "version-dir"

    @property
    def ledger_jsonl_path(self) -> Path:
        return self.payload_dir / LEDGER_OBJECT_NAME

    @property
    def latest_path(self) -> Path:
        return self.payload_dir / LATEST_OBJECT_NAME

    @property
    def latest_signature_path(self) -> Path:
        return self.payload_dir / LATEST_SIGNATURE_NAME

    @property
    def entry_path(self) -> Path:
        return self.version_dir / ENTRY_OBJECT_NAME

    @property
    def entry_signature_path(self) -> Path:
        return self.version_dir / ENTRY_SIGNATURE_NAME

    def immutable_files(self) -> tuple[tuple[str, Path], ...]:
        ordered_names = [ENTRY_OBJECT_NAME, ENTRY_SIGNATURE_NAME]
        ordered_names.extend(
            sorted(
                path.name
                for path in self.version_dir.iterdir()
                if path.is_file() and path.name not in set(ordered_names)
            )
        )
        return tuple((name, self.version_dir / name) for name in ordered_names)


@dataclass(frozen=True)
class PublishResult:
    product: str
    version: str
    seq: int
    entry_sha256: str
    public_urls: tuple[str, ...]
    archive_receipt_sha256: str
    witness_status: WitnessStatus
    elapsed_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "archive_receipt_sha256": self.archive_receipt_sha256,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "entry_sha256": self.entry_sha256,
            "product": self.product,
            "public_urls": list(self.public_urls),
            "seq": self.seq,
            "version": self.version,
            "witness_status": self.witness_status.state,
            "witness_message": self.witness_status.message,
        }


def _verify_bytes(
    signer: TransparencySigner,
    body: bytes,
    signature: bytes,
    *,
    expected_trusted_comment: str,
) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        message_path = tmp_path / "message"
        signature_path = tmp_path / "message.minisig"
        message_path.write_bytes(body)
        signature_path.write_bytes(signature)
        signer.verify_file(
            message_path,
            signature_path,
            expected_trusted_comment=expected_trusted_comment,
        )
        return signer.trusted_comment(signature_path)


def _extract_version_from_entry_key(product: str, key: str) -> str | None:
    prefix = f"releases/{product}/v/"
    suffix = f"/{ENTRY_OBJECT_NAME}"
    if not key.startswith(prefix) or not key.endswith(suffix):
        return None
    version = key[len(prefix) : -len(suffix)]
    return version or None


def _immutable_entry_keys(
    product: str, keys: Sequence[str]
) -> tuple[tuple[str, str], ...]:
    versions: list[tuple[str, str]] = []
    for key in keys:
        version = _extract_version_from_entry_key(product, key)
        if version is not None:
            versions.append((version, key))
    return tuple(sorted(versions, key=lambda item: item[0]))


def _raise_http_failure(
    error: str,
    *,
    key: str,
    result: HttpResult,
    retryable: bool,
) -> None:
    repair = (
        "retry after confirming the remote state is unchanged"
        if retryable
        else "stop and audit the transparency state before retrying"
    )
    raise DriverError(
        [
            failure(
                error,
                expected=f"successful HTTP status for {key}",
                actual=(
                    f"status={result.status} exit_code={result.exit_code} "
                    f"body={result.body.decode('utf-8', errors='replace')}"
                ),
                repair=repair,
            )
        ]
    )


def _require_etag(result: HttpResult, *, key: str, label: str) -> str:
    if result.etag:
        return result.etag
    fail_closed(
        f"transparency {label} ETag is missing",
        expected=f"{key} ETag",
        actual=f"status={result.status} exit_code={result.exit_code}",
        repair="stop and audit the object store response headers before retrying",
    )
    raise AssertionError("unreachable")


def _verify_remote_entry(
    *,
    product: str,
    version: str,
    transport: TransparencyTransport,
    signer: TransparencySigner,
) -> EntryRecord:
    key = version_object_key(product, version, ENTRY_OBJECT_NAME)
    result = transport.get_object(key, cache_bypass=True)
    if result.status != 200:
        _raise_http_failure(
            "transparency locked entry could not be fetched",
            key=key,
            result=result,
            retryable=True,
        )
    record = parse_ledger_entry_bytes(result.body, label=f"{version} locked entry")
    signature_key = version_object_key(product, version, ENTRY_SIGNATURE_NAME)
    signature_result = transport.get_object(signature_key, cache_bypass=True)
    if signature_result.status != 200:
        _raise_http_failure(
            "transparency locked entry signature could not be fetched",
            key=signature_key,
            result=signature_result,
            retryable=True,
        )
    comment = _verify_bytes(
        signer,
        record.bytes,
        signature_result.body,
        expected_trusted_comment=entry_trusted_comment(record.entry, record.sha256),
    )
    failures = validate_entry_trusted_comment(
        comment,
        entry=record.entry,
        entry_sha256=record.sha256,
    )
    if failures:
        raise DriverError(failures)
    return record


def fetch_chain_state(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
    signer: TransparencySigner,
) -> ChainState:
    local_highest = highest_seq(config.root, product=config.product)
    latest_result = transport.get_object(latest_key(config.product), cache_bypass=True)
    if latest_result.status == 404:
        if local_highest > 0:
            fail_closed(
                "transparency remote chain is behind the local head log",
                expected=f"chain_length >= {local_highest}",
                actual="0",
                repair=f"stop and audit {HEAD_LOG} for rollback or split-view evidence",
            )
        if config.genesis != "1":
            fail_closed(
                "missing transparency pointer requires TRANSPARENCY_GENESIS=1",
                expected="TRANSPARENCY_GENESIS=1",
                actual=repr(config.genesis),
                repair="set TRANSPARENCY_GENESIS=1 only for the first product publication",
            )
        listed = transport.list_prefix(f"releases/{config.product}/v/")
        if listed.status != 200:
            raise DriverError(
                [
                    failure(
                        "transparency genesis LIST failed",
                        expected="empty immutable version prefix",
                        actual=f"status={listed.status} exit_code={listed.exit_code}",
                        repair="retry after the S3 list operation is healthy",
                    )
                ]
            )
        if listed.keys:
            requested_prefix = version_prefix(config.product, config.version)
            if _stage_path(
                config.root, config.product, config.version
            ).exists() and all(key.startswith(requested_prefix) for key in listed.keys):
                LOG.warning(
                    "transparency genesis immutable zone already contains "
                    "requested version objects; adopting staged retry"
                )
            else:
                fail_closed(
                    "transparency genesis remote immutable zone is not empty",
                    expected="no releases/<product>/v/ objects",
                    actual=", ".join(listed.keys),
                    repair="resume the existing chain instead of starting genesis",
                )
        return ChainState(
            entries=(),
            pointer=None,
            pointer_signature=None,
            pointer_signature_etag=None,
            next_seq=1,
            prev_sha256=ZERO_SHA256,
            prev_version="",
            tip_published_utc=None,
            derived_ledger_jsonl=b"",
        )
    if latest_result.status != 200:
        _raise_http_failure(
            "transparency latest pointer could not be fetched",
            key=latest_key(config.product),
            result=latest_result,
            retryable=True,
        )
    pointer = parse_latest_bytes(latest_result.body)
    signature_result = transport.get_object(
        latest_signature_key(config.product),
        cache_bypass=True,
    )
    if signature_result.status != 200:
        _raise_http_failure(
            "transparency latest pointer signature could not be fetched",
            key=latest_signature_key(config.product),
            result=signature_result,
            retryable=True,
        )
    _require_etag(
        latest_result,
        key=latest_key(config.product),
        label="pointer",
    )
    signature_etag = _require_etag(
        signature_result,
        key=latest_signature_key(config.product),
        label="latest signature",
    )
    comment = _verify_bytes(
        signer,
        latest_result.body,
        signature_result.body,
        expected_trusted_comment=latest_trusted_comment(pointer.pointer),
    )
    failures = validate_latest_trusted_comment(comment, pointer=pointer.pointer)
    if failures:
        raise DriverError(failures)
    chain_length = int(pointer.pointer["chain_length"])
    if chain_length < local_highest:
        fail_closed(
            "transparency remote chain is behind the local head log",
            expected=f"chain_length >= {local_highest}",
            actual=str(chain_length),
            repair=f"stop and audit {HEAD_LOG} for rollback or split-view evidence",
        )
    listed = transport.list_prefix(f"releases/{config.product}/v/")
    if listed.status != 200:
        raise DriverError(
            [
                failure(
                    "transparency immutable LIST failed",
                    expected="releases/<product>/v/ listing",
                    actual=f"status={listed.status} exit_code={listed.exit_code}",
                    repair="retry after the S3 list operation is healthy",
                )
            ]
        )
    fetched_entries = tuple(
        _verify_remote_entry(
            product=config.product,
            version=version,
            transport=transport,
            signer=signer,
        )
        for version, _key in _immutable_entry_keys(config.product, listed.keys)
    )
    entries = tuple(
        sorted(fetched_entries, key=lambda record: int(record.entry["seq"]))
    )
    chain_failures = validate_entry_chain(entries)
    if chain_failures:
        raise DriverError(chain_failures)
    pointer_entries = tuple(
        entry for entry in entries if int(entry.entry["seq"]) <= chain_length
    )
    if len(pointer_entries) < chain_length:
        fail_closed(
            "transparency latest pointer tip entry is missing",
            expected=f"seq {chain_length} locked entry",
            actual=f"{len(pointer_entries)} locked entries",
            repair="retry after the immutable zone is visible",
        )
    tip = pointer_entries[-1]
    if (
        pointer.pointer["tip_sha256"] != tip.sha256
        or pointer.pointer["version"] != tip.entry["version"]
    ):
        fail_closed(
            "transparency latest pointer tip does not match locked entry",
            expected=f"{tip.entry['version']} {tip.sha256}",
            actual=f"{pointer.pointer['version']} {pointer.pointer['tip_sha256']}",
            repair="stop and audit the signed latest pointer before publishing",
        )
    ledger_result = transport.get_object(ledger_key(config.product), cache_bypass=True)
    if ledger_result.status == 200:
        ledger_records = parse_ledger_jsonl(ledger_result.body)
        ledger_failures = validate_entry_chain(ledger_records)
        if ledger_failures:
            raise DriverError(ledger_failures)
        locked_by_seq = {int(entry.entry["seq"]): entry for entry in entries}
        for record in ledger_records:
            locked = locked_by_seq.get(int(record.entry["seq"]))
            if locked is not None and locked.sha256 != record.sha256:
                fail_closed(
                    "transparency ledger.jsonl contradicts a locked entry",
                    expected=f"seq {record.entry['seq']} {locked.sha256}",
                    actual=record.sha256,
                    repair="stop and audit the mutable ledger before publishing",
                )
    elif ledger_result.status != 404:
        raise DriverError(
            [
                failure(
                    "transparency ledger.jsonl could not be fetched",
                    expected="200 or 404",
                    actual=(
                        f"status={ledger_result.status} "
                        f"exit_code={ledger_result.exit_code}"
                    ),
                    repair="retry after the S3 get operation is healthy",
                )
            ]
        )
    return ChainState(
        entries=pointer_entries,
        pointer=pointer,
        pointer_signature=signature_result.body,
        pointer_signature_etag=signature_etag,
        next_seq=chain_length + 1,
        prev_sha256=tip.sha256,
        prev_version=str(tip.entry["version"]),
        tip_published_utc=str(tip.entry["published_utc"]),
        derived_ledger_jsonl=ledger_jsonl_bytes(pointer_entries),
    )


def _stage_path(root: Path, product: str, version: str) -> Path:
    return root / STAGING_ROOT / encoded_stage_key(product, version)


def _validate_staging_manifest_path(relative_path: str) -> None:
    try:
        relative_path.encode("ascii")
    except UnicodeEncodeError:
        fail_closed(
            "transparency staging payload path is not ASCII",
            expected="ASCII relative POSIX path",
            actual=relative_path,
            repair="stage only archive payload paths from the retained candidate layout",
        )
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in relative_path
    ):
        fail_closed(
            "transparency staging payload path contains a control character",
            expected="printable ASCII relative POSIX path",
            actual=repr(relative_path),
            repair="stage only archive payload paths from the retained candidate layout",
        )


def render_staging_manifest(payload_dir: Path) -> bytes:
    paths_by_relative: dict[str, Path] = {}
    for path in payload_dir.rglob("*"):
        if path.is_symlink():
            fail_closed(
                "transparency staging payload contains a symlink",
                expected="regular files and directories only",
                actual=str(path),
                repair="rebuild the transparency stage from retained candidate bytes",
            )
        if path.is_dir():
            continue
        if not path.is_file():
            fail_closed(
                "transparency staging payload contains a non-regular file",
                expected="regular files only",
                actual=str(path),
                repair="rebuild the transparency stage from retained candidate bytes",
            )
        relative_path = path.relative_to(payload_dir).as_posix()
        _validate_staging_manifest_path(relative_path)
        paths_by_relative[relative_path] = path

    lines: list[str] = []
    # Paths are ASCII-only here, so Python string ordering matches byte ordering.
    for relative_path in sorted(paths_by_relative):
        data = paths_by_relative[relative_path].read_bytes()
        lines.append(
            f"sha256={sha256_bytes(data)}\tbytes={len(data)}\tpath={relative_path}\n"
        )
    return "".join(lines).encode("ascii")


def staging_manifest_digest(payload_dir: Path) -> str:
    return sha256_bytes(render_staging_manifest(payload_dir))


def _write_stage_manifest(stage_path: Path) -> str:
    manifest = render_staging_manifest(stage_path / PAYLOAD_DIR_NAME)
    atomic_write(stage_path / STAGING_MANIFEST_NAME, manifest)
    return sha256_bytes(manifest)


def _load_stage_metadata(stage_path: Path) -> Mapping[str, Any]:
    try:
        return json.loads((stage_path / "staging.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError(
            [
                failure(
                    "transparency staging metadata could not be read",
                    expected=str(stage_path / "staging.json"),
                    actual=type(exc).__name__,
                    repair="remove only this version's staging dir and retry",
                )
            ]
        ) from None


def load_existing_stage(
    *,
    root: Path,
    product: str,
    version: str,
    signer: TransparencySigner,
) -> StagedPublish:
    stage_path = _stage_path(root, product, version)
    metadata = _load_stage_metadata(stage_path)
    if metadata.get("product") != product or metadata.get("version") != version:
        fail_closed(
            "transparency staging metadata does not match requested version",
            expected=f"{product}/{version}",
            actual=f"{metadata.get('product')}/{metadata.get('version')}",
            repair="remove only the mismatched version staging dir and retry",
        )
    staged = StagedPublish(
        path=stage_path,
        product=product,
        version=version,
        seq=int(metadata["seq"]),
        source_commit=str(metadata["source_commit"]),
        entry_sha256=str(metadata["entry_sha256"]),
        published_utc=str(metadata["published_utc"]),
        valid_until=str(metadata["valid_until"]),
        staging_manifest_sha256=str(metadata["staging_manifest_sha256"]),
    )
    _verify_stage_signatures(staged, signer)
    return staged


def _assert_stage_extends_state(stage: StagedPublish, state: ChainState) -> None:
    record = parse_ledger_entry_bytes(stage.entry_path.read_bytes())
    entry = record.entry
    if (
        int(entry["seq"]) == state.next_seq
        and entry["prev_sha256"] == state.prev_sha256
    ):
        return
    fail_closed(
        (
            f"version {stage.version} is already permanently recorded at "
            f"seq={entry['seq']} source_commit={entry['source_commit']} "
            f"entry_sha256={record.sha256}"
        ),
        expected=f"seq={state.next_seq} prev_sha256={state.prev_sha256}",
        actual=f"seq={entry['seq']} prev_sha256={entry['prev_sha256']}",
        repair="cut the next version; a version key is one-shot and permanent",
    )


def create_stage_from_candidate(
    *,
    config: PublishConfig,
    state: ChainState,
    signer: TransparencySigner,
    now: datetime,
) -> StagedPublish:
    stage_path = _stage_path(config.root, config.product, config.version)
    if stage_path.exists():
        return load_existing_stage(
            root=config.root,
            product=config.product,
            version=config.version,
            signer=signer,
        )
    recover_candidate(
        config.root,
        version=config.version,
        source_commit=config.source_commit,
    )
    tmp_path = stage_path.with_name(f".{stage_path.name}.tmp")
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    try:
        snapshot_root = tmp_path / "snapshot"
        snapshot_candidate(
            source_root=config.root,
            snapshot_root=snapshot_root,
            version=config.version,
        )
        report = recover_candidate(
            snapshot_root,
            version=config.version,
            source_commit=config.source_commit,
        )
        parts = collect_candidate_parts(report)
        published_utc = format_published_utc(now)
        if state.tip_published_utc is not None:
            previous = parse_published_utc(
                state.tip_published_utc,
                label="tip published_utc",
            )
            current = parse_published_utc(published_utc, label="published_utc")
            if current <= previous:
                fail_closed(
                    "transparency published_utc is not later than the tip",
                    expected=f"later than {state.tip_published_utc}",
                    actual=published_utc,
                    repair="retry with a later wall clock time",
                )
        entry = build_ledger_entry(
            artifacts=parts.artifacts,
            manifests=parts.manifests,
            proofs=parts.proofs,
            prev_sha256=state.prev_sha256,
            prev_version=state.prev_version,
            product=config.product,
            published_utc=published_utc,
            seq=state.next_seq,
            source_commit=parts.source_commit,
            version=config.version,
        )
        entry_bytes = canonical_json_bytes(entry, label="ledger entry")
        entry_sha256 = sha256_bytes(entry_bytes)
        pointer = build_latest_pointer(
            chain_length=state.next_seq,
            product=config.product,
            signed_at=published_utc,
            tip_sha256=entry_sha256,
            valid_until=plus_14_days(published_utc),
            version=config.version,
        )
        pointer_bytes = canonical_json_bytes(pointer, label="latest pointer")
        payload_dir = tmp_path / PAYLOAD_DIR_NAME
        version_dir = payload_dir / "version-dir"
        version_dir.mkdir(parents=True)
        atomic_write(version_dir / ENTRY_OBJECT_NAME, entry_bytes)
        for name, path in sorted(parts.version_files.items()):
            shutil.copy2(path, version_dir / name)
        artifact_dir = payload_dir / ARTIFACT_DIR_NAME
        artifact_dir.mkdir(parents=True)
        for name, path in sorted(parts.artifact_files.items()):
            shutil.copy2(path, artifact_dir / name)
        signer.sign_file(
            version_dir / ENTRY_OBJECT_NAME,
            version_dir / ENTRY_SIGNATURE_NAME,
            trusted_comment=entry_trusted_comment(entry, entry_sha256),
        )
        atomic_write(
            payload_dir / LEDGER_OBJECT_NAME,
            state.derived_ledger_jsonl + entry_bytes,
        )
        atomic_write(payload_dir / LATEST_OBJECT_NAME, pointer_bytes)
        signer.sign_file(
            payload_dir / LATEST_OBJECT_NAME,
            payload_dir / LATEST_SIGNATURE_NAME,
            trusted_comment=latest_trusted_comment(pointer),
        )
        metadata = {
            "entry_sha256": entry_sha256,
            "product": config.product,
            "published_utc": published_utc,
            "seq": state.next_seq,
            "source_commit": parts.source_commit,
            "valid_until": pointer["valid_until"],
            "version": config.version,
        }
        atomic_write(
            tmp_path / "staging.json",
            canonical_json_bytes(metadata, label="staging metadata"),
        )
        manifest_sha256 = _write_stage_manifest(tmp_path)
        metadata["staging_manifest_sha256"] = manifest_sha256
        atomic_write(
            tmp_path / "staging.json",
            canonical_json_bytes(metadata, label="staging metadata"),
        )
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.rename(stage_path)
    except BaseException:
        if tmp_path.exists():
            shutil.rmtree(tmp_path)
        raise
    return load_existing_stage(
        root=config.root,
        product=config.product,
        version=config.version,
        signer=signer,
    )


def _verify_stage_signatures(staged: StagedPublish, signer: TransparencySigner) -> None:
    entry_record = parse_ledger_entry_bytes(staged.entry_path.read_bytes())
    signer.verify_file(
        staged.entry_path,
        staged.entry_signature_path,
        expected_trusted_comment=entry_trusted_comment(
            entry_record.entry,
            entry_record.sha256,
        ),
    )
    entry_comment = signer.trusted_comment(staged.entry_signature_path)
    entry_failures = validate_entry_trusted_comment(
        entry_comment,
        entry=entry_record.entry,
        entry_sha256=entry_record.sha256,
    )
    if entry_failures:
        raise DriverError(entry_failures)
    pointer_record = parse_latest_bytes(staged.latest_path.read_bytes())
    signer.verify_file(
        staged.latest_path,
        staged.latest_signature_path,
        expected_trusted_comment=latest_trusted_comment(pointer_record.pointer),
    )
    pointer_comment = signer.trusted_comment(staged.latest_signature_path)
    pointer_failures = validate_latest_trusted_comment(
        pointer_comment,
        pointer=pointer_record.pointer,
    )
    if pointer_failures:
        raise DriverError(pointer_failures)


def _default_archive_runner(command: str) -> ArchiveRunner:
    def run(stage_dir: Path, _expected_digest: str) -> str:
        result = subprocess.run(
            [*shlex.split(command), str(stage_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise DriverError(
                [
                    failure(
                        "transparency archive channel failed",
                        expected="exit 0 and ARCHIVED <sha256>",
                        actual=(
                            result.stderr or result.stdout or str(result.returncode)
                        ).strip(),
                        repair="retry after the archive channel is healthy",
                    )
                ]
            )
        return result.stdout

    return run


def _record_archive_call(
    transport: TransparencyTransport,
    *,
    stage: StagedPublish,
    status: str,
) -> None:
    call_log = getattr(transport, "call_log", None)
    if isinstance(call_log, list):
        call_log.append(
            {
                "plane": "archive",
                "op": "ARCHIVE",
                "key": str(stage.payload_dir),
                "destination": "archive-channel",
                "status": status,
            }
        )


def archive_stage(
    *,
    stage: StagedPublish,
    transport: TransparencyTransport,
    runner: ArchiveRunner,
) -> str:
    try:
        stdout = runner(stage.payload_dir, stage.staging_manifest_sha256)
    except DriverError:
        _record_archive_call(transport, stage=stage, status="failed")
        raise
    final_line = stdout.rstrip("\n").splitlines()[-1:] or [""]
    expected = f"ARCHIVED {stage.staging_manifest_sha256}"
    if final_line[0] != expected:
        _record_archive_call(transport, stage=stage, status="digest-mismatch")
        raise DriverError(
            [
                failure(
                    "transparency archive receipt digest mismatch",
                    expected=expected,
                    actual=final_line[0],
                    repair="retry only after the archive channel returns the staged digest",
                )
            ]
        )
    _record_archive_call(transport, stage=stage, status="archived")
    return stage.staging_manifest_sha256


def _clear_publish_call_log(transport: TransparencyTransport) -> None:
    call_log = getattr(transport, "call_log", None)
    if isinstance(call_log, list):
        call_log.clear()


def _put_success(result: HttpResult) -> bool:
    return 200 <= result.status < 300


def _upload_immutable(
    *,
    config: PublishConfig,
    stage: StagedPublish,
    transport: TransparencyTransport,
) -> tuple[str, ...]:
    uploaded: list[str] = []
    for name, path in stage.immutable_files():
        key = version_object_key(config.product, config.version, name)
        body = path.read_bytes()
        result = transport.put_object(
            key,
            body,
            content_type=SIG_CONTENT_TYPE
            if name.endswith(".minisig")
            else JSON_CONTENT_TYPE,
            cache_control=IMMUTABLE_CACHE,
            if_none_match=True,
        )
        if _put_success(result):
            uploaded.append(key)
            continue
        if result.status == 412:
            current = transport.get_object(key, cache_bypass=True)
            if current.status == 200 and current.body == body:
                uploaded.append(key)
                continue
            if name == ENTRY_OBJECT_NAME and current.status == 200:
                try:
                    existing = parse_ledger_entry_bytes(current.body)
                    error = (
                        f"version {config.version} is already permanently recorded at "
                        f"seq={existing.entry['seq']} source_commit={existing.entry['source_commit']} "
                        f"entry_sha256={existing.sha256}"
                    )
                except DriverError:
                    error = f"version {config.version} is already permanently recorded at unreadable bytes"
            else:
                error = (
                    f"version {config.version} is already permanently recorded at "
                    f"{name} status={current.status}"
                )
            raise DriverError(
                [
                    failure(
                        error,
                        expected=sha256_bytes(body),
                        actual=sha256_bytes(current.body)
                        if current.body
                        else f"status={current.status}",
                        repair="cut the next version; a locked-zone object can never be replaced",
                    )
                ]
            )
        if result.status in {0, 500, 502, 503, 504}:
            current = transport.get_object(key, cache_bypass=True)
            if current.status == 200 and current.body == body:
                uploaded.append(key)
                continue
        _raise_http_failure(
            "transparency immutable create-only upload failed",
            key=key,
            result=result,
            retryable=result.status >= 500 or result.status == 0,
        )
    return tuple(uploaded)


def _verify_public_objects(
    *,
    keys: Sequence[str],
    stage: StagedPublish,
    config: PublishConfig,
    transport: TransparencyTransport,
) -> None:
    by_key = {
        version_object_key(config.product, config.version, name): path
        for name, path in stage.immutable_files()
    }
    for key in keys:
        result = transport.get_public(key, cache_bypass=True)
        if result.status != 200:
            _raise_http_failure(
                "transparency public immutable verification failed",
                key=key,
                result=result,
                retryable=True,
            )
        expected = sha256_bytes(by_key[key].read_bytes())
        actual = sha256_bytes(result.body)
        if actual != expected:
            raise DriverError(
                [
                    failure(
                        "transparency public immutable bytes do not match staged bytes",
                        expected=f"{key} {expected}",
                        actual=actual,
                        repair="retry after the public surface exposes the uploaded immutable object",
                    )
                ]
            )


def _pre_pointer_guard(
    *,
    config: PublishConfig,
    state: ChainState,
    transport: TransparencyTransport,
) -> str | None:
    current = transport.get_object(latest_key(config.product), cache_bypass=True)
    if state.pointer is None:
        if current.status == 404:
            return None
        fail_closed(
            "transparency latest pointer moved before pointer write",
            expected="missing genesis pointer",
            actual=f"status={current.status}",
            repair="retry after refetching the chain state",
        )
    if current.status != 200 or current.body != state.pointer.bytes:
        fail_closed(
            "transparency latest pointer moved before pointer write",
            expected=sha256_bytes(state.pointer.bytes),
            actual=f"status={current.status} sha256={sha256_bytes(current.body) if current.body else '<empty>'}",
            repair="retry after refetching the chain state",
        )
    return _require_etag(current, key=latest_key(config.product), label="pointer")


def _latest_signature_result(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
) -> HttpResult:
    return transport.get_object(
        latest_signature_key(config.product),
        cache_bypass=True,
    )


def _raise_pointer_state_unknown(
    *,
    config: PublishConfig,
    pointer_result: HttpResult,
    latest_result: HttpResult,
    signature_result: HttpResult | None = None,
) -> None:
    signature_state = (
        "<not fetched>"
        if signature_result is None
        else f"status={signature_result.status} sha256={sha256_bytes(signature_result.body) if signature_result.body else '<empty>'}"
    )
    raise DriverError(
        [
            failure(
                "transparency latest pointer failure state could not be established",
                expected="live latest pointer is old-valid or new-valid",
                actual=(
                    f"put_status={pointer_result.status} "
                    f"latest_status={latest_result.status} "
                    f"latest_sha256={sha256_bytes(latest_result.body) if latest_result.body else '<empty>'} "
                    f"signature={signature_state}"
                ),
                repair=f"retry after re-querying {latest_key(config.product)} and its signature",
            )
        ]
    )


def _remote_byte_state(
    result: HttpResult,
    *,
    old_bytes: bytes,
    new_bytes: bytes,
) -> str:
    if result.status == 200:
        if result.body == old_bytes:
            return "old"
        if result.body == new_bytes:
            return "new"
        return f"other:{sha256_bytes(result.body) if result.body else '<empty>'}"
    if result.status == 404:
        return "missing"
    return (
        f"status={result.status}:"
        f"{sha256_bytes(result.body) if result.body else '<empty>'}"
    )


def _raise_torn_pointer_pair_after_restore(
    *,
    restore_result: HttpResult,
    latest_result: HttpResult,
    signature_result: HttpResult,
    pointer_state: str,
    signature_state: str,
) -> None:
    raise DriverError(
        [
            failure(
                "transparency latest pointer pair is torn after restore failure",
                expected="latest.json/latest.json.minisig are old-valid or new-valid",
                actual=(
                    f"latest.json={pointer_state} "
                    f"latest.json.minisig={signature_state} "
                    f"latest_status={latest_result.status} "
                    f"signature_status={signature_result.status} "
                    f"restore_status={restore_result.status} "
                    f"restore_exit_code={restore_result.exit_code}"
                ),
                repair=(
                    "retry after the object store converges; staged bytes are "
                    "deterministic, so a subsequent retry converges once the "
                    "pointer pair agrees"
                ),
            )
        ]
    )


def _resolve_restore_put_failure(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
    pointer_result: HttpResult,
    restore_result: HttpResult,
    old_pointer_bytes: bytes,
    old_signature_bytes: bytes,
    new_pointer_bytes: bytes,
    new_signature_bytes: bytes,
) -> None:
    latest_result = transport.get_object(latest_key(config.product), cache_bypass=True)
    signature_result = _latest_signature_result(config=config, transport=transport)
    pointer_state = _remote_byte_state(
        latest_result,
        old_bytes=old_pointer_bytes,
        new_bytes=new_pointer_bytes,
    )
    signature_state = _remote_byte_state(
        signature_result,
        old_bytes=old_signature_bytes,
        new_bytes=new_signature_bytes,
    )
    if pointer_state == "new" and signature_state == "new":
        return
    if pointer_state == "old" and signature_state == "old":
        _raise_http_failure(
            "transparency latest pointer upload failed",
            key=latest_key(config.product),
            result=pointer_result,
            retryable=pointer_result.status in {0, 412, 500, 502, 503, 504},
        )
    if (pointer_state, signature_state) in {("old", "new"), ("new", "old")}:
        _raise_torn_pointer_pair_after_restore(
            restore_result=restore_result,
            latest_result=latest_result,
            signature_result=signature_result,
            pointer_state=pointer_state,
            signature_state=signature_state,
        )
    _raise_pointer_state_unknown(
        config=config,
        pointer_result=pointer_result,
        latest_result=latest_result,
        signature_result=signature_result,
    )


def _restore_old_signature_or_raise(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
    pointer_result: HttpResult,
    signature_result: HttpResult,
    old_pointer_bytes: bytes,
    old_signature_bytes: bytes,
    new_pointer_bytes: bytes,
    new_signature_bytes: bytes,
) -> None:
    if not signature_result.etag:
        _raise_pointer_state_unknown(
            config=config,
            pointer_result=pointer_result,
            latest_result=transport.get_object(
                latest_key(config.product), cache_bypass=True
            ),
            signature_result=signature_result,
        )
        raise AssertionError("unreachable")
    restore_result = transport.put_object(
        latest_signature_key(config.product),
        old_signature_bytes,
        content_type=SIG_CONTENT_TYPE,
        cache_control=MUTABLE_CACHE,
        if_match=signature_result.etag,
    )
    if not _put_success(restore_result):
        _resolve_restore_put_failure(
            config=config,
            transport=transport,
            pointer_result=pointer_result,
            restore_result=restore_result,
            old_pointer_bytes=old_pointer_bytes,
            old_signature_bytes=old_signature_bytes,
            new_pointer_bytes=new_pointer_bytes,
            new_signature_bytes=new_signature_bytes,
        )
    _raise_http_failure(
        "transparency latest pointer upload failed",
        key=latest_key(config.product),
        result=pointer_result,
        retryable=pointer_result.status in {0, 412, 500, 502, 503, 504},
    )


def _resolve_pointer_put_failure(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
    pointer_result: HttpResult,
    old_pointer_bytes: bytes | None,
    old_signature_bytes: bytes | None,
    new_pointer_bytes: bytes,
    new_signature_bytes: bytes,
) -> None:
    latest_result = transport.get_object(latest_key(config.product), cache_bypass=True)
    if latest_result.status == 200 and latest_result.body == new_pointer_bytes:
        signature_result = _latest_signature_result(config=config, transport=transport)
        if (
            signature_result.status == 200
            and signature_result.body == new_signature_bytes
        ):
            return
        _raise_pointer_state_unknown(
            config=config,
            pointer_result=pointer_result,
            latest_result=latest_result,
            signature_result=signature_result,
        )

    if old_pointer_bytes is not None and latest_result.status == 200:
        if latest_result.body != old_pointer_bytes:
            _raise_pointer_state_unknown(
                config=config,
                pointer_result=pointer_result,
                latest_result=latest_result,
            )
        signature_result = _latest_signature_result(config=config, transport=transport)
        if old_signature_bytes is None or signature_result.status != 200:
            _raise_pointer_state_unknown(
                config=config,
                pointer_result=pointer_result,
                latest_result=latest_result,
                signature_result=signature_result,
            )
        if signature_result.body == old_signature_bytes:
            _raise_http_failure(
                "transparency latest pointer upload failed",
                key=latest_key(config.product),
                result=pointer_result,
                retryable=pointer_result.status in {0, 412, 500, 502, 503, 504},
            )
        if signature_result.body == new_signature_bytes:
            _restore_old_signature_or_raise(
                config=config,
                transport=transport,
                pointer_result=pointer_result,
                signature_result=signature_result,
                old_pointer_bytes=old_pointer_bytes,
                old_signature_bytes=old_signature_bytes,
                new_pointer_bytes=new_pointer_bytes,
                new_signature_bytes=new_signature_bytes,
            )
        _raise_pointer_state_unknown(
            config=config,
            pointer_result=pointer_result,
            latest_result=latest_result,
            signature_result=signature_result,
        )

    if old_pointer_bytes is None and latest_result.status == 404:
        _raise_http_failure(
            "transparency latest pointer upload failed",
            key=latest_key(config.product),
            result=pointer_result,
            retryable=pointer_result.status in {0, 412, 500, 502, 503, 504},
        )

    _raise_pointer_state_unknown(
        config=config,
        pointer_result=pointer_result,
        latest_result=latest_result,
    )


def _put_and_public_verify_mutable(
    *,
    key: str,
    body: bytes,
    content_type: str,
    transport: TransparencyTransport,
) -> None:
    result = transport.put_object(
        key,
        body,
        content_type=content_type,
        cache_control=MUTABLE_CACHE,
    )
    if not _put_success(result):
        _raise_http_failure(
            "transparency mutable upload failed",
            key=key,
            result=result,
            retryable=result.status >= 500 or result.status == 0,
        )
    public_result = transport.get_public(key, cache_bypass=True)
    if public_result.status != 200 or public_result.body != body:
        raise DriverError(
            [
                failure(
                    "transparency mutable public verification failed",
                    expected=f"{key} {sha256_bytes(body)}",
                    actual=f"status={public_result.status} sha256={sha256_bytes(public_result.body) if public_result.body else '<empty>'}",
                    repair="retry after the public surface exposes the mutable object",
                )
            ]
        )


def _adopt_existing_genesis_signature(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
    signature_result: HttpResult,
    expected_signature: bytes,
) -> bool:
    if signature_result.status != 412:
        return False
    current = transport.get_object(
        latest_signature_key(config.product),
        cache_bypass=True,
    )
    if current.status == 200 and current.body == expected_signature:
        LOG.warning(
            "transparency latest signature already exists for genesis; "
            "adopting staged-identical signature and resuming pointer write"
        )
        return True
    if current.status == 200:
        fail_closed(
            "transparency latest signature conflicts with staged genesis signature",
            expected=sha256_bytes(expected_signature),
            actual=sha256_bytes(current.body),
            repair="stop and audit the mutable latest signature before retrying",
        )
    return False


def _write_mutable_objects(
    *,
    config: PublishConfig,
    state: ChainState,
    stage: StagedPublish,
    transport: TransparencyTransport,
) -> None:
    etag = _pre_pointer_guard(config=config, state=state, transport=transport)
    old_pointer_bytes = state.pointer.bytes if state.pointer is not None else None
    old_signature_bytes = state.pointer_signature
    new_pointer_bytes = stage.latest_path.read_bytes()
    new_signature_bytes = stage.latest_signature_path.read_bytes()
    _put_and_public_verify_mutable(
        key=ledger_key(config.product),
        body=stage.ledger_jsonl_path.read_bytes(),
        content_type=TEXT_CONTENT_TYPE,
        transport=transport,
    )
    signature_result = transport.put_object(
        latest_signature_key(config.product),
        new_signature_bytes,
        content_type=SIG_CONTENT_TYPE,
        cache_control=MUTABLE_CACHE,
        if_none_match=state.pointer is None,
        if_match=state.pointer_signature_etag,
    )
    if not _put_success(signature_result):
        if not (
            state.pointer is None
            and _adopt_existing_genesis_signature(
                config=config,
                transport=transport,
                signature_result=signature_result,
                expected_signature=new_signature_bytes,
            )
        ):
            _raise_http_failure(
                "transparency latest signature upload failed",
                key=latest_signature_key(config.product),
                result=signature_result,
                retryable=signature_result.status >= 500
                or signature_result.status == 0,
            )
    pointer_result = transport.put_object(
        latest_key(config.product),
        new_pointer_bytes,
        content_type=JSON_CONTENT_TYPE,
        cache_control=MUTABLE_CACHE,
        if_none_match=state.pointer is None,
        if_match=etag,
    )
    if not _put_success(pointer_result):
        _resolve_pointer_put_failure(
            config=config,
            transport=transport,
            pointer_result=pointer_result,
            old_pointer_bytes=old_pointer_bytes,
            old_signature_bytes=old_signature_bytes,
            new_pointer_bytes=new_pointer_bytes,
            new_signature_bytes=new_signature_bytes,
        )


def publish_transparency(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
    signer: TransparencySigner,
    archive_runner: ArchiveRunner | None = None,
    now: datetime | None = None,
) -> PublishResult:
    started = time.monotonic()
    signer.check()
    state = fetch_chain_state(config=config, transport=transport, signer=signer)
    if state.pointer is not None and state.pointer.pointer["version"] == config.version:
        tip_source_commit = (
            state.entries[-1].entry.get("source_commit")
            if state.entries
            else "<unknown>"
        )
        fail_closed(
            f"version {config.version} is already permanently recorded at seq={state.pointer.pointer['chain_length']} source_commit={tip_source_commit} entry_sha256={state.pointer.pointer['tip_sha256']}",
            expected="new unpublished version",
            actual=config.version,
            repair="cut the next version; a version key is one-shot and permanent",
        )
    stage_path = _stage_path(config.root, config.product, config.version)
    if stage_path.exists():
        stage = load_existing_stage(
            root=config.root,
            product=config.product,
            version=config.version,
            signer=signer,
        )
    else:
        stage = create_stage_from_candidate(
            config=config,
            state=state,
            signer=signer,
            now=now or datetime.now(tz=UTC),
        )
    _assert_stage_extends_state(stage, state)
    _clear_publish_call_log(transport)
    archive_digest = archive_stage(
        stage=stage,
        transport=transport,
        runner=archive_runner or _default_archive_runner(config.archive_channel),
    )
    uploaded = _upload_immutable(config=config, stage=stage, transport=transport)
    _verify_public_objects(
        keys=uploaded,
        stage=stage,
        config=config,
        transport=transport,
    )
    _write_mutable_objects(
        config=config,
        state=state,
        stage=stage,
        transport=transport,
    )
    row = HeadLogRow(
        product=config.product,
        seq=stage.seq,
        version=stage.version,
        entry_sha256=stage.entry_sha256,
        published_utc=stage.published_utc,
    )
    append_head_row(config.root, row)
    witness = git_witness_status(config.root)
    public_urls = tuple(
        public_url(config.base_url, key)
        for key in (
            version_object_key(config.product, config.version, ENTRY_OBJECT_NAME),
            version_object_key(config.product, config.version, ENTRY_SIGNATURE_NAME),
            ledger_key(config.product),
            latest_signature_key(config.product),
            latest_key(config.product),
            PUBLIC_TRUST_ANCHOR_PATH,
        )
    )
    return PublishResult(
        product=config.product,
        version=config.version,
        seq=stage.seq,
        entry_sha256=stage.entry_sha256,
        public_urls=public_urls,
        archive_receipt_sha256=archive_digest,
        witness_status=witness,
        elapsed_seconds=time.monotonic() - started,
    )


@dataclass(frozen=True)
class VerifiedPointerBundle:
    pointer: Mapping[str, Any]
    pointer_bytes: bytes
    signature_bytes: bytes


def fetch_verified_pointer(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
    signer: TransparencySigner,
) -> VerifiedPointerBundle:
    state = fetch_chain_state(config=config, transport=transport, signer=signer)
    if state.pointer is None:
        fail_closed(
            "transparency latest pointer is missing",
            expected="existing signed latest pointer",
            actual="missing",
            repair="publish genesis before re-signing a pointer",
        )
    return VerifiedPointerBundle(
        pointer=state.pointer.pointer,
        pointer_bytes=state.pointer.bytes,
        signature_bytes=state.pointer_signature or b"",
    )


def resign_transparency_pointer(
    *,
    config: PublishConfig,
    transport: TransparencyTransport,
    signer: TransparencySigner,
    now: datetime | None = None,
) -> PublishResult:
    started = time.monotonic()
    signer.check()
    bundle = fetch_verified_pointer(config=config, transport=transport, signer=signer)
    signed_at = format_published_utc(now or datetime.now(tz=UTC))
    pointer = build_latest_pointer(
        chain_length=int(bundle.pointer["chain_length"]),
        product=str(bundle.pointer["product"]),
        signed_at=signed_at,
        tip_sha256=str(bundle.pointer["tip_sha256"]),
        valid_until=plus_14_days(signed_at),
        version=str(bundle.pointer["version"]),
    )
    pointer_bytes = canonical_json_bytes(pointer, label="latest pointer")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        latest_path = tmp_path / LATEST_OBJECT_NAME
        sig_path = tmp_path / LATEST_SIGNATURE_NAME
        latest_path.write_bytes(pointer_bytes)
        signer.sign_file(
            latest_path,
            sig_path,
            trusted_comment=latest_trusted_comment(pointer),
        )
        signer.verify_file(
            latest_path,
            sig_path,
            expected_trusted_comment=latest_trusted_comment(pointer),
        )
        current = transport.get_object(latest_key(config.product), cache_bypass=True)
        if current.status != 200 or current.body != bundle.pointer_bytes:
            fail_closed(
                "transparency latest pointer moved before re-sign",
                expected=sha256_bytes(bundle.pointer_bytes),
                actual=f"status={current.status} sha256={sha256_bytes(current.body) if current.body else '<empty>'}",
                repair="retry after refetching the signed pointer",
            )
        pointer_etag = _require_etag(
            current,
            key=latest_key(config.product),
            label="pointer",
        )
        current_signature = _latest_signature_result(config=config, transport=transport)
        if (
            current_signature.status != 200
            or current_signature.body != bundle.signature_bytes
        ):
            fail_closed(
                "transparency latest pointer signature moved before re-sign",
                expected=sha256_bytes(bundle.signature_bytes),
                actual=(
                    f"status={current_signature.status} "
                    f"sha256={sha256_bytes(current_signature.body) if current_signature.body else '<empty>'}"
                ),
                repair="retry after refetching the signed pointer",
            )
        signature_etag = _require_etag(
            current_signature,
            key=latest_signature_key(config.product),
            label="latest signature",
        )
        new_signature_bytes = sig_path.read_bytes()
        signature_result = transport.put_object(
            latest_signature_key(config.product),
            new_signature_bytes,
            content_type=SIG_CONTENT_TYPE,
            cache_control=MUTABLE_CACHE,
            if_match=signature_etag,
        )
        if not _put_success(signature_result):
            _raise_http_failure(
                "transparency latest signature upload failed",
                key=latest_signature_key(config.product),
                result=signature_result,
                retryable=True,
            )
        pointer_result = transport.put_object(
            latest_key(config.product),
            pointer_bytes,
            content_type=JSON_CONTENT_TYPE,
            cache_control=MUTABLE_CACHE,
            if_match=pointer_etag,
        )
        if not _put_success(pointer_result):
            _resolve_pointer_put_failure(
                config=config,
                transport=transport,
                pointer_result=pointer_result,
                old_pointer_bytes=bundle.pointer_bytes,
                old_signature_bytes=bundle.signature_bytes,
                new_pointer_bytes=pointer_bytes,
                new_signature_bytes=new_signature_bytes,
            )
    witness = WitnessStatus(
        state="not-written",
        message="resign-transparency-pointer does not append a head-log row",
    )
    return PublishResult(
        product=config.product,
        version=str(pointer["version"]),
        seq=int(pointer["chain_length"]),
        entry_sha256=str(pointer["tip_sha256"]),
        public_urls=(
            public_url(config.base_url, latest_signature_key(config.product)),
            public_url(config.base_url, latest_key(config.product)),
            public_url(config.base_url, PUBLIC_TRUST_ANCHOR_PATH),
        ),
        archive_receipt_sha256="",
        witness_status=witness,
        elapsed_seconds=time.monotonic() - started,
    )


def _config_from_args(
    args: argparse.Namespace, env: Mapping[str, str]
) -> PublishConfig:
    root = Path(args.root).resolve()
    version = args.version
    source_commit = args.source_commit
    release_dir = env.get("RELEASE_DIR")
    if release_dir and not version:
        version = Path(release_dir).name
    if not version:
        raise DriverError(
            [
                failure(
                    "transparency version is missing",
                    expected="--version or RELEASE_DIR basename",
                    actual="<missing>",
                    repair="pass --version <release-version>",
                )
            ]
        )
    if not source_commit:
        source_commit = env.get("SOURCE_COMMIT", "")
    if not source_commit:
        source_commit = _source_commit_from_retained_ledger(root=root, version=version)
    if not source_commit:
        raise DriverError(
            [
                failure(
                    "transparency source commit is missing",
                    expected="--source-commit, SOURCE_COMMIT, or retained ledger source_commit",
                    actual="<missing>",
                    repair="point RELEASE_DIR at a retained candidate with release evidence",
                )
            ]
        )
    return PublishConfig.from_env(
        root=root,
        version=version,
        source_commit=source_commit,
        env=env,
    )


def _source_commit_from_retained_ledger(*, root: Path, version: str) -> str:
    ledger_path = root / "target" / "release-evidence" / version / "ledger.json"
    try:
        payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    source_commit = payload.get("source_commit")
    return source_commit if isinstance(source_commit, str) else ""


def _transport_from_config(config: PublishConfig) -> CurlTransparencyTransport:
    return CurlTransparencyTransport(
        endpoint=config.s3_endpoint,
        bucket=config.bucket,
        access_key_id=config.access_key_id,
        secret_access_key=config.secret_access_key,
        base_url=config.base_url,
    )


def _signer_from_config(config: PublishConfig) -> LocalMinisignSigner:
    return LocalMinisignSigner(
        secret_key=config.minisign_key,
        public_key=config.minisign_pub,
    )


def _print_failures(error: DriverError) -> None:
    for item in error.failures:
        LOG.error(
            "%s\n  expected: %s\n  actual: %s\n  repair: %s",
            item.error,
            item.expected,
            item.actual,
            item.repair,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish transparency ledger entries.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--root", default=".")
    publish_parser.add_argument("--version", default="")
    publish_parser.add_argument("--source-commit", default="")
    resign_parser = subparsers.add_parser("resign-transparency-pointer")
    resign_parser.add_argument("--root", default=".")
    resign_parser.add_argument("--version", default="resign")
    resign_parser.add_argument("--source-commit", default="0" * 40)
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
        transport = _transport_from_config(config)
        signer = _signer_from_config(config)
        if args.command == "publish":
            result = publish_transparency(
                config=config,
                transport=transport,
                signer=signer,
            )
        elif args.command == "resign-transparency-pointer":
            result = resign_transparency_pointer(
                config=config,
                transport=transport,
                signer=signer,
            )
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
    except DriverError as exc:
        _print_failures(exc)
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
