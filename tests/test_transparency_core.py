from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path

import pytest

from scripts.release_candidate_driver import DriverError
from scripts.transparency_core import (
    ENTRY_KEYS,
    ENTRY_SCHEMA,
    LATEST_KEYS,
    LATEST_SCHEMA,
    PRODUCT,
    PUBLIC_TRUST_ANCHOR_FILENAME,
    PUBLIC_TRUST_ANCHOR_PATH,
    ZERO_SHA256,
    EntryRecord,
    build_latest_pointer,
    build_ledger_entry,
    canonical_json_bytes,
    entry_trusted_comment,
    latest_trusted_comment,
    parse_ledger_entry_bytes,
    parse_ledger_jsonl,
    snapshot_candidate,
    validate_entry_chain,
    validate_entry_trusted_comment,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transparency"
SCHEMAS = Path(__file__).parents[1] / "schemas"
ENTRY_SHA = "30fa37a5d4a1b254e695339b1b0dcaa7a481bb26cca92dfd888f8186f049599f"
LATEST_SHA = "598d1e2acd1765b6ab3bf7ebf915efe9077cb869ed6d67d39c4262de512d9061"
ENTRY_SCHEMA_SHA = "b4889cc7195e13a32a76041349103c3829b19a363d49f27e0df62cbf65fb9476"
LATEST_SCHEMA_SHA = "46e655f17170105f73c5f1183e976d2100198bbeb16818d2e666bd6e4630b9a2"


def test_public_trust_anchor_constants_match_contract() -> None:
    assert PUBLIC_TRUST_ANCHOR_FILENAME == "solpbc-transparency-1.pub"
    assert PUBLIC_TRUST_ANCHOR_PATH == "releases/keys/solpbc-transparency-1.pub"


def test_vendored_transparency_schemas_are_pinned() -> None:
    entry_bytes = (SCHEMAS / "transparency-ledger-entry" / "v1.json").read_bytes()
    latest_bytes = (SCHEMAS / "transparency-latest" / "v1.json").read_bytes()
    assert hashlib.sha256(entry_bytes).hexdigest() == ENTRY_SCHEMA_SHA
    assert len(entry_bytes) == 2805
    assert hashlib.sha256(latest_bytes).hexdigest() == LATEST_SCHEMA_SHA
    assert len(latest_bytes) == 1140

    entry_schema = json.loads(entry_bytes)
    latest_schema = json.loads(latest_bytes)
    assert entry_schema["$id"] == ENTRY_SCHEMA
    assert set(entry_schema["required"]) == ENTRY_KEYS
    assert set(entry_schema["properties"]) == ENTRY_KEYS
    assert latest_schema["$id"] == LATEST_SCHEMA
    assert set(latest_schema["required"]) == LATEST_KEYS
    assert set(latest_schema["properties"]) == LATEST_KEYS


def _reverse_order_entry_vector() -> OrderedDict[str, object]:
    return OrderedDict(
        (
            ("version", "0.0.1"),
            ("source_commit", "0123456789abcdef0123456789abcdef01234567"),
            ("seq", 1),
            ("schema", ENTRY_SCHEMA),
            ("published_utc", "2026-07-22T00:00:00Z"),
            ("proofs", []),
            ("product", "example"),
            ("prev_version", ""),
            ("prev_sha256", "0" * 64),
            (
                "manifests",
                [
                    OrderedDict(
                        (
                            ("sha256", "cd" * 32),
                            ("name", "example-0.0.1.rust-release-manifest.json"),
                        )
                    )
                ],
            ),
            (
                "artifacts",
                [
                    OrderedDict(
                        (
                            ("sha256", "ab" * 32),
                            ("name", "example-0.0.1.tar.gz"),
                            ("bytes", 100000000),
                        )
                    )
                ],
            ),
        )
    )


def _reverse_order_pointer_vector() -> OrderedDict[str, object]:
    return OrderedDict(
        (
            ("version", "0.0.1"),
            ("valid_until", "2026-08-05T00:00:00Z"),
            ("tip_sha256", ENTRY_SHA),
            ("signed_at", "2026-07-22T00:00:00Z"),
            ("schema", LATEST_SCHEMA),
            ("product", "example"),
            ("chain_length", 1),
        )
    )


def test_canonical_entry_vector_matches_committed_bytes() -> None:
    payload = canonical_json_bytes(_reverse_order_entry_vector(), label="entry vector")
    assert payload == (FIXTURES / "canonical-entry-v1.json").read_bytes()
    assert len(payload) == 611
    assert hashlib.sha256(payload).hexdigest() == ENTRY_SHA


def test_canonical_latest_vector_matches_committed_bytes() -> None:
    payload = canonical_json_bytes(
        _reverse_order_pointer_vector(), label="latest vector"
    )
    assert payload == (FIXTURES / "canonical-latest-v1.json").read_bytes()
    assert len(payload) == 275
    assert hashlib.sha256(payload).hexdigest() == LATEST_SHA


def test_canonicalization_rejects_non_ascii_before_serialization() -> None:
    with pytest.raises(DriverError) as error:
        canonical_json_bytes({"name": "caf\u00e9"}, label="entry")
    assert error.value.failures[0].error == "entry.name contains a non-ASCII string"


def test_canonicalization_rejects_float_values() -> None:
    with pytest.raises(DriverError) as error:
        canonical_json_bytes({"bytes": 1.0}, label="entry")
    assert error.value.failures[0].error == "entry.bytes contains a float"


def test_canonicalization_rejects_bool_values() -> None:
    with pytest.raises(DriverError) as error:
        canonical_json_bytes({"seq": True}, label="entry")
    assert error.value.failures[0].error == "entry.seq contains a boolean"


def _entry(seq: int, *, prev: str = ZERO_SHA256, prev_version: str = "") -> EntryRecord:
    entry = build_ledger_entry(
        artifacts=[],
        manifests=[],
        proofs=[],
        prev_sha256=prev,
        prev_version=prev_version,
        product=PRODUCT,
        published_utc=f"2026-07-22T00:00:0{seq}Z",
        seq=seq,
        source_commit="a" * 40,
        version=f"0.0.{seq}",
    )
    data = canonical_json_bytes(entry)
    return EntryRecord(entry=entry, bytes=data, sha256=hashlib.sha256(data).hexdigest())


def test_entry_trusted_comment_fixture_matches_contract() -> None:
    entry = json.loads(
        (FIXTURES / "canonical-entry-v1.json").read_text(encoding="utf-8")
    )
    assert (
        entry_trusted_comment(entry, ENTRY_SHA)
        == (FIXTURES / "entry-trusted-comment.txt").read_text(encoding="utf-8").strip()
    )
    pointer = json.loads(
        (FIXTURES / "canonical-latest-v1.json").read_text(encoding="utf-8")
    )
    assert (
        latest_trusted_comment(pointer)
        == (FIXTURES / "latest-trusted-comment.txt").read_text(encoding="utf-8").strip()
    )


def test_trusted_comment_body_mismatch_fails_semantically() -> None:
    record = _entry(6)
    mismatched = entry_trusted_comment({**record.entry, "seq": 5}, record.sha256)
    failures = validate_entry_trusted_comment(
        mismatched,
        entry=record.entry,
        entry_sha256=record.sha256,
    )
    assert failures[0].error == "transparency entry trusted comment does not match body"


def test_parse_entry_rejects_tampered_bytes() -> None:
    record = _entry(1)
    tampered = record.bytes.replace(b'"version":"0.0.1"', b'"version":"0.0.2"')
    parsed = parse_ledger_entry_bytes(tampered)
    assert parsed.sha256 != record.sha256


def test_chain_rejects_broken_prev_linkage() -> None:
    first = _entry(1)
    second = _entry(2, prev="f" * 64, prev_version="0.0.1")
    failures = validate_entry_chain((first, second))
    assert failures[0].error == "transparency ledger prev_sha256 linkage is broken"


def test_chain_rejects_gapped_seq() -> None:
    first = _entry(1)
    third = _entry(3, prev=first.sha256, prev_version="0.0.1")
    failures = validate_entry_chain((first, third))
    assert failures[0].error == "transparency ledger seq is non-monotonic or gapped"


def test_parse_jsonl_requires_trailing_newline() -> None:
    record = _entry(1)
    with pytest.raises(DriverError) as error:
        parse_ledger_jsonl(record.bytes.rstrip(b"\n"))
    assert (
        error.value.failures[0].error
        == "transparency ledger.jsonl line is not newline-terminated"
    )


def test_snapshot_candidate_dangling_symlink_fails_closed(tmp_path: Path) -> None:
    version = "0.9.1"
    release_dir = tmp_path / "dist" / "release-candidate" / version
    evidence_dir = tmp_path / "target" / "release-evidence" / version
    release_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)
    (release_dir / "dangling").symlink_to(tmp_path / "missing")
    (evidence_dir / "ledger.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(DriverError) as error:
        snapshot_candidate(
            source_root=tmp_path,
            snapshot_root=tmp_path / "snapshot",
            version=version,
        )

    failure = error.value.failures[0]
    assert failure.error == "transparency snapshot copy failed"
    assert str(release_dir / "dangling") in failure.actual


def test_latest_pointer_rejects_bool_numeric_field() -> None:
    with pytest.raises(DriverError) as error:
        build_latest_pointer(
            chain_length=True,
            product=PRODUCT,
            signed_at="2026-07-22T00:00:00Z",
            tip_sha256="a" * 64,
            valid_until="2026-08-05T00:00:00Z",
            version="0.0.1",
        )
    assert error.value.failures[0].error == "latest pointer chain_length is invalid"


def test_entry_rejects_offset_published_utc() -> None:
    with pytest.raises(DriverError) as error:
        build_ledger_entry(
            artifacts=[],
            manifests=[],
            proofs=[],
            prev_sha256=ZERO_SHA256,
            prev_version="",
            product=PRODUCT,
            published_utc="2026-07-22T00:00:00+00:00",
            seq=1,
            source_commit="a" * 40,
            version="0.0.1",
        )
    assert error.value.failures[0].error == "ledger entry published_utc is malformed"


def test_latest_rejects_fractional_published_utc() -> None:
    with pytest.raises(DriverError) as error:
        build_latest_pointer(
            chain_length=1,
            product=PRODUCT,
            signed_at="2026-07-22T00:00:00.000Z",
            tip_sha256="a" * 64,
            valid_until="2026-08-05T00:00:00Z",
            version="0.0.1",
        )
    assert error.value.failures[0].error == "latest pointer signed_at is malformed"


def test_chain_rejects_published_utc_not_later_than_tip() -> None:
    first = _entry(1)
    second_entry = build_ledger_entry(
        artifacts=[],
        manifests=[],
        proofs=[],
        prev_sha256=first.sha256,
        prev_version="0.0.1",
        product=PRODUCT,
        published_utc="2026-07-22T00:00:01Z",
        seq=2,
        source_commit="a" * 40,
        version="0.0.2",
    )
    second_bytes = canonical_json_bytes(second_entry)
    second = EntryRecord(
        entry=second_entry,
        bytes=second_bytes,
        sha256=hashlib.sha256(second_bytes).hexdigest(),
    )
    failures = validate_entry_chain((first, second))
    assert (
        failures[0].error
        == "transparency ledger published_utc is not strictly increasing"
    )
