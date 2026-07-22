from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.transparency_publish as publisher
from scripts.release_candidate_driver import CandidateReport, DriverError
from scripts.transparency_core import (
    ENTRY_OBJECT_NAME,
    ENTRY_SIGNATURE_NAME,
    LATEST_OBJECT_NAME,
    LATEST_SIGNATURE_NAME,
    LEDGER_OBJECT_NAME,
    PRODUCT,
    PUBLIC_TRUST_ANCHOR_FILENAME,
    PUBLIC_TRUST_ANCHOR_PATH,
    ZERO_SHA256,
    EntryRecord,
    NamedDigest,
    build_latest_pointer,
    build_ledger_entry,
    canonical_json_bytes,
    entry_trusted_comment,
    latest_key,
    latest_signature_key,
    latest_trusted_comment,
    ledger_key,
    parse_latest_bytes,
    parse_ledger_entry_bytes,
    sha256_bytes,
    version_object_key,
)
from scripts.transparency_head_log import HeadLogRow, append_head_row
from scripts.transparency_signing import FakeTransparencySigner
from scripts.transparency_transport import DirectoryTransparencyTransport, HttpResult

PROOF_TARGETS = ("linux-aarch64-musl", "linux-x86_64-musl", "macos-arm64")
SOURCE_COMMIT = "a" * 40
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "transparency"
STAGING_MANIFEST_SHA256 = (
    "c764f7770f4c8d582519f2ea1608a9811c425a71e1aea8ae076aec5d272d0e3f"
)


def _sha(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _candidate(
    root: Path,
    *,
    version: str = "0.9.1",
    proof_version: str | None = None,
    dirty: bool = False,
) -> CandidateReport:
    release_dir = root / "dist" / "release-candidate" / version
    evidence_dir = root / "target" / "release-evidence" / version
    release_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "proofs").mkdir(parents=True, exist_ok=True)
    package_names = [f"package-{index}.whl" for index in range(11)]
    manifest_names = [
        "solstone-core.rust-release-manifest.json",
        "solstone-journal.rust-release-manifest.json",
        "solstone-journal-cuda.rust-release-manifest.json",
        "solstone-journal-models.rust-release-manifest.json",
    ]
    for name in package_names:
        (release_dir / name).write_bytes(f"{name}\n".encode("utf-8"))
    for name in manifest_names:
        payload = {
            "source_commit": SOURCE_COMMIT,
            "source_dirty": dirty,
            "version": version,
        }
        (release_dir / name).write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
    files: list[dict[str, Any]] = []
    for path in sorted(release_dir.iterdir(), key=lambda item: item.name):
        digest, byte_count = _sha(path)
        files.append({"bytes": byte_count, "name": path.name, "sha256": digest})
    ledger = {
        "candidate": {
            "candidate_digest": "b" * 64,
            "file_count": len(files),
            "files": files,
            "manifest_file_count": len(manifest_names),
            "package_file_count": len(package_names),
            "path": f"dist/release-candidate/{version}",
        },
        "models": {"decision": "exclude", "package_version": "1.0.0"},
        "product": "solstone",
        "proofs": {"expected_targets": list(PROOF_TARGETS)},
        "source_commit": SOURCE_COMMIT,
        "version": version,
    }
    (evidence_dir / "ledger.json").write_text(
        json.dumps(ledger, sort_keys=True),
        encoding="utf-8",
    )
    proof_hashes: dict[str, str] = {}
    for target in PROOF_TARGETS:
        proof = {"target": target, "version": proof_version or version}
        path = evidence_dir / "proofs" / f"{target}.json"
        path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
        proof_hashes[target] = _sha(path)[0]
    return CandidateReport(
        heading="retained-candidate-valid",
        version=version,
        release_dir=release_dir,
        evidence_dir=evidence_dir,
        payload_files=len(files),
        candidate_digest="b" * 64,
        ledger_sha256=_sha(evidence_dir / "ledger.json")[0],
        proof_sha256=proof_hashes,
        bundle_digest="c" * 64,
    )


def _patch_recover(monkeypatch: pytest.MonkeyPatch, *, version: str = "0.9.1") -> None:
    def recover(root: Path, *, version: str, source_commit: str) -> CandidateReport:
        assert source_commit == SOURCE_COMMIT
        return _candidate(root, version=version)

    monkeypatch.setattr(publisher, "recover_candidate", recover)


def _config(
    root: Path, *, version: str = "0.9.1", genesis: str | None = "1"
) -> publisher.PublishConfig:
    return publisher.PublishConfig(
        root=root,
        version=version,
        source_commit=SOURCE_COMMIT,
        base_url="https://transparency.solstone.app",
        s3_endpoint="https://r2.example.invalid",
        bucket="transparency-test",
        access_key_id="AKIA_TEST",
        secret_access_key="SECRET_TEST",
        minisign_key=root / "secret.key",
        minisign_pub=root / "public.key",
        archive_channel="fake-archive",
        genesis=genesis,
    )


def _archive_failure(error: str, *, expected: str, actual: str) -> DriverError:
    return DriverError(
        [
            publisher.failure(
                error,
                expected=expected,
                actual=actual,
                repair="retry after rebuilding the transparency stage",
            )
        ]
    )


def _assert_archived_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_bytes: int | None,
) -> None:
    if not path.is_file():
        raise _archive_failure(
            "archive channel rejected missing staged file",
            expected=str(path),
            actual="missing",
        )
    data = path.read_bytes()
    actual_sha256 = sha256_bytes(data)
    if actual_sha256 != expected_sha256:
        raise _archive_failure(
            "archive channel rejected staged file digest",
            expected=expected_sha256,
            actual=actual_sha256,
        )
    if expected_bytes is not None and len(data) != expected_bytes:
        raise _archive_failure(
            "archive channel rejected staged file size",
            expected=str(expected_bytes),
            actual=str(len(data)),
        )


