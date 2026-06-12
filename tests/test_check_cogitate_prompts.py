# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-test for scripts/check_cogitate_prompts.py."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import frontmatter
import pytest

import solstone.think.talent as talent_module
from solstone.think.prompts import PromptMetadataError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_cogitate_prompts.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_cogitate_prompts", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ccp = _load_checker()


def _write_file(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _prompt(frontmatter_json: str, body: str) -> str:
    return f"{frontmatter_json.strip()}\n\n{body.strip()}\n"


def _cogitate_prompt(body: str) -> str:
    return _prompt('{\n  "type": "cogitate"\n}', body)


def _patch_talent_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    talent_dir = tmp_path / "talent"
    apps_dir = tmp_path / "apps"
    talent_dir.mkdir()
    apps_dir.mkdir()
    monkeypatch.setattr(talent_module, "TALENT_DIR", talent_dir)
    monkeypatch.setattr(talent_module, "APPS_DIR", apps_dir)
    return talent_dir, apps_dir


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ("Bad `journal navigate`.", "bare-journal"),
        ("Bad `Bash`.", "cli-agent-tool"),
        ("Bad `Write`.", "cli-agent-tool"),
        ("Bad `Read('x')`.", "cli-agent-tool"),
        ("Bad `cat foo`.", "shell-read"),
        ("Bad `journal/chronicle/20260101/x.json`.", "raw-journal-path"),
    ],
)
def test_lint_prompt_flags_cogitate_policy_violations(body: str, kind: str) -> None:
    findings = ccp.lint_prompt(body)
    assert kind in [finding[1] for finding in findings]


def test_bare_journal_flags_fenced_commands() -> None:
    findings = ccp.lint_prompt("```sh\njournal supervisor\n```\n")
    assert findings == [
        (
            2,
            "bare-journal",
            "forbidden `journal supervisor`; use `journal` with one of "
            "{identity, health, talent}, or use `sol`/`sol call`",
        )
    ]


@pytest.mark.parametrize(
    "body",
    [
        "`journal health`",
        "`journal talent logs --daily`",
        "`sol doctor`",
        "`sol call entities list`",
        "`sol call facets list-candidates --json`",
        "`read_file journal/chronicle/20260101/x.json`",
        "`list_directory journal/chronicle`",
        "`glob journal/chronicle/*`",
        "`grep_search needle journal/chronicle`",
        "`identity/partner.md`",
        "`chronicle/20260101`",
        "`## observations`",
        "`write_file`",
        "`replace`",
    ],
)
def test_lint_prompt_ignores_sanctioned_forms(body: str) -> None:
    assert ccp.lint_prompt(body) == []


@pytest.mark.parametrize(
    "body",
    [
        "`sol call journal search x && sol call entities list`",
        "`echo $(sol call support create --subject x)`",
        "`bash -lc 'sol call journal search x'`",
    ],
)
def test_lint_prompt_flags_shell_composition(body: str) -> None:
    findings = ccp.lint_prompt(body)
    assert [(line, kind) for line, kind, _detail in findings] == [
        (1, "shell-composition")
    ]


def test_lint_prompt_allows_multiline_partner_value_example() -> None:
    body = """```bash
journal identity partner --update-section 'work patterns' --value 'My partner tends to batch meetings before noon and protects afternoon blocks for focused work. Calendar data from March 25-31 shows 85% of meetings before 12:00 (sol://20260328/archon/091500_300).

Deep work sessions typically run 2-3 hours — calendar and activity signals show fewer interruptions during these blocks.'
```
"""

    findings = ccp.lint_prompt(body)

    assert "shell-composition" not in [kind for _line, kind, _detail in findings]


@pytest.mark.parametrize(
    "body",
    [
        "`sol call activities list --source anticipated --from $day_YYYYMMDD --to <+7>`",
        '`sol call journal search "" --day-to <+6> -a pulse -n 12`',
    ],
)
def test_lint_prompt_allows_angle_placeholder_metavars(body: str) -> None:
    findings = ccp.lint_prompt(body)

    assert "shell-composition" not in [kind for _line, kind, _detail in findings]


def test_prose_is_not_linted_as_command_context() -> None:
    body = (
        "A prose sentence may mention journal, read, write, and agent. "
        "It may also say never delegate to a sub-agent, as long as this is not "
        "inside a markdown code span."
    )
    assert ccp.lint_prompt(body) == []


