# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date as real_date
from datetime import timedelta
from pathlib import Path

import pytest

from solstone.apps.tokens import routes as token_routes


def _day(offset: int, today: real_date | None = None) -> str:
    base = today or real_date.today()
    return (base - timedelta(days=offset)).strftime("%Y%m%d")


def _entry(model: str, total_tokens: int) -> dict:
    return {
        "timestamp": 1772676000.0,
        "model": model,
        "context": "think.cortex.flow:42",
        "usage": {
            "input_tokens": total_tokens // 2,
            "output_tokens": total_tokens // 2,
            "total_tokens": total_tokens,
        },
    }


def _patch_token_cost(monkeypatch):
    def calc_cost(entry: dict) -> dict:
        total_tokens = entry.get("usage", {}).get("total_tokens", 0) or 0
        return {
            "total_cost": total_tokens / 10000,
            "input_cost": 0.0,
            "output_cost": total_tokens / 10000,
            "currency": "USD",
        }

    monkeypatch.setattr(token_routes, "calc_token_cost", calc_cost)


def _tokens_snapshot(journal: Path) -> dict[Path, int]:
    tokens = journal / "tokens"
    if not tokens.exists():
        return {}
    return {path: path.stat().st_mtime_ns for path in sorted(tokens.rglob("*"))}


def _static_empty_cell(html: str, tbody_id: str) -> str:
    match = re.search(
        rf'<tbody id="{re.escape(tbody_id)}">\s*'
        r'<tr class="empty-row">\s*'
        r'<td colspan="\d+">([^<]+)</td>\s*'
        r"</tr>\s*</tbody>",
        html,
        re.S,
    )
    assert match is not None, tbody_id
    return match.group(1)


def _dynamic_empty_cell(html: str, function_name: str) -> str:
    start = html.index(f"function {function_name}")
    next_function = html.find("\nfunction ", start + 1)
    body = html[start : next_function if next_function != -1 else len(html)]
    match = re.search(
        r"tbody\.innerHTML = '<tr class=\"empty-row\"><td colspan=\"\d+\">"
        r"([^<]+)</td></tr>';",
        body,
    )
    assert match is not None, function_name
    return match.group(1)


def _tokens_workspace_html(tokens_env) -> str:
    env = tokens_env({})
    response = env.client.get("/app/tokens/workspace")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _tokens_copy_constants(html: str) -> dict[str, str]:
    start = html.index("  const TOKENS_COPY = {")
    end = html.index("  };", start)
    block = html[start:end]
    return {
        match.group("key"): match.group("value")
        for match in re.finditer(
            r"(?P<key>TOKENS_[A-Z_]+):\s*\"(?P<value>[^\"]*)\"",
            block,
        )
    }


def _extract_tokens_copy(html: str) -> str:
    start = html.index("  const TOKENS_COPY = {")
    end = html.index("  window.TOKENS_COPY = TOKENS_COPY;", start)
    return html[start:end].strip()


def _extract_function(html: str, function_name: str) -> str:
    start = html.index(f"function {function_name}")
    async_prefix_start = start - len("async ")
    if async_prefix_start >= 0 and html[async_prefix_start:start] == "async ":
        start = async_prefix_start
    next_function = html.find("\nfunction ", start + 1)
    return html[start : next_function if next_function != -1 else len(html)].strip()


def _assert_brace_balance(label: str, source: str) -> None:
    assert source.count("{") == source.count("}"), f"{label} extraction is truncated"


def test_api_daily_happy_path(tokens_env, monkeypatch):
    token_logs = {
        _day(2): [_entry("gpt-5", 1000)],
        _day(1): [
            _entry("gemini-2.5-flash", 2000),
            _entry("claude-sonnet-4-5", 3000),
        ],
        _day(0): [_entry("claude-sonnet-4-5", 4000)],
    }
    env = tokens_env(token_logs)
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/daily?days=14")

    assert response.status_code == 200
    payload = response.get_json()
    rows = payload["items"]
    assert payload["total"] == len(rows)
    assert len(rows) == 14
    assert all(set(row) == {"day", "cost", "tokens"} for row in rows)
    assert sorted(rows, key=lambda row: row["day"]) == rows

    by_day = {row["day"]: row for row in rows}
    assert by_day[_day(2)]["tokens"] == 1000
    assert by_day[_day(2)]["cost"] == pytest.approx(0.1)
    assert by_day[_day(1)]["tokens"] == 5000
    assert by_day[_day(1)]["cost"] == pytest.approx(0.5)
    assert by_day[_day(0)]["tokens"] == 4000
    assert by_day[_day(0)]["cost"] == pytest.approx(0.4)

    zero_rows = [row for row in rows if row["day"] not in token_logs]
    assert len(zero_rows) == 11
    assert all(row["cost"] == 0.0 and row["tokens"] == 0 for row in zero_rows)


