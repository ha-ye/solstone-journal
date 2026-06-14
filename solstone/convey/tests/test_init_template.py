# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from pathlib import Path
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


def _finalize_body(gemini_key: str) -> dict[str, Any]:
    return {
        "name": "Setup Test",
        "preferred": "",
        "timezone": "UTC",
        "gemini_key": gemini_key,
        "retention_mode": "keep",
        "retention_days": None,
    }


def test_init_provider_section_is_basics_only(convey_env_setup_pending) -> None:
    html = _render_init(convey_env_setup_pending)

    assert 'id="gemini-key"' in html
    assert 'id="gemini-validate"' in html
    assert "how should sol think?" in html
    assert "become a solstone scout" in html
    assert "use your own key" in html
    assert "use local thinking models" in html
    assert (
        "be an early tester for solstone — we'll cover your thinking, using Gemini."
        in html
    )
    assert "init captures your choice" in html
    assert "skip for now" in html
    assert "INSTALL.md#choosing-how-to-power-sol" in html
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


def test_finalize_empty_gemini_key_preserves_existing_scout_config(
    convey_env_setup_pending,
) -> None:
    env = convey_env_setup_pending()
    config = _read_config(env.journal)
    scout_block = {"account_id": "x", "enrolled_at_ms": 1}
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "SCOUT_FIXTURE"
    config.setdefault("services", {})["scout"] = scout_block.copy()
    _write_config(env.journal, config)

    response = env.client.post(
        "/init/finalize",
        json=_finalize_body(""),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    saved = _read_config(env.journal)
    assert saved["env"]["GOOGLE_API_KEY"] == "SCOUT_FIXTURE"
    assert saved["services"]["scout"] == scout_block


def test_finalize_manual_paste_writes_gemini_key(convey_env_setup_pending) -> None:
    env = convey_env_setup_pending()

    response = env.client.post(
        "/init/finalize",
        json=_finalize_body("MANUAL_FIXTURE"),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    saved = _read_config(env.journal)
    assert saved["env"]["GOOGLE_API_KEY"] == "MANUAL_FIXTURE"
