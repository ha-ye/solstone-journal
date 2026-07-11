# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from solstone.think.services.spp_attest.nvgpu.appraise import appraise_gpu_leg
from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.tlv import decode_gpu_envelope

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2]
    / "solstone"
    / "think"
    / "services"
    / "spp_attest"
)
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spp_attest"
NVATTEST_FIXTURE_DIR = FIXTURE_DIR / "nvattest"
PURE_EXCLUDED = {"nvgpu/appraise.py"}
PURE_NON_VACUITY = {
    "__init__.py",
    "binding.py",
    "errors.py",
    "nvgpu/binary.py",
    "nvgpu/claims.py",
    "nvgpu/evidence.py",
    "nvgpu/errors.py",
    "nvgpu/__init__.py",
    "snp.py",
    "tlv.py",
    "tpm_quote.py",
}
BANNED_IMPORT_ROOTS = {
    "http",
    "httpx",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "tempfile",
    "urllib",
}
APPRAISE_BANNED_IMPORT_ROOTS = {
    "http",
    "httpx",
    "requests",
    "shutil",
    "socket",
    "urllib",
}
BANNED_WRITE_ATTRS = {
    "write_text",
    "write_bytes",
    "mkdir",
    "unlink",
    "rename",
    "replace",
    "rmtree",
    "atomic_write",
    "atomic_replace",
}
BANNED_WRITE_NAMES = {"atomic_write", "atomic_replace"}
WRITE_MODE_CHARS = frozenset({"w", "a", "x", "+"})


def test_spp_attest_package_stays_pure_python_read_only_except_nvgpu_appraise() -> None:
    assert PURE_EXCLUDED == {"nvgpu/appraise.py"}
    files = [
        path
        for path in sorted(PACKAGE_DIR.rglob("*.py"))
        if path.relative_to(PACKAGE_DIR).as_posix() not in PURE_EXCLUDED
    ]
    assert files, f"no Python files found under {PACKAGE_DIR}"
    scanned = {path.relative_to(PACKAGE_DIR).as_posix() for path in files}
    assert PURE_NON_VACUITY <= scanned

    findings: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            findings.extend(_scan_node(path, node))

    assert findings == []


def test_nvgpu_appraise_impurity_is_narrow() -> None:
    path = PACKAGE_DIR / "nvgpu" / "appraise.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = _import_roots(tree)

    assert "subprocess" in imports
    assert "tempfile" in imports

    findings: list[str] = []
    for node in ast.walk(tree):
        findings.extend(
            _scan_node(
                path,
                node,
                banned_import_roots=APPRAISE_BANNED_IMPORT_ROOTS,
                banned_write_attrs=BANNED_WRITE_ATTRS - {"unlink"},
                ban_solstone_utils=True,
            )
        )

    assert findings == []