class ValidatingArchiveChannel:
    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str], str] = {}

    def __call__(self, stage_dir: Path, digest: str) -> str:
        actual_digest = publisher.staging_manifest_digest(stage_dir)
        if actual_digest != digest:
            raise _archive_failure(
                "archive channel rejected staging manifest digest",
                expected=actual_digest,
                actual=digest,
            )
        entry_record = parse_ledger_entry_bytes(
            (stage_dir / "version-dir" / ENTRY_OBJECT_NAME).read_bytes()
        )
        entry = entry_record.entry
        for item in entry["artifacts"]:
            _assert_archived_file(
                stage_dir / "artifacts" / item["name"],
                expected_sha256=item["sha256"],
                expected_bytes=item["bytes"],
            )
        for collection in ("manifests", "proofs"):
            for item in entry[collection]:
                _assert_archived_file(
                    stage_dir / "version-dir" / item["name"],
                    expected_sha256=item["sha256"],
                    expected_bytes=None,
                )
        for relative_path in (
            LEDGER_OBJECT_NAME,
            LATEST_OBJECT_NAME,
            LATEST_SIGNATURE_NAME,
            f"version-dir/{ENTRY_SIGNATURE_NAME}",
        ):
            if not (stage_dir / relative_path).is_file():
                raise _archive_failure(
                    "archive channel rejected missing staged file",
                    expected=relative_path,
                    actual="missing",
                )
        key = (str(entry["product"]), str(entry["version"]))
        previous = self._receipts.get(key)
        if previous is not None and previous != digest:
            raise _archive_failure(
                "archive channel rejected conflicting staged receipt",
                expected=previous,
                actual=digest,
            )
        self._receipts[key] = digest
        return f"ARCHIVED {digest}\n"


def _archive_ok(stage_dir: Path, digest: str) -> str:
    return ValidatingArchiveChannel()(stage_dir, digest)


def _write_staging_manifest_fixture_tree(root: Path) -> None:
    (root / "a").mkdir()
    (root / "a" / "b").write_bytes(b"bee\n")
    (root / "a-c").write_bytes(b"dash\n")
    (root / "root.txt").write_bytes(b"root\n")


def _naive_full_manifest_digest(root: Path) -> str:
    rows: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root).as_posix()
        data = path.read_bytes()
        rows.append(
            (
                relative_path,
                f"sha256={sha256_bytes(data)}\tbytes={len(data)}\tpath={relative_path}\n",
            )
        )
    return sha256_bytes("".join(row for _path, row in sorted(rows)).encode("ascii"))


def _stage_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "0.9.1",
    now: datetime = datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
) -> publisher.StagedPublish:
    _candidate(tmp_path, version=version)
    _patch_recover(monkeypatch, version=version)
    config = _config(tmp_path, version=version)
    signer = FakeTransparencySigner()
    state = publisher.fetch_chain_state(
        config=config,
        transport=DirectoryTransparencyTransport(tmp_path / "remote"),
        signer=signer,
    )
    return publisher.create_stage_from_candidate(
        config=config,
        state=state,
        signer=signer,
        now=now,
    )


def _install_remote_entry(
    *,
    transport: DirectoryTransparencyTransport,
    signer: FakeTransparencySigner,
    tmp_path: Path,
    seq: int,
    version: str,
    prev_sha256: str = ZERO_SHA256,
    prev_version: str = "",
    published_utc: str = "2026-07-22T00:00:00Z",
) -> EntryRecord:
    entry = build_ledger_entry(
        artifacts=[
            NamedDigest(name=f"artifact-{version}.whl", sha256="d" * 64, bytes=5)
        ],
        manifests=[],
        proofs=[],
        prev_sha256=prev_sha256,
        prev_version=prev_version,
        product=PRODUCT,
        published_utc=published_utc,
        seq=seq,
        source_commit=SOURCE_COMMIT,
        version=version,
    )
    entry_bytes = canonical_json_bytes(entry)
    entry_sha = sha256_bytes(entry_bytes)
    entry_path = tmp_path / f"{version}-entry.json"
    sig_path = tmp_path / f"{version}-entry.minisig"
    entry_path.write_bytes(entry_bytes)
    signer.sign_file(
        entry_path,
        sig_path,
        trusted_comment=entry_trusted_comment(entry, entry_sha),
    )
    transport.put_object(
        version_object_key(PRODUCT, version, ENTRY_OBJECT_NAME),
        entry_bytes,
        content_type="application/json",
        cache_control="immutable",
        if_none_match=True,
    )
    transport.put_object(
        version_object_key(PRODUCT, version, ENTRY_SIGNATURE_NAME),
        sig_path.read_bytes(),
        content_type="application/octet-stream",
        cache_control="immutable",
        if_none_match=True,
    )
    pointer = build_latest_pointer(
        chain_length=seq,
        product=PRODUCT,
        signed_at=published_utc,
        tip_sha256=entry_sha,
        valid_until="2026-08-05T00:00:00Z",
        version=version,
    )
    pointer_bytes = canonical_json_bytes(pointer)
    latest_path = tmp_path / "latest.json"
    latest_sig = tmp_path / "latest.json.minisig"
    latest_path.write_bytes(pointer_bytes)
    signer.sign_file(
        latest_path,
        latest_sig,
        trusted_comment=latest_trusted_comment(pointer),
    )
    transport.put_object(
        ledger_key(PRODUCT),
        entry_bytes,
        content_type="application/jsonl",
        cache_control="no-cache",
    )
    transport.put_object(
        latest_signature_key(PRODUCT),
        latest_sig.read_bytes(),
        content_type="application/octet-stream",
        cache_control="no-cache",
    )
    transport.put_object(
        latest_key(PRODUCT),
        pointer_bytes,
        content_type="application/json",
        cache_control="no-cache",
    )
    return EntryRecord(entry=entry, bytes=entry_bytes, sha256=entry_sha)


def test_staging_manifest_v1_pinned_fixture(tmp_path: Path) -> None:
    _write_staging_manifest_fixture_tree(tmp_path)
    manifest = publisher.render_staging_manifest(tmp_path)
    assert manifest == (FIXTURE_DIR / "staging-manifest-v1.txt").read_bytes()
    assert sha256_bytes(manifest) == STAGING_MANIFEST_SHA256


def test_staging_manifest_uses_ascii_string_sort_order(tmp_path: Path) -> None:
    _write_staging_manifest_fixture_tree(tmp_path)
    manifest = publisher.render_staging_manifest(tmp_path).decode("ascii")
    assert [line.rsplit("path=", 1)[1] for line in manifest.splitlines()] == [
        "a-c",
        "a/b",
        "root.txt",
    ]


def test_staging_manifest_rejects_symlinks(tmp_path: Path) -> None:
    (tmp_path / "regular").write_bytes(b"ok")
    (tmp_path / "link").symlink_to(tmp_path / "regular")
    with pytest.raises(DriverError) as error:
        publisher.render_staging_manifest(tmp_path)
    assert (
        error.value.failures[0].error
        == "transparency staging payload contains a symlink"
    )


