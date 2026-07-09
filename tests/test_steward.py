# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import logging
import os
import time
from pathlib import Path

import pytest

from solstone.think.day_accumulator import append_record
from solstone.think.identity import (
    STEWARD_SECTION_ATTENTION,
    STEWARD_SECTION_AUTO_REPAIRS,
    STEWARD_SECTION_STATUS,
    ensure_identity_directory,
)
from solstone.think.steward import (
    STALE_PENDING_RECIPE,
    _recipe_outcomes_7d,
    acquire_steward_lock,
    append_steward_event,
    default_summary_from_body,
    load_latest_pass_event,
    load_steward_log,
    normalize_summary,
    prune_steward_log,
    read_steward_health,
    read_steward_summary,
    release_steward_lock,
    render_health_body,
    run_recipe_pass,
    validate_steward_health,
    write_health_md,
)
from solstone.think.utils import now_ms


def _set_journal(monkeypatch: pytest.MonkeyPatch, journal: Path) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))


def _select_local_provider(journal: Path) -> None:
    config_dir = journal / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "providers": {
                    "generate": {"provider": "local"},
                    "cogitate": {"provider": "local"},
                }
            }
        ),
        encoding="utf-8",
    )


def _valid_body(*, status: str = "sol is well.", needs: str = "") -> str:
    return "\n".join(
        [
            STEWARD_SECTION_STATUS,
            "<!-- generated_at: 2026-05-26T17:32:18Z -->",
            status,
            "",
            STEWARD_SECTION_ATTENTION,
            needs,
            "",
            STEWARD_SECTION_AUTO_REPAIRS,
            "",
        ]
    )


def _seed_stale_pending_segment(
    journal: Path,
    day: str,
    stream: str,
    segment_key: str,
    modality: str,
    age_seconds: int,
) -> Path:
    segment_dir = journal / "chronicle" / day / stream / segment_key
    segment_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".flac" if modality == "audio" else ".webm"
    raw_path = segment_dir / f"{segment_key}_{modality}{suffix}"
    raw_path.write_bytes(b"raw")
    mtime = time.time() - age_seconds
    os.utime(raw_path, (mtime, mtime))
    return segment_dir


def _seed_steward_log(journal: Path, rows: list[dict]) -> None:
    path = journal / "health" / "steward.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _recipe_row(target: str, outcome: str, ts: int) -> dict:
    return {
        "event": "recipe.outcome",
        "ts": ts,
        "recipe": "stale_pending_segment_reprocess",
        "target": target,
        "outcome": outcome,
        "detail": None,
    }


NOW = 1_000_000_000_000
DAY_MS = 86_400_000


