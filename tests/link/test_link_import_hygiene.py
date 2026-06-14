# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
from pathlib import Path

import solstone.think.link

ALLOWED_CONVEY_IMPORTS = {"solstone.convey.secure_listener.framing"}


def _convey_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "solstone.convey" or alias.name.startswith(
                    "solstone.convey."
                ):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                continue
            if node.module == "solstone.convey" or node.module.startswith(
                "solstone.convey."
            ):
                imports.add(node.module)
    return imports


def test_think_link_convey_imports_stay_wire_protocol_only() -> None:
    """Keep presentation imports off `sol link`, while allowlisting framing.

    The removed `solstone.convey.utils` dependency was a presentation leak on the
    caller-side client path. `solstone.convey.secure_listener.framing` remains
    legitimate because the surviving serve/dialer path shares the wire protocol.
    """
    package_dir = Path(solstone.think.link.__file__).resolve().parent
    imports: set[str] = set()
    for path in package_dir.rglob("*.py"):
        imports.update(_convey_imports(path))

    assert imports == ALLOWED_CONVEY_IMPORTS