@pytest.mark.parametrize(
    ("name", "expected_error"),
    (
        ("caf\u00e9", "transparency staging payload path is not ASCII"),
        ("bad\nname", "transparency staging payload path contains a control character"),
    ),
)
def test_staging_manifest_rejects_invalid_paths(
    tmp_path: Path,
    name: str,
    expected_error: str,
) -> None:
    (tmp_path / name).write_bytes(b"bad")
    with pytest.raises(DriverError) as error:
        publisher.render_staging_manifest(tmp_path)
    assert error.value.failures[0].error == expected_error


def test_staging_payload_digest_has_no_exclusions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage_candidate(tmp_path, monkeypatch)
    assert publisher.staging_manifest_digest(
        stage.payload_dir
    ) == _naive_full_manifest_digest(stage.payload_dir)
    assert not (stage.payload_dir / "staging.json").exists()
    assert not (stage.payload_dir / "staging-manifest.txt").exists()


def test_staging_payload_is_archive_superset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage_candidate(tmp_path, monkeypatch)
    artifact_names = {path.name for path in (stage.payload_dir / "artifacts").iterdir()}
    assert artifact_names == {f"package-{index}.whl" for index in range(11)}
    evidence_names = {path.name for path in stage.version_dir.iterdir()}
    assert {
        ENTRY_OBJECT_NAME,
        ENTRY_SIGNATURE_NAME,
        "linux-aarch64-musl.json",
        "linux-x86_64-musl.json",
        "macos-arm64.json",
        "solstone-core.rust-release-manifest.json",
        "solstone-journal-cuda.rust-release-manifest.json",
        "solstone-journal-models.rust-release-manifest.json",
        "solstone-journal.rust-release-manifest.json",
    }.issubset(evidence_names)
    assert (stage.payload_dir / LEDGER_OBJECT_NAME).is_file()
    assert (stage.payload_dir / LATEST_OBJECT_NAME).is_file()
    assert (stage.payload_dir / LATEST_SIGNATURE_NAME).is_file()


def test_archive_channel_accepts_identical_retry_and_rejects_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage_candidate(tmp_path, monkeypatch)
    channel = ValidatingArchiveChannel()
    assert channel(stage.payload_dir, stage.staging_manifest_sha256) == (
        f"ARCHIVED {stage.staging_manifest_sha256}\n"
    )
    assert channel(stage.payload_dir, stage.staging_manifest_sha256) == (
        f"ARCHIVED {stage.staging_manifest_sha256}\n"
    )
    (stage.payload_dir / LATEST_OBJECT_NAME).write_bytes(b"conflict\n")
    conflict_digest = publisher.staging_manifest_digest(stage.payload_dir)
    with pytest.raises(DriverError) as error:
        channel(stage.payload_dir, conflict_digest)
    assert (
        error.value.failures[0].error
        == "archive channel rejected conflicting staged receipt"
    )


def test_archive_channel_rejects_declared_file_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage_candidate(tmp_path, monkeypatch)
    artifact = stage.payload_dir / "artifacts" / "package-0.whl"
    artifact.write_bytes(b"wrong\n")
    digest = publisher.staging_manifest_digest(stage.payload_dir)
    with pytest.raises(DriverError) as error:
        ValidatingArchiveChannel()(stage.payload_dir, digest)
    assert (
        error.value.failures[0].error == "archive channel rejected staged file digest"
    )


def test_publish_genesis_uploads_fixed_layout_and_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path)
    _patch_recover(monkeypatch)
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    result = publisher.publish_transparency(
        config=_config(tmp_path),
        transport=transport,
        signer=signer,
        archive_runner=_archive_ok,
        now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    assert result.product == PRODUCT
    assert result.version == "0.9.1"
    assert result.seq == 1
    assert (
        f"https://transparency.solstone.app/{PUBLIC_TRUST_ANCHOR_PATH}"
        in result.public_urls
    )
    calls = [(call["plane"], call["op"], call["key"]) for call in transport.call_log]
    immutable = [
        version_object_key(PRODUCT, "0.9.1", ENTRY_OBJECT_NAME),
        version_object_key(PRODUCT, "0.9.1", ENTRY_SIGNATURE_NAME),
        *[
            version_object_key(PRODUCT, "0.9.1", name)
            for name in (
                "linux-aarch64-musl.json",
                "linux-x86_64-musl.json",
                "macos-arm64.json",
                "solstone-core.rust-release-manifest.json",
                "solstone-journal-cuda.rust-release-manifest.json",
                "solstone-journal-models.rust-release-manifest.json",
                "solstone-journal.rust-release-manifest.json",
            )
        ],
    ]
    assert transport.list_prefix(f"releases/{PRODUCT}/").keys == tuple(
        sorted(
            (
                *immutable,
                ledger_key(PRODUCT),
                latest_key(PRODUCT),
                latest_signature_key(PRODUCT),
            )
        )
    )
    public_keys = transport.list_prefix(f"releases/{PRODUCT}/").keys
    assert all("package-" not in key for key in public_keys)
    all_release_keys = transport.list_prefix("releases/").keys
    assert all(not key.startswith("releases/keys/") for key in all_release_keys)
    assert all(
        not str(call["key"]).startswith("releases/keys/")
        for call in transport.call_log
        if call["op"] == "PUT"
    )
    ledger = transport.get_object(ledger_key(PRODUCT)).body
    latest = parse_latest_bytes(transport.get_object(latest_key(PRODUCT)).body)
    assert (
        hashlib.sha256(ledger.splitlines(keepends=True)[-1]).hexdigest()
        == latest.pointer["tip_sha256"]
    )
    assert calls[0] == (
        "archive",
        "ARCHIVE",
        str(tmp_path / "target/transparency-publish/solstone-journal/0.9.1/payload"),
    )
    assert calls[1:10] == [("s3", "PUT", key) for key in immutable]
    assert calls[10:19] == [("public", "GET", key) for key in immutable]
    assert calls[19:] == [
        ("s3", "GET", latest_key(PRODUCT)),
        ("s3", "PUT", ledger_key(PRODUCT)),
        ("public", "GET", ledger_key(PRODUCT)),
        ("s3", "PUT", latest_signature_key(PRODUCT)),
        ("s3", "PUT", latest_key(PRODUCT)),
    ]


def test_publish_and_resign_accept_lexicographically_inverted_version_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path, version="0.11.0")
    _patch_recover(monkeypatch, version="0.11.0")
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    first = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.9.1",
        published_utc="2026-07-22T00:00:00Z",
    )
    second = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=2,
        version="0.10.0",
        prev_sha256=first.sha256,
        prev_version="0.9.1",
        published_utc="2026-07-23T00:00:00Z",
    )
    transport.put_object(
        ledger_key(PRODUCT),
        first.bytes + second.bytes,
        content_type="application/jsonl",
        cache_control="no-cache",
    )

    result = publisher.publish_transparency(
        config=_config(tmp_path, version="0.11.0", genesis=None),
        transport=transport,
        signer=signer,
        archive_runner=_archive_ok,
        now=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )

    assert result.seq == 3
    assert result.version == "0.11.0"
    resigned = publisher.resign_transparency_pointer(
        config=_config(tmp_path, version="0.11.0", genesis=None),
        transport=transport,
        signer=signer,
        now=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
    )
    assert resigned.seq == 3
    assert resigned.version == "0.11.0"


