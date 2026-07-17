# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Differential harness for journal indexer implementations."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solstone.think.utils import get_rev

try:
    from tests._baseline_harness import copytree_tracked
    from tests._sqlite_assertions import (
        CHUNK_COLUMNS,
        EDGE_COLUMNS,
        EDGE_FILE_COLUMNS,
        FILE_COLUMNS,
        rows_content_hash,
        table_content_hash,
    )
except ModuleNotFoundError:
    from _baseline_harness import copytree_tracked
    from _sqlite_assertions import (
        CHUNK_COLUMNS,
        EDGE_COLUMNS,
        EDGE_FILE_COLUMNS,
        FILE_COLUMNS,
        rows_content_hash,
        table_content_hash,
    )

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_JOURNAL = (ROOT / "tests" / "fixtures" / "journal").resolve()
DB_REL = Path("indexer") / "journal.sqlite"
COMMAND_SIDES = ("left", "right")
EDGE_SKIP_RULE = "edge_extraction_skip"
EDGE_SKIP_PREFIX = "ERROR:solstone.think.indexer.edges:Skipping edge extraction for "
LOG_RECORD_RE = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):[^:]+:")
TRACEBACK_HEADER = "Traceback (most recent call last):"
TRACEBACK_TERMINAL_RE = re.compile(r"^[A-Za-z_][\w.]*: .*$")
EXCLUDED_SHADOW_TABLES = [
    "chunks_config",
    "chunks_content",
    "chunks_data",
    "chunks_docsize",
    "chunks_idx",
]
CANONICAL_TABLES = [
    ("files", FILE_COLUMNS),
    ("chunks", CHUNK_COLUMNS),
    ("edge_files", EDGE_FILE_COLUMNS),
    ("edges", EDGE_COLUMNS),
]
VOCAB_TABLE = "chunks_vocab_row"
VOCAB_COLUMNS = ["term", "doc", "cnt"]
ENTITY_SEARCH_MTIME_RULE = "entity_search_mtime"
ENTITY_SEARCH_MTIME_PATH = "entity_search:__mtime__"
NORMALIZED_MTIME = "<normalized:entity_search_mtime>"


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _render_report(report: dict[str, Any]) -> str:
    return json.dumps(report, default=_json_default, indent=2, sort_keys=True) + "\n"


def _command_ids() -> dict[str, str]:
    return {"left": "a", "right": "b"}


def _prepare_working_copies(journal: Path, work_root: Path) -> dict[str, Path]:
    copies: dict[str, Path] = {}
    for side in COMMAND_SIDES:
        dest = work_root / side / "journal"
        copytree_tracked(journal, dest)
        db_path = dest / DB_REL
        if db_path.exists():
            raise RuntimeError(f"{side} working copy unexpectedly contains {DB_REL}")
        copies[side] = dest.resolve()

    mismatches = _mtime_mismatches(copies["left"], copies["right"])
    if mismatches:
        sample = ", ".join(mismatches[:3])
        raise RuntimeError(f"working copy mtimes differ: {sample}")
    return copies


def _mtime_mismatches(left: Path, right: Path) -> list[str]:
    mismatches: list[str] = []
    left_files = sorted(
        path.relative_to(left) for path in left.rglob("*") if path.is_file()
    )
    right_files = sorted(
        path.relative_to(right) for path in right.rglob("*") if path.is_file()
    )
    if left_files != right_files:
        return ["file sets differ"]
    for rel in left_files:
        if (left / rel).stat().st_mtime_ns != (right / rel).stat().st_mtime_ns:
            mismatches.append(rel.as_posix())
    return mismatches


def _run_command(
    *,
    side: str,
    template: str,
    journal: Path,
    output_dir: Path,
) -> dict[str, Any]:
    argv = shlex.split(template)
    stdout_path = output_dir / "stdout.txt"
    stderr_path = output_dir / "stderr.txt"
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # The harness owns subprocess journal selection: SOLSTONE_JOURNAL points the
    # command under test at its isolated copy, while SOL_SKIP_SUPERVISOR_CHECK
    # bypasses require_solstone()'s live-stack gate. This is not application code
    # resolving its own journal path, so it is not the AGENTS.md §8 violation.
    env["SOLSTONE_JOURNAL"] = str(journal)
    env["SOL_SKIP_SUPERVISOR_CHECK"] = "1"

    start = time.monotonic()
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    return {
        "id": side,
        "argv": argv,
        "cwd": str(ROOT),
        "journal": str(journal),
        "exit_code": completed.returncode,
        "duration_ms": duration_ms,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "implementation": {"version": None},
        "stderr_classification": classify_stderr(completed.stderr),
    }


