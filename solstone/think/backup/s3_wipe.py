# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Minimal SigV4 S3 prefix wipe client for operated backup teardown."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from solstone import __version__ as solstone_version

logger = logging.getLogger("solstone.backup.s3_wipe")

S3_WIPE_TIMEOUT_SECONDS = 60
DELETE_OBJECT_BATCH_SIZE = 1000
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class WipeResult:
    status: str
    reason_code: str | None


@dataclass(frozen=True)
class _Upload:
    key: str
    upload_id: str


class _WipeFailure(RuntimeError):
    def __init__(self, reason_code: str, *, http_status: int | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.http_status = http_status


def _http_reason(status: int) -> str:
    if status in {401, 403}:
        return "auth_failed"
    if status in {408, 504}:
        return "timeout"
    return "failed"


def _is_timeout_urlerror(exc: urllib.error.URLError) -> bool:
    return isinstance(exc.reason, (socket.timeout, TimeoutError))


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="~")


def _canonical_uri(bucket: str, key: str | None = None) -> str:
    segments = [bucket]
    if key is not None:
        segments.extend(key.split("/"))
    return "/" + "/".join(_quote(segment) for segment in segments)


def _canonical_query(params: list[tuple[str, str]]) -> str:
    return "&".join(f"{_quote(name)}={_quote(value)}" for name, value in sorted(params))


