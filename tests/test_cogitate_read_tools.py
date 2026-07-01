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
    write(
        "chronicle/20260608/session/090000_300/talents/sense.json",
        '{"activity_summary":"rohan"}\n',
    )
    write("chronicle/20260608/foo", "date redirected\n")
    write("chronicle/20260608/.git/config", "git-secret x\n")
    write("chronicle/20260608/.cache/x", "cache-secret x\n")
    write("chronicle/20260608/node_modules/pkg/index.js", "node-secret x\n")
    write(
        "chronicle/20260608/.venv/lib/python3.12/site-packages/pkg.py",
        "venv-secret x\n",
    )
    write("chronicle/20260608/id_rsa", "credential-secret x\n")
    write("chronicle/20260608/private.pem", "credential-secret x\n")
    write("chronicle/20260608/.env", "credential-secret x\n")
    write("notes/nested/a.txt", "note x\n")
    write("entities/rohan/entity.json", '{"name":"Rohan"}\n')
    write("facets/work/facet.json", '{"facet":"work"}\n')
    write("facets/work/activities/20260608.jsonl", '{"activity":"standup"}\n')
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
    os.symlink(".git", journal / "chronicle" / "20260608" / "logs")
    os.symlink("..", journal / "chronicle" / "loop")

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


def _assert_refusal(result: crt.ReadResult, refusal: str) -> None:
    assert result.ok is False
    assert result.refusal == refusal


def _assert_broad_root(result: crt.ReadResult) -> None:
    _assert_refusal(result, crt.REFUSAL_BROAD_ROOT)


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


def test_ac02_journal_root_recursive_scans_refuse_broad(read_tools_journal):
    journal = read_tools_journal.journal

    listed = crt.list_directory(journal)
    listed_recursive = crt.list_directory(journal, recursive=True)
    globbed = crt.glob(journal, "*")
    grepped = crt.grep_search(journal, "x")

    assert listed.ok is True
    for result in [listed_recursive, globbed, grepped]:
        assert result.ok is False
        assert result.refusal == crt.REFUSAL_BROAD_ROOT


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

    listed = crt.list_directory(
        journal,
        "chronicle/20260608",
        recursive=True,
        include_hidden=True,
    )
    globbed = crt.glob(
        journal,
        "*",
        root="chronicle/20260608",
        include_hidden=True,
    )
    grepped = crt.grep_search(
        journal,
        "git-secret",
        path="chronicle/20260608",
        include_hidden=True,
    )

    assert all("chronicle/20260608/logs" not in path for path in _payload_paths(listed))
    assert all(
        "chronicle/20260608/logs" not in path for path in _payload_paths(globbed)
    )
    assert all(
        "chronicle/20260608/logs" not in path for path in _payload_paths(grepped)
    )
    assert all(
        "chronicle/20260608/.git/config" not in path for path in _payload_paths(listed)
    )
    assert "chronicle/20260608/.git/config" not in _payload_paths(globbed)
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

    listed_root = crt.list_directory(journal)
    assert all(
        rel not in _payload_paths(listed_root)
        for rel in [".git", ".cache", "node_modules", ".venv"]
    )

    listed = crt.list_directory(
        journal,
        "chronicle/20260608",
        recursive=True,
        include_hidden=True,
    )
    globbed = crt.glob(
        journal,
        "*",
        root="chronicle/20260608",
        include_hidden=True,
    )
    grepped = crt.grep_search(
        journal,
        "secret",
        path="chronicle/20260608",
        include_hidden=True,
    )

    bounded_denied = [f"chronicle/20260608/{rel}" for rel in denied]
    for rel in bounded_denied:
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

    globbed = crt.glob(
        journal,
        "*",
        root="chronicle/20260608",
        include_hidden=True,
    )
    grepped = crt.grep_search(
        journal,
        "credential-secret",
        path="chronicle/20260608",
        include_hidden=True,
    )
    for rel in [f"chronicle/20260608/{item}" for item in denied]:
        assert rel not in _payload_paths(globbed)
        assert rel not in _payload_paths(grepped)


def test_broad_glob_refuses_root_chronicle_and_facets(read_tools_journal):
    journal = read_tools_journal.journal
    cases = [
        ("*", "."),
        ("*", ""),
        ("*", "./"),
        ("chronicle/x", "."),
        ("20260608/**", "chronicle"),
        ("*/x", "facets"),
    ]

    for pattern, root in cases:
        _assert_broad_root(crt.glob(journal, pattern, root=root))


def test_broad_grep_directory_refuses_even_with_file_glob(read_tools_journal):
    journal = read_tools_journal.journal
    cases = [
        (".", "entities.txt"),
        (".", "*.py"),
        ("chronicle", None),
        ("facets", None),
    ]

    for path, file_glob in cases:
        _assert_broad_root(
            crt.grep_search(journal, "x", path=path, file_glob=file_glob)
        )


