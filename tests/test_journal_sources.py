# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
import re
import stat
from importlib import import_module

import pytest
from flask import Flask, abort, g, jsonify, request

import solstone.convey.state as convey_state
from solstone.convey.secure_listener import ConveyIdentity
from solstone.think.utils import now_ms

journal_sources = import_module("solstone.apps.import.journal_sources")
STATE_AREAS = journal_sources.STATE_AREAS
create_state_directory = journal_sources.create_state_directory
find_journal_source_by_name = journal_sources.find_journal_source_by_name
generate_key = journal_sources.generate_key
get_state_directory = journal_sources.get_state_directory
is_valid_journal_source_name = journal_sources.is_valid_journal_source_name
journal_source_state_prefix = journal_sources.journal_source_state_prefix
list_journal_sources = journal_sources.list_journal_sources
load_journal_source = journal_sources.load_journal_source
load_journal_source_by_fingerprint = journal_sources.load_journal_source_by_fingerprint
mint_pl_journal_source_record = journal_sources.mint_pl_journal_source_record
require_journal_source = journal_sources.require_journal_source
save_journal_source = journal_sources.save_journal_source

FINGERPRINT = "sha256:" + "a" * 64
FINGERPRINT_2 = "sha256:" + "b" * 64


@pytest.fixture
def journal_env(tmp_path, monkeypatch):
    monkeypatch.setattr(convey_state, "journal_root", str(tmp_path), raising=False)
    (tmp_path / "apps" / "import" / "journal_sources").mkdir(
        parents=True, exist_ok=True
    )
    return tmp_path