def _signing_key(secret_access_key: str, date: str, region: str) -> bytes:
    date_key = hmac.new(
        f"AWS4{secret_access_key}".encode("utf-8"),
        date.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _normalize_header_value(value: str) -> str:
    return " ".join(value.strip().split())


def _authorization_header(
    *,
    method: str,
    canonical_uri: str,
    canonical_query: str,
    headers: dict[str, str],
    payload_hash: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    now: datetime,
) -> str:
    lowered = {key.lower(): value for key, value in headers.items()}
    signed_names = sorted(
        name
        for name in lowered
        if name
        in {
            "content-md5",
            "host",
            "x-amz-content-sha256",
            "x-amz-date",
            "x-amz-security-token",
        }
    )
    canonical_headers = "".join(
        f"{name}:{_normalize_header_value(lowered[name])}\n" for name in signed_names
    )
    signed_headers = ";".join(signed_names)
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    credential_scope = f"{date}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_access_key, date, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )


def _xml_root(raw_body: bytes) -> ET.Element:
    try:
        return ET.fromstring(raw_body)
    except ET.ParseError as exc:
        raise _WipeFailure("failed") from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _child_text(element: ET.Element, name: str) -> str | None:
    matches = _children(element, name)
    if not matches:
        return None
    value = matches[0].text
    return value if value is not None else ""


def _is_true(value: str | None) -> bool:
    return isinstance(value, str) and value.lower() == "true"


def _delete_body(keys: list[str]) -> bytes:
    root = ET.Element("Delete")
    for key in keys:
        item = ET.SubElement(root, "Object")
        ET.SubElement(item, "Key").text = key
    return ET.tostring(root, encoding="utf-8")


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


class _S3Client:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key_id: str,
        secret_access_key: str,
        session_token: str,
        region: str,
        opener: Callable[..., object],
        timeout: float,
    ) -> None:
        parsed = urllib.parse.urlparse(endpoint.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _WipeFailure("failed")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.host = parsed.netloc
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.session_token = session_token
        self.region = region
        self.opener = opener
        self.timeout = timeout

    def request(
        self,
        method: str,
        *,
        bucket: str,
        key: str | None = None,
        query: list[tuple[str, str]] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> bytes:
        expected = expected_statuses or {200}
        params = query or []
        canonical_uri = _canonical_uri(bucket, key)
        canonical_query = _canonical_query(params)
        url = f"{self.base_url}{canonical_uri}"
        if canonical_query:
            url = f"{url}?{canonical_query}"

        now = datetime.now(UTC)
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        headers = {
            "Host": self.host,
            "User-Agent": f"solstone-backup/{solstone_version}",
            "Connection": "close",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "x-amz-security-token": self.session_token,
        }
        if extra_headers:
            headers.update(extra_headers)
        headers["Authorization"] = _authorization_header(
            method=method,
            canonical_uri=canonical_uri,
            canonical_query=canonical_query,
            headers=headers,
            payload_hash=payload_hash,
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            region=self.region,
            now=now,
        )

        request = urllib.request.Request(
            url,
            data=body if body else None,
            headers=headers,
            method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            raise _WipeFailure(
                _http_reason(int(exc.code)),
                http_status=int(exc.code),
            ) from None
        except ssl.SSLError as exc:
            raise _WipeFailure("unreachable") from exc
        except urllib.error.URLError as exc:
            if _is_timeout_urlerror(exc):
                raise _WipeFailure("timeout") from exc
            raise _WipeFailure("unreachable") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise _WipeFailure("timeout") from exc

        if status not in expected:
            raise _WipeFailure(_http_reason(status), http_status=status)
        return raw_body

    def list_objects(self, *, bucket: str, prefix: str) -> list[str]:
        keys: list[str] = []
        continuation: str | None = None
        while True:
            query = [("list-type", "2"), ("prefix", prefix)]
            if continuation is not None:
                query.append(("continuation-token", continuation))
            root = _xml_root(self.request("GET", bucket=bucket, query=query))
            for contents in _children(root, "Contents"):
                key = _child_text(contents, "Key")
                if not key:
                    raise _WipeFailure("failed")
                keys.append(key)

            if not _is_true(_child_text(root, "IsTruncated")):
                return keys
            continuation = _child_text(root, "NextContinuationToken")
            if not continuation:
                raise _WipeFailure("failed")

    def delete_objects(self, *, bucket: str, keys: list[str]) -> None:
        body = _delete_body(keys)
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
        raw_body = self.request(
            "POST",
            bucket=bucket,
            query=[("delete", "")],
            body=body,
            extra_headers={"Content-MD5": content_md5},
        )
        root = _xml_root(raw_body)
        errors = _children(root, "Error")
        if not errors:
            return
        for error in errors:
            if _child_text(error, "Code") != "NoSuchKey":
                raise _WipeFailure("failed")

    def list_uploads(self, *, bucket: str, prefix: str) -> list[_Upload]:
        uploads: list[_Upload] = []
        key_marker: str | None = None
        upload_marker: str | None = None
        while True:
            query = [("uploads", ""), ("prefix", prefix)]
            if key_marker is not None:
                query.append(("key-marker", key_marker))
            if upload_marker is not None:
                query.append(("upload-id-marker", upload_marker))
            root = _xml_root(self.request("GET", bucket=bucket, query=query))
            for upload in _children(root, "Upload"):
                key = _child_text(upload, "Key")
                upload_id = _child_text(upload, "UploadId")
                if not key or not upload_id:
                    raise _WipeFailure("failed")
                uploads.append(_Upload(key=key, upload_id=upload_id))

            if not _is_true(_child_text(root, "IsTruncated")):
                return uploads
            key_marker = _child_text(root, "NextKeyMarker")
            upload_marker = _child_text(root, "NextUploadIdMarker")
            if not key_marker or not upload_marker:
                raise _WipeFailure("failed")

    def abort_upload(self, *, bucket: str, upload: _Upload) -> None:
        self.request(
            "DELETE",
            bucket=bucket,
            key=upload.key,
            query=[("uploadId", upload.upload_id)],
            expected_statuses={200, 204},
        )


def wipe_prefix(
    *,
    endpoint: str,
    bucket: str,
    prefix: str,
    access_key_id: str,
    secret_access_key: str,
    session_token: str,
    region: str = "auto",
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = S3_WIPE_TIMEOUT_SECONDS,
) -> WipeResult:
    try:
        client = _S3Client(
            endpoint=endpoint,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            region=region,
            opener=opener,
            timeout=timeout,
        )
        keys = client.list_objects(bucket=bucket, prefix=prefix)
        for batch in _chunks(keys, DELETE_OBJECT_BATCH_SIZE):
            client.delete_objects(bucket=bucket, keys=batch)
        uploads = client.list_uploads(bucket=bucket, prefix=prefix)
        for upload in uploads:
            client.abort_upload(bucket=bucket, upload=upload)
    except _WipeFailure as exc:
        logger.warning(
            "s3 prefix wipe failed reason_code=%s http_status=%s",
            exc.reason_code,
            exc.http_status,
        )
        return WipeResult("error", exc.reason_code)

    logger.info("s3 prefix wipe completed reason_code=ok")
    return WipeResult("ok", None)


__all__ = [
    "S3_WIPE_TIMEOUT_SECONDS",
    "WipeResult",
    "wipe_prefix",
]
