from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.transparency_transport import (
    CurlResult,
    CurlTransparencyTransport,
    DirectoryTransparencyTransport,
    parse_curl_write_out,
)


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


def test_curl_write_out_parser_extracts_status_and_etag() -> None:
    assert parse_curl_write_out('200\t"abc123"\n') == (200, '"abc123"')
    assert parse_curl_write_out("204\t\n") == (204, None)


def test_curl_run_preserves_exit_code_without_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["curl"],
            returncode=7,
            stdout="000\t\n",
            stderr="curl: failed to connect",
        )

    monkeypatch.setattr("scripts.transparency_transport.subprocess.run", run)
    transport = CurlTransparencyTransport(
        endpoint="https://r2.example.invalid",
        bucket="transparency-test",
        access_key_id="key",
        secret_access_key="secret",
        base_url="https://transparency.solstone.app",
    )
    result = transport._run_curl([], body_output=tmp_path / "body")
    assert result.status == 0
    assert result.exit_code == 7
    assert result.body == b"curl: failed to connect"


def test_curl_transport_repr_does_not_expose_secret() -> None:
    transport = CurlTransparencyTransport(
        endpoint="https://r2.example.invalid",
        bucket="transparency-test",
        access_key_id="key",
        secret_access_key="SECRET",
        base_url="https://transparency.solstone.app",
    )
    assert "SECRET" not in repr(transport)


def test_curl_list_prefix_parses_namespaced_s3_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>transparency-test</Name>
  <Prefix>releases/solstone-journal/v/</Prefix>
  <IsTruncated>false</IsTruncated>
  <Contents><Key>releases/solstone-journal/v/0.0.1/ledger-entry.json</Key></Contents>
  <Contents><Key>releases/solstone-journal/v/0.0.1/ledger-entry.json.minisig</Key></Contents>
</ListBucketResult>
"""

    transport = CurlTransparencyTransport(
        endpoint="https://r2.example.invalid",
        bucket="transparency-test",
        access_key_id="key",
        secret_access_key="secret",
        base_url="https://transparency.solstone.app",
    )

    def run(
        _self: CurlTransparencyTransport,
        _args: object,
        *,
        body_output: Path,
    ) -> CurlResult:
        return CurlResult(status=200, body=body, etag=None, exit_code=0)

    monkeypatch.setattr(CurlTransparencyTransport, "_run_curl", run)
    result = transport.list_prefix("releases/solstone-journal/v/")
    assert result.status == 200
    assert result.keys == (
        "releases/solstone-journal/v/0.0.1/ledger-entry.json",
        "releases/solstone-journal/v/0.0.1/ledger-entry.json.minisig",
    )


def test_curl_list_prefix_fails_closed_on_truncated_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>next</NextContinuationToken>
  <Contents><Key>releases/solstone-journal/v/0.0.1/ledger-entry.json</Key></Contents>
</ListBucketResult>
"""
    transport = CurlTransparencyTransport(
        endpoint="https://r2.example.invalid",
        bucket="transparency-test",
        access_key_id="key",
        secret_access_key="secret",
        base_url="https://transparency.solstone.app",
    )

    def run(
        _self: CurlTransparencyTransport,
        _args: object,
        *,
        body_output: Path,
    ) -> CurlResult:
        return CurlResult(status=200, body=body, etag=None, exit_code=0)

    monkeypatch.setattr(CurlTransparencyTransport, "_run_curl", run)
    result = transport.list_prefix("releases/solstone-journal/v/")
    assert result.status == 0
    assert result.keys == ()
    assert result.body == b"truncated S3 ListObjectsV2 response"
