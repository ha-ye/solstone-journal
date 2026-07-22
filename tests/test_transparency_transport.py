from __future__ import annotations

from pathlib import Path

from scripts.transparency_transport import DirectoryTransparencyTransport


def test_directory_transport_create_only_conflict_returns_412_exit_zero(
    tmp_path: Path,
) -> None:
    transport = DirectoryTransparencyTransport(tmp_path)
    first = transport.put_object(
        "releases/solstone-journal/v/0.0.1/ledger-entry.json",
        b"first",
        content_type="application/json",
        cache_control="immutable",
        if_none_match=True,
    )
    second = transport.put_object(
        "releases/solstone-journal/v/0.0.1/ledger-entry.json",
        b"second",
        content_type="application/json",
        cache_control="immutable",
        if_none_match=True,
    )
    assert first.status == 200
    assert second.status == 412
    assert second.exit_code == 0
    assert second.body == b"precondition failed"
    assert (
        transport.get_object("releases/solstone-journal/v/0.0.1/ledger-entry.json").body
        == b"first"
    )


def test_directory_transport_if_match_uses_captured_etag(tmp_path: Path) -> None:
    transport = DirectoryTransparencyTransport(tmp_path)
    key = "releases/solstone-journal/latest.json"
    first = transport.put_object(
        key,
        b"old",
        content_type="application/json",
        cache_control="no-cache",
    )
    rejected = transport.put_object(
        key,
        b"new",
        content_type="application/json",
        cache_control="no-cache",
        if_match='"wrong"',
    )
    accepted = transport.put_object(
        key,
        b"new",
        content_type="application/json",
        cache_control="no-cache",
        if_match=first.etag,
    )
    assert rejected.status == 412
    assert rejected.exit_code == 0
    assert accepted.status == 200
    assert transport.get_object(key).body == b"new"


def test_directory_transport_forbidden_returns_403_with_body_exit_zero(
    tmp_path: Path,
) -> None:
    transport = DirectoryTransparencyTransport(tmp_path)
    key = "releases/solstone-journal/latest.json"
    transport.forbidden.add(key)
    result = transport.put_object(
        key,
        b"body",
        content_type="application/json",
        cache_control="no-cache",
    )
    assert result.status == 403
    assert result.body == b"forbidden"
    assert result.exit_code == 0


def test_directory_transport_public_get_reads_same_object_store(tmp_path: Path) -> None:
    transport = DirectoryTransparencyTransport(tmp_path)
    key = "releases/solstone-journal/v/0.0.1/ledger-entry.json"
    transport.put_object(
        key,
        b"entry",
        content_type="application/json",
        cache_control="immutable",
    )
    public = transport.get_public(key, cache_bypass=True)
    assert public.status == 200
    assert public.body == b"entry"


def test_directory_transport_records_exact_destination_set(tmp_path: Path) -> None:
    transport = DirectoryTransparencyTransport(
        tmp_path,
        endpoint="https://r2.example.invalid",
        bucket="transparency-test",
        public_base_url="https://transparency.solstone.app",
    )
    key = "releases/solstone-journal/v/0.0.1/ledger-entry.json"
    transport.put_object(
        key,
        b"entry",
        content_type="application/json",
        cache_control="immutable",
    )
    transport.get_public(key)
    assert transport.destination_set == {
        "https://r2.example.invalid/transparency-test",
        "https://transparency.solstone.app",
    }
    assert [
        {"plane": call["plane"], "op": call["op"], "key": call["key"]}
        for call in transport.call_log
    ] == [
        {"plane": "s3", "op": "PUT", "key": key},
        {"plane": "public", "op": "GET", "key": key},
    ]
