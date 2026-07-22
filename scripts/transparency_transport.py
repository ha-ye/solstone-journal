#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Transparency publisher transport seam and local directory-backed fake."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, MutableSequence, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: Mapping[str, str]
    etag: str | None
    exit_code: int


@dataclass(frozen=True)
class ListResult:
    status: int
    keys: tuple[str, ...]
    body: bytes
    exit_code: int


@dataclass(frozen=True)
class CurlResult:
    status: int
    body: bytes
    etag: str | None
    exit_code: int


class TransparencyTransport(Protocol):
    def put_object(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> HttpResult: ...

    def get_object(self, key: str, *, cache_bypass: bool = False) -> HttpResult: ...

    def list_prefix(self, prefix: str) -> ListResult: ...

    def get_public(self, path: str, *, cache_bypass: bool = True) -> HttpResult: ...


@dataclass(frozen=True)
class FakeFailure:
    plane: str
    op: str
    key: str
    status: int
    body: bytes = b"forced failure"
    once: bool = True


@dataclass
class DirectoryTransparencyTransport:
    root: Path
    endpoint: str = "https://r2.example.invalid"
    bucket: str = "transparency-test"
    public_base_url: str = "https://transparency.solstone.app"
    call_log: MutableSequence[dict[str, object]] = field(default_factory=list)
    destination_set: set[str] = field(default_factory=set)
    failures: MutableSequence[FakeFailure] = field(default_factory=list)
    forbidden: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        (self.root / "objects").mkdir(parents=True, exist_ok=True)

    @property
    def s3_destination(self) -> str:
        return f"{self.endpoint.rstrip('/')}/{self.bucket}"

    def add_failure(
        self,
        *,
        plane: str,
        op: str,
        key: str,
        status: int,
        body: bytes = b"forced failure",
        once: bool = True,
    ) -> None:
        self.failures.append(
            FakeFailure(
                plane=plane,
                op=op,
                key=key,
                status=status,
                body=body,
                once=once,
            )
        )

    def _object_path(self, key: str) -> Path:
        if key.startswith("/") or "\x00" in key:
            raise ValueError(f"unsafe object key: {key!r}")
        path = self.root / "objects" / key
        resolved_root = (self.root / "objects").resolve()
        resolved_path = path.resolve()
        if resolved_root not in (resolved_path, *resolved_path.parents):
            raise ValueError(f"unsafe object key: {key!r}")
        return path

    def _etag_for(self, body: bytes) -> str:
        return f'"{hashlib.sha256(body).hexdigest()}"'

    def _consume_failure(self, *, plane: str, op: str, key: str) -> FakeFailure | None:
        for index, failure in enumerate(tuple(self.failures)):
            if failure.plane == plane and failure.op == op and failure.key == key:
                if failure.once:
                    del self.failures[index]
                return failure
        return None

    def _record(
        self,
        *,
        plane: str,
        op: str,
        key: str,
        status: int,
        **extra: object,
    ) -> None:
        destination = (
            self.s3_destination if plane == "s3" else self.public_base_url.rstrip("/")
        )
        self.destination_set.add(destination)
        self.call_log.append(
            {
                "plane": plane,
                "op": op,
                "key": key,
                "destination": destination,
                "status": status,
                **extra,
            }
        )

    def put_object(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> HttpResult:
        forced = self._consume_failure(plane="s3", op="PUT", key=key)
        if forced is not None:
            self._record(
                plane="s3",
                op="PUT",
                key=key,
                status=forced.status,
                if_none_match=if_none_match,
                if_match=if_match,
            )
            return HttpResult(
                status=forced.status,
                body=forced.body,
                headers={},
                etag=None,
                exit_code=0,
            )
        if key in self.forbidden:
            self._record(
                plane="s3",
                op="PUT",
                key=key,
                status=403,
                if_none_match=if_none_match,
                if_match=if_match,
            )
            return HttpResult(
                status=403, body=b"forbidden", headers={}, etag=None, exit_code=0
            )
        path = self._object_path(key)
        exists = path.exists()
        current_etag = self._etag_for(path.read_bytes()) if exists else None
        if if_none_match and exists:
            self._record(
                plane="s3",
                op="PUT",
                key=key,
                status=412,
                if_none_match=if_none_match,
                if_match=if_match,
            )
            return HttpResult(
                status=412,
                body=b"precondition failed",
                headers={},
                etag=current_etag,
                exit_code=0,
            )
        if if_match is not None and current_etag != if_match:
            self._record(
                plane="s3",
                op="PUT",
                key=key,
                status=412,
                if_none_match=if_none_match,
                if_match=if_match,
            )
            return HttpResult(
                status=412,
                body=b"precondition failed",
                headers={},
                etag=current_etag,
                exit_code=0,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        etag = self._etag_for(body)
        metadata = {
            "content_type": content_type,
            "cache_control": cache_control,
            "etag": etag,
        }
        path.with_name(f"{path.name}.metadata.json").write_text(
            json.dumps(metadata, sort_keys=True),
            encoding="utf-8",
        )
        self._record(
            plane="s3",
            op="PUT",
            key=key,
            status=200,
            if_none_match=if_none_match,
            if_match=if_match,
        )
        return HttpResult(
            status=200,
            body=b"",
            headers={"etag": etag},
            etag=etag,
            exit_code=0,
        )

    def get_object(self, key: str, *, cache_bypass: bool = False) -> HttpResult:
        forced = self._consume_failure(plane="s3", op="GET", key=key)
        if forced is not None:
            self._record(
                plane="s3",
                op="GET",
                key=key,
                status=forced.status,
                cache_bypass=cache_bypass,
            )
            return HttpResult(
                status=forced.status,
                body=forced.body,
                headers={},
                etag=None,
                exit_code=0,
            )
        path = self._object_path(key)
        if not path.exists():
            self._record(
                plane="s3",
                op="GET",
                key=key,
                status=404,
                cache_bypass=cache_bypass,
            )
            return HttpResult(
                status=404, body=b"missing", headers={}, etag=None, exit_code=0
            )
        body = path.read_bytes()
        etag = self._etag_for(body)
        self._record(
            plane="s3",
            op="GET",
            key=key,
            status=200,
            cache_bypass=cache_bypass,
        )
        return HttpResult(
            status=200,
            body=body,
            headers={"etag": etag},
            etag=etag,
            exit_code=0,
        )

    def list_prefix(self, prefix: str) -> ListResult:
        forced = self._consume_failure(plane="s3", op="LIST", key=prefix)
        if forced is not None:
            self._record(plane="s3", op="LIST", key=prefix, status=forced.status)
            return ListResult(
                status=forced.status,
                keys=(),
                body=forced.body,
                exit_code=0,
            )
        base = self.root / "objects"
        keys: list[str] = []
        if base.exists():
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.name.endswith(".metadata.json"):
                    continue
                key = path.relative_to(base).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        self._record(plane="s3", op="LIST", key=prefix, status=200)
        return ListResult(status=200, keys=tuple(keys), body=b"", exit_code=0)

    def get_public(self, path: str, *, cache_bypass: bool = True) -> HttpResult:
        forced = self._consume_failure(plane="public", op="GET", key=path)
        if forced is not None:
            self._record(
                plane="public",
                op="GET",
                key=path,
                status=forced.status,
                cache_bypass=cache_bypass,
            )
            return HttpResult(
                status=forced.status,
                body=forced.body,
                headers={},
                etag=None,
                exit_code=0,
            )
        object_path = self._object_path(path)
        if not object_path.exists():
            self._record(
                plane="public",
                op="GET",
                key=path,
                status=404,
                cache_bypass=cache_bypass,
            )
            return HttpResult(
                status=404, body=b"missing", headers={}, etag=None, exit_code=0
            )
        body = object_path.read_bytes()
        etag = self._etag_for(body)
        self._record(
            plane="public",
            op="GET",
            key=path,
            status=200,
            cache_bypass=cache_bypass,
        )
        return HttpResult(
            status=200,
            body=body,
            headers={"etag": etag},
            etag=etag,
            exit_code=0,
        )


@dataclass(frozen=True)
class CurlTransparencyTransport:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    base_url: str
    region: str = "auto"
    curl: str = "curl"

    @property
    def _bucket_url(self) -> str:
        return f"{self.endpoint.rstrip('/')}/{self.bucket}"

    def _config(self) -> str:
        return "\n".join(
            (
                "silent",
                "show-error",
                f'user = "{self.access_key_id}:{self.secret_access_key}"',
            )
        )

    def _run_curl(
        self,
        args: Sequence[str],
        *,
        body_output: Path,
    ) -> CurlResult:
        result = subprocess.run(
            [self.curl, "-K", "-", *args],
            input=self._config(),
            text=True,
            capture_output=True,
            check=False,
        )
        body = body_output.read_bytes() if body_output.exists() else b""
        if result.stderr and not body:
            body = result.stderr.encode("utf-8", errors="replace")
        status, etag = parse_curl_write_out(result.stdout)
        return CurlResult(
            status=status,
            body=body,
            etag=etag,
            exit_code=result.returncode,
        )

    def put_object(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> HttpResult:
        payload_sha256 = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload_path = tmp_path / "payload"
            body_path = tmp_path / "body"
            payload_path.write_bytes(body)
            headers = [
                "-H",
                f"x-amz-content-sha256: {payload_sha256}",
                "-H",
                f"content-type: {content_type}",
                "-H",
                f"cache-control: {cache_control}",
            ]
            if if_none_match:
                headers.extend(("-H", "If-None-Match: *"))
            if if_match is not None:
                headers.extend(("-H", f"If-Match: {if_match}"))
            curl_result = self._run_curl(
                [
                    "--aws-sigv4",
                    f"aws:amz:{self.region}:s3",
                    "-X",
                    "PUT",
                    "--upload-file",
                    str(payload_path),
                    "-w",
                    CURL_WRITE_OUT,
                    "-o",
                    str(body_path),
                    *headers,
                    f"{self._bucket_url}/{quote(key, safe='/')}",
                ],
                body_output=body_path,
            )
        return HttpResult(
            status=curl_result.status,
            body=curl_result.body,
            headers={"etag": curl_result.etag} if curl_result.etag is not None else {},
            etag=curl_result.etag,
            exit_code=curl_result.exit_code,
        )

    def get_object(self, key: str, *, cache_bypass: bool = False) -> HttpResult:
        return self._get_url(
            f"{self._bucket_url}/{quote(key, safe='/')}",
            signed=True,
            cache_bypass=cache_bypass,
        )

    def list_prefix(self, prefix: str) -> ListResult:
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "body"
            curl_result = self._run_curl(
                [
                    "--aws-sigv4",
                    f"aws:amz:{self.region}:s3",
                    "-G",
                    "-w",
                    CURL_WRITE_OUT,
                    "-o",
                    str(body_path),
                    "--data-urlencode",
                    "list-type=2",
                    "--data-urlencode",
                    f"prefix={prefix}",
                    self._bucket_url,
                ],
                body_output=body_path,
            )
        keys: tuple[str, ...] = ()
        status = curl_result.status
        body = curl_result.body
        if status == 200:
            keys, truncated = parse_s3_list_keys(body)
            if truncated:
                return ListResult(
                    status=0,
                    keys=(),
                    body=b"truncated S3 ListObjectsV2 response",
                    exit_code=curl_result.exit_code,
                )
        return ListResult(
            status=status,
            keys=keys,
            body=body,
            exit_code=curl_result.exit_code,
        )

    def get_public(self, path: str, *, cache_bypass: bool = True) -> HttpResult:
        return self._get_url(
            f"{self.base_url.rstrip('/')}/{quote(path, safe='/')}",
            signed=False,
            cache_bypass=cache_bypass,
        )

    def _get_url(self, url: str, *, signed: bool, cache_bypass: bool) -> HttpResult:
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "body"
            args = ["-w", CURL_WRITE_OUT, "-o", str(body_path)]
            if signed:
                args.extend(("--aws-sigv4", f"aws:amz:{self.region}:s3"))
            if cache_bypass:
                args.extend(("-H", "Cache-Control: no-cache"))
            args.append(url)
            curl_result = self._run_curl(args, body_output=body_path)
        return HttpResult(
            status=curl_result.status,
            body=curl_result.body,
            headers={"etag": curl_result.etag} if curl_result.etag is not None else {},
            etag=curl_result.etag,
            exit_code=curl_result.exit_code,
        )


CURL_WRITE_OUT = "%{http_code}\t%header{etag}\n"


def parse_curl_write_out(stdout: str) -> tuple[int, str | None]:
    line = stdout.splitlines()[-1] if stdout.splitlines() else ""
    status_text, separator, etag_text = line.partition("\t")
    try:
        status = int(status_text)
    except ValueError:
        status = 0
    etag = etag_text.strip() if separator and etag_text.strip() else None
    return status, etag


def parse_s3_list_keys(body: bytes) -> tuple[tuple[str, ...], bool]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return (), True

    keys: list[str] = []
    truncated = False
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name == "Key" and element.text is not None:
            keys.append(element.text)
        elif name == "IsTruncated":
            truncated = (element.text or "").strip().lower() == "true"
    return tuple(keys), truncated