def test_genesis_retry_adopts_orphan_latest_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _candidate(tmp_path)
    _patch_recover(monkeypatch)
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    config = _config(tmp_path)
    transport.add_failure(plane="s3", op="PUT", key=latest_key(PRODUCT), status=500)

    with pytest.raises(DriverError):
        publisher.publish_transparency(
            config=config,
            transport=transport,
            signer=signer,
            archive_runner=_archive_ok,
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )

    assert transport.get_object(latest_key(PRODUCT)).status == 404
    orphan_signature = transport.get_object(latest_signature_key(PRODUCT)).body
    caplog.set_level(logging.WARNING, logger=publisher.LOG.name)
    result = publisher.publish_transparency(
        config=config,
        transport=transport,
        signer=signer,
        archive_runner=_archive_ok,
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )

    assert result.seq == 1
    assert transport.get_object(latest_key(PRODUCT)).status == 200
    assert transport.get_object(latest_signature_key(PRODUCT)).body == orphan_signature
    assert "adopting staged-identical signature" in caplog.text


def test_minisign_pub_env_overrides_local_path_only(tmp_path: Path) -> None:
    local_pub = tmp_path / "operator-local-name.pub"
    assert local_pub.name != PUBLIC_TRUST_ANCHOR_FILENAME
    env = {
        "TRANSPARENCY_S3_ENDPOINT": "https://r2.example.invalid",
        "TRANSPARENCY_BUCKET": "bucket",
        "TRANSPARENCY_S3_ACCESS_KEY_ID": "key",
        "TRANSPARENCY_S3_SECRET_ACCESS_KEY": "secret",
        "TRANSPARENCY_MINISIGN_KEY": str(tmp_path / "secret.key"),
        "TRANSPARENCY_MINISIGN_PUB": str(local_pub),
        "TRANSPARENCY_ARCHIVE_CHANNEL": "archive",
    }
    config = publisher.PublishConfig.from_env(
        root=tmp_path,
        version="0.9.1",
        source_commit=SOURCE_COMMIT,
        env=env,
    )
    assert config.minisign_pub == local_pub


def test_publish_config_repr_does_not_expose_secret(tmp_path: Path) -> None:
    assert "SECRET_TEST" not in repr(_config(tmp_path))


