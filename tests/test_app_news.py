# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
from html import unescape
from pathlib import Path
from unittest.mock import patch

from solstone.apps.news import copy as news_copy
from solstone.convey import create_app

VERONA_FIXTURE = Path("tests/fixtures/journal/facets/verona/news/20260310.md")


def _make_client(journal: Path):
    app = create_app(str(journal))
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session.permanent = True
    return client


def _html(response) -> str:
    return unescape(response.get_data(as_text=True))


def _seed_news(journal: Path, facet: str, day: str, body: str) -> None:
    target = journal / "facets" / facet / "news" / f"{day}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _clear_news(journal: Path) -> None:
    facets_dir = journal / "facets"
    if not facets_dir.is_dir():
        return
    for facet_dir in facets_dir.iterdir():
        news_dir = facet_dir / "news"
        if news_dir.is_dir():
            shutil.rmtree(news_dir, ignore_errors=True)


def _clear_chronicle(journal: Path) -> None:
    shutil.rmtree(journal / "chronicle", ignore_errors=True)


def test_news_app_json_icon_and_label():
    data = json.loads(Path("solstone/apps/news/app.json").read_text())
    assert data["icon"] == "📰"
    assert data["label"] == "newsletters"


def test_news_sidebar_adjacent_to_reflections(journal_copy):
    client = _make_client(journal_copy)
    response = client.get("/app/news/")
    html = _html(response)
    # After convey config seeding, news and reflections sit next to each
    # other in the unstarred drawer (DEFAULT_APP_ORDER ends in
    # `reflections, news`).
    news_idx = html.find('data-app-name="news"')
    refl_idx = html.find('data-app-name="reflections"')
    assert news_idx > 0
    assert refl_idx > 0
    between = html[min(news_idx, refl_idx) : max(news_idx, refl_idx)]
    # No other app entry should appear between them.
    assert not re.search(r'data-app-name="(?!news"|reflections")[^"]+"', between)


