# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
import logging
import socket
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import pytest

from solstone.think.backup import s3_wipe

ENDPOINT = "https://r2.example"
BUCKET = "journal-backups"
PREFIX = "users/acct/inst/"
ACCESS_KEY = "AKIDSECRET"
SECRET_KEY = "SECRETKEYVALUE"
SESSION_TOKEN = "SESSIONTOKENVALUE"
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


@dataclass(frozen=True)
class RequestRecord:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    timeout: float


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        return False

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    def __init__(self, items: list[Any]) -> None:
        self.items = list(items)
        self.records: list[RequestRecord] = []

    def __call__(self, request, timeout: float):
        _ = timeout
        self.records.append(
            RequestRecord(
                method=request.get_method(),
                url=request.full_url,
                headers={key.lower(): value for key, value in request.header_items()},
                body=request.data or b"",
                timeout=timeout,
            )
        )
        if not self.items:
            raise AssertionError("unexpected S3 request")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _xml(tag: str, body: str = "") -> bytes:
    return f'<{tag} xmlns="{S3_NS}">{body}</{tag}>'.encode("utf-8")


def _list_objects(
    keys: list[str],
    *,
    truncated: bool = False,
    token: str | None = None,
) -> bytes:
    contents = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in keys)
    trailer = f"<IsTruncated>{str(truncated).lower()}</IsTruncated>"
    if token is not None:
        trailer = f"{trailer}<NextContinuationToken>{token}</NextContinuationToken>"
    return _xml("ListBucketResult", contents + trailer)


def _list_uploads(
    uploads: list[tuple[str, str]],
    *,
    truncated: bool = False,
    next_key: str | None = None,
    next_upload: str | None = None,
) -> bytes:
    body = "".join(
        f"<Upload><Key>{key}</Key><UploadId>{upload_id}</UploadId></Upload>"
        for key, upload_id in uploads
    )
    body = f"{body}<IsTruncated>{str(truncated).lower()}</IsTruncated>"
    if next_key is not None:
        body = f"{body}<NextKeyMarker>{next_key}</NextKeyMarker>"
    if next_upload is not None:
        body = f"{body}<NextUploadIdMarker>{next_upload}</NextUploadIdMarker>"
    return _xml("ListMultipartUploadsResult", body)


def _delete_result(errors: list[str] | None = None) -> bytes:
    body = ""
    for code in errors or []:
        body = f"{body}<Error><Key>key</Key><Code>{code}</Code></Error>"
    return _xml("DeleteResult", body)


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"{ENDPOINT}/{BUCKET}",
        status,
        "error",
        hdrs=None,
        fp=io.BytesIO(b""),
    )


def _wipe(opener: FakeOpener | Any) -> s3_wipe.WipeResult:
    return s3_wipe.wipe_prefix(
        endpoint=ENDPOINT,
        bucket=BUCKET,
        prefix=PREFIX,
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        session_token=SESSION_TOKEN,
        opener=opener,
        timeout=1,
    )


def _object_count(body: bytes) -> int:
    return len(ET.fromstring(body).findall("Object"))


def test_signed_headers_include_sts_token_on_every_request() -> None:
    opener = FakeOpener(
        [
            FakeResponse(200, _list_objects([])),
            FakeResponse(200, _list_uploads([])),
        ]
    )

    result = _wipe(opener)

    assert result == s3_wipe.WipeResult("ok", None)
    for record in opener.records:
        assert record.headers["x-amz-security-token"] == SESSION_TOKEN
        assert "x-amz-security-token" in record.headers["authorization"]
        assert record.headers["authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential="
        )
        assert "x-amz-date" in record.headers
        assert "x-amz-content-sha256" in record.headers


def test_populated_prefix_deletes_objects_then_aborts_uploads() -> None:
    keys = [f"{PREFIX}object-{index}" for index in range(1001)]
    opener = FakeOpener(
        [
            FakeResponse(200, _list_objects(keys)),
            FakeResponse(200, _delete_result()),
            FakeResponse(200, _delete_result()),
            FakeResponse(200, _list_uploads([(f"{PREFIX}multipart", "upload-1")])),
            FakeResponse(204),
        ]
    )

    result = _wipe(opener)

    assert result == s3_wipe.WipeResult("ok", None)
    assert [record.method for record in opener.records] == [
        "GET",
        "POST",
        "POST",
        "GET",
        "DELETE",
    ]
    assert _object_count(opener.records[1].body) == 1000
    assert _object_count(opener.records[2].body) == 1
    assert "delete=" in opener.records[1].url
    assert "Content-md5".lower() in opener.records[1].headers
    assert "uploadId=upload-1" in opener.records[4].url


def test_wipe_prefix_lists_uploads_only_after_object_deletes() -> None:
    opener = FakeOpener(
        [
            FakeResponse(200, _list_objects([f"{PREFIX}object"])),
            FakeResponse(200, _delete_result()),
            FakeResponse(200, _list_uploads([(f"{PREFIX}multipart", "upload-1")])),
            FakeResponse(204),
        ]
    )

    result = _wipe(opener)

    assert result == s3_wipe.WipeResult("ok", None)
    assert [record.method for record in opener.records] == [
        "GET",
        "POST",
        "GET",
        "DELETE",
    ]
    assert "delete=" in opener.records[1].url
    assert "uploads=" in opener.records[2].url