def test_extract_command_spans_scans_fences_per_line_without_inline_double_scan() -> (
    None
):
    body = (
        "Before `journal navigate`.\n"
        "```sh\n"
        "journal supervisor\n"
        "echo `journal health`\n"
        "\n"
        "```\n"
        "After `journal identity partner`.\n"
    )

    assert ccp.extract_command_spans(body) == [
        (1, "journal navigate"),
        (3, "journal supervisor"),
        (4, "echo `journal health`"),
        (7, "journal identity partner"),
    ]

    findings = ccp.lint_prompt(body)
    assert [(line, kind) for line, kind, _detail in findings] == [
        (1, "bare-journal"),
        (3, "bare-journal"),
        (4, "shell-composition"),
    ]


def test_ratchet_by_file_kind_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    talent_dir, _apps_dir = _patch_talent_dirs(monkeypatch, tmp_path)
    _write_file(
        talent_dir,
        "bad.md",
        _cogitate_prompt("# Bad\n\n`journal navigate`"),
    )

    over, stale, tracked = ccp.evaluate({})
    assert over
    assert stale == []
    assert tracked == []

    counts = ccp.count_violations()
    over_exact, stale_exact, tracked_exact = ccp.evaluate(counts)
    assert over_exact == []
    assert stale_exact == []
    assert tracked_exact

    key = next(iter(counts))
    ratcheted = dict(counts)
    ratcheted[key] = counts[key] - 1
    over_lowered, stale_lowered, _ = ccp.evaluate(ratcheted)
    assert over_lowered
    assert stale_lowered == []


def test_stale_allowlist_entries_are_reported_for_vanished_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    talent_dir, _apps_dir = _patch_talent_dirs(monkeypatch, tmp_path)
    _write_file(
        talent_dir,
        "clean.md",
        _cogitate_prompt("# Clean\n\n`journal health`"),
    )
    allowlist = {("solstone/talent/vanished.md", "bare-journal"): 1}

    over, stale, tracked = ccp.evaluate(allowlist)
    assert over == []
    assert any("solstone/talent/vanished.md" in line for line in stale)
    assert tracked == []


def test_discovery_uses_only_cogitate_talent_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    talent_dir, _apps_dir = _patch_talent_dirs(monkeypatch, tmp_path)
    cogitate = _write_file(
        talent_dir,
        "clean.md",
        _cogitate_prompt("# Clean\n\n`journal health`"),
    )
    _write_file(
        talent_dir,
        "generate.md",
        _prompt(
            '{\n  "type": "generate",\n  "output": "md"\n}',
            "# Generate\n\n`journal supervisor`\n\n`journal/x`",
        ),
    )
    _write_file(
        talent_dir,
        "SKILL.md",
        "# Skill\n\n`journal supervisor`\n\n`journal/x`",
    )

    prompts = ccp.discover_prompts()
    assert prompts == [
        (
            Path(os.path.relpath(cogitate, REPO_ROOT)).as_posix(),
            "# Clean\n\n`journal health`",
        )
    ]
    assert ccp.evaluate({}) == ([], [], [])


def test_malformed_frontmatter_hard_fails_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    talent_dir, _apps_dir = _patch_talent_dirs(monkeypatch, tmp_path)
    _write_file(
        talent_dir,
        "broken.md",
        '{\n  "type": "cogitate",\n}\n\n# Broken\n',
    )

    with pytest.raises(PromptMetadataError):
        ccp.discover_prompts()


def test_discovery_floor_matches_cogitate_frontmatter() -> None:
    expected: set[str] = set()
    for path in sorted((REPO_ROOT / "solstone" / "talent").glob("*.md")):
        post = frontmatter.load(path)
        if post.metadata.get("type") == "cogitate":
            expected.add(Path(os.path.relpath(path, REPO_ROOT)).as_posix())
    for path in sorted((REPO_ROOT / "solstone" / "apps").glob("*/talent/*.md")):
        post = frontmatter.load(path)
        if post.metadata.get("type") == "cogitate":
            expected.add(Path(os.path.relpath(path, REPO_ROOT)).as_posix())

    discovered = {rel for rel, _body in ccp.discover_prompts()}
    assert expected
    assert expected <= discovered


def test_repo_tree_is_green() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cogitate-prompts: pass" in result.stdout