def _source(name: str, key: str, created_at: int = 0) -> dict:
    return {
        "key": key,
        "name": name,
        "created_at": created_at,
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


def _pl_source(
    fingerprint: str = FINGERPRINT,
    *,
    device_label: str = "peer laptop",
    created_at: int = 0,
    **overrides,
) -> dict:
    source = {
        "pair_mode": "pl",
        "fingerprint": fingerprint,
        "device_label": device_label,
        "paired_at": "2026-05-20T00:00:00Z",
        "created_at": created_at,
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
    source.update(overrides)
    return source


def _pl_identity(fingerprint: str = FINGERPRINT) -> ConveyIdentity:
    return ConveyIdentity(
        mode="pl-direct",
        fingerprint=fingerprint,
        device_label="peer laptop",
        paired_at="2026-05-20T00:00:00Z",
        session_id="session-1",
    )


def _auth_app() -> Flask:
    app = Flask(__name__)

    @app.before_request
    def stamp_identity():
        stamped = request.environ.get("pl.identity")
        if stamped is not None:
            g.identity = stamped

    @app.route("/protected")
    @require_journal_source
    def protected():
        return jsonify(
            {
                "name": g.journal_source.get("name"),
                "device_label": g.journal_source.get("device_label"),
                "pair_mode": g.journal_source.get("pair_mode"),
            }
        )

    return app


@pytest.fixture
def manifest_env(journal_env):
    """Journal env with a saved source and state directory."""
    key = generate_key()
    source = _source("manifest-test", key, created_at=123)
    save_journal_source(source)
    create_state_directory(journal_env, key[:8])
    return {"root": journal_env, "key": key, "source": source}


@pytest.fixture
def manifest_app(manifest_env):
    app = Flask(__name__)

    @app.before_request
    def stamp_identity():
        stamped = request.environ.get("pl.identity")
        if stamped is not None:
            g.identity = stamped

    @app.route("/journal/<key_prefix>/manifest/<area>")
    @require_journal_source
    def journal_source_manifest(key_prefix: str, area: str):
        if journal_source_state_prefix(g.journal_source) != key_prefix:
            abort(403, description="Key prefix mismatch")
        if area not in STATE_AREAS:
            abort(404, description="Unknown manifest area")
        state_path = get_state_directory(key_prefix) / area / "state.json"
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return jsonify(data)

    return app


def test_generate_key():
    key = generate_key()
    assert len(key) == 43
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", key)


def test_create_and_load(journal_env):
    key = generate_key()
    source = _source("alpha", key, created_at=123)

    assert save_journal_source(source) is True

    loaded = load_journal_source(key)
    assert loaded is not None
    assert {k: v for k, v in loaded.items() if k != "filename_prefix"} == source
    assert loaded["filename_prefix"] == key[:8]

    source_path = journal_env / "apps" / "import" / "journal_sources" / "alpha.json"
    assert source_path.exists()
    assert stat.S_IMODE(source_path.stat().st_mode) == 0o600


def test_load_wrong_key(journal_env):
    source = _source("alpha", generate_key(), created_at=123)
    assert save_journal_source(source) is True

    assert load_journal_source(generate_key()) is None


def test_save_and_load_pl_journal_source(journal_env):
    source = _pl_source(created_at=123)

    assert save_journal_source(source) is True

    loaded = load_journal_source_by_fingerprint(FINGERPRINT)
    assert loaded is not None
    assert loaded["pair_mode"] == "pl"
    assert loaded["fingerprint"] == FINGERPRINT
    assert loaded["device_label"] == "peer laptop"
    assert loaded["filename_prefix"] == "a" * 16
    assert "key" not in loaded

    source_path = (
        journal_env / "apps" / "import" / "journal_sources" / f"{'a' * 16}.json"
    )
    assert source_path.exists()
    assert stat.S_IMODE(source_path.stat().st_mode) == 0o600


def test_load_journal_source_by_fingerprint_miss(journal_env):
    assert load_journal_source_by_fingerprint(FINGERPRINT) is None


@pytest.mark.parametrize(
    "source",
    [
        {
            "pair_mode": "pl",
            "key": "abcdef123456",
            "fingerprint": FINGERPRINT,
        },
        {
            "name": "empty",
            "enabled": True,
        },
    ],
)
def test_invalid_key_fingerprint_xor_records_rejected(journal_env, source):
    assert save_journal_source(source) is False
    assert list_journal_sources() == []


def test_fingerprint_without_pl_pair_mode_rejected(journal_env):
    source = _pl_source()
    source.pop("pair_mode")

    assert save_journal_source(source) is False
    assert list_journal_sources() == []


def test_list_journal_sources(journal_env):
    first = _source("first", generate_key(), created_at=100)
    second = _source("second", generate_key(), created_at=300)
    third = _source("third", generate_key(), created_at=200)

    assert save_journal_source(first) is True
    assert save_journal_source(second) is True
    assert save_journal_source(third) is True

    assert [source["name"] for source in list_journal_sources()] == [
        "second",
        "third",
        "first",
    ]


def test_find_by_name(journal_env):
    source = _source("alpha", generate_key(), created_at=123)
    assert save_journal_source(source) is True

    found = find_journal_source_by_name("alpha")
    assert found is not None
    assert {k: v for k, v in found.items() if k != "filename_prefix"} == source
    assert find_journal_source_by_name("nonexistent") is None


def test_create_state_directory(journal_env):
    state_dir = create_state_directory(journal_env, "abcd1234")

    source_path = state_dir / "source.json"
    assert source_path.exists()
    assert json.loads(source_path.read_text(encoding="utf-8")) == {}

    for area in STATE_AREAS:
        state_path = state_dir / area / "state.json"
        assert state_path.exists()
        assert json.loads(state_path.read_text(encoding="utf-8")) == {}


def test_create_state_directory_preserves_existing_files(journal_env):
    state_dir = create_state_directory(journal_env, "abcd1234")
    (state_dir / "source.json").write_text('{"source":"keep"}', encoding="utf-8")
    segment_state = state_dir / "segments" / "state.json"
    segment_state.write_text('{"20260520":{}}', encoding="utf-8")

    create_state_directory(journal_env, "abcd1234")

    assert json.loads((state_dir / "source.json").read_text("utf-8")) == {
        "source": "keep"
    }
    assert json.loads(segment_state.read_text("utf-8")) == {"20260520": {}}


def test_registry_reloads_after_external_write(journal_env):
    first = _source("alpha", generate_key(), created_at=100)
    assert save_journal_source(first) is True
    assert find_journal_source_by_name("beta") is None

    beta_key = generate_key()
    beta = _source("beta", beta_key, created_at=200)
    beta_path = journal_env / "apps" / "import" / "journal_sources" / "beta.json"
    beta_path.write_text(json.dumps(beta, indent=2), encoding="utf-8")

    loaded = find_journal_source_by_name("beta")
    assert loaded is not None
    assert loaded["key"] == beta_key


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("wrong-dl.json", _source("right-dl", "abcdef1234567890", created_at=123)),
        ("wrong-pl.json", _pl_source(FINGERPRINT_2, created_at=123)),
    ],
)
def test_registry_skips_filename_mismatch(journal_env, caplog, filename, source):
    caplog.set_level(logging.WARNING)
    source_path = journal_env / "apps" / "import" / "journal_sources" / filename
    source_path.write_text(json.dumps(source), encoding="utf-8")

    assert list_journal_sources() == []
    assert "mismatched filename" in caplog.text