def test_prune_steward_log_age_prune_keeps_order(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    rows = [
        {"event": "pass", "ts": NOW - 40 * DAY_MS, "seq": "drop-40"},
        {"event": "pass", "ts": NOW - 31 * DAY_MS, "seq": "drop-31"},
        {"event": "pass", "ts": NOW - 29 * DAY_MS, "seq": "keep-29"},
        {"event": "pass", "ts": NOW - DAY_MS, "seq": "keep-1"},
    ]
    _seed_steward_log(tmp_path, rows)

    prune_steward_log()

    assert load_steward_log() == [rows[2], rows[3]]


def test_prune_steward_log_preserves_7d_rollup(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    rows = [
        _recipe_row("within-7", "verified_healed", NOW - 2 * DAY_MS),
        _recipe_row("within-7-failed", "failed", NOW - 6 * DAY_MS),
        _recipe_row("within-30", "running", NOW - 20 * DAY_MS),
        _recipe_row("older-than-30", "accepted", NOW - 31 * DAY_MS),
    ]
    _seed_steward_log(tmp_path, rows)
    rollup_before = _recipe_outcomes_7d(load_steward_log())

    prune_steward_log()
    rollup_after = _recipe_outcomes_7d(load_steward_log())

    assert rollup_after == rollup_before


def test_prune_steward_log_preserves_latest_pass(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    rows = [
        {"event": "pass", "ts": NOW - 31 * DAY_MS, "seq": "oldest"},
        {"event": "pass", "ts": NOW - 15 * DAY_MS, "seq": "middle"},
        {"event": "pass", "ts": NOW - DAY_MS, "seq": "newest"},
    ]
    _seed_steward_log(tmp_path, rows)
    before = load_latest_pass_event()

    prune_steward_log()

    assert load_latest_pass_event() == before


def test_prune_steward_log_fail_safe_and_malformed(tmp_path, monkeypatch, caplog):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    rows = [
        {"event": "pass", "seq": "no-ts"},
        123,
        {"event": "pass", "ts": NOW - DAY_MS, "seq": "recent"},
    ]
    _seed_steward_log(tmp_path, rows)
    path = tmp_path / "health" / "steward.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")

    with caplog.at_level(logging.INFO):
        prune_steward_log()

    raw = path.read_text(encoding="utf-8")
    assert "not json at all" not in raw
    assert '"seq":"no-ts"' in raw
    assert "123\n" in raw
    assert '"seq":"recent"' in raw
    assert load_steward_log() == [rows[0], rows[2]]
    assert (
        "steward: pruned 0 stale row(s), dropped 1 malformed line(s) from steward.log"
        in caplog.text
    )


def test_prune_steward_log_missing_and_empty_noop(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    path = tmp_path / "health" / "steward.log"

    prune_steward_log()

    assert not path.exists()

    path.write_text("", encoding="utf-8")
    prune_steward_log()

    assert path.read_text(encoding="utf-8") == ""
    assert list((tmp_path / "health").glob("*.tmp")) == []


def test_prune_steward_log_atomicity_and_valid_jsonl(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    rows = [
        {"event": "pass", "ts": NOW - 40 * DAY_MS, "seq": "stale"},
        {"event": "pass", "ts": NOW - DAY_MS, "seq": "fresh"},
    ]
    _seed_steward_log(tmp_path, rows)

    prune_steward_log()

    path = tmp_path / "health" / "steward.log"
    assert list((tmp_path / "health").glob("*.tmp")) == []
    for line in path.read_text(encoding="utf-8").splitlines():
        json.loads(line)
    assert load_steward_log() == [rows[1]]


def test_prune_steward_log_skips_when_steward_lock_held(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    rows = [
        {"event": "pass", "ts": NOW - 40 * DAY_MS, "seq": "stale"},
        {"event": "pass", "ts": NOW - DAY_MS, "seq": "fresh"},
    ]
    _seed_steward_log(tmp_path, rows)
    path = tmp_path / "health" / "steward.log"
    before = path.read_bytes()
    fd = acquire_steward_lock()
    assert fd is not None
    try:
        prune_steward_log()
    finally:
        release_steward_lock(fd)

    assert path.read_bytes() == before


def test_prune_steward_log_recent_render_failed_survives(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    rows = [
        {
            "event": "render.failed",
            "ts": NOW - DAY_MS,
            "target": "identity/health.md",
        },
        _recipe_row("stale-target", "failed", NOW - 40 * DAY_MS),
    ]
    _seed_steward_log(tmp_path, rows)

    prune_steward_log()

    assert load_steward_log() == [rows[0]]


def test_prune_steward_log_noop_when_nothing_dropped(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: NOW)
    rows = [
        {"event": "pass", "ts": NOW - 29 * DAY_MS, "seq": "fresh-29"},
        _recipe_row("fresh-target", "running", NOW - DAY_MS),
    ]
    _seed_steward_log(tmp_path, rows)
    path = tmp_path / "health" / "steward.log"
    before = path.read_bytes()

    prune_steward_log()

    assert path.read_bytes() == before


def _fixed_facts(
    errors: list[str] | None = None,
    pipeline_day: dict | None = None,
    recipe_outcomes_7d: list | None = None,
) -> dict:
    return {
        "generated_at": "2026-06-07T00:00:00Z",
        "pipeline_day": pipeline_day if pipeline_day is not None else {"anomalies": []},
        "recipe_outcomes_7d": recipe_outcomes_7d or [],
        "data_source_errors": list(errors) if errors else [],
    }


def test_recipe_rollup_folds_legacy_success_to_verified_healed(monkeypatch):
    monkeypatch.setattr("solstone.think.steward.now_ms", lambda: 100_000)
    target = "20260526/archon/120000_300:screen"
    rollup = _recipe_outcomes_7d([_recipe_row(target, "success", 99_000)])

    assert rollup[0]["verified_healed"] == 1
    assert rollup[0]["failed"] == 0
    assert rollup[0]["total"] == 1


def test_pre_process_uses_pass_event_without_refiring_recipes(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    today = "20260607"
    target = f"{today}/local/120000_300:audio"
    _seed_stale_pending_segment(
        tmp_path, today, "local", "120000_300", "audio", 7 * 60 * 60
    )
    _seed_steward_log(tmp_path, [_recipe_row(target, "accepted", now_ms() - 1000)])
    before = load_steward_log()

    import urllib.request

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("report-only recipe pass must not make HTTP requests")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    result = run_recipe_pass(today)

    assert result == {"fired": [], "escalated_targets": [], "data_source_errors": []}
    assert load_steward_log() == before


def test_pre_process_renders_pass_event_into_health_body(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    append_steward_event(
        "pass",
        fired=[],
        escalated_targets=["20260607/local/seg2:screen"],
        data_source_errors=["convey port: x"],
    )

    import solstone.talent.steward as steward_hook

    monkeypatch.setattr(
        steward_hook,
        "gather_health_facts",
        lambda day: _fixed_facts(["pipeline_day: y"]),
    )
    result = steward_hook.pre_process({"day": "20260607"})

    assert result is not None
    body = result["template_vars"]["health_state"]
    # Deterministic body folds in both the gathered and pass-event facts.
    assert (
        "sol has a partial health picture: some health sources could not be read."
        in body
    )
    assert "escalating: stale-pending segment reprocess" not in body
    assert "could not read pipeline_day: y" in body
    assert "could not read convey port: x" in body
    assert validate_steward_health(body) is None
    # health.md is written deterministically (no model call in that path).
    assert read_steward_health(tmp_path) is not None


def test_pre_process_fresh_journal_writes_well_health(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)

    import solstone.talent.steward as steward_hook

    monkeypatch.setattr(steward_hook, "gather_health_facts", lambda day: _fixed_facts())
    result = steward_hook.pre_process({"day": "20260607"})

    assert result is not None
    body = result["template_vars"]["health_state"]
    assert "sol is well." in body
    assert validate_steward_health(body) is None
    # Healthy body → home widget hidden.
    assert read_steward_health(tmp_path) is None
    assert result["template_vars"]["previous_summary"] == "(none — first run)"


def test_pre_process_dry_run_does_not_write_health(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)

    import solstone.talent.steward as steward_hook

    monkeypatch.setattr(
        steward_hook,
        "gather_health_facts",
        lambda day: _fixed_facts(["pipeline_day: y"]),
    )
    result = steward_hook.pre_process({"day": "20260607", "dry_run": True})

    assert result is not None
    assert "template_vars" in result
    # Dry run still renders the body but must not mutate the journal.
    assert not (tmp_path / "identity" / "health.md").exists()


def test_validator_rejects_missing_section():
    body = _valid_body().replace(f"\n{STEWARD_SECTION_AUTO_REPAIRS}\n", "\n")

    assert (
        validate_steward_health(body)
        == f"missing section: {STEWARD_SECTION_AUTO_REPAIRS}"
    )


def test_validator_rejects_wrong_order():
    body = "\n".join(
        [
            STEWARD_SECTION_STATUS,
            "<!-- generated_at: 2026-05-26T17:32:18Z -->",
            "sol is well.",
            "",
            STEWARD_SECTION_AUTO_REPAIRS,
            "",
            STEWARD_SECTION_ATTENTION,
            "",
        ]
    )

    assert validate_steward_health(body) == "sections out of order"


def test_validator_rejects_trends_section():
    body = _valid_body() + "## Trends (last 7d)\n"

    assert validate_steward_health(body) == "unexpected section: ## Trends (last 7d)"


def test_validator_rejects_extra_section():
    body = _valid_body() + "\n## Extra\n"

    assert validate_steward_health(body) == "unexpected section: ## Extra"


def test_validator_rejects_empty_status():
    body = _valid_body(status="")

    assert validate_steward_health(body) == "empty status section"


def test_validator_rejects_missing_generated_at():
    body = _valid_body().replace("<!-- generated_at: 2026-05-26T17:32:18Z -->\n", "")

    assert validate_steward_health(body) == "missing or invalid generated_at"


def test_validator_accepts_well_formed():
    assert validate_steward_health(_valid_body()) is None


def test_read_steward_health_returns_none_when_missing(tmp_path):
    assert read_steward_health(tmp_path) is None


def test_read_steward_health_returns_none_when_healthy(tmp_path):
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    path.write_text(_valid_body(), encoding="utf-8")

    assert read_steward_health(tmp_path) is None


def test_read_steward_health_surfaces_first_attention_bullet(tmp_path):
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    path.write_text(
        _valid_body(
            status="sol found a pipeline gap.",
            needs="- Foo bar\n- Baz",
        ),
        encoding="utf-8",
    )

    assert read_steward_health(tmp_path) == {"status": "warning", "message": "Foo bar"}


def test_read_steward_health_needs_wins_over_status_mismatch(tmp_path):
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    path.write_text(_valid_body(needs="- Foo bar"), encoding="utf-8")

    assert read_steward_health(tmp_path) == {"status": "warning", "message": "Foo bar"}


def test_read_steward_health_returns_none_when_malformed(tmp_path):
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    path.write_text("not markdown", encoding="utf-8")

    assert read_steward_health(tmp_path) is None


def test_write_health_md_logs_render_failed_and_preserves_prior_file(
    tmp_path, monkeypatch
):
    _set_journal(monkeypatch, tmp_path)
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    prior = _valid_body()
    path.write_text(prior, encoding="utf-8")

    reason = write_health_md("## Status\nbroken\n")

    assert reason is not None
    assert path.read_text(encoding="utf-8") == prior
    assert load_steward_log()[0]["event"] == "render.failed"


def test_health_md_history_has_only_steward_and_bootstrap_writers(
    tmp_path, monkeypatch
):
    _set_journal(monkeypatch, tmp_path)
    ensure_identity_directory()
    assert write_health_md(_valid_body()) is None
    assert read_steward_health(tmp_path) is None

    history_path = tmp_path / "identity" / "history.jsonl"
    rows = [json.loads(line) for line in history_path.read_text().splitlines()]
    actors = {row["actor"] for row in rows if row["file"] == "health.md"}

    assert actors <= {"steward", "ensure_identity_directory"}


# ---------------------------------------------------------------------------
# Deterministic renderer
# ---------------------------------------------------------------------------

_GEN_AT = "2026-06-07T00:00:00Z"


def test_render_health_body_healthy_is_valid_and_well():
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day={"anomalies": []},
        recipe_outcomes_7d=[],
        data_source_errors=[],
    )

    assert validate_steward_health(body) is None
    assert f"<!-- generated_at: {_GEN_AT} -->" in body
    assert "sol is well." in body


def test_render_health_body_healthy_reads_as_none(tmp_path):
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    path.write_text(
        render_health_body(
            generated_at=_GEN_AT,
            pipeline_day={"anomalies": []},
            recipe_outcomes_7d=[],
            data_source_errors=[],
        ),
        encoding="utf-8",
    )

    assert read_steward_health(tmp_path) is None


def test_render_health_body_activity_gap_bullet():
    pipeline_day = {
        "anomalies": [{"kind": "activity_agents_missing"}],
        "activities": {"detected": 3},
    }
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day=pipeline_day,
        recipe_outcomes_7d=[],
        data_source_errors=[],
    )

    assert validate_steward_health(body) is None
    assert "sol is well." not in body
    assert (
        "sol detected pipeline issues during yesterday's processing "
        "that need attention." in body
    )
    assert "3 activities ended yesterday" in body


def test_render_health_body_segment_status_unknown_bullet():
    pipeline_day = {
        "status": "unknown",
        "anomalies": [{"kind": "segments_not_thought", "error": "no_health_dir"}],
    }

    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day=pipeline_day,
        recipe_outcomes_7d=[],
        data_source_errors=[],
    )

    assert validate_steward_health(body) is None
    assert "sol is well." not in body
    assert "Segment thinking status could not be determined." in body


def test_render_health_body_talent_failure_timed_out():
    pipeline_day = {
        "anomalies": [
            {"kind": "talent_failure", "name": "entities", "state": "timeout"},
            {"kind": "talent_failure", "name": "documents", "state": "timeout"},
        ],
        "talents": {"failed": 9, "outstanding_failed": 2},
    }
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day=pipeline_day,
        recipe_outcomes_7d=[],
        data_source_errors=[],
    )

    assert (
        "2 agents timed out during yesterday's processing (entities, documents)."
        in (body)
    )


def test_render_health_body_talent_failure_request_lost():
    pipeline_day = {
        "anomalies": [
            {"kind": "talent_failure", "name": "entities", "state": "request_lost"},
            {"kind": "talent_failure", "name": "documents", "state": "request_lost"},
        ],
        "talents": {"outstanding_failed": 2},
    }
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day=pipeline_day,
        recipe_outcomes_7d=[],
        data_source_errors=[],
    )

    assert (
        "2 agents couldn't start during yesterday's processing "
        "(entities, documents)." in body
    )


def test_render_health_body_auto_repair_rollup():
    rollup = [
        {
            "recipe": STALE_PENDING_RECIPE,
            "accepted": 1,
            "running": 1,
            "verified_healed": 2,
            "failed": 1,
            "no_output": 1,
            "unverified": 2,
            "total": 6,
            "last_iso": "2026-06-06T10:00:00Z",
        }
    ]
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day={"anomalies": []},
        recipe_outcomes_7d=rollup,
        data_source_errors=[],
    )

    assert validate_steward_health(body) is None
    # A 7d rollup with a failure means sol is not "well".
    assert "sol is well." not in body
    assert (
        "stale-pending segment reprocess — 6x in 7d (2 verified-healed, "
        "2 in-flight, 2 failed), "
        "last 2026-06-06T10:00:00Z" in body
    )


def test_render_health_body_inflight_rollup_is_not_well():
    rollup = [
        {
            "recipe": STALE_PENDING_RECIPE,
            "accepted": 1,
            "running": 1,
            "verified_healed": 0,
            "failed": 0,
            "no_output": 0,
            "unverified": 2,
            "total": 2,
            "last_iso": "2026-06-06T10:00:00Z",
        }
    ]
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day={"anomalies": []},
        recipe_outcomes_7d=rollup,
        data_source_errors=[],
    )

    assert validate_steward_health(body) is None
    assert "sol is well." not in body
    assert "2 stale segment repairs in progress, not yet verified." in body


def test_render_health_body_first_attention_bullet_drives_widget(tmp_path):
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    path.write_text(
        render_health_body(
            generated_at=_GEN_AT,
            pipeline_day={"anomalies": [{"kind": "daily_agents_missing"}]},
            recipe_outcomes_7d=[],
            data_source_errors=[],
        ),
        encoding="utf-8",
    )

    status = read_steward_health(tmp_path)
    assert status is not None
    assert status["status"] == "warning"
    assert "Daily agents didn't run yesterday" in status["message"]


# ---------------------------------------------------------------------------
# Human-friendly summaries
# ---------------------------------------------------------------------------


def _seed_summary(day: str, payload: dict) -> None:
    append_record(day, "steward", dict(payload))


def test_read_steward_summary_returns_latest(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_summary(
        "20260607",
        {
            "headline": "All clear",
            "summary_sentence": "sol is well.",
            "suggested_action": "none",
        },
    )

    assert read_steward_summary(day="20260607") == {
        "headline": "All clear",
        "summary_sentence": "sol is well.",
        "suggested_action": "none",
    }


def test_read_steward_summary_walks_back(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_summary(
        "20260605",
        {
            "headline": "Pipeline gap",
            "summary_sentence": "Two segments awaiting thinking.",
            "suggested_action": "open_health_detail",
        },
    )

    summary = read_steward_summary(day="20260607")
    assert summary is not None
    assert summary["headline"] == "Pipeline gap"


def test_read_steward_summary_missing_returns_none(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    assert read_steward_summary(day="20260607") is None


def test_read_steward_summary_clamps_bad_enum(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_summary(
        "20260607",
        {
            "headline": "X",
            "summary_sentence": "Y",
            "suggested_action": "delete_everything",
        },
    )

    summary = read_steward_summary(day="20260607")
    assert summary is not None
    assert summary["suggested_action"] == "none"


def test_read_steward_summary_malformed_returns_none(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    # A record that fails coercion yields None.
    _seed_summary("20260607", {"headline": "x"})

    assert read_steward_summary(day="20260607") is None


def test_normalize_summary_passthrough():
    default = {
        "headline": "d",
        "summary_sentence": "d",
        "suggested_action": "open_health_detail",
    }
    summary = normalize_summary(
        json.dumps(
            {
                "headline": "Repairs failing",
                "summary_sentence": "Two repairs failed twice.",
                "suggested_action": "open_support",
            }
        ),
        default,
    )

    assert summary["headline"] == "Repairs failing"
    assert summary["suggested_action"] == "open_support"


def test_normalize_summary_falls_back_on_garbage():
    default = {
        "headline": "d",
        "summary_sentence": "d",
        "suggested_action": "open_health_detail",
    }

    assert normalize_summary("definitely not json", default) == default


def test_normalize_summary_clamps_enum():
    default = {
        "headline": "d",
        "summary_sentence": "d",
        "suggested_action": "open_health_detail",
    }
    summary = normalize_summary(
        json.dumps(
            {"headline": "h", "summary_sentence": "s", "suggested_action": "bogus"}
        ),
        default,
    )

    assert summary["suggested_action"] == "none"


def test_default_summary_from_body_healthy():
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day={"anomalies": []},
        recipe_outcomes_7d=[],
        data_source_errors=[],
    )

    summary = default_summary_from_body(body)
    assert summary["headline"] == "All clear"
    assert summary["suggested_action"] == "none"


def test_default_summary_from_body_not_healthy_opens_detail():
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day={"anomalies": [{"kind": "daily_agents_missing"}]},
        recipe_outcomes_7d=[],
        data_source_errors=[],
    )

    summary = default_summary_from_body(body)
    assert summary["suggested_action"] == "open_health_detail"


def test_read_steward_summary_preserves_open_support(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_summary(
        "20260607",
        {
            "headline": "Repairs failing",
            "summary_sentence": "sol couldn't fix two segments after retrying.",
            "suggested_action": "open_support",
        },
    )

    summary = read_steward_summary(day="20260607")
    assert summary is not None
    assert summary["suggested_action"] == "open_support"


def test_accumulate_suppresses_single_file_output_path(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    from solstone.think.talent import get_talent, get_talent_configs
    from solstone.think.talents import prepare_config
    from solstone.think.thinking import _apply_output_persistence

    raw_config = get_talent_configs()["steward"]
    steward_config = get_talent("steward")
    assert raw_config["output"] == "json"
    assert raw_config["schema"] == "steward.schema.json"
    assert steward_config["json_schema"]["required"] == [
        "headline",
        "summary_sentence",
        "suggested_action",
    ]
    assert steward_config["accumulate"] is True

    request_config = {}
    _apply_output_persistence(request_config, steward_config, force_refresh=False)
    assert "output" not in request_config
    assert "refresh" not in request_config

    _select_local_provider(tmp_path)
    prepared = prepare_config({"name": "steward", "day": "20260607"})
    assert "output_path" not in prepared
