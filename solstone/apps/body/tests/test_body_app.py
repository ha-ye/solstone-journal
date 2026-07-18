# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import json
import logging
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

from solstone.apps.body import routes as body_routes
from solstone.apps.events import EventContext
from solstone.think.importers import health_schema

BODY_ROOT = Path(body_routes.__file__).resolve().parent


def _apple_health_card_stream() -> str:
    return health_schema.health_card_stream(health_schema.SOURCE_APPLE_HEALTH)


def _workspace_source() -> str:
    return (BODY_ROOT / "workspace.html").read_text(encoding="utf-8")


def _function_source(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    open_paren = source.index("(", start)
    paren_depth = 0
    close_paren = -1
    for index in range(open_paren, len(source)):
        char = source[index]
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close_paren = index
                break
    assert close_paren != -1, f"function {name} has no closing parameter list"
    brace = source.index("{", close_paren)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"function {name} has no closing brace")


def _function_sources(*names: str) -> str:
    source = _workspace_source()
    return "\n".join(_function_source(source, name) for name in names)


def _node_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    return node


def _collect_strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        strings: list[str] = []
        for value in node.values():
            strings.extend(_collect_strings(value))
        return strings
    if isinstance(node, list):
        strings: list[str] = []
        for value in node:
            strings.extend(_collect_strings(value))
        return strings
    return []


def _collect_strings_except_keys(node: object, excluded: set[str]) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        strings: list[str] = []
        for key, value in node.items():
            if key in excluded:
                continue
            strings.extend(_collect_strings_except_keys(value, excluded))
        return strings
    if isinstance(node, list):
        strings: list[str] = []
        for value in node:
            strings.extend(_collect_strings_except_keys(value, excluded))
        return strings
    return []


DEDUPE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS health_dedupe (
    dedupe_key TEXT PRIMARY KEY,
    source_family TEXT NOT NULL,
    source_record_id TEXT,
    record_type TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    value_hash TEXT,
    first_import_id TEXT,
    last_seen_import_id TEXT,
    normalized_ref TEXT,
    raw_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _row(
    record_type: str,
    start: str,
    end: str | None = None,
    *,
    value: str | None = None,
    unit: str | None = None,
    source: str = "Synthetic Device",
    kind: str = "record",
    metadata: dict | None = None,
) -> dict:
    day = start[:10].replace("-", "")
    row = {
        "schema": "solstone.health.normalized.v1",
        "source_family": "apple_health",
        "kind": kind,
        "dedupe_key": f"apple-health:{record_type}:{source}:{start}",
        "record_type": record_type,
        "day": day,
        "start_date": start,
        "source_name": source,
        "month": start[:7],
    }
    if end is not None:
        row["end_date"] = end
    if value is not None:
        row["value"] = value
    if unit is not None:
        row["unit"] = unit
    if metadata is not None:
        row["metadata"] = metadata
    return row


OURA_READINESS_TYPE = "oura.daily_readiness"
OURA_SLEEP_SCORE_TYPE = "oura.daily_sleep"
OURA_SLEEP_PERIOD_TYPE = "oura.sleep"
OURA_RESILIENCE_TYPE = "oura.daily_resilience"
OURA_STRESS_TYPE = "oura.daily_stress"
OURA_SPO2_TYPE = "oura.daily_spo2"
OURA_TEMP_DEV_TYPE = "oura.temperature_deviation"
OURA_CARDIOVASCULAR_AGE_TYPE = "oura.daily_cardiovascular_age"
OURA_VO2_MAX_TYPE = "oura.vo2_max"
OURA_WORKOUT_TYPE = "oura.workout"
OURA_SESSION_TYPE = "oura.session"
OURA_TAG_TYPE = "oura.enhanced_tag"
OURA_HEARTRATE_TYPE = "oura.heartrate"


def _oura_row(
    record_type: str,
    day: str,
    *,
    value=None,
    unit: str | None = None,
    start: str | None = None,
    end: str | None = None,
    kind: str = "daily_summary",
    metadata: dict | None = None,
) -> dict:
    """A normalized Oura API row matching the oura design doc's row schema.

    Values stay native JSON (ints, floats, strings) exactly as the
    importer's normalizer emits them — no stringly-typed values.
    """

    iso_day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    start = start or f"{iso_day}T04:00:00+00:00"
    row = {
        "schema": "solstone.health.oura.v1",
        "source_family": "oura_api",
        "kind": kind,
        "dedupe_key": f"oura-api:{record_type}:{day}:{start}",
        "record_type": record_type,
        "day": day,
        "start_date": start,
        "source_record_id": f"{record_type}-{day}",
        "month": iso_day[:7],
    }
    if end is not None:
        row["end_date"] = end
    if value is not None:
        row["value"] = value
    if unit is not None:
        row["unit"] = unit
    if metadata is not None:
        row["metadata"] = metadata
    return row


def _insert_dedupe_rows(journal: Path, rows: list[dict], import_id: str) -> None:
    db_path = journal / "imports" / "health-dedupe.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(DEDUPE_TABLE_SQL)
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO health_dedupe (
                    dedupe_key,
                    source_family,
                    record_type,
                    start_time,
                    end_time,
                    first_import_id,
                    last_seen_import_id,
                    normalized_ref,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["dedupe_key"],
                    row["source_family"],
                    row["record_type"],
                    row["start_date"],
                    row.get("end_date"),
                    import_id,
                    import_id,
                    row.get("normalized_ref", ""),
                    "2026-07-04T01:00:00Z",
                    "2026-07-04T01:00:00Z",
                ),
            )


def _seed_import(
    journal: Path,
    import_id: str,
    rows: list[dict],
    *,
    imported_at: str = "2026-07-04T01:00:00",
) -> None:
    """Write manifest + month shards + dedupe rows for synthetic entries."""
    for row in rows:
        row.setdefault("import_id", import_id)
    days = sorted({row["day"] for row in rows})
    _write_json(
        journal / "imports" / import_id / "manifest.json",
        {
            "import_id": import_id,
            "source_type": "apple_health",
            "source_hash": f"sha256:{import_id}",
            "entry_count": len(rows),
            "days_affected": days,
            "files_created": [],
            "imported_at": imported_at,
            "imported_via": "test",
        },
    )
    by_month: dict[str, list[dict]] = {}
    for row in rows:
        by_month.setdefault(row["month"], []).append(row)
    for month, month_rows in by_month.items():
        _append_jsonl(
            journal / "imports" / import_id / "normalized" / f"{month}.jsonl",
            month_rows,
        )
    _insert_dedupe_rows(journal, rows, import_id)


def _seed_health_import(journal: Path) -> None:
    import_id = "20260703_120000"
    card_stream = _apple_health_card_stream()
    _write_json(
        journal / "imports" / import_id / "manifest.json",
        {
            "import_id": import_id,
            "source_type": "apple_health",
            "source_hash": "sha256:synthetic",
            "entry_count": 5,
            "days_affected": ["20260703", "20260704"],
            "files_created": [
                str(
                    journal
                    / "chronicle"
                    / "20260703"
                    / card_stream
                    / "000000_300"
                    / "day_summary_transcript.md"
                )
            ],
            "imported_at": "2026-07-04T01:00:00",
            "imported_via": "test",
        },
    )
    rows = [
        {
            "schema": "solstone.health.normalized.v1",
            "source_family": "apple_health",
            "kind": "record",
            "dedupe_key": "apple-health:glucose:1",
            "record_type": "HKQuantityTypeIdentifierBloodGlucose",
            "day": "20260703",
            "start_date": "2026-07-03T08:00:00-06:00",
            "end_date": "2026-07-03T08:01:00-06:00",
            "source_name": "Synthetic Stelo",
            "unit": "mg/dL",
            "value": "100",
            "import_id": import_id,
            "month": "2026-07",
            "normalized_ref": (f"imports/{import_id}/normalized/2026-07.jsonl#L1"),
        },
        {
            "schema": "solstone.health.normalized.v1",
            "source_family": "apple_health",
            "kind": "record",
            "dedupe_key": "apple-health:glucose:2",
            "record_type": "HKQuantityTypeIdentifierBloodGlucose",
            "day": "20260703",
            "start_date": "2026-07-03T12:00:00-06:00",
            "end_date": "2026-07-03T12:01:00-06:00",
            "source_name": "Synthetic Stelo",
            "unit": "mg/dL",
            "value": "140",
            "import_id": import_id,
            "month": "2026-07",
            "normalized_ref": (f"imports/{import_id}/normalized/2026-07.jsonl#L2"),
        },
        {
            "schema": "solstone.health.normalized.v1",
            "source_family": "apple_health",
            "kind": "record",
            "dedupe_key": "apple-health:heart-rate:1",
            "record_type": "HKQuantityTypeIdentifierHeartRate",
            "day": "20260703",
            "start_date": "2026-07-03T13:00:00-06:00",
            "source_name": "Synthetic Watch",
            "unit": "count/min",
            "value": "72",
            "import_id": import_id,
            "month": "2026-07",
            "normalized_ref": (f"imports/{import_id}/normalized/2026-07.jsonl#L3"),
        },
        {
            "schema": "solstone.health.normalized.v1",
            "source_family": "apple_health",
            "kind": "workout",
            "dedupe_key": "apple-health:workout:1",
            "record_type": "HKWorkoutActivityTypeRunning",
            "day": "20260704",
            "start_date": "2026-07-04T06:30:00-06:00",
            "source_name": "Synthetic Watch",
            "import_id": import_id,
            "month": "2026-07",
            "normalized_ref": (f"imports/{import_id}/normalized/2026-07.jsonl#L4"),
        },
    ]
    _append_jsonl(
        journal / "imports" / import_id / "normalized" / "2026-07.jsonl", rows
    )
    summary = (
        "# Apple Health Summary\n\n"
        "Day: 20260703\n\n"
        "Counts by type:\n"
        "- HKQuantityTypeIdentifierBloodGlucose: 2\n"
        "- HKQuantityTypeIdentifierHeartRate: 1\n"
    )
    summary_path = (
        journal
        / "chronicle"
        / "20260703"
        / _apple_health_card_stream()
        / "000000_300"
        / "day_summary_transcript.md"
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary, encoding="utf-8")
    _insert_dedupe_rows(journal, rows, import_id)


