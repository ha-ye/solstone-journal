# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think import cogitate_read_tools as crt


@pytest.fixture
def read_tools_journal(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()

    def write(rel: str, content: str) -> Path:
        path = journal / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    write("chronicle/20260608/session/090000_300/evidence.md", "x evidence\n")
    write("chronicle/20260608/session/090000_300/nested/deep.txt", "deep x\n")
    write("chronicle/20260608/foo", "date redirected\n")
    write("notes/nested/a.txt", "note x\n")
    write(".agents/skills/journal/SKILL.md", "# Journal Skill\n")
    write(".git/config", "git-secret x\n")
    write(".cache/x", "cache-secret x\n")
    write("node_modules/pkg/index.js", "node-secret x\n")
    write(".venv/lib/python3.12/site-packages/pkg.py", "venv-secret x\n")
    write("id_rsa", "credential-secret x\n")
    write("private.pem", "credential-secret x\n")
    write(".env", "credential-secret x\n")
    write("binary.bin", "placeholder\n").write_bytes(b"abc\x00def")
    write("real/inside.txt", "alias target x\n")

    fifo = journal / "fifo"
    os.mkfifo(fifo)

    os.symlink(journal / "missing-target", journal / "dangling")

    denied = write("denied.txt", "permission-secret\n")
    os.chmod(denied, 0)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside x\n", encoding="utf-8")
    os.symlink(outside, journal / "escape")

    os.symlink(journal / ".git", journal / "logs")
    os.symlink(journal / "real", journal / "alias")

    env = SimpleNamespace(journal=journal, denied=denied)
    try:
        yield env
    finally:
        if denied.exists():
            os.chmod(denied, 0o600)


def _payload_paths(result: crt.ReadResult) -> list[str]:
    payload = result.payload
    if not isinstance(payload, list):
        return []
    paths: list[str] = []
    for item in payload:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, crt.Entry | crt.GrepMatch):
            paths.append(item.path)
    return paths


def test_module_docstring_declares_security_contract():
    doc = (crt.__doc__ or "").lower()

    assert "read-only" in doc
    assert "journal root" in doc
    assert "denylist" in doc


def test_ac01_non_root_paths_use_contained_path_for_escape(read_tools_journal):
    journal = read_tools_journal.journal

    results = [
        crt.read_file(journal, "escape/secret.txt"),
        crt.list_directory(journal, "escape"),
        crt.glob(journal, "*", root="escape"),
        crt.grep_search(journal, "outside", path="escape"),
    ]

    assert all(result.ok is False for result in results)
    assert {result.refusal for result in results} == {crt.REFUSAL_PATH_ESCAPE}


def test_ac02_journal_root_defaults_succeed(read_tools_journal):
    journal = read_tools_journal.journal

    listed = crt.list_directory(journal)
    globbed = crt.glob(journal, "*")
    grepped = crt.grep_search(journal, "x")

    assert listed.ok is True
    assert globbed.ok is True
    assert grepped.ok is True


def test_ac03_read_file_date_prefix_redirects_to_chronicle(read_tools_journal):
    result = crt.read_file(read_tools_journal.journal, "20260608/foo")

    assert result.ok is True
    assert result.payload == "date redirected"


def test_ac04_traversal_paths_are_refused_by_all_tools(read_tools_journal):
    journal = read_tools_journal.journal

    results = [
        crt.read_file(journal, "../outside"),
        crt.list_directory(journal, "../outside"),
        crt.glob(journal, "*", root="../outside"),
        crt.grep_search(journal, "x", path="../outside"),
    ]

    assert all(result.ok is False for result in results)
    assert {result.refusal for result in results} == {crt.REFUSAL_BAD_PATH}


def test_ac05_symlink_escape_explicit_target_refused_by_all_tools(read_tools_journal):
    journal = read_tools_journal.journal

    results = [
        crt.read_file(journal, "escape/secret.txt"),
        crt.list_directory(journal, "escape"),
        crt.glob(journal, "*", root="escape"),
        crt.grep_search(journal, "outside", path="escape"),
    ]

    assert all(result.ok is False for result in results)
    assert {result.refusal for result in results} == {crt.REFUSAL_PATH_ESCAPE}


