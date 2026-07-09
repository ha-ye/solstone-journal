# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

INIT_HTML = Path(__file__).resolve().parents[1] / "templates" / "init.html"
BRAND_CANON_RE = re.compile(
    r"\b("
    r"sign\s+in|signed\s+in|signing\s+in|log\s+in|logged\s+in|"
    r"account|account_id|account\s+settings|linked|authenticate|"
    r"log\s+into|sign\s+into|your\s+services|journal\s+services|"
    r"capture|watch|record|monitor|track|collect"
    r")\b",
    re.IGNORECASE,
)


def _render_init(convey_env_setup_pending) -> str:
    env = convey_env_setup_pending()
    response = env.client.get("/init")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _read_config(journal: Path) -> dict[str, Any]:
    return json.loads((journal / "config" / "journal.json").read_text())


def _write_config(journal: Path, config: dict[str, Any]) -> None:
    (journal / "config" / "journal.json").write_text(json.dumps(config, indent=2))


def _commit_journal_identity() -> None:
    from solstone.think.link.ca import load_or_generate_ca
    from solstone.think.link.paths import ca_dir

    load_or_generate_ca(ca_dir())


def _finalize_body() -> dict[str, Any]:
    return {
        "name": "Setup Test",
        "preferred": "",
        "timezone": "UTC",
        "retention_mode": "keep",
        "retention_days": None,
    }


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def test_init_provider_section_is_basics_only(convey_env_setup_pending) -> None:
    html = _render_init(convey_env_setup_pending)

    assert 'id="provider-lanes"' in html
    assert "LANE_GLYPHS = { local: '⌂', confidential: '◎', byo: '⚿' }" in html
    assert "how should sol think?" in html
    assert (
        "init only opens the right next step. when you finish setup, "
        "<b>thinking</b> opens to the lane you picked." in html
    )
    assert "save keys, join scout, or turn on local there." in html
    assert "skip for now" in html
    assert "LOCAL_REQUIREMENTS_URL" in html
    assert "gemini-key" not in html
    assert "gemini-validate" not in html
    assert "provider-key-block" not in html
    assert "password-toggle" not in html
    assert "validate-provider" not in html
    assert "scout-setup" not in html
    assert "data-scout-state" not in html
    assert "enableScout" not in html
    assert "subscribeScoutStream" not in html
    assert "/init/services/scout" not in html
    assert ".portal-unreachable" not in html
    assert "portal-unreachable" not in html


def test_init_scout_stubs_removed(convey_env_setup_pending) -> None:
    html = _render_init(convey_env_setup_pending)
    raw_template = INIT_HTML.read_text(encoding="utf-8")

    assert "retention-prefill-hint" not in html
    assert "L11-stub: retention-prefill-hint" not in html
    assert "L11-stub: signed-in retention pre-fill" not in html
    assert "L11-stub: portal-unreachable" not in html
    assert "{% if false %}" not in raw_template


def test_init_rendered_html_is_brand_canon_clean(convey_env_setup_pending) -> None:
    html = _render_init(convey_env_setup_pending)

    assert BRAND_CANON_RE.search(html) is None


def test_init_state_json_is_brand_canon_clean(convey_env_setup_pending) -> None:
    env = convey_env_setup_pending()
    response = env.client.get("/init/api/state")

    assert response.status_code == 200
    for value in _walk_strings(response.get_json()):
        assert BRAND_CANON_RE.search(value) is None, value


def test_init_local_capability_json_is_brand_canon_clean(
    convey_env_setup_pending, monkeypatch
) -> None:
    env = convey_env_setup_pending()
    report = SimpleNamespace(
        report=SimpleNamespace(
            overall="ok",
            checks=(
                SimpleNamespace(
                    name="platform",
                    severity="ok",
                    detail="Linux (x86_64)",
                ),
                SimpleNamespace(
                    name="gpu",
                    severity="ok",
                    detail="NVIDIA GPU with 6 GB",
                ),
            ),
        )
    )
    monkeypatch.setattr("solstone.think.check.build_check_report", lambda: report)

    response = env.client.get("/init/api/local-capability")

    assert response.status_code == 200
    for value in _walk_strings(response.get_json()):
        assert BRAND_CANON_RE.search(value) is None, value


def test_finalize_preserves_existing_provider_and_scout_config(
    convey_env_setup_pending,
) -> None:
    env = convey_env_setup_pending()
    config = _read_config(env.journal)
    scout_block = {"account_id": "x", "enrolled_at_ms": 1}
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "SCOUT_FIXTURE"
    config.setdefault("services", {})["scout"] = scout_block.copy()
    _write_config(env.journal, config)
    before = _read_config(env.journal)
    _commit_journal_identity()

    response = env.client.post(
        "/init/finalize",
        json=_finalize_body(),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    saved = _read_config(env.journal)
    assert saved["env"] == before["env"]
    assert saved["services"]["scout"] == before["services"]["scout"]
