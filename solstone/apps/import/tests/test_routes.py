# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

IMPORT_APP_ROOT = Path(__file__).resolve().parents[1]
IMPORT_DETAIL_JS = IMPORT_APP_ROOT / "static" / "import_detail.js"
REPO_ROOT = Path(__file__).resolve().parents[4]
DRAWER_JS = REPO_ROOT / "solstone" / "convey" / "static" / "drawer.js"


def _unescape_js_string(value: str) -> str:
    return value.replace("\\'", "'").replace('\\"', '"')


def _pdf_literal(value: str) -> bytes:
    out = bytearray()
    for char in value:
        if char == "\\":
            out += b"\\\\"
        elif char == "(":
            out += b"\\("
        elif char == ")":
            out += b"\\)"
        else:
            out += char.encode("ascii")
    return b"(" + bytes(out) + b")"


def _node_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    return node


def _write_text_pdf(path: Path, text: str) -> Path:
    content = b"BT /F1 12 Tf 72 720 Td " + _pdf_literal(text) + b" Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [ 4 0 R ] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1700000000000}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    from solstone.convey import create_app

    app = create_app(journal=str(journal))
    app.config.update(TESTING=True)
    return app.test_client()


def _seed_imported_json(journal: Path, timestamp: str, payload: dict) -> None:
    import_dir = journal / "imports" / timestamp
    import_dir.mkdir(parents=True, exist_ok=True)
    (import_dir / "imported.json").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )


def test_import_index_serves_injected_spa_shell(client):
    response = client.get("/app/import/")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_import_detail_serves_spa_shell_even_when_missing(client):
    response = client.get("/app/import/missing-import")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_import_missing_detail_api_still_returns_not_found(client):
    response = client.get("/app/import/api/missing-import")

    assert response.status_code == 404
    assert response.get_json()["reason_code"] == "import_not_found"


def _seed_import_json(journal: Path, timestamp: str, payload: dict) -> None:
    import_dir = journal / "imports" / timestamp
    import_dir.mkdir(parents=True, exist_ok=True)
    (import_dir / "import.json").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )


def test_import_detail_api_reports_running_for_an_unfinished_import(client):
    import importlib

    import_routes = importlib.import_module("solstone.apps.import.routes")
    journal = Path(import_routes.state.journal_root)
    _seed_import_json(
        journal,
        "20260101_120000",
        {"original_filename": "in-progress.pdf", "task_id": "123"},
    )

    response = client.get("/app/import/api/20260101_120000")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "running"
    assert body["error"] is None
    assert body["error_stage"] is None


def test_import_detail_api_reports_failed_with_error_and_stage(client):
    import importlib

    import_routes = importlib.import_module("solstone.apps.import.routes")
    journal = Path(import_routes.state.journal_root)
    _seed_import_json(
        journal,
        "20260101_130000",
        {"original_filename": "broken.pdf", "task_id": "456"},
    )
    _seed_imported_json(
        journal,
        "20260101_130000",
        {"error": "bad <archive>", "error_stage": "writing"},
    )

    response = client.get("/app/import/api/20260101_130000")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "failed"
    assert body["error"] == "bad <archive>"
    assert body["error_stage"] == "writing"


def test_import_detail_api_path_resolves(client):
    adapter = client.application.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/import/api/missing-import", method="GET")

    assert endpoint == "app:import.import_detail_api"


def test_import_detail_static_module_serves(client):
    response = client.get("/app/import/static/import_detail.js")

    assert response.status_code == 200
    assert b"window.ImportDetail" in response.data
    assert b"renderDetail" in response.data


def test_import_sources_emit_icon_svg_not_emoji(client):
    response = client.get("/app/import/api/sources")

    assert response.status_code == 200
    payload = response.get_json()
    sources = payload["items"]
    assert payload["total"] == len(sources)
    assert sources
    by_name = {source["name"]: source for source in sources}
    assert "emoji" not in by_name["quick"]
    assert by_name["quick"]["icon"] == "zap"
    assert "<svg" in by_name["quick"]["icon_svg"]