def test_broad_recursive_list_directory_refuses_root_chronicle_facets(
    read_tools_journal,
):
    journal = read_tools_journal.journal

    for path in [".", "chronicle", "facets"]:
        _assert_broad_root(crt.list_directory(journal, path, recursive=True))


def test_broad_refusal_happens_before_walk_allowed(read_tools_journal, monkeypatch):
    journal = read_tools_journal.journal
    called = False

    def fail_walk(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("_walk_allowed should not be called")

    monkeypatch.setattr(crt, "_walk_allowed", fail_walk)

    _assert_broad_root(crt.glob(journal, "*"))
    _assert_broad_root(crt.list_directory(journal, recursive=True))
    _assert_broad_root(crt.grep_search(journal, "x"))
    assert called is False


def test_broad_root_normalization_and_symlink_back_to_root(read_tools_journal):
    journal = read_tools_journal.journal

    for root in ["", ".", "./"]:
        _assert_broad_root(crt.glob(journal, "*", root=root))

    _assert_broad_root(crt.glob(journal, "*", root="chronicle/loop"))
    _assert_broad_root(crt.list_directory(journal, "chronicle/loop", recursive=True))


def test_broad_specific_pattern_never_rescues_root(read_tools_journal):
    journal = read_tools_journal.journal

    _assert_broad_root(crt.glob(journal, "entities/rohan/entity.json", root="."))
    _assert_broad_root(
        crt.grep_search(
            journal,
            "Rohan",
            path=".",
            file_glob="entities/rohan/entity.json",
        )
    )


def test_allowed_bounded_recursive_roots_succeed(read_tools_journal):
    journal = read_tools_journal.journal

    root_list = crt.list_directory(journal)
    day_glob = crt.glob(journal, "*090000*", root="chronicle/20260608")
    sense_json = crt.glob(
        journal,
        "*/talents/sense.json",
        root="chronicle/20260608/session/090000_300",
    )
    facet_glob = crt.glob(journal, "*", root="facets/work")
    facet_list = crt.list_directory(journal, "facets/work", recursive=True)
    day_grep = crt.grep_search(journal, "evidence", path="chronicle/20260608")
    file_grep = crt.grep_search(
        journal,
        "Rohan",
        path="entities/rohan/entity.json",
    )

    assert root_list.ok is True
    assert _payload_paths(root_list)
    assert day_glob.ok is True
    assert any("090000_300" in path for path in _payload_paths(day_glob))
    assert sense_json.ok is True
    assert _payload_paths(sense_json) == [
        "chronicle/20260608/session/090000_300/talents/sense.json"
    ]
    assert facet_glob.ok is True
    assert "facets/work/facet.json" in _payload_paths(facet_glob)
    assert facet_list.ok is True
    assert "facets/work/activities/20260608.jsonl" in _payload_paths(facet_list)
    assert day_grep.ok is True
    assert [match.path for match in day_grep.payload] == [
        "chronicle/20260608/session/090000_300/evidence.md"
    ]
    assert file_grep.ok is True
    assert [match.path for match in file_grep.payload] == ["entities/rohan/entity.json"]


def test_entities_top_level_recursive_exemption_succeeds(read_tools_journal):
    journal = read_tools_journal.journal

    globbed = crt.glob(journal, "*rohan*", root="entities")
    listed = crt.list_directory(journal, "entities", recursive=True)
    grepped = crt.grep_search(
        journal,
        "Rohan",
        path="entities",
        file_glob="*/entity.json",
    )

    assert globbed.ok is True
    assert "entities/rohan/entity.json" in _payload_paths(globbed)
    assert listed.ok is True
    assert "entities/rohan/entity.json" in _payload_paths(listed)
    assert grepped.ok is True
    assert [match.path for match in grepped.payload] == ["entities/rohan/entity.json"]


def test_security_refusals_win_before_broad_guard(read_tools_journal):
    journal = read_tools_journal.journal

    _assert_refusal(
        crt.glob(journal, "*", root="escape"),
        crt.REFUSAL_PATH_ESCAPE,
    )
    # Denial runs before the broad guard; denied components are not broad roots.
    _assert_refusal(
        crt.list_directory(journal, ".git", recursive=True),
        crt.REFUSAL_DENIED_COMPONENT,
    )
    _assert_refusal(
        crt.glob(journal, "*", root=".git"),
        crt.REFUSAL_DENIED_COMPONENT,
    )


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
    globbed = crt.glob(journal, "*", root="capdir", max_matches=2)
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
    crt.list_directory(
        journal,
        "chronicle/20260608",
        recursive=True,
        include_hidden=True,
    )
    crt.glob(journal, "*", root="chronicle/20260608", include_hidden=True)
    crt.grep_search(journal, "x", path="chronicle/20260608", include_hidden=True)

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
