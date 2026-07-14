#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Lint: the thin-base / journal leaf package menu stays internally consistent.

After the package split, the root distribution ships only the thin `sol` /
`solstone` access scripts. `solstone-journal` and `solstone-journal-cuda` are
leaf packages that own the host-only `journal` and `mlx-vlm-server` console
scripts and compose the root `[journal-host]` building block with exactly one
ONNX runtime.

The invariants are:

  1. Base `[project.dependencies]` is exactly the thin access partition — the
     boundary the access-surface import-clean guard enforces. No heavy host
     dependency may leak into base.
  2. There is no `[all]` extra.
  3. Root `[journal]` and `[journal-cuda]` are tombstones pinned exactly to
     `solstone-journal-host==0.7.0`.
  4. `[journal-host]` stays in root, folds in the `[pdf]` building block, pins
     `solstone-journal-models==<models leaf version>`, and pins the tested
     LiteLLM runtime used by OpenHands.
  5. The CPU leaf depends on `solstone[journal-host]==<root version>`, pulls
     CPU `onnxruntime`, and does not pull `onnxruntime-gpu`.
  6. The CUDA leaf depends on `solstone[journal-host]==<root version>`, pulls
     `onnxruntime-gpu` plus the seven NVIDIA CUDA wheels, and does not pull CPU
     `onnxruntime`.
  7. The two leaves never depend on each other.
  8. Both leaves own exactly the host-only console scripts.
  9. Root scripts stay exactly the thin access scripts.
 10. Each leaf has metadata-only setuptools config, a workspace source for
     `solstone`, the expected package name, and the root version.
 11. uv workspace members/sources are exactly the two journal leaves plus models;
     `solstone-journal-host` is absent.
 12. `[tool.uv].override-dependencies` contains the tombstone pin.
 13. The Makefile no longer uses root journal extra spellings.
