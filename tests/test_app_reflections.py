# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from unittest.mock import patch

from solstone.apps.reflections import copy as reflections_copy
from solstone.convey import create_app

REFLECTION_FIXTURE = Path("tests/fixtures/journal/reflections/weekly/20260308.md")


def _seed_reflection(journal: Path, content: str | None = None) -> None:
    target = journal / "reflections" / "weekly" / "20260308.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        content
        if content is not None
        else REFLECTION_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _make_client(journal: Path):
    app = create_app(str(journal))
    app.config["TESTING"] = True
    client = app.test_client()
    return client


def _clear_weekly_reflections(journal: Path) -> None:
    shutil.rmtree(journal / "reflections" / "weekly", ignore_errors=True)


def test_reflections_index_lists_available_weeks(journal_copy):
    _seed_reflection(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/api/state")
    data = response.get_json()

    assert response.status_code == 200
    assert data["copy"]["populated_framing"] == reflections_copy.POPULATED_FRAMING
    assert data["weeks"] == [
        {
            "day": "20260308",
            "label": "Sunday March 8th",
            "url": "/app/reflections/20260308",
        }
    ]


def test_reflections_index_empty_state_shows_new_copy_next_date_and_sample_link(
    monkeypatch, journal_copy
):
    _clear_weekly_reflections(journal_copy)
    monkeypatch.setattr(
        "solstone.apps.reflections.routes.next_reflection_sunday",
        lambda journal, today, tz: "Sunday, March 15",
    )
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/api/state")
    data = response.get_json()
    copy = data["copy"]

    assert response.status_code == 200
    assert data["weeks"] == []
    assert copy["subtitle"] == reflections_copy.SUBTITLE
    assert copy["empty_body"] == reflections_copy.EMPTY_BODY
    assert copy["empty_next"] == "Your first reflection arrives on Sunday, March 15."
    assert copy["empty_until_then"] == reflections_copy.EMPTY_UNTIL_THEN
    assert copy["sample_url"] == "/app/reflections/sample"
    assert copy["sample_link_label"] == reflections_copy.SAMPLE_LINK_LABEL


def test_reflections_index_empty_state_uses_fallback_when_next_date_unavailable(
    monkeypatch, journal_copy
):
    _clear_weekly_reflections(journal_copy)
    monkeypatch.setattr(
        "solstone.apps.reflections.routes.next_reflection_sunday",
        lambda journal, today, tz: None,
    )
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/api/state")
    copy = response.get_json()["copy"]

    assert response.status_code == 200
    assert copy["empty_next"] == reflections_copy.EMPTY_NEXT_NO_DATE
    assert copy["populated_next_footer"] is None


def test_reflections_index_populated_state_shows_framing_sample_link_and_next_footer(
    monkeypatch, journal_copy
):
    _seed_reflection(journal_copy)
    monkeypatch.setattr(
        "solstone.apps.reflections.routes.next_reflection_sunday",
        lambda journal, today, tz: "Sunday, March 15",
    )
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/api/state")
    copy = response.get_json()["copy"]

    assert response.status_code == 200
    assert copy["populated_framing"] == reflections_copy.POPULATED_FRAMING
    assert copy["sample_url"] == "/app/reflections/sample"
    assert copy["populated_sample_link"] == reflections_copy.POPULATED_SAMPLE_LINK
    assert copy["populated_next_footer"] == "next reflection: Sunday, March 15"


def test_reflections_detail_api_returns_week(journal_copy):
    _seed_reflection(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/api/20260308")
    data = response.get_json()

    assert response.status_code == 200
    assert data["day"] == "20260308"
    assert data["week_label"] == "Sunday March 8th"
    assert data["raw_url"] == "/app/reflections/20260308/raw"
    assert data["pdf_url"] == "/app/reflections/20260308/pdf"
    assert "boardroom balcony inflection" in data["markdown"]


def test_reflections_sample_api_returns_fixture_markdown(journal_copy):
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/api/sample")
    data = response.get_json()

    assert response.status_code == 200
    assert data["sample_banner"] == reflections_copy.SAMPLE_BANNER
    assert "boardroom balcony inflection" in data["markdown"]
    assert data["raw_url"] == "/app/reflections/sample/raw"
    assert "pdf_url" not in data


def test_reflections_sample_raw_returns_markdown(journal_copy):
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/sample/raw")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert text.startswith("---\ntype: weekly_reflection")
    assert "boardroom balcony inflection" in text


def test_reflections_sample_content_matches_fixture_on_disk():
    fixture_text = REFLECTION_FIXTURE.read_text(encoding="utf-8")
    assert reflections_copy.SAMPLE_CONTENT == fixture_text


def test_reflections_no_uppercase_transform_on_title(journal_copy):
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/workspace")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    for selector in (
        ".reflection-shell",
        ".reflection-header",
        ".reflection-title",
    ):
        match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", html, re.S)
        if match is None:
            continue
        rule_body = match.group("body")
        assert "text-transform: uppercase" not in rule_body
        assert "text-transform: capitalize" not in rule_body


def test_reflections_no_mirror_string_in_surface(journal_copy):
    _seed_reflection(journal_copy)
    client = _make_client(journal_copy)

    texts = [
        client.get("/app/reflections/workspace").get_data(as_text=True),
        json.dumps(client.get("/app/reflections/api/state").get_json()),
        json.dumps(client.get("/app/reflections/api/20260308").get_json()),
        json.dumps(client.get("/app/reflections/api/sample").get_json()),
    ]

    for text in texts:
        assert "mirror" not in text.lower()
        assert "🪞" not in text


def test_reflections_app_json_icon_is_moon():
    data = json.loads(Path("solstone/apps/reflections/app.json").read_text())

    assert data["icon"] == "🌙"


def test_reflections_detail_canonicalizes_to_sunday_in_api(journal_copy):
    _seed_reflection(journal_copy)
    client = _make_client(journal_copy)

    page_response = client.get("/app/reflections/20260310")
    api_response = client.get("/app/reflections/api/20260310")

    assert page_response.status_code == 200
    assert b'data-solstone-shell="spa"' in page_response.data
    assert api_response.status_code == 200
    assert api_response.get_json()["day"] == "20260308"


def test_reflections_missing_week_returns_api_404(journal_copy):
    client = _make_client(journal_copy)

    page_response = client.get("/app/reflections/20260315")
    api_response = client.get("/app/reflections/api/20260315")

    assert page_response.status_code == 200
    assert b'data-solstone-shell="spa"' in page_response.data
    assert api_response.status_code == 404
    assert api_response.get_json()["reason_code"] == "file_not_found"


def test_reflections_raw_returns_markdown(journal_copy):
    _seed_reflection(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/20260308/raw")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert text.startswith("---\ntype: weekly_reflection")


def test_reflections_pdf_returns_attachment(journal_copy):
    _seed_reflection(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/20260308/pdf")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert (
        response.headers["Content-Disposition"]
        == 'attachment; filename="reflection-20260308.pdf"'
    )
    assert response.data.startswith(b"%PDF")


def test_reflections_pdf_rejects_remote_assets(journal_copy):
    _seed_reflection(
        journal_copy,
        """---
type: weekly_reflection
week: 20260308
generated: 2026-03-10T19:00:00Z
model: openai/gpt-5
sources:
  newsletters: 0
  activities: 0
  decisions: 0
  followups: 0
  relationship_signals: 0
gaps: []
---

![remote](https://example.com/reflection.png)
""",
    )
    client = _make_client(journal_copy)

    with (
        patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("network disabled during reflection pdf render"),
        ),
        patch("weasyprint.default_url_fetcher") as mock_fetcher,
    ):
        response = client.get("/app/reflections/20260308/pdf")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF")
    mock_fetcher.assert_not_called()


def test_reflections_stats_returns_month_counts(journal_copy):
    _seed_reflection(journal_copy)
    client = _make_client(journal_copy)

    response = client.get("/app/reflections/api/stats/202603")

    assert response.status_code == 200
    assert response.get_json() == {"20260308": 1}