def test_public_immutable_verification_failure_prevents_mutable_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path)
    _patch_recover(monkeypatch)
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    failed_key = version_object_key(PRODUCT, "0.9.1", ENTRY_OBJECT_NAME)
    transport.add_failure(
        plane="public",
        op="GET",
        key=failed_key,
        status=500,
        body=b"not visible",
    )
    with pytest.raises(DriverError) as error:
        publisher.publish_transparency(
            config=_config(tmp_path),
            transport=transport,
            signer=signer,
            archive_runner=_archive_ok,
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    assert (
        error.value.failures[0].error
        == "transparency public immutable verification failed"
    )
    mutable_puts = [
        call
        for call in transport.call_log
        if call["op"] == "PUT"
        and call["key"]
        in {ledger_key(PRODUCT), latest_key(PRODUCT), latest_signature_key(PRODUCT)}
    ]
    assert mutable_puts == []


def test_missing_archive_channel_fails_before_upload(tmp_path: Path) -> None:
    env = {
        "TRANSPARENCY_S3_ENDPOINT": "https://r2.example.invalid",
        "TRANSPARENCY_BUCKET": "bucket",
        "TRANSPARENCY_S3_ACCESS_KEY_ID": "key",
        "TRANSPARENCY_S3_SECRET_ACCESS_KEY": "secret",
        "TRANSPARENCY_MINISIGN_KEY": str(tmp_path / "secret"),
        "TRANSPARENCY_MINISIGN_PUB": str(tmp_path / "pub"),
    }
    with pytest.raises(DriverError) as error:
        publisher.PublishConfig.from_env(
            root=tmp_path,
            version="0.9.1",
            source_commit=SOURCE_COMMIT,
            env=env,
        )
    assert (
        error.value.failures[0].error
        == "transparency publish environment is incomplete"
    )


def test_archive_channel_failure_and_digest_mismatch_fail_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path)
    _patch_recover(monkeypatch)
    transport = DirectoryTransparencyTransport(tmp_path / "remote")

    def fail_archive(_stage_dir: Path, _digest: str) -> str:
        raise DriverError(
            [
                publisher.failure(
                    "transparency archive channel failed",
                    expected="exit 0",
                    actual="1",
                    repair="retry after archive recovery",
                )
            ]
        )

    with pytest.raises(DriverError):
        publisher.publish_transparency(
            config=_config(tmp_path),
            transport=transport,
            signer=FakeTransparencySigner(),
            archive_runner=fail_archive,
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    assert [call["op"] for call in transport.call_log] == ["ARCHIVE"]
    transport.call_log.clear()
    with pytest.raises(DriverError) as error:
        publisher.publish_transparency(
            config=_config(tmp_path),
            transport=transport,
            signer=FakeTransparencySigner(),
            archive_runner=lambda _stage, _digest: "ARCHIVED bad\n",
            now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        )
    assert (
        error.value.failures[0].error == "transparency archive receipt digest mismatch"
    )
    assert [call["op"] for call in transport.call_log] == ["ARCHIVE"]


def test_genesis_requires_explicit_env_gate(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    with pytest.raises(DriverError) as error:
        publisher.fetch_chain_state(
            config=_config(tmp_path, genesis=None),
            transport=transport,
            signer=signer,
        )
    assert (
        error.value.failures[0].error
        == "missing transparency pointer requires TRANSPARENCY_GENESIS=1"
    )


def test_head_log_rollback_guard_fails_closed(tmp_path: Path) -> None:
    append_head_row(
        tmp_path,
        HeadLogRow(
            product=PRODUCT,
            seq=2,
            version="0.0.2",
            entry_sha256="e" * 64,
            published_utc="2026-07-22T00:00:00Z",
        ),
    )
    with pytest.raises(DriverError) as error:
        publisher.fetch_chain_state(
            config=_config(tmp_path, genesis="1"),
            transport=DirectoryTransparencyTransport(tmp_path / "remote"),
            signer=FakeTransparencySigner(),
        )
    assert (
        error.value.failures[0].error
        == "transparency remote chain is behind the local head log"
    )


def test_staged_retry_reuses_entry_bytes_despite_advancing_wall_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path)
    _patch_recover(monkeypatch)
    config = _config(tmp_path)
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    state = publisher.fetch_chain_state(
        config=config, transport=transport, signer=signer
    )
    first = publisher.create_stage_from_candidate(
        config=config,
        state=state,
        signer=signer,
        now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    first_bytes = first.entry_path.read_bytes()
    second = publisher.create_stage_from_candidate(
        config=config,
        state=state,
        signer=signer,
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    assert second.entry_path.read_bytes() == first_bytes


def test_stale_stage_fails_poisoned_version_before_mutable_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path, version="0.9.2")
    _patch_recover(monkeypatch, version="0.9.2")
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    first = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.9.1",
    )
    config = _config(tmp_path, version="0.9.2", genesis=None)
    state = publisher.fetch_chain_state(
        config=config,
        transport=transport,
        signer=signer,
    )
    stage = publisher.create_stage_from_candidate(
        config=config,
        state=state,
        signer=signer,
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    second = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=2,
        version="0.9.3",
        prev_sha256=first.sha256,
        prev_version="0.9.1",
        published_utc="2026-07-24T00:00:00Z",
    )
    transport.put_object(
        ledger_key(PRODUCT),
        first.bytes + second.bytes,
        content_type="application/jsonl",
        cache_control="no-cache",
    )
    transport.call_log.clear()

    with pytest.raises(DriverError) as error:
        publisher.publish_transparency(
            config=config,
            transport=transport,
            signer=signer,
            archive_runner=_archive_ok,
            now=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        )

    assert "version 0.9.2 is already permanently recorded at seq=2" in (
        error.value.failures[0].error
    )
    assert f"entry_sha256={stage.entry_sha256}" in error.value.failures[0].error
    mutable_puts = [
        call
        for call in transport.call_log
        if call["op"] == "PUT"
        and call["key"]
        in {
            ledger_key(PRODUCT),
            latest_signature_key(PRODUCT),
            latest_key(PRODUCT),
        }
    ]
    assert mutable_puts == []


def test_candidate_revalidation_failure_stops_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recover(_root: Path, *, version: str, source_commit: str) -> CandidateReport:
        raise DriverError(
            [
                publisher.failure(
                    "candidate revalidation failed",
                    expected=version,
                    actual=source_commit,
                    repair="bash scripts/release.sh --recover",
                )
            ]
        )

    monkeypatch.setattr(publisher, "recover_candidate", recover)
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    with pytest.raises(DriverError) as error:
        publisher.publish_transparency(
            config=_config(tmp_path),
            transport=transport,
            signer=FakeTransparencySigner(),
            archive_runner=_archive_ok,
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    assert error.value.failures[0].error == "candidate revalidation failed"
    assert transport.call_log[-1]["op"] == "LIST"


def test_stale_proofs_fail_closed_before_signing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path, proof_version="0.9.0")

    def recover(root: Path, *, version: str, source_commit: str) -> CandidateReport:
        assert source_commit == SOURCE_COMMIT
        return _candidate(root, version=version, proof_version="0.9.0")

    monkeypatch.setattr(publisher, "recover_candidate", recover)
    with pytest.raises(DriverError) as error:
        publisher.publish_transparency(
            config=_config(tmp_path),
            transport=DirectoryTransparencyTransport(tmp_path / "remote"),
            signer=FakeTransparencySigner(),
            archive_runner=_archive_ok,
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    assert error.value.failures[0].error == "retained proof version is stale"


def test_dirty_retained_manifest_fails_closed_before_signing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path, dirty=True)

    def recover(root: Path, *, version: str, source_commit: str) -> CandidateReport:
        assert source_commit == SOURCE_COMMIT
        return _candidate(root, version=version, dirty=True)

    monkeypatch.setattr(publisher, "recover_candidate", recover)
    with pytest.raises(DriverError) as error:
        publisher.publish_transparency(
            config=_config(tmp_path),
            transport=DirectoryTransparencyTransport(tmp_path / "remote"),
            signer=FakeTransparencySigner(),
            archive_runner=_archive_ok,
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    assert (
        error.value.failures[0].error
        == "candidate validated state reports a dirty source"
    )


def test_existing_version_is_permanent_terminal_failure(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    existing = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.9.1",
    )
    with pytest.raises(DriverError) as error:
        publisher.publish_transparency(
            config=_config(tmp_path, genesis=None),
            transport=transport,
            signer=signer,
            archive_runner=_archive_ok,
            now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        )
    assert error.value.failures[0].error == (
        f"version 0.9.1 is already permanently recorded at seq=1 source_commit={SOURCE_COMMIT} "
        f"entry_sha256={existing.sha256}"
    )


def test_foreign_product_in_fetched_state_fails_closed(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    pointer = build_latest_pointer(
        chain_length=1,
        product=PRODUCT,
        signed_at="2026-07-22T00:00:00Z",
        tip_sha256="e" * 64,
        valid_until="2026-08-05T00:00:00Z",
        version="0.0.1",
    )
    pointer["product"] = "solstone-linux"
    body = canonical_json_bytes(pointer, label="foreign pointer")
    path = tmp_path / "latest.json"
    sig = tmp_path / "latest.json.minisig"
    path.write_bytes(body)
    signer.sign_file(path, sig, trusted_comment=latest_trusted_comment(pointer))
    transport.put_object(
        latest_key(PRODUCT),
        body,
        content_type="application/json",
        cache_control="no-cache",
    )
    transport.put_object(
        latest_signature_key(PRODUCT),
        sig.read_bytes(),
        content_type="application/octet-stream",
        cache_control="no-cache",
    )
    with pytest.raises(DriverError) as error:
        publisher.fetch_chain_state(
            config=_config(tmp_path, genesis=None),
            transport=transport,
            signer=signer,
        )
    assert error.value.failures[0].error == "latest pointer product is invalid"


def test_ledger_jsonl_contradicting_locked_entry_fails(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    locked = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.0.1",
    )
    forged_entry = {**locked.entry, "source_commit": "b" * 40}
    forged_bytes = canonical_json_bytes(forged_entry)
    transport.put_object(
        ledger_key(PRODUCT),
        forged_bytes,
        content_type="application/jsonl",
        cache_control="no-cache",
    )
    with pytest.raises(DriverError) as error:
        publisher.fetch_chain_state(
            config=_config(tmp_path, version="0.0.2", genesis=None),
            transport=transport,
            signer=signer,
        )
    assert (
        error.value.failures[0].error
        == "transparency ledger.jsonl contradicts a locked entry"
    )


def test_missing_ledger_jsonl_is_rederived_without_failure(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    first = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.0.1",
    )
    transport._object_path(ledger_key(PRODUCT)).unlink()
    state = publisher.fetch_chain_state(
        config=_config(tmp_path, version="0.0.2", genesis=None),
        transport=transport,
        signer=signer,
    )
    assert state.derived_ledger_jsonl == first.bytes


def test_superset_ledger_jsonl_that_still_chains_is_rederived(
    tmp_path: Path,
) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    first = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.0.1",
    )
    first_latest = transport.get_object(latest_key(PRODUCT)).body
    first_latest_sig = transport.get_object(latest_signature_key(PRODUCT)).body
    second = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=2,
        version="0.0.2",
        prev_sha256=first.sha256,
        prev_version="0.0.1",
        published_utc="2026-07-23T00:00:00Z",
    )
    transport.put_object(
        latest_signature_key(PRODUCT),
        first_latest_sig,
        content_type="application/octet-stream",
        cache_control="no-cache",
    )
    transport.put_object(
        latest_key(PRODUCT),
        first_latest,
        content_type="application/json",
        cache_control="no-cache",
    )
    transport.put_object(
        ledger_key(PRODUCT),
        first.bytes + second.bytes,
        content_type="application/jsonl",
        cache_control="no-cache",
    )
    state = publisher.fetch_chain_state(
        config=_config(tmp_path, version="0.0.3", genesis=None),
        transport=transport,
        signer=signer,
    )
    assert state.derived_ledger_jsonl == first.bytes


def test_tip_signature_invalid_fails_closed(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.0.1",
    )
    transport._object_path(latest_signature_key(PRODUCT)).write_text(
        "untrusted comment: fake transparency signature\nbad\nwrong\n",
        encoding="utf-8",
    )
    with pytest.raises(DriverError) as error:
        publisher.fetch_chain_state(
            config=_config(tmp_path, version="0.0.2", genesis=None),
            transport=transport,
            signer=signer,
        )
    assert (
        error.value.failures[0].error
        == "fake transparency trusted comment line is malformed"
    )


def test_existing_pointer_without_etag_fails_closed(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = NoLatestEtagTransport(tmp_path / "remote")
    _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.0.1",
    )
    with pytest.raises(DriverError) as error:
        publisher.fetch_chain_state(
            config=_config(tmp_path, version="0.0.2", genesis=None),
            transport=transport,
            signer=signer,
        )
    assert error.value.failures[0].error == "transparency pointer ETag is missing"


class MovingLatestTransport(DirectoryTransparencyTransport):
    latest_gets: int = 0

    def get_object(self, key: str, *, cache_bypass: bool = False):  # type: ignore[no-untyped-def]
        if key == latest_key(PRODUCT):
            self.latest_gets += 1
            if self.latest_gets == 2:
                self._object_path(key).write_bytes(b"moved")
        return super().get_object(key, cache_bypass=cache_bypass)


class NoLatestEtagTransport(DirectoryTransparencyTransport):
    def get_object(self, key: str, *, cache_bypass: bool = False):  # type: ignore[no-untyped-def]
        result = super().get_object(key, cache_bypass=cache_bypass)
        if key == latest_key(PRODUCT) and result.status == 200:
            return HttpResult(
                status=result.status,
                body=result.body,
                headers={},
                etag=None,
                exit_code=result.exit_code,
            )
        return result


class NthGetFailureTransport(DirectoryTransparencyTransport):
    def __init__(
        self,
        root: Path,
        *,
        fail_key: str,
        fail_on: int,
        status: int,
    ) -> None:
        super().__init__(root)
        self.fail_key = fail_key
        self.fail_on = fail_on
        self.status = status
        self.get_count = 0

    def get_object(self, key: str, *, cache_bypass: bool = False):  # type: ignore[no-untyped-def]
        if key == self.fail_key:
            self.get_count += 1
            if self.get_count == self.fail_on:
                self._record(
                    plane="s3",
                    op="GET",
                    key=key,
                    status=self.status,
                    cache_bypass=cache_bypass,
                )
                return HttpResult(
                    status=self.status,
                    body=b"forced failure",
                    headers={},
                    etag=None,
                    exit_code=0,
                )
        return super().get_object(key, cache_bypass=cache_bypass)


class NthPutFailureTransport(DirectoryTransparencyTransport):
    def __init__(
        self,
        root: Path,
        *,
        fail_key: str,
        fail_on: int,
        status: int,
    ) -> None:
        super().__init__(root)
        self.fail_key = fail_key
        self.fail_on = fail_on
        self.status = status
        self.put_count = 0

    def put_object(  # type: ignore[override]
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> HttpResult:
        if key == self.fail_key:
            self.put_count += 1
            if self.put_count == self.fail_on:
                self._record(
                    plane="s3",
                    op="PUT",
                    key=key,
                    status=self.status,
                    if_none_match=if_none_match,
                    if_match=if_match,
                )
                return HttpResult(
                    status=self.status,
                    body=b"forced failure",
                    headers={},
                    etag=None,
                    exit_code=7,
                )
        return super().put_object(
            key,
            body,
            content_type=content_type,
            cache_control=cache_control,
            if_none_match=if_none_match,
            if_match=if_match,
        )


class AmbiguousLatestPointerPutTransport(DirectoryTransparencyTransport):
    def __init__(self, root: Path, *, status: int = 500) -> None:
        super().__init__(root)
        self.status = status

    def put_object(  # type: ignore[override]
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> HttpResult:
        if key != latest_key(PRODUCT):
            return super().put_object(
                key,
                body,
                content_type=content_type,
                cache_control=cache_control,
                if_none_match=if_none_match,
                if_match=if_match,
            )
        committed = super().put_object(
            key,
            body,
            content_type=content_type,
            cache_control=cache_control,
            if_none_match=if_none_match,
            if_match=if_match,
        )
        if committed.status != 200:
            return committed
        self.call_log[-1]["status"] = self.status
        return HttpResult(
            status=self.status,
            body=b"ambiguous transfer",
            headers=committed.headers,
            etag=committed.etag,
            exit_code=7,
        )


def _prepare_existing_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transport: DirectoryTransparencyTransport,
) -> tuple[
    publisher.PublishConfig,
    FakeTransparencySigner,
    EntryRecord,
    publisher.StagedPublish,
]:
    _candidate(tmp_path, version="0.9.2")
    _patch_recover(monkeypatch, version="0.9.2")
    signer = FakeTransparencySigner()
    previous = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.9.1",
    )
    config = _config(tmp_path, version="0.9.2", genesis=None)
    state = publisher.fetch_chain_state(
        config=config,
        transport=transport,
        signer=signer,
    )
    stage = publisher.create_stage_from_candidate(
        config=config,
        state=state,
        signer=signer,
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    transport.call_log.clear()
    return config, signer, previous, stage


def _pointer_pair_state(
    tmp_path: Path,
    *,
    transport: DirectoryTransparencyTransport,
    signer: FakeTransparencySigner,
    previous_sha256: str,
    new_sha256: str,
) -> str:
    latest_result = transport.get_object(latest_key(PRODUCT), cache_bypass=True)
    if latest_result.status == 404:
        return "missing"
    signature_result = transport.get_object(
        latest_signature_key(PRODUCT),
        cache_bypass=True,
    )
    if latest_result.status != 200 or signature_result.status != 200:
        return "invalid"
    try:
        record = parse_latest_bytes(latest_result.body)
    except DriverError:
        return "invalid"
    latest_path = tmp_path / "crash-latest.json"
    signature_path = tmp_path / "crash-latest.json.minisig"
    latest_path.write_bytes(latest_result.body)
    signature_path.write_bytes(signature_result.body)
    try:
        signer.verify_file(
            latest_path,
            signature_path,
            expected_trusted_comment=latest_trusted_comment(record.pointer),
        )
    except DriverError:
        return "invalid"
    tip = str(record.pointer["tip_sha256"])
    if tip == previous_sha256:
        return "old"
    if tip == new_sha256:
        return "new"
    return "invalid"


@pytest.mark.parametrize(
    "seam",
    (
        "fetch-latest",
        "fetch-latest-signature",
        "fetch-list",
        "fetch-entry",
        "fetch-entry-signature",
        "fetch-ledger",
        "archive",
        "immutable-put",
        "immutable-public-get",
        "pre-pointer-refetch",
        "ledger-put",
        "ledger-public-get",
        "latest-signature-put",
        "latest-pointer-put",
        "latest-pointer-put-ambiguous-committed",
        "latest-pointer-put-412",
        "latest-signature-restore-put",
    ),
)
def test_crash_injection_classifies_pointer_pair_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    if seam == "pre-pointer-refetch":
        transport: DirectoryTransparencyTransport = NthGetFailureTransport(
            tmp_path / "remote",
            fail_key=latest_key(PRODUCT),
            fail_on=3,
            status=500,
        )
    elif seam == "latest-pointer-put-ambiguous-committed":
        transport = AmbiguousLatestPointerPutTransport(tmp_path / "remote")
    elif seam == "latest-signature-restore-put":
        transport = NthPutFailureTransport(
            tmp_path / "remote",
            fail_key=latest_signature_key(PRODUCT),
            fail_on=3,
            status=500,
        )
    else:
        transport = DirectoryTransparencyTransport(tmp_path / "remote")
    config, signer, previous, stage = _prepare_existing_publish(
        tmp_path,
        monkeypatch,
        transport=transport,
    )
    archive_runner = _archive_ok
    if seam == "fetch-latest":
        transport.add_failure(plane="s3", op="GET", key=latest_key(PRODUCT), status=500)
    elif seam == "fetch-latest-signature":
        transport.add_failure(
            plane="s3", op="GET", key=latest_signature_key(PRODUCT), status=500
        )
    elif seam == "fetch-list":
        transport.add_failure(
            plane="s3", op="LIST", key=f"releases/{PRODUCT}/v/", status=500
        )
    elif seam == "fetch-entry":
        transport.add_failure(
            plane="s3",
            op="GET",
            key=version_object_key(PRODUCT, "0.9.1", ENTRY_OBJECT_NAME),
            status=500,
        )
    elif seam == "fetch-entry-signature":
        transport.add_failure(
            plane="s3",
            op="GET",
            key=version_object_key(PRODUCT, "0.9.1", ENTRY_SIGNATURE_NAME),
            status=500,
        )
    elif seam == "fetch-ledger":
        transport.add_failure(plane="s3", op="GET", key=ledger_key(PRODUCT), status=500)
    elif seam == "archive":

        def archive_runner(_stage_dir: Path, _digest: str) -> str:
            raise DriverError(
                [
                    publisher.failure(
                        "transparency archive channel failed",
                        expected="exit 0",
                        actual="crash seam",
                        repair="retry after archive recovery",
                    )
                ]
            )

    elif seam == "immutable-put":
        transport.add_failure(
            plane="s3",
            op="PUT",
            key=version_object_key(PRODUCT, "0.9.2", ENTRY_OBJECT_NAME),
            status=500,
        )
    elif seam == "immutable-public-get":
        transport.add_failure(
            plane="public",
            op="GET",
            key=version_object_key(PRODUCT, "0.9.2", ENTRY_OBJECT_NAME),
            status=500,
        )
    elif seam == "ledger-put":
        transport.add_failure(plane="s3", op="PUT", key=ledger_key(PRODUCT), status=500)
    elif seam == "ledger-public-get":
        transport.add_failure(
            plane="public", op="GET", key=ledger_key(PRODUCT), status=500
        )
    elif seam == "latest-signature-put":
        transport.add_failure(
            plane="s3", op="PUT", key=latest_signature_key(PRODUCT), status=500
        )
    elif seam == "latest-pointer-put":
        transport.add_failure(plane="s3", op="PUT", key=latest_key(PRODUCT), status=500)
    elif seam == "latest-pointer-put-ambiguous-committed":
        pass
    elif seam == "latest-pointer-put-412":
        transport.add_failure(plane="s3", op="PUT", key=latest_key(PRODUCT), status=412)
    elif seam == "latest-signature-restore-put":
        transport.add_failure(plane="s3", op="PUT", key=latest_key(PRODUCT), status=500)

    if seam == "latest-pointer-put-ambiguous-committed":
        publisher.publish_transparency(
            config=config,
            transport=transport,
            signer=signer,
            archive_runner=archive_runner,
            now=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        )
        expected_states = {"new"}
    else:
        with pytest.raises(DriverError) as error:
            publisher.publish_transparency(
                config=config,
                transport=transport,
                signer=signer,
                archive_runner=archive_runner,
                now=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
            )
        if seam == "latest-signature-restore-put":
            failure = error.value.failures[0]
            assert (
                failure.error
                == "transparency latest pointer pair is torn after restore failure"
            )
            assert "latest.json=old latest.json.minisig=new" in failure.actual
            expected_states = {"invalid"}
        else:
            expected_states = {"old", "new"}
    assert (
        _pointer_pair_state(
            tmp_path,
            transport=transport,
            signer=signer,
            previous_sha256=previous.sha256,
            new_sha256=stage.entry_sha256,
        )
        in expected_states
    )
    if seam in {"latest-pointer-put", "latest-signature-restore-put"}:
        signature_puts = [
            call
            for call in transport.call_log
            if call["op"] == "PUT" and call["key"] == latest_signature_key(PRODUCT)
        ]
        assert signature_puts[-1]["if_match"] is not None


def test_pre_pointer_recheck_failure_stops_before_pointer_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path, version="0.9.2")
    _patch_recover(monkeypatch, version="0.9.2")
    signer = FakeTransparencySigner()
    transport = MovingLatestTransport(tmp_path / "remote")
    previous = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.9.1",
    )
    with pytest.raises(DriverError) as error:
        publisher.publish_transparency(
            config=_config(tmp_path, version="0.9.2", genesis=None),
            transport=transport,
            signer=signer,
            archive_runner=_archive_ok,
            now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        )
    assert (
        error.value.failures[0].error
        == "transparency latest pointer moved before pointer write"
    )
    latest_puts = [
        call
        for call in transport.call_log
        if call["op"] == "PUT" and call["key"] == latest_key(PRODUCT)
    ]
    assert latest_puts == []
    assert previous.sha256


