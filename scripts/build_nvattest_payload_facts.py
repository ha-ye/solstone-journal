#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Build committed nvattest payload executable-bit fixtures."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from solstone.think.providers import nvattest_install
from solstone.think.providers.nvattest_authority import authority_entry

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "nvattest"
TARGET_KEYS = ("linux-aarch64", "linux-x86_64", "macos-arm64")
SCHEMA_VERSION = 1
USER_AGENT = "solstone-nvattest-payload-facts/1.0"


class NvattestPayloadFactsError(RuntimeError):
    pass


def render_payload_facts_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _archive_name(url: str) -> str:
    name = Path(urlparse(url).path).name
    if not name.endswith(".tar.xz"):
        raise NvattestPayloadFactsError(f"nvattest archive url is invalid: {url}")
    return name


def _fixture_path(url: str) -> Path:
    archive_name = _archive_name(url)
    return FIXTURE_DIR / f"{archive_name.removesuffix('.tar.xz')}.executable-bits.json"


def _download_verified_archive(url: str, expected_sha256: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            with tmp.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise NvattestPayloadFactsError(
                f"sha256 mismatch for {_archive_name(url)}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        os.replace(tmp, dest)
        return actual_sha256
    except NvattestPayloadFactsError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise NvattestPayloadFactsError(
            f"failed to download nvattest archive {_archive_name(url)}: {exc}"
        ) from exc


def _find_payload_root(extract_dir: Path) -> Path:
    entries = sorted(extract_dir.iterdir(), key=lambda item: item.name)
    if not entries:
        raise NvattestPayloadFactsError("nvattest archive extracted no payload members")
    if len(entries) == 1:
        only = entries[0]
        if only.is_dir() and not only.is_symlink():
            return only
        raise NvattestPayloadFactsError(
            f"nvattest archive single extracted member is not a directory: {only.name}"
        )
    return extract_dir


def _derive_executable_bits(payload_root: Path) -> dict[str, bool]:
    observed: dict[str, dict[str, Any]] = {}
    observed_dirs: set[str] = set()
    for child in sorted(payload_root.iterdir(), key=lambda item: item.name):
        nvattest_install._scan_payload_path(
            child,
            payload_root,
            observed,
            observed_dirs,
        )
    if not observed:
        raise NvattestPayloadFactsError("nvattest archive contains no payload members")
    executable: dict[str, bool] = {}
    for relpath, fact in sorted(observed.items()):
        kind = fact.get("kind")
        if kind not in {"regular", "symlink"}:
            raise NvattestPayloadFactsError(
                f"nvattest archive member {relpath} has unsupported kind {kind!r}"
            )
        value = fact.get("executable")
        if not isinstance(value, bool):
            raise NvattestPayloadFactsError(
                f"nvattest archive member {relpath} executable bit is invalid"
            )
        executable[relpath] = value
    return executable


def build_payload_facts(target_key: str, work_dir: Path) -> tuple[Path, str]:
    entry = authority_entry(target_key)
    url = entry.artifact.url
    expected_sha256 = entry.artifact.sha256
    target_dir = work_dir / target_key
    archive = target_dir / _archive_name(url)
    extract_dir = target_dir / "extract"
    actual_sha256 = _download_verified_archive(url, expected_sha256, archive)
    nvattest_install._safe_extract_nvattest_tarball(archive, extract_dir)
    payload = {
        "archive_sha256": actual_sha256,
        "executable": _derive_executable_bits(_find_payload_root(extract_dir)),
        "schema_version": SCHEMA_VERSION,
        "target": target_key,
    }
    return _fixture_path(url), render_payload_facts_json(payload)


def _render_all(work_dir: Path) -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    for target_key in TARGET_KEYS:
        path, text = build_payload_facts(target_key, work_dir)
        outputs[path] = text
    return outputs


def write_outputs() -> None:
    with tempfile.TemporaryDirectory(prefix="solstone-nvattest-payload-facts-") as tmp:
        outputs = _render_all(Path(tmp))
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for path, text in sorted(outputs.items()):
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


def check_outputs() -> int:
    with tempfile.TemporaryDirectory(prefix="solstone-nvattest-payload-facts-") as tmp:
        outputs = _render_all(Path(tmp))
    status = 0
    for path, expected in sorted(outputs.items()):
        try:
            actual = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            actual = ""
        if actual == expected:
            continue
        status = 1
        relpath = path.relative_to(ROOT)
        print(
            f"nvattest executable-bit fixture is stale: {relpath}. "
            "Run: make nvattest-payload-facts",
            file=sys.stderr,
        )
        diff = difflib.unified_diff(
            actual.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=f"{relpath} (actual)",
            tofile=f"{relpath} (expected)",
        )
        print("".join(diff), file=sys.stderr, end="")
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            return check_outputs()
        write_outputs()
        return 0
    except NvattestPayloadFactsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
