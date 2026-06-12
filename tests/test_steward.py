# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import dataclasses
import json
import os
import time
from pathlib import Path

import pytest

from solstone.think.day_accumulator import append_record
from solstone.think.identity import (
    STEWARD_SECTION_ATTENTION,
    STEWARD_SECTION_AUTO_REPAIRS,
    STEWARD_SECTION_STATUS,
    STEWARD_SECTION_TRENDS,
    ensure_identity_directory,
)
from solstone.think.steward import (
    STALE_PENDING_RECIPE,
    RecipeOutcome,
    StalePendingTarget,
    _modality_signals,
    append_steward_event,
    default_summary_from_body,
    detect_stale_pending_segments,
    load_steward_log,
    normalize_summary,
    read_steward_health,
    read_steward_summary,
    render_health_body,
    run_recipe_pass,
    validate_steward_health,
    write_health_md,
)
from solstone.think.utils import now_ms


def _set_journal(monkeypatch: pytest.MonkeyPatch, journal: Path) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))


def _valid_body(*, status: str = "Sol is well.", needs: str = "") -> str:
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
            STEWARD_SECTION_TRENDS,
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


def _fixed_facts(
    errors: list[str] | None = None,
    pipeline_day: dict | None = None,
    recipe_outcomes_7d: list | None = None,
) -> dict:
    return {
        "generated_at": "2026-06-07T00:00:00Z",
        "health_report": {},
        "pipeline_day": pipeline_day if pipeline_day is not None else {"anomalies": []},
        "recipe_outcomes_7d": recipe_outcomes_7d or [],
        "data_source_errors": list(errors) if errors else [],
    }