def test_ac06_logs_symlink_to_git_is_pruned_from_traversal(read_tools_journal):
    journal = read_tools_journal.journal

    listed = crt.list_directory(journal, recursive=True, include_hidden=True)
    globbed = crt.glob(journal, "*", include_hidden=True)
    grepped = crt.grep_search(journal, "git-secret", include_hidden=True)

    assert all("logs" not in path for path in _payload_paths(listed))
    assert all("logs" not in path for path in _payload_paths(globbed))
    assert all("logs" not in path for path in _payload_paths(grepped))
    assert all(".git/config" not in path for path in _payload_paths(listed))
    assert ".git/config" not in _payload_paths(globbed)
    assert grepped.payload == []


def test_ac07_component_denylist_loud_for_read_silent_for_traversal(
    read_tools_journal,
):
    journal = read_tools_journal.journal
    denied = [
        ".git/config",
        ".cache/x",
        "node_modules/pkg/index.js",
        ".venv/lib/python3.12/site-packages/pkg.py",
    ]

    for rel in denied:
        result = crt.read_file(journal, rel)
        assert result.ok is False
        assert result.refusal == crt.REFUSAL_DENIED_COMPONENT

    listed = crt.list_directory(journal, recursive=True, include_hidden=True)
    globbed = crt.glob(journal, "*", include_hidden=True)
    grepped = crt.grep_search(journal, "secret", include_hidden=True)

    for rel in denied:
        assert rel not in _payload_paths(listed)
        assert rel not in _payload_paths(globbed)
        assert rel not in _payload_paths(grepped)


def test_ac08_credential_denylist_loud_for_read_excluded_from_search(
    read_tools_journal,
):
    journal = read_tools_journal.journal
    denied = ["id_rsa", "private.pem", ".env"]

    for rel in denied:
        result = crt.read_file(journal, rel)
        assert result.ok is False
        assert result.refusal == crt.REFUSAL_CREDENTIAL_FILE

    globbed = crt.glob(journal, "*", include_hidden=True)
    grepped = crt.grep_search(journal, "credential-secret", include_hidden=True)
    for rel in denied:
        assert rel not in _payload_paths(globbed)
        assert rel not in _payload_paths(grepped)


def test_ac09_hidden_agents_skill_readable_by_explicit_read(read_tools_journal):
    result = crt.read_file(
        read_tools_journal.journal, ".agents/skills/journal/SKILL.md"
    )

    assert result.ok is True
    assert result.payload == "# Journal Skill"


def test_ac10_read_file_stable_refusals_do_not_raise(read_tools_journal):
    journal = read_tools_journal.journal

    cases = {
        "real": crt.REFUSAL_NOT_FILE,
        "binary.bin": crt.REFUSAL_BINARY,
        "fifo": crt.REFUSAL_SPECIAL_FILE,
        "dangling": crt.REFUSAL_MISSING,
        "denied.txt": crt.REFUSAL_PERMISSION_DENIED,
    }

    for rel, refusal in cases.items():
        result = crt.read_file(journal, rel)
        assert result.ok is False
        assert result.refusal == refusal


def test_ac11_caps_and_budget_truncate_or_exhaust(read_tools_journal):
    journal = read_tools_journal.journal
    capdir = journal / "capdir"
    capdir.mkdir()
    for idx in range(5):
        (capdir / f"item{idx}.txt").write_text(f"needle {idx}\n", encoding="utf-8")
    (journal / "many-lines.txt").write_text(
        "\n".join(f"line {idx}" for idx in range(10)),
        encoding="utf-8",
    )
    (journal / "big-bytes.txt").write_text("x" * 40, encoding="utf-8")
    (journal / "many-grep.txt").write_text("needle\n" * 10, encoding="utf-8")

    line_read = crt.read_file(journal, "many-lines.txt", max_lines=3)
    byte_read = crt.read_file(journal, "big-bytes.txt", max_bytes=5)
    listed = crt.list_directory(journal, "capdir", max_entries=2)
    globbed = crt.glob(journal, "capdir/*", max_matches=2)
    grepped = crt.grep_search(journal, "needle", path="many-grep.txt", max_matches=2)

    assert line_read.truncated is True
    assert line_read.notice == crt.NOTICE_READ_FILE_TRUNCATED
    assert byte_read.truncated is True
    assert byte_read.notice == crt.NOTICE_READ_FILE_TRUNCATED
    assert listed.truncated is True
    assert listed.notice == crt.NOTICE_LIST_DIRECTORY_TRUNCATED
    assert globbed.truncated is True
    assert globbed.notice == crt.NOTICE_GLOB_TRUNCATED
    assert grepped.truncated is True
    assert grepped.notice == crt.NOTICE_GREP_TRUNCATED

    budget = crt.ReadBudget(cap=2)
    assert crt.read_file(journal, "notes/nested/a.txt", budget=budget).ok is True
    assert crt.list_directory(journal, budget=budget).ok is True
    exhausted = crt.glob(journal, "*", budget=budget)
    assert exhausted.ok is False
    assert exhausted.refusal == crt.REFUSAL_BUDGET_EXHAUSTED


