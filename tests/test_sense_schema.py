# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import copy
import json
from pathlib import Path

import frontmatter
from jsonschema import Draft202012Validator

from solstone.think.activities import DEFAULT_ACTIVITIES
from solstone.think.prompts import _resolve_facets, reset_identity_vars_cache
from solstone.think.talent import (
    RUNTIME_FACETS_SENTINEL,
    get_talent,
    hydrate_runtime_enums,
)

SENSE_PATH = Path(__file__).resolve().parents[1] / "solstone" / "talent" / "sense.md"
SENSE_SCHEMA_PATH = SENSE_PATH.with_suffix(".schema.json")
FACET_NAMING_PATH = (
    Path(__file__).resolve().parents[1]
    / "solstone"
    / "think"
    / "templates"
    / "facet_naming.md"
)


def _section(text: str, start: str, end: str | None = None) -> str:
    section_start = text.index(start)
    if end is None:
        return text[section_start:]
    section_end = text.index(end, section_start)
    return text[section_start:section_end]


def _facet_naming_template() -> str:
    return FACET_NAMING_PATH.read_text(encoding="utf-8").strip()


def _write_prompt_journal(tmp_path: Path, *, with_facet: bool) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"identity": {"name": "Test User", "preferred": "Tester"}}),
        encoding="utf-8",
    )
    if with_facet:
        facet_dir = tmp_path / "facets" / "steady"
        facet_dir.mkdir(parents=True)
        (facet_dir / "facet.json").write_text(
            json.dumps({"title": "Steady Facet", "description": "Prompt testing"}),
            encoding="utf-8",
        )


def _render_sense_for_tmp_journal(
    tmp_path: Path, monkeypatch, *, with_facet: bool
) -> str:
    _write_prompt_journal(tmp_path, with_facet=with_facet)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    reset_identity_vars_cache()
    return get_talent("sense")["user_instruction"]


def test_sense_prompt_parses_and_documents_role_and_source():
    post = frontmatter.load(SENSE_PATH)

    assert post.metadata["tier"] == 3

    output_schema = _section(
        post.content, "## Output Schema", "## Field-by-Field Instructions"
    )
    entities = _section(post.content, "### entities", "### facets")
    entity_props = get_talent("sense")["json_schema"]["properties"]["entities"][
        "items"
    ]["properties"]

    assert post.metadata["schema"] == "sense.schema.json"
    assert "Authoritative schema: `sense.schema.json`." in output_schema
    assert set(entity_props["role"]["enum"]) == {"attendee", "mentioned"}
    assert set(entity_props["source"]["enum"]) == {
        "voice",
        "speaker_label",
        "transcript",
        "screen",
        "other",
    }
    assert "#### role" in entities
    assert "#### source" in entities


def test_sense_loaded_json_schema_matches_on_disk_schema():
    on_disk = json.loads(SENSE_SCHEMA_PATH.read_text(encoding="utf-8"))

    assert get_talent("sense")["json_schema"] == on_disk


