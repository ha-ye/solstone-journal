#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Capture the temporary Python `sol skills` parity oracle.

This intentionally has no ``--check`` mode, unlike
``scripts/build_journal_resolution_vectors.py``.  The oracle captured here is
``solstone/think/skills_cli.py``, and that Python implementation is deleted in a
later commit of this same lode.  Wiring a check gate to it would leave a
permanently red gate after the native Rust implementation becomes the source of
truth.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    REPO_ROOT / "core/fixtures/native-sol/skills-parity-v1/vectors.json"
)
PYTHON = REPO_ROOT / ".venv/bin/python"

SCHEMA = "native-sol-skills-parity-v1"
PLACEHOLDERS = {
    "project_root": "${PROJECT_ROOT}",
    "temp_root": "${TEMP_ROOT}",
    "home": "${HOME}",
    "cwd": "${CWD}",
    "fake_root": "${FAKE_ROOT}",
}


@dataclass(frozen=True)
class Operation:
    op: str
    path: str
    content: str | None = None
    target: str | None = None
    agent: str | None = None

    def as_json(self) -> dict[str, str]:
        data = {"op": self.op, "path": self.path}
        if self.content is not None:
            data["content_b64"] = base64.b64encode(self.content.encode()).decode("ascii")
        if self.target is not None:
            data["target"] = self.target
        if self.agent is not None:
            data["agent"] = self.agent
        return data


@dataclass(frozen=True)
class VectorSpec:
    id: str
    argv: list[str]
    mode: str
    home: str
    cwd: str = "${PROJECT_ROOT}"
    project_root: str = "${PROJECT_ROOT}"
    setup: list[Operation] = field(default_factory=list)
    compare_stderr: bool = True
    python_overrides: bool = False


def subst(text: str, values: dict[str, str]) -> str:
    for token in PLACEHOLDERS.values():
        if token not in values:
            continue
        text = text.replace(token, values[token])
    return text


