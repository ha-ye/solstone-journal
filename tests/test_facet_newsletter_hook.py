# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
from pathlib import Path


def _write_facet(journal: Path, facet: str) -> None:
    (journal / "facets" / facet).mkdir(parents=True, exist_ok=True)


def test_facet_newsletter_post_process_writes_news(tmp_path, monkeypatch):
    from solstone.talent.facet_newsletter import post_process

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_facet(tmp_path, "work")

    result = post_process(
        "# Work News\n\nThe launch review moved forward.",
        {"facet": "work", "day": "20260609"},
    )

    news_path = tmp_path / "facets" / "work" / "news" / "20260609.md"
    assert result is None
    assert news_path.read_text(encoding="utf-8") == (
        "# Work News\n\nThe launch review moved forward."
    )


def test_facet_newsletter_post_process_skips_no_activity(tmp_path, monkeypatch, caplog):
    from solstone.talent.facet_newsletter import post_process

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_facet(tmp_path, "work")
    caplog.set_level(logging.INFO, logger="solstone.talent.facet_newsletter")

    result = post_process("No activity", {"facet": "work", "day": "20260609"})

    assert result is None
    assert not (tmp_path / "facets" / "work" / "news").exists()
    assert "no activity" in caplog.text


def test_facet_newsletter_post_process_skips_blank(tmp_path, monkeypatch, caplog):
    from solstone.talent.facet_newsletter import post_process

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_facet(tmp_path, "work")
    caplog.set_level(logging.INFO, logger="solstone.talent.facet_newsletter")

    result = post_process("  \n", {"facet": "work", "day": "20260609"})

    assert result is None
    assert not (tmp_path / "facets" / "work" / "news").exists()
    assert "blank newsletter" in caplog.text


def test_facet_newsletter_post_process_missing_facet_logs(
    tmp_path, monkeypatch, caplog
):
    from solstone.talent.facet_newsletter import post_process

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    caplog.set_level(logging.ERROR, logger="solstone.talent.facet_newsletter")

    result = post_process("# News", {"day": "20260609"})

    assert result is None
    assert "missing facet" in caplog.text


def test_facet_newsletter_post_process_missing_day_logs(tmp_path, monkeypatch, caplog):
    from solstone.talent.facet_newsletter import post_process

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_facet(tmp_path, "work")
    caplog.set_level(logging.ERROR, logger="solstone.talent.facet_newsletter")

    result = post_process("# News", {"facet": "work"})

    assert result is None
    assert "missing day" in caplog.text


def test_facet_newsletter_post_process_facet_error_logs_no_raise(
    tmp_path, monkeypatch, caplog
):
    from solstone.talent.facet_newsletter import post_process

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    caplog.set_level(logging.ERROR, logger="solstone.talent.facet_newsletter")

    result = post_process("# News", {"facet": "missing", "day": "20260609"})

    assert result is None
    assert "Facet 'missing' not found" in caplog.text