def test_import_sources_omit_granola_and_keep_guide_counts(client):
    response = client.get("/app/import/api/sources")

    assert response.status_code == 200
    sources = response.get_json()["items"]
    names = {source["name"] for source in sources}
    assert len(sources) == 11
    assert "granola" not in names
    assert sum(source["has_guide"] is True for source in sources) == 7
    assert [
        source["name"] for source in sources if source["input_type"] == "path_input"
    ] == ["obsidian"]


def test_import_list_source_display_matches_current_metadata(client):
    import importlib

    import_routes = importlib.import_module("solstone.apps.import.routes")
    journal = Path(import_routes.state.journal_root)
    _seed_imported_json(
        journal,
        "20260101_100000",
        {"source_type": "claude", "source_display": "Claude Chat"},
    )
    _seed_imported_json(
        journal,
        "20260101_110000",
        {"source_type": "ics", "source_display": "Calendar"},
    )
    expected = {
        source["name"]: source["display_name"]
        for source in import_routes.SOURCE_METADATA
    }

    response = client.get("/app/import/api/list")

    assert response.status_code == 200
    checked = 0
    for row in response.get_json()["imports"]:
        source_type = row.get("source_type")
        if source_type in expected:
            checked += 1
            assert row["source_display"] == expected[source_type]
    assert checked >= 2


def test_import_list_preserves_display_for_unknown_source_types(client):
    import importlib

    import_routes = importlib.import_module("solstone.apps.import.routes")
    journal = Path(import_routes.state.journal_root)
    _seed_imported_json(
        journal,
        "20260101_100000",
        {"source_type": "claude", "source_display": "Claude Chat"},
    )
    _seed_imported_json(
        journal,
        "20260101_110000",
        {
            "source_type": "not_a_real_source",
            "source_display": "Some Recorded Name",
        },
    )
    _seed_imported_json(
        journal,
        "20260101_120000",
        {"source_display": "Recorded Only"},
    )

    response = client.get("/app/import/api/list")

    assert response.status_code == 200
    by_timestamp = {row["timestamp"]: row for row in response.get_json()["imports"]}
    assert by_timestamp["20260101_100000"]["source_display"] == "Claude"
    assert by_timestamp["20260101_110000"]["source_display"] == "Some Recorded Name"
    assert by_timestamp["20260101_120000"]["source_type"] is None
    assert by_timestamp["20260101_120000"]["source_display"] == "Recorded Only"