def normalize(text: str, values: dict[str, str]) -> str:
    ordered = sorted(
        ((value, token) for token, value in values.items()),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for value, token in ordered:
        text = text.replace(value, token)
    return text


def apply_setup(ops: Iterable[Operation], values: dict[str, str]) -> None:
    for op in ops:
        path = Path(subst(op.path, values))
        if op.op == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
        elif op.op == "write_file":
            path.parent.mkdir(parents=True, exist_ok=True)
            assert op.content is not None
            path.write_text(op.content, encoding="utf-8")
        elif op.op == "symlink":
            path.parent.mkdir(parents=True, exist_ok=True)
            assert op.target is not None
            path.symlink_to(subst(op.target, values))
        elif op.op == "remove":
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        elif op.op == "mutate_file":
            assert op.content is not None
            path.write_text(op.content, encoding="utf-8")
        elif op.op == "copy_user_skill":
            copy_files_only(REPO_ROOT / "solstone/talent/sol", path)
        elif op.op == "project_links":
            agent = op.agent or "all"
            create_project_links(path, agent)
        else:
            raise ValueError(f"unknown setup op {op.op!r}")


def copy_files_only(src: Path, dst: Path) -> None:
    for source in sorted(path for path in src.rglob("*") if path.is_file()):
        target = dst / source.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def create_project_links(project: Path, agent: str) -> None:
    targets: list[tuple[str, Path]]
    if agent == "claude":
        targets = [("claude", project / ".claude/skills")]
    elif agent == "all":
        targets = [
            ("claude", project / ".claude/skills"),
            ("agents", project / ".agents/skills"),
        ]
    else:
        raise ValueError(f"unsupported setup project_links agent {agent!r}")
    del targets
    for link_parent in (
        [project / ".claude/skills"]
        if agent == "claude"
        else [project / ".claude/skills", project / ".agents/skills"]
    ):
        link_parent.mkdir(parents=True, exist_ok=True)
        for name in ("journal", "sol"):
            source = REPO_ROOT / "solstone/talent" / name
            (link_parent / name).symlink_to(os.path.relpath(source, link_parent))


def run_oracle(spec: VectorSpec, values: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["HOME"] = subst(spec.home, values)
    cwd = subst(spec.cwd, values)
    argv = [subst(arg, values) for arg in spec.argv[1:]]
    if spec.python_overrides:
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from solstone.think import skills_cli\n"
            f"skills_cli.get_project_root = lambda: {subst(spec.project_root, values)!r}\n"
            "skills_cli.resources.files = lambda _package: "
            f"Path({subst(spec.project_root, values)!r}) / 'solstone' / 'talent'\n"
            f"sys.argv = {['sol skills', *argv]!r}\n"
            "raise SystemExit(skills_cli.main())\n"
        )
        return subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    return subprocess.run(
        [str(PYTHON), "-m", "solstone.think.skills_cli", *argv],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def vector_specs() -> list[VectorSpec]:
    return [
        VectorSpec("fresh_user_install", ["skills", "install"], "user", "${TEMP_ROOT}/u_fresh"),
        VectorSpec(
            "idempotent_user_reinstall",
            ["skills", "install"],
            "user",
            "${TEMP_ROOT}/u_idempotent",
            setup=[
                Operation("copy_user_skill", "${TEMP_ROOT}/u_idempotent/.claude/skills/sol"),
                Operation("copy_user_skill", "${TEMP_ROOT}/u_idempotent/.codex/skills/sol"),
                Operation("copy_user_skill", "${TEMP_ROOT}/u_idempotent/.gemini/skills/sol"),
            ],
        ),
        VectorSpec(
            "replace_on_change",
            ["skills", "install", "--agent", "claude"],
            "user",
            "${TEMP_ROOT}/u_replace_change",
            setup=[
                Operation("copy_user_skill", "${TEMP_ROOT}/u_replace_change/.claude/skills/sol"),
                Operation(
                    "mutate_file",
                    "${TEMP_ROOT}/u_replace_change/.claude/skills/sol/SKILL.md",
                    "changed\n",
                ),
            ],
        ),
        VectorSpec(
            "replace_on_change_initial",
            ["skills", "install", "--agent", "claude"],
            "user",
            "${TEMP_ROOT}/u_replace_change_initial",
        ),
        VectorSpec(
            "replace_symlink_target",
            ["skills", "install", "--agent", "claude"],
            "user",
            "${TEMP_ROOT}/u_replace_symlink",
            setup=[
                Operation("write_file", "${TEMP_ROOT}/external_populated/keep.txt", "keep\n"),
                Operation(
                    "symlink",
                    "${TEMP_ROOT}/u_replace_symlink/.claude/skills/sol",
                    target="${TEMP_ROOT}/external_populated",
                ),
            ],
        ),
        VectorSpec(
            "replace_regular_file_target",
            ["skills", "install", "--agent", "claude"],
            "user",
            "${TEMP_ROOT}/u_replace_file",
            setup=[
                Operation(
                    "write_file",
                    "${TEMP_ROOT}/u_replace_file/.claude/skills/sol",
                    "not a dir\n",
                ),
            ],
        ),
        VectorSpec(
            "user_uninstall_absent_target",
            ["skills", "uninstall", "--agent", "claude"],
            "user",
            "${TEMP_ROOT}/u_uninstall_absent",
            setup=[Operation("mkdir", "${TEMP_ROOT}/u_uninstall_absent/.claude")],
        ),
        VectorSpec(
            "user_uninstall_refuses_regular_file",
            ["skills", "uninstall", "--agent", "claude"],
            "user",
            "${TEMP_ROOT}/u_uninstall_file",
            setup=[
                Operation(
                    "write_file",
                    "${TEMP_ROOT}/u_uninstall_file/.claude/skills/sol",
                    "not a dir\n",
                )
            ],
        ),
        VectorSpec(
            "user_uninstall_refuses_symlink",
            ["skills", "uninstall", "--agent", "claude"],
            "user",
            "${TEMP_ROOT}/u_uninstall_symlink",
            setup=[
                Operation("mkdir", "${TEMP_ROOT}/external_uninstall"),
                Operation(
                    "symlink",
                    "${TEMP_ROOT}/u_uninstall_symlink/.claude/skills/sol",
                    target="${TEMP_ROOT}/external_uninstall",
                ),
            ],
        ),
        VectorSpec(
            "user_install_absent_agent_config",
            ["skills", "install", "--agent", "codex"],
            "user",
            "${TEMP_ROOT}/u_absent_install",
        ),
        VectorSpec(
            "user_uninstall_absent_single_config",
            ["skills", "uninstall", "--agent", "claude"],
            "user",
            "${TEMP_ROOT}/u_absent_uninstall",
        ),
        VectorSpec(
            "user_uninstall_absent_agent_config",
            ["skills", "uninstall"],
            "user",
            "${TEMP_ROOT}/u_global_skip",
        ),
        VectorSpec(
            "explicit_gemini_absent_not_silent",
            ["skills", "uninstall", "--agent", "gemini"],
            "user",
            "${TEMP_ROOT}/u_gemini_explicit",
        ),
        VectorSpec(
            "project_install",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_install", "--agent", "all"],
            "project",
            "${TEMP_ROOT}/p_home",
        ),
        VectorSpec(
            "project_idempotent_reinstall",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_idempotent", "--agent", "all"],
            "project",
            "${TEMP_ROOT}/p_home_idempotent",
            setup=[Operation("project_links", "${TEMP_ROOT}/p_idempotent", agent="all")],
        ),
        VectorSpec(
            "project_link_target_changed",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_link_changed", "--agent", "all"],
            "project",
            "${TEMP_ROOT}/p_home_link_changed",
            setup=[
                Operation("project_links", "${TEMP_ROOT}/p_link_changed", agent="all"),
                Operation("remove", "${TEMP_ROOT}/p_link_changed/.claude/skills/sol"),
                Operation(
                    "symlink",
                    "${TEMP_ROOT}/p_link_changed/.claude/skills/sol",
                    target="bogus-target",
                ),
            ],
        ),
        VectorSpec(
            "project_user_content_preserved",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_user_content", "--agent", "all"],
            "project",
            "${TEMP_ROOT}/p_home_user_content",
            setup=[
                Operation(
                    "write_file",
                    "${TEMP_ROOT}/p_user_content/.claude/skills/journal",
                    "user-content\n",
                )
            ],
        ),
        VectorSpec(
            "project_stale_link_removal",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_stale_link", "--agent", "all"],
            "project",
            "${TEMP_ROOT}/p_home_stale_link",
            setup=[
                Operation("project_links", "${TEMP_ROOT}/p_stale_link", agent="all"),
                Operation(
                    "symlink",
                    "${TEMP_ROOT}/p_stale_link/.claude/skills/entities",
                    target="../../../solstone/apps/entities/talent/entities",
                ),
            ],
        ),
        VectorSpec(
            "project_stale_non_symlink_preserved",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_stale_content", "--agent", "all"],
            "project",
            "${TEMP_ROOT}/p_home_stale_content",
            setup=[
                Operation("project_links", "${TEMP_ROOT}/p_stale_content", agent="all"),
                Operation(
                    "write_file",
                    "${TEMP_ROOT}/p_stale_content/.claude/skills/entities/SKILL.md",
                    "user stale\n",
                ),
            ],
        ),
        VectorSpec(
            "project_uninstall",
            ["skills", "uninstall", "--project", "${TEMP_ROOT}/p_uninstall", "--agent", "all"],
            "project",
            "${TEMP_ROOT}/p_home_uninstall",
            setup=[Operation("project_links", "${TEMP_ROOT}/p_uninstall", agent="all")],
        ),
        VectorSpec(
            "list_user_mode",
            ["skills", "list"],
            "user",
            "${TEMP_ROOT}/u_list",
            setup=[Operation("copy_user_skill", "${TEMP_ROOT}/u_list/.claude/skills/sol")],
        ),
        VectorSpec(
            "list_project_mode",
            ["skills", "list", "--project", "${TEMP_ROOT}/p_list"],
            "project",
            "${TEMP_ROOT}/p_home_list",
            setup=[Operation("project_links", "${TEMP_ROOT}/p_list", agent="all")],
        ),
        VectorSpec(
            "list_rejects_agent",
            ["skills", "list", "--agent", "claude"],
            "usage",
            "${TEMP_ROOT}/u_list_agent_error",
            compare_stderr=False,
        ),
        VectorSpec(
            "project_rejects_codex",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_reject_codex", "--agent", "codex"],
            "project",
            "${TEMP_ROOT}/p_home_reject_codex",
        ),
        VectorSpec(
            "project_rejects_gemini",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_reject_gemini", "--agent", "gemini"],
            "project",
            "${TEMP_ROOT}/p_home_reject_gemini",
        ),
        VectorSpec(
            "project_no_value_uses_cwd",
            ["skills", "install", "--project", "--agent", "claude"],
            "project",
            "${TEMP_ROOT}/p_home_const_cwd",
            cwd="${TEMP_ROOT}/p_const_cwd",
            setup=[Operation("mkdir", "${TEMP_ROOT}/p_const_cwd")],
        ),
        VectorSpec(
            "project_tilde_expands",
            ["skills", "install", "--project", "~/foo", "--agent", "claude"],
            "project",
            "${TEMP_ROOT}/p_home_tilde",
        ),
        VectorSpec(
            "project_nonexistent_dir_created",
            ["skills", "install", "--project", "${TEMP_ROOT}/p_nonexistent/deep", "--agent", "claude"],
            "project",
            "${TEMP_ROOT}/p_home_nonexistent",
        ),
        VectorSpec(
            "payload_missing_user_mode",
            ["skills", "install", "--agent", "claude"],
            "payload-missing",
            "${TEMP_ROOT}/missing_user_home",
            project_root="${FAKE_ROOT}",
            python_overrides=True,
        ),
        VectorSpec(
            "payload_missing_project_mode",
            [
                "skills",
                "install",
                "--project",
                "${TEMP_ROOT}/missing_project_target",
                "--agent",
                "claude",
            ],
            "payload-missing",
            "${TEMP_ROOT}/missing_project_home",
            project_root="${FAKE_ROOT}",
            python_overrides=True,
        ),
    ]


def fake_root(values: dict[str, str]) -> None:
    root = Path(values["${FAKE_ROOT}"])
    (root / "solstone/talent").mkdir(parents=True, exist_ok=True)


def capture(output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="solstone-skills-oracle-") as temp:
        temp_root = Path(temp)
        fake = temp_root / "fake-root"
        values = {
            "${PROJECT_ROOT}": str(REPO_ROOT),
            "${TEMP_ROOT}": str(temp_root),
            "${FAKE_ROOT}": str(fake),
        }
        fake_root(values)
        vectors = []
        for spec in vector_specs():
            home = Path(subst(spec.home, values))
            cwd = Path(subst(spec.cwd, values))
            home.mkdir(parents=True, exist_ok=True)
            cwd.mkdir(parents=True, exist_ok=True)
            local_values = {
                **values,
                "${HOME}": str(home),
                "${CWD}": str(cwd),
            }
            apply_setup(spec.setup, local_values)
            result = run_oracle(spec, local_values)
            expected = {
                "stdout": normalize(result.stdout.decode("utf-8"), local_values),
                "stderr": normalize(result.stderr.decode("utf-8"), local_values),
                "exit": result.returncode,
            }
            if not spec.compare_stderr:
                expected["compare_stderr"] = False
                expected["stderr"] = ""
            vectors.append(
                {
                    "id": spec.id,
                    "argv": spec.argv,
                    "mode": spec.mode,
                    "home": spec.home,
                    "cwd": spec.cwd,
                    "project_root": spec.project_root,
                    "setup": [op.as_json() for op in spec.setup],
                    "expected": expected,
                }
            )

        data = {
            "schema": SCHEMA,
            "placeholders": PLACEHOLDERS,
            "vectors": vectors,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    capture(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