def test_recipe_detects_stale_pending_segment(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_stale_pending_segment(
        tmp_path, "20260526", "archon", "120000_300", "audio", 7 * 60 * 60
    )

    targets = detect_stale_pending_segments("20260526", "20260525")

    assert [target.target for target in targets] == ["20260526/archon/120000_300:audio"]


def test_recipe_skips_fresh_pending_segment(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_stale_pending_segment(
        tmp_path, "20260526", "archon", "120000_300", "audio", 60
    )

    assert detect_stale_pending_segments("20260526", "20260525") == []


def test_recipe_skips_already_analyzing(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    segment_dir = _seed_stale_pending_segment(
        tmp_path, "20260526", "archon", "120000_300", "audio", 7 * 60 * 60
    )
    (segment_dir / ".analyzing_audio").write_text("{}", encoding="utf-8")

    assert detect_stale_pending_segments("20260526", "20260525") == []


def test_modality_signals_repairs_chunks_win_marker(tmp_path):
    segment_dir = tmp_path / "090000_300"
    segment_dir.mkdir()
    marker = segment_dir / ".analyzing_screen"
    marker.write_text(
        '{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n',
        encoding="utf-8",
    )
    (segment_dir / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n{"timestamp": 1, "content": {}}\n',
        encoding="utf-8",
    )

    signals = _modality_signals(segment_dir, "screen")

    assert signals["state"] == "analyzed"
    assert not marker.exists()


def test_modality_signals_repairs_stale_pending_marker(tmp_path):
    segment_dir = tmp_path / "090000_300"
    segment_dir.mkdir()
    marker = segment_dir / ".analyzing_screen"
    failed = segment_dir / ".analyze_failed_screen"
    (segment_dir / "screen.webm").write_bytes(b"raw")
    marker.write_text(
        '{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n',
        encoding="utf-8",
    )
    old_time = time.time() - 2000
    os.utime(marker, (old_time, old_time))

    signals = _modality_signals(segment_dir, "screen")

    assert signals["state"] == "failed"
    assert not marker.exists()
    payload = json.loads(failed.read_text(encoding="utf-8"))
    assert payload["reason"] == "stale"
    assert payload["modality"] == "screen"


def test_modality_signals_does_not_repair_media_purged_marker(tmp_path):
    segment_dir = tmp_path / "090000_300"
    segment_dir.mkdir()
    marker = segment_dir / ".analyzing_screen"
    failed = segment_dir / ".analyze_failed_screen"
    marker.write_text(
        '{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n',
        encoding="utf-8",
    )
    old_time = time.time() - 2000
    os.utime(marker, (old_time, old_time))
    (segment_dir / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n',
        encoding="utf-8",
    )

    signals = _modality_signals(segment_dir, "screen")

    assert signals["state"] == "purged"
    assert marker.exists()
    assert not failed.exists()


def test_recipe_fire_success_appends_log_entry(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_stale_pending_segment(
        tmp_path, "20260526", "archon", "120000_300", "audio", 7 * 60 * 60
    )

    def fake_fire(target: StalePendingTarget, *, port: int) -> RecipeOutcome:
        return RecipeOutcome(
            recipe="stale_pending_segment_reprocess",
            target=target.target,
            outcome="success",
            detail=None,
            ts=now_ms(),
        )

    monkeypatch.setattr("solstone.think.steward.fire_stale_pending_recipe", fake_fire)

    result = run_recipe_pass("20260526")

    assert result["fired"][0].outcome == "success"
    assert load_steward_log()[0]["outcome"] == "success"


def test_recipe_fire_failure_appends_log_entry(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_stale_pending_segment(
        tmp_path, "20260526", "archon", "120000_300", "audio", 7 * 60 * 60
    )

    def fake_fire(target: StalePendingTarget, *, port: int) -> RecipeOutcome:
        return RecipeOutcome(
            recipe="stale_pending_segment_reprocess",
            target=target.target,
            outcome="failure",
            detail="500",
            ts=now_ms(),
        )

    monkeypatch.setattr("solstone.think.steward.fire_stale_pending_recipe", fake_fire)

    run_recipe_pass("20260526")

    row = load_steward_log()[0]
    assert row["outcome"] == "failure"
    assert row["detail"] == "500"


def test_escalation_after_two_consecutive_failures(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    target = "20260526/archon/120000_300:audio"
    _seed_stale_pending_segment(
        tmp_path, "20260526", "archon", "120000_300", "audio", 7 * 60 * 60
    )
    _seed_steward_log(
        tmp_path,
        [
            _recipe_row(target, "failure", now_ms() - 2000),
            _recipe_row(target, "failure", now_ms() - 1000),
        ],
    )
    calls = []
    monkeypatch.setattr(
        "solstone.think.steward.fire_stale_pending_recipe",
        lambda target, *, port: calls.append(target),
    )

    result = run_recipe_pass("20260526")

    assert result["escalated_targets"] == [target]
    assert calls == []


def test_escalation_resets_after_success(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    target = "20260526/archon/120000_300:audio"
    _seed_stale_pending_segment(
        tmp_path, "20260526", "archon", "120000_300", "audio", 7 * 60 * 60
    )
    _seed_steward_log(
        tmp_path,
        [
            _recipe_row(target, "failure", now_ms() - 3000),
            _recipe_row(target, "failure", now_ms() - 2000),
            _recipe_row(target, "success", now_ms() - 1000),
        ],
    )

    def fake_fire(target: StalePendingTarget, *, port: int) -> RecipeOutcome:
        return RecipeOutcome(
            recipe="stale_pending_segment_reprocess",
            target=target.target,
            outcome="success",
            detail=None,
            ts=now_ms(),
        )

    monkeypatch.setattr("solstone.think.steward.fire_stale_pending_recipe", fake_fire)

    result = run_recipe_pass("20260526")

    assert result["escalated_targets"] == []
    assert result["fired"][0].target == target


def test_pre_process_uses_pass_event_without_refiring_recipes(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    today = "20260607"
    _seed_stale_pending_segment(
        tmp_path, today, "local", "120000_300", "audio", 7 * 60 * 60
    )
    _seed_stale_pending_segment(
        tmp_path, today, "local", "130000_300", "screen", 7 * 60 * 60
    )
    fired_targets = []

    def fake_fire(target: StalePendingTarget, *, port: int) -> RecipeOutcome:
        fired_targets.append(target.target)
        return RecipeOutcome(
            recipe=STALE_PENDING_RECIPE,
            target=target.target,
            outcome="success",
            detail=None,
            ts=now_ms(),
        )

    monkeypatch.setattr("solstone.think.steward.fire_stale_pending_recipe", fake_fire)

    result = run_recipe_pass(today)
    append_steward_event(
        "pass",
        fired=[dataclasses.asdict(outcome) for outcome in result["fired"]],
        escalated_targets=result["escalated_targets"],
        data_source_errors=result["data_source_errors"],
    )

    assert len(fired_targets) == 2

    import solstone.talent.steward as steward_hook

    monkeypatch.setattr(steward_hook, "gather_health_facts", lambda day: _fixed_facts())
    hook_result = steward_hook.pre_process({"day": today})

    assert hook_result is not None
    assert "template_vars" in hook_result
    assert "health_state" in hook_result["template_vars"]
    # The talent never fires repair — only the deterministic heartbeat does.
    assert len(fired_targets) == 2


def test_pre_process_renders_pass_event_into_health_body(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    fired = [
        dataclasses.asdict(
            RecipeOutcome(
                recipe=STALE_PENDING_RECIPE,
                target="20260607/local/seg1:audio",
                outcome="failure",
                detail="boom",
                ts=123,
            )
        )
    ]
    append_steward_event(
        "pass",
        fired=fired,
        escalated_targets=["20260607/local/seg2:screen"],
        data_source_errors=["convey port: x"],
    )

    import solstone.talent.steward as steward_hook

    monkeypatch.setattr(
        steward_hook,
        "gather_health_facts",
        lambda day: _fixed_facts(["health_report: y"]),
    )
    result = steward_hook.pre_process({"day": "20260607"})

    assert result is not None
    body = result["template_vars"]["health_state"]
    # Deterministic body folds in both the gathered and pass-event facts.
    assert (
        "escalating: stale-pending segment reprocess on 20260607/local/seg2:screen"
        in body
    )
    assert "could not read health_report: y" in body
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
    assert "Sol is well." in body
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
        lambda day: _fixed_facts(["health_report: y"]),
    )
    result = steward_hook.pre_process({"day": "20260607", "dry_run": True})

    assert result is not None
    assert "template_vars" in result
    # Dry run still renders the body but must not mutate the journal.
    assert not (tmp_path / "identity" / "health.md").exists()


def test_validator_rejects_missing_section():
    body = _valid_body().replace(f"\n{STEWARD_SECTION_TRENDS}\n", "\n")

    assert validate_steward_health(body) == f"missing section: {STEWARD_SECTION_TRENDS}"


def test_validator_rejects_wrong_order():
    body = "\n".join(
        [
            STEWARD_SECTION_STATUS,
            "<!-- generated_at: 2026-05-26T17:32:18Z -->",
            "Sol is well.",
            "",
            STEWARD_SECTION_AUTO_REPAIRS,
            "",
            STEWARD_SECTION_ATTENTION,
            "",
            STEWARD_SECTION_TRENDS,
            "",
        ]
    )

    assert validate_steward_health(body) == "sections out of order"


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
            status="Sol found a pipeline gap.",
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
        escalated_targets=[],
        data_source_errors=[],
    )

    assert validate_steward_health(body) is None
    assert f"<!-- generated_at: {_GEN_AT} -->" in body
    assert "Sol is well." in body


def test_render_health_body_healthy_reads_as_none(tmp_path):
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    path.write_text(
        render_health_body(
            generated_at=_GEN_AT,
            pipeline_day={"anomalies": []},
            recipe_outcomes_7d=[],
            escalated_targets=[],
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
        escalated_targets=[],
        data_source_errors=[],
    )

    assert validate_steward_health(body) is None
    assert "Sol is well." not in body
    assert "3 activities ended yesterday" in body


def test_render_health_body_talent_failure_timed_out():
    pipeline_day = {
        "anomalies": [
            {"kind": "talent_failure", "name": "entities", "state": "timeout"},
            {"kind": "talent_failure", "name": "documents", "state": "timeout"},
        ],
        "talents": {"failed": 2},
    }
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day=pipeline_day,
        recipe_outcomes_7d=[],
        escalated_targets=[],
        data_source_errors=[],
    )

    assert (
        "2 agents timed out during yesterday's processing (entities, documents)."
        in (body)
    )


def test_render_health_body_auto_repair_rollup():
    rollup = [
        {
            "recipe": STALE_PENDING_RECIPE,
            "success": 2,
            "failure": 1,
            "total": 3,
            "last_iso": "2026-06-06T10:00:00Z",
        }
    ]
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day={"anomalies": []},
        recipe_outcomes_7d=rollup,
        escalated_targets=[],
        data_source_errors=[],
    )

    assert validate_steward_health(body) is None
    # A 7d rollup with a failure means Sol is not "well".
    assert "Sol is well." not in body
    assert (
        "stale-pending segment reprocess — 3x in 7d (2 succeeded, 1 failed), "
        "last 2026-06-06T10:00:00Z" in body
    )


def test_render_health_body_first_attention_bullet_drives_widget(tmp_path):
    path = tmp_path / "identity" / "health.md"
    path.parent.mkdir()
    path.write_text(
        render_health_body(
            generated_at=_GEN_AT,
            pipeline_day={"anomalies": [{"kind": "daily_agents_missing"}]},
            recipe_outcomes_7d=[],
            escalated_targets=[],
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
            "summary_sentence": "Sol is well.",
            "suggested_action": "none",
        },
    )

    assert read_steward_summary(day="20260607") == {
        "headline": "All clear",
        "summary_sentence": "Sol is well.",
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
                "suggested_action": "reprocess_stale",
            }
        ),
        default,
    )

    assert summary["headline"] == "Repairs failing"
    assert summary["suggested_action"] == "reprocess_stale"


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
        escalated_targets=[],
        data_source_errors=[],
    )

    summary = default_summary_from_body(body)
    assert summary["headline"] == "All clear"
    assert summary["suggested_action"] == "none"


def test_default_summary_from_body_escalation_suggests_support():
    body = render_health_body(
        generated_at=_GEN_AT,
        pipeline_day={"anomalies": []},
        recipe_outcomes_7d=[],
        escalated_targets=["20260607/local/seg2:screen"],
        data_source_errors=[],
    )

    summary = default_summary_from_body(body)
    # An escalated repair already failed twice → point at support, not retry.
    assert summary["suggested_action"] == "open_support"


def test_read_steward_summary_preserves_open_support(tmp_path, monkeypatch):
    _set_journal(monkeypatch, tmp_path)
    _seed_summary(
        "20260607",
        {
            "headline": "Repairs failing",
            "summary_sentence": "Sol couldn't fix two segments after retrying.",
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

    prepared = prepare_config({"name": "steward", "day": "20260607"})
    assert "output_path" not in prepared