def test_api_daily_zero_fills_missing_days(tokens_env, monkeypatch):
    token_logs = {
        _day(5): [_entry("gpt-5", 1000)],
        _day(0): [_entry("gemini-2.5-flash", 2000)],
    }
    env = tokens_env(token_logs)
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/daily?days=7")

    assert response.status_code == 200
    rows = response.get_json()["items"]
    assert [row["day"] for row in rows] == [_day(offset) for offset in range(6, -1, -1)]
    by_day = {row["day"]: row for row in rows}
    assert by_day[_day(5)]["tokens"] == 1000
    assert by_day[_day(0)]["tokens"] == 2000
    for day in {_day(offset) for offset in range(6, -1, -1)} - set(token_logs):
        assert by_day[day] == {"day": day, "cost": 0.0, "tokens": 0}


def test_api_daily_rejects_invalid_days(tokens_env):
    env = tokens_env({})

    cases = {
        "abc": "days must be a number",
        "0": "days must be between 1 and 90",
        "-1": "days must be between 1 and 90",
        "91": "days must be between 1 and 90",
    }
    for days, expected_detail in cases.items():
        response = env.client.get(f"/app/tokens/api/daily?days={days}")
        assert response.status_code == 400
        payload = response.get_json()
        assert payload["reason_code"] == "invalid_request_value"
        assert payload["detail"] == expected_detail


def test_api_daily_cross_month_boundary(tokens_env, monkeypatch):
    fixed_today = real_date(2026, 3, 4)

    class FakeDate(real_date):
        @staticmethod
        def today():
            return fixed_today

    monkeypatch.setattr(token_routes, "date", FakeDate)
    token_logs = {
        _day(offset, fixed_today): [_entry("claude-sonnet-4-5", (7 - offset) * 1000)]
        for offset in range(6, -1, -1)
    }
    env = tokens_env(token_logs)
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/daily?days=7")

    assert response.status_code == 200
    rows = response.get_json()["items"]
    assert [row["day"] for row in rows] == [
        "20260226",
        "20260227",
        "20260228",
        "20260301",
        "20260302",
        "20260303",
        "20260304",
    ]
    expected_rate = sum(row["cost"] for row in rows) / 7
    assert expected_rate == pytest.approx(0.4)


def test_api_daily_window_ends_at_requested_day(tokens_env, monkeypatch):
    token_logs = {
        _day(6): [_entry("gpt-5", 1000)],
        _day(0): [_entry("gpt-5", 4000)],
    }
    env = tokens_env(token_logs)
    _patch_token_cost(monkeypatch)

    earlier_response = env.client.get(f"/app/tokens/api/daily?days=7&day={_day(6)}")
    today_response = env.client.get(f"/app/tokens/api/daily?days=7&day={_day(0)}")

    assert earlier_response.status_code == 200
    assert today_response.status_code == 200
    earlier_items = earlier_response.get_json()["items"]
    today_items = today_response.get_json()["items"]
    assert earlier_items[0]["day"] == _day(12)
    assert earlier_items[-1]["day"] == _day(6)
    assert today_items[-1]["day"] == _day(0)
    assert earlier_items != today_items
    assert sum(row["cost"] for row in earlier_items[-7:]) != sum(
        row["cost"] for row in today_items[-7:]
    )


