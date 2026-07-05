#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Lint: the thin-base / journal-host package menu stays internally consistent.

After the package split, the root distribution ships only the thin `sol` /
`solstone` access scripts. The `solstone-journal-host` shim distribution owns
the host-only `journal` and `mlx-vlm-server` console scripts and is pulled in
by the root `[journal]` / `[journal-cuda]` extras.

The invariants are:

  1. Base `[project.dependencies]` is exactly the thin access partition — the
     boundary the access-surface import-clean guard enforces. No heavy host
     dependency may leak into base.
  2. There is no `[all]` extra.
  3. `[journal]` and `[journal-cuda]` each contain exactly one
     `solstone-journal-host==<root version>` pin.
  4. `[journal-host]` stays in root and folds in the `[pdf]` building block
     ("choose journal, get it all").
  5. The CPU/CUDA ONNX runtime split holds: `[journal]` pulls the CPU
     `onnxruntime` and NOT `onnxruntime-gpu`; `[journal-cuda]` pulls
     `onnxruntime-gpu` and NOT the CPU `onnxruntime`. They must never both
     install (the packages own the same `onnxruntime/` import dir).
  6. Root and host pyprojects agree on version, script ownership, uv workspace
     sources, and the host shim's metadata-only setuptools config.
  7. `[journal-host]` contains exactly one `solstone-journal-models==`
     pin matching the models workspace member version.
