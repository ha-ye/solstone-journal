# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
from pathlib import Path

import frontmatter
import pytest

from solstone.think import skills_build
from solstone.think.command_polarity import classify_verb

REAL_ROOT = Path(__file__).resolve().parents[1]


def _copy_fragment_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel_path in skills_build.FRAGMENT_SOURCES.values():
        src = REAL_ROOT / rel_path
        dst = root / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return root


def _patch_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(skills_build, "ROOT", root)


def _output_paths(root: Path) -> set[Path]:
    return {
        root / skills_build.SOL_COMMANDS_PATH,
        root / skills_build.JOURNAL_COMMANDS_PATH,
    }


def _section(content: str, heading: str) -> str:
    start = content.index(heading)
    next_heading = content.find("\n## ", start + 1)
    if next_heading == -1:
        return content[start:]
    return content[start:next_heading]


def test_render_is_deterministic_and_build_check_is_current(monkeypatch, tmp_path):
    root = _copy_fragment_tree(tmp_path)
    _patch_root(monkeypatch, root)

    first = skills_build.render()
    second = skills_build.render()
    assert first == second

    written = skills_build.build()
    assert set(written) == _output_paths(root)
    assert skills_build.check() == []

    before = {path: path.read_bytes() for path in written}
    skills_build.build()
    after = {path: path.read_bytes() for path in written}
    assert before == after


def test_check_detects_staleness_without_writing(monkeypatch, tmp_path):
    root = _copy_fragment_tree(tmp_path)
    _patch_root(monkeypatch, root)
    written = skills_build.build()
    stale_path = written[0]
    stale_path.write_text(
        stale_path.read_text(encoding="utf-8") + "stale\n", encoding="utf-8"
    )
    before = stale_path.read_text(encoding="utf-8")

    stale = skills_build.check()

    assert stale == [stale_path]
    assert stale_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("---\nname: [\n---\n", "malformed frontmatter"),
        ("---\nname: wrong\ndescription: nope\n---\n", "does not match directory"),
        ("---\nname: activities\n---\n", "missing frontmatter description"),
    ],
)
def test_malformed_fragment_raises_with_path_without_partial_output(
    monkeypatch, tmp_path, content, message
):
    root = _copy_fragment_tree(tmp_path)
    _patch_root(monkeypatch, root)
    target = root / skills_build.FRAGMENT_SOURCES["activities"]
    target.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        skills_build.build()

    assert str(target) in str(exc_info.value)
    assert message in str(exc_info.value)
    for output in _output_paths(root):
        assert not output.exists()


def test_duplicate_fragment_name_raises_with_path_without_partial_output(
    monkeypatch, tmp_path
):
    root = _copy_fragment_tree(tmp_path)
    _patch_root(monkeypatch, root)
    duplicate = Path("solstone/apps/entities-copy/talent/entities/SKILL.md")
    duplicate_path = root / duplicate
    duplicate_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / skills_build.FRAGMENT_SOURCES["entities"], duplicate_path)
    sources = dict(skills_build.FRAGMENT_SOURCES)
    sources["entities_copy"] = duplicate
    monkeypatch.setattr(skills_build, "FRAGMENT_SOURCES", sources)

    with pytest.raises(ValueError) as exc_info:
        skills_build.build()

    assert str(duplicate_path) in str(exc_info.value)
    assert "duplicate app key 'entities'" in str(exc_info.value)
    for output in _output_paths(root):
        assert not output.exists()


def test_polarity_classification_and_rendered_other_group(monkeypatch, tmp_path):
    assert classify_verb("search") == "read"
    assert classify_verb("merge") == "write"
    assert classify_verb("detect") == "other"
    assert classify_verb("scan") == "read"
    assert classify_verb("segments") == "other"

    root = _copy_fragment_tree(tmp_path)
    _patch_root(monkeypatch, root)
    content = skills_build.render()[str(root / skills_build.SOL_COMMANDS_PATH)]
    health = _section(content, "## health")
    assert "Other: `for-range`, `full`, `pipeline`, `summary`" in health


def test_health_contributes_to_both_router_references(monkeypatch, tmp_path):
    root = _copy_fragment_tree(tmp_path)
    _patch_root(monkeypatch, root)

    outputs = skills_build.render()
    sol = outputs[str(root / skills_build.SOL_COMMANDS_PATH)]
    journal = outputs[str(root / skills_build.JOURNAL_COMMANDS_PATH)]

    assert "## health — `sol call health`" in sol
    assert "## health — `journal health`, `journal talent`" in journal
    assert "Guidance: `solstone/apps/health/talent/health/SKILL.md`" in journal


def test_trigger_parsing_matches_fragment_description():
    post = frontmatter.load(REAL_ROOT / skills_build.FRAGMENT_SOURCES["activities"])

    assert skills_build._parse_triggers(post.metadata["description"]) == (
        "activity",
        "activities",
        "work session",
        "completed span",
        "mute/unmute",
        "activity record",
        "meeting attendees",
    )
