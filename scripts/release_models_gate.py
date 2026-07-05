#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Decide whether the independently-versioned models artifacts should publish."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

PROJECT = "solstone-journal-models"


@dataclass(frozen=True)
class ReleaseIndex:
    kind: Literal["released", "not_found", "error"]
    versions: frozenset[str] = frozenset()
    detail: str = ""


class ReleaseIndexError(RuntimeError):
    pass


def decide_models_publish(models_version: str, index: ReleaseIndex) -> bool:
    if index.kind == "error":
        raise ReleaseIndexError(index.detail)
    if index.kind == "not_found":
        return True
    return models_version not in index.versions


def fetch_release_index(project: str, *, test: bool) -> ReleaseIndex:
    base = "https://test.pypi.org" if test else "https://pypi.org"
    url = f"{base}/pypi/{project}/json"
    try:
        with urlopen(url, timeout=10) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status == 404:
                return ReleaseIndex("not_found", detail=f"HTTP 404: {url}")
            if status != 200:
                return ReleaseIndex("error", detail=f"HTTP {status}: {url}")
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return ReleaseIndex("not_found", detail=f"HTTP 404: {url}")
        return ReleaseIndex("error", detail=f"HTTP {exc.code}: {url}")
    except URLError as exc:
        return ReleaseIndex("error", detail=f"URL error: {exc.reason}")
    except json.JSONDecodeError as exc:
        return ReleaseIndex("error", detail=f"invalid JSON: {exc}")

    try:
        releases = data["releases"]
        if not isinstance(releases, dict):
            return ReleaseIndex(
                "error", detail="invalid JSON: releases is not an object"
            )
        return ReleaseIndex("released", versions=frozenset(releases))
    except KeyError as exc:
        return ReleaseIndex("error", detail=f"invalid JSON: missing {exc.args[0]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args(argv)

    target = "TestPyPI" if args.test else "PyPI"
    index = fetch_release_index(PROJECT, test=args.test)
    try:
        publish = decide_models_publish(args.version, index)
    except ReleaseIndexError as exc:
        print(
            f"error: could not decide whether to publish {PROJECT} {args.version}: {exc}",
            file=sys.stderr,
        )
        return 1

    print("publish" if publish else "skip")
    if publish:
        if index.kind == "not_found":
            print(
                f"{PROJECT} has no release index on {target}; publishing {args.version}.",
                file=sys.stderr,
            )
        else:
            print(
                f"{PROJECT} {args.version} is absent from {target}; publishing models artifacts.",
                file=sys.stderr,
            )
    else:
        print(
            f"{PROJECT} {args.version} already exists on {target}; skipping models artifacts.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