def _git_rev_for_path(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _corpus_provenance(journal: Path) -> dict[str, Any]:
    resolved = journal.resolve()
    corpus: dict[str, Any] = {"source_path": str(journal)}
    try:
        resolved.relative_to(FIXTURE_JOURNAL)
    except ValueError:
        corpus["copy_route"] = "git-ls-files-live"
        corpus["identity"] = None
        return corpus

    repo_commit = _git_rev_for_path(FIXTURE_JOURNAL)
    corpus["copy_route"] = "git-archive-head"
    corpus["identity"] = (
        {"kind": "git-archive-head", "repo_commit": repo_commit}
        if repo_commit is not None
        else None
    )
    return corpus


def classify_stderr(stderr: str) -> dict[str, Any]:
    rule = {"name": EDGE_SKIP_RULE, "count": 0, "examples": []}
    unclassified: list[str] = []
    current_allowed = False
    for line in stderr.splitlines():
        if not line.strip():
            continue
        if LOG_RECORD_RE.match(line):
            current_allowed = line.startswith(EDGE_SKIP_PREFIX)
            if current_allowed:
                rule["count"] += 1
                if len(rule["examples"]) < 3:
                    rule["examples"].append(line)
            else:
                unclassified.append(line)
            continue
        continuation = _traceback_continuation(line)
        if current_allowed and continuation is not None:
            if continuation == "terminal":
                current_allowed = False
            continue
        current_allowed = False
        unclassified.append(line)
    return {"rules": [rule], "unclassified": unclassified}


def _traceback_continuation(line: str) -> str | None:
    if line == TRACEBACK_HEADER or line.startswith((" ", "\t")):
        return "body"
    if TRACEBACK_TERMINAL_RE.match(line):
        return "terminal"
    return None


def _database_check(journal: Path) -> dict[str, Any]:
    db_path = journal / DB_REL
    if not db_path.exists():
        return {
            "status": "missing",
            "db_path": str(db_path),
            "exists": False,
            "files_rows": 0,
            "chunks_rows": 0,
        }
    with sqlite3.connect(db_path) as conn:
        files_rows = conn.execute("SELECT count(*) FROM files").fetchone()[0]
        chunks_rows = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    status = "ok" if files_rows > 0 and chunks_rows > 0 else "empty"
    return {
        "status": status,
        "db_path": str(db_path),
        "exists": True,
        "files_rows": int(files_rows),
        "chunks_rows": int(chunks_rows),
    }


def _snapshot_database(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    # Opening the source WAL database may perform physical recovery or shared
    # memory setup; that is acceptable. The harness must not perform logical
    # source mutation, so all canonicalizing writes happen only in this scratch DB.
    with sqlite3.connect(source) as source_conn:
        with sqlite3.connect(dest) as dest_conn:
            source_conn.backup(dest_conn)


def _ordered_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
) -> list[list[Any]]:
    column_sql = ", ".join(columns)
    return [
        list(row)
        for row in conn.execute(
            f"SELECT {column_sql} FROM {table} ORDER BY {column_sql}"
        )
    ]


def _canonicalize_database(db_path: Path, scratch_path: Path) -> dict[str, Any]:
    _snapshot_database(db_path, scratch_path)
    with sqlite3.connect(scratch_path) as conn:
        conn.execute(
            f"CREATE VIRTUAL TABLE {VOCAB_TABLE} USING fts5vocab(chunks, 'row')"
        )
        tables = []
        for table, columns in CANONICAL_TABLES:
            rows = _ordered_rows(conn, table, columns)
            tables.append(
                {
                    "name": table,
                    "columns": columns,
                    "rows": rows,
                    "hash": table_content_hash(conn, table, columns),
                    "row_count": len(rows),
                }
            )
        vocab_rows = _ordered_rows(conn, VOCAB_TABLE, VOCAB_COLUMNS)
        vocab = {
            "variant": "row",
            "columns": VOCAB_COLUMNS,
            "rows": vocab_rows,
            "hash": rows_content_hash(vocab_rows),
            "row_count": len(vocab_rows),
        }
    return {"tables": tables, "fts5vocab": vocab}


def canonicalize_pair(
    left_db: Path,
    right_db: Path,
    scratch_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = {
        "left": _canonicalize_database(left_db, scratch_root / "left.sqlite"),
        "right": _canonicalize_database(right_db, scratch_root / "right.sqlite"),
    }
    normalized = deepcopy(raw)
    rules_fired = _apply_normalization(normalized)
    comparison = _compare_canonical(raw, normalized, rules_fired)
    return normalized, comparison


def _apply_normalization(canonical: dict[str, Any]) -> list[dict[str, Any]]:
    left_files = _table_by_name(canonical["left"], "files")
    right_files = _table_by_name(canonical["right"], "files")
    path_index = FILE_COLUMNS.index("path")
    mtime_index = FILE_COLUMNS.index("mtime")
    left_row = _find_row(left_files["rows"], path_index, ENTITY_SEARCH_MTIME_PATH)
    right_row = _find_row(right_files["rows"], path_index, ENTITY_SEARCH_MTIME_PATH)
    if (
        left_row is None
        or right_row is None
        or left_row[mtime_index] == right_row[mtime_index]
    ):
        return []

    left_row[mtime_index] = NORMALIZED_MTIME
    right_row[mtime_index] = NORMALIZED_MTIME
    left_files["hash"] = rows_content_hash(left_files["rows"])
    right_files["hash"] = rows_content_hash(right_files["rows"])
    return [
        {
            "name": ENTITY_SEARCH_MTIME_RULE,
            "table": "files",
            "path": ENTITY_SEARCH_MTIME_PATH,
            "column": "mtime",
        }
    ]


def _table_by_name(canonical: dict[str, Any], name: str) -> dict[str, Any]:
    for table in canonical["tables"]:
        if table["name"] == name:
            return table
    raise KeyError(name)


def _find_row(rows: list[list[Any]], index: int, value: Any) -> list[Any] | None:
    for row in rows:
        if row[index] == value:
            return row
    return None


def _compare_canonical(
    raw: dict[str, Any],
    normalized: dict[str, Any],
    rules_fired: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_differences = _canonical_differences(raw)
    normalized_differences = _canonical_differences(normalized)
    if not raw_differences:
        classification = "equal"
    elif not normalized_differences:
        classification = "functionally-equal"
    else:
        classification = "unexpected-differs"
    return {
        "classification": classification,
        "raw_differences": raw_differences,
        "differences": normalized_differences,
        "rules_fired": rules_fired,
    }


def _canonical_differences(canonical: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    left_tables = {table["name"]: table for table in canonical["left"]["tables"]}
    right_tables = {table["name"]: table for table in canonical["right"]["tables"]}
    for table_name in left_tables:
        if left_tables[table_name]["rows"] != right_tables[table_name]["rows"]:
            differences.append(
                {
                    "kind": "table",
                    "name": table_name,
                    "left_hash": left_tables[table_name]["hash"],
                    "right_hash": right_tables[table_name]["hash"],
                }
            )
    if (
        canonical["left"]["fts5vocab"]["rows"]
        != canonical["right"]["fts5vocab"]["rows"]
    ):
        differences.append(
            {
                "kind": "fts5vocab",
                "name": "chunks",
                "left_hash": canonical["left"]["fts5vocab"]["hash"],
                "right_hash": canonical["right"]["fts5vocab"]["hash"],
            }
        )
    return differences


def _canonical_report(comparison_source: dict[str, Any]) -> dict[str, Any]:
    tables = []
    left_tables = {
        table["name"]: table for table in comparison_source["left"]["tables"]
    }
    right_tables = {
        table["name"]: table for table in comparison_source["right"]["tables"]
    }
    for table_name, columns in CANONICAL_TABLES:
        tables.append(
            {
                "name": table_name,
                "columns": columns,
                "hashes": {
                    "left": left_tables[table_name]["hash"],
                    "right": right_tables[table_name]["hash"],
                },
                "row_counts": {
                    "left": left_tables[table_name]["row_count"],
                    "right": right_tables[table_name]["row_count"],
                },
                "equal": left_tables[table_name]["rows"]
                == right_tables[table_name]["rows"],
            }
        )
    return {
        "excluded_tables": EXCLUDED_SHADOW_TABLES,
        "rowids_excluded": True,
        "tables": tables,
        "fts5vocab": {
            "variant": "row",
            "columns": VOCAB_COLUMNS,
            "hashes": {
                "left": comparison_source["left"]["fts5vocab"]["hash"],
                "right": comparison_source["right"]["fts5vocab"]["hash"],
            },
            "row_counts": {
                "left": comparison_source["left"]["fts5vocab"]["row_count"],
                "right": comparison_source["right"]["fts5vocab"]["row_count"],
            },
            "equal": comparison_source["left"]["fts5vocab"]["rows"]
            == comparison_source["right"]["fts5vocab"]["rows"],
        },
    }


def run_differential(
    *,
    journal: Path,
    command_a: str,
    command_b: str,
    work_root: Path,
    seed: int | None = None,
) -> dict[str, Any]:
    work_root.mkdir(parents=True, exist_ok=True)
    copies = _prepare_working_copies(journal.resolve(), work_root)
    commands = []
    templates = {"left": command_a, "right": command_b}
    for side in COMMAND_SIDES:
        commands.append(
            _run_command(
                side=side,
                template=templates[side],
                journal=copies[side],
                output_dir=work_root / side,
            )
        )

    for command in commands:
        command["checks"] = {
            "exit": {"status": "ok" if command["exit_code"] == 0 else "nonzero"},
            "database": _database_check(Path(command["journal"])),
            "stderr": {
                "status": "ok"
                if not command["stderr_classification"]["unclassified"]
                else "unclassified"
            },
        }

    report = _base_report(
        journal=journal,
        work_root=work_root,
        command_a=command_a,
        command_b=command_b,
        seed=seed,
        commands=commands,
    )
    failure = _failure_for_commands(commands)
    if failure is not None:
        report["classification"] = "failed"
        report["failure"] = failure
        return report

    normalized, comparison = canonicalize_pair(
        Path(commands[0]["checks"]["database"]["db_path"]),
        Path(commands[1]["checks"]["database"]["db_path"]),
        work_root / "canonical",
    )
    report["classification"] = comparison["classification"]
    report["canonical"] = _canonical_report(normalized)
    report["normalization"]["rules_fired"] = comparison["rules_fired"]
    report["differences"] = comparison["differences"]
    if comparison["classification"] == "functionally-equal":
        report["raw_differences"] = comparison["raw_differences"]
    return report


def _base_report(
    *,
    journal: Path,
    work_root: Path,
    command_a: str,
    command_b: str,
    seed: int | None,
    commands: list[dict[str, Any]],
) -> dict[str, Any]:
    command_ids = _command_ids()
    return {
        "schema": "solstone-indexer-differential-report",
        "schema_version": 1,
        "classification": "failed",
        "failure": None,
        "provenance": {
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "harness": {
                "name": "tests.verify_indexer_differential",
                "repo_commit": get_rev(),
                "version": None,
            },
            "corpus": _corpus_provenance(journal),
            "command_templates": [
                {"id": command_ids["left"], "argv_template": shlex.split(command_a)},
                {"id": command_ids["right"], "argv_template": shlex.split(command_b)},
            ],
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
            },
            "seed": seed,
            "work_root": str(work_root),
        },
        "commands": commands,
        "canonical": {
            "excluded_tables": EXCLUDED_SHADOW_TABLES,
            "rowids_excluded": True,
            "tables": [],
            "fts5vocab": {
                "variant": "row",
                "columns": VOCAB_COLUMNS,
                "hashes": {},
                "row_counts": {},
                "equal": False,
            },
        },
        "normalization": {
            "available_rules": [ENTITY_SEARCH_MTIME_RULE],
            "rules_fired": [],
        },
        "differences": [],
    }


def _failure_for_commands(commands: list[dict[str, Any]]) -> dict[str, Any] | None:
    for command in commands:
        if command["checks"]["exit"]["status"] != "ok":
            return {
                "class": "command_nonzero",
                "command_id": command["id"],
                "exit_code": command["exit_code"],
            }
        db_status = command["checks"]["database"]["status"]
        if db_status == "missing":
            return {"class": "db_missing", "command_id": command["id"]}
        if db_status == "empty":
            return {"class": "db_empty", "command_id": command["id"]}
        if command["checks"]["stderr"]["status"] != "ok":
            return {
                "class": "stderr_unclassified",
                "command_id": command["id"],
                "count": len(command["stderr_classification"]["unclassified"]),
            }
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, help="Source journal tree to copy")
    parser.add_argument("--a", required=True, help="Command template for side A")
    parser.add_argument("--b", required=True, help="Command template for side B")
    parser.add_argument("--work-dir", help="Directory for working copies and artifacts")
    parser.add_argument("--report", help="Report path; defaults under --work-dir")
    parser.add_argument(
        "--seed", type=int, default=None, help="Recorded seed, unused today"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    work_root = (
        Path(args.work_dir).resolve()
        if args.work_dir
        else Path(tempfile.mkdtemp(prefix="solstone-indexer-diff-")).resolve()
    )
    report_path = (
        Path(args.report).resolve() if args.report else work_root / "report.json"
    )
    try:
        report = run_differential(
            journal=Path(args.journal),
            command_a=args.a,
            command_b=args.b,
            work_root=work_root,
            seed=args.seed,
        )
    except Exception as exc:
        report = {
            "schema": "solstone-indexer-differential-report",
            "schema_version": 1,
            "classification": "failed",
            "failure": {"class": "harness_error", "message": str(exc)},
        }

    rendered = _render_report(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report.get("classification") in {"equal", "functionally-equal"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
