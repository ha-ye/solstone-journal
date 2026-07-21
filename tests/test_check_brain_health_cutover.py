# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-tests for scripts/check_brain_health_cutover.py."""

from __future__ import annotations

from pathlib import Path

from scripts import check_brain_health_cutover as guard


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path) -> int:
    return guard.main(["--root", str(root), "--all-files"])


def test_guard_flags_legacy_health_file_literal(tmp_path, capsys) -> None:
    legacy_file = "talents" + ".json"
    _write(tmp_path, "docs/bad.md", f"old snapshot: {legacy_file}\n")

    assert _run(tmp_path) == 1
    assert "legacy-health-file" in capsys.readouterr().out


def test_guard_flags_provider_command_shapes(tmp_path, capsys) -> None:
    provider_cmd = "journal " + "providers" + " check"
    journal_token = "'" + "journal" + "'"
    providers_token = "'" + "providers" + "'"
    check_token = "'" + "check" + "'"
    _write(tmp_path, "docs/bad.md", provider_cmd + "\n")
    _write(
        tmp_path,
        "solstone/bad.py",
        f"CMD = [{journal_token}, {providers_token}, {check_token}, '--targeted']\n",
    )

    assert _run(tmp_path) == 1
    output = capsys.readouterr().out
    assert "legacy-provider-check-text" in output
    assert "legacy-provider-check-cmd" in output


def test_guard_flags_owner_labels_and_quoted_payload_keys(tmp_path, capsys) -> None:
    label = "Provider " + "Readiness"
    quoted_key = '"' + "ai" + "_readiness" + '"'
    _write(tmp_path, "solstone/bad.py", f"TITLE = {label!r}\nKEY = {quoted_key}\n")

    assert _run(tmp_path) == 1
    output = capsys.readouterr().out
    assert "legacy-owner-label" in output
    assert "legacy-payload-key" in output


def test_guard_allows_unquoted_provider_readiness_identifier(tmp_path) -> None:
    _write(tmp_path, "solstone/good.py", "provider_readiness = {'ok': True}\n")

    assert _run(tmp_path) == 0


def test_guard_flags_unauthorized_brain_reader_import(tmp_path, capsys) -> None:
    _write(
        tmp_path,
        "solstone/think/work.py",
        "from solstone.think.brain_health import build_brain_snapshot\n",
    )

    assert _run(tmp_path) == 1
    assert "unauthorized-brain-health-reader" in capsys.readouterr().out


def test_guard_allows_declared_brain_reader_import(tmp_path) -> None:
    _write(
        tmp_path,
        "solstone/think/top.py",
        "from solstone.think.brain_health import build_brain_snapshot\n",
    )

    assert _run(tmp_path) == 0
