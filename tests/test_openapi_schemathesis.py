# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis import GenerationMode

import solstone.think.utils as think_utils
from solstone.convey import create_app
from solstone.think.convey_client import resolve_base_url
from solstone.think.utils import get_journal
from tests._baseline_harness import (
    isolated_app_env,
    mark_setup_complete,
    prepare_isolated_journal,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_CONTRACT = REPO_ROOT / "docs" / "openapi" / "convey-clients.json"

LIVE_ENV = "SOLSTONE_SCHEMATHESIS_LIVE"
LIVE_BASE_URL_ENV = "SOLSTONE_SCHEMATHESIS_BASE_URL"
LIVE_JOURNAL_ENV = "SOLSTONE_SCHEMATHESIS_JOURNAL"

ALLOWLIST_OPERATION_IDS = [
    "home.pulse",
    "link.status",
    "observer.ingestManifest",
    "voice.status",
    "link.localEndpoints",
]

# Floor operations must always prove a useful 2xx response under this fixture.
# The committed contract has 31 of 32 operations documenting 401 and/or 403;
# home.pulse is the sole 200-only exception. A blanket auth rejection can be
# status-conformant for the other 31 operations, so these floor assertions prove
# the lane is exercising successful reads instead of merely conformant denials.
FLOOR_OPERATION_IDS = frozenset(
    {
        "home.pulse",
        "link.status",
        "observer.ingestManifest",
    }
)

OBSERVER_AUTH_OPERATION_IDS = frozenset({"observer.ingestManifest"})

# Purpose-mutating POST/DELETE operations stay outside this in-process WSGI lane
# by policy. The map below records non-obvious GET exclusions at the exclusion
# point so the lane cannot quietly drift into known blocking or destructive paths.
EXCLUDED_GET_OPERATION_REASONS = {
    # /sse/events reads request.environ["pl.disconnect_event"]. The only repo
    # writer is solstone/convey/secure_listener/wsgi.py:328; Flask's WSGI test
    # client does not set it, so disconnected() is permanently False and the
    # heartbeat loop is unbounded.
    "callosum.rootEvents": "unbounded SSE stream under Flask WSGI test client",
    # /app/observer/callosum never consults pl.disconnect_event. Its while True
    # exits only on handle.dropped or the observer going missing, revoked, or
    # disabled, so injecting the valid observer handle is exactly what hangs it.
    "observer.callosumStream": "unbounded observer SSE stream with valid handle",
    "chat.session": "may recover chat, spawn agents/timers, and touch callosum",
    "observer.ingestManifestDay": "requires day fixture state not worth this lane",
    "observer.ingestSegments": "requires day fixture state not worth this lane",
}

# Durable findings from the lane design. These are reported as bugs, not fixed
# here, because this module verifies the committed contract against behavior.
INCIDENTAL_READ_FINDINGS = {
    "link.status": (
        "GET /app/network/api/status calls ca_dir().exists(), but ca_dir() creates "
        "journal/link/ca first; _ca_fingerprint() then load_or_generate_ca() writes "
        "private.pem and cert.pem on a read path."
    ),
    "home.pulse": (
        "GET /app/home/api/pulse can write awareness/current.json through the "
        "thinking_readiness cache; this is weaker than link.status because it is "
        "routed through the awareness owner and is not an L3 read-verb violation."
    ),
    "link.status.socket": (
        "GET /app/network/api/status reaches _detect_lan_ip(), which opens a UDP "
        "socket to 8.8.8.8:80 to read the kernel-selected source address."
    ),
}

REGISTER_OBSERVER_PAYLOAD = {
    "platform": "linux",
    "hostname": "contract-host",
    "stream_type": "desktop",
    "version": "1",
}


def _load_schema() -> schemathesis.schemas.BaseSchema:
    schema = schemathesis.openapi.from_path(OPENAPI_CONTRACT)
    schema.config.base_url = "http://example.invalid"
    schema.config.generation.max_examples = 3
    schema.config.generation.modes = [GenerationMode.POSITIVE]
    return schema


SCHEMA = _load_schema()


@dataclass(frozen=True)
class ContractTarget:
    app: Any | None
    base_url: str | None
    observer_key: str


def _resolved_operation_ids(schema: schemathesis.schemas.BaseSchema) -> set[str]:
    operation_ids: set[str] = set()
    for result in schema.get_all_operations():
        operation = result.ok()
        operation_ids.add(str(operation.definition.raw["operationId"]))
    return operation_ids


def _register_observer_wsgi(client: Any) -> str:
    response = client.post("/app/observer/register", json=REGISTER_OBSERVER_PAYLOAD)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert isinstance(body, dict)
    return str(body["key"])


def _live_enabled() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


def _live_failure(base_url: str, port_path: Path, detail: str) -> None:
    pytest.fail(
        "Schemathesis live target unavailable: "
        f"base_url={base_url!r}; "
        f"port_path={port_path}; "
        f"override_env={LIVE_BASE_URL_ENV}; "
        f"{detail}",
    )


def _resolve_live_base_url(monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    live_journal = os.environ.get(LIVE_JOURNAL_ENV)
    if live_journal:
        monkeypatch.setenv("SOLSTONE_JOURNAL", live_journal)
        think_utils._journal_path_cache = None

    base_url = os.environ.get(LIVE_BASE_URL_ENV) or resolve_base_url()
    port_path = Path(get_journal()) / "health" / "convey.port"
    return base_url, port_path


def _register_observer_live(base_url: str, port_path: Path) -> str:
    url = f"{base_url.rstrip('/')}/app/observer/register"
    try:
        response = requests.post(url, json=REGISTER_OBSERVER_PAYLOAD, timeout=5)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        _live_failure(base_url, port_path, f"observer registration failed: {exc}")
    except ValueError as exc:
        _live_failure(
            base_url, port_path, f"observer registration returned JSON error: {exc}"
        )

    if not isinstance(body, dict) or "key" not in body:
        _live_failure(base_url, port_path, "observer registration did not return key")
    return str(body["key"])


@pytest.fixture
def contract_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if _live_enabled():
        base_url, port_path = _resolve_live_base_url(monkeypatch)
        observer_key = _register_observer_live(base_url, port_path)
        yield ContractTarget(app=None, base_url=base_url, observer_key=observer_key)
        return

    journal = prepare_isolated_journal(tmp_path / "journal")
    mark_setup_complete(journal)
    with isolated_app_env(journal):
        app = create_app(journal=str(journal.resolve()))
        app.config["TESTING"] = True
        observer_key = _register_observer_wsgi(app.test_client())
        yield ContractTarget(app=app, base_url=None, observer_key=observer_key)


def test_schemathesis_allowlist_resolves_committed_schema() -> None:
    selected = _resolved_operation_ids(
        SCHEMA.include(operation_id=ALLOWLIST_OPERATION_IDS)
    )
    assert selected == set(ALLOWLIST_OPERATION_IDS)


def _operation_id(case: Any) -> str:
    return str(case.operation.definition.raw["operationId"])


def _headers_for_case(case: Any, target: ContractTarget) -> dict[str, str]:
    if _operation_id(case) in OBSERVER_AUTH_OPERATION_IDS:
        return {"X-Solstone-Observer": target.observer_key}
    return {}


def _call_case(case: Any, target: ContractTarget, headers: dict[str, str]):
    if target.app is not None:
        return case.call(app=target.app, headers=headers)
    assert target.base_url is not None
    return case.call(base_url=target.base_url, headers=headers)


@pytest.mark.timeout(10)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@SCHEMA.include(operation_id=ALLOWLIST_OPERATION_IDS).parametrize()
def test_committed_openapi_contract_with_schemathesis(
    case: Any,
    contract_target: ContractTarget,
) -> None:
    operation_id = _operation_id(case)
    response = _call_case(
        case, contract_target, _headers_for_case(case, contract_target)
    )
    if operation_id in FLOOR_OPERATION_IDS:
        assert 200 <= response.status_code < 300, response.text
    case.validate_response(response)
