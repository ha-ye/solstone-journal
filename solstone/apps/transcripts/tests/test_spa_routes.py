# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations


def test_transcripts_day_serves_spa_shell(client):
    response = client.get("/app/transcripts/20260304")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_transcripts_index_redirects_to_spa_shell(client):
    response = client.get("/app/transcripts/", follow_redirects=True)

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_transcripts_day_guard_still_404s(client):
    response = client.get("/app/transcripts/notaday")

    assert response.status_code == 404
