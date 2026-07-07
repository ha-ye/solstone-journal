# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import html
import re

from solstone.apps.network import copy


def _workspace_body(env) -> str:
    response = env.client.get("/app/network/workspace")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _body_text(body: str) -> str:
    return (
        html.unescape(body)
        .replace('\\"', '"')
        .replace("\\u00b7", "·")
        .replace("\\u2014", "—")
        .replace("\\u2192", "→")
    )


def _link_copy(env) -> dict[str, object]:
    response = env.client.get("/app/network/api/state")
    assert response.status_code == 200
    return response.get_json()["link_copy"]


def test_workspace_identity_fetches_once_and_not_on_poll(link_env) -> None:
    env = link_env()
    body = _workspace_body(env)

    assert body.count("/app/network/api/identity") == 1
    assert "loadIdentity()" in body
    assert re.search(r"setInterval\s*\([^)]*loadIdentity\s*\(?", body) is None


def test_workspace_identity_scaffold_is_empty_and_guarded(link_env) -> None:
    env = link_env()
    body = _workspace_body(env)

    assert 'id="link-identity-header"' in body
    match = re.search(
        r'<div id="link-identity-mark" class="link-identity-mark" '
        r'aria-hidden="true">(.*?)</div>',
        body,
        re.DOTALL,
    )
    assert match is not None
    assert match.group(1).strip() == ""
    assert "words.length !== 2" in body
    assert (
        "icons.every(icon => icon && icon.svg && icon.color && icon.color.hex)" in body
    )
    assert '<svg viewBox="0 0 24 24" fill="none" stroke="${icon.color.hex}"' in body
    assert "link-mark-chip" not in match.group(1)
    assert "<svg" not in match.group(1)


def test_workspace_identity_copy_and_visible_terms(link_env) -> None:
    env = link_env()
    body = _workspace_body(env)
    payload = _link_copy(env)

    assert 'data-copy="IDENTITY_HEADER_LABEL"' in body
    assert 'data-copy="IDENTITY_ID_LABEL"' in body
    assert payload["IDENTITY_HEADER_LABEL"] == copy.IDENTITY_HEADER_LABEL
    assert payload["IDENTITY_ID_LABEL"] == copy.IDENTITY_ID_LABEL

    visible_text = _body_text(re.sub(r"<[^>]+>", " ", body))
    assert re.search(r"\bjid\b", visible_text) is None


def test_workspace_linked_assets_load(link_env) -> None:
    env = link_env()
    body = _workspace_body(env)
    urls = [
        match.group(1)
        for pattern in (
            r'<script\b[^>]*\bsrc="([^"]+)"',
            r'<link\b[^>]*\bhref="([^"]+)"',
        )
        for match in re.finditer(pattern, body)
    ]
    assert urls

    for url in urls:
        assert url.startswith("/")
        response = env.client.get(url)
        assert response.status_code == 200, url