def test_status_api_summarizes_synthetic_health_import(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/status")

    assert response.status_code == 200
    status = response.get_json()
    assert status["imports"][0]["import_id"] == "20260703_120000"
    assert status["coverage_window"] == {
        "start": "2026-07-03T08:00:00-06:00",
        "end": "2026-07-04T06:30:00-06:00",
    }
    assert status["normalized"]["total"] == 4
    assert status["normalized"]["by_type"] == {
        "HKQuantityTypeIdentifierBloodGlucose": 2,
        "HKQuantityTypeIdentifierHeartRate": 1,
        "HKWorkoutActivityTypeRunning": 1,
    }
    assert status["normalized"]["by_source"] == {
        "Synthetic Stelo": 2,
        "Synthetic Watch": 2,
    }
    assert status["normalized"]["by_month"] == {"2026-07": 4}
    assert status["dedupe"]["total"] == 4
    assert status["dedupe"]["by_type"] == {
        "HKQuantityTypeIdentifierBloodGlucose": 2,
        "HKQuantityTypeIdentifierHeartRate": 1,
        "HKWorkoutActivityTypeRunning": 1,
    }
    assert status["dedupe"]["by_source"] == {"apple_health": 4}
    assert status["dedupe"]["by_month"] == {"2026-07": 4}
    assert status["latest_by_source"] == {
        "Synthetic Stelo": "2026-07-03T12:00:00-06:00",
        "Synthetic Watch": "2026-07-04T06:30:00-06:00",
    }


def test_status_api_and_workspace_cover_import_and_dedupe_sections(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/status")

    assert response.status_code == 200
    status = response.get_json()
    assert status["imports"][0]["import_id"] == "20260703_120000"
    assert status["normalized"]["by_type"]["HKQuantityTypeIdentifierBloodGlucose"] == 2
    assert status["dedupe"]["by_source"] == {"apple_health": 4}

    source = _function_source(_workspace_source(), "renderOverviewAudit")
    assert "By type" in source
    assert "Dedupe by type" in source
    assert "renderCountList(status.normalized?.by_type" in source
    assert "renderCountList(status.dedupe?.by_type" in source


def test_body_drawer_line_builders_run_under_node(body_env):
    node = _node_or_skip()
    env = body_env()
    empty_day = env.client.get("/app/body/api/day/20260703").get_json()
    _seed_health_import(env.journal)
    status = env.client.get("/app/body/api/status").get_json()
    day_payload = env.client.get("/app/body/api/day/20260703").get_json()
    source = _workspace_source()
    functions = "\n".join(
        _function_source(source, name)
        for name in (
            "asArray",
            "asObject",
            "formatSourceTime",
            "sourceTimeSortKey",
            "auditDrawerLine",
            "dayAuditDrawerLine",
            "anatomyDrawerLine",
            "dayAuditHasNothingToDisclose",
        )
    )
    script = "\n".join(
        [
            functions,
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            f"const status = {json.dumps(status)};",
            f"const dayPayload = {json.dumps(day_payload)};",
            f"const emptyDay = {json.dumps(empty_day)};",
            """
function expectedStatusLine(payload) {
  const imports = Array.isArray(payload.imports) ? payload.imports : [];
  const dedupe = payload.dedupe && typeof payload.dedupe === 'object' ? payload.dedupe : {};
  const total = Number(dedupe.total || 0);
  const latest = latestSourceValue(payload);
  const parts = [
    `${imports.length} ${imports.length === 1 ? "import" : "imports"}`,
    `${total} ${total === 1 ? "row" : "rows"} deduped`,
  ];
  if (latest) parts.push(`latest ${formatSourceTime(latest)}`);
  return parts.join(" · ");
}

function latestSourceValue(payload) {
  const latestMap = payload.latest_by_source && typeof payload.latest_by_source === 'object' && !Array.isArray(payload.latest_by_source)
    ? payload.latest_by_source
    : {};
  return Object.values(latestMap)
    .map((value) => String(value || '').trim())
    .filter(Boolean)
    .reduce((max, value) => sourceTimeSortKey(value) > sourceTimeSortKey(max) ? value : max, '');
}

function expectedDayLine(audit) {
  const types = audit.types && typeof audit.types === 'object' && !Array.isArray(audit.types) ? audit.types : {};
  const kinds = Object.keys(types).length;
  const total = Object.values(types).reduce((sum, count) => sum + Number(count || 0), 0);
  return `${kinds} ${kinds === 1 ? "kind" : "kinds"} · ${total} ${total === 1 ? "row" : "rows"}`;
}

function expectedAnatomyLine(items) {
  if (!items.length) return "";
  let strongest = null;
  for (const item of items) {
    const value = Number(item.value);
    if (!Number.isFinite(value)) continue;
    if (!strongest || value > strongest.value) {
      strongest = { label: String(item.label || ""), value };
    }
  }
  return strongest ? `${items.length} ${items.length === 1 ? "contributor" : "contributors"} · strongest: ${strongest.label}` : "";
}

assert(formatSourceTime("2026-07-03T12:00:00-06:00") === "jul 3, 12:00 pm", "fixture time formats deterministically");
assert(sourceTimeSortKey("2026-07-04T10:00:00") === sourceTimeSortKey("2026-07-04T10:00:00Z"), "missing offset sorts as utc");
assert(sourceTimeSortKey("not a time") < sourceTimeSortKey("2026-07-04T10:00:00Z"), "unparseable source time sorts before parsed values");
const statusLine = auditDrawerLine(status);
const latest = latestSourceValue(status);
assert(latest, "status fixture carries latest source times");
assert(statusLine === expectedStatusLine(status), "overview line carries imports, deduped rows, and latest source time");
assert(statusLine.includes(`latest ${formatSourceTime(latest)}`), "overview line formats max latest source time");
const noLatestStatus = {...status, latest_by_source: {}};
const noLatestLine = auditDrawerLine(noLatestStatus);
assert(noLatestLine === expectedStatusLine(noLatestStatus), "overview line follows payload with no latest source times");
assert(!noLatestLine.includes("latest"), "overview no-latest line drops latest fragment");
assert(noLatestLine.split(" · ").length === 2, "overview no-latest line has no dangling separator");
const sameWallClockStatus = {
  ...status,
  latest_by_source: {
    lexical: "2026-07-04T09:00:00Z",
    later: "2026-07-04T09:00:00-06:00",
  },
};
assert(sameWallClockStatus.latest_by_source.lexical > sameWallClockStatus.latest_by_source.later, "same-wall-clock fixture defeats lexicographic max");
assert(sourceTimeSortKey(sameWallClockStatus.latest_by_source.lexical) < sourceTimeSortKey(sameWallClockStatus.latest_by_source.later), "same-wall-clock mixed offsets sort by instant");
const mixedOffsetStatus = {
  ...status,
  latest_by_source: {
    lexical: "2026-07-04T23:30:00+14:00",
    later: "2026-07-04T10:00:00-06:00",
  },
};
assert(mixedOffsetStatus.latest_by_source.lexical > mixedOffsetStatus.latest_by_source.later, "mixed-offset fixture defeats lexicographic max");
assert(auditDrawerLine(mixedOffsetStatus) === expectedStatusLine(mixedOffsetStatus), "overview line follows mixed-offset payload");
assert(auditDrawerLine(mixedOffsetStatus).includes(`latest ${formatSourceTime(mixedOffsetStatus.latest_by_source.later)}`), "overview line normalizes mixed offsets");
assert(dayAuditDrawerLine(dayPayload.audit) === expectedDayLine(dayPayload.audit), "day line carries kinds and rows");
assert(dayAuditHasNothingToDisclose(emptyDay) === true, "empty day has nothing to disclose");
assert(dayAuditHasNothingToDisclose(dayPayload) === false, "seeded day has disclosure data");
assert(anatomyDrawerLine([]) === "", "empty anatomy has no line");
const tied = [{label: "alpha", value: 10}, {label: "beta", value: 10}, {label: "gamma", value: 9}];
assert(anatomyDrawerLine(tied) === expectedAnatomyLine(tied), "anatomy line carries contributor count and strongest label");
assert(anatomyDrawerLine(tied).endsWith(`strongest: ${tied[0].label}`), "strongest contributor tie keeps first payload item");
""",
        ]
    )
    subprocess.run([node, "-e", script], check=True, text=True)


def test_drawer_render_emphasizes_digits_after_escaping_under_node():
    node = _node_or_skip()
    source = Path("solstone/convey/static/drawer.js").read_text(encoding="utf-8")
    functions = "\n".join(
        _function_source(source, name) for name in ("escapeHtml", "render")
    )
    script = "\n".join(
        [
            functions,
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            """
function decodeEntities(text) {
  const named = { amp: "&", lt: "<", gt: ">", quot: "\\\"", apos: "'" };
  return String(text).replace(/&(#\\d+|#x[0-9a-fA-F]+|\\w+);/g, (entity, body) => {
    if (body[0] === "#") {
      const value = body[1]?.toLowerCase() === "x"
        ? Number.parseInt(body.slice(2), 16)
        : Number.parseInt(body.slice(1), 10);
      return Number.isFinite(value) ? String.fromCodePoint(value) : entity;
    }
    return Object.prototype.hasOwnProperty.call(named, body) ? named[body] : entity;
  });
}

function textContentAfterUnescape(html) {
  return String(html).split(/(<[^>]*>)/g)
    .filter((chunk) => chunk && !chunk.startsWith("<"))
    .map(decodeEntities)
    .join("");
}

function drawerLineHtml(line) {
  const rendered = render({ id: "probe", label: "audit", line, bodyHtml: "" });
  const match = rendered.match(/<span class="drawer-line">([\\s\\S]*?)<\\/span>/);
  return match ? match[1] : "";
}

const plainLine = "oura's ring · 3 imports · latest jul 3, 12:00 pm";
const lineHtml = drawerLineHtml(plainLine);
assert(lineHtml.includes("<b>3</b>"), "digit run is emphasized");
assert(lineHtml.includes("&#39;"), "apostrophe entity remains intact");
assert(!lineHtml.includes("&#<b>"), "emphasis does not split escaped entity");
assert(textContentAfterUnescape(lineHtml) === plainLine, "line text content is invariant");

const proseHtml = drawerLineHtml("oura's ring latest today");
assert(!proseHtml.includes("<b>"), "pure prose line has no emphasis");

const injectionHtml = drawerLineHtml("<img src=x onerror=alert(1)>");
assert(injectionHtml.includes("&lt;img"), "injection probe remains escaped");
assert(!injectionHtml.includes("<img"), "injection probe does not render raw tag");
""",
        ]
    )
    subprocess.run([node, "-e", script], check=True, text=True)


def test_overview_import_evidence_preserves_manifest_fields(body_env):
    node = _node_or_skip()
    env = body_env()
    _seed_health_import(env.journal)
    status = env.client.get("/app/body/api/status").get_json()
    item = status["imports"][0]
    source = _workspace_source()
    functions = "\n".join(
        _function_source(source, name)
        for name in (
            "asArray",
            "safeDay",
            "bodyDayHref",
            "importEvidenceMeta",
            "renderImportEvidence",
        )
    )
    script = "\n".join(
        [
            "const DAY_RE = /^\\d{8}$/;",
            "function escapeHtml(value) { return String(value ?? ''); }",
            functions,
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            f"const item = {json.dumps(item)};",
            """
const rendered = renderImportEvidence([item]);
assert(rendered.includes(item.import_id), "import id renders");
assert(rendered.includes(`/app/import/${item.import_id}`), "import id links to import detail");
assert(rendered.includes(item.source_type), "source type renders");
assert(rendered.includes(item.imported_at), "imported timestamp renders");
assert(rendered.includes(String(item.entry_count)), "entry count renders");
assert(rendered.includes(item.normalized_months_label), "normalized months render");
for (const day of item.days_affected) {
  assert(rendered.includes(`href="/app/body/${day}"`), `day ${day} link renders`);
  assert(rendered.includes(`>${day}</a>`), `day ${day} text renders`);
}
""",
        ]
    )
    subprocess.run([node, "-e", script], check=True, text=True)


def test_overview_audit_empty_state_is_not_a_drawer(body_env):
    node = _node_or_skip()
    env = body_env()
    _seed_health_import(env.journal)
    status = env.client.get("/app/body/api/status").get_json()
    source = _workspace_source()
    functions = "\n".join(
        _function_source(source, name)
        for name in (
            "html",
            "asArray",
            "asObject",
            "safeDay",
            "bodyDayHref",
            "renderCountList",
            "formatSourceTime",
            "sourceTimeSortKey",
            "auditDrawerLine",
            "renderDrawerKv",
            "importEvidenceMeta",
            "renderImportEvidence",
            "renderOverviewAudit",
        )
    )
    script = "\n".join(
        [
            "const DAY_RE = /^\\d{8}$/;",
            "function escapeHtml(value) { return String(value ?? ''); }",
            functions,
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            f"const status = {json.dumps(status)};",
            """
const drawerCalls = [];
const window = {
  Drawer: {
    render(options) {
      drawerCalls.push(options);
      return `<details class="drawer"><summary>${options.label}</summary><div>${options.bodyHtml || ""}</div></details>`;
    },
  },
};
const emptyRendered = renderOverviewAudit({ ...status, imports: [] });
assert(emptyRendered === '<p class="drawer-empty">no imports yet — body data appears here when a health export is imported.</p>', "empty overview audit renders empty paragraph");
assert(!emptyRendered.includes("<details"), "empty overview audit is not a details drawer");
assert(drawerCalls.length === 0, "empty overview audit does not call Drawer.render");

const populatedRendered = renderOverviewAudit(status);
assert(populatedRendered.includes('<details class="drawer">'), "populated overview audit renders drawer");
assert(drawerCalls.length === 1, "populated overview audit calls Drawer.render");
assert(drawerCalls[0].id === "body-overview-audit", "populated overview audit uses audit drawer id");
""",
        ]
    )
    subprocess.run([node, "-e", script], check=True, text=True)


def test_day_audit_empty_state_is_not_a_drawer():
    source = _function_source(_workspace_source(), "renderDayAudit")

    assert (
        "return '<p class=\"drawer-empty\">no import bookkeeping for this day.</p>';"
        in source
    )
    assert source.index("dayAuditHasNothingToDisclose(bodyDay)") < source.index(
        "window.Drawer.render"
    )
    assert 'label: "raw types"' in source


def test_body_drawer_labels_follow_contract():
    source = _function_sources(
        "renderOverviewAudit",
        "renderDayAudit",
        "renderScoreAnatomy",
    )

    assert 'label: "audit"' in source
    assert 'label: "raw types"' in source
    assert 'label: "why this score?"' in source
    assert "Audit ·" not in source
    assert "Why this score" not in source


def test_day_api_returns_summary_and_factual_glucose_stats(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/day/20260703")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["day"] == "20260703"
    assert "HKQuantityTypeIdentifierBloodGlucose: 2" in payload["summary_markdown"]
    assert payload["glucose"] == {
        "count": 2,
        "min": 100.0,
        "max": 140.0,
        "mean": 120.0,
        "unit": "mg/dL",
    }


def test_day_api_and_workspace_cover_summary_and_glucose_facts_only(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/day/20260703")

    assert response.status_code == 200
    payload = response.get_json()
    assert "HKQuantityTypeIdentifierBloodGlucose: 2" in payload["summary_markdown"]
    glucose = payload["glucose_series"][0]
    assert glucose["mean_label"] == "120"
    assert glucose["range_label"] == "100–140 mg/dL"

    source = _function_sources("renderDayAudit", "renderGlucoseCard")
    assert "Day summary" in source
    assert "What was glucose doing?" in source
    assert "mean_label" in source
    lowered = source.lower()
    assert "normal glucose" not in lowered
    assert "high glucose" not in lowered
    assert "low glucose" not in lowered


def test_day_api_lede_and_workspace_hero_render_once(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    lede = env.client.get("/app/body/api/day/20260703").get_json()["lede"]
    source = _function_source(_workspace_source(), "renderDayBrief")

    # The lede lives in the "What your body added to the day" hero card
    # only — the page header carries just the date context.
    assert lede
    assert source.count("bodyDay.lede") == 1
    assert "What your body added to the day" in source


def test_body_workspace_template_avoids_surveillance_verbs():
    body_root = Path(body_routes.__file__).resolve().parent
    banned = {"capture", "watch", "record", "monitor", "track", "collect"}
    checked = [body_root / "workspace.html"]

    for path in checked:
        source = path.read_text(encoding="utf-8").lower()
        found = {word for word in banned if word in source}
        assert found == set(), f"{path.name} contains banned copy terms: {found}"


def test_body_call_module_uses_convey_http_only():
    body_root = Path(body_routes.__file__).resolve().parent
    source = (body_root / "call.py").read_text(encoding="utf-8")

    assert "solstone.think.convey_client" in source
    assert '@app.command("window")' in source
    assert "/app/body/api/window" in source
    assert "from pathlib" not in source
    assert "import os" not in source
    assert "sqlite3" not in source
    assert "open(" not in source


def test_read_routes_create_nothing_in_empty_journal(body_env):
    env = body_env()
    imports_root = env.journal / "imports"
    assert not imports_root.exists()

    assert env.client.get("/app/body/api/status").status_code == 200
    assert env.client.get("/app/body/api/day/20260703").status_code == 200
    assert env.client.get("/app/body/api/index").status_code == 200
    assert env.client.get("/app/body/api/stats/202607").status_code == 200
    recent = env.client.get("/app/body/api/recent?before=20260703")
    assert recent.status_code == 200
    assert recent.get_json() == {"days": [], "has_more": False}
    assert env.client.get("/app/body/api/trends").status_code == 200
    assert env.client.get("/app/body/").status_code == 200
    assert env.client.get("/app/body/trends").status_code == 200
    assert env.client.get("/app/body/20260703").status_code == 200

    assert not imports_root.exists()
    assert not (imports_root / "health-dedupe.sqlite").exists()


def test_non_health_import_manifests_are_excluded(body_env):
    env = body_env()
    _seed_health_import(env.journal)
    _write_json(
        env.journal / "imports" / "20260601_090000" / "manifest.json",
        {
            "import_id": "20260601_090000",
            "source_type": "plaud",
            "source_hash": "sha256:other",
            "entry_count": 3,
            "days_affected": ["20260601"],
            "files_created": [],
            "imported_at": "2026-06-01T09:00:00",
            "imported_via": "test",
        },
    )
    (env.journal / "imports" / "20260602_090000").mkdir(parents=True)
    (env.journal / "imports" / "20260602_090000" / "manifest.json").write_text(
        "not json", encoding="utf-8"
    )

    response = env.client.get("/app/body/api/status")

    assert response.status_code == 200
    import_ids = [item["import_id"] for item in response.get_json()["imports"]]
    assert import_ids == ["20260703_120000"]


def test_status_api_lists_apple_health_and_oura_api_import_manifests(body_env):
    env = body_env()
    _seed_health_import(env.journal)
    import_id = "20260705_090000"
    oura_row = _oura_row(OURA_READINESS_TYPE, "20260705", value=82, unit="score")
    _write_json(
        env.journal / "imports" / import_id / "manifest.json",
        {
            "import_id": import_id,
            "source_type": health_schema.SOURCE_OURA_API,
            "source_hash": "sha256:oura",
            "entry_count": 1,
            "days_affected": ["20260705"],
            "files_created": [],
            "imported_at": "2026-07-05T09:00:00",
            "imported_via": "test",
        },
    )
    _append_jsonl(
        env.journal / "imports" / import_id / "normalized" / "2026-07.jsonl",
        [oura_row],
    )

    response = env.client.get("/app/body/api/status")

    assert response.status_code == 200
    status = response.get_json()
    imports = status["imports"]
    assert len(imports) == 2
    assert status["archive"]["import_count"] == 2
    by_id = {item["import_id"]: item for item in imports}
    assert set(by_id) == {"20260703_120000", import_id}
    assert by_id["20260703_120000"]["source_type"] == "apple_health"
    assert by_id[import_id]["source_type"] == health_schema.SOURCE_OURA_API
    assert by_id[import_id]["normalized_months_label"] == "2026-07"
    hero_source = _function_source(_workspace_source(), "renderArchiveHero")
    assert "Apple Health " not in hero_source
    assert "health " in hero_source


def test_month_stats_api_returns_day_counts(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/stats/202607")

    assert response.status_code == 200
    assert response.get_json() == {"20260703": 3, "20260704": 1}


def test_month_stats_api_rejects_dashed_month(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/stats/2026-07")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_request_value"


def _imports_snapshot(journal: Path) -> dict[Path, int]:
    imports = journal / "imports"
    if not imports.exists():
        return {}
    return {path: path.stat().st_mtime_ns for path in sorted(imports.rglob("*"))}


def test_api_index_reports_nonzero_coverage_and_months(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/index")

    assert response.status_code == 200
    assert response.get_json() == {
        "coverage": {"start": "20260703", "end": "20260704"},
        "months": {"202607": 4},
    }


def test_api_index_month_totals_match_api_stats(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/index")

    assert response.status_code == 200
    body = response.get_json()
    for month, total in body["months"].items():
        month_response = env.client.get(f"/app/body/api/stats/{month}")
        assert month_response.status_code == 200
        assert total == sum(month_response.get_json().values())


def test_api_index_empty_journal(body_env):
    env = body_env()

    response = env.client.get("/app/body/api/index")

    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "months": {}}


def test_api_index_is_read_only(body_env):
    env = body_env()
    _seed_health_import(env.journal)
    before = _imports_snapshot(env.journal)

    response = env.client.get("/app/body/api/index")

    assert response.status_code == 200
    assert _imports_snapshot(env.journal) == before


def test_day_api_rejects_invalid_day(body_env):
    env = body_env()

    assert env.client.get("/app/body/api/day/not-a-day").status_code == 400
    assert env.client.get("/app/body/api/day/2026-07-03").status_code == 400
    assert env.client.get("/app/body/api/day/20261399").status_code == 400


def test_day_page_rejects_invalid_day_and_valid_day_serves_shell(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    invalid = env.client.get("/app/body/20261399")
    assert invalid.status_code == 400
    assert invalid.get_json()["reason_code"] == "invalid_day"

    valid = env.client.get("/app/body/20260703")
    assert valid.status_code == 200
    html = valid.get_data(as_text=True)
    assert 'data-solstone-shell="spa"' in html
    assert "HKQuantityTypeIdentifierBloodGlucose: 2" not in html


def test_day_api_counts_overlapping_bundles_once(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    first_shard = (
        env.journal / "imports" / "20260703_120000" / "normalized" / "2026-07.jsonl"
    )
    second_shard = (
        env.journal / "imports" / "20260704_090000" / "normalized" / "2026-07.jsonl"
    )
    second_shard.parent.mkdir(parents=True)
    second_shard.write_text(first_shard.read_text(encoding="utf-8"), encoding="utf-8")
    _write_json(
        env.journal / "imports" / "20260704_090000" / "manifest.json",
        {
            "import_id": "20260704_090000",
            "source_type": "apple_health",
            "source_hash": "sha256:full-backfill",
            "entry_count": 4,
            "days_affected": ["20260703", "20260704"],
            "files_created": [],
            "imported_at": "2026-07-04T09:00:00",
            "imported_via": "test",
        },
    )

    response = env.client.get("/app/body/api/day/20260703")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["glucose"]["count"] == 2
    assert payload["entry_total"] == 3


def test_status_api_counts_latest_sources_from_overlapping_bundles_once(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    first_shard = (
        env.journal / "imports" / "20260703_120000" / "normalized" / "2026-07.jsonl"
    )
    second_shard = (
        env.journal / "imports" / "20260704_090000" / "normalized" / "2026-07.jsonl"
    )
    second_shard.parent.mkdir(parents=True)
    second_shard.write_text(first_shard.read_text(encoding="utf-8"), encoding="utf-8")
    _write_json(
        env.journal / "imports" / "20260704_090000" / "manifest.json",
        {
            "import_id": "20260704_090000",
            "source_type": "apple_health",
            "source_hash": "sha256:full-backfill",
            "entry_count": 4,
            "days_affected": ["20260703", "20260704"],
            "files_created": [],
            "imported_at": "2026-07-04T09:00:00",
            "imported_via": "test",
        },
    )

    response = env.client.get("/app/body/api/status")

    assert response.status_code == 200
    status = response.get_json()
    assert status["normalized"]["by_source"] == {
        "Synthetic Stelo": 2,
        "Synthetic Watch": 2,
    }


def test_status_api_cache_invalidates_when_dedupe_db_changes(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    first = env.client.get("/app/body/api/status")
    assert first.status_code == 200
    assert first.get_json()["dedupe"]["total"] == 4

    db_path = env.journal / "imports" / "health-dedupe.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO health_dedupe (
                dedupe_key,
                source_family,
                record_type,
                start_time,
                end_time,
                first_import_id,
                last_seen_import_id,
                normalized_ref,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "apple-health:glucose:cache-invalidates",
                "apple_health",
                "HKQuantityTypeIdentifierBloodGlucose",
                "2026-07-04T07:30:00-06:00",
                "2026-07-04T07:31:00-06:00",
                "20260703_120000",
                "20260703_120000",
                "imports/20260703_120000/normalized/2026-07.jsonl#L5",
                "2026-07-04T01:00:00Z",
                "2026-07-04T01:00:00Z",
            ),
        )

    second = env.client.get("/app/body/api/status")

    assert second.status_code == 200
    payload = second.get_json()
    assert payload["dedupe"]["total"] == 5
    assert payload["normalized"]["total"] == 5


# --- Day view: sleep --------------------------------------------------------


SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
STEP_TYPE = "HKQuantityTypeIdentifierStepCount"
GLUCOSE_TYPE = "HKQuantityTypeIdentifierBloodGlucose"


def _seed_cross_midnight_sleep(journal: Path) -> None:
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-06-30T22:58:00-06:00",
            "2026-07-01T02:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-01T02:00:00-06:00",
            "2026-07-01T07:08:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepDeep",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-06-30T23:30:00-06:00",
            "2026-07-01T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            source="Synthetic Wrist",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-01T14:00:00-06:00",
            "2026-07-01T14:45:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(journal, "20260801_000000", rows)


def test_day_api_sleep_session_crosses_midnight_and_month_boundary(body_env):
    env = body_env()
    _seed_cross_midnight_sleep(env.journal)

    response = env.client.get("/app/body/api/day/20260701")

    assert response.status_code == 200
    sleep = response.get_json()["sleep"]
    assert sleep is not None
    # The session ending that morning starts the previous evening — in the
    # prior month's shard.
    assert sleep["window"] == "10:58 PM – 7:08 AM"
    assert sleep["duration"] == "8h 10m"
    # Longest-coverage source is primary; the other is named, never summed.
    assert sleep["source"] == "Synthetic Ring"
    assert sleep["other_sources"] == ["Synthetic Wrist"]
    # A session fully inside the day lists as a nap.
    assert sleep["naps"] == [{"window": "2:00 PM – 2:45 PM", "duration": "45m"}]
    kinds = [segment["kind"] for segment in sleep["bar"]["segments"]]
    assert kinds == ["main", "nap"]


def test_day_api_sleep_not_attributed_to_the_night_start_day(body_env):
    env = body_env()
    _seed_cross_midnight_sleep(env.journal)

    response = env.client.get("/app/body/api/day/20260630")

    assert response.status_code == 200
    # The night that starts on the 30th ends on July 1 — it belongs to the
    # July 1 card, not this one.
    assert response.get_json()["sleep"] is None


def test_day_api_bedtime_fragment_attributes_only_to_next_days_night(body_env):
    env = body_env()
    rows = [
        # The night that ends this day's morning (starts the prior evening).
        _row(
            SLEEP_TYPE,
            "2023-08-14T23:02:00-06:00",
            "2023-08-15T06:35:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
        # Bedtime fragment before midnight — the first slice of the night
        # that ends the NEXT morning, day-attributed by its start time.
        _row(
            SLEEP_TYPE,
            "2023-08-15T23:17:00-06:00",
            "2023-08-15T23:58:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
        # The rest of that night, within the merge gap, on the next day.
        _row(
            SLEEP_TYPE,
            "2023-08-16T00:20:00-06:00",
            "2023-08-16T06:35:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260910_110000", rows)

    day = env.client.get("/app/body/api/day/20230815").get_json()["sleep"]
    # Main night unchanged; the 11:17 PM fragment merges into the following
    # night instead of misreading as this day's nap.
    assert day["window"] == "11:02 PM – 6:35 AM"
    assert day["naps"] == []
    assert [segment["kind"] for segment in day["bar"]["segments"]] == ["main"]

    next_day = env.client.get("/app/body/api/day/20230816").get_json()["sleep"]
    # The fragment appears exactly once: as the start of the next day's main.
    assert next_day["window"] == "11:17 PM – 6:35 AM"
    assert next_day["naps"] == []


def test_day_api_bedtime_fragment_merges_across_month_boundary(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-08-30T23:00:00-06:00",
            "2026-08-31T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
        # Month-boundary bedtime fragment: its continuation lives in the
        # NEXT month's shard, which the day view must also read.
        _row(
            SLEEP_TYPE,
            "2026-08-31T23:17:00-06:00",
            "2026-08-31T23:58:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
        _row(
            SLEEP_TYPE,
            "2026-09-01T00:15:00-06:00",
            "2026-09-01T06:40:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260910_120000", rows)

    day = env.client.get("/app/body/api/day/20260831").get_json()["sleep"]
    assert day["window"] == "11:00 PM – 6:30 AM"
    assert day["naps"] == []

    next_day = env.client.get("/app/body/api/day/20260901").get_json()["sleep"]
    assert next_day["window"] == "11:17 PM – 6:40 AM"
    assert next_day["naps"] == []


def test_recent_rail_sleep_matches_day_page_sleep(body_env):
    env = body_env()
    _seed_cross_midnight_sleep(env.journal)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]
    day = env.client.get("/app/body/api/day/20260701").get_json()

    # One canonical sleep number: the recent-days rail and the day page
    # answer with the same merged cross-midnight session.
    rail = {item["day"]: item for item in archive["recent_days"]}
    assert day["sleep"]["duration"] == "8h 10m"
    assert rail["20260701"]["sleep_duration"] == day["sleep"]["duration"]


# --- Day view: activity / steps ---------------------------------------------


def test_day_api_steps_total_only_with_single_source(body_env):
    env = body_env()
    rows = [
        _row(
            STEP_TYPE,
            "2026-07-10T08:00:00-06:00",
            value="5000",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-10T12:00:00-06:00",
            value="1412",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-11T08:00:00-06:00",
            value="3000",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-11T09:00:00-06:00",
            value="2000",
            unit="count",
            source="Synthetic Wrist",
        ),
    ]
    _seed_import(env.journal, "20260802_000000", rows)

    single = env.client.get("/app/body/api/day/20260710").get_json()
    steps = single["activity"]["steps"]
    assert steps["mode"] == "total"
    assert steps["total"] == 6412
    assert steps["total_label"] == "6,412"
    assert steps["source"] == "Synthetic Phone"

    multi = env.client.get("/app/body/api/day/20260711").get_json()
    steps = multi["activity"]["steps"]
    assert steps["mode"] == "samples"
    assert steps["samples"] == 2
    assert "total" not in steps


# --- Window API -------------------------------------------------------------


def test_window_api_returns_factual_window_aggregates(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-07-10T10:00:00-06:00",
            value="88",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-10T10:30:00-06:00",
            value="96",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            "HKQuantityTypeIdentifierHeartRate",
            "2026-07-10T10:05:00-06:00",
            value="62",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            "HKQuantityTypeIdentifierHeartRate",
            "2026-07-10T10:45:00-06:00",
            value="89",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            STEP_TYPE,
            "2026-07-10T10:15:00-06:00",
            value="200",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-10T10:45:00-06:00",
            value="212",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            "HKWorkoutActivityTypeWalking",
            "2026-07-10T10:20:00-06:00",
            "2026-07-10T11:20:00-06:00",
            source="Synthetic Watch",
            kind="workout",
            metadata={
                "duration": "60",
                "durationUnit": "min",
                "totalDistance": "4.2",
                "totalDistanceUnit": "mi",
                "totalDistanceType": ("HKQuantityTypeIdentifierDistanceWalkingRunning"),
                "totalEnergyBurned": "198.4",
                "totalEnergyBurnedUnit": "Cal",
                "totalEnergyBurnedType": ("HKQuantityTypeIdentifierActiveEnergyBurned"),
            },
        ),
    ]
    _seed_import(env.journal, "20260808_000000", rows)

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T10:00:00-06:00&to=2026-07-10T11:00:00-06:00"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["has_data"] is True
    assert payload["entry_total"] == 7
    assert payload["heart_rate"]["count"] == 2
    assert payload["heart_rate"]["min"] == 62.0
    assert payload["heart_rate"]["max"] == 89.0
    assert payload["glucose"]["delta_label"] == "88 → 96 mg/dL"
    assert payload["glucose"]["range_label"] == "88–96 mg/dL"
    assert [reading["value"] for reading in payload["glucose"]["readings"]] == [
        88.0,
        96.0,
    ]
    assert payload["steps"]["mode"] == "total"
    assert payload["steps"]["total"] == 412
    assert payload["workouts"][0]["name"] == "Walking"
    assert payload["workouts"][0]["overlap_label"] == "40m"
    assert payload["workouts"][0]["distance"]["label"] == "4.2 mi"
    assert payload["workouts"][0]["energy"]["label"] == "198 Cal"
    assert payload["workouts"][0]["metric_labels"] == ["4.2 mi", "198 Cal"]
    assert payload["workouts"][0]["metrics_label"] == "4.2 mi · 198 Cal"
    assert {event["kind"] for event in payload["events"]} == {"workout"}
    assert payload["events"][0]["metrics_label"] == "4.2 mi · 198 Cal"
    assert payload["sources"]["names"] == [
        "Synthetic Phone",
        "Synthetic Stelo",
        "Synthetic Watch",
    ]
    assert [family["name"] for family in payload["families"]] == [
        "Glucose",
        "Activity",
        "Heart",
    ]
    assert len(payload["hourly"]) == 1
    hour = payload["hourly"][0]
    assert hour["range_label"] == "10:00 AM – 11:00 AM"
    assert hour["entry_total"] == 7
    assert hour["glucose"]["range_label"] == "88–96 mg/dL"
    assert hour["heart_rate"]["label"] == "62–89 bpm"
    assert hour["events"][0]["metrics_label"] == "4.2 mi · 198 Cal"
    assert hour["steps"]["label"] == "412 steps"
    assert [event["label"] for event in hour["events"]] == ["Walking"]
    assert hour["summary"] == [
        "Walking",
        "Glucose 88–96 mg/dL",
        "HR 62–89 bpm",
        "412 steps",
    ]


def test_window_api_reads_cross_month_and_dedupes_overlapping_bundles(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-06-30T23:55:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-01T00:05:00-06:00",
            value="104",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260809_000000", rows)
    first_june = (
        env.journal / "imports" / "20260809_000000" / "normalized" / "2026-06.jsonl"
    )
    first_july = (
        env.journal / "imports" / "20260809_000000" / "normalized" / "2026-07.jsonl"
    )
    second_root = env.journal / "imports" / "20260809_010000" / "normalized"
    second_root.mkdir(parents=True)
    (second_root / "2026-06.jsonl").write_text(
        first_june.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (second_root / "2026-07.jsonl").write_text(
        first_july.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _write_json(
        env.journal / "imports" / "20260809_010000" / "manifest.json",
        {
            "import_id": "20260809_010000",
            "source_type": "apple_health",
            "source_hash": "sha256:overlap",
            "entry_count": 2,
            "days_affected": ["20260630", "20260701"],
            "files_created": [],
            "imported_at": "2026-08-09T01:00:00",
            "imported_via": "test",
        },
    )

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-06-30T23:45:00-06:00&to=2026-07-01T00:15:00-06:00"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["entry_total"] == 2
    assert payload["glucose"]["delta_label"] == "100 → 104 mg/dL"


def test_window_api_steps_do_not_total_multiple_sources(body_env):
    env = body_env()
    rows = [
        _row(
            STEP_TYPE,
            "2026-07-10T09:00:00-06:00",
            value="300",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-10T09:05:00-06:00",
            value="200",
            unit="count",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260810_000000", rows)

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T09:00:00-06:00&to=2026-07-10T10:00:00-06:00"
    )

    assert response.status_code == 200
    steps = response.get_json()["steps"]
    assert steps["mode"] == "samples"
    assert steps["samples"] == 2
    assert "total" not in steps


def test_window_api_preserves_full_day_as_hourly_context(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-07-10T00:05:00-06:00",
            value="88",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-10T13:15:00-06:00",
            value="101",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            "HKQuantityTypeIdentifierHeartRate",
            "2026-07-10T13:30:00-06:00",
            value="82",
            unit="count/min",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260810_010000", rows)

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T00:00:00-06:00&to=2026-07-11T00:00:00-06:00"
    )

    assert response.status_code == 200
    hourly = response.get_json()["hourly"]
    assert len(hourly) == 24
    assert hourly[0]["range_label"] == "12:00 AM – 1:00 AM"
    assert hourly[0]["glucose"]["range_label"] == "88 mg/dL"
    assert hourly[12]["has_data"] is False
    assert hourly[13]["range_label"] == "1:00 PM – 2:00 PM"
    assert hourly[13]["summary"] == [
        "Glucose 101 mg/dL",
        "HR 82 bpm",
    ]


def test_window_api_includes_sleep_events_from_previous_day(body_env):
    env = body_env()
    _seed_cross_midnight_sleep(env.journal)

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-01T00:00:00-06:00&to=2026-07-02T00:00:00-06:00"
    )

    assert response.status_code == 200
    events = response.get_json()["events"]
    sleep_events = [event for event in events if event["kind"] == "sleep"]
    assert sleep_events
    assert len(sleep_events) == 2
    assert [event["source"] for event in sleep_events] == [
        "Synthetic Ring",
        "Synthetic Ring",
    ]
    assert sleep_events[0]["start"] == "2026-06-30T22:58:00-06:00"
    assert sleep_events[0]["end"] == "2026-07-01T07:08:00-06:00"
    assert sleep_events[0]["overlap_label"] == "7h 08m"
    assert sleep_events[1]["start"] == "2026-07-01T14:00:00-06:00"
    assert sleep_events[1]["overlap_label"] == "45m"


def test_window_api_rejects_invalid_and_too_large_spans(body_env):
    env = body_env()

    missing = env.client.get("/app/body/api/window")
    assert missing.status_code == 400
    assert missing.get_json()["reason_code"] == "invalid_request_value"

    reversed_span = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T10:00:00-06:00&to=2026-07-10T09:00:00-06:00"
    )
    assert reversed_span.status_code == 400

    too_large = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-01T00:00:00-06:00&to=2026-07-09T00:00:00-06:00"
    )
    assert too_large.status_code == 400
    assert "7 days or less" in too_large.get_json()["detail"]


def test_window_api_empty_window_is_read_only(body_env):
    env = body_env()
    imports_root = env.journal / "imports"
    assert not imports_root.exists()

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T09:00:00-06:00&to=2026-07-10T10:00:00-06:00"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["has_data"] is False
    assert payload["entry_total"] == 0
    assert payload["families"] == []
    assert payload["events"] == []
    assert not imports_root.exists()


# --- Day view: glucose curve -------------------------------------------------


def test_day_api_glucose_curve_points_and_sparse_dots(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    payload = env.client.get("/app/body/api/day/20260703").get_json()

    series = payload["glucose_series"]
    assert len(series) == 1
    curve = series[0]
    assert curve["unit"] == "mg/dL"
    assert curve["count"] == 2
    # Server-computed [minute-of-day, value] pairs in the record's own offset.
    assert curve["points"] == [[480, 100.0], [720, 140.0]]
    # Readings four hours apart do not get a line drawn across the gap.
    assert curve["svg"]["paths"] == []
    assert len(curve["svg"]["dots"]) == 2


def test_day_api_glucose_curve_path_for_contiguous_readings(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-07-12T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-12T08:05:00-06:00",
            value="105",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-12T08:10:00-06:00",
            value="102",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260803_000000", rows)

    payload = env.client.get("/app/body/api/day/20260712").get_json()

    curve = payload["glucose_series"][0]
    assert curve["points"][0] == [480, 100.0]
    assert len(curve["svg"]["paths"]) == 1
    assert curve["svg"]["paths"][0].startswith("M480 ")
    assert curve["svg"]["dots"] == []


# --- Day view: empty days ----------------------------------------------------


def test_day_api_empty_day_links_nearest_days_with_data(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    before = env.client.get("/app/body/api/day/20260601").get_json()
    assert before["has_data"] is False
    assert before["entry_total"] == 0
    assert before["nearest"]["prev"] is None
    assert before["nearest"]["next"] == "20260703"

    after = env.client.get("/app/body/api/day/20260710").get_json()
    assert after["nearest"]["prev"] == "20260704"
    assert after["nearest"]["next"] is None


def test_day_api_empty_day_and_workspace_empty_state(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/day/20260601")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["has_data"] is False
    assert payload["nearest"]["next"] == "20260703"

    source = _function_source(_workspace_source(), "renderEmptyDay")
    assert "No body data present for this day." in source
    assert "bodyDayHref(nearest.next)" in source


# --- Day view: prompts hook --------------------------------------------------


def test_day_api_prompts_and_workspace_chat_hook(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    payload = env.client.get("/app/body/api/day/20260703").get_json()
    assert len(payload["prompts"]) == 3
    assert any("glucose peak" in prompt for prompt in payload["prompts"])

    source = _function_sources("renderPrompts", "bindPromptButtons")
    assert "ask sol about this day" in source
    assert "data-prompt=" in source
    assert "window.fillChat" in source


# --- Archive: day grid, families, rail ----------------------------------------


def _grid_cells(block: dict) -> list[dict]:
    return [cell for week in block["weeks"] for cell in week if cell is not None]


def test_status_api_day_grid_groups_years_and_log_scales(body_env):
    env = body_env()
    december = [
        _row(
            GLUCOSE_TYPE,
            f"2025-12-30T0{i}:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for i in range(2)
    ]
    july = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-03T0{i}:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for i in range(5)
    ]
    _seed_import(env.journal, "20260804_000000", december + july)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    grid = archive["day_grid"]
    assert [block["year"] for block in grid] == [2025, 2026]

    # Weeks are Monday-first columns of exactly seven weekday slots; the
    # first day with data sits at its own weekday row after None padding.
    for block in grid:
        assert all(len(week) == 7 for week in block["weeks"])
    assert grid[0]["weeks"][0][date(2025, 12, 30).weekday()]["day"] == "20251230"

    # The span runs the first through the last day with data, continuously
    # across the year-block boundary.
    first_cells = _grid_cells(grid[0])
    second_cells = _grid_cells(grid[1])
    assert first_cells[0]["day"] == "20251230"
    assert first_cells[-1]["day"] == "20251231"
    assert second_cells[0]["day"] == "20260101"
    assert second_cells[-1]["day"] == "20260703"
    assert len(second_cells) == (date(2026, 7, 3) - date(2026, 1, 1)).days + 1

    by_day = {cell["day"]: cell for cell in first_cells + second_cells}
    # Log scaling: monotone in count, busiest day pinned to 1.0, and above
    # what a linear ramp (2/5 = 0.4) would give.
    assert by_day["20260703"]["intensity"] == 1.0
    assert 0.0 < by_day["20251230"]["intensity"] < by_day["20260703"]["intensity"]
    assert by_day["20251230"]["intensity"] == pytest.approx(
        math.log1p(2) / math.log1p(5), abs=1e-3
    )
    # Days without data stay pale: zero count, zero intensity.
    assert by_day["20260102"]["count"] == 0
    assert by_day["20260102"]["intensity"] == 0.0
    assert by_day["20260703"]["title"] == "Jul 3, 2026 · 5 entries"
    assert by_day["20260102"]["title"] == "Jan 2, 2026 · no entries"


def test_status_api_day_grid_and_workspace_links_only_days_with_data(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-03-15T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-03T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260807_000000", rows)

    status = env.client.get("/app/body/api/status").get_json()
    cells = {
        cell["day"]: cell
        for block in status["archive"]["day_grid"]
        for cell in _grid_cells(block)
    }

    assert cells["20260315"]["count"] == 1
    assert cells["20260315"]["title"] == "Mar 15, 2026 · 1 entry"
    # A day inside the span with no entries renders pale and unlinked.
    assert cells["20260401"]["count"] == 0
    assert cells["20260401"]["title"] == "Apr 1, 2026 · no entries"

    source = _function_source(_workspace_source(), "renderDayGrid")
    assert 'href="${bodyDayHref(cell.day)}"' in source
    assert 'class="body-day-cell body-day-cell--empty"' in source
    assert 'aria-hidden="true"' in source


def test_dedupe_stats_include_per_type_time_ranges(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-03-10T23:00:00-06:00",
            "2026-03-11T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-01T23:00:00-06:00",
            "2026-07-02T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-03T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260805_000000", rows)

    stats = body_routes._read_health_dedupe_stats(env.journal)

    assert stats["type_ranges"][SLEEP_TYPE] == {
        "first": "2026-03-10T23:00:00-06:00",
        "last": "2026-07-01T23:00:00-06:00",
    }
    assert stats["type_ranges"][GLUCOSE_TYPE] == {
        "first": "2026-07-03T08:00:00-06:00",
        "last": "2026-07-03T08:00:00-06:00",
    }


def test_status_api_coverage_families_fold_type_ranges(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-03-10T23:00:00-06:00",
            "2026-03-11T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-01T23:00:00-06:00",
            "2026-07-02T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-03T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260806_000000", rows)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    families = {family["name"]: family for family in archive["families"]}
    assert families["Sleep"]["range_label"] == "Mar 2026 – Jul 2026"
    assert families["Glucose"]["range_label"] == "Jul 2026"
    # Friendly names, never raw identifiers, on the owner-facing chips.
    assert "Sleep" in families["Sleep"]["types_label"]
    assert "HK" not in families["Glucose"]["types_label"]


def test_status_api_recent_day_rail_has_per_day_facts(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    rail = archive["recent_days"]
    assert [item["day"] for item in rail] == ["20260704", "20260703"]
    # Both archive days fit in the initial rail — nothing older remains.
    assert archive["recent_days_has_more"] is False
    glucose_day = rail[1]
    assert glucose_day["glucose_label"] == "100–140 mg/dL · avg 120"
    assert glucose_day["workout_count"] == 0
    assert glucose_day["source_count"] == 2
    workout_day = rail[0]
    assert workout_day["workout_count"] == 1
    assert workout_day["sleep_duration"] is None


def test_status_api_recent_day_rail_caps_at_fourteen_days(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-{day:02d}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for day in range(1, 19)
    ]
    _seed_import(env.journal, "20260801_000000", rows)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]
    rail = archive["recent_days"]

    # 18 days with data collapse to the newest 14, newest first; the
    # archive flags that older days remain for the carousel to page in.
    assert len(rail) == 14
    assert [item["day"] for item in rail] == [
        f"202607{day:02d}" for day in range(18, 4, -1)
    ]
    assert archive["recent_days_has_more"] is True


def test_overview_recent_days_workspace_renders_snap_carousel():
    source = _workspace_source()

    # The rail is a horizontal scroll-snap carousel with fixed-width cards
    # and a thin scrollbar; the page itself never scrolls sideways.
    assert 'class="body-recent-carousel"' in source
    assert "scroll-snap-type: x mandatory" in source
    assert "overflow-x: auto" in source
    assert "scroll-snap-align: start" in source
    assert "scrollbar-width: thin" in source

    # Paging buttons: newest-first order puts newer days to the left, so
    # the labels follow content, not direction.
    assert 'aria-label="Newer days"' in source
    assert 'aria-label="Earlier days"' in source

    # Buttons disable at the respective end of the scroll range and the
    # control cluster hides entirely when every card fits without overflow.
    assert "backBtn.disabled" in source
    assert "fwdBtn.disabled" in source
    assert "controls.hidden = true" in source


def _seed_july_days(journal: Path, last_day: int) -> None:
    """One glucose entry per day for 2026-07-01 … 2026-07-``last_day``."""
    rows = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-{day:02d}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for day in range(1, last_day + 1)
    ]
    _seed_import(journal, "20260801_000000", rows)


def test_recent_api_pages_strictly_older_newest_first(body_env):
    env = body_env()
    _seed_july_days(env.journal, 18)

    response = env.client.get("/app/body/api/recent?before=20260715")

    assert response.status_code == 200
    batch = response.get_json()
    days = [item["day"] for item in batch["days"]]
    # Strictly older than the cursor, newest first, default batch of 14.
    assert days == [f"202607{day:02d}" for day in range(14, 0, -1)]
    assert all(day < "20260715" for day in days)
    # The batch reaches the archive's earliest day, so nothing remains.
    assert batch["has_more"] is False
    # Same per-card payload the overview's rail carries.
    assert batch["days"][0]["glucose_label"] == "100–100 mg/dL · avg 100"
    assert batch["days"][0]["source_count"] == 1


def test_recent_api_two_page_walk_covers_archive_without_duplicates(body_env):
    env = body_env()
    _seed_july_days(env.journal, 18)

    first = env.client.get("/app/body/api/recent?before=20260719").get_json()
    first_days = [item["day"] for item in first["days"]]
    assert first_days == [f"202607{day:02d}" for day in range(18, 4, -1)]
    assert first["has_more"] is True

    # The next cursor is the oldest card of the previous page.
    second = env.client.get(f"/app/body/api/recent?before={first_days[-1]}").get_json()
    second_days = [item["day"] for item in second["days"]]
    assert second_days == [f"202607{day:02d}" for day in range(4, 0, -1)]
    assert second["has_more"] is False

    assert set(first_days) & set(second_days) == set()
    assert sorted(first_days + second_days) == [
        f"202607{day:02d}" for day in range(1, 19)
    ]


def test_recent_api_limit_respected_and_capped(body_env):
    env = body_env()
    june = [
        _row(
            GLUCOSE_TYPE,
            f"2026-06-{day:02d}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for day in range(1, 31)
    ]
    july = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-{day:02d}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for day in range(1, 11)
    ]
    _seed_import(env.journal, "20260801_000000", june + july)

    small = env.client.get("/app/body/api/recent?before=20260711&limit=3").get_json()
    assert [item["day"] for item in small["days"]] == [
        "20260710",
        "20260709",
        "20260708",
    ]
    assert small["has_more"] is True

    # An oversized limit clamps to the batch cap instead of folding an
    # unbounded stretch of the archive into one response.
    large = env.client.get("/app/body/api/recent?before=20260711&limit=100").get_json()
    assert len(large["days"]) == body_routes.RECENT_BATCH_LIMIT_CAP
    assert large["days"][0]["day"] == "20260710"
    assert large["days"][-1]["day"] == "20260610"
    assert large["has_more"] is True


def test_recent_api_rejects_invalid_before_and_limit(body_env):
    env = body_env()

    assert env.client.get("/app/body/api/recent").status_code == 400
    assert env.client.get("/app/body/api/recent?before=2026-07-03").status_code == 400
    assert env.client.get("/app/body/api/recent?before=notaday1").status_code == 400
    assert env.client.get("/app/body/api/recent?before=20261399").status_code == 400
    assert (
        env.client.get("/app/body/api/recent?before=20260703&limit=abc").status_code
        == 400
    )
    assert (
        env.client.get("/app/body/api/recent?before=20260703&limit=0").status_code
        == 400
    )


def test_recent_api_skips_empty_days_across_month_boundary(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            f"{stamp}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for stamp in ("2026-06-28", "2026-06-30", "2026-07-02")
    ]
    _seed_import(env.journal, "20260801_000000", rows)

    first = env.client.get("/app/body/api/recent?before=20260704&limit=2").get_json()
    # Only days with entries appear: the gaps at 06-29 and 07-01 fold away
    # and the batch spans the June/July boundary.
    assert [item["day"] for item in first["days"]] == ["20260702", "20260630"]
    assert first["has_more"] is True

    second = env.client.get("/app/body/api/recent?before=20260630").get_json()
    assert [item["day"] for item in second["days"]] == ["20260628"]
    assert second["has_more"] is False


def test_recent_api_returns_payload_for_single_day_card_renderer(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    batch = env.client.get("/app/body/api/recent?before=20260704").get_json()

    assert set(batch) == {"days", "has_more"}
    assert [item["day"] for item in batch["days"]] == ["20260703"]
    assert batch["has_more"] is False

    workspace = _workspace_source()
    assert workspace.count("function renderDayCard") == 1
    assert 'days.map(renderDayCard).join("")' in workspace
    assert "renderDayCard(item)" in workspace
    routes_source = (BODY_ROOT / "routes.py").read_text(encoding="utf-8")
    assert '"html"' not in batch
    assert "get_template_attribute" not in routes_source


def test_overview_carousel_pages_archive_with_guarded_fetches(body_env):
    env = body_env()
    _seed_july_days(env.journal, 18)

    status = env.client.get("/app/body/api/status").get_json()
    source = _workspace_source()

    # The initial API payload stays the newest 14 cards and flags that
    # older days remain for the carousel to page in.
    assert len(status["archive"]["recent_days"]) == 14
    assert status["archive"]["recent_days_has_more"] is True
    assert (
        'data-has-more="${archive.recent_days_has_more ? "true" : "false"}"' in source
    )

    # Cursor-paged fetch of earlier days, triggered within ~2 card widths
    # of the right end, one request in flight at a time, deduped by day.
    assert "/app/body/api/recent?before=" in source
    assert "window.apiJson" in source
    assert "remaining > 2 * cardStep()" in source
    assert "if (!hasMore || fetching) return;" in source
    assert "present[card.dataset.day]" in source
    assert "appendBatchDays(batch.days)" in source
    assert "batch.html" not in source
    assert "template.innerHTML" not in source

    # A neutral placeholder card shows while a batch loads.
    assert "Loading earlier days" in source
    assert "body-recent-loading" in source

    # The forward control only hard-disables at the true archive end.
    assert (
        "fwdBtn.disabled = !hasMore && carousel.scrollLeft >= maxScroll - 1;" in source
    )
    assert "hasMore = !!batch.has_more;" in source


def test_status_api_and_workspace_cover_archive_sections(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]
    source = _workspace_source()

    assert archive["coverage"]["range_label"] == "Jul 2026 – Jul 2026"
    assert archive["recent_days"]
    assert archive["day_grid"]
    assert archive["families"]
    assert archive["sources"]
    assert "Body archive" in source
    assert "Recent body days" in source
    assert "Explore all history" in source
    assert "Coverage areas" in source
    assert "Sources represented" in source
    assert "body-day-cell" in source
    assert "months held" in source
    # Month labels above the grid and the ramp legend under it.
    assert "body-days-months" in source
    assert "more body data" in source


def test_status_api_archive_latest_day_and_month_labels(body_env):
    env = body_env()
    december = [
        _row(
            GLUCOSE_TYPE,
            f"2025-12-30T0{i}:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for i in range(2)
    ]
    july = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-03T0{i}:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for i in range(5)
    ]
    _seed_import(env.journal, "20260808_000000", december + july)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    assert archive["latest_day"] == "20260703"

    grid = archive["day_grid"]
    assert [block["year"] for block in grid] == [2025, 2026]
    # Each month labels the week column it first leads; a final month too
    # short to lead a column (July here, span ends Jul 3) gets no label.
    assert grid[0]["month_labels"] == [{"index": 0, "label": "Dec"}]
    labels = grid[1]["month_labels"]
    assert [item["label"] for item in labels] == [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
    ]
    indices = [item["index"] for item in labels]
    assert labels[0] == {"index": 0, "label": "Jan"}
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)
    assert all(0 <= index < len(grid[1]["weeks"]) for index in indices)


def test_overview_quick_entry_row_and_section_order(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]
    source = _workspace_source()

    # Quick-entry row: solid button to the latest day with data and trends.
    # No legacy jump calendar or "This week" button.
    assert archive["latest_day"] == "20260704"
    assert archive["coverage"]["start_month"] == "2026-07"
    assert archive["coverage"]["end_month"] == "2026-07"
    assert "Open latest day" in source
    assert "bodyDayHref(latestDay)" in source
    assert 'href="/app/body/trends"' in source
    assert "Jump to date" not in source
    assert "This week" not in source

    # Latest-first order: hero, quick entry, recent days, all history,
    # coverage/sources panels, audit drawer last.
    overview_source = _function_source(source, "renderOverview")
    order = [
        overview_source.index("renderArchiveHero"),
        overview_source.index("renderQuickActions"),
        overview_source.index("renderRecentDaysSection"),
        overview_source.index("renderDayGrid"),
        overview_source.index("renderCoverageAreas"),
        overview_source.index("renderSourcesRepresented"),
        overview_source.index("renderOverviewAudit"),
    ]
    assert order == sorted(order)


# --- Overview vs day-page navigation model --------------------------------------


def test_overview_mounts_dayless_date_nav_and_keeps_body_title(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/")
    source = _workspace_source()

    assert response.status_code == 200
    assert "data-date-nav" in source
    assert "data-date-nav-heading" in source
    assert (
        'renderHeader("body", "your health, brought into your journal.", false)'
        in source
    )


def test_valid_day_page_serves_shell_and_workspace_has_overview_backlink(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    html = env.client.get("/app/body/20260703").get_data(as_text=True)

    assert 'data-solstone-shell="spa"' in html
    assert "Day summary" not in html
    assert "Glucose 100–140 mg/dL." not in html
    source = _workspace_source()
    assert "Body overview" in source
    assert 'renderHeader("", "", true, { showTitle: false })' in source
    assert "data-date-nav-heading" in source


# --- Shared display normalizers ------------------------------------------------


def test_display_normalizers_scale_fraction_percents_and_relabel_units():
    # HK fraction-percent convention: 0–1 fractions with unit '%' scale by
    # 100 — blood oxygen must never render as "1.0 %".
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierOxygenSaturation", 0.98, "%"
        )
        == "98%"
    )
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierBodyFatPercentage", 0.223, "%"
        )
        == "22.3%"
    )
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierAppleWalkingSteadiness", 0.87, "%"
        )
        == "87%"
    )
    # AFib burden follows the same HealthKit percent-quantity convention.
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierAtrialFibrillationBurden", 0.021, "%"
        )
        == "2.1%"
    )
    # Heart-family 'count/min' reads as bpm; respiratory as breaths/min.
    for record_type in (
        "HKQuantityTypeIdentifierHeartRate",
        "HKQuantityTypeIdentifierRestingHeartRate",
        "HKQuantityTypeIdentifierWalkingHeartRateAverage",
        "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute",
    ):
        assert health_schema.friendly_unit_label(record_type, "count/min") == "bpm"
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierRespiratoryRate", "count/min"
        )
        == "breaths/min"
    )
    # '%' stays '%'; unknown units pass through; bare counts drop the unit.
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierOxygenSaturation", "%"
        )
        == "%"
    )
    assert (
        health_schema.friendly_unit_label("HKQuantityTypeIdentifierBodyMass", "lb")
        == "lb"
    )
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierFlightsClimbed", 23, "count"
        )
        == "23"
    )
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierRestingHeartRate", 52.0, "count/min"
        )
        == "52 bpm"
    )
    # Type-independent raw exporter units relabel for owners.
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierHeadphoneAudioExposure", "dBASPL"
        )
        == "dB"
    )
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierWalkingSpeed", "mi/hr"
        )
        == "mph"
    )
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierWalkingSpeed", "km/hr"
        )
        == "km/h"
    )
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierActiveEnergyBurned", "kcal"
        )
        == "Cal"
    )
    # Apple's native energy rows already say 'Cal'; it passes through.
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierActiveEnergyBurned", "Cal"
        )
        == "Cal"
    )
    # Oura API units: scores are unitless numbers ('82', never '82 score');
    # the temperature deviation reads in °C.
    assert health_schema.friendly_unit_label(OURA_READINESS_TYPE, "score") == ""
    assert health_schema.display_value(OURA_READINESS_TYPE, 82, "score") == "82"
    assert health_schema.friendly_unit_label(OURA_TEMP_DEV_TYPE, "degC") == "°C"
    # Oura's nightly SpO2 average arrives as whole percents — the HealthKit
    # 0–1 fraction-percent scaling must never touch it.
    assert health_schema.display_number(OURA_SPO2_TYPE, 97.4, "%") == "97.4"
    assert health_schema.display_value(OURA_SPO2_TYPE, 97.4, "%") == "97.4%"