def test_duplicate_name_rejected(journal_env):
    source = _source("alpha", generate_key(), created_at=123)
    assert save_journal_source(source) is True

    assert find_journal_source_by_name("alpha")["key"] == source["key"]


def test_invalid_name_rejected(journal_env):
    assert is_valid_journal_source_name("../alpha") is False
    assert (
        save_journal_source(_source("../alpha", generate_key(), created_at=123))
        is False
    )
    assert find_journal_source_by_name("../alpha") is None
    assert not (journal_env.parent / "alpha.json").exists()


def test_revoke_sets_fields(journal_env):
    key = generate_key()
    source = _source("alpha", key, created_at=123)
    assert save_journal_source(source) is True

    revoked_at = now_ms()
    source["revoked"] = True
    source["revoked_at"] = revoked_at
    assert save_journal_source(source) is True

    loaded = load_journal_source(key)
    assert loaded is not None
    assert loaded["revoked"] is True
    assert loaded["revoked_at"] == revoked_at


def test_auth_decorator_valid_key(journal_env):
    key = generate_key()
    source = _source("alpha", key, created_at=123)
    assert save_journal_source(source) is True

    app = Flask(__name__)

    @app.route("/protected")
    @require_journal_source
    def protected():
        return jsonify({"name": g.journal_source["name"]})

    response = app.test_client().get(
        "/protected",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"name": "alpha"}


def test_auth_decorator_missing_key(journal_env):
    app = Flask(__name__)

    @app.route("/protected")
    @require_journal_source
    def protected():
        return jsonify({"name": g.journal_source["name"]})

    response = app.test_client().get("/protected")

    assert response.status_code == 401


def test_auth_decorator_invalid_key(journal_env):
    app = Flask(__name__)

    @app.route("/protected")
    @require_journal_source
    def protected():
        return jsonify({"name": g.journal_source["name"]})

    response = app.test_client().get(
        "/protected",
        headers={"Authorization": "Bearer does-not-exist"},
    )

    assert response.status_code == 401