def test_nvgpu_appraise_removes_temp_evidence_file_on_return_and_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nvattest_dir = tmp_path / "nvattest"
    (nvattest_dir / "bin").mkdir(parents=True)
    (nvattest_dir / "bin" / "nvattest").write_text("#!/bin/sh\n", encoding="utf-8")
    (nvattest_dir / "lib").mkdir()
    envelope = decode_gpu_envelope((FIXTURE_DIR / "gpu-envelope.tlv").read_bytes())
    owner_nonce = bytes.fromhex((FIXTURE_DIR / "nonce.hex").read_text().strip())
    observed: list[Path] = []

    def positive_run(argv, **_kwargs):
        evidence_path = Path(argv[argv.index("--gpu-evidence-file") + 1])
        assert evidence_path.is_file()
        observed.append(evidence_path)
        return subprocess.CompletedProcess(
            argv,
            0,
            (NVATTEST_FIXTURE_DIR / "positive.stdout").read_text(encoding="utf-8"),
            (NVATTEST_FIXTURE_DIR / "positive.stderr").read_text(encoding="utf-8"),
        )

    monkeypatch.setattr(
        "solstone.think.services.spp_attest.nvgpu.appraise.subprocess.run",
        positive_run,
    )
    appraise_gpu_leg(envelope, owner_nonce, nvattest_dir=nvattest_dir)
    assert observed and not observed[-1].exists()

    def negative_run(argv, **_kwargs):
        evidence_path = Path(argv[argv.index("--gpu-evidence-file") + 1])
        assert evidence_path.is_file()
        observed.append(evidence_path)
        return subprocess.CompletedProcess(
            argv,
            0,
            (NVATTEST_FIXTURE_DIR / "negC.stdout").read_text(encoding="utf-8"),
            (NVATTEST_FIXTURE_DIR / "negC.stderr").read_text(encoding="utf-8"),
        )

    monkeypatch.setattr(
        "solstone.think.services.spp_attest.nvgpu.appraise.subprocess.run",
        negative_run,
    )
    with pytest.raises(GpuAppraisalError):
        appraise_gpu_leg(envelope, owner_nonce, nvattest_dir=nvattest_dir)
    assert not observed[-1].exists()

    class FailingTempFile:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.name = str(path)
            path.write_text("", encoding="utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info) -> bool:
            return False

        def write(self, _value: str) -> int:
            raise OSError("disk full")

    failing_temp_file = FailingTempFile(tmp_path / "write-failure.json")
    monkeypatch.setattr(
        "solstone.think.services.spp_attest.nvgpu.appraise.tempfile.NamedTemporaryFile",
        lambda *_args, **_kwargs: failing_temp_file,
    )
    with pytest.raises(OSError, match="disk full"):
        appraise_gpu_leg(envelope, owner_nonce, nvattest_dir=nvattest_dir)
    assert not failing_temp_file.path.exists()


def test_purity_scanner_bans_aliased_shutil_import() -> None:
    tree = ast.parse("import shutil as sh\nsh.which('tpm2_checkquote')\n")
    findings = [
        finding
        for node in ast.walk(tree)
        for finding in _scan_node(Path("snippet.py"), node)
    ]

    assert findings == ["snippet.py:1: banned import shutil"]


def _scan_node(
    path: Path,
    node: ast.AST,
    *,
    banned_import_roots: set[str] = BANNED_IMPORT_ROOTS,
    banned_write_attrs: set[str] = BANNED_WRITE_ATTRS,
    ban_solstone_utils: bool = False,
) -> list[str]:
    if isinstance(node, ast.Import):
        return _scan_import(path, node, banned_import_roots)
    if isinstance(node, ast.ImportFrom):
        return _scan_import_from(
            path,
            node,
            banned_import_roots,
            ban_solstone_utils,
        )
    if isinstance(node, ast.Call):
        return _scan_call(path, node, banned_write_attrs)
    return []


def _scan_import(
    path: Path,
    node: ast.Import,
    banned_import_roots: set[str],
) -> list[str]:
    findings: list[str] = []
    for alias in node.names:
        root = alias.name.split(".", maxsplit=1)[0]
        if root in banned_import_roots:
            findings.append(f"{path}:{node.lineno}: banned import {alias.name}")
    return findings


def _scan_import_from(
    path: Path,
    node: ast.ImportFrom,
    banned_import_roots: set[str],
    ban_solstone_utils: bool,
) -> list[str]:
    findings: list[str] = []
    module = node.module or ""
    root = module.split(".", maxsplit=1)[0]
    if root in banned_import_roots:
        findings.append(f"{path}:{node.lineno}: banned import from {module}")
    if ban_solstone_utils and (
        module == "solstone.think.utils" or module.startswith("solstone.think.utils.")
    ):
        findings.append(f"{path}:{node.lineno}: banned import from {module}")
    for alias in node.names:
        if alias.name in BANNED_WRITE_NAMES:
            findings.append(f"{path}:{node.lineno}: banned write helper {alias.name}")
    return findings


def _scan_call(
    path: Path,
    node: ast.Call,
    banned_write_attrs: set[str],
) -> list[str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        if _is_shutil_which(func):
            return [f"{path}:{node.lineno}: banned shutil.which call"]
        if _is_json_dump(func):
            return [f"{path}:{node.lineno}: banned json.dump call"]
        if func.attr in banned_write_attrs:
            return [f"{path}:{node.lineno}: banned write API {func.attr}"]
    if isinstance(func, ast.Name):
        if func.id in BANNED_WRITE_NAMES:
            return [f"{path}:{node.lineno}: banned write helper {func.id}"]
        if func.id == "open" and _open_uses_write_mode(node):
            return [f"{path}:{node.lineno}: banned write-mode open call"]
    return []


def _is_shutil_which(func: ast.Attribute) -> bool:
    return (
        func.attr == "which"
        and isinstance(func.value, ast.Name)
        and func.value.id == "shutil"
    )


def _is_json_dump(func: ast.Attribute) -> bool:
    return (
        func.attr == "dump"
        and isinstance(func.value, ast.Name)
        and func.value.id == "json"
    )


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", maxsplit=1)[0])
    return roots


def _open_uses_write_mode(node: ast.Call) -> bool:
    mode_node = None
    if len(node.args) >= 2:
        mode_node = node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
            break
    if mode_node is None:
        return False
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return any(char in mode_node.value for char in WRITE_MODE_CHARS)
    return True