# --- Day view: heart card ------------------------------------------------------


HR_TYPE = "HKQuantityTypeIdentifierHeartRate"
RESTING_HR_TYPE = "HKQuantityTypeIdentifierRestingHeartRate"
SPO2_TYPE = "HKQuantityTypeIdentifierOxygenSaturation"
RESP_TYPE = "HKQuantityTypeIdentifierRespiratoryRate"
HRV_TYPE = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
BP_SYS_TYPE = "HKQuantityTypeIdentifierBloodPressureSystolic"
BP_DIA_TYPE = "HKQuantityTypeIdentifierBloodPressureDiastolic"


def test_day_api_heart_card_ranges_and_friendly_units(body_env):
    env = body_env()
    rows = [
        _row(HR_TYPE, "2026-07-15T06:00:00-06:00", value="55", unit="count/min"),
        _row(HR_TYPE, "2026-07-15T12:00:00-06:00", value="72", unit="count/min"),
        _row(HR_TYPE, "2026-07-15T18:00:00-06:00", value="142", unit="count/min"),
        _row(
            RESTING_HR_TYPE, "2026-07-15T07:00:00-06:00", value="54", unit="count/min"
        ),
        _row(
            RESTING_HR_TYPE, "2026-07-15T22:00:00-06:00", value="52", unit="count/min"
        ),
        _row(SPO2_TYPE, "2026-07-15T03:00:00-06:00", value="0.97", unit="%"),
        _row(SPO2_TYPE, "2026-07-15T04:00:00-06:00", value="0.99", unit="%"),
        _row(RESP_TYPE, "2026-07-15T03:30:00-06:00", value="14.5", unit="count/min"),
        _row(RESP_TYPE, "2026-07-15T04:30:00-06:00", value="16", unit="count/min"),
        _row(HRV_TYPE, "2026-07-15T05:00:00-06:00", value="45", unit="ms"),
        _row(HRV_TYPE, "2026-07-15T06:30:00-06:00", value="60", unit="ms"),
    ]
    _seed_import(env.journal, "20260901_100000", rows)

    heart = env.client.get("/app/body/api/day/20260715").get_json()["heart"]

    # Heart rate: full-day range in bpm with its reading count.
    assert heart["heart_rate"]["summary"] == "55–142 bpm · 3 readings"
    assert heart["heart_rate"]["min"] == 55.0
    assert heart["heart_rate"]["max"] == 142.0
    facts = {fact["label"]: fact["value"] for fact in heart["facts"]}
    # Resting heart rate keeps its latest value even on multi-reading days.
    assert facts["Resting heart rate"] == "52 bpm"
    # Multi-reading signals summarize as min–max in friendly units.
    assert facts["Blood oxygen"] == "97–99%"
    assert facts["Respiratory rate"] == "14.5–16 breaths/min"
    assert facts["Heart rate variability"] == "45–60 ms"


def test_day_api_single_blood_oxygen_reading_renders_as_percent(body_env):
    env = body_env()
    rows = [_row(SPO2_TYPE, "2026-07-16T03:00:00-06:00", value="0.98", unit="%")]
    _seed_import(env.journal, "20260901_110000", rows)

    payload = env.client.get("/app/body/api/day/20260716").get_json()

    facts = {fact["label"]: fact["value"] for fact in payload["heart"]["facts"]}
    assert facts["Blood oxygen"] == "98%"

    heart_strings = "\n".join(_collect_strings(payload["heart"]))
    assert "98%" in heart_strings
    assert "1.0 %" not in heart_strings


def test_day_api_blood_pressure_pairs_by_start_time(body_env):
    env = body_env()
    rows = [
        _row(BP_SYS_TYPE, "2026-07-17T08:30:00-06:00", value="122", unit="mmHg"),
        _row(BP_DIA_TYPE, "2026-07-17T08:30:00-06:00", value="78", unit="mmHg"),
        _row(BP_SYS_TYPE, "2026-07-17T21:05:00-06:00", value="118", unit="mmHg"),
        _row(BP_DIA_TYPE, "2026-07-17T21:05:00-06:00", value="76", unit="mmHg"),
        # An unpaired systolic row never fabricates a reading.
        _row(BP_SYS_TYPE, "2026-07-17T12:00:00-06:00", value="130", unit="mmHg"),
    ]
    _seed_import(env.journal, "20260901_120000", rows)

    heart = env.client.get("/app/body/api/day/20260717").get_json()["heart"]

    bp = heart["blood_pressure"]
    assert bp["mode"] == "readings"
    assert bp["count"] == 2
    assert bp["readings"] == [
        {"time": "8:30 AM", "label": "122/78 mmHg"},
        {"time": "9:05 PM", "label": "118/76 mmHg"},
    ]
    # The paired card replaces the two count-only fact rows.
    labels = [fact["label"] for fact in heart["facts"]]
    assert "Blood pressure (systolic)" not in labels
    assert "Blood pressure (diastolic)" not in labels

    source = _function_source(_workspace_source(), "renderHeartCard")
    assert "Blood pressure" in source
    assert "bp.readings" in source


def test_day_api_blood_pressure_compresses_to_ranges_when_many_readings(body_env):
    env = body_env()
    rows = []
    for hour, (systolic, diastolic) in enumerate(
        [(110, 70), (112, 72), (115, 74), (120, 76), (124, 78), (126, 80), (128, 82)],
        start=8,
    ):
        start = f"2026-07-18T{hour:02d}:00:00-06:00"
        rows.append(_row(BP_SYS_TYPE, start, value=str(systolic), unit="mmHg"))
        rows.append(_row(BP_DIA_TYPE, start, value=str(diastolic), unit="mmHg"))
    _seed_import(env.journal, "20260901_130000", rows)

    bp = env.client.get("/app/body/api/day/20260718").get_json()["heart"][
        "blood_pressure"
    ]

    assert bp["mode"] == "range"
    assert bp["count"] == 7
    assert bp["readings"] == []
    assert bp["range_label"] == "systolic 110–128 mmHg · diastolic 70–82 mmHg"


# --- Day view: heart-rhythm events ---------------------------------------------
#
# Rhythm rows are the most sensitive display in the app. The card states
# what the device reported, attributed to the device — a count, a device-
# stated value — and nothing else: no interpretation, no alarm framing,
# no advice.


IRREGULAR_RHYTHM_TYPE = "HKCategoryTypeIdentifierIrregularHeartRhythmEvent"
HIGH_HR_EVENT_TYPE = "HKCategoryTypeIdentifierHighHeartRateEvent"
LOW_HR_EVENT_TYPE = "HKCategoryTypeIdentifierLowHeartRateEvent"
AFIB_BURDEN_TYPE = "HKQuantityTypeIdentifierAtrialFibrillationBurden"

# Advisory or interpretive phrasing that must never accompany a rhythm row.
# Checked over every server-produced string in the heart subtree and over
# the renderer function that contributes the card's literal copy.
RHYTHM_BANNED_PHRASES = (
    "atrial fibrillation",
    "detected",
    "warning",
    "alert",
    "abnormal",
    "normal",
    "risk",
    "urgent",
    "emergency",
    "danger",
    "consult",
    "advice",
    "recommend",
    "doctor",
    "seek ",
)


