# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import frontmatter
from flask import Blueprint, Response, render_template, url_for
from markdown import Markdown

from solstone.apps.news import copy as news_copy
from solstone.apps.news.dates import format_news_list_date, next_newsletter_when
from solstone.convey.utils import DATE_RE
from solstone.think.features import require_extra
from solstone.think.utils import get_journal, get_owner_timezone

news_bp = Blueprint(
    "app:news",
    __name__,
    url_prefix="/app/news",
)

# Facet directory names use the same identifier shape as facet slugs.
_FACET_RE = re.compile(r"[A-Za-z0-9_-]+")


def _journal_root() -> Path:
    return Path(get_journal())


def _facets_root() -> Path:
    return _journal_root() / "facets"


def _newsletter_path(facet: str, day: str) -> Path:
    return _facets_root() / facet / "news" / f"{day}.md"


def _plain_not_found() -> tuple[str, int, dict[str, str]]:
    return ("Newsletter not found", 404, {"Content-Type": "text/plain; charset=utf-8"})


def _list_newsletters() -> list[dict[str, str]]:
    """Return reverse-chrono list of (facet, day) newsletters.

    Reads every `facets/*/news/*.md` whose filename matches YYYYMMDD. The list
    is sorted by day desc, then facet asc for stable ordering inside a day.
    """
    facets_root = _facets_root()
    if not facets_root.is_dir():
        return []

    rows: list[dict[str, str]] = []
    for facet_dir in facets_root.iterdir():
        if not facet_dir.is_dir():
            continue
        if not _FACET_RE.fullmatch(facet_dir.name):
            continue
        news_dir = facet_dir / "news"
        if not news_dir.is_dir():
            continue
        for path in news_dir.glob("*.md"):
            if not path.is_file():
                continue
            day = path.stem
            if not DATE_RE.fullmatch(day):
                continue
            rows.append({"facet": facet_dir.name, "day": day})

    rows.sort(key=lambda r: (r["day"], r["facet"]), reverse=True)
    # Adjust facet ordering to ascending within each day.
    rows.sort(key=lambda r: r["day"], reverse=True)
    return rows


def _load_newsletter(facet: str, day: str) -> tuple[Path, str, frontmatter.Post]:
    path = _newsletter_path(facet, day)
    if not path.is_file():
        raise FileNotFoundError(f"{facet}/{day}")
    raw_markdown = path.read_text(encoding="utf-8")
    return path, raw_markdown, frontmatter.loads(raw_markdown)


def _weasyprint() -> tuple[type, Callable[..., Any]]:
    require_extra("pdf")
    from weasyprint import HTML, default_url_fetcher

    return HTML, default_url_fetcher