"""

import sys
import tomllib
from pathlib import Path

from solstone.think.features import FEATURES

# The thin access partition. Adding anything here must keep the `sol` access
# commands import-clean (scripts/check_access_imports_clean.py) — keep this in
# lockstep with pyproject's [project.dependencies].
THIN_BASE = {
    "setproctitle",
    "typer",
    "requests",
    "timefhuman",
    "cryptography>=42,<47",
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
TOMBSTONE_PIN = "solstone-journal-host==0.7.0"
LITELLM_PIN = "litellm==1.86.1"
PDF_META_EXTRA = [
    "solstone[pdf-import]",
    "solstone[pdf-export]",
    "pypdf>=4.0.0",
    "pdf2image>=1.16.0",
]
DIST_TO_IMPORT_NAME = {
    "pillow": "PIL",
}
CPU_ONNXRUNTIME_DEPS = {
    "onnxruntime>=1.20.0,!=1.24.1",
    "onnxruntime>=1.25.0,!=1.24.1; sys_platform == 'linux' and platform_machine == 'x86_64'",
}
CUDA_ONNXRUNTIME_DEP = "onnxruntime-gpu>=1.25.0"
NVIDIA_CUDA_DEPS = {
    "nvidia-cuda-runtime-cu12",
    "nvidia-cudnn-cu12",
    "nvidia-cublas-cu12",
    "nvidia-cufft-cu12",
    "nvidia-curand-cu12",
    "nvidia-cuda-nvrtc-cu12",
    "nvidia-nvjitlink-cu12",
}
WORKSPACE_MEMBERS = [
    "packages/solstone-journal",
    "packages/solstone-journal-cuda",
    "packages/solstone-journal-models",
]
WORKSPACE_SOURCES = {
    "solstone-journal",
    "solstone-journal-cuda",
    "solstone-journal-models",
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


def _import_names(reqs: list[str]) -> set[str]:
    return {DIST_TO_IMPORT_NAME.get(name, name) for name in _names(reqs)}


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


def _read_toml(path: Path, root: Path, errors: list[str]) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing pyproject: {path.relative_to(root)}")
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"invalid TOML in {path.relative_to(root)}: {exc}")
    return {}


def _leaf_dependencies(
    *,
    label: str,
    data: dict,
    expected_name: str,
    root_version: str | None,
    errors: list[str],
) -> list[str]:
    project = data.get("project", {})
    tool = data.get("tool", {})
    setuptools = tool.get("setuptools", {})
    uv = tool.get("uv", {})
    deps = project.get("dependencies", [])

    if project.get("name") != expected_name:
        errors.append(f"{label} [project].name must be {expected_name!r}")
    if project.get("version") != root_version:
        errors.append(
            f"{label} [project].version must match root version {root_version}; "
            f"found {project.get('version')!r}"
        )
    if root_version:
        expected_pin = f"solstone[journal-host]=={root_version}"
        pins = [dep for dep in deps if dep.startswith("solstone[journal-host]==")]
        if len(pins) != 1:
            errors.append(
                f"{label} must contain exactly one solstone[journal-host]== pin; found {len(pins)}"
            )
        elif pins[0] != expected_pin:
            errors.append(f"{label} host pin must be {expected_pin}; found {pins[0]}")
    if project.get("scripts", {}) != HOST_SCRIPTS:
        errors.append(
            f"{label} [project.scripts] must be exactly {HOST_SCRIPTS}; "
            f"found {project.get('scripts', {})}"
        )
    if setuptools.get("packages") != []:
        errors.append(f"{label} [tool.setuptools].packages must be []")
    if setuptools.get("py-modules") != []:
        errors.append(f"{label} [tool.setuptools].py-modules must be []")
    if uv.get("sources", {}).get("solstone") != {"workspace": True}:
        errors.append(
            f"{label} [tool.uv.sources].solstone must be {{workspace = true}}"
        )
    return deps


def main(root: Path | None = None) -> int:
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    cpu_pyproject = root / "packages" / "solstone-journal" / "pyproject.toml"
    cuda_pyproject = root / "packages" / "solstone-journal-cuda" / "pyproject.toml"
    models_pyproject = root / "packages" / "solstone-journal-models" / "pyproject.toml"
    makefile = root / "Makefile"
    errors: list[str] = []

    data = _read_toml(pyproject, root, errors)
    cpu_data = _read_toml(cpu_pyproject, root, errors)
    cuda_data = _read_toml(cuda_pyproject, root, errors)
    models_data = _read_toml(models_pyproject, root, errors)

    project = data.get("project", {})
    root_version = project.get("version")
    base = project.get("dependencies", [])
    extras = project.get("optional-dependencies", {})
    root_tool = data.get("tool", {})
    root_uv = root_tool.get("uv", {})
    models_version = models_data.get("project", {}).get("version")

    if not isinstance(root_version, str) or not root_version:
        errors.append("root [project].version must be a non-empty string")
        root_version = None
    if not isinstance(models_version, str) or not models_version:
        errors.append(
            "models [project].version must be a non-empty string "
            f"in {models_pyproject.relative_to(root)}"
        )
        models_version = None

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

    for name in (
        "pdf-import",
        "pdf-export",
        "pdf",
        "journal",
        "journal-cuda",
        "journal-host",
    ):
        if name not in extras:
            errors.append(f"missing required extra: [{name}]")

    for name in ("pdf-import", "pdf-export"):
        if name in extras and name in FEATURES:
            feature_modules = set(FEATURES[name].pip_modules)
            extra_modules = _import_names(extras[name])
            if extra_modules != feature_modules:
                errors.append(
                    f"[{name}] package set must match features.py pip_modules "
                    f"{sorted(feature_modules)}; found {sorted(extra_modules)}"
                )

    if extras.get("pdf") != PDF_META_EXTRA:
        errors.append(f"[pdf] must be exactly {PDF_META_EXTRA!r}")

    # 3. Root user-facing journal extras are tombstones.
    for name in ("journal", "journal-cuda"):
        if extras.get(name) != [TOMBSTONE_PIN]:
            errors.append(f"[{name}] must be exactly [{TOMBSTONE_PIN!r}]")

    # 4. journal-host folds pdf and pins models plus the tested OpenHands
    # runtime. OpenHands leaves LiteLLM broad, so an unconstrained fresh install
    # can drift beyond the version exercised by this repository's lockfile.
    if "journal-host" in extras:
        host = extras["journal-host"]
        if "solstone[pdf]" not in host:
            errors.append("[journal-host] must fold in solstone[pdf]")
        errors.extend(_check_models_pin(extras, models_version))
        litellm_requirements = [
            dep
            for dep in host
            if dep.split(";", 1)[0].strip().lower().startswith("litellm")
        ]
        if litellm_requirements != [LITELLM_PIN]:
            errors.append(
                f"[journal-host] must contain exactly {LITELLM_PIN!r}; "
                f"found {litellm_requirements}"
            )

    root_scripts = project.get("scripts", {})
    if root_scripts != ROOT_SCRIPTS:
        errors.append(
            f"root [project.scripts] must be exactly {ROOT_SCRIPTS}; found {root_scripts}"
        )

    cpu_deps = _leaf_dependencies(
        label="CPU leaf",
        data=cpu_data,
        expected_name="solstone-journal",
        root_version=root_version,
        errors=errors,
    )
    cuda_deps = _leaf_dependencies(
        label="CUDA leaf",
        data=cuda_data,
        expected_name="solstone-journal-cuda",
        root_version=root_version,
        errors=errors,
    )

    # 5. CPU leaf runtime split.
    missing_cpu_runtime = sorted(CPU_ONNXRUNTIME_DEPS - set(cpu_deps))
    if missing_cpu_runtime:
        errors.append(f"CPU leaf missing CPU onnxruntime deps: {missing_cpu_runtime}")
    if "onnxruntime-gpu" in _names(cpu_deps):
        errors.append("CPU leaf must NOT pull onnxruntime-gpu")

    # 6. CUDA leaf runtime split.
    if CUDA_ONNXRUNTIME_DEP not in cuda_deps:
        errors.append(f"CUDA leaf must pull {CUDA_ONNXRUNTIME_DEP}")
    missing_nvidia = sorted(NVIDIA_CUDA_DEPS - set(cuda_deps))
    if missing_nvidia:
        errors.append(f"CUDA leaf missing NVIDIA CUDA deps: {missing_nvidia}")
    if "onnxruntime" in _names(cuda_deps):
        errors.append("CUDA leaf must NOT pull CPU onnxruntime")

    # 7. Leaves do not depend on each other.
    if "solstone-journal-cuda" in _names(cpu_deps):
        errors.append("CPU leaf must not depend on solstone-journal-cuda")
    if "solstone-journal" in _names(cuda_deps):
        errors.append("CUDA leaf must not depend on solstone-journal")

    # 11. uv workspace members/sources.
    workspace_members = root_uv.get("workspace", {}).get("members", [])
    if workspace_members != WORKSPACE_MEMBERS:
        errors.append(
            f"root [tool.uv.workspace].members must be exactly {WORKSPACE_MEMBERS}; "
            f"found {workspace_members}"
        )
    root_sources = root_uv.get("sources", {})
    for name in sorted(WORKSPACE_SOURCES):
        if root_sources.get(name) != {"workspace": True}:
            errors.append(f"root [tool.uv.sources].{name} must be {{workspace = true}}")
    if "solstone-journal-host" in root_sources:
        errors.append("root [tool.uv.sources] must not include solstone-journal-host")

    # 12. uv override prunes the tombstone pin from workspace resolution.
    override_deps = root_uv.get("override-dependencies", [])
    if not any(dep.split(";", 1)[0].strip() == TOMBSTONE_PIN for dep in override_deps):
        errors.append(
            "[tool.uv].override-dependencies must contain "
            f"{TOMBSTONE_PIN!r} with any marker"
        )

    # 13. Makefile no longer installs retired journal extras.
    try:
        makefile_text = makefile.read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append("missing Makefile")
    else:
        for spelling in ("--extra journal", "--extra journal-cuda"):
            if spelling in makefile_text:
                errors.append(f"Makefile must not contain {spelling!r}")

    if errors:
        print("ERROR: package-menu consistency check failed", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
