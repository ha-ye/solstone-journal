# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Text projections and formatters for structured talent outputs."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.formatters import format_file, get_formatter
from solstone.think.utils import get_journal, journal_relative_path

logger = logging.getLogger(__name__)

ABSENT_TEXT = "Not specified in this document"


@dataclass(frozen=True)
class TalentTextProjection:
    """Rendered text for one talent output artifact."""

    key: str
    stem: str
    relative_path: str
    text: str
    source_path: Path


def _first_object(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not entries:
        return None
    first = entries[0]
    return first if isinstance(first, dict) else None


def _clean(value: object) -> str:
    return str(value or "").strip()


def _append_section(lines: list[str], heading: str, body: str) -> None:
    if lines:
        lines.append("")
    lines.append(f"## {heading}")
    lines.append("")
    lines.append(body or ABSENT_TEXT)


def _append_item_detail(lines: list[str], label: str, value: str) -> None:
    if value:
        lines.append(f"  {label}: {value}")


def format_document_analysis(
    entries: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Render the structured Document Analysis talent output."""
    _ = context
    meta = {"indexer": {"agent": "documents"}}
    document = _first_object(entries)
    if document is None:
        return [], meta

    lines: list[str] = []
    _append_section(lines, "Overview", _clean(document.get("overview")))

    party_lines: list[str] = []
    parties = document.get("parties") or []
    if isinstance(parties, list):
        for party in parties:
            if not isinstance(party, dict):
                continue
            name = _clean(party.get("name"))
            role = _clean(party.get("role"))
            formal_term = _clean(party.get("formal_term"))
            tier = _clean(party.get("appointment_tier"))
            context_text = _clean(party.get("context"))
            if not any((name, role, formal_term, tier, context_text)):
                continue
            label = name or "Unnamed party"
            if role:
                label += f" - {role}"
            if formal_term:
                label += f" ({formal_term})"
            if tier and tier != "not_applicable":
                label += f" [{tier}]"
            party_lines.append(f"- {label}")
            if context_text:
                party_lines.append(f"  {context_text}")
    _append_section(
        lines,
        "Parties and Roles",
        "\n".join(party_lines) if party_lines else ABSENT_TEXT,
    )

    provision_lines: list[str] = []
    provisions = document.get("key_provisions") or []
    if isinstance(provisions, list):
        for provision in provisions:
            if not isinstance(provision, dict):
                continue
            provision_type = _clean(provision.get("type"))
            text = _clean(provision.get("text"))
            applies_to = _clean(provision.get("applies_to"))
            if not any((provision_type, text, applies_to)):
                continue
            prefix = f"**{provision_type}:** " if provision_type else ""
            provision_lines.append(f"- {prefix}{text}".rstrip())
            _append_item_detail(provision_lines, "Applies to", applies_to)
    _append_section(
        lines,
        "Key Provisions",
        "\n".join(provision_lines) if provision_lines else ABSENT_TEXT,
    )

    asset_lines: list[str] = []
    assets = document.get("assets") or []
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = _clean(asset.get("name"))
            asset_type = _clean(asset.get("asset_type"))
            disposition = _clean(asset.get("disposition"))
            if not any((name, asset_type, disposition)):
                continue
            label = f"**{name}**" if name else "Unnamed asset"
            if asset_type and asset_type != "unspecified":
                label += f" ({asset_type})"
            if disposition:
                label += f" - {disposition}"
            asset_lines.append(f"- {label}")
    _append_section(
        lines,
        "Assets and Property",
        "\n".join(asset_lines) if asset_lines else ABSENT_TEXT,
    )

    condition_lines: list[str] = []
    conditions = document.get("conditions") or []
    if isinstance(conditions, list):
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            trigger = _clean(condition.get("trigger"))
            effect = _clean(condition.get("effect"))
            timing = _clean(condition.get("date_or_timing"))
            if not any((trigger, effect, timing)):
                continue
            if trigger:
                line = f"- **{trigger}:** {effect}".rstrip()
            else:
                line = f"- {effect or timing}"
            condition_lines.append(line)
            _append_item_detail(condition_lines, "Timing", timing)
    _append_section(
        lines,
        "Conditions and Triggers",
        "\n".join(condition_lines) if condition_lines else ABSENT_TEXT,
    )

    date_lines: list[str] = []
    dates = document.get("important_dates") or []
    if isinstance(dates, list):
        for date_entry in dates:
            if not isinstance(date_entry, dict):
                continue
            date_text = _clean(date_entry.get("date"))
            meaning = _clean(date_entry.get("meaning"))
            if not any((date_text, meaning)):
                continue
            if date_text and meaning:
                date_lines.append(f"- **{date_text}:** {meaning}")
            else:
                date_lines.append(f"- {date_text or meaning}")
    _append_section(
        lines,
        "Important Dates",
        "\n".join(date_lines) if date_lines else ABSENT_TEXT,
    )

    _append_section(lines, "Summary", _clean(document.get("summary")))

    markdown = "\n".join(lines).strip()
    if not markdown:
        return [], meta
    return [{"markdown": markdown, "timestamp": 0, "source": document}], meta


def format_screen_record(
    entries: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Render the structured Screen Record talent output."""
    _ = context
    meta = {"indexer": {"agent": "screen"}}
    record = _first_object(entries)
    if record is None:
        return [], meta

    lines: list[str] = []
    narrative = _clean(record.get("narrative"))
    if narrative:
        lines.append(narrative)

    entity_lines: list[str] = []
    entities = record.get("entities") or []
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            entity_type = _clean(entity.get("type"))
            name = _clean(entity.get("name"))
            role = _clean(entity.get("role"))
            context_text = _clean(entity.get("context"))
            if not any((entity_type, name, role, context_text)):
                continue
            label = f"{entity_type}: {name}" if entity_type else name
            if not label:
                label = "Entity"
            if role:
                label += f" ({role})"
            if context_text:
                label += f" - {context_text}"
            entity_lines.append(f"- {label}")

    if lines:
        lines.append("")
    lines.append("## Entities")
    lines.append("")
    lines.extend(entity_lines or ["Not specified"])

    markdown = "\n".join(lines).strip()
    if not markdown:
        return [], meta
    return [{"markdown": markdown, "timestamp": 0, "source": record}], meta


def _render_json_projection(path: Path) -> str | None:
    try:
        chunks, _meta = format_file(path)
    except Exception:
        logger.warning("failed to render talent JSON output %s", path, exc_info=True)
        return None
    rendered = "\n".join(
        chunk["markdown"] for chunk in chunks if isinstance(chunk.get("markdown"), str)
    ).strip()
    return rendered or None


def _journal_rel(path: Path) -> str | None:
    try:
        return journal_relative_path(Path(get_journal()).resolve(), path.resolve())
    except ValueError:
        logger.warning("talent output is outside journal: %s", path, exc_info=True)
        return None


def iter_talent_text_projections(
    talents_dir: Path,
    stem_filter: Callable[[str], bool] | None = None,
) -> Iterator[TalentTextProjection]:
    """Yield one text projection per talent-output key under ``talents_dir``."""
    if not talents_dir.is_dir():
        return

    keys = {
        path.relative_to(talents_dir).with_suffix("").as_posix()
        for path in talents_dir.rglob("*")
        if path.is_file() and path.suffix in {".json", ".md"}
    }

    for key in sorted(keys):
        stem = Path(key).name
        if stem_filter is not None and not stem_filter(stem):
            continue
        json_path = talents_dir / f"{key}.json"
        md_path = talents_dir / f"{key}.md"
        if json_path.is_file():
            rel_path = _journal_rel(json_path)
            if rel_path is not None and get_formatter(rel_path) is not None:
                rendered = _render_json_projection(json_path)
                if rendered:
                    yield TalentTextProjection(
                        key=key,
                        stem=json_path.stem,
                        relative_path=json_path.relative_to(talents_dir).as_posix(),
                        text=rendered,
                        source_path=json_path,
                    )
                continue
        if md_path.is_file():
            try:
                text = md_path.read_text(encoding="utf-8").strip()
            except OSError:
                logger.warning(
                    "failed to read talent Markdown output %s", md_path, exc_info=True
                )
                continue
            if text:
                yield TalentTextProjection(
                    key=key,
                    stem=md_path.stem,
                    relative_path=md_path.relative_to(talents_dir).as_posix(),
                    text=text,
                    source_path=md_path,
                )


def talent_projection_map(talents_dir: Path) -> dict[str, str]:
    """Return rendered talent-output projections keyed by relative stem path."""
    return {
        projection.key: projection.text
        for projection in iter_talent_text_projections(talents_dir)
    }
