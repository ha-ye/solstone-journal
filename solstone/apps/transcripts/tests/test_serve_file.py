# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import shutil
from pathlib import Path

import pytest

from solstone.convey import create_app

DAY = "20990104"
STREAM = "pro5e"
SEGMENT = "122500_300"
MEDIA_FILE = "display_1_screen.mp4"
FIXTURE_MEDIA = Path(__file__).parent / "fixtures" / "tiny-h264.mp4"
SERVE_URL = f"/app/transcripts/api/serve_file/{DAY}/{STREAM}/{SEGMENT}/{MEDIA_FILE}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    config_dir = journal / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "convey": {"trust_localhost": True},
                "setup": {"completed_at": 1700000000000},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    segment_dir = journal / "chronicle" / DAY / STREAM / SEGMENT
    segment_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURE_MEDIA, segment_dir / MEDIA_FILE)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    app = create_app(str(journal))
    return app.test_client()


def test_serve_file_path_traversal_returns_non_200(client):
    response = client.get(
        "/app/transcripts/api/serve_file/20240101/../../../etc/passwd"
    )

    assert response.status_code != 200


def test_serve_file_malformed_day_returns_404(client):
    response = client.get("/app/transcripts/api/serve_file/notadate/foo")

    assert response.status_code == 404


def test_serve_file_returns_video_mp4_with_range_support(client):
    response = client.get(SERVE_URL)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("video/mp4")
    assert response.data[4:8] == b"ftyp"

    range_response = client.get(SERVE_URL, headers={"Range": "bytes=0-127"})

    assert range_response.status_code == 206
    assert range_response.headers["Accept-Ranges"] == "bytes"
    assert range_response.headers["Content-Range"].startswith("bytes 0-127/")
    assert len(range_response.data) == 128
