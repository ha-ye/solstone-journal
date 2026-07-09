# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Guard helpers for generation-schema bounds."""

from __future__ import annotations

from typing import Any


def _has_type(node: dict[str, Any], schema_type: str) -> bool:
    value = node.get("type")
    if value == schema_type:
        return True
    return isinstance(value, list) and schema_type in value


def unbounded_nodes(schema: dict[str, Any]) -> list[str]:
    """Return paths for generation nodes missing local grammar bounds.

    Violations:
    - array nodes (``type == "array"`` or nullable/list-valued type containing
      ``"array"``) without ``maxItems``
    - free-text string nodes (``"string"`` in type) that have none of
      ``enum``, ``const``, ``pattern``, or ``format`` and no ``maxLength``
    """
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if _has_type(node, "array") and "maxItems" not in node:
                found.append(path)
            if (
                _has_type(node, "string")
                and "maxLength" not in node
                and not {"enum", "const", "pattern", "format"} & set(node)
            ):
                found.append(path)
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(schema, "$")
    return found