def test_resign_pointer_preserves_chain_length_tip_and_version(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    previous = _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.9.1",
    )
    result = publisher.resign_transparency_pointer(
        config=_config(tmp_path, version="0.9.1", genesis=None),
        transport=transport,
        signer=signer,
        now=datetime(2026, 7, 24, 0, 0, tzinfo=UTC),
    )
    latest = parse_latest_bytes(transport.get_object(latest_key(PRODUCT)).body)
    assert result.seq == 1
    assert result.version == "0.9.1"
    assert result.entry_sha256 == previous.sha256
    assert latest.pointer["chain_length"] == 1
    assert latest.pointer["tip_sha256"] == previous.sha256
    assert latest.pointer["version"] == "0.9.1"
    assert latest.pointer["signed_at"] == "2026-07-24T00:00:00Z"
    assert latest.pointer["valid_until"] == "2026-08-07T00:00:00Z"


def test_resign_pointer_failure_restores_old_signature_conditionally(
    tmp_path: Path,
) -> None:
    signer = FakeTransparencySigner()
    transport = DirectoryTransparencyTransport(tmp_path / "remote")
    _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.9.1",
    )
    old_latest = transport.get_object(latest_key(PRODUCT)).body
    old_signature = transport.get_object(latest_signature_key(PRODUCT)).body
    transport.add_failure(plane="s3", op="PUT", key=latest_key(PRODUCT), status=500)

    with pytest.raises(DriverError):
        publisher.resign_transparency_pointer(
            config=_config(tmp_path, version="0.9.1", genesis=None),
            transport=transport,
            signer=signer,
            now=datetime(2026, 7, 24, 0, 0, tzinfo=UTC),
        )

    assert transport.get_object(latest_key(PRODUCT)).body == old_latest
    assert transport.get_object(latest_signature_key(PRODUCT)).body == old_signature
    signature_puts = [
        call
        for call in transport.call_log
        if call["op"] == "PUT" and call["key"] == latest_signature_key(PRODUCT)
    ]
    assert signature_puts[-1]["if_match"] is not None