def test_content_type_enum_matches_default_activities_drift_detector():
    schedule_only = "Scheduled events emitted by talent/schedule.md"
    expected = [
        a["id"]
        for a in DEFAULT_ACTIVITIES
        if schedule_only not in a.get("instructions", "")
    ] + ["idle"]
    schema = json.loads(SENSE_SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["properties"]["content_type"]["enum"] == expected


def test_sense_schema_facet_uses_runtime_sentinel_constant():
    schema = json.loads(SENSE_SCHEMA_PATH.read_text(encoding="utf-8"))
    facet_schema = schema["properties"]["facets"]["items"]["properties"]["facet"]

    assert facet_schema["enum"] == [RUNTIME_FACETS_SENTINEL]


def test_sense_schema_speculative_facet_nullable_and_required():
    schema = json.loads(SENSE_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    valid = {
        "density": "active",
        "content_type": "coding",
        "activity_summary": "Writing tests.",
        "entities": [],
        "facets": [
            {
                "facet": RUNTIME_FACETS_SENTINEL,
                "activity": "Writing tests.",
                "level": "low",
            }
        ],
        "speculative_facet": "test-planning",
        "meeting_detected": False,
        "speakers": [],
        "recommend": {
            "screen_record": False,
            "speaker_attribution": False,
        },
        "emotional_register": "focused",
    }

    assert schema["properties"]["speculative_facet"] == {"type": ["string", "null"]}
    assert "speculative_facet" in schema["required"]
    assert list(validator.iter_errors(valid)) == []

    valid_null = dict(valid)
    valid_null["speculative_facet"] = None
    assert list(validator.iter_errors(valid_null)) == []

    missing = dict(valid)
    del missing["speculative_facet"]
    assert list(validator.iter_errors(missing))


def test_sense_prompt_renders_speculative_facet_instruction_in_steady_state(
    tmp_path, monkeypatch
):
    rendered = _render_sense_for_tmp_journal(tmp_path, monkeypatch, with_facet=True)
    speculative_section = _section(
        rendered, "### speculative_facet", "### meeting_detected"
    )

    assert "### speculative_facet" in rendered
    assert "Propose a name for a NEW facet" in speculative_section
    assert "level: low" in speculative_section
    assert _facet_naming_template() in speculative_section


def test_sense_prompt_keeps_forced_configured_facet_routing(tmp_path, monkeypatch):
    rendered = _render_sense_for_tmp_journal(tmp_path, monkeypatch, with_facet=True)
    facets_section = _section(rendered, "### facets", "### speculative_facet")

    assert "Always include at least one facet" in facets_section
    assert "MUST be one of the configured facets listed in the input" in facets_section
    assert "`facets` always has at least one entry" in rendered


def test_sense_prompt_suppresses_speculative_facet_when_configured_match_fits(
    tmp_path, monkeypatch
):
    rendered = _render_sense_for_tmp_journal(tmp_path, monkeypatch, with_facet=True)
    speculative_section = _section(
        rendered, "### speculative_facet", "### meeting_detected"
    )

    assert "level: medium" in speculative_section
    assert "level: high" in speculative_section
    assert "emit `null`" in speculative_section


def test_resolve_facets_zero_facet_discovery_embeds_facet_naming(tmp_path, monkeypatch):
    _write_prompt_journal(tmp_path, with_facet=False)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    resolved = _resolve_facets(None)

    assert "No facets are defined yet. You are in discovery mode." in resolved
    assert (
        "These names will be used to suggest journal organization to the user."
        in resolved
    )
    assert _facet_naming_template() in resolved


def test_hydrate_runtime_enums_replaces_facet_sentinel(monkeypatch):
    monkeypatch.setattr(
        "solstone.think.talent.get_facets",
        lambda: {"alpha": {}, "Beta": {}, "weird,name": {}, "valid_one": {}},
    )
    schema = {
        "properties": {"facet": {"type": "string", "enum": [RUNTIME_FACETS_SENTINEL]}}
    }

    hydrated = hydrate_runtime_enums(schema)

    assert hydrated["properties"]["facet"]["enum"] == ["alpha", "valid_one"]


def test_hydrate_runtime_enums_preserves_facet_minItems_when_facets_exist(
    monkeypatch,
):
    monkeypatch.setattr(
        "solstone.think.talent.get_facets",
        lambda: {"alpha": {}, "Beta": {}, "weird,name": {}, "valid_one": {}},
    )
    schema = {
        "type": "object",
        "properties": {
            "facets": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "facet": {
                            "type": "string",
                            "enum": [RUNTIME_FACETS_SENTINEL],
                        }
                    },
                },
            }
        },
    }

    hydrated = hydrate_runtime_enums(schema)
    facets_node = hydrated["properties"]["facets"]
    facet_schema = facets_node["items"]["properties"]["facet"]

    assert facets_node["minItems"] == 1
    assert facet_schema["enum"] == ["alpha", "valid_one"]
    Draft202012Validator.check_schema(hydrated)


def test_hydrate_runtime_enums_empty_facets_fallback(monkeypatch):
    monkeypatch.setattr("solstone.think.talent.get_facets", lambda: {})
    schema = {
        "type": "object",
        "properties": {"facet": {"type": "string", "enum": [RUNTIME_FACETS_SENTINEL]}},
    }

    hydrated = hydrate_runtime_enums(schema)
    facet_schema = hydrated["properties"]["facet"]

    assert facet_schema == {"type": "string"}
    Draft202012Validator.check_schema(hydrated)


def test_hydrate_runtime_enums_keeps_portable_facet_shape_on_empty_facets_fallback(
    monkeypatch,
):
    monkeypatch.setattr("solstone.think.talent.get_facets", lambda: {})
    schema = {
        "type": "object",
        "properties": {
            "facets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "facet": {
                            "type": "string",
                            "enum": [RUNTIME_FACETS_SENTINEL],
                        }
                    },
                },
            }
        },
    }

    hydrated = hydrate_runtime_enums(schema)
    facets_node = hydrated["properties"]["facets"]
    facet_schema = facets_node["items"]["properties"]["facet"]

    assert "minItems" not in facets_node
    assert facet_schema == {"type": "string"}
    Draft202012Validator.check_schema(hydrated)


def test_hydrate_runtime_enums_idempotent_and_pure(monkeypatch):
    monkeypatch.setattr("solstone.think.talent.get_facets", lambda: {})
    original = {"type": "object", "properties": {"x": {"type": "string"}}}
    saved_copy = copy.deepcopy(original)

    hydrated = hydrate_runtime_enums(original)

    assert hydrated == original
    assert original == saved_copy
    assert hydrated is not original
    assert hydrate_runtime_enums(hydrated) == hydrated


def test_hydrate_runtime_enums_none_passthrough():
    assert hydrate_runtime_enums(None) is None


def test_role_and_source_do_not_leak_into_other_sense_sections():
    content = frontmatter.load(SENSE_PATH).content

    sections = [
        _section(content, "### density", "### content_type"),
        _section(content, "### content_type", "### activity_summary"),
        _section(content, "### activity_summary", "### entities"),
        _section(content, "### facets", "### speculative_facet"),
        _section(content, "### speculative_facet", "### meeting_detected"),
        _section(content, "### meeting_detected", "### speakers"),
        _section(content, "### speakers", "### recommend"),
        _section(content, "### recommend", "### emotional_register"),
        _section(content, "### emotional_register", "## Rules"),
        _section(content, "## Rules"),
    ]

    for section in sections:
        assert "attendee|mentioned" not in section
        assert "voice|speaker_label|transcript|screen|other" not in section
        assert "#### role" not in section
        assert "#### source" not in section
