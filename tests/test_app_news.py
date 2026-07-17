# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from unittest.mock import patch

from solstone.apps.news import copy as news_copy
from solstone.convey import create_app

VERONA_FIXTURE = Path("tests/fixtures/journal/facets/verona/news/20260310.md")


def _make_client(journal: Path):
    app = create_app(str(journal))
    app.config["TESTING"] = True
    client = app.test_client()
    return client


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
    response = client.get("/api/shell")

    assert response.status_code == 200
    names = [app["name"] for app in response.get_json()["apps"]]
    news_idx = names.index("news")
    refl_idx = names.index("reflections")
    between = names[min(news_idx, refl_idx) + 1 : max(news_idx, refl_idx)]
    assert between == []


def test_news_index_empty_state_self_explains(journal_copy):
    _clear_news(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/news/api/state")
    data = response.get_json()
    copy = data["copy"]

    assert response.status_code == 200
    assert data["newsletters"] == []
    assert copy["kicker"] == news_copy.NEWS_KICKER
    assert copy["index_h1"] == news_copy.NEWS_INDEX_H1
    assert copy["subtitle"] == news_copy.NEWS_SUBTITLE
    assert copy["empty_body"] == news_copy.NEWS_EMPTY_BODY
    assert copy["empty_next"] == "Your first newsletters arrive tomorrow morning."
    assert copy["empty_until_then"] == news_copy.NEWS_EMPTY_UNTIL_THEN
    assert copy["sample_link_label"] == news_copy.NEWS_SAMPLE_LINK_LABEL
    assert copy["sample_url"] == "/app/news/sample"


def test_news_index_empty_state_no_date_when_journal_brand_new(journal_copy):
    _clear_news(journal_copy)
    _clear_chronicle(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/news/api/state")
    copy = response.get_json()["copy"]

    assert response.status_code == 200
    assert copy["empty_next"] == news_copy.NEWS_EMPTY_NO_DATE
    assert "Your first newsletters arrive" not in copy["empty_next"]


def test_news_index_populated_lists_files_reverse_chrono(journal_copy):
    _clear_news(journal_copy)
    _seed_news(journal_copy, "personal", "20260526", "# personal 5/26")
    _seed_news(journal_copy, "solstone", "20260526", "# solstone 5/26")
    _seed_news(journal_copy, "kognova", "20260525", "# kognova 5/25")

    client = _make_client(journal_copy)
    response = client.get("/app/news/api/state")
    data = response.get_json()
    copy = data["copy"]

    assert response.status_code == 200
    assert copy["populated_framing"] == news_copy.NEWS_POPULATED_FRAMING
    assert copy["populated_sample_link"] == news_copy.NEWS_POPULATED_SAMPLE_LINK
    assert copy["populated_next_footer"] == "next newsletters: tomorrow morning"
    urls = [item["url"] for item in data["newsletters"]]
    assert "/app/news/personal/20260526" in urls
    assert "/app/news/solstone/20260526" in urls
    assert "/app/news/kognova/20260525" in urls
    assert [item["day"] for item in data["newsletters"]][:2] == [
        "20260526",
        "20260526",
    ]
    assert data["newsletters"][2]["day"] == "20260525"
    assert data["newsletters"][0]["label"] == "Tue May 26, 2026"


def test_news_detail_api_returns_file(journal_copy):
    _clear_news(journal_copy)
    _seed_news(
        journal_copy,
        "personal",
        "20260526",
        "# 2026-05-26 personal\n\nA newsletter body.\n",
    )
    client = _make_client(journal_copy)

    response = client.get("/app/news/api/personal/20260526")
    data = response.get_json()

    assert response.status_code == 200
    assert data["kicker"] == news_copy.NEWS_KICKER
    assert data["facet"] == "personal"
    assert data["date_label"] == "Tue May 26, 2026"
    assert data["subtitle"] == "sol's notes for personal on this day."
    assert data["raw_url"] == "/app/news/personal/20260526/raw"
    assert data["pdf_url"] == "/app/news/personal/20260526/pdf"
    assert data["debug_link_url"] == "/app/sol/20260526/talents/facet_newsletter"
    assert data["debug_link_label"] == news_copy.NEWS_DETAIL_DEBUG_LINK
    assert data["markdown"].startswith("# 2026-05-26 personal")


def test_news_detail_missing_page_shell_and_api_empty_state(journal_copy):
    _clear_news(journal_copy)
    client = _make_client(journal_copy)

    page_response = client.get("/app/news/nonexistent/20260526")
    api_response = client.get("/app/news/api/nonexistent/20260526")

    assert page_response.status_code == 200
    assert b'data-solstone-shell="spa"' in page_response.data
    assert api_response.status_code == 200
    data = api_response.get_json()
    assert data["empty"] is True
    assert "reason_code" not in data


def test_news_sample_api_returns_inlined_content(journal_copy):
    client = _make_client(journal_copy)
    response = client.get("/app/news/api/sample")
    data = response.get_json()

    assert response.status_code == 200
    assert data["sample_banner"] == news_copy.NEWS_SAMPLE_BANNER
    assert data["sample_h1"] == news_copy.NEWS_SAMPLE_H1
    assert "Verona Platform Joint Venture" in data["markdown"]
    assert data["raw_url"] == "/app/news/sample/raw"
    assert "pdf_url" not in data


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

    index_data = client.get("/app/news/api/state").get_json()
    detail_data = client.get("/app/news/api/personal/20260526").get_json()
    sample_data = client.get("/app/news/api/sample").get_json()
    workspace_html = client.get("/app/news/workspace").get_data(as_text=True)

    assert index_data["copy"]["index_h1"] == "newsletters"
    assert f"{detail_data['facet']} · {detail_data['date_label']}" == (
        "personal · Tue May 26, 2026"
    )
    assert sample_data["sample_h1"] == "sample newsletter"

    for selector in (".news-shell", ".news-title", ".news-header"):
        match = re.search(
            rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}",
            workspace_html,
            re.S,
        )
        if match is None:
            continue
        rule_body = match.group("body")
        assert "text-transform: uppercase" not in rule_body
        assert "text-transform: capitalize" not in rule_body


def test_news_no_run_log_string_in_surface(journal_copy):
    _seed_news(journal_copy, "personal", "20260526", "# 2026-05-26 personal\n")
    client = _make_client(journal_copy)

    texts = [
        client.get("/app/news/workspace").get_data(as_text=True),
        json.dumps(client.get("/app/news/api/state").get_json()),
        json.dumps(client.get("/app/news/api/personal/20260526").get_json()),
        json.dumps(client.get("/app/news/api/sample").get_json()),
    ]

    for text in texts:
        assert "run log" not in text.lower()


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