def _safe_pdf_url_fetcher(url: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    _, default_url_fetcher = _weasyprint()
    scheme = urlsplit(url).scheme.lower()
    if scheme in {"http", "https"}:
        raise ValueError("Remote assets are disabled for newsletter PDFs")
    return default_url_fetcher(url, *args, **kwargs)


def _render_newsletter_pdf(
    path: Path, post: frontmatter.Post, facet: str, day: str
) -> bytes:
    HTML, _ = _weasyprint()
    markdown = Markdown(extensions=["extra", "sane_lists"])
    body_html = markdown.convert(post.content)
    html = render_template(
        "news/pdf.html",
        facet=facet,
        date_label=format_news_list_date(day),
        newsletter_html=body_html,
    )
    return HTML(
        string=html,
        base_url=path.parent.resolve().as_uri(),
        url_fetcher=_safe_pdf_url_fetcher,
    ).write_pdf()


@news_bp.route("/")
def index() -> str:
    rows = _list_newsletters()
    when = next_newsletter_when(_today())

    newsletters = [
        {
            "facet": row["facet"],
            "day": row["day"],
            "label": format_news_list_date(row["day"]),
            "url": url_for("app:news.detail", facet=row["facet"], day=row["day"]),
        }
        for row in rows
    ]

    empty_next = news_copy.NEWS_EMPTY_TOMORROW_WITH_DATE.format(tomorrow=when)
    populated_next_footer = news_copy.NEWS_POPULATED_NEXT_FOOTER.format(when=when)
    if not _journal_has_any_observer_input():
        empty_next = news_copy.NEWS_EMPTY_NO_DATE

    return render_template(
        "app.html",
        app="news",
        view_mode="index",
        newsletters=newsletters,
        kicker=news_copy.NEWS_KICKER,
        index_h1=news_copy.NEWS_INDEX_H1,
        subtitle=news_copy.NEWS_SUBTITLE,
        empty_body=news_copy.NEWS_EMPTY_BODY,
        empty_next=empty_next,
        empty_until_then=news_copy.NEWS_EMPTY_UNTIL_THEN,
        sample_link_label=news_copy.NEWS_SAMPLE_LINK_LABEL,
        sample_url=url_for("app:news.sample"),
        populated_framing=news_copy.NEWS_POPULATED_FRAMING,
        populated_sample_link=news_copy.NEWS_POPULATED_SAMPLE_LINK,
        populated_next_footer=populated_next_footer,
    )


@news_bp.route("/sample")
def sample() -> Any:
    post = frontmatter.loads(news_copy.SAMPLE_CONTENT)
    return render_template(
        "app.html",
        app="news",
        view_mode="sample",
        kicker=news_copy.NEWS_KICKER,
        sample_h1=news_copy.NEWS_SAMPLE_H1,
        newsletter_markdown=post.content,
        raw_url=url_for("app:news.sample_raw"),
        sample_banner=news_copy.NEWS_SAMPLE_BANNER,
    )


@news_bp.route("/sample/raw")
def sample_raw() -> Any:
    return (
        news_copy.SAMPLE_CONTENT,
        200,
        {"Content-Type": "text/markdown; charset=utf-8"},
    )


@news_bp.route("/<facet>/<day>")
def detail(facet: str, day: str) -> Any:
    if not _FACET_RE.fullmatch(facet) or not DATE_RE.fullmatch(day):
        return _plain_not_found()

    try:
        _path, _raw_markdown, post = _load_newsletter(facet, day)
    except FileNotFoundError:
        return _plain_not_found()

    return render_template(
        "app.html",
        app="news",
        view_mode="detail",
        kicker=news_copy.NEWS_KICKER,
        detail_facet=facet,
        detail_date_label=format_news_list_date(day),
        detail_subtitle=news_copy.NEWS_DETAIL_SUBTITLE.format(facet=facet),
        debug_link_label=news_copy.NEWS_DETAIL_DEBUG_LINK,
        debug_link_url=f"/app/sol/{day}/talents/facet_newsletter",
        newsletter_markdown=post.content,
        raw_url=url_for("app:news.detail_raw", facet=facet, day=day),
        pdf_url=url_for("app:news.detail_pdf", facet=facet, day=day),
    )


@news_bp.route("/<facet>/<day>/raw")
def detail_raw(facet: str, day: str) -> Any:
    if not _FACET_RE.fullmatch(facet) or not DATE_RE.fullmatch(day):
        return _plain_not_found()

    try:
        _path, raw_markdown, _post = _load_newsletter(facet, day)
    except FileNotFoundError:
        return _plain_not_found()

    return (
        raw_markdown,
        200,
        {"Content-Type": "text/markdown; charset=utf-8"},
    )


@news_bp.route("/<facet>/<day>/pdf")
def detail_pdf(facet: str, day: str) -> Any:
    if not _FACET_RE.fullmatch(facet) or not DATE_RE.fullmatch(day):
        return _plain_not_found()

    try:
        path, _raw_markdown, post = _load_newsletter(facet, day)
        pdf_bytes = _render_newsletter_pdf(path, post, facet, day)
    except FileNotFoundError:
        return _plain_not_found()
    except ValueError as exc:
        return (str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"})

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="newsletter-{facet}-{day}.pdf"'
            )
        },
    )


def _today() -> date:
    return datetime.now(get_owner_timezone()).date()


def _journal_has_any_observer_input() -> bool:
    """Has the journal seen at least one observer-stream day?"""
    chronicle_dir = _journal_root() / "chronicle"
    if not chronicle_dir.is_dir():
        return False
    for child in chronicle_dir.iterdir():
        if not child.is_dir():
            continue
        if len(child.name) == 8 and child.name.isdigit():
            return True
    return False