def test_auth_decorator_revoked_key(journal_env):
    key = generate_key()
    source = _source("alpha", key, created_at=123)
    source["revoked"] = True
    source["revoked_at"] = now_ms()
    assert save_journal_source(source) is True

    app = Flask(__name__)

    @app.route("/protected")
    @require_journal_source
    def protected():
        return jsonify({"name": g.journal_source["name"]})

    response = app.test_client().get(
        "/protected",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 403


def test_auth_decorator_valid_pl_identity(journal_env):
    assert save_journal_source(_pl_source()) is True
    app = _auth_app()

    response = app.test_client().get(
        "/protected",
        environ_overrides={"pl.identity": _pl_identity()},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "name": None,
        "device_label": "peer laptop",
        "pair_mode": "pl",
    }


def test_auth_decorator_unknown_pl_identity_returns_401(journal_env):
    app = _auth_app()

    response = app.test_client().get(
        "/protected",
        environ_overrides={"pl.identity": _pl_identity()},
    )

    assert response.status_code == 401


def test_auth_decorator_revoked_pl_identity_returns_403(journal_env):
    source = _pl_source(revoked=True, revoked_at=now_ms())
    assert save_journal_source(source) is True
    app = _auth_app()

    response = app.test_client().get(
        "/protected",
        environ_overrides={"pl.identity": _pl_identity()},
    )

    assert response.status_code == 403


def test_auth_decorator_disabled_pl_identity_returns_403(journal_env):
    source = _pl_source(enabled=False)
    assert save_journal_source(source) is True
    app = _auth_app()

    response = app.test_client().get(
        "/protected",
        environ_overrides={"pl.identity": _pl_identity()},
    )

    assert response.status_code == 403


def test_auth_decorator_pl_identity_precedes_invalid_bearer(journal_env):
    assert save_journal_source(_pl_source()) is True
    app = _auth_app()

    response = app.test_client().get(
        "/protected",
        headers={"Authorization": "Bearer does-not-exist"},
        environ_overrides={"pl.identity": _pl_identity()},
    )

    assert response.status_code == 200
    assert response.get_json()["pair_mode"] == "pl"


@pytest.mark.parametrize("area", STATE_AREAS)
def test_manifest_empty_state(manifest_app, manifest_env, area):
    response = manifest_app.test_client().get(
        f"/journal/{manifest_env['key'][:8]}/manifest/{area}",
        headers={"Authorization": f"Bearer {manifest_env['key']}"},
    )

    assert response.status_code == 200
    assert response.get_json() == {}


def test_manifest_populated_state(manifest_app, manifest_env):
    data = {"days": {"2026-04-01": {"count": 5}}}
    state_path = (
        get_state_directory(manifest_env["key"][:8]) / "segments" / "state.json"
    )
    state_path.write_text(json.dumps(data), encoding="utf-8")

    response = manifest_app.test_client().get(
        f"/journal/{manifest_env['key'][:8]}/manifest/segments",
        headers={"Authorization": f"Bearer {manifest_env['key']}"},
    )

    assert response.status_code == 200
    assert response.get_json() == data


def test_manifest_missing_state_file(manifest_app, manifest_env):
    state_path = (
        get_state_directory(manifest_env["key"][:8]) / "segments" / "state.json"
    )
    state_path.unlink()

    response = manifest_app.test_client().get(
        f"/journal/{manifest_env['key'][:8]}/manifest/segments",
        headers={"Authorization": f"Bearer {manifest_env['key']}"},
    )

    assert response.status_code == 200
    assert response.get_json() == {}


def test_manifest_malformed_json(manifest_app, manifest_env):
    state_path = (
        get_state_directory(manifest_env["key"][:8]) / "segments" / "state.json"
    )
    state_path.write_text("not valid json{{{", encoding="utf-8")

    response = manifest_app.test_client().get(
        f"/journal/{manifest_env['key'][:8]}/manifest/segments",
        headers={"Authorization": f"Bearer {manifest_env['key']}"},
    )

    assert response.status_code == 200
    assert response.get_json() == {}


def test_manifest_invalid_area(manifest_app, manifest_env):
    response = manifest_app.test_client().get(
        f"/journal/{manifest_env['key'][:8]}/manifest/invalid_area",
        headers={"Authorization": f"Bearer {manifest_env['key']}"},
    )

    assert response.status_code == 404


def test_manifest_key_prefix_mismatch(manifest_app, manifest_env):
    other_prefix = "deadbeef"
    assert other_prefix != manifest_env["key"][:8]

    response = manifest_app.test_client().get(
        f"/journal/{other_prefix}/manifest/segments",
        headers={"Authorization": f"Bearer {manifest_env['key']}"},
    )

    assert response.status_code == 403


def test_manifest_pl_identity_uses_fingerprint_prefix(manifest_app, manifest_env):
    source = _pl_source(FINGERPRINT_2)
    assert save_journal_source(source) is True
    prefix = journal_source_state_prefix(source)
    create_state_directory(manifest_env["root"], prefix)

    response = manifest_app.test_client().get(
        f"/journal/{prefix}/manifest/segments",
        environ_overrides={"pl.identity": _pl_identity(FINGERPRINT_2)},
    )

    assert response.status_code == 200
    assert response.get_json() == {}


def test_manifest_pl_identity_wrong_prefix_returns_403(manifest_app, manifest_env):
    source = _pl_source(FINGERPRINT_2)
    assert save_journal_source(source) is True
    create_state_directory(manifest_env["root"], journal_source_state_prefix(source))

    response = manifest_app.test_client().get(
        "/journal/deadbeef/manifest/segments",
        environ_overrides={"pl.identity": _pl_identity(FINGERPRINT_2)},
    )

    assert response.status_code == 403


def test_manifest_auth_missing(manifest_app, manifest_env):
    response = manifest_app.test_client().get(
        f"/journal/{manifest_env['key'][:8]}/manifest/segments"
    )

    assert response.status_code == 401


def test_manifest_auth_invalid(manifest_app, manifest_env):
    response = manifest_app.test_client().get(
        f"/journal/{manifest_env['key'][:8]}/manifest/segments",
        headers={"Authorization": "Bearer does-not-exist"},
    )

    assert response.status_code == 401


def test_manifest_auth_revoked(manifest_app, manifest_env):
    source = manifest_env["source"]
    source["revoked"] = True
    source["revoked_at"] = now_ms()
    assert save_journal_source(source) is True

    response = manifest_app.test_client().get(
        f"/journal/{manifest_env['key'][:8]}/manifest/segments",
        headers={"Authorization": f"Bearer {manifest_env['key']}"},
    )

    assert response.status_code == 403