def test_list_prefix_contents_reads_objects_and_uploads_without_mutation() -> None:
    opener = FakeOpener(
        [
            FakeResponse(
                200,
                _list_objects([f"{PREFIX}a"], truncated=True, token="next"),
            ),
            FakeResponse(200, _list_objects([f"{PREFIX}b"])),
            FakeResponse(
                200,
                _list_uploads([(f"{PREFIX}upload", "upload-1")]),
            ),
        ]
    )

    keys, uploads = s3_wipe.list_prefix_contents(
        endpoint=ENDPOINT,
        bucket=BUCKET,
        prefix=PREFIX,
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        session_token=SESSION_TOKEN,
        opener=opener,
        timeout=7,
        budget_s=30,
    )

    assert keys == (f"{PREFIX}a", f"{PREFIX}b")
    assert uploads == ((f"{PREFIX}upload", "upload-1"),)
    assert [record.method for record in opener.records] == ["GET", "GET", "GET"]
    assert all(record.timeout <= 7 for record in opener.records)


def test_list_prefix_contents_propagates_ambiguous_pagination() -> None:
    opener = FakeOpener([FakeResponse(200, _list_objects([], truncated=True))])

    with pytest.raises(Exception, match="failed"):
        s3_wipe.list_prefix_contents(
            endpoint=ENDPOINT,
            bucket=BUCKET,
            prefix=PREFIX,
            access_key_id=ACCESS_KEY,
            secret_access_key=SECRET_KEY,
            session_token=SESSION_TOKEN,
            opener=opener,
            timeout=1,
        )


def test_list_objects_paginates_with_continuation_token() -> None:
    opener = FakeOpener(
        [
            FakeResponse(
                200, _list_objects([f"{PREFIX}a"], truncated=True, token="T2")
            ),
            FakeResponse(200, _list_objects([f"{PREFIX}b"])),
            FakeResponse(200, _delete_result()),
            FakeResponse(200, _list_uploads([])),
        ]
    )

    result = _wipe(opener)

    assert result.status == "ok"
    assert [record.method for record in opener.records] == ["GET", "GET", "POST", "GET"]
    assert "continuation-token=T2" in opener.records[1].url


def test_multipart_upload_listing_paginates_with_markers() -> None:
    opener = FakeOpener(
        [
            FakeResponse(200, _list_objects([])),
            FakeResponse(
                200,
                _list_uploads(
                    [(f"{PREFIX}upload-a", "upload-a")],
                    truncated=True,
                    next_key=f"{PREFIX}next",
                    next_upload="next-upload",
                ),
            ),
            FakeResponse(200, _list_uploads([(f"{PREFIX}upload-b", "upload-b")])),
            FakeResponse(204),
            FakeResponse(204),
        ]
    )

    result = _wipe(opener)

    assert result.status == "ok"
    assert [record.method for record in opener.records] == [
        "GET",
        "GET",
        "GET",
        "DELETE",
        "DELETE",
    ]
    assert "key-marker=users%2Facct%2Finst%2Fnext" in opener.records[2].url
    assert "upload-id-marker=next-upload" in opener.records[2].url


def test_empty_prefix_is_success_without_delete_or_abort() -> None:
    opener = FakeOpener(
        [
            FakeResponse(200, _list_objects([])),
            FakeResponse(200, _list_uploads([])),
        ]
    )

    result = _wipe(opener)

    assert result == s3_wipe.WipeResult("ok", None)
    assert [record.method for record in opener.records] == ["GET", "GET"]


def test_delete_objects_per_object_access_denied_is_failed() -> None:
    opener = FakeOpener(
        [
            FakeResponse(200, _list_objects([f"{PREFIX}object"])),
            FakeResponse(200, _delete_result(["AccessDenied"])),
        ]
    )

    result = _wipe(opener)

    assert result == s3_wipe.WipeResult("error", "failed")


def test_delete_objects_only_no_such_key_is_idempotent_success() -> None:
    opener = FakeOpener(
        [
            FakeResponse(200, _list_objects([f"{PREFIX}object"])),
            FakeResponse(200, _delete_result(["NoSuchKey", "NoSuchKey"])),
            FakeResponse(200, _list_uploads([])),
        ]
    )

    result = _wipe(opener)

    assert result == s3_wipe.WipeResult("ok", None)


def test_error_mapping() -> None:
    cases = [
        (_http_error(403), "auth_failed"),
        (_http_error(500), "failed"),
        (urllib.error.URLError("down"), "unreachable"),
        (socket.timeout("slow"), "timeout"),
        (FakeResponse(200, b"<not-xml"), "failed"),
    ]

    for item, reason_code in cases:
        opener = FakeOpener([item])

        assert _wipe(opener) == s3_wipe.WipeResult("error", reason_code)


def test_wipe_result_and_logs_are_secret_free(
    caplog,
) -> None:
    opener = FakeOpener([_http_error(403)])
    caplog.set_level(logging.WARNING, logger="solstone.backup.s3_wipe")

    result = _wipe(opener)
    serialized = json.dumps(result.__dict__)

    for secret in (ACCESS_KEY, SECRET_KEY, SESSION_TOKEN):
        assert secret not in serialized
        assert secret not in repr(result)
        assert secret not in caplog.text