def test_day_api_rhythm_events_state_count_and_device_only(body_env):
    env = body_env()
    rows = [
        _row(
            IRREGULAR_RHYTHM_TYPE,
            "2026-09-02T09:15:00-06:00",
            value="HKCategoryValueNotApplicable",
            source="Synthetic Watch",
        ),
        _row(
            HIGH_HR_EVENT_TYPE,
            "2026-09-02T14:00:00-06:00",
            value="HKCategoryValueNotApplicable",
            source="Synthetic Watch",
            metadata={"HKMetadataKeyHeartRateEventThreshold": "120 count/min"},
        ),
        _row(
            HIGH_HR_EVENT_TYPE,
            "2026-09-02T18:30:00-06:00",
            value="HKCategoryValueNotApplicable",
            source="Synthetic Watch",
        ),
        _row(
            LOW_HR_EVENT_TYPE,
            "2026-09-02T04:00:00-06:00",
            value="HKCategoryValueNotApplicable",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260920_100000", rows)

    payload = env.client.get("/app/body/api/day/20260902").get_json()

    rhythm = payload["heart"]["rhythm"]
    # Exact factual lines: type · count · device attribution, nothing else.
    assert [event["line"] for event in rhythm["events"]] == [
        "High heart-rate notification · 2 events · reported by Synthetic Watch",
        "Irregular rhythm notification · 1 event · reported by Synthetic Watch",
        "Low heart-rate notification · 1 event · reported by Synthetic Watch",
    ]
    assert rhythm["burden"] is None
    # Rhythm rows never double-render as generic count facts.
    assert payload["heart"]["facts"] == []
    # The Other-signals catch-all no longer claims rhythm rows.
    assert payload["other_signals"] is None


def test_day_api_rhythm_subtree_pins_format_and_bans_advisory_phrasing(body_env):
    env = body_env()
    rows = [
        _row(
            IRREGULAR_RHYTHM_TYPE,
            "2026-09-02T09:15:00-06:00",
            value="HKCategoryValueNotApplicable",
            source="Synthetic Watch",
        ),
        _row(
            AFIB_BURDEN_TYPE,
            "2026-09-02T00:00:00-06:00",
            "2026-09-03T00:00:00-06:00",
            value="0.021",
            unit="%",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260920_110000", rows)

    payload = env.client.get("/app/body/api/day/20260902").get_json()
    rhythm = payload["heart"]["rhythm"]

    # Exact card line format: label cell, then the factual detail cell.
    assert rhythm["events"] == [
        {
            "label": "Irregular rhythm notification",
            "count": 1,
            "count_label": "1",
            "sources": ["Synthetic Watch"],
            "detail": "1 event · reported by Synthetic Watch",
            "line": "Irregular rhythm notification · 1 event · reported by Synthetic Watch",
        }
    ]
    assert rhythm["burden"] == {
        "label": "AFib burden",
        "value": "2.1%",
        "count": 1,
        "count_label": "1",
        "sources": ["Synthetic Watch"],
        "detail": "2.1% · reported by Synthetic Watch",
        "line": "AFib burden · 2.1% · reported by Synthetic Watch",
    }
    heart_strings = [text.lower() for text in _collect_strings(payload["heart"])]
    for phrase in RHYTHM_BANNED_PHRASES:
        assert all(phrase not in text for text in heart_strings), (
            f"advisory phrasing in heart payload: {phrase!r}"
        )


def test_workspace_heart_renderer_adds_no_rhythm_advisory_phrasing():
    source = _function_source(_workspace_source(), "renderHeartCard").lower()
    assert "heart.rhythm" in source
    assert "heart.rhythm.burden" in source
    for phrase in RHYTHM_BANNED_PHRASES:
        assert phrase not in source, f"advisory phrasing in heart renderer: {phrase!r}"


def test_day_api_afib_burden_scales_fraction_percent(body_env):
    env = body_env()
    rows = [
        _row(
            AFIB_BURDEN_TYPE,
            "2026-09-04T00:00:00-06:00",
            value="0.021",
            unit="%",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260920_120000", rows)

    payload = env.client.get("/app/body/api/day/20260904").get_json()

    burden = payload["heart"]["rhythm"]["burden"]
    assert burden["value"] == "2.1%"
    assert burden["line"] == "AFib burden · 2.1% · reported by Synthetic Watch"
    assert payload["heart"]["rhythm"]["events"] == []

    # The 0–1 fraction never reaches the page unscaled.
    assert "0.021" not in "\n".join(_collect_strings(payload["heart"]))


def test_day_api_multi_entry_afib_burden_leads_with_latest(body_env):
    env = body_env()
    rows = [
        _row(
            AFIB_BURDEN_TYPE,
            "2026-09-05T08:00:00-06:00",
            value="0.018",
            unit="%",
            source="Synthetic Watch",
        ),
        _row(
            AFIB_BURDEN_TYPE,
            "2026-09-05T20:00:00-06:00",
            value="0.021",
            unit="%",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260920_130000", rows)

    burden = env.client.get("/app/body/api/day/20260905").get_json()["heart"]["rhythm"][
        "burden"
    ]

    assert burden["line"] == (
        "AFib burden · latest 2.1% · 2 entries · reported by Synthetic Watch"
    )


def test_day_api_rhythm_rows_leave_other_signals_to_real_leftovers(body_env):
    env = body_env()
    rows = [
        _row(
            IRREGULAR_RHYTHM_TYPE,
            "2026-09-06T09:15:00-06:00",
            value="HKCategoryValueNotApplicable",
            source="Synthetic Watch",
        ),
        _row(
            "HKQuantityTypeIdentifierNumberOfTimesFallen",
            "2026-09-06T10:00:00-06:00",
            value="1",
            unit="count",
        ),
    ]
    _seed_import(env.journal, "20260920_140000", rows)

    payload = env.client.get("/app/body/api/day/20260906").get_json()

    other_labels = [fact["label"] for fact in payload["other_signals"]["facts"]]
    assert other_labels == ["Number of times fallen"]
    assert [event["label"] for event in payload["heart"]["rhythm"]["events"]] == [
        "Irregular rhythm notification"
    ]


def test_window_api_signals_carry_rhythm_rows_with_friendly_labels(body_env):
    env = body_env()
    rows = [
        _row(
            IRREGULAR_RHYTHM_TYPE,
            "2026-09-07T10:10:00-06:00",
            value="HKCategoryValueNotApplicable",
            source="Synthetic Watch",
        ),
        _row(
            AFIB_BURDEN_TYPE,
            "2026-09-07T10:20:00-06:00",
            value="0.021",
            unit="%",
            source="Synthetic Watch",
        ),
        _row(
            HR_TYPE,
            "2026-09-07T10:05:00-06:00",
            value="62",
            unit="count/min",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260920_150000", rows)

    payload = env.client.get(
        "/app/body/api/window"
        "?from=2026-09-07T10:00:00-06:00&to=2026-09-07T11:00:00-06:00"
    ).get_json()

    # Category-kind rhythm rows join the signals list with the same
    # factual labels the day card uses — never a raw identifier.
    assert payload["signals"] == [
        {"label": "AFib burden", "count": 1, "count_label": "1"},
        {"label": "Heart rate", "count": 1, "count_label": "1"},
        {"label": "Irregular rhythm notification", "count": 1, "count_label": "1"},
    ]
    assert [family["name"] for family in payload["families"]] == ["Heart"]


# --- Day view: heart-rate day curve -------------------------------------------


def _hr_rows(day: str, times_values: list[tuple[str, int]]) -> list[dict]:
    return [
        _row(
            HR_TYPE,
            f"{day}T{clock}:00-06:00",
            value=str(value),
            unit="count/min",
            source="Synthetic Watch",
        )
        for clock, value in times_values
    ]


def test_day_api_heart_rate_series_buckets_band_and_gap_split(body_env):
    env = body_env()
    # Two adjacent 5-minute buckets in the morning, then a >45-minute gap
    # to an isolated bucket: the curve must split and the tail render as
    # a dot.
    readings = [
        ("06:00", 60),
        ("06:01", 100),
        ("06:02", 62),
        ("06:03", 58),
        ("06:04", 90),
        ("06:05", 64),
        ("06:06", 66),
        ("06:07", 68),
        ("06:08", 70),
        ("06:09", 72),
        ("08:00", 80),
        ("08:01", 82),
        ("08:02", 84),
        ("08:03", 86),
        ("08:04", 88),
    ]
    _seed_import(env.journal, "20260910_000000", _hr_rows("2026-08-10", readings))

    payload = env.client.get("/app/body/api/day/20260810").get_json()

    series = payload["heart"]["series"]
    assert series is not None
    assert series["count"] == 15
    assert series["unit"] == "count/min"
    # Friendly unit only — the raw count/min never reaches the owner.
    assert series["unit_label"] == "bpm"
    assert series["bucket_minutes"] == 5
    # Bucket medians at bucket-center minutes-of-day.
    assert series["points"] == [
        [362.5, 62.0],
        [367.5, 68.0],
        [482.5, 84.0],
    ]
    # Per-bucket min–max carries the honest instantaneous variability.
    assert series["bands"] == [
        [362.5, 58.0, 100.0],
        [367.5, 64.0, 72.0],
        [482.5, 80.0, 88.0],
    ]
    # The 06:07→08:02 bucket gap (115 min) splits the curve: one median
    # path with its band polygon, plus the isolated bucket as a dot.
    assert len(series["svg"]["paths"]) == 1
    assert series["svg"]["paths"][0].startswith("M362.5 ")
    assert "L367.5 " in series["svg"]["paths"][0]
    assert len(series["svg"]["band_paths"]) == 1
    band = series["svg"]["band_paths"][0]
    assert band.startswith("M")
    assert band.endswith(" Z")
    # Closed polygon: maxima out, minima back — four points for two buckets.
    assert band.count("L") == 3
    assert len(series["svg"]["dots"]) == 1
    assert series["svg"]["dots"][0][0] == 482.5


def test_day_api_heart_rate_series_absent_below_threshold(body_env):
    env = body_env()
    readings = [(f"06:{5 * i:02d}", 60 + i) for i in range(11)]
    _seed_import(env.journal, "20260910_010000", _hr_rows("2026-08-11", readings))

    payload = env.client.get("/app/body/api/day/20260811").get_json()

    # Eleven readings are below the curve threshold: the payload keeps
    # the key additively but the card stays a text-only range row.
    heart = payload["heart"]
    assert heart["series"] is None
    assert heart["heart_rate"]["summary"] == "60–70 bpm · 11 readings"

    source = _function_source(_workspace_source(), "renderHeartCard")
    assert "heart.heart_rate" in source
    assert "hr ? html([" in source
    assert 'aria-label="Heart rate through the day"' in source
    assert 'class="body-curve-band"' in source


def test_day_api_heart_rate_revision_moves_across_month_shards(body_env):
    env = body_env()
    dedupe_key = "sha256:oura-heartrate-utc-boundary"
    source_record_id = "heartrate/2026-07-01T00:02:57.000Z/sleep"
    old = _oura_row(
        OURA_HEARTRATE_TYPE,
        "20260701",
        value=63,
        unit="bpm",
        start="2026-07-01T00:02:57+00:00",
        kind="sample",
        metadata={"source": "sleep", "raw_timestamp": source_record_id},
    )
    corrected = _oura_row(
        OURA_HEARTRATE_TYPE,
        "20260630",
        value=63,
        unit="bpm",
        start="2026-06-30T18:02:57-06:00",
        kind="sample",
        metadata={
            "source": "sleep",
            "raw_timestamp": "2026-07-01T00:02:57.000Z",
            "timezone": "America/Denver",
        },
    )
    for row in (old, corrected):
        row["dedupe_key"] = dedupe_key
        row["source_record_id"] = source_record_id

    _seed_import(env.journal, "20260701_000000", [old])
    _seed_import(env.journal, "20260706_000000", [corrected])

    june = env.client.get("/app/body/api/day/20260630").get_json()
    july = env.client.get("/app/body/api/day/20260701").get_json()

    assert june["heart"]["heart_rate"]["summary"] == "63 bpm · 1 reading"
    assert june["heart"]["heart_rate"]["count"] == 1
    assert july["heart"] is None


def test_day_api_heart_series_y_axis_labels_match_padded_domain(body_env):
    env = body_env()
    values = [55, 60, 70, 80, 90, 100, 110, 120, 125, 130, 135, 142]
    readings = [(f"06:{5 * i:02d}", value) for i, value in enumerate(values)]
    _seed_import(env.journal, "20260910_020000", _hr_rows("2026-08-12", readings))

    series = env.client.get("/app/body/api/day/20260812").get_json()["heart"]["series"]

    # Same axis convention as glucose: readings span 55–142, the domain
    # pads by 8% (6.96) and rounds outward to whole numbers the labels
    # state.
    assert series["min"] == 55.0
    assert series["max"] == 142.0
    assert series["svg"]["y_min_label"] == "48"
    assert series["svg"]["y_max_label"] == "149"


def test_day_api_and_workspace_render_heart_curve_under_range_row(body_env):
    env = body_env()
    readings = [(f"07:{i:02d}", 62 + (i % 7)) for i in range(20)]
    rows = _hr_rows("2026-08-13", readings) + [
        _row(
            RESP_TYPE,
            "2026-08-13T03:30:00-06:00",
            value="14.5",
            unit="count/min",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260910_030000", rows)

    # Range row, then the curve with its band, then the other facts.
    heart = env.client.get("/app/body/api/day/20260813").get_json()["heart"]
    assert heart["heart_rate"]["summary"] == "62–68 bpm · 20 readings"
    assert heart["series"] is not None
    assert {fact["label"] for fact in heart["facts"]} == {"Respiratory rate"}
    displayed = "\n".join(_collect_strings_except_keys(heart, {"unit"}))
    assert "count/min" not in displayed

    source = _function_source(_workspace_source(), "renderHeartCard")
    curve_at = source.index('aria-label="Heart rate through the day"')
    assert source.index("heart.heart_rate") < curve_at
    assert curve_at < source.index("facts.length")
    assert 'class="body-curve-band"' in source


def test_day_api_payload_keys_stay_additive_with_heart_series(body_env):
    env = body_env()
    readings = [(f"09:{i:02d}", 70 + i) for i in range(12)]
    rows = _hr_rows("2026-08-14", readings) + [
        _row(
            GLUCOSE_TYPE,
            "2026-08-14T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-08-14T08:05:00-06:00",
            value="105",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260910_040000", rows)

    payload = env.client.get("/app/body/api/day/20260814").get_json()

    # Existing top-level keys stay intact alongside the new series.
    assert {
        "day",
        "date_label",
        "summary_markdown",
        "glucose",
        "entry_total",
        "has_data",
        "lede",
        "sleep",
        "glucose_series",
        "activity",
        "heart",
        "recovery",
        "mind_sound",
        "walking",
        "body_measurements",
        "other_signals",
        "sources",
        "prompts",
        "audit",
        "nearest",
    } <= set(payload)
    assert {"heart_rate", "series", "blood_pressure", "rhythm", "facts"} <= set(
        payload["heart"]
    )
    # The glucose curve payload keeps its shape — the band is HR-only.
    glucose_svg = payload["glucose_series"][0]["svg"]
    assert {"width", "height", "paths", "dots", "y_min_label", "y_max_label"} <= set(
        glucose_svg
    )
    assert "band_paths" not in glucose_svg
    series_svg = payload["heart"]["series"]["svg"]
    assert {"paths", "band_paths", "dots", "y_min_label", "y_max_label"} <= set(
        series_svg
    )


# --- Day view: steps primary source ---------------------------------------------


def test_day_api_steps_pick_primary_source_by_coverage(body_env):
    env = body_env()
    rows = [
        _row(
            STEP_TYPE,
            "2026-07-19T08:00:00-06:00",
            "2026-07-19T09:00:00-06:00",
            value="2000",
            unit="count",
            source="Synthetic Ring",
        ),
        _row(
            STEP_TYPE,
            "2026-07-19T10:00:00-06:00",
            "2026-07-19T11:00:00-06:00",
            value="3000",
            unit="count",
            source="Synthetic Ring",
        ),
        _row(
            STEP_TYPE,
            "2026-07-19T12:00:00-06:00",
            "2026-07-19T13:00:00-06:00",
            value="1412",
            unit="count",
            source="Synthetic Ring",
        ),
        _row(
            STEP_TYPE,
            "2026-07-19T08:10:00-06:00",
            "2026-07-19T08:20:00-06:00",
            value="800",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-19T12:10:00-06:00",
            "2026-07-19T12:20:00-06:00",
            value="400",
            unit="count",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260901_140000", rows)

    steps = env.client.get("/app/body/api/day/20260719").get_json()["activity"]["steps"]

    # The largest-coverage source's total wins; the other source is only
    # named, never summed into the figure.
    assert steps["mode"] == "total"
    assert steps["total"] == 6412
    assert steps["total_label"] == "6,412"
    assert steps["source"] == "Synthetic Ring"
    assert steps["others"] == ["Synthetic Phone"]
    assert steps["others_label"] == "Synthetic Phone also contributed"

    source = _function_source(_workspace_source(), "renderActivityCard")
    assert "activity.steps.total_label" in source
    assert "activity.steps.others_label" in source


# --- Day view: asleep vs in-bed --------------------------------------------------


def test_day_api_sleep_splits_asleep_from_in_bed_when_stages_exist(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-07-20T22:58:00-06:00",
            "2026-07-21T02:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-21T02:00:00-06:00",
            "2026-07-21T02:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAwake",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-21T02:30:00-06:00",
            "2026-07-21T07:08:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepDeep",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(env.journal, "20260901_150000", rows)

    payload = env.client.get("/app/body/api/day/20260721").get_json()

    sleep = payload["sleep"]
    assert sleep["has_stage_detail"] is True
    # The awake gap stays inside the merged in-bed span but out of asleep.
    assert sleep["in_bed_duration"] == "8h 10m"
    assert sleep["asleep_duration"] == "7h 40m"
    # ``duration`` keeps the merged-span figure the importer's day card
    # states, so the two surfaces stay on one number.
    assert sleep["duration"] == "8h 10m"
    assert payload["lede"].startswith("Slept 7h 40m (in bed 8h 10m)")

    source = _function_source(_workspace_source(), "renderSleepCard")
    assert "asleep" in source
    assert "in bed" in source

    rail = {
        item["day"]: item
        for item in env.client.get("/app/body/api/status").get_json()["archive"][
            "recent_days"
        ]
    }
    assert rail["20260721"]["sleep_duration"] == "7h 40m"
    assert rail["20260721"]["sleep_in_bed"] == "8h 10m"


def test_day_api_sleep_without_stage_detail_keeps_single_figure(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-07-22T23:00:00-06:00",
            "2026-07-23T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Wrist",
        ),
    ]
    _seed_import(env.journal, "20260901_160000", rows)

    sleep = env.client.get("/app/body/api/day/20260723").get_json()["sleep"]

    # All rows in-bed/unspecified: asleep falls back to the merged span
    # and the card keeps the single pre-stage figure.
    assert sleep["has_stage_detail"] is False
    assert sleep["asleep_duration"] is None
    assert sleep["in_bed_duration"] == "7h 30m"
    assert sleep["duration"] == "7h 30m"


def test_day_api_oura_sleep_uses_stage_durations_for_asleep_time(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260901_163000",
        [
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                "20260705",
                value=None,
                unit="s",
                start="2026-07-04T22:15:00-06:00",
                end="2026-07-05T08:41:00-06:00",
                kind="sleep_period",
                metadata={
                    "type": "long_sleep",
                    "deep_sleep_duration": 5400,
                    "light_sleep_duration": 17400,
                    "rem_sleep_duration": 6000,
                    "awake_time": 7560,
                    "time_in_bed": 37560,
                },
            )
        ],
    )

    payload = env.client.get("/app/body/api/day/20260705").get_json()
    sleep = payload["sleep"]

    assert sleep["source"] == "Oura (API)"
    assert sleep["window"] == "10:15 PM – 8:41 AM"
    assert sleep["has_stage_detail"] is True
    assert sleep["asleep_duration"] == "8h 00m"
    assert sleep["in_bed_duration"] == "10h 26m"
    assert sleep["duration"] == "10h 26m"
    assert payload["lede"].startswith("Slept 8h 00m (in bed 10h 26m)")

    source = _function_source(_workspace_source(), "renderSleepCard")
    assert (
        'asleep <span class="body-num">${escapeHtml(sleep.asleep_duration)}</span>'
        in source
    )
    assert (
        'in bed <span class="body-num">${escapeHtml(sleep.in_bed_duration)}</span>'
        in source
    )

    trends = _trends_after_warm(env.client)
    asleep = next(s for s in trends["signals"] if s["key"] == "asleep_minutes")
    assert asleep["daily"] == [["20260705", 480.0]]


# --- Day view: workout aggregation ------------------------------------------------


def test_day_api_workout_summary_aggregates_kinds(body_env):
    env = body_env()
    cycling = "HKWorkoutActivityTypeCycling"
    walking = "HKWorkoutActivityTypeWalking"
    rows = [
        _row(cycling, "2026-07-25T06:00:00-06:00", source="S", kind="workout"),
        _row(cycling, "2026-07-25T17:00:00-06:00", source="S", kind="workout"),
        _row(walking, "2026-07-25T12:00:00-06:00", source="S", kind="workout"),
    ]
    more = [
        _row(cycling, "2026-07-26T06:00:00-06:00", source="S", kind="workout"),
        _row(cycling, "2026-07-26T17:00:00-06:00", source="S", kind="workout"),
        _row(
            "HKWorkoutActivityTypeHiking",
            "2026-07-26T08:00:00-06:00",
            source="S",
            kind="workout",
        ),
        _row(
            "HKWorkoutActivityTypeRunning",
            "2026-07-26T10:00:00-06:00",
            source="S",
            kind="workout",
        ),
        _row(walking, "2026-07-26T12:00:00-06:00", source="S", kind="workout"),
    ]
    _seed_import(env.journal, "20260901_170000", rows + more)

    day_one = env.client.get("/app/body/api/day/20260725").get_json()["activity"]
    day_two = env.client.get("/app/body/api/day/20260726").get_json()["activity"]

    assert day_one["workout_summary"] == "Cycling ×2 · Walking"
    # More than two kinds compress to "+N more"; the detail list keeps all.
    assert day_two["workout_summary"] == "Cycling ×2 · Hiking +2 more"
    assert len(day_two["workouts"]) == 5

    source = _function_source(_workspace_source(), "renderDayHighlights")
    assert "bodyDay.activity.workout_summary" in source


def test_day_api_workouts_show_recovered_distance_and_energy(body_env):
    env = body_env()
    rows = [
        _row(
            "HKWorkoutActivityTypeCycling",
            "2026-07-27T07:00:00-06:00",
            "2026-07-27T07:45:00-06:00",
            source="Synthetic Watch",
            kind="workout",
            metadata={
                "duration": "45",
                "durationUnit": "min",
                "totalDistance": "12.4",
                "totalDistanceUnit": "km",
                "totalDistanceType": "HKQuantityTypeIdentifierDistanceCycling",
                "totalEnergyBurned": "321.5",
                "totalEnergyBurnedUnit": "Cal",
                "totalEnergyBurnedType": ("HKQuantityTypeIdentifierActiveEnergyBurned"),
            },
        ),
        _row(
            "HKWorkoutActivityTypeWalking",
            "2026-07-27T18:00:00-06:00",
            "2026-07-27T18:20:00-06:00",
            source="Synthetic Watch",
            kind="workout",
        ),
    ]
    _seed_import(env.journal, "20260901_171500", rows)

    workouts = env.client.get("/app/body/api/day/20260727").get_json()["activity"][
        "workouts"
    ]

    assert workouts[0]["name"] == "Cycling"
    assert workouts[0]["distance"]["label"] == "12.4 km"
    assert workouts[0]["energy"]["label"] == "322 Cal"
    assert workouts[0]["metric_labels"] == ["12.4 km", "322 Cal"]
    assert workouts[0]["metrics_label"] == "12.4 km · 322 Cal"
    assert workouts[1]["distance"] is None
    assert workouts[1]["energy"] is None
    assert workouts[1]["metric_labels"] == []
    assert workouts[1]["metrics_label"] is None

    source = _function_source(_workspace_source(), "renderActivityCard")
    assert "workout.start" in source
    assert "workout.duration" in source
    assert "workout.metrics_label" in source
    assert "None" not in "\n".join(_collect_strings({"workouts": workouts}))


# --- Day view: running dynamics ---------------------------------------------------


def test_day_api_running_dynamics_summarize_instead_of_counting(body_env):
    env = body_env()
    power = "HKQuantityTypeIdentifierRunningPower"
    speed = "HKQuantityTypeIdentifierRunningSpeed"
    contact = "HKQuantityTypeIdentifierRunningGroundContactTime"
    osc = "HKQuantityTypeIdentifierRunningVerticalOscillation"
    rows = [
        _row(power, "2026-07-30T06:00:00-06:00", value="240", unit="W"),
        _row(power, "2026-07-30T06:01:00-06:00", value="250", unit="W"),
        _row(power, "2026-07-30T06:02:00-06:00", value="260", unit="W"),
        _row(speed, "2026-07-30T06:00:00-06:00", value="2.5", unit="m/s"),
        _row(speed, "2026-07-30T06:01:00-06:00", value="3.125", unit="m/s"),
        _row(contact, "2026-07-30T06:00:00-06:00", value="240", unit="ms"),
        _row(osc, "2026-07-30T06:00:00-06:00", value="8.1", unit="cm"),
        _row(osc, "2026-07-30T06:01:00-06:00", value="9.3", unit="cm"),
    ]
    _seed_import(env.journal, "20260901_180000", rows)

    activity = env.client.get("/app/body/api/day/20260730").get_json()["activity"]

    running = {item["label"]: item["summary"] for item in activity["running"]}
    assert running["Running power"] == "240–260 W · avg 250"
    # Speed converts to pace when the unit permits; fastest pace first.
    assert running["Running speed"] == "5:20–6:40 /km · avg 5:56 /km"
    assert running["Running ground contact time"] == "240 ms"
    assert running["Running vertical oscillation"] == "8.1–9.3 cm · avg 8.7"
    # Running dynamics leave the raw entry-count list.
    counter_labels = [item["label"] for item in activity["counters"]]
    assert "Running power" not in counter_labels

    source = _function_source(_workspace_source(), "renderActivityCard")
    assert "Running dynamics" in source
    assert "item.summary" in source


# --- Day view: summable single-source totals --------------------------------------


def test_day_api_summable_quantities_total_when_single_source(body_env):
    env = body_env()
    flights = "HKQuantityTypeIdentifierFlightsClimbed"
    exercise = "HKQuantityTypeIdentifierAppleExerciseTime"
    daylight = "HKQuantityTypeIdentifierTimeInDaylight"
    mindful = "HKCategoryTypeIdentifierMindfulSession"
    rows = [
        _row(flights, "2026-07-31T08:00:00-06:00", value="9", unit="count"),
        _row(flights, "2026-07-31T15:00:00-06:00", value="14", unit="count"),
        _row(exercise, "2026-07-31T08:00:00-06:00", value="22", unit="min"),
        _row(exercise, "2026-07-31T18:00:00-06:00", value="20", unit="min"),
        _row(daylight, "2026-07-31T12:00:00-06:00", value="38", unit="min"),
        _row(
            mindful,
            "2026-07-31T07:00:00-06:00",
            "2026-07-31T07:10:00-06:00",
            source="Synthetic Phone",
        ),
        _row(
            mindful,
            "2026-07-31T21:00:00-06:00",
            "2026-07-31T21:05:00-06:00",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260901_190000", rows)

    payload = env.client.get("/app/body/api/day/20260731").get_json()

    counters = {
        item["label"]: item["value"] for item in payload["activity"]["counters"]
    }
    assert counters["Flights climbed"] == "23"
    assert counters["Exercise minutes"] == "42 min"
    assert counters["Time in daylight"] == "38 min"
    mind_facts = {
        fact["label"]: fact["value"] for fact in payload["mind_sound"]["facts"]
    }
    # Mindful sessions sum to minutes of session time.
    assert mind_facts["Mindful sessions"] == "15m"


# --- Day view: activity value parity ---------------------------------------------


ACTIVE_ENERGY_TYPE = "HKQuantityTypeIdentifierActiveEnergyBurned"
DISTANCE_TYPE = "HKQuantityTypeIdentifierDistanceWalkingRunning"
STAND_HOUR_TYPE = "HKCategoryTypeIdentifierAppleStandHour"
WALK_SPEED_TYPE = "HKQuantityTypeIdentifierWalkingSpeed"
STEP_LENGTH_TYPE = "HKQuantityTypeIdentifierWalkingStepLength"
DOUBLE_SUPPORT_TYPE = "HKQuantityTypeIdentifierWalkingDoubleSupportPercentage"
ASYMMETRY_TYPE = "HKQuantityTypeIdentifierWalkingAsymmetryPercentage"


def test_day_api_energy_and_distance_pick_primary_source_totals(body_env):
    env = body_env()
    rows = [
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-20T08:00:00-06:00",
            "2026-08-20T09:00:00-06:00",
            value="300.5",
            unit="Cal",
            source="Synthetic Ring",
        ),
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-20T10:00:00-06:00",
            "2026-08-20T11:00:00-06:00",
            value="311.6",
            unit="Cal",
            source="Synthetic Ring",
        ),
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-20T08:05:00-06:00",
            "2026-08-20T08:10:00-06:00",
            value="120",
            unit="Cal",
            source="Synthetic Phone",
        ),
        _row(
            DISTANCE_TYPE,
            "2026-08-20T08:00:00-06:00",
            "2026-08-20T09:00:00-06:00",
            value="2.13",
            unit="mi",
            source="Synthetic Ring",
        ),
        _row(
            DISTANCE_TYPE,
            "2026-08-20T10:00:00-06:00",
            "2026-08-20T11:00:00-06:00",
            value="2.11",
            unit="mi",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(env.journal, "20260910_060000", rows)

    payload = env.client.get("/app/body/api/day/20260820").get_json()
    counters = {
        item["label"]: item["value"] for item in payload["activity"]["counters"]
    }

    # The largest-coverage source's sum wins; the other source is only
    # named, never summed into the figure. Energy reads as whole Cal.
    assert (
        counters["Active energy"]
        == "612 Cal · Synthetic Ring — Synthetic Phone also contributed"
    )
    # Distance totals keep one decimal in the rows' own unit.
    assert counters["Walking + running distance"] == "4.2 mi"


def test_day_api_zero_summable_totals_fall_back_to_entry_counts(body_env):
    env = body_env()
    rows = [
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-22T08:00:00-06:00",
            "2026-08-22T08:05:00-06:00",
            value="0",
            unit="Cal",
            source="Synthetic Phone",
        ),
        _row(
            DISTANCE_TYPE,
            "2026-08-22T08:00:00-06:00",
            "2026-08-22T08:05:00-06:00",
            value="0.04",
            unit="mi",
            source="Synthetic Phone",
        ),
        _row(
            "HKQuantityTypeIdentifierFlightsClimbed",
            "2026-08-22T09:00:00-06:00",
            value="3",
            unit="count",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260910_130000", rows)

    payload = env.client.get("/app/body/api/day/20260822").get_json()
    counters = {item["label"]: item for item in payload["activity"]["counters"]}

    # A zero-reading day must not manufacture a '0 Cal' health fact — the
    # card falls back to the factual entry count.
    assert counters["Active energy"]["value"] is None
    assert counters["Active energy"]["count"] == 1
    # A distance summing to 0.0 at display precision falls back the same way.
    assert counters["Walking + running distance"]["value"] is None
    # A real total still reads as its value.
    assert counters["Flights climbed"]["value"] == "3"

    activity_strings = "\n".join(_collect_strings(payload["activity"]))
    assert "0 Cal" not in activity_strings
    assert "0.0 mi" not in activity_strings
    # The count fallback pluralizes honestly: one row reads '1 entry'.
    assert counters["Active energy"]["count_label"] == "1"


def test_day_api_stand_hours_count_distinct_stood_hours(body_env):
    env = body_env()
    rows = [
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T08:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
            source="Synthetic Watch",
        ),
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T09:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
            source="Synthetic Watch",
        ),
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T10:00:00-06:00",
            value="HKCategoryValueAppleStandHourIdle",
            source="Synthetic Watch",
        ),
        # The phone repeating an hour the watch already stood must not
        # double-count; a phone-only stood hour still counts.
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T09:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
            source="Synthetic Phone",
        ),
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T14:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260910_070000", rows)

    counters = {
        item["label"]: item["value"]
        for item in env.client.get("/app/body/api/day/20260821").get_json()["activity"][
            "counters"
        ]
    }

    # Distinct stood hours: 8 AM, 9 AM (once), 2 PM. Idle hours are not
    # stand hours; the label stays the factual count, no goal framing.
    assert counters["Stand hours"] == "3"


# --- Day view: walking metric value summaries --------------------------------------


def test_day_api_walking_metrics_summarize_values(body_env):
    env = body_env()
    rows = [
        _row(WALK_SPEED_TYPE, "2026-08-22T08:00:00-06:00", value="2.1", unit="mi/hr"),
        _row(WALK_SPEED_TYPE, "2026-08-22T12:00:00-06:00", value="3.4", unit="mi/hr"),
        _row(WALK_SPEED_TYPE, "2026-08-22T16:00:00-06:00", value="2.9", unit="mi/hr"),
        _row(STEP_LENGTH_TYPE, "2026-08-22T08:00:00-06:00", value="27.2", unit="in"),
        _row(STEP_LENGTH_TYPE, "2026-08-22T12:00:00-06:00", value="28.4", unit="in"),
        _row(DOUBLE_SUPPORT_TYPE, "2026-08-22T08:00:00-06:00", value="0.281", unit="%"),
        _row(DOUBLE_SUPPORT_TYPE, "2026-08-22T12:00:00-06:00", value="0.285", unit="%"),
        _row(ASYMMETRY_TYPE, "2026-08-22T08:00:00-06:00", value="0.02", unit="%"),
        _row(ASYMMETRY_TYPE, "2026-08-22T12:00:00-06:00", value="0.04", unit="%"),
    ]
    _seed_import(env.journal, "20260910_080000", rows)

    payload = env.client.get("/app/body/api/day/20260822").get_json()
    walking = {item["label"]: item for item in payload["walking"]["facts"]}

    # Speed: min–max plus average in the friendly mph label.
    assert walking["Walking speed"]["summary"] == "2.1–3.4 mph · avg 2.8"
    assert walking["Walking speed"]["count_label"] == "3"
    # Step length: average with its unit.
    assert walking["Walking step length"]["summary"] == "avg 27.8 in"
    # Fraction-percent rows scale through the shared normalizers —
    # 0.283 renders as 28.3%, never "0.3 %".
    assert walking["Walking double support percentage"]["summary"] == "avg 28.3%"
    assert walking["Walking asymmetry percentage"]["summary"] == "avg 3%"

    source = _function_sources("renderSecondaryLists", "renderSimpleFactSection")
    assert "Walking metrics" in source
    assert "fact.summary" in source
    # Entry counts stay secondary next to the value.
    assert walking["Walking speed"]["count_label"] == "3"


# --- Day view: source chip counts ---------------------------------------------------


def test_day_api_source_chips_carry_entry_counts(body_env):
    env = body_env()
    rows = [
        _row(
            HR_TYPE,
            "2026-08-23T08:00:00-06:00",
            value="70",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            HR_TYPE,
            "2026-08-23T09:00:00-06:00",
            value="80",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-08-23T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260910_090000", rows)

    sources = env.client.get("/app/body/api/day/20260823").get_json()["sources"]

    chips = {chip["name"]: chip for chip in sources["chips"]}
    assert chips["Synthetic Watch"]["count"] == 2
    assert chips["Synthetic Watch"]["entries_label"] == "2 entries"
    assert chips["Synthetic Stelo"]["entries_label"] == "1 entry"
    # The footer total is unchanged alongside the per-chip counts.
    assert sources["entry_total_label"] == "3"

    source = _function_source(_workspace_source(), "renderSourcesThisDay")
    assert "entries_label" in source
    assert "entries observed" in source
    # The sources highlight pluralizes by count.
    highlight_source = _function_source(_workspace_source(), "renderDayHighlights")
    assert 'source${sourceNames.length === 1 ? "" : "s"}' in highlight_source


def test_day_api_single_source_highlight_reads_singular(body_env):
    env = body_env()
    rows = [
        _row(
            HR_TYPE,
            "2026-08-25T08:00:00-06:00",
            value="70",
            unit="count/min",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260910_150000", rows)

    payload = env.client.get("/app/body/api/day/20260825").get_json()
    assert payload["sources"]["names"] == ["Synthetic Watch"]
    source = _function_source(_workspace_source(), "renderDayHighlights")
    assert 'source${sourceNames.length === 1 ? "" : "s"}' in source


# --- Day view: raw units never reach the page ---------------------------------------


def test_day_api_owner_facing_payload_carries_no_raw_unit_strings(body_env):
    env = body_env()
    rows = [
        _row(HR_TYPE, "2026-08-24T09:00:00-06:00", value="70", unit="count/min"),
        _row(HR_TYPE, "2026-08-24T12:00:00-06:00", value="88", unit="count/min"),
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-24T10:00:00-06:00",
            value="52.1",
            unit="dBASPL",
        ),
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-24T11:00:00-06:00",
            value="63.9",
            unit="dBASPL",
        ),
        _row(WALK_SPEED_TYPE, "2026-08-24T08:00:00-06:00", value="2.5", unit="mi/hr"),
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-24T08:00:00-06:00",
            "2026-08-24T09:00:00-06:00",
            value="512.4",
            unit="Cal",
        ),
        _row(
            "HKQuantityTypeIdentifierAppleExerciseTime",
            "2026-08-24T08:00:00-06:00",
            value="22",
            unit="min",
        ),
        _row(
            STAND_HOUR_TYPE,
            "2026-08-24T08:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
        ),
    ]
    _seed_import(env.journal, "20260910_100000", rows)

    payload = env.client.get("/app/body/api/day/20260824").get_json()
    owner_payload = {
        "heart": payload["heart"],
        "activity": payload["activity"],
        "mind_sound": payload["mind_sound"],
        "walking": payload["walking"],
    }
    owner_strings = "\n".join(_collect_strings_except_keys(owner_payload, {"unit"}))

    # Raw exporter unit strings never reach the page — the shared
    # normalizers relabel them ('bpm', 'dB', 'mph', 'Cal').
    for raw in ("count/min", "dBASPL", "kcal", "mi/hr"):
        assert raw not in owner_strings, f"raw unit string leaked into payload: {raw}"
    assert "bpm" in owner_strings
    assert "dB" in owner_strings
    assert "mph" in owner_strings
    assert "512 Cal" in owner_strings


# --- Day view: workout ordering and sub-minute durations -----------------------------


def test_day_api_workouts_sort_by_start_and_never_render_zero_minutes(body_env):
    env = body_env()
    rows = [
        _row(
            "HKWorkoutActivityTypeWalking",
            "2026-08-25T13:05:00-06:00",
            "2026-08-25T13:35:00-06:00",
            source="S",
            kind="workout",
        ),
        _row(
            "HKWorkoutActivityTypeRunning",
            "2026-08-25T00:03:00-06:00",
            "2026-08-25T00:03:20-06:00",
            source="S",
            kind="workout",
        ),
        _row(
            "HKWorkoutActivityTypeYoga",
            "2026-08-25T06:00:00-06:00",
            "2026-08-25T06:00:00-06:00",
            source="S",
            kind="workout",
        ),
    ]
    _seed_import(env.journal, "20260910_110000", rows)

    workouts = env.client.get("/app/body/api/day/20260825").get_json()["activity"][
        "workouts"
    ]

    # Strict start-time order: the after-midnight workout leads the list.
    assert [w["start"] for w in workouts] == ["12:03 AM", "6:00 AM", "1:05 PM"]
    # A 20-second workout reads '<1m', never '0m'; a zero-length row
    # carries no duration at all.
    assert workouts[0]["duration"] == "<1m"
    assert workouts[1]["duration"] is None
    assert workouts[2]["duration"] == "30m"

    source = _function_source(_workspace_source(), "renderActivityCard")
    assert "escapeHtml(workout.duration)" in source
    assert all(workout.get("duration") != "0m" for workout in workouts)


# --- Day view: audio-level summaries ------------------------------------------


HEADPHONE_AUDIO_TYPE = "HKQuantityTypeIdentifierHeadphoneAudioExposure"
ENVIRONMENTAL_AUDIO_TYPE = "HKQuantityTypeIdentifierEnvironmentalAudioExposure"


def test_day_api_audio_levels_summarize_factual_range(body_env):
    env = body_env()
    rows = [
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-06T09:00:00-06:00",
            value="52.1",
            unit="dBASPL",
            source="Synthetic Buds",
        ),
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-06T13:00:00-06:00",
            value="78",
            unit="dBASPL",
            source="Synthetic Buds",
        ),
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-06T17:00:00-06:00",
            value="60.5",
            unit="dBASPL",
            source="Synthetic Buds",
        ),
        _row(
            ENVIRONMENTAL_AUDIO_TYPE,
            "2026-08-06T10:00:00-06:00",
            value="61.7",
            unit="dBASPL",
            source="Synthetic Watch",
        ),
        _row(
            ENVIRONMENTAL_AUDIO_TYPE,
            "2026-08-06T15:00:00-06:00",
            value="84.6",
            unit="dBASPL",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260910_050000", rows)

    payload = env.client.get("/app/body/api/day/20260806").get_json()

    facts = {fact["label"]: fact["value"] for fact in payload["mind_sound"]["facts"]}
    # Entry count plus the day's factual level range in the friendly
    # 'dB' label — no exposure judgments.
    assert facts["Headphone audio level"] == "3 entries · 52.1–78 dB"
    assert facts["Environmental audio level"] == "2 entries · 61.7–84.6 dB"

    source = _function_sources("renderSecondaryLists", "renderSimpleFactSection")
    assert "Mind & sound" in source
    assert "fact.value" in source
    # Factual range only inside the card — no exposure judgments.
    lowered = "\n".join(_collect_strings(payload["mind_sound"])).lower()
    assert "loud" not in lowered
    assert "warning" not in lowered
    assert "exposure" not in lowered


# --- Day view: new cards -----------------------------------------------------------


def test_day_api_body_measurements_and_other_signals_cards(body_env):
    env = body_env()
    rows = [
        _row(
            "HKQuantityTypeIdentifierBodyMass",
            "2026-08-01T07:00:00-06:00",
            value="185.2",
            unit="lb",
        ),
        _row(
            "HKQuantityTypeIdentifierBodyFatPercentage",
            "2026-08-01T07:00:30-06:00",
            value="0.223",
            unit="%",
        ),
        _row(
            "HKQuantityTypeIdentifierBodyMassIndex",
            "2026-08-01T07:01:00-06:00",
            value="24.1",
            unit="count",
        ),
        _row(
            "HKQuantityTypeIdentifierHeight",
            "2026-08-01T07:02:00-06:00",
            value="70",
            unit="in",
        ),
        _row(
            "HKQuantityTypeIdentifierAppleSleepingWristTemperature",
            "2026-08-01T03:00:00-06:00",
            value="96.53",
            unit="degF",
        ),
        _row(
            "HKQuantityTypeIdentifierNumberOfTimesFallen",
            "2026-08-01T10:00:00-06:00",
            value="1",
            unit="count",
        ),
    ]
    _seed_import(env.journal, "20260901_200000", rows)

    payload = env.client.get("/app/body/api/day/20260801").get_json()

    body_facts = {
        fact["label"]: fact["value"] for fact in payload["body_measurements"]["facts"]
    }
    assert body_facts["Body mass"] == "185.2 lb"
    # Fraction-percent fix: 0.223 renders as 22.3%, never "0.2 %".
    assert body_facts["Body fat"] == "22.3%"
    assert body_facts["Body mass index"] == "24.1"
    assert body_facts["Height"] == "70 in"
    other_facts = {
        fact["label"]: fact["value"] for fact in payload["other_signals"]["facts"]
    }
    assert other_facts["Wrist temperature"] == "96.5 degF"
    assert other_facts["Number of times fallen"] == "1"

    source = _function_source(_workspace_source(), "renderSecondaryLists")
    assert "Body measurements" in source
    assert "Other signals" in source


def test_day_api_multi_row_body_measurements_show_latest_value(body_env):
    env = body_env()
    mass = "HKQuantityTypeIdentifierBodyMass"
    fat = "HKQuantityTypeIdentifierBodyFatPercentage"
    rows = [
        _row(
            mass,
            "2026-05-08T07:00:00-06:00",
            value="173.1",
            unit="lb",
            source="Synthetic Scale",
        ),
        # The latest reading is picked by start time, not row order.
        _row(
            mass,
            "2026-05-08T21:00:00-06:00",
            value="172.4",
            unit="lb",
            source="Synthetic Scale",
        ),
        _row(
            mass,
            "2026-05-08T12:00:00-06:00",
            value="172.9",
            unit="lb",
            source="Synthetic Scale",
        ),
        _row(
            fat,
            "2026-05-08T07:00:00-06:00",
            value="0.231",
            unit="%",
            source="Synthetic Scale",
        ),
        _row(
            fat,
            "2026-05-08T21:00:00-06:00",
            value="0.223",
            unit="%",
            source="Synthetic Scale",
        ),
    ]
    _seed_import(env.journal, "20260910_140000", rows)

    payload = env.client.get("/app/body/api/day/20260508").get_json()
    facts = {
        fact["label"]: fact["value"] for fact in payload["body_measurements"]["facts"]
    }

    # Multi-entry measurement types headline the day's latest reading
    # through the shared normalizers (body fat's 0–1 fraction reads as a
    # percent), with the factual entry count alongside.
    assert facts["Body mass"] == "latest 172.4 lb · 3 entries"
    assert facts["Body fat"] == "latest 22.3% · 2 entries"


# --- Day view: prompt gating --------------------------------------------------------


def test_day_prompts_gate_journal_references_on_chronicle_day(body_env):
    env = body_env()
    rows = [
        _row(
            "HKWorkoutActivityTypeCycling",
            "2026-08-02T06:00:00-06:00",
            source="S",
            kind="workout",
        ),
    ]
    _seed_import(env.journal, "20260901_210000", rows)

    without_chronicle = env.client.get("/app/body/api/day/20260802").get_json()
    assert without_chronicle["prompts"]
    assert not any("journal" in prompt for prompt in without_chronicle["prompts"])

    (env.journal / "chronicle" / "20260802").mkdir(parents=True)
    with_chronicle = env.client.get("/app/body/api/day/20260802").get_json()
    assert any("journal" in prompt for prompt in with_chronicle["prompts"])


# --- Day view: sparse lede + glucose axis --------------------------------------------


def test_day_api_sparse_lede_names_families(body_env):
    env = body_env()
    rows = [
        _row(HR_TYPE, "2026-08-03T06:00:00-06:00", value="60", unit="count/min"),
        _row(HR_TYPE, "2026-08-03T07:00:00-06:00", value="62", unit="count/min"),
        _row(HR_TYPE, "2026-08-03T08:00:00-06:00", value="64", unit="count/min"),
        _row(STEP_TYPE, "2026-08-03T09:00:00-06:00", value="100", unit="count"),
        _row(STEP_TYPE, "2026-08-03T10:00:00-06:00", value="200", unit="count"),
        _row(
            "HKQuantityTypeIdentifierBodyMass",
            "2026-08-03T07:00:00-06:00",
            value="185",
            unit="lb",
        ),
    ]
    _seed_import(env.journal, "20260901_220000", rows)

    lede = env.client.get("/app/body/api/day/20260803").get_json()["lede"]

    assert lede == "6 entries across Heart, Activity, and 1 more area."


def test_day_api_glucose_axis_labels_match_padded_domain(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    curve = env.client.get("/app/body/api/day/20260703").get_json()["glucose_series"][0]

    # Readings span 100–140; the axis pads by 8% (3.2) and rounds outward,
    # and the labels state that actual rendered domain.
    assert curve["min"] == 100.0
    assert curve["max"] == 140.0
    assert curve["svg"]["y_min_label"] == "96"
    assert curve["svg"]["y_max_label"] == "144"


# --- Day view: evening nap bar sliver -------------------------------------------------


def test_day_api_post_6pm_doze_drops_bar_and_label_together(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-08-04T23:00:00-06:00",
            "2026-08-05T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        # An evening doze past the 6 PM axis end that never merged into a
        # following night: it has no honest place inside the 6 PM – 6 PM
        # axis, so the bar and its nap label drop together — never a
        # clamped sliver at the edge under an orphaned label.
        _row(
            SLEEP_TYPE,
            "2026-08-05T19:00:00-06:00",
            "2026-08-05T19:40:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(env.journal, "20260901_230000", rows)

    sleep = env.client.get("/app/body/api/day/20260805").get_json()["sleep"]

    assert [segment["kind"] for segment in sleep["bar"]["segments"]] == ["main"]
    assert sleep["naps"] == []
    # The main night stays the headline either way.
    assert sleep["window"] == "11:00 PM – 6:30 AM"


def test_day_api_nap_straddling_axis_end_keeps_visible_bar_and_label(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-08-06T23:00:00-06:00",
            "2026-08-07T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        # A doze straddling the 6 PM axis end keeps its list label and its
        # bar clips to a visible minimum width inside the axis.
        _row(
            SLEEP_TYPE,
            "2026-08-07T17:58:00-06:00",
            "2026-08-07T18:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(env.journal, "20260901_233000", rows)

    sleep = env.client.get("/app/body/api/day/20260807").get_json()["sleep"]

    nap_segments = [
        segment for segment in sleep["bar"]["segments"] if segment["kind"] == "nap"
    ]
    assert len(nap_segments) == 1
    assert nap_segments[0]["width"] >= 4.0
    assert nap_segments[0]["x"] + nap_segments[0]["width"] <= 1440.0
    assert sleep["naps"] == [{"window": "5:58 PM – 6:30 PM", "duration": "32m"}]


# --- Overview: month qualifiers, import list, audit bundles ---------------------------


def test_overview_titles_carry_sources_month_qualifier(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    status = env.client.get("/app/body/api/status").get_json()
    assert status["sources_month_label"] == "July 2026"

    source = _function_sources("renderSourcesRepresented", "renderOverviewAudit")
    assert "Sources represented" in source
    assert "status.sources_month_label" in source
    assert "By source" in source


def test_status_api_import_months_render_as_range_label(body_env):
    env = body_env()
    december = [
        _row(
            GLUCOSE_TYPE,
            "2025-12-30T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
    ]
    july = [
        _row(
            GLUCOSE_TYPE,
            "2026-07-03T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
    ]
    _seed_import(env.journal, "20260902_000000", december + july)
    # A manifest with no days and no normalized shards renders honestly.
    _write_json(
        env.journal / "imports" / "20260902_010000" / "manifest.json",
        {
            "import_id": "20260902_010000",
            "source_type": "apple_health",
            "source_hash": "sha256:empty",
            "entry_count": 0,
            "days_affected": [],
            "files_created": [],
            "imported_at": "2026-09-02T01:00:00",
            "imported_via": "test",
        },
    )

    status = env.client.get("/app/body/api/status").get_json()

    by_id = {item["import_id"]: item for item in status["imports"]}
    assert (
        by_id["20260902_000000"]["normalized_months_label"]
        == "2025-12 – 2026-07 · 2 months"
    )
    assert by_id["20260902_010000"]["normalized_months_label"] == "—"

    source = _function_sources("renderOverviewAudit", "importEvidenceMeta")
    assert "item.normalized_months_label" in source
    assert "—" in source


def test_day_audit_lists_every_bundle_containing_the_day(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-08-05T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
    ]
    _seed_import(env.journal, "20260903_000000", rows)
    # A second bundle imported the same entries (same dedupe keys) with
    # its own import id — the audit drawer must name both bundles.
    first_shard = (
        env.journal / "imports" / "20260903_000000" / "normalized" / "2026-08.jsonl"
    )
    second_shard = (
        env.journal / "imports" / "20260903_010000" / "normalized" / "2026-08.jsonl"
    )
    second_shard.parent.mkdir(parents=True)
    second_shard.write_text(
        first_shard.read_text(encoding="utf-8").replace(
            "20260903_000000", "20260903_010000"
        ),
        encoding="utf-8",
    )
    _write_json(
        env.journal / "imports" / "20260903_010000" / "manifest.json",
        {
            "import_id": "20260903_010000",
            "source_type": "apple_health",
            "source_hash": "sha256:overlap",
            "entry_count": 1,
            "days_affected": ["20260805"],
            "files_created": [],
            "imported_at": "2026-09-03T01:00:00",
            "imported_via": "test",
        },
    )

    payload = env.client.get("/app/body/api/day/20260805").get_json()

    assert payload["entry_total"] == 1
    assert payload["audit"]["import_ids"] == [
        "20260903_000000",
        "20260903_010000",
    ]


# --- Startup cache warm ------------------------------------------------------


def test_events_module_import_kicks_cache_warms(monkeypatch):
    """Convey startup — discover_handlers importing events.py — warms both caches.

    The import-time kick is the startup marker; the importer-completed
    handler re-warms through the same guarded entry points. The trends
    warm receives the stats warm's thread so its build runs after the
    stats fold instead of alongside it.
    """
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        body_routes,
        "warm_dedupe_stats_cache",
        lambda: calls.append(("stats", None)) or "stats-thread",
    )
    monkeypatch.setattr(
        body_routes,
        "warm_trends_cache",
        lambda after=None: calls.append(("trends", after)),
    )

    sys.modules.pop("solstone.apps.body.events", None)
    events = importlib.import_module("solstone.apps.body.events")

    assert calls == [("stats", None), ("trends", "stats-thread")]

    ctx = EventContext(
        msg={"tract": "importer", "event": "completed"},
        app="body",
        tract="importer",
        event="completed",
    )
    events.rewarm_caches_after_import(ctx)

    assert calls == [("stats", None), ("trends", "stats-thread")] * 2


def test_stats_warm_is_single_flight_and_recovers(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls: list[Path] = []

    def _blocking_stats(journal_root: Path) -> dict:
        calls.append(journal_root)
        started.set()
        assert release.wait(timeout=5)
        return {}

    monkeypatch.setattr(body_routes, "_read_health_dedupe_stats", _blocking_stats)

    first = body_routes.warm_dedupe_stats_cache()
    assert first is not None
    assert started.wait(timeout=5)

    # A second kick while the first is in flight is a no-op.
    assert body_routes.warm_dedupe_stats_cache() is None

    release.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert len(calls) == 1

    # The flight lock is released once the warm finishes — a new warm runs.
    follow_up = body_routes.warm_dedupe_stats_cache()
    assert follow_up is not None
    follow_up.join(timeout=5)
    assert not follow_up.is_alive()
    assert len(calls) == 2


def test_stats_warm_failure_is_contained_and_logged(body_env, caplog):
    env = body_env()
    _seed_health_import(env.journal)

    def _boom(journal_root: Path) -> dict:
        raise RuntimeError("synthetic warm failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(body_routes, "_read_health_dedupe_stats", _boom)
    try:
        with caplog.at_level(logging.ERROR, logger="solstone.apps.body.routes"):
            thread = body_routes.warm_dedupe_stats_cache()
            assert thread is not None
            thread.join(timeout=5)
            assert not thread.is_alive()
    finally:
        mp.undo()

    assert any("cache warm failed" in record.getMessage() for record in caplog.records)

    # The failed warm released the flight lock and never touched serving:
    # the request path recomputes on demand exactly as before.
    response = env.client.get("/app/body/api/status")
    assert response.status_code == 200
    assert response.get_json()["dedupe"]["total"] == 4

    recovery = body_routes.warm_dedupe_stats_cache()
    assert recovery is not None
    recovery.join(timeout=5)
    assert not recovery.is_alive()


# --- Trends ------------------------------------------------------------------


def _drain_trends_flight() -> None:
    """Wait out any in-flight trends build (the flight lock is held during one)."""
    assert body_routes._trends_warm_flight.acquire(timeout=10)
    body_routes._trends_warm_flight.release()


def _trends_after_warm(client) -> dict:
    """Kick the trends build through the API and return the warmed payload."""
    _drain_trends_flight()
    first = client.get("/app/body/api/trends").get_json()
    if first["warming"]:
        # While warming, nothing else rides along — the whole payload is
        # the flag.
        assert first == {"warming": True}
        _drain_trends_flight()
        first = client.get("/app/body/api/trends").get_json()
    assert first["warming"] is False
    return first


HEART_RATE_TYPE = "HKQuantityTypeIdentifierHeartRate"
BODY_MASS_TYPE = "HKQuantityTypeIdentifierBodyMass"


def _seed_trend_days(journal: Path) -> None:
    """Two June days exercising every ribbon's honesty rule at once.

    June 1: resting HR (two readings — latest wins), a single-source step
    total, a body-mass reading beside a lean-mass row that must not leak
    into the mass ribbon, and two glucose readings averaging to one
    decimal. June 2: raw heart-rate rows but no resting HR (the ribbon
    stays absent), two-source interval-less steps (sample-only — absent),
    BMI-only mass rows (absent), and the stage-aware night that started
    June 1 at 10:30 PM — asleep minutes attribute to June 2, the morning
    the night ended.
    """
    rows = [
        # June 1 — resting HR: 7 AM reading superseded by the 9 PM one.
        _row(
            RESTING_HR_TYPE,
            "2026-06-01T07:00:00-06:00",
            value="60",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            RESTING_HR_TYPE,
            "2026-06-01T21:00:00-06:00",
            value="58",
            unit="count/min",
            source="Synthetic Watch",
        ),
        # June 1 — single-source steps sum to a real total.
        _row(
            STEP_TYPE,
            "2026-06-01T08:00:00-06:00",
            value="500",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-06-01T12:00:00-06:00",
            value="700",
            unit="count",
            source="Synthetic Phone",
        ),
        # June 1 — body mass, plus a lean-mass row the ribbon must ignore.
        _row(
            BODY_MASS_TYPE,
            "2026-06-01T08:00:00-06:00",
            value="172.4",
            unit="lb",
            source="Synthetic Scale",
        ),
        _row(
            "HKQuantityTypeIdentifierLeanBodyMass",
            "2026-06-01T08:00:00-06:00",
            value="140.0",
            unit="lb",
            source="Synthetic Scale",
        ),
        # June 1 — glucose readings average to one decimal.
        _row(
            GLUCOSE_TYPE,
            "2026-06-01T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-06-01T12:00:00-06:00",
            value="141",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        # June 2 — raw heart-rate rows are never a resting-HR fallback.
        _row(
            HEART_RATE_TYPE,
            "2026-06-02T12:00:00-06:00",
            value="72",
            unit="count/min",
            source="Synthetic Watch",
        ),
        # June 2 — two sources, no intervals: no coverage to rank by, so
        # the day has no honest total and stays absent.
        _row(
            STEP_TYPE,
            "2026-06-02T08:00:00-06:00",
            value="3000",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-06-02T09:00:00-06:00",
            value="2000",
            unit="count",
            source="Synthetic Wrist",
        ),
        # June 2 — BMI is not the body-mass ribbon.
        _row(
            "HKQuantityTypeIdentifierBodyMassIndex",
            "2026-06-02T08:00:00-06:00",
            value="24.1",
            unit="count",
            source="Synthetic Scale",
        ),
        # June 2 — glucose.
        _row(
            GLUCOSE_TYPE,
            "2026-06-02T08:00:00-06:00",
            value="90",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-06-02T12:00:00-06:00",
            value="110",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        # The night crossing midnight: in-bed span with asleep stages —
        # 180 + 210 = 390 asleep minutes, ending the morning of June 2.
        _row(
            SLEEP_TYPE,
            "2026-06-01T22:30:00-06:00",
            "2026-06-02T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-06-01T23:00:00-06:00",
            "2026-06-02T02:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-06-02T02:30:00-06:00",
            "2026-06-02T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepDeep",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(journal, "20260901_000000", rows)


def test_trends_page_serves_static_shell_and_static_rule_wins(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    # The static /trends rule wins over the /<day> converter.
    response = env.client.get("/app/body/trends")
    assert response.status_code == 200
    assert 'data-solstone-shell="spa"' in response.get_data(as_text=True)

    source = _function_source(_workspace_source(), "initBodyTrends")
    assert 'if (segment !== "trends") return;' in source
    assert not hasattr(body_routes, "render_template")


def test_trends_api_contract_shape_and_signal_honesty(body_env):
    env = body_env()
    _seed_trend_days(env.journal)

    payload = _trends_after_warm(env.client)

    assert set(payload) == {"warming", "signals", "annotations", "generated_at_day"}
    assert body_routes.DAY_RE.fullmatch(payload["generated_at_day"])

    signals = {signal["key"]: signal for signal in payload["signals"]}
    assert [signal["key"] for signal in payload["signals"]] == [
        "resting_hr",
        "asleep_minutes",
        "steps",
        "body_mass",
        "glucose_avg",
    ]
    assert {key: signal["label"] for key, signal in signals.items()} == {
        "resting_hr": "Resting heart rate",
        "asleep_minutes": "Asleep",
        "steps": "Steps",
        "body_mass": "Body mass",
        "glucose_avg": "Glucose average",
    }
    assert {key: signal["unit_label"] for key, signal in signals.items()} == {
        "resting_hr": "bpm",
        "asleep_minutes": "h",
        "steps": "steps",
        "body_mass": "lb",
        "glucose_avg": "mg/dL",
    }

    # Latest resting reading wins; June 2's raw heart-rate rows never
    # fabricate a resting value — the day is simply absent.
    assert signals["resting_hr"]["daily"] == [["20260601", 58.0]]
    # Canonical cross-midnight sleep: stage-aware asleep minutes on the
    # morning the night ended, nothing on the evening it started.
    assert signals["asleep_minutes"]["daily"] == [["20260602", 390.0]]
    # Single-source total on June 1; the multi-source sample-only June 2
    # stays absent instead of faking a total.
    assert signals["steps"]["daily"] == [["20260601", 1200]]
    # Sparse body mass stays sparse; lean mass and BMI never leak in.
    assert signals["body_mass"]["daily"] == [["20260601", 172.4]]
    # Daily mean to one decimal, days ascending.
    assert signals["glucose_avg"]["daily"] == [
        ["20260601", 120.5],
        ["20260602", 100.0],
    ]
    assert signals["glucose_avg"]["coverage"] == {
        "first_day": "20260601",
        "last_day": "20260602",
        "days": 2,
    }
    assert signals["steps"]["coverage"] == {
        "first_day": "20260601",
        "last_day": "20260601",
        "days": 1,
    }

    # The only source arriving after the archive's first day is the June 2
    # wrist — day-one sources draw no marker.
    assert payload["annotations"] == [
        {"day": "20260602", "label": "Synthetic Wrist data begins"}
    ]


def test_trends_api_warming_is_single_flight_and_never_blocks(body_env, monkeypatch):
    env = body_env()
    _seed_health_import(env.journal)
    _drain_trends_flight()

    started = threading.Event()
    release = threading.Event()
    calls: list[Path] = []
    real_build = body_routes._build_trends_payload

    def _blocking_build(journal_root: Path) -> dict:
        calls.append(journal_root)
        started.set()
        assert release.wait(timeout=5)
        return real_build(journal_root)

    monkeypatch.setattr(body_routes, "_build_trends_payload", _blocking_build)

    # First request: cache empty → warming, build kicked.
    assert env.client.get("/app/body/api/trends").get_json() == {"warming": True}
    assert started.wait(timeout=5)
    # Mid-build request: still warming, no second build stacked, and the
    # response returns while the build is still blocked — it never waits.
    assert env.client.get("/app/body/api/trends").get_json() == {"warming": True}

    release.set()
    _drain_trends_flight()
    assert len(calls) == 1

    payload = env.client.get("/app/body/api/trends").get_json()
    assert payload["warming"] is False
    assert payload["signals"]


def test_trends_cache_invalidates_when_new_bundle_lands(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260901_000000",
        [
            _row(
                GLUCOSE_TYPE,
                "2026-06-01T08:00:00-06:00",
                value="100",
                unit="mg/dL",
                source="Synthetic Stelo",
            )
        ],
    )

    first = _trends_after_warm(env.client)
    glucose = next(s for s in first["signals"] if s["key"] == "glucose_avg")
    assert [entry[0] for entry in glucose["daily"]] == ["20260601"]

    # A new bundle writes the dedupe database — the signature change must
    # invalidate the warmed payload.
    _seed_import(
        env.journal,
        "20260902_000000",
        [
            _row(
                GLUCOSE_TYPE,
                "2026-07-05T08:00:00-06:00",
                value="120",
                unit="mg/dL",
                source="Synthetic Stelo",
            )
        ],
    )

    _drain_trends_flight()
    assert env.client.get("/app/body/api/trends").get_json() == {"warming": True}

    second = _trends_after_warm(env.client)
    glucose = next(s for s in second["signals"] if s["key"] == "glucose_avg")
    assert [entry[0] for entry in glucose["daily"]] == ["20260601", "20260705"]


def test_trends_api_empty_journal_serves_empty_payload_read_only(body_env):
    env = body_env()
    imports_root = env.journal / "imports"
    assert not imports_root.exists()

    payload = _trends_after_warm(env.client)

    assert payload["signals"] == []
    assert payload["annotations"] == []
    assert body_routes.DAY_RE.fullmatch(payload["generated_at_day"])
    # The warmed build read nothing into being.
    assert not imports_root.exists()
    assert env.client.get("/app/body/trends").status_code == 200
    assert not imports_root.exists()


def test_trends_annotations_mark_first_appearances(body_env):
    env = body_env()
    rows = [
        _row(
            HEART_RATE_TYPE,
            "2026-05-01T08:00:00-06:00",
            value="70",
            unit="count/min",
            source="Synthetic Phone",
        ),
        _row(
            HEART_RATE_TYPE,
            "2026-06-10T08:00:00-06:00",
            value="64",
            unit="count/min",
            source="Oura",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-06-15T08:00:00-06:00",
            value="105",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260901_000000", rows)

    payload = _trends_after_warm(env.client)

    # First appearances only, day-ascending, none for the day-one source
    # (its ribbon's left edge already says it), capped by the limit.
    assert payload["annotations"] == [
        {"day": "20260610", "label": "Oura data begins"},
        {"day": "20260615", "label": "CGM readings begin"},
        {"day": "20260615", "label": "Synthetic Stelo data begins"},
    ]
    assert len(payload["annotations"]) <= body_routes.TREND_ANNOTATION_LIMIT


def test_trends_warm_builds_after_stats_thread(monkeypatch):
    order: list[str] = []
    release = threading.Event()

    def _slow_stats() -> None:
        assert release.wait(timeout=5)
        order.append("stats")

    stats_thread = threading.Thread(target=_slow_stats)
    stats_thread.start()

    def _fake_build(journal_root: Path) -> dict:
        order.append("trends")
        return {"signals": [], "annotations": [], "generated_at_day": "20260101"}

    monkeypatch.setattr(body_routes, "_build_trends_payload", _fake_build)
    _drain_trends_flight()
    body_routes._trends_cache.clear()

    warm = body_routes.warm_trends_cache(after=stats_thread)
    assert warm is not None
    release.set()
    warm.join(timeout=5)
    assert not warm.is_alive()

    # The trends fold waited for the stats thread — the two heavy builds
    # run sequentially on the shared warm path.
    assert order == ["stats", "trends"]


def test_stats_warm_leaves_first_request_hot(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    thread = body_routes.warm_dedupe_stats_cache()
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive()

    # After the warm the overview no longer needs the database at all:
    # block sqlite in the routes module and the warmed cache still serves.
    class _NoSqlite:
        def connect(self, *args, **kwargs):
            raise AssertionError("dedupe stats were not served from the warm cache")

    mp = pytest.MonkeyPatch()
    mp.setattr(body_routes, "sqlite3", _NoSqlite())
    try:
        response = env.client.get("/app/body/api/status")
    finally:
        mp.undo()

    assert response.status_code == 200
    assert response.get_json()["dedupe"]["total"] == 4


# --- Trends front-end --------------------------------------------------------
#
# Owned by the Trends front-end change-set (workspace.html). The pins below
# are fragment-source-level — the repo's JS-invariant idiom.


def _trends_workspace_source() -> str:
    return _workspace_source()


def test_trends_init_selects_trends_segment_only():
    source = _trends_workspace_source()

    overview_at = source.index("function initBodyOverview()")
    day_at = source.index("function initBodyDay()")
    trends_at = source.index("function initBodyTrends()")
    assert overview_at < day_at < trends_at

    # Header: Trends title plus the overview backlink, day-page idiom.
    branch = _function_sources("renderHeader", "renderTrendsShell", "initBodyTrends")
    assert "Body · Trends" in branch
    assert 'href="/app/body/"' in branch
    assert "Body overview" in branch
    assert 'if (segment !== "trends") return;' in branch


def test_trends_view_fetches_api_and_polls_while_warming():
    source = _trends_workspace_source()

    assert 'window.apiJson("/app/body/api/trends")' in source
    # The calm warming placeholder, re-polled every five seconds until the
    # first build lands.
    assert (
        "Preparing trends — the first build over five years takes a minute." in source
    )
    assert "const POLL_MS = 5000;" in source
    assert "window.setTimeout(loadTrends, POLL_MS);" in source
    # The ribbon stack stays hidden until real series arrive.
    assert (
        '<div class="body-trends-stack" id="body-trends-stack" hidden></div>' in source
    )


def test_trends_ribbons_bucket_weekly_medians_with_honest_gaps():
    source = _trends_workspace_source()

    # Client-side weekly median bucketing of the daily points.
    assert "function weeklyMedians(daily)" in source
    assert "function median(values)" in source
    assert "Math.floor((dayToUtc(day) - WEEK_EPOCH) / MS_WEEK)" in source

    # The app's curve gap rule at week scale: an absent week splits the
    # segment — never a line drawn across it — and isolated weeks are dots.
    assert "point.week - current[current.length - 1].week > 1" in source
    assert "segment.length === 1" in source

    # Ribbons are real buttons that disclose the inline canvas.
    assert 'class="body-trends-ribbon-btn"' in source
    assert 'aria-expanded="false" aria-controls="' in source
    # Annotation ticks draw on the ribbon sparklines.
    assert "body-trends-tick" in source


def test_trends_canvas_mirrors_curve_idiom_with_year_axis():
    source = _trends_workspace_source()

    # The drilldown chart is the day page's .body-curve treatment.
    assert '<svg class="body-curve" viewBox="0 0 ' in source
    assert "body-curve-wrap" in source
    assert "body-curve-y" in source

    # Padded y-domain rounded outward to integers, the app convention.
    assert "Math.max((vMax - vMin) * 0.08, 2.0)" in source
    assert "Math.floor(vMin - pad)" in source
    assert "Math.ceil(vMax + pad)" in source

    # The x-axis marks year boundaries within the visible window.
    assert "year !== lastYear" in source
    assert "body-trends-axis" in source


def test_trends_range_chips_annotations_and_collapse_controls():
    source = _trends_workspace_source()

    # 1y / 3y / All re-window client-side from the latest data week.
    assert 'const RANGE_WEEKS = { "1y": 52, "3y": 156 }' in source
    assert '["1y", "3y", "all"]' in source
    assert "data-range" in source
    assert "Math.max(domain.w0, domain.w1 - span)" in source

    # Annotation flags ride dashed verticals and toggle off as a group.
    assert "body-trends-flag-line" in source
    assert "border-left: 1px dashed var(--orange-ink)" in source
    assert "data-flags-toggle" in source
    assert 'aria-pressed="' in source

    # Close control and Escape both collapse the open canvas.
    assert "data-collapse" in source
    assert ">Close<" in source
    assert 'if (event.key === "Escape") collapseOpen(true);' in source
    assert 'event.key !== "Enter" && event.key !== " "' in source
    assert "toggleSignal(btn.dataset.signal);" in source

    # One canvas at a time; resting heart rate opens first when present.
    assert "function expandSignal(key)" in source
    assert "collapseOpen(false);" in _function_source(source, "expandSignal")
    assert 'expandSignal("resting_hr")' in source


def test_trends_overview_button_sits_in_quick_entry_row():
    source = _function_source(_workspace_source(), "renderQuickActions")

    assert (
        '<a class="body-btn body-btn--outline" href="/app/body/trends">Trends</a>'
        in source
    )
    # The link sits in the quick-entry row after the latest-day button.
    latest_at = source.index("Open latest day")
    trends_at = source.index('href="/app/body/trends"')
    assert latest_at < trends_at


def test_trends_copy_avoids_surveillance_and_interpretation_words():
    # The whole-template surveillance-verb ban is asserted elsewhere; the
    # trends surface additionally never interprets — no progress or
    # judgment vocabulary anywhere in the template source.
    source = _trends_workspace_source()
    interpretive = {"improving", "declining", "better", "worse"}
    found = {word for word in interpretive if word in source}
    assert found == set(), f"workspace.html contains interpretive copy: {found}"


def test_trends_canvas_domain_floor_never_negative_for_nonnegative_signals():
    # Steps rendered a "-1,117 steps" axis label when padding pushed the
    # floor below zero (Codex trends review finding, 2026-07-05).
    workspace_html = (
        Path(__file__).resolve().parents[1] / "workspace.html"
    ).read_text()
    assert "if (vMin >= 0 && lo < 0) lo = 0;" in workspace_html


# --- Oura display: O-5C same-device supersede ---------------------------------
#
# The same ring's data can arrive twice: normalized Oura API rows
# (source_family "oura_api") and rows the Oura app mirrored into Apple
# Health (Oura-named source). Day-level aggregation uses ONE canonical
# pipe: where API rows exist for a day+signal, the mirror's rows for that
# signal stay out of aggregates, curves, and counts. Cross-device data
# (Apple Watch, iPhone) is never touched. The audit drawer lists all.


def test_day_api_same_device_supersede_prefers_api_pipe(body_env):
    env = body_env()
    # One bundle carries the Apple Health rows: a genuine Watch night and
    # SpO2 reading, plus the ring's mirror rows for the same night.
    _seed_import(
        env.journal,
        "20260801_100000",
        [
            _row(
                SLEEP_TYPE,
                "2026-07-10T00:15:00-06:00",
                "2026-07-10T07:00:00-06:00",
                value="HKCategoryValueSleepAnalysisAsleepUnspecified",
                source="Synthetic Watch",
            ),
            _row(
                SPO2_TYPE,
                "2026-07-10T03:30:00-06:00",
                value="0.97",
                unit="%",
                source="Synthetic Watch",
            ),
            _row(
                SLEEP_TYPE,
                "2026-07-10T00:05:00-06:00",
                "2026-07-10T03:00:00-06:00",
                value="HKCategoryValueSleepAnalysisAsleepCore",
                source="Oura",
            ),
            _row(
                SLEEP_TYPE,
                "2026-07-10T03:00:00-06:00",
                "2026-07-10T07:20:00-06:00",
                value="HKCategoryValueSleepAnalysisAsleepDeep",
                source="Oura",
            ),
            _row(
                SPO2_TYPE,
                "2026-07-10T03:00:00-06:00",
                value="0.94",
                unit="%",
                source="Oura",
            ),
            _row(
                SPO2_TYPE,
                "2026-07-10T04:00:00-06:00",
                value="0.99",
                unit="%",
                source="Oura",
            ),
        ],
    )
    # A second bundle carries the same night through the API pipe.
    _seed_import(
        env.journal,
        "20260805_100000",
        [
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                "20260710",
                value=27000,
                unit="s",
                start="2026-07-10T00:00:00-06:00",
                end="2026-07-10T07:45:00-06:00",
                kind="sleep_period",
            ),
            _oura_row(
                OURA_SPO2_TYPE,
                "20260710",
                value=97.4,
                unit="%",
                metadata={"breathing_disturbance_index": 3},
            ),
            _oura_row(OURA_READINESS_TYPE, "20260710", value=82, unit="score"),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260710").get_json()

    # Sleep aggregates from the API period and the Watch only; the mirror
    # source vanishes from the card (API supersedes mirror).
    sleep = payload["sleep"]
    assert sleep["source"] == "Oura (API)"
    assert sleep["other_sources"] == ["Synthetic Watch"]
    assert sleep["window"] == "12:00 AM – 7:45 AM"
    assert sleep["duration"] == "7h 45m"
    assert sleep["score_line"] is None
    # Blood oxygen keeps only the genuine cross-device reading — the
    # mirror's 94/99 samples never widen the range.
    assert payload["heart"]["facts"] == [
        {"label": "Blood oxygen", "count": 1, "count_label": "1", "value": "97%"}
    ]
    # The API's nightly average renders on the recovery card, attributed.
    assert [fact["line"] for fact in payload["recovery"]["facts"]] == [
        "Readiness 82 · Oura's score",
        "Nightly blood oxygen 97.4% · Oura's average",
    ]
    # Counts follow the canonical pipe.
    assert payload["entry_total"] == 5
    sources = payload["sources"]
    assert sources["names"] == ["Oura (API)", "Synthetic Watch"]
    assert sources["via"] == "Apple Health + Oura API"
    # The audit drawer still lists every row from both pipes and both
    # bundles — superseded mirror rows included.
    assert payload["audit"]["types"] == {
        "HKCategoryTypeIdentifierSleepAnalysis": 3,
        "HKQuantityTypeIdentifierOxygenSaturation": 3,
        "oura.daily_readiness": 1,
        "oura.daily_spo2": 1,
        "oura.sleep": 1,
    }
    assert payload["audit"]["import_ids"] == ["20260801_100000", "20260805_100000"]


def test_day_api_mirror_rows_aggregate_when_no_api_rows(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260801_110000",
        [
            _row(
                SLEEP_TYPE,
                "2026-07-12T00:10:00-06:00",
                "2026-07-12T07:30:00-06:00",
                value="HKCategoryValueSleepAnalysisInBed",
                source="Oura",
            ),
            _row(
                SPO2_TYPE,
                "2026-07-12T03:00:00-06:00",
                value="0.94",
                unit="%",
                source="Oura",
            ),
            _row(
                SPO2_TYPE,
                "2026-07-12T04:00:00-06:00",
                value="0.99",
                unit="%",
                source="Oura",
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260712").get_json()

    # Backward compatible: without API rows the mirror aggregates exactly
    # as before the supersede seam existed.
    sleep = payload["sleep"]
    assert sleep["source"] == "Oura"
    assert sleep["window"] == "12:10 AM – 7:30 AM"
    assert payload["heart"]["facts"] == [
        {"label": "Blood oxygen", "count": 2, "count_label": "2", "value": "94–99%"}
    ]
    assert payload["recovery"] is None


def test_trends_asleep_supersedes_mirror_on_api_days(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260901_170000",
        [
            # June 3: mirror-only night — folds as today.
            _row(
                SLEEP_TYPE,
                "2026-06-03T00:00:00-06:00",
                "2026-06-03T08:20:00-06:00",
                value="HKCategoryValueSleepAnalysisInBed",
                source="Oura",
            ),
            # June 5: the mirror's longer span would win coverage, but the
            # API pipe carries the same night — the mirror stays out.
            _row(
                SLEEP_TYPE,
                "2026-06-05T00:00:00-06:00",
                "2026-06-05T09:00:00-06:00",
                value="HKCategoryValueSleepAnalysisInBed",
                source="Oura",
            ),
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                "20260605",
                value=25200,
                unit="s",
                start="2026-06-05T00:30:00-06:00",
                end="2026-06-05T07:30:00-06:00",
                kind="sleep_period",
            ),
        ],
    )

    payload = _trends_after_warm(env.client)

    asleep = next(s for s in payload["signals"] if s["key"] == "asleep_minutes")
    assert asleep["daily"] == [["20260603", 500.0], ["20260605", 420.0]]


# --- Day view: recovery card + Oura sleep score --------------------------------
#
# "How recovered am I?" renders Oura's daily facts, attributed. Round 2
# (owner decision): Oura's qualitative labels — the resilience level, the
# stress day summary — render as attributed facts in the fixed
# "<value> · Oura's label" format. They are Oura's words, named as
# Oura's, never Solstone's conclusion.

# Oura's own qualitative vocabulary (resilience levels, stress day
# summaries) plus generic judgment words — each may reach the JSON payload
# ONLY inside the attributed-label format, immediately followed by
# " · Oura's label". Anywhere else it is an unattributed judgment and fails
# the sweep.
RECOVERY_BANNED_ADJECTIVES = (
    "solid",
    "normal",
    "stressful",
    "restored",
    "optimal",
    "good",
    "poor",
    "excellent",
    "limited",
    "adequate",
)

# The lowercased raw JSON attribution tail that must immediately follow
# any adjective in the API payload. Client-side escaping happens later via
# AppServices.escapeHtml, so the stale HTML-escaped form must not be used.
_ATTRIBUTED_LABEL_TAIL = " · oura's label"


def _assert_adjectives_only_attributed(card: str) -> None:
    """Every banned adjective in ``card`` sits in an attributed-label line."""
    for word in RECOVERY_BANNED_ADJECTIVES:
        for match in re.finditer(re.escape(word), card):
            tail = card[match.end() : match.end() + len(_ATTRIBUTED_LABEL_TAIL)]
            assert tail == _ATTRIBUTED_LABEL_TAIL, (
                f"unattributed adjective in recovery card: {word!r} "
                f"(followed by {tail!r})"
            )


def _seed_recovery_day(journal: Path, day: str = "20260715") -> None:
    _seed_import(
        journal,
        "20260905_100000",
        [
            _oura_row(
                OURA_READINESS_TYPE,
                day,
                value=82,
                unit="score",
                metadata={"contributors": {"hrv_balance": 88}},
            ),
            _oura_row(OURA_TEMP_DEV_TYPE, day, value=0.34, unit="degC"),
            _oura_row(
                OURA_SPO2_TYPE,
                day,
                value=97.4,
                unit="%",
                metadata={"breathing_disturbance_index": 3},
            ),
            _oura_row(
                OURA_STRESS_TYPE,
                day,
                value="normal",
                metadata={"stress_high": 7200, "recovery_high": 20400},
            ),
            _oura_row(OURA_RESILIENCE_TYPE, day, value="solid"),
        ],
    )


def test_day_api_recovery_subtree_pins_attributed_number_lines(body_env):
    env = body_env()
    _seed_recovery_day(env.journal)

    payload = env.client.get("/app/body/api/day/20260715").get_json()

    # Exact attributed lines, in fixed order, matching the importer's
    # render_day_summary copy reference. Round 2: the resilience level
    # and the stress day summary render as attributed facts in the
    # "<value> · Oura's label" format — Oura's words, named as Oura's.
    assert [fact["line"] for fact in payload["recovery"]["facts"]] == [
        "Readiness 82 · Oura's score",
        "Resilience solid · Oura's label",
        "Temperature deviation +0.34 °C · Oura's measurement",
        "Nightly blood oxygen 97.4% · Oura's average",
        "Daytime stress high 2h 00m · recovery 5h 40m · Oura's measurement",
        "Day stress summary normal · Oura's label",
    ]
    # The readiness contributors ride along as attributed numbers with
    # owner-facing labels — the score's anatomy.
    assert payload["recovery"]["contributors"] == [
        {"label": "HRV balance", "value": 88}
    ]
    # The nightly average never doubles as a generic heart fact.
    assert payload["heart"] is None
    # Recovery rows never leak into the Other-signals catch-all.
    assert payload["other_signals"] is None

    # Adjectives render only inside the attributed-label format; the
    # sweep still bans every unattributed appearance.
    recovery_strings = [text.lower() for text in _collect_strings(payload["recovery"])]
    assert "solid · oura's label" in recovery_strings
    assert "normal · oura's label" in recovery_strings
    _assert_adjectives_only_attributed("\n".join(recovery_strings))

    # The window API picks up the same rows with their friendly labels.
    window = env.client.get(
        "/app/body/api/window?from=2026-07-15T00:00:00Z&to=2026-07-15T23:00:00Z"
    ).get_json()
    labels = {signal["label"] for signal in window["signals"]}
    assert "Readiness" in labels
    assert "Nightly blood oxygen" in labels


def test_workspace_recovery_renderer_adds_no_unattributed_adjectives():
    source = _function_sources(
        "renderRecoveryCard",
        "renderScoreAnatomy",
        "renderSimpleFactSection",
    ).lower()
    assert "recovery.facts" in source
    assert "renderscoreanatomy" in source
    for word in RECOVERY_BANNED_ADJECTIVES:
        assert word not in source, (
            f"unattributed adjective in recovery renderer: {word!r}"
        )


def test_day_api_recovery_temperature_sign_is_explicit(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260905_110000",
        [_oura_row(OURA_TEMP_DEV_TYPE, "20260716", value=-0.21, unit="degC")],
    )

    payload = env.client.get("/app/body/api/day/20260716").get_json()

    assert [fact["line"] for fact in payload["recovery"]["facts"]] == [
        "Temperature deviation -0.21 °C · Oura's measurement"
    ]


def test_day_api_recovery_absent_without_oura_rows(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    payload = env.client.get("/app/body/api/day/20260703").get_json()

    assert payload["recovery"] is None


def test_day_api_sleep_card_gains_oura_score_line(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260905_120000",
        [
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                "20260718",
                value=27000,
                unit="s",
                start="2026-07-18T00:00:00-06:00",
                end="2026-07-18T07:30:00-06:00",
                kind="sleep_period",
            ),
            _oura_row(OURA_SLEEP_SCORE_TYPE, "20260718", value=88, unit="score"),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260718").get_json()

    sleep = payload["sleep"]
    assert sleep["source"] == "Oura (API)"
    assert sleep["score_line"] == "Sleep score 88 · Oura's score"
    # The score renders only as the sleep card's attributed line, never as
    # an unattributed count fact elsewhere.
    assert payload["other_signals"] is None

    source = _function_source(_workspace_source(), "renderSleepCard")
    assert "sleep.score_line" in source
    assert "escapeHtml(sleep.score_line)" in source


def test_status_api_recovery_family_and_oura_api_source_chip(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260905_130000",
        [
            _oura_row(OURA_READINESS_TYPE, "20260720", value=82, unit="score"),
            _oura_row(OURA_RESILIENCE_TYPE, "20260720", value="solid"),
            _oura_row(
                OURA_STRESS_TYPE,
                "20260720",
                value="normal",
                metadata={"stress_high": 3600, "recovery_high": 7200},
            ),
            _oura_row(OURA_TEMP_DEV_TYPE, "20260720", value=0.1, unit="degC"),
            _oura_row(OURA_SLEEP_SCORE_TYPE, "20260720", value=88, unit="score"),
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                "20260720",
                value=27000,
                unit="s",
                start="2026-07-20T00:00:00-06:00",
                end="2026-07-20T07:30:00-06:00",
                kind="sleep_period",
            ),
            _oura_row(OURA_SPO2_TYPE, "20260720", value=97.4, unit="%"),
            _row(
                GLUCOSE_TYPE,
                "2026-07-20T08:00:00-06:00",
                value="100",
                unit="mg/dL",
                source="Synthetic Stelo",
            ),
            _row(
                STEP_TYPE,
                "2026-07-20T09:00:00-06:00",
                value="1200",
                unit="count",
                source="Synthetic Phone",
            ),
        ],
    )

    status = env.client.get("/app/body/api/status").get_json()

    # The Recovery family folds the four Oura daily types, ordered after
    # Glucose and before Activity; sleep types stay in Sleep, SpO2 in Heart.
    families = {chip["name"]: chip for chip in status["archive"]["families"]}
    assert [chip["name"] for chip in status["archive"]["families"]] == [
        "Sleep",
        "Glucose",
        "Recovery",
        "Activity",
        "Heart",
    ]
    assert families["Recovery"]["count"] == 4
    assert families["Recovery"]["types_label"] == (
        "Daytime stress, Readiness, Resilience, Temperature deviation"
    )
    # Rows without a device source name chip as the pipe they arrived by.
    assert "Oura (API)" in status["normalized"]["by_source"]


# --- Trends: readiness ribbon ---------------------------------------------------


def test_trends_readiness_signal_folds_daily_scores(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260905_140000",
        [
            _row(
                RESTING_HR_TYPE,
                "2026-06-01T21:00:00-06:00",
                value="58",
                unit="count/min",
                source="Synthetic Watch",
            ),
            _oura_row(OURA_READINESS_TYPE, "20260601", value=82, unit="score"),
            _oura_row(OURA_READINESS_TYPE, "20260602", value=74, unit="score"),
            _row(
                STEP_TYPE,
                "2026-06-01T09:00:00-06:00",
                value="1200",
                unit="count",
                source="Synthetic Phone",
            ),
        ],
    )

    payload = _trends_after_warm(env.client)

    # Readiness joins the fixed ribbon order between Asleep and Steps;
    # signals the journal has never held (asleep, body mass, glucose)
    # draw no ribbon at all.
    assert [signal["key"] for signal in payload["signals"]] == [
        "resting_hr",
        "readiness",
        "steps",
    ]
    readiness = next(s for s in payload["signals"] if s["key"] == "readiness")
    assert readiness["label"] == "Readiness"
    # Oura's score is unitless — the empty label renders plain numbers.
    assert readiness["unit_label"] == ""
    assert readiness["daily"] == [["20260601", 82.0], ["20260602", 74.0]]
    assert readiness["coverage"] == {
        "first_day": "20260601",
        "last_day": "20260602",
        "days": 2,
    }


# --- Round-2 Oura display SERVER: overlap endpoints, anatomy, typical ----------
#
# Owned by the round-2 SERVER change-set (routes.py + health_schema.py).
# Covers: the AH-mirror overlap endpoints (oura.heartrate joins the heart
# range/curve; oura.daily_activity joins Activity with the ring's step
# total as the one canonical device), the extended O-5C supersede, score
# contributors, attributed adjectives, per-fact "vs your typical"
# medians, cross-device comparison lines, and the new trends ribbons.

OURA_ACTIVITY_TYPE = "oura.daily_activity"
OURA_HR_SAMPLE_TYPE = "oura.heartrate"


def _hr_sample_row(day: str, clock: str, bpm: int) -> dict:
    """A normalized oura.heartrate sample row (the API pipe's raw beats)."""
    iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    return _oura_row(
        OURA_HR_SAMPLE_TYPE,
        day,
        value=bpm,
        unit="bpm",
        start=f"{iso}T{clock}:00-06:00",
        kind="sample",
    )


def test_day_api_heart_range_and_curve_join_ring_beats(body_env):
    env = body_env()
    watch = _hr_rows("2026-07-22", [(f"06:{i:02d}", 60 + i) for i in range(12)])
    ring = [
        _hr_sample_row("20260722", "06:00", 45),
        _hr_sample_row("20260722", "06:11", 150),
    ]
    _seed_import(env.journal, "20260906_100000", watch + ring)

    payload = env.client.get("/app/body/api/day/20260722").get_json()

    heart = payload["heart"]
    # Ring beats join the one range computation and widen the honest band.
    assert heart["heart_rate"]["count"] == 14
    assert heart["heart_rate"]["min"] == 45.0
    assert heart["heart_rate"]["max"] == 150.0
    assert heart["heart_rate"]["label"] == "45–150 bpm"
    # count/min and bpm are one display unit — the curve still draws,
    # and its buckets fold watch and ring beats together.
    series = heart["series"]
    assert series is not None
    assert series["count"] == 14
    assert series["unit"] == "bpm"
    assert series["unit_label"] == "bpm"
    assert series["bands"][0] == [362.5, 45.0, 64.0]
    assert series["bands"][-1] == [372.5, 70.0, 150.0]
    # Both devices sampled: the comparison line juxtaposes their ranges,
    # cross-device first, the ring closing — no delta, no winner.
    assert heart["comparison_line"] == (
        "Synthetic Watch 60–71 bpm · Oura (API) 45–150 bpm"
    )
    # The ring's beat rows never re-list as generic heart facts, and the
    # catch-all relinquishes the overlap type.
    assert all(fact["label"] != "Heart rate" for fact in heart["facts"])
    assert payload["other_signals"] is None


def test_day_api_ring_api_beats_supersede_mirror_heart_rows(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_110000",
        [
            # The mirror's copy of the ring's beats — superseded on API days.
            _row(
                HR_TYPE,
                "2026-07-23T06:00:00-06:00",
                value="40",
                unit="count/min",
                source="Oura",
            ),
            _row(
                HR_TYPE,
                "2026-07-23T07:00:00-06:00",
                value="41",
                unit="count/min",
                source="Oura",
            ),
            # The mirror's HRV row carries a signal the API pipe has no row
            # type for — the exact-type match keeps it aggregating.
            _row(
                HRV_TYPE,
                "2026-07-23T06:30:00-06:00",
                value="52",
                unit="ms",
                source="Oura",
            ),
            # Genuine cross-device beats always stay.
            _row(
                HR_TYPE,
                "2026-07-23T12:00:00-06:00",
                value="88",
                unit="count/min",
                source="Synthetic Watch",
            ),
            # The API pipe carries the ring's beats.
            _hr_sample_row("20260723", "06:00", 47),
            _hr_sample_row("20260723", "06:05", 52),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260723").get_json()

    heart = payload["heart"]
    # Range and count fold the Watch and the API pipe only — the mirror's
    # 40/41 never widen the band.
    assert heart["heart_rate"]["count"] == 3
    assert heart["heart_rate"]["min"] == 47.0
    assert heart["heart_rate"]["max"] == 88.0
    # Post-supersede the ring is the API pipe; the juxtaposition names it.
    assert heart["comparison_line"] == ("Synthetic Watch 88 bpm · Oura (API) 47–52 bpm")
    # The mirror's HRV row is NOT superseded (exact-type match only).
    facts = {fact["label"]: fact["value"] for fact in heart["facts"]}
    assert facts["Heart rate variability"] == "52 ms"
    # The audit drawer still lists every row from both pipes.
    assert payload["audit"]["types"]["HKQuantityTypeIdentifierHeartRate"] == 3
    assert payload["audit"]["types"][OURA_HR_SAMPLE_TYPE] == 2


def test_day_api_steps_treat_ring_api_and_mirror_as_one_device(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_120000",
        [
            # The mirror's step and energy rows for the ring — superseded.
            _row(
                STEP_TYPE,
                "2026-07-24T08:00:00-06:00",
                "2026-07-24T09:00:00-06:00",
                value="4000",
                unit="count",
                source="Oura",
            ),
            _row(
                STEP_TYPE,
                "2026-07-24T09:00:00-06:00",
                "2026-07-24T10:00:00-06:00",
                value="5000",
                unit="count",
                source="Oura",
            ),
            _row(
                "HKQuantityTypeIdentifierActiveEnergyBurned",
                "2026-07-24T08:00:00-06:00",
                "2026-07-24T09:00:00-06:00",
                value="300",
                unit="Cal",
                source="Oura",
            ),
            # Genuine cross-device sources with partial coverage.
            _row(
                STEP_TYPE,
                "2026-07-24T08:00:00-06:00",
                "2026-07-24T08:30:00-06:00",
                value="3000",
                unit="count",
                source="Synthetic Watch",
            ),
            _row(
                "HKQuantityTypeIdentifierActiveEnergyBurned",
                "2026-07-24T08:00:00-06:00",
                "2026-07-24T09:00:00-06:00",
                value="500",
                unit="Cal",
                source="Synthetic Watch",
            ),
            # The ring's API daily-activity document.
            _oura_row(
                OURA_ACTIVITY_TYPE,
                "20260724",
                value=85,
                unit="score",
                metadata={"steps": 9500, "active_calories": 320},
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260724").get_json()

    steps = payload["activity"]["steps"]
    # One ring device, the API pipe canonical: its daily total is the
    # figure, the mirror's 9,000 never sums in and never appears as a
    # second Oura source. Full-day coverage outranks the Watch half hour.
    assert steps["mode"] == "total"
    assert steps["total"] == 9500
    assert steps["source"] == "Oura (API)"
    assert steps["others"] == ["Synthetic Watch"]
    counters = {item["label"]: item for item in payload["activity"]["counters"]}
    # The mirror's energy rows are out too: the Watch total stands alone,
    # no "also contributed" suffix.
    assert counters["Active energy"]["value"] == "500 Cal"
    # Oura's activity score renders as an attributed number.
    assert counters["Daily activity"]["value"] == "85 · Oura's score"
    # Nothing leaks into the Other-signals catch-all.
    assert payload["other_signals"] is None


def test_day_api_mirror_activity_aggregates_when_no_api_rows(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_130000",
        [
            _row(
                STEP_TYPE,
                "2026-07-25T08:00:00-06:00",
                "2026-07-25T09:00:00-06:00",
                value="4000",
                unit="count",
                source="Oura",
            ),
            _row(
                STEP_TYPE,
                "2026-07-25T09:00:00-06:00",
                "2026-07-25T10:00:00-06:00",
                value="5000",
                unit="count",
                source="Oura",
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260725").get_json()

    # Backward compatible: without API rows the mirror aggregates exactly
    # as before the supersede seam covered activity.
    steps = payload["activity"]["steps"]
    assert steps["mode"] == "total"
    assert steps["total"] == 9000
    assert steps["source"] == "Oura"


def test_day_api_catch_all_relinquishes_oura_overlap_types(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_140000",
        [
            _hr_sample_row("20260726", "10:00", 64),
            _oura_row(
                OURA_ACTIVITY_TYPE,
                "20260726",
                value=70,
                unit="score",
                metadata={"steps": 4200},
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260726").get_json()

    # Both overlap types land in their families — never raw in Other
    # signals (the leak the owner saw).
    assert payload["other_signals"] is None
    assert payload["heart"]["heart_rate"]["label"] == "64 bpm"
    # Ring-only day: no cross-device source, so no comparison line.
    assert payload["heart"]["comparison_line"] is None
    assert payload["activity"]["steps"]["total"] == 4200
    assert payload["activity"]["steps"]["source"] == "Oura (API)"


def test_day_api_oura_workout_supersedes_mirror_but_keeps_watch_workout(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_145000",
        [
            _row(
                "HKWorkoutActivityTypeCycling",
                "2026-07-27T08:00:00-06:00",
                "2026-07-27T08:30:00-06:00",
                source="Synthetic Watch",
                kind="workout",
                metadata={
                    "totalDistance": 3.2,
                    "totalDistanceUnit": "mi",
                    "totalEnergyBurned": 210,
                    "totalEnergyBurnedUnit": "Cal",
                },
            ),
            # The same ring workout mirrored through Apple Health.
            _row(
                "HKWorkoutActivityTypeWalking",
                "2026-07-27T10:00:00-06:00",
                "2026-07-27T10:25:00-06:00",
                source="Oura",
                kind="workout",
                metadata={
                    "totalDistance": 1100,
                    "totalDistanceUnit": "m",
                    "totalEnergyBurned": 70,
                    "totalEnergyBurnedUnit": "Cal",
                },
            ),
            # The canonical API pipe for the ring workout.
            _oura_row(
                OURA_WORKOUT_TYPE,
                "20260727",
                kind="workout",
                start="2026-07-27T10:00:00-06:00",
                end="2026-07-27T10:25:00-06:00",
                metadata={"activity": "walking", "distance": 1200, "calories": 80},
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260727").get_json()

    workouts = payload["activity"]["workouts"]
    assert [item["name"] for item in workouts] == ["Cycling", "Walking"]
    assert [item["source"] for item in workouts] == ["Synthetic Watch", "Oura (API)"]
    assert workouts[1]["metrics_label"] == "1,200.0 m · 80 Cal"
    assert payload["activity"]["workout_summary"] == "Cycling · Walking"
    assert payload["sources"]["names"] == ["Oura (API)", "Synthetic Watch"]
    # The audit drawer names the mirrored Oura workout, but aggregation
    # and source chips use one canonical ring pipe.
    assert payload["audit"]["types"] == {
        "HKWorkoutActivityTypeCycling": 1,
        "HKWorkoutActivityTypeWalking": 1,
        "oura.workout": 1,
    }

    source = _function_source(_workspace_source(), "renderActivityCard")
    assert "workout.name" in source
    assert "workout.source" in source
    assert "workout.metrics_label" in source

    window = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-27T07:00:00-06:00&to=2026-07-27T11:00:00-06:00"
    ).get_json()
    assert [item["name"] for item in window["workouts"]] == ["Cycling", "Walking"]
    assert [item["source"] for item in window["workouts"]] == [
        "Synthetic Watch",
        "Oura (API)",
    ]


def test_day_api_oura_cardio_vo2_and_audit_only_details(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_145500",
        [
            _oura_row(
                OURA_CARDIOVASCULAR_AGE_TYPE,
                "20260728",
                value=34,
                unit="years",
                metadata={"pulse_wave_velocity": 7.8},
            ),
            _oura_row(OURA_VO2_MAX_TYPE, "20260728", value=42, unit="mL/kg/min"),
            _oura_row(
                OURA_SESSION_TYPE,
                "20260728",
                kind="session",
                start="2026-07-28T07:30:00-06:00",
                end="2026-07-28T07:45:00-06:00",
                metadata={"type": "meditation", "mood": "good"},
            ),
            _oura_row(
                OURA_TAG_TYPE,
                "20260728",
                kind="tag",
                start="2026-07-28T09:15:00-06:00",
                metadata={"custom_name": "caffeine", "comment": "owner note"},
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260728").get_json()

    facts = {fact["label"]: fact["value"] for fact in payload["heart"]["facts"]}
    assert facts["Vascular age"] == "34 · Oura's estimate"
    assert facts["VO2 max"] == "42 mL/kg/min · Oura's estimate"
    # Sessions/tags are deliberately audit-only, and the new heart seats
    # keep their raw Oura identifiers out of Other signals.
    assert payload["other_signals"] is None
    assert payload["audit"]["oura_appendix"] == [
        {"label": "Pulse-wave velocity", "detail": "7.8 m/s · Oura's measurement"},
        {"label": "Meditation", "detail": "7:30 AM – 7:45 AM · Oura (API) · mood Good"},
        {"label": "Caffeine", "detail": "9:15 AM · Oura (API) · note present"},
    ]

    source = _function_sources("renderHeartCard", "renderDayAudit")
    assert "fact.label" in source
    assert "fact.value" in source
    assert "oura_appendix" in source
    assert "Sessions and tags stay here until a day-card use is clear." in source


def test_day_api_sleep_score_contributors_join_sleep_card(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_150000",
        [
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                "20260727",
                value=27000,
                unit="s",
                start="2026-07-27T00:00:00-06:00",
                end="2026-07-27T07:30:00-06:00",
                kind="sleep_period",
            ),
            _oura_row(
                OURA_SLEEP_SCORE_TYPE,
                "20260727",
                value=88,
                unit="score",
                metadata={
                    "contributors": {
                        "deep_sleep": 92,
                        "efficiency": 85,
                        "latency": 60,
                        "rem_sleep": 74,
                        "restfulness": 55,
                        "timing": 61,
                        "total_sleep": 90,
                    }
                },
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260727").get_json()

    sleep = payload["sleep"]
    assert sleep["score_line"] == "Sleep score 88 · Oura's score"
    # The sleep score's anatomy: Oura's contributor numbers with the
    # shared owner-facing labels, in stable key order.
    assert sleep["score_contributors"] == [
        {"label": "Deep sleep", "value": 92},
        {"label": "Efficiency", "value": 85},
        {"label": "Latency", "value": 60},
        {"label": "REM sleep", "value": 74},
        {"label": "Restfulness", "value": 55},
        {"label": "Timing", "value": 61},
        {"label": "Total sleep", "value": 90},
    ]
    # Ring-only night: no cross-device source, so no comparison line.
    assert sleep["comparison_line"] is None


def test_day_api_recovery_contributors_full_anatomy(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_160000",
        [
            _oura_row(
                OURA_READINESS_TYPE,
                "20260728",
                value=82,
                unit="score",
                metadata={
                    "contributors": {
                        "activity_balance": 77,
                        "body_temperature": 98,
                        "hrv_balance": 88,
                        "previous_day_activity": 90,
                        "previous_night": 66,
                        "recovery_index": 84,
                        "resting_heart_rate": 92,
                        "sleep_balance": 71,
                    },
                    "temperature_trend_deviation": 0.1,
                },
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260728").get_json()

    assert payload["recovery"]["contributors"] == [
        {"label": "Activity balance", "value": 77},
        {"label": "Body temperature", "value": 98},
        {"label": "HRV balance", "value": 88},
        {"label": "Previous day activity", "value": 90},
        {"label": "Previous night", "value": 66},
        {"label": "Recovery index", "value": 84},
        {"label": "Resting heart rate", "value": 92},
        {"label": "Sleep balance", "value": 71},
    ]


def test_day_api_sleep_comparison_line_requires_both_devices(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_170000",
        [
            _row(
                SLEEP_TYPE,
                "2026-07-29T00:10:00-06:00",
                "2026-07-29T08:08:00-06:00",
                value="HKCategoryValueSleepAnalysisAsleepUnspecified",
                source="Synthetic Watch",
            ),
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                "20260729",
                value=29400,
                unit="s",
                start="2026-07-29T00:00:00-06:00",
                end="2026-07-29T08:10:00-06:00",
                kind="sleep_period",
            ),
        ],
    )

    payload = env.client.get("/app/body/api/day/20260729").get_json()

    # Both devices measured the night: the juxtaposition states both
    # spans verbatim, cross-device first, the ring closing.
    assert payload["sleep"]["comparison_line"] == (
        "Synthetic Watch saw 7h 58m · Oura (API) saw 8h 10m"
    )


# --- Round-2: "vs your typical" self-baselines ---------------------------------


def _seed_typical_history(journal: Path) -> None:
    """Twenty June days of history plus a July 15 target day.

    Readiness 61–80, sleep score 71–90, nightly sleep 7h, resting HR
    cycling 50–54 — enough density for every 90-day median, with target-
    day values chosen to prove the day never joins its own baseline.
    """
    rows: list[dict] = []
    for i in range(1, 21):
        day = f"202606{i:02d}"
        iso = f"2026-06-{i:02d}"
        rows.append(_oura_row(OURA_READINESS_TYPE, day, value=60 + i, unit="score"))
        rows.append(_oura_row(OURA_SLEEP_SCORE_TYPE, day, value=70 + i, unit="score"))
        rows.append(
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                day,
                value=25200,
                unit="s",
                start=f"{iso}T00:00:00-06:00",
                end=f"{iso}T07:00:00-06:00",
                kind="sleep_period",
            )
        )
        rows.append(
            _row(
                RESTING_HR_TYPE,
                f"{iso}T07:00:00-06:00",
                value=str(50 + (i % 5)),
                unit="count/min",
                source="Synthetic Watch",
            )
        )
    rows.append(_oura_row(OURA_READINESS_TYPE, "20260715", value=82, unit="score"))
    rows.append(_oura_row(OURA_SLEEP_SCORE_TYPE, "20260715", value=95, unit="score"))
    rows.append(
        _oura_row(
            OURA_SLEEP_PERIOD_TYPE,
            "20260715",
            value=30600,
            unit="s",
            start="2026-07-15T00:00:00-06:00",
            end="2026-07-15T08:30:00-06:00",
            kind="sleep_period",
        )
    )
    rows.append(
        _row(
            RESTING_HR_TYPE,
            "2026-07-15T07:00:00-06:00",
            value="58",
            unit="count/min",
            source="Synthetic Watch",
        )
    )
    _seed_import(journal, "20260906_180000", rows)


def test_day_api_typical_medians_ride_facts_after_trends_warm(body_env):
    env = body_env()
    _seed_typical_history(env.journal)

    # Cold trends cache: the day payload carries no baselines at all —
    # day pages never block on (or kick) the all-shards fold.
    cold = env.client.get("/app/body/api/day/20260715").get_json()
    assert "typical" not in cold["recovery"]["facts"][0]
    assert "score_typical" not in cold["sleep"]
    assert "asleep_typical" not in cold["sleep"]

    _trends_after_warm(env.client)

    payload = env.client.get("/app/body/api/day/20260715").get_json()
    readiness_fact = payload["recovery"]["facts"][0]
    # The window is the 90 days strictly before the day: the day's own
    # 82 never joins its own baseline (61–80 → median 70.5).
    assert readiness_fact["typical"] == "70.5"
    assert readiness_fact["typical_label"] == "your 90-day median 70.5"
    sleep = payload["sleep"]
    assert sleep["score_typical"] == "80.5"
    assert sleep["score_typical_label"] == "your 90-day median 80.5"
    assert sleep["asleep_typical"] == "7h 00m"
    assert sleep["asleep_typical_label"] == "your 90-day median 7h 00m"
    heart_facts = {fact["label"]: fact for fact in payload["heart"]["facts"]}
    resting = heart_facts["Resting heart rate"]
    assert resting["value"] == "58 bpm"
    assert resting["typical"] == "52 bpm"
    assert resting["typical_label"] == "your 90-day median 52 bpm"


def test_day_api_typical_absent_below_value_floor(body_env):
    env = body_env()
    rows = [
        _oura_row(OURA_READINESS_TYPE, f"202607{i:02d}", value=70 + i, unit="score")
        for i in range(1, 6)
    ]
    rows.append(_oura_row(OURA_READINESS_TYPE, "20260715", value=82, unit="score"))
    _seed_import(env.journal, "20260906_190000", rows)

    _trends_after_warm(env.client)

    payload = env.client.get("/app/body/api/day/20260715").get_json()
    fact = payload["recovery"]["facts"][0]
    # Five prior days is not "typical": below the 14-value floor the
    # baseline stays absent even with a warm cache.
    assert "typical" not in fact
    assert "typical_label" not in fact


# --- Round-2: new trends ribbons ------------------------------------------------


def test_trends_sleep_score_temp_and_stress_ribbons(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_200000",
        [
            _oura_row(OURA_SLEEP_SCORE_TYPE, "20260601", value=88, unit="score"),
            _oura_row(OURA_SLEEP_SCORE_TYPE, "20260602", value=74, unit="score"),
            _oura_row(OURA_TEMP_DEV_TYPE, "20260601", value=-0.21, unit="degC"),
            _oura_row(OURA_TEMP_DEV_TYPE, "20260602", value=0.34, unit="degC"),
            _oura_row(
                OURA_STRESS_TYPE,
                "20260601",
                value="normal",
                metadata={"stress_high": 7200, "recovery_high": 20400},
            ),
            _oura_row(
                OURA_STRESS_TYPE,
                "20260602",
                value="stressful",
                metadata={"stress_high": 5430},
            ),
        ],
    )

    payload = _trends_after_warm(env.client)

    # The three new ribbons hold their fixed order slots; signals the
    # journal has never held draw no ribbon.
    assert [signal["key"] for signal in payload["signals"]] == [
        "sleep_score",
        "temp_deviation",
        "stress_high_minutes",
    ]
    by_key = {signal["key"]: signal for signal in payload["signals"]}
    score = by_key["sleep_score"]
    assert score["label"] == "Sleep score"
    # Oura's score is unitless — plain numbers, like readiness.
    assert score["unit_label"] == ""
    assert score["daily"] == [["20260601", 88.0], ["20260602", 74.0]]
    temp = by_key["temp_deviation"]
    assert temp["label"] == "Temperature deviation"
    assert temp["unit_label"] == "°C"
    # Signed values verbatim — a genuinely negative-capable ribbon.
    assert temp["daily"] == [["20260601", -0.21], ["20260602", 0.34]]
    stress = by_key["stress_high_minutes"]
    assert stress["label"] == "Daytime stress high"
    # Values travel as minutes against the "h" duration unit label,
    # mirroring the asleep ribbon's convention.
    assert stress["unit_label"] == "h"
    assert stress["daily"] == [["20260601", 120.0], ["20260602", 90.5]]
    assert stress["coverage"] == {
        "first_day": "20260601",
        "last_day": "20260602",
        "days": 2,
    }


def test_trends_steps_use_ring_api_total_over_mirror(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_210000",
        [
            # June 3: mirror-only — the ring's mirror steps still total.
            _row(
                STEP_TYPE,
                "2026-06-03T08:00:00-06:00",
                "2026-06-03T09:00:00-06:00",
                value="4000",
                unit="count",
                source="Oura",
            ),
            # June 5: the API document carries the canonical total; the
            # mirror's rows stay out (one device, API pipe canonical).
            _row(
                STEP_TYPE,
                "2026-06-05T08:00:00-06:00",
                "2026-06-05T09:00:00-06:00",
                value="8000",
                unit="count",
                source="Oura",
            ),
            _oura_row(
                OURA_ACTIVITY_TYPE,
                "20260605",
                value=85,
                unit="score",
                metadata={"steps": 9500},
            ),
        ],
    )

    payload = _trends_after_warm(env.client)

    steps = next(s for s in payload["signals"] if s["key"] == "steps")
    assert steps["daily"] == [["20260603", 4000], ["20260605", 9500]]


def test_trends_vascular_age_ribbon_is_bare_age_without_typical(body_env):
    env = body_env()
    rows = [
        _oura_row(
            OURA_CARDIOVASCULAR_AGE_TYPE,
            f"202606{day:02d}",
            value=34 + (day % 2),
            unit="years",
        )
        for day in range(1, 17)
    ]
    _seed_import(env.journal, "20260906_220000", rows)

    payload = _trends_after_warm(env.client)

    assert [signal["key"] for signal in payload["signals"]] == ["vascular_age"]
    vascular = payload["signals"][0]
    assert vascular["label"] == "Vascular age"
    # The ribbon is years, but the unit label stays empty so the client
    # renders a bare number instead of "34 years".
    assert vascular["unit_label"] == ""
    assert vascular["daily"][0] == ["20260601", 35.0]
    assert vascular["daily"][-1] == ["20260616", 34.0]

    day_payload = env.client.get("/app/body/api/day/20260616").get_json()
    fact = day_payload["heart"]["facts"][0]
    assert fact == {
        "label": "Vascular age",
        "count": 1,
        "count_label": "1",
        "value": "34 · Oura's estimate",
    }


# --- Round-2 Oura display front-end: anatomy, medians, juxtaposition ----------
#
# Owned by the round-2 front-end surface (workspace.html). Assertions here
# pin the static fragment's renderer functions and pair them with API payload
# checks for the server-produced strings they render.


def test_score_anatomy_renderer_uses_drawer_contract():
    source = _function_sources(
        "renderScoreAnatomy",
        "anatomyDrawerLine",
        "renderSleepCard",
        "renderRecoveryCard",
    )

    # Shared renderer: both cards disclose through the same markup.
    assert source.count("function renderScoreAnatomy") == 1
    assert (
        'renderScoreAnatomy("body-sleep-anatomy", sleep.score_contributors)' in source
    )
    assert (
        'renderScoreAnatomy("body-recovery-anatomy", recovery.contributors)' in source
    )
    assert "window.Drawer.render" in source
    assert 'label: "why this score?"' in source
    assert "line: anatomyDrawerLine(items)" in source
    assert 'if (!items.length) return "";' in source
    assert "drawer-empty" not in source
    # The list is attributed to Oura, §13-style.
    assert "Oura\\'s contributors" in source

    lowered = _function_sources("renderScoreAnatomy", "anatomyDrawerLine").lower()
    for word in ("derived", "computed", "from ", "readings", "timestamp"):
        assert word not in lowered


def test_score_anatomy_guarded_on_contributors_before_rendering():
    source = _function_source(_workspace_source(), "renderScoreAnatomy")

    assert "const items = asArray(contributors);" in source
    assert 'if (!items.length) return "";' in source
    assert "window.Drawer.render" in source
    assert "drawer-empty" not in source


def test_fact_typical_median_rides_muted_never_colorized(body_env):
    env = body_env()
    _seed_typical_history(env.journal)
    _trends_after_warm(env.client)

    payload = env.client.get("/app/body/api/day/20260715").get_json()
    assert payload["recovery"]["facts"][0]["typical_label"] == (
        "your 90-day median 70.5"
    )
    assert payload["sleep"]["score_typical_label"] == "your 90-day median 80.5"
    assert payload["sleep"]["asleep_typical_label"] == "your 90-day median 7h 00m"
    resting = {fact["label"]: fact for fact in payload["heart"]["facts"]}[
        "Resting heart rate"
    ]
    assert resting["typical_label"] == "your 90-day median 52 bpm"

    # The muted class never colorizes: faint ink only, no orange — the
    # median is stated, never graded.
    source = _trends_workspace_source()
    renderer = _function_sources(
        "typicalSpan",
        "renderSleepCard",
        "renderHeartCard",
        "renderRecoveryCard",
    )
    assert 'class="body-fact-typical' in renderer
    assert "typical_label" in renderer
    rule_at = source.index(".body-fact-typical {")
    rule = source[rule_at : source.index("}", rule_at)]
    assert "var(--ink-faint-paper)" in rule
    assert "orange" not in rule


def test_fact_typical_absent_without_key(body_env):
    env = body_env()
    rows = [
        _oura_row(OURA_READINESS_TYPE, f"202607{i:02d}", value=70 + i, unit="score")
        for i in range(1, 6)
    ]
    rows.append(_oura_row(OURA_READINESS_TYPE, "20260715", value=82, unit="score"))
    _seed_import(env.journal, "20260906_190000", rows)

    _trends_after_warm(env.client)

    fact = env.client.get("/app/body/api/day/20260715").get_json()["recovery"]["facts"][
        0
    ]
    assert "typical" not in fact
    assert "typical_label" not in fact

    source = _function_sources(
        "typicalSpan",
        "renderSleepCard",
        "renderHeartCard",
        "renderRecoveryCard",
    )
    assert "typical_label" in source
    assert "typicalSpan(" in source


def test_attributed_adjective_line_renders_as_plain_fact_row(body_env):
    # Pre-formatted attributed adjectives are ordinary facts to the
    # renderer: the same row markup as every number line, no special case.
    env = body_env()
    _seed_recovery_day(env.journal)

    recovery = env.client.get("/app/body/api/day/20260715").get_json()["recovery"]
    resilience = next(
        fact for fact in recovery["facts"] if fact["label"] == "Resilience"
    )
    assert resilience["detail"] == "solid · Oura's label"

    source = _function_source(_workspace_source(), "renderRecoveryCard")
    assert "asArray(recovery.facts).map((fact)" in source
    assert "<li><span>${escapeHtml(fact.label)}</span>" in source
    assert "escapeHtml(fact.detail)" in source


def test_device_comparison_lines_render_only_when_server_sends_them(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260906_170000",
        [
            _row(
                SLEEP_TYPE,
                "2026-07-29T00:10:00-06:00",
                "2026-07-29T08:08:00-06:00",
                value="HKCategoryValueSleepAnalysisAsleepUnspecified",
                source="Synthetic Watch",
            ),
            _oura_row(
                OURA_SLEEP_PERIOD_TYPE,
                "20260729",
                value=29400,
                unit="s",
                start="2026-07-29T00:00:00-06:00",
                end="2026-07-29T08:10:00-06:00",
                kind="sleep_period",
            ),
            _row(
                HR_TYPE,
                "2026-07-29T06:00:00-06:00",
                value="60",
                unit="count/min",
                source="Synthetic Watch",
            ),
            _hr_sample_row("20260729", "06:00", 51),
            _row(
                SLEEP_TYPE,
                "2026-07-30T00:10:00-06:00",
                "2026-07-30T08:08:00-06:00",
                value="HKCategoryValueSleepAnalysisAsleepUnspecified",
                source="Synthetic Watch",
            ),
        ],
    )

    with_lines = env.client.get("/app/body/api/day/20260729").get_json()
    assert with_lines["sleep"]["comparison_line"] == (
        "Synthetic Watch saw 7h 58m · Oura (API) saw 8h 10m"
    )
    assert with_lines["heart"]["comparison_line"] == (
        "Synthetic Watch 60 bpm · Oura (API) 51 bpm"
    )
    without_lines = env.client.get("/app/body/api/day/20260730").get_json()
    assert without_lines["sleep"]["comparison_line"] is None

    source = _function_sources("renderSleepCard", "renderHeartCard")
    assert source.count('class="body-muted body-num body-device-compare"') == 3
    assert "sleep.comparison_line ?" in source
    assert "heart.comparison_line ?" in source
    assert "heart.resting_comparison_line ?" in source


def test_trends_new_signal_units_format_score_degrees_and_minutes():
    source = _trends_workspace_source()

    # temp_deviation: signed °C, two decimals, the day card's convention.
    assert 'if (signal.key === "temp_deviation")' in source
    assert "let deg = value.toFixed(2);" in source
    assert 'if (deg.charAt(0) !== "-") deg = `+${deg}`;' in source
    assert 'return `${deg} ${signal.unit_label || "°C"}`;' in source

    # stress_high_minutes: minute totals as "2h 00m" / "45m". The key
    # branch must sit before the generic "h" unit path — the server
    # labels the signal "h", which would otherwise render decimal hours.
    assert (
        'if (signal.key === "stress_high_minutes") return formatMinutes(value);'
        in source
    )
    assert source.index('if (signal.key === "stress_high_minutes")') < source.index(
        'if (signal.unit_label === "h")'
    )
    assert "function formatMinutes(minutes)" in source
    assert 'return `${hours}h ${String(mins).padStart(2, "0")}m`;' in source
    assert "return `${mins}m`;" in source

    # Scores stay bare: sleep_score takes the same empty-unit-label
    # fallback readiness already uses — no special branch.
    assert "sleep_score" not in _function_source(source, "formatValue")
    assert "readiness" not in _function_source(source, "formatValue")
    assert "return `${Math.round(value)}${signal.unit_label" in source

    # Pre-data, a signal in the payload with no points draws the calm
    # placeholder on a disabled ribbon — no canvas to open.
    assert '"not present yet"' in source
    assert 'hasData ? "" : " disabled"' in source


def test_trends_fine_scale_domain_keeps_temp_deviation_legible():
    source = _trends_workspace_source()

    # Temperature deviation lives within ~±1 °C; the integer domain
    # convention would flatten it, so it takes a fractional pad and
    # one-decimal outward rounding — and, being genuinely signed, it
    # keeps a negative floor (the zero clamp guards vMin >= 0 only).
    assert "function fineScale(signal)" in source
    assert 'return signal.key === "temp_deviation";' in source
    assert "pad = Math.max((vMax - vMin) * 0.08, 0.1);" in source
    assert "lo = Math.floor((vMin - pad) * 10) / 10;" in source
    assert "hi = Math.ceil((vMax + pad) * 10) / 10;" in source
    # Both call sites pass the signal-aware flag.
    assert "paddedDomain(points, fineScale(signal))" in source
    assert "paddedDomain(inWindow, fineScale(signal));" in source


# --- Ring resting HR: one ribbon, attributed day facts (oura.sleep) -----------
#
# The watch's RestingHeartRate channel went quiet; the ring measures the
# same physiological quantity nightly (``lowest_heart_rate`` in each
# ``oura.sleep`` period's metadata, beside ``average_heart_rate``). O-5C
# cross-device rule: one resting_hr signal, never double-counted — the
# genuine measurement present wins, the watch staying primary on days
# both measured.


def _ring_sleep_period(
    day: str,
    *,
    lowest: float | int | None = None,
    average: float = 62.0,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """An ``oura.sleep`` period row shaped like the live normalized shards."""
    iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    metadata: dict = {"average_heart_rate": average, "type": "long_sleep"}
    if lowest is not None:
        metadata["lowest_heart_rate"] = lowest
    return _oura_row(
        OURA_SLEEP_PERIOD_TYPE,
        day,
        value=24480,
        unit="s",
        start=start or f"{iso}T00:30:00-06:00",
        end=end or f"{iso}T07:30:00-06:00",
        kind="sleep_period",
        metadata=metadata,
    )


def test_trends_resting_hr_ribbon_fills_watch_gap_with_ring_lowest(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260907_000000",
        [
            # June 1 — both devices measured: the watch's reading stays
            # primary; the ring's lowest never averages in.
            _row(
                RESTING_HR_TYPE,
                "2026-06-01T21:00:00-06:00",
                value="58",
                unit="count/min",
                source="Synthetic Watch",
            ),
            _ring_sleep_period("20260601", lowest=51),
            # June 2 — ring only, two periods: the day's minimum wins.
            _ring_sleep_period("20260602", lowest=53),
            _ring_sleep_period(
                "20260602",
                lowest=51,
                start="2026-06-02T14:00:00-06:00",
                end="2026-06-02T15:00:00-06:00",
            ),
            # June 3 — unmeasured periods: Oura writes zeros into dozes it
            # did not measure; neither a missing field nor a zero ever
            # fabricates a resting value, so the day stays absent.
            _ring_sleep_period("20260603", lowest=None, average=0.0),
            _ring_sleep_period(
                "20260603",
                lowest=0,
                start="2026-06-03T14:00:00-06:00",
                end="2026-06-03T15:00:00-06:00",
            ),
            # June 4 — watch only.
            _row(
                RESTING_HR_TYPE,
                "2026-06-04T07:00:00-06:00",
                value="60",
                unit="count/min",
                source="Synthetic Watch",
            ),
        ],
    )

    payload = _trends_after_warm(env.client)

    # One ribbon — the same physiological quantity measured by whichever
    # device was present, never a second ring-only signal.
    keys = [signal["key"] for signal in payload["signals"]]
    assert keys.count("resting_hr") == 1
    resting = next(s for s in payload["signals"] if s["key"] == "resting_hr")
    assert resting["label"] == "Resting heart rate"
    assert resting["unit_label"] == "bpm"
    assert resting["daily"] == [
        ["20260601", 58.0],
        ["20260602", 51.0],
        ["20260604", 60.0],
    ]


def test_day_api_resting_fact_ring_only_day_attributes_oura(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260907_010000",
        [_ring_sleep_period("20260610", lowest=51)],
    )

    payload = env.client.get("/app/body/api/day/20260610").get_json()

    heart = payload["heart"]
    assert heart is not None
    resting = {fact["label"]: fact for fact in heart["facts"]}["Resting heart rate"]
    # §13 attributed-fact style, same register as the recovery card.
    assert resting["value"] == "51 bpm · Oura's measurement"
    assert heart["resting_comparison_line"] is None

    source = _function_source(_workspace_source(), "renderHeartCard")
    assert "escapeHtml(fact.value)" in source


def test_day_api_resting_fact_keeps_watch_primary_and_juxtaposes_ring(body_env):
    env = body_env()
    _seed_import(
        env.journal,
        "20260907_020000",
        [
            # June 11 — both devices measured.
            _row(
                RESTING_HR_TYPE,
                "2026-06-11T21:00:00-06:00",
                value="58",
                unit="count/min",
                source="Synthetic Watch",
            ),
            _ring_sleep_period("20260611", lowest=51),
            # June 12 — watch only: no juxtaposition, no ring fact.
            _row(
                RESTING_HR_TYPE,
                "2026-06-12T07:00:00-06:00",
                value="60",
                unit="count/min",
                source="Synthetic Watch",
            ),
        ],
    )

    both = env.client.get("/app/body/api/day/20260611").get_json()["heart"]
    resting_facts = [
        fact for fact in both["facts"] if fact["label"] == "Resting heart rate"
    ]
    # One fact: the genuine cross-device reading stays primary and
    # unattributed to the ring; the ring's figure rides the day card's
    # comparison-line pattern — juxtaposition, no verdict.
    assert len(resting_facts) == 1
    assert resting_facts[0]["value"] == "58 bpm"
    assert both["resting_comparison_line"] == (
        "Synthetic Watch 58 bpm · Oura (API) 51 bpm"
    )

    source = _function_source(_workspace_source(), "renderHeartCard")
    assert "heart.resting_comparison_line" in source

    watch_only = env.client.get("/app/body/api/day/20260612").get_json()["heart"]
    resting = {f["label"]: f for f in watch_only["facts"]}["Resting heart rate"]
    assert resting["value"] == "60 bpm"
    assert watch_only["resting_comparison_line"] is None
    assert "Oura's measurement" not in json.dumps(watch_only)


def test_day_api_ring_resting_fact_carries_typical_after_warm(body_env):
    env = body_env()
    _seed_typical_history(env.journal)
    # July 16 — ring only; its own value never joins its baseline.
    _seed_import(
        env.journal,
        "20260907_030000",
        [_ring_sleep_period("20260716", lowest=51)],
    )

    _trends_after_warm(env.client)

    payload = env.client.get("/app/body/api/day/20260716").get_json()
    resting = {f["label"]: f for f in payload["heart"]["facts"]}["Resting heart rate"]
    assert resting["value"] == "51 bpm · Oura's measurement"
    # 21 prior daily values (20 watch June days + July 15) → median 52.
    assert resting["typical_label"] == "your 90-day median 52 bpm"


# --- Source-freshness sentinel ------------------------------------------------
#
# For each configured source the status payload states days-since-last-data;
# the overview names quiet sources in a slim muted strip and lists every
# configured source's last-delivered day in the coverage area. Copy is §13-
# factual: names, dates, day counts — never advice, never alarm styling.
# Fixtures seed days relative to the real clock so the assertions hold on
# any run date.

QUIET_DAY_EXPECTATIONS = {
    "Oura": 2,
    "Test Phone": 4,
    "Stelo": 4,
    "Lingo": 7,
}


def _seed_quiet_day_expectations(
    journal: Path,
    expectations: dict[str, int] | None = None,
) -> None:
    config_path = journal / "config" / "journal.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("body", {}).setdefault("freshness", {})["quiet_days"] = dict(
        expectations or QUIET_DAY_EXPECTATIONS
    )
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _day_key_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).strftime("%Y%m%d")


def _delivery_row(source: str, days_ago: int) -> dict:
    day = _day_key_ago(days_ago)
    iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    return _row(
        GLUCOSE_TYPE,
        f"{iso}T08:00:00-06:00",
        value="100",
        unit="mg/dL",
        source=source,
    )


def test_status_api_freshness_names_quiet_source_factually(body_env):
    env = body_env()
    _seed_quiet_day_expectations(env.journal)
    _seed_import(
        env.journal,
        "20260907_040000",
        [
            _delivery_row("Stelo", 6),
            # The ring delivers through its API pipe — the "Oura (API)"
            # label must satisfy the "Oura" expectation.
            _oura_row(OURA_READINESS_TYPE, _day_key_ago(1), value=82, unit="score"),
            # The source label may carry extra model text; the config key
            # still matches by substring.
            _delivery_row("Test Phone Pro", 1),
            _delivery_row("Lingo", 3),
        ],
    )

    freshness = env.client.get("/app/body/api/status").get_json()["freshness"]

    by_name = {source["name"]: source for source in freshness["sources"]}
    assert set(by_name) == set(QUIET_DAY_EXPECTATIONS)
    stelo = by_name["Stelo"]
    assert stelo["quiet"] is True
    assert stelo["days_since"] == 6
    assert stelo["quiet_after_days"] == 4
    assert stelo["last_day"] == _day_key_ago(6)
    assert stelo["line"] == "Stelo last delivered 6 days ago"
    assert by_name["Oura"]["quiet"] is False
    assert by_name["Oura"]["days_since"] == 1
    assert by_name["Test Phone"]["quiet"] is False
    assert by_name["Lingo"]["quiet"] is False
    assert freshness["quiet"] is True
    assert freshness["quiet_lines"] == ["Stelo last delivered 6 days ago"]

    freshness_strings = [text.lower() for text in _collect_strings(freshness)]
    assert "stelo last delivered 6 days ago" in freshness_strings
    # §13: facts only — the banner never advises.
    assert all("you should" not in text for text in freshness_strings)
    assert all("check your" not in text for text in freshness_strings)

    source = _function_source(_workspace_source(), "renderFreshnessStrip")
    # The strip is present only for quiet lines, muted, and states the
    # server fact verbatim.
    assert 'aria-label="Source freshness"' in source
    assert "quiet_lines" in source
    assert 'if (!lines.length) return "";' in source


def test_status_api_freshness_states_no_data_for_absent_source(body_env):
    env = body_env()
    _seed_quiet_day_expectations(env.journal)
    _seed_import(env.journal, "20260907_050000", [_delivery_row("Stelo", 1)])

    freshness = env.client.get("/app/body/api/status").get_json()["freshness"]

    by_name = {source["name"]: source for source in freshness["sources"]}
    lingo = by_name["Lingo"]
    assert lingo["last_day"] is None
    assert lingo["days_since"] is None
    assert lingo["quiet"] is True
    cap = body_routes.FRESHNESS_SCAN_MONTH_CAP
    assert lingo["line"] == f"Lingo — no data in the last {cap} months"
    assert by_name["Stelo"]["quiet"] is False

    freshness_strings = [text.lower() for text in _collect_strings(freshness)]
    assert f"lingo — no data in the last {cap} months".lower() in freshness_strings
    assert all("you should" not in text for text in freshness_strings)
    assert all("check your" not in text for text in freshness_strings)


def test_overview_freshness_banner_absent_when_sources_fresh(body_env):
    env = body_env()
    _seed_quiet_day_expectations(env.journal)
    _seed_import(
        env.journal,
        "20260907_060000",
        [
            _delivery_row("Stelo", 1),
            _delivery_row("Lingo", 1),
            _delivery_row("Test Phone", 1),
            _oura_row(OURA_READINESS_TYPE, _day_key_ago(1), value=82, unit="score"),
        ],
    )

    status = env.client.get("/app/body/api/status").get_json()
    assert status["freshness"]["quiet"] is False
    assert status["freshness"]["quiet_lines"] == []

    strip_source = _function_source(_workspace_source(), "renderFreshnessStrip")
    assert 'aria-label="Source freshness"' in strip_source
    assert 'if (!lines.length) return "";' in strip_source
    # The coverage-area Sources block still lists every expected source
    # with its last-delivered day.
    sources_source = _function_source(_workspace_source(), "renderFreshnessSources")
    assert 'id="body-sources-fresh-title"' in sources_source
    assert "Expected sources" in sources_source
    for name in QUIET_DAY_EXPECTATIONS:
        assert name in {source["name"] for source in status["freshness"]["sources"]}
    yesterday_label = body_routes._format_day_long(_day_key_ago(1))
    assert all(
        source["detail"] == f"{yesterday_label} · 1 day ago"
        for source in status["freshness"]["sources"]
    )


def test_freshness_empty_journal_stays_silent(body_env):
    env = body_env()
    _seed_quiet_day_expectations(env.journal)

    status = env.client.get("/app/body/api/status").get_json()

    # A journal that has never held body data has no sources to go
    # quiet: the sentinel stays empty rather than naming four absences.
    assert status["freshness"] == {"sources": [], "quiet_lines": [], "quiet": False}

    source = _function_sources("renderFreshnessStrip", "renderFreshnessSources")
    assert 'aria-label="Source freshness"' in source
    assert 'id="body-sources-fresh-title"' in source
    assert source.count('if (!lines.length) return "";') == 1
    assert source.count('if (!sources.length) return "";') == 1


def test_freshness_scan_is_calendar_bounded_and_signature_cached(body_env, monkeypatch):
    env = body_env()
    _seed_quiet_day_expectations(env.journal)
    old_day = _day_key_ago(250)
    iso = f"{old_day[:4]}-{old_day[4:6]}-{old_day[6:8]}"
    _seed_import(
        env.journal,
        "20260907_070000",
        [
            # Stelo's only delivery sits ~8 months back — outside the
            # bounded window the sentinel is allowed to walk.
            _row(
                GLUCOSE_TYPE,
                f"{iso}T08:00:00-06:00",
                value="100",
                unit="mg/dL",
                source="Stelo",
            ),
            _delivery_row("Lingo", 1),
        ],
    )

    freshness = env.client.get("/app/body/api/status").get_json()["freshness"]
    by_name = {source["name"]: source for source in freshness["sources"]}
    cap = body_routes.FRESHNESS_SCAN_MONTH_CAP
    # Never a full-archive scan: the fold states the fact it verified
    # within the window instead of walking back to the old shard.
    assert by_name["Stelo"]["last_day"] is None
    assert by_name["Stelo"]["line"] == f"Stelo — no data in the last {cap} months"
    assert by_name["Lingo"]["days_since"] == 1

    # Signature-cached (the trends pattern): with no new import, a second
    # status call serves the fold from cache without touching shards.
    def _boom(*args, **kwargs):
        raise AssertionError("freshness fold must serve from its cache")

    monkeypatch.setattr(body_routes, "_recent_month_keys", _boom)
    second = env.client.get("/app/body/api/status").get_json()["freshness"]
    assert second == freshness


def test_freshness_unconfigured_journal_with_body_data_stays_dormant(body_env):
    env = body_env()
    _seed_import(env.journal, "20260907_080000", [_delivery_row("Stelo", 6)])

    status = env.client.get("/app/body/api/status").get_json()

    assert status["freshness"] == {"sources": [], "quiet_lines": [], "quiet": False}
