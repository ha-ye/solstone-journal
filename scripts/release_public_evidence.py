#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Validate public release evidence strings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from scripts.check_rust_release_manifest import Failure, validate_public_evidence_text
from scripts.release_tool_pins import PRIVATE_SIGNING_POLICY_VALUES


def validate_public_evidence_tree(label: str, value: Any) -> list[Failure]:
    failures: list[Failure] = []

    def validate_string(path: str, text: str) -> None:
        failures.extend(validate_public_evidence_text(path, text))
        for private_value in PRIVATE_SIGNING_POLICY_VALUES:
            if private_value in text:
                failures.append(
                    Failure(
                        error=f"{path} contains private signing policy",
                        expected=f"{path} public release evidence",
                        actual="redacted",
                        repair="python3 scripts/check_rust_release_manifest.py",
                    )
                )

    def safe_key_path(path: str, key: object, index: int) -> str:
        if not isinstance(key, str):
            return f"{path}[{key!r}]"
        key_failures = validate_public_evidence_text(f"{path}.<key>", key)
        contains_private_policy = any(
            private_value in key for private_value in PRIVATE_SIGNING_POLICY_VALUES
        )
        if key_failures or contains_private_policy:
            return f"{path}.<key[{index}]>"
        return f"{path}.{key}"

    def visit(path: str, node: Any) -> None:
        if isinstance(node, Mapping):
            for index, (key, child) in enumerate(node.items()):
                child_path = safe_key_path(path, key, index)
                if isinstance(key, str):
                    validate_string(f"{child_path} key", key)
                visit(child_path, child)
            return
        if isinstance(node, str):
            validate_string(path, node)
            return
        if isinstance(node, Sequence) and not isinstance(node, (bytes, bytearray)):
            for index, child in enumerate(node):
                visit(f"{path}[{index}]", child)

    visit(label, value)
    return failures
