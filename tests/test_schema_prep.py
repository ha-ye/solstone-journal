# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from solstone.apps.timeline.rollup import build_rollup_schema
from solstone.think.models import SchemaValidationError, generate
from solstone.think.schema_prep import prepare_provider_schema, unsupported_keyword_hits
from solstone.think.talent import RUNTIME_FACETS_SENTINEL
from tests.eval_schemas import DEFAULT_CASES, load_cases

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bounded_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "string",
                    "maxLength": 12,
                    "pattern": "^[a-z]+$",
                    "enum": ["alpha", "beta"],
                },
            }
        },
        "required": ["labels"],
        "additionalProperties": False,
    }


def _discover_schemas() -> tuple[Any, ...]:
    discovered: list[Any] = []
    for path in sorted((REPO_ROOT / "solstone").glob("**/*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(schema.get("x-journal-contract"), dict):
            continue
        discovered.append(
            pytest.param(schema, id=path.relative_to(REPO_ROOT).as_posix())
        )
    discovered.append(pytest.param(build_rollup_schema(3), id="build_rollup_schema(3)"))
    return tuple(discovered)


def test_local_receives_canonical_copy(bounded_schema: dict[str, Any]) -> None:
    prepared = prepare_provider_schema(bounded_schema, "local")

    assert prepared == bounded_schema
    assert prepared is not bounded_schema


@pytest.mark.parametrize("provider", ["openai", "google"])
def test_openai_and_google_keep_array_bounds_and_strip_length_bounds(
    bounded_schema: dict[str, Any], provider: str
) -> None:
    prepared = prepare_provider_schema(bounded_schema, provider)

    labels = prepared["properties"]["labels"]  # type: ignore[index]
    item = labels["items"]
    assert labels["maxItems"] == 2
    assert "maxLength" not in item
    assert item["pattern"] == "^[a-z]+$"
    assert item["enum"] == ["alpha", "beta"]


def test_anthropic_strips_array_and_length_bounds(
    bounded_schema: dict[str, Any],
) -> None:
    prepared = prepare_provider_schema(bounded_schema, "anthropic")

    labels = prepared["properties"]["labels"]  # type: ignore[index]
    item = labels["items"]
    assert "maxItems" not in labels
    assert "maxLength" not in item
    assert item["pattern"] == "^[a-z]+$"
    assert item["enum"] == ["alpha", "beta"]


@pytest.mark.parametrize("provider", ["openai", "google", "anthropic"])
def test_morning_briefing_schema_is_provider_portable(provider: str) -> None:
    schema = json.loads(
        (REPO_ROOT / "solstone/talent/morning_briefing.schema.json").read_text(
            encoding="utf-8"
        )
    )

    prepared = prepare_provider_schema(schema, provider)

    assert unsupported_keyword_hits(prepared, provider) == []
    assert prepared["properties"]["reading"]["items"]["properties"]["facet"][
        "enum"
    ] == ["__RUNTIME_FACETS__"]
    assert (
        "pattern" in prepared["properties"]["your_day"]["items"]["properties"]["time"]
    )
    assert (
        "pattern"
        in prepared["properties"]["needs_attention"]["items"]["properties"]["source_id"]
    )


def test_schema_eval_cases_hydrate_runtime_facets() -> None:
    cases = load_cases(DEFAULT_CASES)
    morning_case = next(
        case for case in cases if case["name"] == "morning_briefing_20260708_trimmed"
    )

    facet_schema = morning_case["schema"]["properties"]["reading"]["items"][
        "properties"
    ]["facet"]

    assert facet_schema.get("enum") != [RUNTIME_FACETS_SENTINEL]
    assert RUNTIME_FACETS_SENTINEL not in facet_schema.get("enum", [])
    assert facet_schema["maxLength"] == 80


@pytest.mark.parametrize("provider", ["local", "openai", "google", "anthropic", "fake"])
def test_prepare_provider_schema_is_pure_and_idempotent(
    bounded_schema: dict[str, Any], provider: str
) -> None:
    original = copy.deepcopy(bounded_schema)
    prepared = prepare_provider_schema(bounded_schema, provider)

    assert bounded_schema == original
    assert prepare_provider_schema(prepared, provider) == prepared


def test_none_and_unknown_provider_passthrough(
    bounded_schema: dict[str, Any],
) -> None:
    assert prepare_provider_schema(None, "openai") is None

    prepared = prepare_provider_schema(bounded_schema, "fake")
    assert prepared == bounded_schema
    assert prepared is not bounded_schema


@pytest.mark.parametrize("provider", ["local", "openai", "google", "anthropic"])
@pytest.mark.parametrize("schema", _discover_schemas())
def test_shipped_schemas_prep_to_a_provider_supported_subset(
    schema: dict[str, Any], provider: str
) -> None:
    """Prep strips exactly the provider's unsupported keywords, and nothing else.

    Asserting byte-identity here would pin the suite to "no shipped schema is
    bounded", which the schema-bounds ratchet is designed to falsify.
    """
    original = copy.deepcopy(schema)
    prepared = prepare_provider_schema(schema, provider)

    assert schema == original
    assert unsupported_keyword_hits(prepared, provider) == []
    if unsupported_keyword_hits(schema, provider):
        assert prepared != schema
    else:
        assert prepared == schema


# Schemas carrying generation bounds. The schema-bounds ratchet
# (scripts/check_schema_bounds.py) grows this set; add entries as schemas
# graduate off its allowlist.
BOUNDED_SCHEMAS = (
    "solstone/talent/story.schema.json",
    "solstone/talent/documents.schema.json",
    "solstone/talent/screen.schema.json",
    "solstone/apps/entities/talent/entity_observer.schema.json",
)


def _load_shipped_schema(relative_path: str) -> dict[str, Any]:
    return json.loads((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


def _keyword_paths(node: Any, keyword: str, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == keyword:
                found.append(f"{path}/{key}")
            found.extend(_keyword_paths(value, keyword, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_keyword_paths(value, keyword, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize("relative_path", BOUNDED_SCHEMAS)
def test_bounded_schemas_really_carry_bounds(relative_path: str) -> None:
    """Guards the three tests below from passing vacuously."""
    schema = _load_shipped_schema(relative_path)

    assert _keyword_paths(schema, "maxLength")
    assert _keyword_paths(schema, "maxItems")


@pytest.mark.parametrize("relative_path", BOUNDED_SCHEMAS)
def test_bounded_schemas_reach_local_provider_with_bounds_intact(
    relative_path: str,
) -> None:
    schema = _load_shipped_schema(relative_path)

    assert prepare_provider_schema(schema, "local") == schema


@pytest.mark.parametrize("provider", ["openai", "google"])
@pytest.mark.parametrize("relative_path", BOUNDED_SCHEMAS)
def test_bounded_schemas_lose_only_maxlength_for_openai_and_google(
    relative_path: str, provider: str
) -> None:
    prepared = prepare_provider_schema(_load_shipped_schema(relative_path), provider)

    assert _keyword_paths(prepared, "maxLength") == []
    assert _keyword_paths(prepared, "maxItems")


@pytest.mark.parametrize("relative_path", BOUNDED_SCHEMAS)
def test_bounded_schemas_lose_every_size_bound_for_anthropic(
    relative_path: str,
) -> None:
    prepared = prepare_provider_schema(_load_shipped_schema(relative_path), "anthropic")

    assert _keyword_paths(prepared, "maxLength") == []
    assert _keyword_paths(prepared, "maxItems") == []


def _patched_generate(
    provider: str, schema: dict[str, Any], response_text: str
) -> MagicMock:
    """Run ``generate`` against a stubbed provider module; return its mock."""
    run_generate = MagicMock(
        return_value={"text": response_text, "finish_reason": "stop"}
    )
    provider_module = SimpleNamespace(run_generate=run_generate)

    with (
        patch(
            "solstone.think.models.resolve_provider",
            return_value=(provider, "model"),
        ),
        patch(
            "solstone.think.providers.get_provider_module",
            return_value=provider_module,
        ),
    ):
        generate("hello", "test.context", json_schema=schema)

    return run_generate


def test_generate_sends_reduced_schema_to_anthropic(
    bounded_schema: dict[str, Any],
) -> None:
    run_generate = _patched_generate("anthropic", bounded_schema, '{"labels": []}')

    sent = run_generate.call_args.kwargs["json_schema"]
    assert sent == prepare_provider_schema(bounded_schema, "anthropic")
    assert "maxItems" not in sent["properties"]["labels"]
    assert bounded_schema["properties"]["labels"]["maxItems"] == 2


def test_generate_sends_canonical_schema_to_local(
    bounded_schema: dict[str, Any],
) -> None:
    run_generate = _patched_generate("local", bounded_schema, '{"labels": []}')

    assert run_generate.call_args.kwargs["json_schema"] == bounded_schema


def test_generate_validates_response_against_canonical_schema(
    bounded_schema: dict[str, Any],
) -> None:
    """D4: bounds stripped from the anthropic request are still enforced on the
    response. A 3-item answer overruns the canonical maxItems:2 and fails loudly."""
    overrun = '{"labels": ["alpha", "beta", "alpha"]}'

    with pytest.raises(SchemaValidationError):
        _patched_generate("anthropic", bounded_schema, overrun)