def test_resign_pointer_ambiguous_committed_put_reports_success(tmp_path: Path) -> None:
    signer = FakeTransparencySigner()
    transport = AmbiguousLatestPointerPutTransport(tmp_path / "remote")
    _install_remote_entry(
        transport=transport,
        signer=signer,
        tmp_path=tmp_path,
        seq=1,
        version="0.9.1",
    )
    old_latest = transport.get_object(latest_key(PRODUCT)).body

    publisher.resign_transparency_pointer(
        config=_config(tmp_path, version="0.9.1", genesis=None),
        transport=transport,
        signer=signer,
        now=datetime(2026, 7, 24, 0, 0, tzinfo=UTC),
    )

    latest_result = transport.get_object(latest_key(PRODUCT))
    signature_result = transport.get_object(latest_signature_key(PRODUCT))
    assert latest_result.body != old_latest
    latest = parse_latest_bytes(latest_result.body)
    latest_path = tmp_path / "resigned-latest.json"
    signature_path = tmp_path / "resigned-latest.json.minisig"
    latest_path.write_bytes(latest_result.body)
    signature_path.write_bytes(signature_result.body)
    signer.verify_file(
        latest_path,
        signature_path,
        expected_trusted_comment=latest_trusted_comment(latest.pointer),
    )