def test_import_content_list_preserves_display_for_retired_source(client):
    import importlib

    import_routes = importlib.import_module("solstone.apps.import.routes")
    journal = Path(import_routes.state.journal_root)
    import_dir = journal / "imports" / "20260101_130000"
    import_dir.mkdir(parents=True)
    (import_dir / "imported.json").write_text(
        json.dumps({"source_type": "granola", "source_display": "Granola"}) + "\n",
        encoding="utf-8",
    )
    (import_dir / "content_manifest.jsonl").write_text(
        json.dumps(
            {
                "id": "meeting-1",
                "date": "20260101",
                "title": "Meeting",
                "preview": "Retired import source",
                "segments": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    response = client.get("/app/import/api/20260101_130000/content")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["source_type"] == "granola"
    assert payload["source_display"] == "Granola"
    assert payload["source_icon_svg"] is None


def test_import_workspace_detail_static_wiring_and_deleted_tabs():
    workspace = (IMPORT_APP_ROOT / "workspace.html").read_text(encoding="utf-8")

    assert '<script src="/app/import/static/import_detail.js"></script>' in workspace
    assert 'data-target="overview"' in workspace
    assert 'data-target="content"' in workspace
    for deleted in (
        'data-target="import-json"',
        'data-target="imported-json"',
        'id="import-json"',
        'id="imported-json"',
        "importJsonContent",
        "importedJsonContent",
        "formatJson",
        ".json-viewer",
        ".json-key",
        ".json-string",
        ".json-number",
        ".json-boolean",
        ".json-null",
        ".info-grid",
        ".merge-summary-card",
        ".links-section",
        ".files-list",
    ):
        assert deleted not in workspace
    assert ".status-badge" in workspace
    assert ".no-data" in workspace
    assert ".drawer-raw" in workspace
    assert ".import-collision-callout" in workspace


def test_import_guided_flow_removes_path_autodetect_but_keeps_path_input_wiring():
    workspace = (IMPORT_APP_ROOT / "workspace.html").read_text(encoding="utf-8")

    for deleted in ("checkDefaultPath", "guidedPathStatus"):
        assert workspace.count(deleted) == 0
    for retained in (
        "guidedStartBtn",
        "startGuidedImport",
        "guidedPathInput.addEventListener('input'",
        'id="guidedPathInput"',
    ):
        assert retained in workspace


def test_import_check_path_route_removed_no_alias(client):
    rules = list(client.application.url_map.iter_rules())

    assert not any(rule.rule == "/app/import/api/check-path/<source>" for rule in rules)
    assert any(rule.rule == "/app/import/api/sources" for rule in rules)


def test_source_metadata_descriptions_are_pinned_byte_for_byte():
    import importlib

    import_routes = importlib.import_module("solstone.apps.import.routes")
    source_metadata = {
        source["name"]: source for source in import_routes.SOURCE_METADATA
    }

    assert (
        source_metadata["journal_archive"]["description"]
        == "import a full journal export from another journal"
    )
    assert (
        source_metadata["recording"]["description"]
        == "import audio from meetings or conversations"
    )


def test_owner_collision_copy_preserves_placeholders_and_entity():
    source_text = IMPORT_DETAIL_JS.read_text(encoding="utf-8")
    target = "${escapeHtml(principalCollision.target_name || '')}"
    source = "${escapeHtml(principalCollision.source_name || '')}"

    assert "importedJson?.principal_collision" in source_text
    assert target in source_text
    assert source in source_text
    assert "journal&#39;s" in source_text
    assert 'href="/app/settings#profile"' in source_text
    assert 'href="/app/settings#identity"' not in source_text


def test_import_detail_owner_facing_strings_are_pinned():
    source = IMPORT_DETAIL_JS.read_text(encoding="utf-8")
    start = source.index("// --- owner-facing strings ---")
    end = source.index("// --- end owner-facing strings ---")
    region = source[start:end]
    values = re.findall(r"'([^']*)'", region)
    assert "object.freeze" in region.lower()
    assert values
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "preserveOpen" not in source
    assert "<b>" not in source
    for singular in ("entry", "entity", "file"):
        assert f"{singular}: '{singular}'" in region
        assert f"'{singular}', strings." not in source
        assert f"'{singular}'" not in source[end:]
    for key in (
        "collision_body_before_target",
        "collision_body_between_names",
        "collision_body_after_source",
        "collision_body_journal_entity",
        "collision_body_after_entity",
        "file_size_units",
        "drawer_unavailable",
    ):
        assert key in region
    for leak in (
        "this journal belongs to",
        "drawer renderer unavailable",
        "const units = ['b'",
        "return '0 b'",
    ):
        assert leak not in source[end:]


def test_import_detail_module_runs_with_real_drawer_under_node():
    node = _node_or_skip()
    clean = {
        "timestamp": "20260101_090000",
        "status": "success",
        "error": None,
        "error_stage": None,
        "import_json": {
            "original_filename": "calendar-export.zip",
            "upload_datetime": "2026-01-01T09:00:00",
            "user_timestamp": "20260101_090000",
            "file_size": 45678,
            "mime_type": "application/zip",
            "facet": "work",
            "setting": "calendar",
        },
        "imported_json": {
            "processing_completed": "2026-01-01T09:10:00",
            "total_files_created": 2,
            "all_created_files": [
                "20260101/import.ics/090000_300/event_transcript.md",
                "20260101/import.ics/093000_300/event_transcript.md",
            ],
            "source_type": "ics",
            "source_display": "calendar",
            "entries_written": 5,
            "entities_seeded": 0,
            "date_range": ["20260101", "20260101"],
            "target_day": "20260101",
        },
    }
    collision = {
        **clean,
        "imported_json": {
            **clean["imported_json"],
            "processing_completed": "2026-01-01T09:00:30",
            "entries_written": 1,
            "total_files_created": 0,
            "principal_collision": {
                "source_name": "source <person>",
                "target_name": "target <person>",
            },
            "merge_summary": {
                "segments_copied": 1,
                "segments_skipped": 0,
                "segments_errored": 0,
                "entities_created": 0,
                "entities_merged": 0,
                "entities_staged": 1,
                "facets_created": 0,
                "facets_merged": 0,
                "imports_copied": 0,
                "imports_skipped": 0,
            },
        },
        "decision_highlights": {
            "staged_entities": [
                {
                    "source_name": "source <person>",
                    "target_name": "target <person>",
                    "staging_path": "/tmp/staging/<entity>/entity.json",
                }
            ],
            "errored_segments": [
                {
                    "item_id": "20260101/default/090000_300",
                    "reason": "bad <segment>",
                }
            ],
        },
        "summary_errors": ["summary <error>"],
        "merge_artifact_paths": {
            "decisions": "/tmp/decisions.jsonl",
            "staging": "/tmp/staging",
        },
    }
    error_payload = {
        **clean,
        "status": "failed",
        "error": "bad <archive>",
        "error_stage": "writing",
        "imported_json": {
            "processing_started": "2026-01-01T09:00:00",
            "processing_failed": "2026-01-01T09:02:00",
            "source_type": "generic",
            "source_display": "calendar-export.zip",
            "entries_written": 0,
            "entities_seeded": 0,
            "date_range": None,
            "error": "bad <archive>",
            "error_stage": "writing",
            "total_files_created": 0,
            "all_created_files": [],
        },
    }
    pending = {
        "timestamp": "20260101_090000",
        "status": "pending",
        "error": None,
        "error_stage": None,
        "import_json": {
            "original_filename": "pending.txt",
            "upload_datetime": "2026-01-01T09:00:00",
        },
        "imported_json": None,
    }
    running_without_result = {
        "timestamp": "20260101_090000",
        "status": "running",
        "error": None,
        "error_stage": None,
        "import_json": {
            "original_filename": "in-progress.txt",
            "upload_datetime": "2026-01-01T09:00:00",
            "task_id": "123",
        },
        "imported_json": None,
    }
    partial = {
        **clean,
        "imported_json": {
            "source_type": "chatgpt",
            "total_files_created": 2,
        },
    }

    script = "\n".join(
        [
            "global.window = global;",
            DRAWER_JS.read_text(encoding="utf-8"),
            IMPORT_DETAIL_JS.read_text(encoding="utf-8"),
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            f"const clean = {json.dumps(clean)};",
            f"const collision = {json.dumps(collision)};",
            f"const errorPayload = {json.dumps(error_payload)};",
            f"const pending = {json.dumps(pending)};",
            f"const runningWithoutResult = {json.dumps(running_without_result)};",
            f"const partial = {json.dumps(partial)};",
            """
function drawerLineHtml(line) {
  const rendered = window.Drawer.render({ id: "probe", label: "probe", line, bodyHtml: "" });
  const match = rendered.match(/<span class="drawer-line">([\\s\\S]*?)<\\/span>/);
  return match ? match[1] : "";
}

function htmlUnescape(value) {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

assert(Object.keys(window.ImportDetail).join(",") === "renderDetail,renderMeta,deriveStatus,formatDuration,composeDrawerLine,resolveDay,createdFileHref,hasValue,kvRow", "public export surface is pinned");
assert(window.ImportDetail.composeDrawerLine(clean) === "2 files created · 5 entries · completed in 10 minutes", "clean line follows grammar");
assert(window.ImportDetail.composeDrawerLine(collision) === "0 files created · 1 entry · completed in under a minute · owner identity differs", "collision line follows grammar");
assert(window.ImportDetail.composeDrawerLine(errorPayload) === "failed while processing", "error line hides counts and duration");
assert(window.ImportDetail.composeDrawerLine(runningWithoutResult) === "processing…", "task id without result yet derives processing line");
assert(window.ImportDetail.composeDrawerLine(pending) === "processing…", "pending line is processing ellipsis");
assert(window.ImportDetail.composeDrawerLine(partial) === "2 files created", "partial line omits missing clauses");

const cleanLine = drawerLineHtml(window.ImportDetail.composeDrawerLine(clean));
assert(cleanLine.includes("<b>2</b> files created"), "drawer emphasizes file count");
assert(cleanLine.includes("<b>5</b> entries"), "drawer emphasizes entry count");
assert(cleanLine.includes("<b>10</b> minutes"), "drawer emphasizes minute count");
const underMinuteLine = drawerLineHtml(window.ImportDetail.composeDrawerLine(collision));
assert(underMinuteLine.includes("under a minute"), "under-minute duration is prose");
assert(!underMinuteLine.includes("second"), "drawer line never emits second");
assert(!underMinuteLine.includes("<b>1 s</b>econd"), "duration avoids drawer regex second split");

assert(window.ImportDetail.formatDuration("2026-01-01T00:00:00", "2026-01-01T00:01:00") === "1 minute", "one minute singular");
assert(window.ImportDetail.formatDuration("2026-01-01T00:00:00", "2026-01-01T01:05:00") === "65 minutes", "minutes ladder");
assert(window.ImportDetail.formatDuration("2026-01-01T00:00:00", "2026-01-01T02:00:00") === "2 hours", "hours ladder");
assert(window.ImportDetail.formatDuration("2026-01-01T00:00:00", "2026-01-03T00:00:00") === "2 days", "days ladder");
assert(drawerLineHtml("completed in 65 minutes").includes("<b>65</b> minutes"), "minutes are drawer-regex safe");
assert(drawerLineHtml("completed in 2 hours").includes("<b>2</b> hours"), "hours are drawer-regex safe");
assert(drawerLineHtml("completed in 2 days").includes("<b>2</b> days"), "days are drawer-regex safe");

assert(window.ImportDetail.hasValue(0) === true, "zero survives empty rule");
assert(window.ImportDetail.hasValue("") === false, "empty string is omitted");
assert(window.ImportDetail.hasValue(null) === false, "null is omitted");
assert(window.ImportDetail.kvRow("entries", 0).includes(">0<"), "kv row renders numeric zero");
assert(window.ImportDetail.kvRow("entries", "").length === 0, "kv row omits empty value");

assert(window.ImportDetail.deriveStatus(clean).status === "completed", "completed status");
assert(window.ImportDetail.deriveStatus(errorPayload).chipTone === "danger", "error danger chip");
assert(window.ImportDetail.deriveStatus(collision).chipTone === "warn", "collision warn chip");
assert(window.ImportDetail.deriveStatus({...collision, status: "failed", error: "bad", imported_json: {...collision.imported_json, error: "bad"}}).chipTone === "danger", "error wins over collision");
assert(window.ImportDetail.deriveStatus(pending).status === "pending", "pending status");
assert(window.ImportDetail.deriveStatus(runningWithoutResult).status === "running", "task id without a result yet is running, not failed");

assert(window.ImportDetail.resolveDay(clean) === "20260101", "resolveDay follows fixture day");
assert(window.ImportDetail.resolveDay({imported_json: {date_range: ["20260101", "20260101"], target_day: "20260102"}}) === "20260102", "target_day wins over date_range");
assert(window.ImportDetail.resolveDay({imported_json: {date_range: ["20260101", "20260103"]}}) === "20260101", "date_range first day is fallback");
assert(window.ImportDetail.createdFileHref("20260101/import.ics/090000_300/event_transcript.md") === null, "actual fixture path has no chronicle anchor");
assert(window.ImportDetail.createdFileHref("chronicle/20260101/import.ics/090000_300/event_transcript.md") === "/app/timeline/20260101", "chronicle path anchors to timeline day");
const oddDay = "2026/01/01?#x";
const encodedOddDay = encodeURIComponent(oddDay);
const oddDayDetail = window.ImportDetail.renderDetail({
  ...clean,
  imported_json: {...clean.imported_json, target_day: oddDay, date_range: ["20260101", "20260101"]}
});
assert(oddDayDetail.includes(`/app/activities/${encodedOddDay}`), "activities day href is url encoded");
assert(oddDayDetail.includes(`/app/timeline/${encodedOddDay}`), "timeline day href is url encoded");

const meta = window.ImportDetail.renderMeta(clean);
assert(meta.includes(clean.import_json.original_filename), "meta includes escaped filename");
assert(meta.includes("status-badge success"), "meta keeps status badge");
const detail = window.ImportDetail.renderDetail(collision);
assert(detail.includes('<details class="drawer"'), "detail renders drawer");
assert(detail.includes('drawer-chip--warn'), "detail renders warn chip");
assert(detail.includes('href="/app/settings#profile"'), "collision links profile section");
assert(detail.includes("source &lt;person&gt;"), "collision source is escaped");
assert(detail.includes("/tmp/staging/&lt;entity&gt;/entity.json"), "staging path is escaped");
assert(detail.includes("bad &lt;segment&gt;"), "segment reason is escaped");
assert(detail.includes("summary &lt;error&gt;"), "summary error is escaped");
assert(detail.includes("processed 2026-01-01 09:00:30 · ics importer · nothing left this machine"), "provenance line is exact");
const sparseSummaryDetail = window.ImportDetail.renderDetail({
  ...clean,
  imported_json: {...clean.imported_json, merge_summary: {segments_copied: 1}}
});
const sparseSummaryMatch = sparseSummaryDetail.match(/<section class="import-drawer-section"><h3>merge summary<\\/h3><dl class="drawer-kv">([\\s\\S]*?)<\\/dl><\\/section>/);
assert(sparseSummaryMatch, "sparse merge summary section renders");
assert(sparseSummaryMatch[1].includes("<dt>segments</dt><dd>1 copied</dd>"), "sparse merge summary renders present counter");
assert(!sparseSummaryMatch[1].includes("0 skipped"), "sparse merge summary omits absent skipped counter");
assert(!sparseSummaryMatch[1].includes("0 errored"), "sparse merge summary omits absent errored counter");
assert(!sparseSummaryMatch[1].includes("<dt>entities</dt>"), "sparse merge summary omits fully absent rows");
const emptySummaryDetail = window.ImportDetail.renderDetail({
  ...clean,
  imported_json: {...clean.imported_json, merge_summary: {}}
});
assert(!emptySummaryDetail.includes("<h3>merge summary</h3>"), "empty merge summary section is omitted");
const rawMatches = detail.match(/<details class="drawer-raw">/g) || [];
assert(rawMatches.length === 1, "detail renders one combined raw disclosure");
const rawMatch = detail.match(/<details class="drawer-raw">[\\s\\S]*?<pre>([\\s\\S]*?)<\\/pre>/);
const parsedRaw = JSON.parse(htmlUnescape(rawMatch[1]));
assert(Object.prototype.hasOwnProperty.call(parsedRaw, "import_json"), "raw payload includes import_json key");
assert(Object.prototype.hasOwnProperty.call(parsedRaw, "imported_json"), "raw payload includes imported_json key");
assert(parsedRaw.import_json.original_filename === collision.import_json.original_filename, "raw payload import filename round-trips");
assert(!window.ImportDetail.renderDetail({import_json: null, imported_json: null}).includes('details class="drawer-raw"'), "raw disclosure is omitted when both payloads are null");
assert(window.ImportDetail.renderDetail(errorPayload).includes("bad &lt;archive&gt;"), "processing error is escaped");
""",
        ]
    )
    subprocess.run([node, "-e", script], check=True, text=True)


def test_document_upload_stages_emits_command_and_imports_new_shape_segment(
    client, tmp_path, monkeypatch
):
    import importlib

    import_routes = importlib.import_module("solstone.apps.import.routes")
    cli_mod = importlib.import_module("solstone.think.importers.cli")
    doc_mod = importlib.import_module("solstone.think.importers.documents")
    journal = Path(import_routes.state.journal_root)
    source_pdf = _write_text_pdf(
        tmp_path / "upload.pdf",
        (
            "Route upload document has enough extractable text for the worker-backed "
            "document importer to create a new transcript."
        ),
    )
    emitted: list[dict] = []

    monkeypatch.setattr(
        import_routes,
        "callosum_send",
        lambda tract, event, **kwargs: (
            emitted.append({"tract": tract, "event": event, **kwargs}) or True
        ),
    )
    monkeypatch.setattr(cli_mod, "CallosumConnection", lambda **kwargs: MagicMock())
    monkeypatch.setattr(cli_mod, "_status_emitter", lambda: None)
    monkeypatch.setattr(cli_mod, "index_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        doc_mod,
        "generate",
        lambda *, contents, context, **kwargs: pytest.fail("unexpected model call"),
    )

    save_response = client.post(
        "/app/import/api/save",
        data={
            "file": (BytesIO(source_pdf.read_bytes()), "contract.pdf"),
            "client_item_id": "document-upload",
            "facet": "work",
            "setting": "review",
            "source_hint": "document",
        },
        content_type="multipart/form-data",
    )

    assert save_response.status_code == 200
    saved = save_response.get_json()
    timestamp = saved["timestamp"]
    staged_pdf = journal / "imports" / timestamp / "contract.pdf"
    import_json = journal / "imports" / timestamp / "import.json"
    assert staged_pdf.read_bytes() == source_pdf.read_bytes()
    assert json.loads(import_json.read_text(encoding="utf-8"))["source_hint"] == (
        "document"
    )

    start_response = client.post(
        "/app/import/api/start",
        json={"path": str(staged_pdf), "timestamp": timestamp, "force": True},
    )

    assert start_response.status_code == 200
    assert emitted
    assert emitted[-1]["queue_if_active_cmd_differs"] is True
    cmd = emitted[-1]["cmd"]
    assert cmd == [
        "journal",
        "importer",
        str(staged_pdf),
        timestamp,
        "--facet",
        "work",
        "--setting",
        "review",
        "--source",
        "document",
        "--force",
    ]

    cli_argv = [cmd[0], cmd[2], "--timestamp", cmd[3], *cmd[4:]]
    monkeypatch.setattr(sys, "argv", cli_argv)
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    cli_mod.main()

    segments = list((journal / "chronicle").glob("*/import.document/*"))
    assert len(segments) == 1
    segment_dir = segments[0]
    transcript = segment_dir / "document_transcript.md"
    assert (segment_dir / "original.pdf").read_bytes() == source_pdf.read_bytes()
    assert transcript.is_file()
    text = transcript.read_text(encoding="utf-8")
    assert "**Type:** Document" in text
    assert "**Extraction:" in text
    assert "Route upload document has enough extractable text" in text
    assert doc_mod.MARKER_MODEL_EXTRACTED.split("{NNNN}", 1)[0] not in text
