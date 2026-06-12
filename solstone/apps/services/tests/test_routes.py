# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Route tests for the services app."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import threading
from pathlib import Path

from solstone.apps.services import routes as services_routes
from solstone.apps.services.copy import services_copy_values
from solstone.think.journal_config import write_journal_config


def _read_config(env):
    return json.loads((env.journal / "config" / "journal.json").read_text("utf-8"))


def _write_config(env, payload):
    write_journal_config(payload)


def _approved_scout_config(env):
    key = "google-key"
    config = _read_config(env)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = key
    config.setdefault("services", {})["scout"] = {
        "enabled_at": "2026-05-24T00:00:00Z",
        "account_id": "acct-secret",
        "key_created_at": "2026-05-24T00:00:00Z",
        "dispatch_token": "dispatch-secret",
        "key_fingerprint_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
    }
    _write_config(env, config)


def _pending_scout_config(env):
    config = _read_config(env)
    config.setdefault("services", {})["scout"] = {
        "state": "pending",
        "account_id": "acct-pending",
        "since": 1770000000000,
        "checked_at": "2026-05-24T00:00:00Z",
    }
    _write_config(env, config)


def test_services_app_discovered_and_auto_appended_for_saved_order(services_env):
    env = services_env()
    convey_path = env.journal / "config" / "convey.json"
    convey_path.write_text(
        json.dumps(
            {
                "apps": {
                    "order": [
                        "home",
                        "activities",
                        "entities",
                        "search",
                        "reflections",
                        "news",
                    ],
                    "starred": ["home"],
                }
            }
        ),
        encoding="utf-8",
    )

    response = env.client.get("/app/services/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-app-name="services"' in html
    assert "your services" in html


def test_status_no_secrets_and_whitelisted_scout_provenance(services_env):
    env = services_env()
    _approved_scout_config(env)

    response = env.client.get("/app/services/scout/status")

    assert response.status_code == 200
    data = response.get_json()
    serialized = json.dumps(data).lower()
    assert "acct-secret" not in serialized
    assert "dispatch-secret" not in serialized
    assert "account_id" not in serialized
    assert "dispatch_token" not in serialized
    assert data["provenance"] == {
        "enabled_at": "2026-05-24T00:00:00Z",
        "key_created_at": "2026-05-24T00:00:00Z",
        "key_fingerprint_sha256": hashlib.sha256(b"google-key").hexdigest(),
    }


def test_pending_scout_status_formats_since_label(services_env):
    env = services_env()
    _pending_scout_config(env)

    response = env.client.get("/app/services/scout/status")

    assert response.status_code == 200
    data = response.get_json()
    assert data["state"] == "pending"
    assert data["provenance"]["since"] == 1770000000000
    assert data["provenance"]["since_label"] == "2026-02-02"


def test_spb_spn_status_coming_soon_and_mutations_write_nothing(services_env):
    env = services_env()
    before = (env.journal / "config" / "journal.json").read_bytes()

    for service in ("spb", "spn"):
        status_response = env.client.get(f"/app/services/{service}/status")
        assert status_response.status_code == 200
        assert status_response.get_json()["state"] == "coming_soon"
        for action in ("enable", "refresh", "disable"):
            response = env.client.post(f"/app/services/{service}/{action}")
            assert response.status_code == 403

    assert (env.journal / "config" / "journal.json").read_bytes() == before


def test_unknown_service_returns_unknown_service(services_env):
    env = services_env()

    response = env.client.get("/app/services/nope/status")

    assert response.status_code == 404
    assert response.get_json()["reason_code"] == "unknown_service"


def test_scout_enable_guards_state_conflicts(services_env):
    env = services_env()
    _approved_scout_config(env)

    enabled_response = env.client.post("/app/services/scout/enable")

    assert enabled_response.status_code == 400
    assert enabled_response.get_json()["reason_code"] == "invalid_operation_for_state"

    config = _read_config(env)
    config.pop("services", None)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual-key"
    _write_config(env, config)

    manual_response = env.client.post("/app/services/scout/enable")

    assert manual_response.status_code == 400
    assert manual_response.get_json()["reason_code"] == "invalid_operation_for_state"


def test_scout_disable_sync_updates_status(services_env):
    env = services_env()
    _approved_scout_config(env)

    response = env.client.post("/app/services/scout/disable")

    assert response.status_code == 200
    data = response.get_json()
    assert data["result"]["was_enabled"] is True
    assert data["status"]["state"] == "disabled"


def test_same_service_concurrent_operation_returns_service_busy(
    services_env,
    monkeypatch,
    wait_until_helper,
):
    env = services_env()
    started = threading.Event()
    release = threading.Event()

    def slow_flow(**_kwargs):
        started.set()
        release.wait(2)
        return services_routes.ScoutOpResult(
            phase="enabled",
            guidance=None,
            retryable=False,
            browser_open_succeeded=True,
            portal_url=None,
        )

    monkeypatch.setattr(services_routes, "run_scout_handoff", slow_flow)

    first = env.client.post("/app/services/scout/enable")
    wait_until_helper(started.is_set)
    second = env.client.post("/app/services/scout/enable")
    release.set()

    assert first.status_code == 202
    assert second.status_code == 503
    assert second.get_json()["reason_code"] == "service_busy"


def test_different_services_can_run_concurrently(
    services_env,
    monkeypatch,
    wait_until_helper,
):
    env = services_env()
    scout_started = threading.Event()
    spl_started = threading.Event()
    release = threading.Event()

    def scout_flow(**_kwargs):
        scout_started.set()
        release.wait(2)
        return services_routes.ScoutOpResult("pending", None, False, True, None)

    def spl_flow(**_kwargs):
        spl_started.set()
        release.wait(2)
        return services_routes.SplOpResult("revoked", None, False, True, None)

    monkeypatch.setattr(services_routes, "run_scout_handoff", scout_flow)
    monkeypatch.setattr(services_routes, "run_spl_handoff", spl_flow)

    scout_response = env.client.post("/app/services/scout/enable")
    spl_response = env.client.post("/app/services/spl/enable")
    wait_until_helper(scout_started.is_set)
    wait_until_helper(spl_started.is_set)
    release.set()

    assert scout_response.status_code == 202
    assert spl_response.status_code == 202


def test_scout_refresh_failure_without_state_write(
    services_env,
    monkeypatch,
    wait_until_helper,
):
    env = services_env()
    _pending_scout_config(env)
    before = (env.journal / "config" / "journal.json").read_bytes()

    def failed_flow(**_kwargs):
        return services_routes.ScoutOpResult(
            phase="error",
            guidance="try again",
            retryable=True,
            browser_open_succeeded=True,
            portal_url=None,
        )

    monkeypatch.setattr(services_routes, "run_scout_handoff", failed_flow)

    response = env.client.post("/app/services/scout/refresh")
    assert response.status_code == 202
    wait_until_helper(
        lambda: (
            env.client.get("/app/services/scout/status").get_json()["operation"][
                "phase"
            ]
            == "error"
        )
    )

    assert (env.journal / "config" / "journal.json").read_bytes() == before


def test_services_routes_do_not_import_app_link_modules():
    violations: list[str] = []
    for path in Path("solstone/apps/services").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("solstone.apps.link"):
                        violations.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("solstone.apps.link"):
                    violations.append(f"{path}: from {node.module} import ...")

    assert violations == []


def test_forbidden_terms_absent_from_services_surfaces(services_env):
    env = services_env()
    html = env.client.get("/app/services/").get_data(as_text=True)
    match = re.search(
        r'(<section class="services-shell".*?</section>\s*<script>.*?window\.SERVICES_INITIAL = SERVICES_INITIAL;\s*</script>)',
        html,
        re.DOTALL,
    )
    assert match, "services render surface not found"
    services_html = match.group(1)
    js = Path("solstone/apps/services/static/services.js").read_text(encoding="utf-8")
    routes_source = Path("solstone/apps/services/routes.py").read_text(encoding="utf-8")
    unknown = env.client.get("/app/services/nope/status").get_json()
    unsupported = env.client.post("/app/services/spb/enable").get_json()
    payloads = [
        env.client.get("/app/services/scout/status").get_json(),
        env.client.get("/app/services/spl/status").get_json(),
        env.client.get("/app/services/spb/status").get_json(),
        unknown,
        unsupported,
    ]
    haystack = "\n".join(
        [
            services_html,
            js,
            routes_source,
            "\n".join(services_copy_values()),
            json.dumps(payloads, sort_keys=True),
        ]
    ).lower()
    forbidden = [
        "activate",
        "subscribe",
        "sign up for",
        "upgrade",
        "log in",
        "sign in",
        "account",
        "account_id",
        "capture",
        "watch",
        "record",
        "monitor",
        "track",
        "collect",
    ]

    hits = [
        term for term in forbidden if re.search(rf"\b{re.escape(term)}\b", haystack)
    ]

    assert hits == []