def test_ac12_none_budget_never_exhausts(read_tools_journal):
    journal = read_tools_journal.journal

    for _idx in range(crt.DEFAULT_READ_CALL_BUDGET + 5):
        result = crt.read_file(journal, "notes/nested/a.txt", budget=None)
        assert result.ok is True
        assert result.refusal != crt.REFUSAL_BUDGET_EXHAUSTED


def test_ac13_grep_literal_default_and_regex_opt_in(read_tools_journal):
    journal = read_tools_journal.journal
    (journal / "regex.txt").write_text("a.b\nacb\n[x]\nx\n", encoding="utf-8")

    literal_dot = crt.grep_search(journal, "a.b", path="regex.txt")
    regex_dot = crt.grep_search(journal, "a.b", path="regex.txt", regex=True)
    literal_class = crt.grep_search(journal, "[x]", path="regex.txt")
    regex_class = crt.grep_search(journal, "[x]", path="regex.txt", regex=True)

    assert [match.line for match in literal_dot.payload] == ["a.b"]
    assert [match.line for match in regex_dot.payload] == ["a.b", "acb"]
    assert [match.line for match in literal_class.payload] == ["[x]"]
    assert [match.line for match in regex_class.payload] == ["[x]", "x"]


def test_invalid_regex_returns_pattern_refusal(read_tools_journal):
    result = crt.grep_search(read_tools_journal.journal, "(", regex=True)

    assert result.ok is False
    assert result.refusal == crt.REFUSAL_BAD_PATTERN


def test_utf8_multibyte_straddling_byte_cap_is_not_binary(read_tools_journal):
    journal = read_tools_journal.journal
    (journal / "unicode.txt").write_bytes("abc é needle\n".encode("utf-8"))

    read_result = crt.read_file(journal, "unicode.txt", max_bytes=5)
    grep_result = crt.grep_search(
        journal,
        "abc",
        path="unicode.txt",
        max_bytes_per_file=5,
    )

    assert read_result.ok is True
    assert read_result.refusal != crt.REFUSAL_BINARY
    assert read_result.truncated is True
    assert read_result.payload == "abc "
    assert grep_result.ok is True
    assert grep_result.truncated is True
    assert [match.line for match in grep_result.payload] == ["abc "]


def _journal_snapshot(
    journal: Path,
) -> tuple[set[tuple[str, int, int, str]], dict[str, bytes]]:
    structural: set[tuple[str, int, int, str]] = set()
    contents: dict[str, bytes] = {}
    for path in sorted(journal.rglob("*")):
        rel = path.relative_to(journal).as_posix()
        st = os.lstat(path)
        kind = stat.S_IFMT(st.st_mode)
        link_target = os.readlink(path) if stat.S_ISLNK(st.st_mode) else ""
        structural.add((rel, kind, stat.S_IMODE(st.st_mode), link_target))
        if (
            stat.S_ISREG(kind)
            and not stat.S_ISLNK(st.st_mode)
            and stat.S_IMODE(st.st_mode) != 0
        ):
            contents[rel] = path.read_bytes()
    return structural, contents


def test_ac14_reads_do_not_mutate_and_imports_stay_read_only(read_tools_journal):
    journal = read_tools_journal.journal
    before = _journal_snapshot(journal)

    crt.read_file(journal, "notes/nested/a.txt")
    crt.read_file(journal, "binary.bin")
    crt.list_directory(journal, recursive=True, include_hidden=True)
    crt.glob(journal, "*", include_hidden=True)
    crt.grep_search(journal, "x", include_hidden=True)

    assert _journal_snapshot(journal) == before

    source = Path("solstone/think/cogitate_read_tools.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = {
        "atomic_replace",
        "write_json",
        "write_jsonl",
        "write_text",
        "append_jsonl",
        "append_text",
        "install_file",
        "hold_lock",
        "save_npz",
        "update_npz",
    }
    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)

    assert imported_names.isdisjoint(banned)
    assert "solstone.think.providers.openhands" not in imported_modules
    assert "openhands" not in imported_modules


def test_alias_symlink_canonicalizes_to_target_path(read_tools_journal):
    result = crt.list_directory(read_tools_journal.journal, "alias")

    assert result.ok is True
    assert _payload_paths(result) == ["real/inside.txt"]
