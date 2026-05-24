# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from importlib import import_module

import pytest
from flask import Blueprint, Flask, g, request

import solstone.convey.state as convey_state
import solstone.think.utils as think_utils
from solstone.convey.secure_listener import ConveyIdentity

journal_sources = import_module("solstone.apps.import.journal_sources")
ingest = import_module("solstone.apps.import.ingest")

create_state_directory = journal_sources.create_state_directory
journal_source_state_prefix = journal_sources.journal_source_state_prefix
save_journal_source = journal_sources.save_journal_source
register_ingest_routes = ingest.register_ingest_routes

FINGERPRINT = "sha256:" + "d" * 64


def _pl_source() -> dict:
    return {
        "pair_mode": "pl",
        "fingerprint": FINGERPRINT,
        "device_label": "peer laptop",
        "paired_at": "2026-05-20T00:00:00Z",
        "created_at": 1000,
        "enabled": True,
        "revoked": False,
        "revoked_at": None,
        "stats": {
            "segments_received": 0,
            "entities_received": 0,
            "facets_received": 0,
            "imports_received": 0,
            "config_received": 0,
        },
    }


def _pl_identity() -> ConveyIdentity:
    return ConveyIdentity(
        mode="pl-direct",
        fingerprint=FINGERPRINT,
        device_label="peer laptop",
        paired_at="2026-05-20T00:00:00Z",
        session_id="session-1",
    )


@pytest.fixture
def pl_ingest_env(tmp_path, monkeypatch):
    monkeypatch.setattr(convey_state, "journal_root", str(tmp_path), raising=False)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    think_utils._journal_path_cache = None
    (tmp_path / "apps" / "import" / "journal_sources").mkdir(
        parents=True, exist_ok=True
    )

    source = _pl_source()
    assert save_journal_source(source) is True
    prefix = journal_source_state_prefix(source)
    create_state_directory(tmp_path, prefix)

    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.before_request
    def stamp_identity():
        stamped = request.environ.get("pl.identity")
        if stamped is not None:
            g.identity = stamped

    bp = Blueprint("import-test", __name__, url_prefix="/app/import")
    register_ingest_routes(bp)
    app.register_blueprint(bp)

    return {"client": app.test_client(), "prefix": prefix}


ROUTES = [
    ("entities", {"json": {"entities": []}}),
    ("imports", {"json": {"imports": []}}),
    ("config", {"json": {"config": {}}}),
]


@pytest.mark.parametrize(("route", "request_kwargs"), ROUTES)
def test_pl_ingest_routes_accept_fingerprint_prefix(
    pl_ingest_env, route: str, request_kwargs: dict
) -> None:
    response = pl_ingest_env["client"].post(
        f"/app/import/journal/{pl_ingest_env['prefix']}/ingest/{route}",
        environ_overrides={"pl.identity": _pl_identity()},
        **request_kwargs,
    )

    assert response.status_code == 200


@pytest.mark.parametrize(("route", "request_kwargs"), ROUTES)
def test_pl_ingest_routes_reject_wrong_prefix(
    pl_ingest_env, route: str, request_kwargs: dict
) -> None:
    response = pl_ingest_env["client"].post(
        f"/app/import/journal/deadbeef/ingest/{route}",
        environ_overrides={"pl.identity": _pl_identity()},
        **request_kwargs,
    )

    assert response.status_code == 403
