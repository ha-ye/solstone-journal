# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Hooks for the pulse cadence talent."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solstone.think.activities import load_activity_records
from solstone.think.awareness import get_current, get_imports
from solstone.think.day_accumulator import append_record, read_latest
from solstone.think.entities.loading import load_recent_entity_names
from solstone.think.facets import get_facets
from solstone.think.utils import (
    day_path,
    get_journal,
    iter_segments,
    now_ms,
    segment_path,
)

logger = logging.getLogger(__name__)

_MAX_UNITS = 8
_MAX_NEEDS = 7
_TITLE_MAX = 80
_SENTENCE_MAX = 240
_DETAILS_MAX = 1800
_NEED_MAX = 240
_PARTNER_MAX = 4000


def _today_from_config(config: dict) -> str:
    day = config.get("day")
    if isinstance(day, str) and day:
        return day
    return datetime.now().strftime("%Y%m%d")


def _generated_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _default_pulse() -> dict[str, Any]:
    return {
        "title": "Day in progress",
        "one_sentence": "The day is still taking shape.",
        "full_details": (
            "There is not enough current context to name a clear shape yet. "
            "Sol will keep watching for completed segments, anticipated events, "
            "and anything that needs the owner's attention."
        ),
        "needs_you": [],
    }


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _string(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return fallback


def _unit_ts(unit: dict[str, Any]) -> int:
    try:
        return int(unit.get("ts") or 0)
    except (TypeError, ValueError):
        return 0


def _candidate_segment_dirs(day: str, segment: str, stream: str | None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: Path) -> None:
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    if stream:
        add(segment_path(day, segment, stream, create=False))

    for _stream, segment_id, seg_dir in iter_segments(day):
        if segment_id == segment:
            add(seg_dir)

    add(day_path(day, create=False) / segment)
    return candidates


def _read_segment_timeline(
    day: str,
    unit: dict[str, Any],
    gaps: list[str],
) -> dict[str, Any] | None:
    segment = str(unit.get("segment") or "").strip()
    if not segment:
        gaps.append("completed segment missing segment id")
        return None
    stream_raw = unit.get("stream")
    stream = str(stream_raw).strip() if stream_raw else None

    try:
        candidates = _candidate_segment_dirs(day, segment, stream)
    except Exception as exc:
        gaps.append(f"could not resolve segment {segment}: {exc}")
        return None

    for seg_dir in candidates:
        timeline_path = seg_dir / "timeline.json"
        if not timeline_path.is_file():
            continue
        try:
            data = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            gaps.append(f"could not read timeline for segment {segment}: {exc}")
            return None
        if not isinstance(data, dict):
            gaps.append(f"timeline for segment {segment} was not an object")
            return None
        return {
            "segment": segment,
            "stream": stream,
            "ts": unit.get("ts"),
            "title": _string(data.get("title"), "Untitled segment"),
            "description": _string(data.get("description"), ""),
        }

    gaps.append(f"no timeline.json found for segment {segment}")
    return None


def _read_activity(
    day: str,
    unit: dict[str, Any],
    gaps: list[str],
) -> dict[str, Any] | None:
    facet_raw = unit.get("facet")
    facet = str(facet_raw).strip() if facet_raw else ""
    activity_id = str(unit.get("activity") or "").strip()
    if not facet or not activity_id:
        gaps.append(
            f"completed activity missing facet or id: {activity_id or '(none)'}"
        )
        return None

    try:
        records = load_activity_records(facet, day)
    except Exception as exc:
        gaps.append(f"could not load activities for {facet}: {exc}")
        return None

    for record in records:
        if str(record.get("id") or "").strip() != activity_id:
            continue
        return {
            "facet": facet,
            "activity": activity_id,
            "ts": unit.get("ts"),
            "title": _string(record.get("title"), activity_id.replace("_", " ")),
            "description": _string(record.get("description"), ""),
            "details": _string(record.get("details"), ""),
            "source": record.get("source"),
            "segments": record.get("segments") or [],
        }

    gaps.append(f"activity record not found: {facet}/{activity_id}")
    return None


def _completed_since(day: str, config: dict, gaps: list[str]) -> dict[str, Any]:
    window = config.get("cadence_window") if isinstance(config, dict) else None
    if not isinstance(window, dict):
        window = {}

    units: list[tuple[int, str, dict[str, Any]]] = []
    for unit in window.get("segments") or []:
        if isinstance(unit, dict):
            units.append((_unit_ts(unit), "segment", unit))
    for unit in window.get("activities") or []:
        if isinstance(unit, dict):
            units.append((_unit_ts(unit), "activity", unit))
    units.sort(key=lambda item: item[0], reverse=True)

    segments: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []
    for _ts, kind, unit in units[:_MAX_UNITS]:
        if kind == "segment":
            segment = _read_segment_timeline(day, unit, gaps)
            if segment is not None:
                segments.append(segment)
        else:
            activity = _read_activity(day, unit, gaps)
            if activity is not None:
                activities.append(activity)

    return {
        "since_ms": window.get("since_ms"),
        "input_segments": len(window.get("segments") or []),
        "input_activities": len(window.get("activities") or []),
        "segments": segments,
        "activities": activities,
    }


def _collect_anticipated_activities(day: str, gaps: list[str]) -> list[dict[str, Any]]:
    anticipated: list[dict[str, Any]] = []
    try:
        facet_names = list(get_facets())
    except Exception as exc:
        gaps.append(f"could not list facets for anticipated activities: {exc}")
        return anticipated

    for facet_name in facet_names:
        try:
            records = load_activity_records(facet_name, day)
        except Exception as exc:
            gaps.append(
                f"could not load anticipated activities for {facet_name}: {exc}"
            )
            continue
        for record in records:
            if record.get("source") != "anticipated":
                continue
            participants: list[str] = []
            for entry in record.get("participation") or []:
                if not isinstance(entry, dict) or entry.get("role") != "attendee":
                    continue
                name = str(entry.get("name") or "").strip()
                if name:
                    participants.append(name)
            anticipated.append(
                {
                    "title": record.get("title", ""),
                    "start": record.get("start") or "",
                    "end": record.get("end") or "",
                    "facet": facet_name,
                    "occurred": False,
                    "participants": participants,
                }
            )
    return anticipated


def _read_partner_profile(gaps: list[str]) -> str:
    path = Path(get_journal()) / "identity" / "partner.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        gaps.append("identity/partner.md missing")
        return "(missing)"
    except OSError as exc:
        gaps.append(f"could not read identity/partner.md: {exc}")
        return "(unavailable)"
    return text[:_PARTNER_MAX] if text else "(empty)"


def _awareness_context(gaps: list[str]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    try:
        context["current"] = get_current()
    except Exception as exc:
        gaps.append(f"could not read current awareness: {exc}")
        context["current"] = {}
    try:
        context["imports"] = get_imports()
    except Exception as exc:
        gaps.append(f"could not read import awareness: {exc}")
        context["imports"] = {}
    return context


def _recent_entities(gaps: list[str]) -> list[str]:
    try:
        names = load_recent_entity_names(limit=12)
    except Exception as exc:
        gaps.append(f"could not read recent entities: {exc}")
        return []
    return names or []


def pre_process(config: dict) -> dict | None:
    """Gather pulse context for the cadence generator."""
    try:
        day = _today_from_config(config)
        gaps: list[str] = []
        default = _default_pulse()
        config["_pulse_default"] = default

        previous = read_latest(day, "pulse")
        completed = _completed_since(day, config, gaps)
        awareness = _awareness_context(gaps)
        anticipated = _collect_anticipated_activities(day, gaps)
        recent_entities = _recent_entities(gaps)
        partner_profile = _read_partner_profile(gaps)

        config["_pulse_window_note"] = {
            "segments": len(completed["segments"]),
            "activities": len(completed["activities"]),
            "input_segments": completed["input_segments"],
            "input_activities": completed["input_activities"],
            "since_ms": completed["since_ms"],
            "gaps": list(gaps),
        }

        return {
            "template_vars": {
                "previous_pulse": (
                    _compact_json(previous)
                    if previous is not None
                    else "(none - first run)"
                ),
                "completed_since": _compact_json(completed),
                "awareness": _compact_json(awareness),
                "anticipated": _compact_json(anticipated),
                "recent_entities": _compact_json(recent_entities),
                "partner_profile": partner_profile,
                "gaps": "\n".join(f"- {gap}" for gap in gaps) if gaps else "(none)",
            }
        }
    except Exception as exc:
        logger.exception("pulse pre-hook failed")
        return {"skip_reason": f"pulse pre-hook failed: {exc}"}


def _parse_object(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        start = result.find("{")
        end = result.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(result[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _coerce_needs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    needs: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            needs.append(text[:_NEED_MAX])
        if len(needs) >= _MAX_NEEDS:
            break
    return needs


def _coerce_pulse(raw: Any) -> dict[str, Any] | None:
    data = _parse_object(raw)
    if data is None:
        return None
    title = data.get("title")
    one_sentence = data.get("one_sentence")
    full_details = data.get("full_details")
    needs_you = data.get("needs_you")
    if not all(isinstance(value, str) for value in (title, one_sentence, full_details)):
        return None
    title = title.strip()
    one_sentence = one_sentence.strip()
    full_details = full_details.strip()
    if not title or not one_sentence or not full_details:
        return None
    return {
        "title": title[:_TITLE_MAX],
        "one_sentence": one_sentence[:_SENTENCE_MAX],
        "full_details": full_details[:_DETAILS_MAX],
        "needs_you": _coerce_needs(needs_you),
    }


def _normalize_pulse(result: Any, default: dict[str, Any]) -> dict[str, Any]:
    summary = _coerce_pulse(result)
    if summary is None:
        return {
            "title": _string(default.get("title"), "Day in progress")[:_TITLE_MAX],
            "one_sentence": _string(
                default.get("one_sentence"), "The day is still taking shape."
            )[:_SENTENCE_MAX],
            "full_details": _string(
                default.get("full_details"),
                "There is not enough current context to name a clear shape yet.",
            )[:_DETAILS_MAX],
            "needs_you": _coerce_needs(default.get("needs_you")),
        }
    return summary


def post_process(result: str, config: dict) -> str:
    """Persist the normalized pulse summary to the day accumulator."""
    default = config.get("_pulse_default") or _default_pulse()
    summary = _normalize_pulse(result, default)
    day = _today_from_config(config)
    record = {
        **summary,
        "model": config.get("model"),
        "generated_at": _generated_at(),
        "ts": now_ms(),
        "window": config.get("_pulse_window_note") or {},
    }
    append_record(day, "pulse", record)
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
