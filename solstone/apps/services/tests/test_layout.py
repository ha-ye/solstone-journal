# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Layout-source tests for the services app."""

from __future__ import annotations

import re
from pathlib import Path

_MEDIA_OPEN = re.compile(r"@media\s*\(\s*max-width\s*:\s*(\d+)px\s*\)\s*\{")
_CSS_RULE = re.compile(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}", re.DOTALL)
_LEFT_CLEARANCE = re.compile(
    r"\b(?:padding-left|margin-left)\s*:\s*[^;]*--menu-bar-width[^;]*;",
    re.DOTALL,
)
_BOTTOM_CLEARANCE = re.compile(
    r"\b(?:padding-bottom|margin-bottom)\s*:\s*[^;]*--app-bar-height[^;]*;",
    re.DOTALL,
)


def _services_css() -> str:
    return Path("solstone/apps/services/static/services.css").read_text(
        encoding="utf-8"
    )


def _media_spans(css: str) -> list[tuple[int, int, int, str]]:
    spans: list[tuple[int, int, int, str]] = []
    for match in _MEDIA_OPEN.finditer(css):
        depth = 1
        index = match.end()
        while index < len(css) and depth > 0:
            char = css[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        if depth != 0:
            raise AssertionError("unterminated @media block in services.css")
        spans.append(
            (match.start(), index, int(match.group(1)), css[match.end() : index - 1])
        )
    return spans


def _narrow_media_blocks(css: str) -> list[str]:
    return [body for _start, _end, width, body in _media_spans(css) if width <= 768]


def _selector_root_tokens(selector: str) -> set[str]:
    tokens: set[str] = set()
    if re.search(r"(?<![\w-])\.services-shell(?![\w-])", selector):
        tokens.add("services-shell")
    if re.search(r"\[data-services-root(?:[\]\s=~|^$*])", selector):
        tokens.add("data-services-root")
    return tokens


def _clearance_tokens(blocks: list[str], declaration: re.Pattern[str]) -> set[str]:
    tokens: set[str] = set()
    for block in blocks:
        for match in _CSS_RULE.finditer(block):
            selector_tokens = _selector_root_tokens(match.group("selector"))
            if selector_tokens and declaration.search(match.group("body")):
                tokens.update(selector_tokens)
    return tokens


def _class_token_present(html: str, class_name: str) -> bool:
    return any(
        class_name in class_attr.split()
        for class_attr in re.findall(r'class="([^"]*)"', html)
    )


def _root_token_present(html: str, token: str) -> bool:
    if token.startswith("data-"):
        return bool(re.search(rf"\s{re.escape(token)}(?:[=\s>]|$)", html))
    return _class_token_present(html, token)


def _row_html(html: str, service: str) -> str:
    match = re.search(
        rf'<article\b(?=[^>]*\bdata-service-row="{re.escape(service)}")[^>]*>'
        r".*?</article>",
        html,
        re.DOTALL,
    )
    assert match, f"services row not rendered for {service}"
    return match.group(0)


def _rendered_services_html(services_env) -> str:
    return services_env().client.get("/app/services/").get_data(as_text=True)


def test_narrow_rules_bound_to_rendered_surface(services_env):
    css = _services_css()
    html = _rendered_services_html(services_env)
    blocks = _narrow_media_blocks(css)

    left_tokens = _clearance_tokens(blocks, _LEFT_CLEARANCE)
    assert left_tokens, "narrow Services root rule must reserve menu-bar width"

    bottom_tokens = _clearance_tokens(blocks, _BOTTOM_CLEARANCE)
    assert bottom_tokens, "narrow Services root rule must reserve app-bar height"

    # These assertions catch both failure modes: deleting a clearance rule leaves
    # no tokens above, and renaming a CSS selector only leaves a token absent here.
    for token in left_tokens | bottom_tokens:
        assert _root_token_present(html, token), f"{token} selector not rendered"


def test_mobile_rules_preserve_all_row_content(services_env):
    html = _rendered_services_html(services_env)

    for service in ("scout", "spl", "spb", "spn"):
        row = _row_html(html, service)
        assert re.search(r"<h2\b[^>]*>.*?</h2>", row, re.DOTALL)
        assert _class_token_present(row, "services-state")
        assert _class_token_present(row, "services-description")
        actions = re.findall(
            r'<button\b(?=[^>]*class="[^"]*\bservices-action\b[^"]*")'
            r'(?=[^>]*data-action="([^"]+)")[^>]*>',
            row,
        )
        assert sorted(actions) == ["disable", "enable", "refresh"]

    for service in ("scout", "spl", "spb"):
        assert _class_token_present(_row_html(html, service), "services-manage")
    for service in ("spn",):
        assert _class_token_present(_row_html(html, service), "services-coming-soon")

    css = _services_css()
    # Scope this check to media bodies only; the top-level JS-driven [hidden]
    # display rule is legitimate and must not be flagged here.
    narrow_css = "\n".join(_narrow_media_blocks(css))
    normalized = re.sub(r"\s*:\s*", ":", narrow_css.lower())
    for forbidden in (
        "display:none",
        "text-overflow:ellipsis",
        "visibility:hidden",
        "font-size:0",
    ):
        assert forbidden not in normalized