"""

import sys
import tomllib
from pathlib import Path

# The thin access partition. Adding anything here must keep the `sol` access
# commands import-clean (scripts/check_access_imports_clean.py) — keep this in
# lockstep with pyproject's [project.dependencies].
THIN_BASE = {
    "setproctitle",
    "typer",
    "requests",
    "timefhuman",
    "cryptography>=42",
    "pyOpenSSL>=24.0",
    "argon2-cffi",
    "websockets>=13.0",
    "psutil",
    "userpath>=1.9.2,<2",
}
ROOT_SCRIPTS = {
    "sol": "solstone.think.sol_cli:main",
    "solstone": "solstone.think.sol_cli:main",
}
HOST_SCRIPTS = {
    "journal": "solstone.think.sol_cli:journal_main",
    "mlx-vlm-server": "solstone.think.providers.mlx_server:main",
}


def _names(reqs: list[str]) -> set[str]:
    """Bare distribution names (drop version specifiers and markers)."""
    out = set()
    for r in reqs:
        head = r.split(";", 1)[0].strip()
        for sep in ("[", ">", "<", "=", "!", "~", " "):
            head = head.split(sep, 1)[0]
        out.add(head.strip().lower())
    return out


def _check_models_pin(extras: dict, member_version: str | None) -> list[str]:
    """Return errors for the journal models distribution pin."""
    host = extras.get("journal-host", [])
    pins = [dep for dep in host if dep.startswith("solstone-journal-models==")]
    if len(pins) != 1:
        return [
            "[journal-host] must contain exactly one solstone-journal-models== pin; "
            f"found {len(pins)}"
        ]
    if (
        member_version is not None
        and pins[0] != f"solstone-journal-models=={member_version}"
    ):
        return [
            "[journal-host] models pin must be "
            f"solstone-journal-models=={member_version}; found {pins[0]}"
        ]
    return []


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    host_pyproject = root / "packages" / "solstone-journal-host" / "pyproject.toml"
    models_pyproject = root / "packages" / "solstone-journal-models" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]
    root_version = project.get("version")
    base = project.get("dependencies", [])
    extras = project.get("optional-dependencies", {})
    root_tool = data.get("tool", {})
    root_uv = root_tool.get("uv", {})
    host_data: dict = {}
    models_version: str | None = None
    errors: list[str] = []

    if not isinstance(root_version, str) or not root_version:
        errors.append("root [project].version must be a non-empty string")

    if host_pyproject.exists():
        host_data = tomllib.loads(host_pyproject.read_text(encoding="utf-8"))
    else:
        errors.append(f"missing host pyproject: {host_pyproject.relative_to(root)}")

    if models_pyproject.exists():
        models_data = tomllib.loads(models_pyproject.read_text(encoding="utf-8"))
        maybe_models_version = models_data.get("project", {}).get("version")
        if isinstance(maybe_models_version, str) and maybe_models_version:
            models_version = maybe_models_version
        else:
            errors.append(
                "models [project].version must be a non-empty string "
                f"in {models_pyproject.relative_to(root)}"
            )
    else:
        errors.append(f"missing models pyproject: {models_pyproject.relative_to(root)}")

    # 1. Base stays exactly the thin access partition.
    if set(base) != THIN_BASE:
        missing = sorted(THIN_BASE - set(base))
        unexpected = sorted(set(base) - THIN_BASE)
        errors.append("base [project.dependencies] drifted from the thin partition")
        if unexpected:
            errors.append(
                f"  unexpected in base (move to [journal-host]?): {unexpected}"
            )
        if missing:
            errors.append(f"  missing from base: {missing}")

    # 2. [all] is retired.
    if "all" in extras:
        errors.append("[all] extra must be removed")

    # 3. Required extras exist.
    for name in ("pdf", "journal", "journal-cuda", "journal-host"):
        if name not in extras:
            errors.append(f"missing required extra: [{name}]")

    if root_version and all(name in extras for name in ("journal", "journal-cuda")):
        expected_host_pin = f"solstone-journal-host=={root_version}"
        for name in ("journal", "journal-cuda"):
            pins = [
                dep for dep in extras[name] if dep.startswith("solstone-journal-host==")
            ]
            if len(pins) != 1:
                errors.append(
                    f"[{name}] must contain exactly one solstone-journal-host== pin; found {len(pins)}"
                )
            elif pins[0] != expected_host_pin:
                errors.append(
                    f"[{name}] host pin must be {expected_host_pin}; found {pins[0]}"
                )

    if "journal-host" in extras:
        # 4. journal-host folds pdf.
        host = extras["journal-host"]
        for block in ("solstone[pdf]",):
            if block not in host:
                errors.append(f"[journal-host] must fold in {block}")
        errors.extend(_check_models_pin(extras, models_version))

    if all(name in extras for name in ("journal", "journal-cuda")):
        # 5. CPU/CUDA ONNX runtime split — never both in one extra.
        journal_names = _names(extras["journal"])
        cuda_names = _names(extras["journal-cuda"])
        if "onnxruntime" not in journal_names:
            errors.append("[journal] must pull the CPU onnxruntime")
        if "onnxruntime-gpu" in journal_names:
            errors.append(
                "[journal] must NOT pull onnxruntime-gpu (that is [journal-cuda])"
            )
        if "onnxruntime-gpu" not in cuda_names:
            errors.append("[journal-cuda] must pull onnxruntime-gpu")
        if "onnxruntime" in cuda_names:
            errors.append(
                "[journal-cuda] must NOT pull the CPU onnxruntime (clobbers the GPU runtime)"
            )

    root_scripts = project.get("scripts", {})
    if root_scripts != ROOT_SCRIPTS:
        errors.append(
            f"root [project.scripts] must be exactly {ROOT_SCRIPTS}; found {root_scripts}"
        )
    for denied in ("journal", "mlx-vlm-server"):
        if denied in root_scripts:
            errors.append(f"root [project.scripts] must not declare {denied}")

    workspace_members = root_uv.get("workspace", {}).get("members", [])
    if "packages/solstone-journal-host" not in workspace_members:
        errors.append(
            "root [tool.uv.workspace].members must include packages/solstone-journal-host"
        )
    root_sources = root_uv.get("sources", {})
    if root_sources.get("solstone-journal-host") != {"workspace": True}:
        errors.append(
            "root [tool.uv.sources].solstone-journal-host must be {workspace = true}"
        )

    if host_data:
        host_project = host_data.get("project", {})
        host_tool = host_data.get("tool", {})
        host_setuptools = host_tool.get("setuptools", {})
        host_uv = host_tool.get("uv", {})
        expected_host_dep = f"solstone[journal-host]=={root_version}"

        if host_project.get("name") != "solstone-journal-host":
            errors.append(
                'host [project].name must be "solstone-journal-host"; '
                f"found {host_project.get('name')!r}"
            )
        if host_project.get("version") != root_version:
            errors.append(
                "host [project].version must match root version "
                f"{root_version}; found {host_project.get('version')!r}"
            )
        if host_project.get("dependencies") != [expected_host_dep]:
            errors.append(
                f"host [project].dependencies must be exactly [{expected_host_dep!r}]; "
                f"found {host_project.get('dependencies')!r}"
            )
        if host_project.get("scripts", {}) != HOST_SCRIPTS:
            errors.append(
                f"host [project.scripts] must be exactly {HOST_SCRIPTS}; "
                f"found {host_project.get('scripts', {})}"
            )
        if host_setuptools.get("packages") != []:
            errors.append("host [tool.setuptools].packages must be []")
        if host_setuptools.get("py-modules") != []:
            errors.append("host [tool.setuptools].py-modules must be []")
        if host_uv.get("sources", {}).get("solstone") != {"workspace": True}:
            errors.append("host [tool.uv.sources].solstone must be {workspace = true}")

    if errors:
        print("ERROR: package-menu consistency check failed", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