def test_api_daily_without_day_anchors_on_today(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/api/daily?days=7")

    assert response.status_code == 200
    assert response.get_json()["items"][-1]["day"] == _day(0)


def test_api_daily_rejects_invalid_day(tokens_env):
    env = tokens_env({})

    for day in ("notaday", "20260231", "99999999"):
        response = env.client.get(f"/app/tokens/api/daily?days=7&day={day}")
        assert response.status_code == 400
        payload = response.get_json()
        assert payload["reason_code"] == "invalid_day"
        assert payload["detail"] == "Invalid day format"

    response = env.client.get("/app/tokens/api/daily?days=abc&day=notaday")
    assert response.status_code == 400


def test_api_index_reports_nonzero_coverage_and_months(tokens_env, monkeypatch):
    env = tokens_env(
        {
            "20260304": [_entry("gpt-5", 1000)],
            "20260305": [_entry("gpt-5", 2000)],
            "20260401": [_entry("gpt-5", 3000)],
        }
    )
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/index")

    assert response.status_code == 200
    body = response.get_json()
    assert body["coverage"] == {"start": "20260304", "end": "20260401"}
    assert body["months"]["202603"] == pytest.approx(0.3)
    assert body["months"]["202604"] == pytest.approx(0.3)


def test_api_index_month_total_keeps_two_decimal_contract(tokens_env, monkeypatch):
    env = tokens_env(
        {
            "20260304": [_entry("gpt-5", 1000)],
            "20260305": [_entry("gpt-5", 2000)],
        }
    )
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/index")

    assert response.status_code == 200
    # Exact equality, not approx: the shared helper sums raw to
    # 0.30000000000000004. The call-site round is what holds the 2dp contract.
    assert response.get_json()["months"]["202603"] == 0.3


def test_api_index_month_totals_match_api_stats(tokens_env, monkeypatch):
    env = tokens_env(
        {
            "20260304": [_entry("gpt-5", 1000)],
            "20260305": [_entry("gpt-5", 2000)],
        }
    )
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/index")

    assert response.status_code == 200
    body = response.get_json()
    for month, total in body["months"].items():
        month_response = env.client.get(f"/app/tokens/api/stats/{month}")
        assert month_response.status_code == 200
        assert total == pytest.approx(sum(month_response.get_json().values()))


def test_api_index_empty_journal(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/api/index")

    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "months": {}}


def test_api_index_is_read_only(tokens_env, monkeypatch):
    env = tokens_env({"20260304": [_entry("gpt-5", 1000)]})
    _patch_token_cost(monkeypatch)
    before = _tokens_snapshot(env.journal)

    response = env.client.get("/app/tokens/api/index")

    assert response.status_code == 200
    assert _tokens_snapshot(env.journal) == before


def test_tokens_page_serves_spa_shell(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/20260304")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_tokens_index_redirects_to_shell(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/", follow_redirects=True)

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_tokens_day_guard_still_404s(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/notaday")

    assert response.status_code == 404


def test_tokens_workspace_contains_client_copy_and_static_labels(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/workspace")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'TOKENS_TILE_COST_LABEL: "today\'s cost"' in html
    assert 'TOKENS_TILE_TOKENS_LABEL: "today\'s tokens"' in html
    assert 'TOKENS_TILE_RUN_RATE_LABEL: "7-day run rate"' in html
    assert 'TOKENS_TILE_TOP_DRIVER_LABEL: "today\'s biggest cost"' in html
    assert 'data-tokens-copy-key="TOKENS_TILE_COST_LABEL"' in html
    assert "window.TOKENS_COPY = TOKENS_COPY" in html


def test_tokens_static_and_dynamic_empty_rows_share_copy(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/workspace")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    empty_rows = {
        "provider-body": "renderProviderTable",
        "model-body": "renderModelTable",
        "token-type-body": "renderTokenTypeTable",
        "segment-body": "renderSegmentTable",
    }
    for tbody_id, function_name in empty_rows.items():
        assert _static_empty_cell(html, tbody_id) == _dynamic_empty_cell(
            html,
            function_name,
        )

    assert _static_empty_cell(html, "provider-body") == "no data for this day"


def test_tokens_page_renders_collapsed_details_for_all_breakdowns(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/workspace")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    tags = re.findall(r'<details[^>]*data-disclosure="([\w-]+)"[^>]*>', html)
    assert set(tags) == {"provider", "model", "token-type", "context", "segment"}
    assert len(tags) == 5
    detail_tags = re.findall(r'<details[^>]*data-disclosure="[\w-]+"[^>]*>', html)
    assert all(" open" not in tag for tag in detail_tags)


def test_tokens_disclosure_summaries_pluralize_counts_under_node(tokens_env):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    html = _tokens_workspace_html(tokens_env)
    tokens_copy = _extract_tokens_copy(html)
    format_copy = _extract_function(html, "formatCopy")
    populate = _extract_function(html, "populateDisclosureSummaries")

    _assert_brace_balance("TOKENS_COPY", tokens_copy)
    _assert_brace_balance("formatCopy", format_copy)
    _assert_brace_balance("populateDisclosureSummaries", populate)

    script = "\n".join(
        [
            "global.window = global;",
            "global.document = undefined;",
            tokens_copy,
            "window.TOKENS_COPY = TOKENS_COPY;",
            "const writes = {};",
            "function setText(id, value) { writes[id] = value; }",
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            format_copy,
            populate,
            """
function run(data) {
  for (const key of Object.keys(writes)) delete writes[key];
  populateDisclosureSummaries(data);
  return Object.assign({}, writes);
}
const singular = run({
  by_provider: [{ provider: 'openai', percent: 51.25 }],
  by_model: [{ model: 'gpt-5', percent: 44.4 }],
  by_context: [{ context: 'think.cortex', percent: 33.3 }],
  by_segment: [{ segment: '090000_300' }],
});
const plural = run({
  by_provider: [{ provider: 'openai', percent: 51.25 }, { provider: 'anthropic', percent: 12 }],
  by_model: [{ model: 'gpt-5', percent: 44.4 }, { model: 'claude-sonnet', percent: 12 }],
  by_context: [{ context: 'think.cortex', percent: 33.3 }, { context: 'convey.tokens', percent: 12 }],
  by_segment: [{ segment: '090000_300' }, { segment: '091000_300' }],
});
assert(singular['summary-provider'] === '1 provider, top: openai 51.3%', 'provider singular');
assert(plural['summary-provider'] === '2 providers, top: openai 51.3%', 'provider plural');
assert(singular['summary-model'] === '1 model, top: gpt-5 44.4%', 'model singular');
assert(plural['summary-model'] === '2 models, top: gpt-5 44.4%', 'model plural');
assert(singular['summary-context'] === '1 context, top: think.cortex 33.3%', 'context singular');
assert(plural['summary-context'] === '2 contexts, top: think.cortex 33.3%', 'context plural');
assert(singular['summary-segment'] === '1 segment', 'segment singular');
assert(plural['summary-segment'] === '2 segments', 'segment plural');
assert(singular['summary-token-type'] === 'input / output / cached / reasoning', 'token type static');
console.log(JSON.stringify({ singular, plural }));
""",
        ]
    )

    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(completed.stdout)["singular"]["summary-provider"] == (
        "1 provider, top: openai 51.3%"
    )


def test_tokens_load_token_data_threads_day_under_node(tokens_env):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    html = _tokens_workspace_html(tokens_env)
    load_daily_series = _extract_function(html, "loadDailySeries")
    load_token_data = _extract_function(html, "loadTokenData")

    _assert_brace_balance("loadDailySeries", load_daily_series)
    _assert_brace_balance("loadTokenData", load_token_data)

    script = "\n".join(
        [
            "global.window = global;",
            "const urls = [];",
            "const errors = [];",
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            """
window.apiJson = async function(url) {
  urls.push(url);
  return {items: []};
};
function renderGlance(data, dailyRows) {
  assert(Array.isArray(dailyRows), 'daily rows should be an array');
}
function renderDashboard(data) {}
const nodes = {
  'tokens-loading': {style: {}},
  dashboard: {style: {}}
};
global.document = {
  getElementById(id) {
    if (!nodes[id]) throw new Error('missing node ' + id);
    return nodes[id];
  }
};
window.CONVEY_COPY = {RELOAD_HINT: 'reload'};
window.SurfaceState = {
  error(opts) { return opts; },
  replaceLoading(_id, state) {
    throw state?.detail || new Error('catch branch reached');
  }
};
global.console = {
  error(...args) { errors.push(args); },
  warn() {},
  log() {}
};
""",
            load_daily_series,
            load_token_data,
            """
async function run() {
  await loadTokenData('20260101');
  assert(errors.length === 0, 'no errors after explicit day');
  assert(urls.includes('/app/tokens/api/usage?day=20260101'), 'usage day url');
  const dailyUrl = urls.find((url) => url.includes('/app/tokens/api/daily'));
  assert(dailyUrl && dailyUrl.includes('day=20260101'), 'daily day url');

  urls.length = 0;
  await loadTokenData(null);
  assert(errors.length === 0, 'no errors after null day');
  const nullDailyUrl = urls.find((url) => url.includes('/app/tokens/api/daily'));
  assert(nullDailyUrl && !nullDailyUrl.includes('day='), 'daily omits null day');
}
run().catch((err) => {
  process.stdout.write((err && err.stack ? err.stack : String(err)) + '\\n');
  process.exit(1);
});
""",
        ]
    )

    subprocess.run([node, "-e", script], check=True, text=True)


def test_tokens_copy_constants_include_other_day_variants(tokens_env):
    html = _tokens_workspace_html(tokens_env)
    copy = _tokens_copy_constants(html)

    expected = {
        "TOKENS_TILE_COST_LABEL_OTHER_DAY": "cost",
        "TOKENS_TILE_TOKENS_LABEL_OTHER_DAY": "tokens",
        "TOKENS_TILE_TOP_DRIVER_LABEL_OTHER_DAY": "biggest cost",
        "TOKENS_TILE_TOP_DRIVER_VALUE_OTHER_DAY": (
            "{provider} · {model} ({pct}% of the day)"
        ),
    }
    for key, value in expected.items():
        assert copy[key] == value
        assert f'{key}: "{value}"' in html

    assert copy["TOKENS_TILE_TOP_DRIVER_VALUE"].endswith("% of today)")
    assert copy["TOKENS_TILE_TOP_DRIVER_VALUE_OTHER_DAY"].endswith("% of the day)")
    assert "TOKENS_TILE_RUN_RATE_LABEL_OTHER_DAY" not in html


def test_tokens_copy_key_resolver_under_node(tokens_env):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    html = _tokens_workspace_html(tokens_env)
    tokens_copy = _extract_tokens_copy(html)
    today_stamp = _extract_function(html, "tokensTodayStamp")
    copy_key_for_day = _extract_function(html, "tokensCopyKeyForDay")

    _assert_brace_balance("TOKENS_COPY", tokens_copy)
    _assert_brace_balance("tokensTodayStamp", today_stamp)
    _assert_brace_balance("tokensCopyKeyForDay", copy_key_for_day)

    script = "\n".join(
        [
            "global.window = global;",
            tokens_copy,
            "window.TOKENS_COPY = TOKENS_COPY;",
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            today_stamp,
            copy_key_for_day,
            """
function stampNow() {
  const now = new Date();
  return String(now.getFullYear()) +
    String(now.getMonth() + 1).padStart(2, '0') +
    String(now.getDate()).padStart(2, '0');
}
assert(
  tokensCopyKeyForDay('TOKENS_TILE_COST_LABEL', '20260101', '20260726') ===
    'TOKENS_TILE_COST_LABEL_OTHER_DAY',
  'non-today uses other-day key'
);
assert(
  tokensCopyKeyForDay('TOKENS_TILE_COST_LABEL', '20260726', '20260726') ===
    'TOKENS_TILE_COST_LABEL',
  'today uses base key'
);
assert(
  tokensCopyKeyForDay('TOKENS_TILE_RUN_RATE_LABEL', '20260101', '20260726') ===
    'TOKENS_TILE_RUN_RATE_LABEL',
  'missing other-day key falls back'
);
assert(
  tokensCopyKeyForDay('TOKENS_TILE_COST_LABEL', null, '20260726') ===
    'TOKENS_TILE_COST_LABEL',
  'missing day falls back'
);
assert(
  TOKENS_COPY[tokensCopyKeyForDay('TOKENS_TILE_COST_LABEL', '20260101', '20260726')] === 'cost',
  'non-today value'
);
assert(
  TOKENS_COPY[tokensCopyKeyForDay('TOKENS_TILE_COST_LABEL', '20260726', '20260726')] === "today's cost",
  'today value'
);
const before = stampNow();
const actual = tokensTodayStamp();
const after = stampNow();
assert(/^\\d{8}$/.test(actual), 'stamp shape');
assert(actual === before || actual === after, 'stamp race tolerance');
""",
        ]
    )

    subprocess.run([node, "-e", script], check=True, text=True)


def test_tokens_top_driver_copy_is_day_aware_under_node(tokens_env):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    html = _tokens_workspace_html(tokens_env)
    tokens_copy = _extract_tokens_copy(html)
    format_copy = _extract_function(html, "formatCopy")
    today_stamp = _extract_function(html, "tokensTodayStamp")
    copy_key_for_day = _extract_function(html, "tokensCopyKeyForDay")
    populate_tiles = _extract_function(html, "populateTiles")

    _assert_brace_balance("TOKENS_COPY", tokens_copy)
    _assert_brace_balance("formatCopy", format_copy)
    _assert_brace_balance("tokensTodayStamp", today_stamp)
    _assert_brace_balance("tokensCopyKeyForDay", copy_key_for_day)
    assert "tokensCopyKeyForDay('TOKENS_TILE_TOP_DRIVER_VALUE'" in populate_tiles

    script = "\n".join(
        [
            "global.window = global;",
            tokens_copy,
            "window.TOKENS_COPY = TOKENS_COPY;",
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            format_copy,
            today_stamp,
            copy_key_for_day,
            """
const values = {provider: 'openai', model: 'gpt-5', pct: '51.3'};
const other = formatCopy(
  TOKENS_COPY[tokensCopyKeyForDay('TOKENS_TILE_TOP_DRIVER_VALUE', '20260101', '20260726')],
  values
);
const today = formatCopy(
  TOKENS_COPY[tokensCopyKeyForDay('TOKENS_TILE_TOP_DRIVER_VALUE', '20260726', '20260726')],
  values
);
assert(other.endsWith('% of the day)'), 'other-day suffix');
assert(today.endsWith('% of today)'), 'today suffix');
""",
        ]
    )

    subprocess.run([node, "-e", script], check=True, text=True)