def test_news_index_empty_state_self_explains(journal_copy):
    _clear_news(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/news/")
    html = _html(response)

    assert response.status_code == 200
    assert news_copy.NEWS_KICKER in html
    assert ">" + news_copy.NEWS_INDEX_H1 + "<" in html
    assert news_copy.NEWS_SUBTITLE in html
    assert news_copy.NEWS_EMPTY_BODY in html
    # Tomorrow cue with the date token interpolated as the safe fallback.
    assert "Your first newsletters arrive tomorrow morning." in html
    assert news_copy.NEWS_EMPTY_UNTIL_THEN in html
    assert news_copy.NEWS_SAMPLE_LINK_LABEL in html
    assert 'href="/app/news/sample"' in html


def test_news_index_empty_state_no_date_when_journal_brand_new(journal_copy):
    _clear_news(journal_copy)
    _clear_chronicle(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/news/")
    html = _html(response)

    assert response.status_code == 200
    assert news_copy.NEWS_EMPTY_NO_DATE in html
    assert "Your first newsletters arrive" not in html


def test_news_index_populated_lists_files_reverse_chrono(journal_copy):
    _clear_news(journal_copy)
    _seed_news(journal_copy, "personal", "20260526", "# personal 5/26")
    _seed_news(journal_copy, "solstone", "20260526", "# solstone 5/26")
    _seed_news(journal_copy, "kognova", "20260525", "# kognova 5/25")

    client = _make_client(journal_copy)
    response = client.get("/app/news/")
    html = _html(response)

    assert response.status_code == 200
    assert news_copy.NEWS_POPULATED_FRAMING in html
    assert news_copy.NEWS_POPULATED_SAMPLE_LINK in html
    assert 'href="/app/news/personal/20260526"' in html
    assert 'href="/app/news/solstone/20260526"' in html
    assert 'href="/app/news/kognova/20260525"' in html
    # Reverse-chrono: 5/26 entries appear before the 5/25 entry.
    p26 = html.find('href="/app/news/personal/20260526"')
    k25 = html.find('href="/app/news/kognova/20260525"')
    assert 0 < p26 < k25
    # Date label format `Tue May 26, 2026` with no leading zero on day.
    assert "Tue May 26, 2026" in html
    assert "20260526" in html
    # Footer line.
    assert "next newsletters: tomorrow morning" in html


def test_news_detail_renders_file(journal_copy):
    _clear_news(journal_copy)
    _seed_news(
        journal_copy,
        "personal",
        "20260526",
        "# 2026-05-26 personal\n\nA newsletter body.\n",
    )
    client = _make_client(journal_copy)

    response = client.get("/app/news/personal/20260526")
    html = _html(response)

    assert response.status_code == 200
    assert news_copy.NEWS_KICKER in html
    assert "personal · Tue May 26, 2026" in html
    assert "sol's notes for personal on this day." in html
    assert ">copy<" in html
    assert ">download PDF<" in html
    assert 'href="/app/sol/20260526/talents/facet_newsletter"' in html
    assert news_copy.NEWS_DETAIL_DEBUG_LINK in html


def test_news_detail_404_on_missing(journal_copy):
    _clear_news(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/news/nonexistent/20260526")

    assert response.status_code == 404
    assert response.mimetype == "text/plain"
    assert "Newsletter not found" in response.get_data(as_text=True)


def test_news_sample_renders_inlined_content(journal_copy):
    client = _make_client(journal_copy)
    response = client.get("/app/news/sample")
    html = _html(response)

    assert response.status_code == 200
    assert news_copy.NEWS_SAMPLE_BANNER in html
    assert ">" + news_copy.NEWS_SAMPLE_H1 + "<" in html
    assert "Verona Platform Joint Venture" in html
    assert 'const rawUrl = "/app/news/sample/raw";' in html
    # PDF button hidden on sample page.
    assert "download PDF" not in html


def test_news_sample_raw_returns_markdown(journal_copy):
    client = _make_client(journal_copy)
    response = client.get("/app/news/sample/raw")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert "Verona Platform Joint Venture" in text


def test_news_sample_content_matches_fixture():
    """SAMPLE_CONTENT must stay in sync with the on-disk verona fixture.

    The fixture is the source of truth for sample bytes. SAMPLE_CONTENT is the
    inlined copy that ships in PyPI wheels (tests/fixtures/ is excluded from
    the wheel — A21 / req_2ntkhdiv lesson). This test fails when either side
    drifts.
    """
    fixture_text = VERONA_FIXTURE.read_text(encoding="utf-8")
    assert news_copy.SAMPLE_CONTENT == fixture_text


def test_news_h1s_are_lowercase(journal_copy):
    _seed_news(journal_copy, "personal", "20260526", "# 2026-05-26 personal\n")
    client = _make_client(journal_copy)

    index_html = client.get("/app/news/").get_data(as_text=True)
    detail_html = client.get("/app/news/personal/20260526").get_data(as_text=True)
    sample_html = client.get("/app/news/sample").get_data(as_text=True)

    # Visible H1 / title strings stay lowercase as authored.
    assert ">newsletters<" in index_html
    assert "personal · Tue May 26, 2026" in detail_html
    assert ">sample newsletter<" in sample_html

    # No CSS text-transform: capitalize/uppercase on news selectors.
    for html in (index_html, detail_html, sample_html):
        for selector in (".news-shell", ".news-title", ".news-header"):
            match = re.search(
                rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", html, re.S
            )
            if match is None:
                continue
            rule_body = match.group("body")
            assert "text-transform: uppercase" not in rule_body
            assert "text-transform: capitalize" not in rule_body


def test_news_no_run_log_string_in_surface(journal_copy):
    _seed_news(journal_copy, "personal", "20260526", "# 2026-05-26 personal\n")
    client = _make_client(journal_copy)

    for url in (
        "/app/news/",
        "/app/news/personal/20260526",
        "/app/news/sample",
    ):
        response = client.get(url)
        html = _html(response)
        assert response.status_code == 200
        assert "run log" not in html.lower()


def test_news_detail_raw_returns_markdown(journal_copy):
    _seed_news(
        journal_copy,
        "personal",
        "20260526",
        "# 2026-05-26 personal\n\nbody\n",
    )
    client = _make_client(journal_copy)

    response = client.get("/app/news/personal/20260526/raw")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert text.startswith("# 2026-05-26 personal")


def test_news_detail_pdf_returns_attachment(journal_copy):
    _seed_news(
        journal_copy,
        "personal",
        "20260526",
        "# 2026-05-26 personal\n\nbody\n",
    )
    client = _make_client(journal_copy)

    response = client.get("/app/news/personal/20260526/pdf")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert (
        response.headers["Content-Disposition"]
        == 'attachment; filename="newsletter-personal-20260526.pdf"'
    )
    assert response.data.startswith(b"%PDF")


def test_news_detail_pdf_rejects_remote_assets(journal_copy):
    _seed_news(
        journal_copy,
        "personal",
        "20260526",
        "# 2026-05-26 personal\n\n![remote](https://example.com/n.png)\n",
    )
    client = _make_client(journal_copy)

    with (
        patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("network disabled during news pdf render"),
        ),
        patch("weasyprint.default_url_fetcher") as mock_fetcher,
    ):
        response = client.get("/app/news/personal/20260526/pdf")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF")
    mock_fetcher.assert_not_called()
