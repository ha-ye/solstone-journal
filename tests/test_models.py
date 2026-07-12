# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for think.models module."""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import solstone.think.models as models_module
from solstone.think.models import (
    CLAUDE_OPUS_4,
    CLAUDE_SONNET_4,
    DEFAULT_MODEL_BY_PROVIDER,
    DEFAULT_PROVIDER_TIMEOUT_S,
    GEMINI_FLASH,
    GEMMA4_26B_A4B_4BIT,
    GPT_5_MINI,
    LOCAL_MODEL,
    NO_BRAIN_PROVIDER,
    PROMPT_PATHS,
    QWEN_35_9B,
    IncompleteJSONError,
    IncompleteTextError,
    NoBrainConfiguredError,
    ProviderResponseInvalidError,
    SchemaValidationError,
    _Family,
    _find_pricing_fallback,
    _parse_family_anthropic,
    _parse_family_gemini,
    _parse_family_openai,
    _validate_schema,
    agenerate,
    agenerate_with_result,
    calc_agent_cost,
    calc_token_cost,
    default_model_for_provider,
    finish_reason_error,
    generate,
    generate_with_result,
    get_context_registry,
    get_model_provider,
    get_usage_cost,
    is_local_provider_needed,
    iter_token_log,
    model_supports,
    resolve_provider,
)


def test_calc_token_cost_basic():
    """Test basic cost calculation with a known model."""
    token_data = {
        "model": "gpt-4o",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
        },
    }

    result = calc_token_cost(token_data)

    assert result is not None
    assert "total_cost" in result
    assert "input_cost" in result
    assert "output_cost" in result
    assert "currency" in result
    assert result["currency"] == "USD"
    assert result["total_cost"] > 0
    assert result["input_cost"] > 0
    assert result["output_cost"] > 0


def test_calc_token_cost_with_cache():
    """Test cost calculation with cached tokens."""
    token_data = {
        "model": "claude-sonnet-4-20250514",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cached_tokens": 500,
            "total_tokens": 1600,
        },
    }

    result = calc_token_cost(token_data)

    assert result is not None
    assert result["total_cost"] > 0
    # Cached tokens should reduce the cost compared to all uncached
    assert result["input_cost"] >= 0


def test_calc_agent_cost_uses_resolved_model_version_from_usage():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "model_version": "gemini-2.5-flash",
    }

    cost = calc_agent_cost("gemini-flash-latest", usage)

    assert cost is not None and cost > 0


def test_calc_token_cost_unknown_model():
    """Test that unknown models return None."""
    token_data = {
        "model": "random-model-xyz",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
        },
    }

    result = calc_token_cost(token_data)
    assert result is None


def test_get_model_provider_mlx_backend_models_are_local():
    assert get_model_provider(GEMMA4_26B_A4B_4BIT) == "local"
    assert get_model_provider(QWEN_35_9B) == "local"


@pytest.mark.parametrize("reason", ["length", "max_tokens", "MAX_TOKENS", " Length "])
def test_incomplete_json_error_sets_length_reason_code(reason):
    exc = IncompleteJSONError(reason, "")

    assert exc.reason_code == "incomplete_json_length"


@pytest.mark.parametrize("reason", ["safety", "content_filter", "recitation", "error"])
def test_incomplete_json_error_non_length_reasons_have_no_reason_code(reason):
    exc = IncompleteJSONError(reason, "")

    assert not hasattr(exc, "reason_code")


def test_incomplete_json_error_preserves_positional_and_keyword_construction():
    positional = IncompleteJSONError("length", "partial")
    keyword = IncompleteJSONError(reason="max_tokens", partial_text="body")

    assert positional.reason == "length"
    assert positional.partial_text == "partial"
    assert keyword.reason == "max_tokens"
    assert keyword.partial_text == "body"
    assert positional.reason_code == "incomplete_json_length"
    assert keyword.reason_code == "incomplete_json_length"


def test_classify_provider_error_uses_incomplete_json_reason_code():
    from solstone.think.providers.shared import classify_provider_error

    assert (
        classify_provider_error(IncompleteJSONError("length", ""), "local")
        == "incomplete_json_length"
    )
    assert (
        classify_provider_error(IncompleteJSONError("safety", ""), "local")
        != "incomplete_json_length"
    )


def test_calc_token_cost_gemma4_zero_cost():
    token_data = {
        "model": GEMMA4_26B_A4B_4BIT,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
        },
    }

    assert calc_token_cost(token_data) == {
        "total_cost": 0.0,
        "input_cost": 0.0,
        "output_cost": 0.0,
        "currency": "USD",
    }


def test_calc_token_cost_missing_data():
    """Test that missing data returns None."""
    # Missing model
    assert calc_token_cost({"usage": {"input_tokens": 1000}}) is None

    # Missing usage
    assert calc_token_cost({"model": "gpt-4o"}) is None

    # Empty dict
    assert calc_token_cost({}) is None


def test_calc_token_cost_with_reasoning_tokens():
    """Test cost calculation includes reasoning tokens in output."""
    token_data = {
        "model": "gpt-4o",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "reasoning_tokens": 50,
            "total_tokens": 1150,
        },
    }

    result = calc_token_cost(token_data)

    # Should succeed - reasoning tokens are implicitly part of output pricing
    assert result is not None
    assert result["total_cost"] > 0


# ---------------------------------------------------------------------------
# resolve_provider tests
# ---------------------------------------------------------------------------


@pytest.fixture
def use_fixtures_journal(monkeypatch):
    """Use the fixtures journal for provider config tests."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", "tests/fixtures/journal")


def _write_tmp_journal_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "journal.json").write_text(json.dumps(config))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def test_resolve_provider_default_generate(use_fixtures_journal):
    """Generate resolves the configured active provider/model."""
    provider, model = resolve_provider("generate")
    assert provider == "google"
    assert model == "gemini-custom-flash-test"


def test_resolve_provider_default_cogitate(use_fixtures_journal):
    """Cogitate resolves the configured active provider/model."""
    provider, model = resolve_provider("cogitate")
    assert provider == "openai"
    assert model == GPT_5_MINI


def test_resolve_provider_contexts_are_inert(monkeypatch, tmp_path):
    """Legacy exact/glob contexts cannot influence active route resolution."""
    _write_tmp_journal_config(
        tmp_path,
        monkeypatch,
        {
            "providers": {
                "generate": {
                    "provider": "google",
                    "model": "gemini-flash-latest",
                },
                "contexts": {
                    "test.openai": {
                        "provider": "openai",
                        "model": "gpt-5-mini",
                    },
                    "observe.*": {
                        "provider": "anthropic",
                        "model": "claude-haiku-4-5",
                    },
                },
            }
        },
    )

    provider, model = resolve_provider("generate")
    assert provider == "google"
    assert model == GEMINI_FLASH


def test_resolve_provider_ordering_witness(monkeypatch, tmp_path):
    """Explicit provider/model wins over key-presence fallback."""
    _write_tmp_journal_config(
        tmp_path,
        monkeypatch,
        {
            "providers": {
                "generate": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                }
            }
        },
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    assert resolve_provider("generate") == ("anthropic", "claude-haiku-4-5")


def test_resolve_provider_empty_context(use_fixtures_journal):
    """The resolver takes only an interface, not a context."""
    assert resolve_provider("generate") == ("google", "gemini-custom-flash-test")


def test_resolve_provider_no_config(monkeypatch, tmp_path):
    """Test no-brain resolution when no provider config exists."""
    # Use a journal path with no config
    empty_journal = tmp_path / "empty_journal"
    empty_journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(empty_journal))
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "solstone.think.providers.state.local_runtime_ready", lambda: False
    )

    provider, model = resolve_provider("generate")
    assert provider == NO_BRAIN_PROVIDER
    assert provider != "google"
    assert model == ""

    provider, model = resolve_provider("cogitate")
    assert provider == NO_BRAIN_PROVIDER
    assert provider != "google"
    assert model == ""


def test_prompt_paths_exist():
    """Test all PROMPT_PATHS files exist and have valid frontmatter."""
    from pathlib import Path

    import frontmatter

    base_dir = Path(__file__).parent.parent / "solstone"  # Package root
    required_keys = {"context", "label", "group"}

    for rel_path in PROMPT_PATHS:
        path = base_dir / rel_path
        assert path.exists(), f"Prompt file not found: {rel_path}"

        post = frontmatter.load(path)
        meta = post.metadata or {}

        assert required_keys <= set(meta.keys()), (
            f"{rel_path} missing keys: {required_keys - set(meta.keys())}"
        )
        assert isinstance(meta["label"], str) and meta["label"], (
            f"{rel_path} has invalid label: {meta['label']}"
        )
        assert isinstance(meta["group"], str) and meta["group"], (
            f"{rel_path} has invalid group: {meta['group']}"
        )


def test_prompt_contexts_in_registry():
    """Test prompt contexts are discovered and in registry."""
    registry = get_context_registry()

    # Verify known prompt contexts exist with correct values
    assert "observe.describe.frame" in registry
    assert registry["observe.describe.frame"]["group"] == "Observe"

    assert "observe.enrich" in registry

    assert "detect.created" in registry


def test_default_model_by_provider():
    assert DEFAULT_MODEL_BY_PROVIDER == {
        "google": GEMINI_FLASH,
        "openai": GPT_5_MINI,
        "anthropic": CLAUDE_SONNET_4,
        "local": LOCAL_MODEL,
    }
    for provider, model in DEFAULT_MODEL_BY_PROVIDER.items():
        assert default_model_for_provider(provider) == model


@pytest.mark.parametrize(
    "config",
    [
        {"providers": {"generate": {"provider": "local"}}},
        {"providers": {"cogitate": {"provider": "local"}}},
    ],
)
def test_is_local_provider_needed_true_for_selected_surfaces(config):
    assert is_local_provider_needed(config) is True


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"providers": {"generate": {"provider": "google"}}},
        {"providers": {"contexts": {"talent.*": {"provider": "anthropic"}}}},
        {"providers": {"contexts": {"talent.*": {"provider": "local"}}}},
        {"providers": []},
    ],
)
def test_is_local_provider_needed_false_when_not_selected(config, monkeypatch):
    monkeypatch.setattr(
        "solstone.think.providers.state.local_runtime_ready", lambda: False
    )
    assert is_local_provider_needed(config) is False


def test_is_local_provider_needed_true_for_implicit_local(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "solstone.think.providers.state.local_runtime_ready", lambda: True
    )

    assert is_local_provider_needed({}) is True


def test_resolve_provider_legacy_keys_are_inert(
    use_fixtures_journal, monkeypatch, tmp_path
):
    """Tier/backup/contexts/models legacy keys do not affect active routing."""
    _write_tmp_journal_config(
        tmp_path,
        monkeypatch,
        {
            "providers": {
                "generate": {
                    "provider": "google",
                    "model": "gemini-flash-latest",
                    "tier": 1,
                    "backup": "anthropic",
                },
                "contexts": {
                    "talent.timeline.segment_summary": {"provider": "local"},
                    "observe.*": {"provider": "anthropic", "tier": 3},
                },
                "models": {
                    "google": {"1": "gemini-pro-latest"},
                    "anthropic": {"3": "claude-haiku-4-5"},
                },
            }
        },
    )

    assert resolve_provider("generate") == ("google", GEMINI_FLASH)


def test_resolve_provider_model_key_wins_even_when_tier_present(
    use_fixtures_journal, monkeypatch, tmp_path
):
    """The retired AC3a quirk is gone: model is honored even with tier present."""
    _write_tmp_journal_config(
        tmp_path,
        monkeypatch,
        {
            "providers": {
                "generate": {
                    "provider": "google",
                    "tier": 1,
                    "model": "gemini-custom-flash-test",
                }
            }
        },
    )

    assert resolve_provider("generate") == ("google", "gemini-custom-flash-test")


def test_resolve_provider_local_type_default_ignores_context_pins(
    use_fixtures_journal, monkeypatch, tmp_path
):
    """Context pins cannot push a local active interface onto cloud."""
    _write_tmp_journal_config(
        tmp_path,
        monkeypatch,
        {
            "providers": {
                "generate": {"provider": "local"},
                "contexts": {
                    "talent.timeline.segment_summary": {
                        "provider": "google",
                        "model": "gemini-flash-lite-latest",
                    },
                },
            }
        },
    )

    assert resolve_provider("generate") == ("local", LOCAL_MODEL)


def test_legacy_context_toggles_remain_on_disk_but_not_routing(
    use_fixtures_journal, monkeypatch, tmp_path
):
    """Grandfathered context keys stay inert next to disabled/extract toggles."""
    _write_tmp_journal_config(
        tmp_path,
        monkeypatch,
        {
            "providers": {
                "generate": {"provider": "google", "model": GEMINI_FLASH},
                "contexts": {
                    "talent.x": {
                        "provider": "local",
                        "tier": 3,
                        "disabled": True,
                        "extract": "foo",
                    }
                },
            }
        },
    )

    assert resolve_provider("generate") == ("google", GEMINI_FLASH)

    stored = json.loads((tmp_path / "config" / "journal.json").read_text())
    context = stored["providers"]["contexts"]["talent.x"]
    assert context["provider"] == "local"
    assert context["disabled"] is True
    assert context["extract"] == "foo"
    assert context["tier"] == 3


def test_prepare_config_legacy_context_routing_keys_are_inert(
    journal_copy: Path,
) -> None:
    """Legacy contexts survive on disk but cannot change prepared identity."""
    from solstone.think.talents import prepare_config

    config_path = journal_copy / "config" / "journal.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["providers"] = {
        "generate": {"provider": "anthropic", "model": CLAUDE_SONNET_4},
        "contexts": {
            "talent.timeline.segment_summary": {
                "provider": "google",
                "model": "gemini-flash-lite-latest",
                "tier": 3,
                "backup": "openai",
            }
        },
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    prepared = prepare_config({"name": "timeline:segment_summary"})

    assert prepared["provider"] == "anthropic"
    assert prepared["model"] == CLAUDE_SONNET_4


def test_prepare_config_frontmatter_provider_pin_is_dead_through_dispatch_identity(
    journal_copy: Path,
) -> None:
    """Removed google frontmatter pins do not override the active brain."""
    from solstone.think.talents import prepare_config

    config_path = journal_copy / "config" / "journal.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["env"] = {"GOOGLE_API_KEY": "test-google-key"}
    config["providers"] = {
        "generate": {"provider": "anthropic", "model": CLAUDE_SONNET_4}
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    segment = prepare_config({"name": "timeline:segment_summary"})
    detection = prepare_config({"name": "entities:detection"})

    assert segment["provider"] == "anthropic"
    assert segment["model"] == CLAUDE_SONNET_4
    assert detection["provider"] == "anthropic"
    assert detection["model"] == CLAUDE_SONNET_4


def test_resolve_provider_cogitate_system_talents_stay_local(
    use_fixtures_journal, monkeypatch, tmp_path
):
    """Cogitate system talents stay local when the cogitate lane is local."""
    _write_tmp_journal_config(
        tmp_path,
        monkeypatch,
        {"providers": {"cogitate": {"provider": "local"}}},
    )

    assert resolve_provider("cogitate") == ("local", LOCAL_MODEL)


def test_resolve_provider_split_lane_other_type_stays_cloud(
    use_fixtures_journal, monkeypatch, tmp_path
):
    """A local generate lane does not force the cogitate lane local."""
    _write_tmp_journal_config(
        tmp_path,
        monkeypatch,
        {
            "providers": {
                "generate": {"provider": "local"},
                "cogitate": {"provider": "openai"},
            }
        },
    )

    assert resolve_provider("generate") == ("local", LOCAL_MODEL)
    assert resolve_provider("cogitate") == ("openai", GPT_5_MINI)


def test_generate_rejects_cloud_model_override_for_local_provider():
    with (
        patch(
            "solstone.think.models.resolve_provider",
            return_value=("local", "local/qwen3.5-4b"),
        ),
        patch(
            "solstone.think.providers.get_provider_module",
            side_effect=AssertionError("provider module should not be invoked"),
        ),
    ):
        with pytest.raises(ValueError, match="local provider cannot serve"):
            generate("hello", "test.context", model="gpt-5.5")


def test_generate_with_result_rejects_cloud_model_override_for_local_provider():
    with (
        patch(
            "solstone.think.models.resolve_provider",
            return_value=("local", "local/qwen3.5-4b"),
        ),
        patch(
            "solstone.think.providers.get_provider_module",
            side_effect=AssertionError("provider module should not be invoked"),
        ),
    ):
        with pytest.raises(ValueError, match="local provider cannot serve"):
            generate_with_result("hello", "test.context", model="gpt-5.5")


def test_agenerate_rejects_cloud_model_override_for_local_provider():
    with (
        patch(
            "solstone.think.models.resolve_provider",
            return_value=("local", "local/qwen3.5-4b"),
        ),
        patch(
            "solstone.think.providers.get_provider_module",
            side_effect=AssertionError("provider module should not be invoked"),
        ),
    ):
        with pytest.raises(ValueError, match="local provider cannot serve"):
            asyncio.run(agenerate("hello", "test.context", model="gpt-5.5"))


def test_generate_stops_before_provider_module_when_no_brain():
    with (
        patch(
            "solstone.think.models.resolve_provider",
            return_value=(NO_BRAIN_PROVIDER, ""),
        ),
        patch(
            "solstone.think.providers.get_provider_module",
            side_effect=AssertionError("provider module should not be invoked"),
        ),
    ):
        with pytest.raises(NoBrainConfiguredError):
            generate("hello", "test.context")


def test_generate_with_result_stops_before_provider_module_when_no_brain():
    with (
        patch(
            "solstone.think.models.resolve_provider",
            return_value=(NO_BRAIN_PROVIDER, ""),
        ),
        patch(
            "solstone.think.providers.get_provider_module",
            side_effect=AssertionError("provider module should not be invoked"),
        ),
    ):
        with pytest.raises(NoBrainConfiguredError):
            generate_with_result("hello", "test.context")


def test_agenerate_stops_before_provider_module_when_no_brain():
    with (
        patch(
            "solstone.think.models.resolve_provider",
            return_value=(NO_BRAIN_PROVIDER, ""),
        ),
        patch(
            "solstone.think.providers.get_provider_module",
            side_effect=AssertionError("provider module should not be invoked"),
        ),
    ):
        with pytest.raises(NoBrainConfiguredError):
            asyncio.run(agenerate("hello", "test.context"))


def test_unknown_model_override_for_local_provider_proceeds_to_provider():
    provider_module = SimpleNamespace(
        run_generate=MagicMock(
            return_value={"text": "ok", "finish_reason": "stop"},
        ),
        run_agenerate=AsyncMock(
            return_value={"text": "ok", "finish_reason": "stop"},
        ),
    )

    with (
        patch(
            "solstone.think.models.resolve_provider",
            return_value=("local", "local/qwen3.5-4b"),
        ),
        patch(
            "solstone.think.providers.get_provider_module",
            return_value=provider_module,
        ),
    ):
        assert generate("hello", "test.context", model="served-model") == "ok"
        assert generate_with_result("hello", "test.context", model="served-model") == {
            "text": "ok",
            "finish_reason": "stop",
        }
        assert (
            asyncio.run(agenerate("hello", "test.context", model="served-model"))
            == "ok"
        )

    assert provider_module.run_generate.call_count == 2
    assert provider_module.run_agenerate.call_count == 1


# ---------------------------------------------------------------------------
# Dynamic context registry tests
# ---------------------------------------------------------------------------


def test_context_registry_includes_prompt_contexts():
    """Test that registry includes all contexts from PROMPT_PATHS."""
    from pathlib import Path

    import frontmatter

    registry = get_context_registry()
    base_dir = Path(__file__).parent.parent / "solstone"

    # All prompt contexts should be in registry with matching metadata
    for rel_path in PROMPT_PATHS:
        path = base_dir / rel_path
        post = frontmatter.load(path)
        meta = post.metadata or {}
        context = meta.get("context")

        assert context in registry, f"Prompt context {context} not in registry"
        assert registry[context]["label"] == meta["label"]
        assert registry[context]["group"] == meta["group"]


def test_context_registry_includes_categories():
    """Test that registry includes discovered category contexts."""
    registry = get_context_registry()

    # Should have category entries (from observe/categories/*.md)
    category_contexts = [k for k in registry if k.startswith("observe.describe.")]

    # Should have frame + all categories (browsing, code, gaming, etc.)
    assert len(category_contexts) > 5, "Should discover category contexts"

    # Each category context should have required fields
    for context in category_contexts:
        assert "label" in registry[context]
        assert "group" in registry[context]


def test_context_registry_includes_talent_configs():
    """Test that registry includes discovered talent contexts (agents + generators)."""
    registry = get_context_registry()

    # Should have talent entries (from talent/*.md and apps/*/talent/*.md)
    talent_contexts = [k for k in registry if k.startswith("talent.")]

    # Should have multiple talent contexts (agents + generators)
    assert len(talent_contexts) > 1, "Should discover talent contexts"

    # Should have system talent configs
    system_talent = [k for k in talent_contexts if k.startswith("talent.system.")]
    assert len(system_talent) > 0, "Should discover system talent configs"

    # Should have app talent configs
    app_talent = [
        k
        for k in talent_contexts
        if k.startswith("talent.") and not k.startswith("talent.system.")
    ]
    assert len(app_talent) > 0, "Should discover app talent configs"

    # Should include type field for talent contexts
    for context in talent_contexts:
        assert "type" in registry[context], f"{context} missing type field"


def test_context_registry_structure():
    """Test that all registry entries have required fields."""
    registry = get_context_registry()
    required_keys = {"label", "group"}

    for context, config in registry.items():
        assert isinstance(config, dict), f"{context} should be a dict"
        assert required_keys <= set(config.keys()), (
            f"{context} missing keys: {required_keys - set(config.keys())}"
        )


def test_context_registry_is_cached():
    """Test that registry is built once and cached."""
    registry1 = get_context_registry()
    registry2 = get_context_registry()

    # Should return the same object (cached)
    assert registry1 is registry2


# ---------------------------------------------------------------------------
# Model pricing support tests
# ---------------------------------------------------------------------------


def test_all_default_models_have_pricing():
    """Verify all default provider models have genai-prices support.

    This test ensures that when default models are updated, we catch any
    missing pricing data early. If this test fails:

    1. Run: make update-prices
    2. Re-run this test
    3. If still failing, the model may be too new for genai-prices

    See think/models.py model constants section for more details.
    """
    all_models = set(DEFAULT_MODEL_BY_PROVIDER.values())
    all_models.add(CLAUDE_OPUS_4)

    missing_pricing = []
    for model in sorted(all_models):
        token_data = {
            "model": model,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
            },
        }
        result = calc_token_cost(token_data)
        if result is None:
            missing_pricing.append(model)

    if missing_pricing:
        pytest.fail(
            f"Models missing genai-prices support: {missing_pricing}\n"
            "Run 'make update-prices' and re-test. "
            "If still failing, model may be too new for genai-prices."
        )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.5", _Family(("openai", None), (5, 5))),
        ("gpt-5", _Family(("openai", None), (5, 0))),
        ("gpt-5.5-mini", _Family(("openai", "mini"), (5, 5))),
        ("gpt-5.4-nano", _Family(("openai", "nano"), (5, 4))),
        ("gpt-5.2-pro", _Family(("openai", "pro"), (5, 2))),
    ],
)
def test_parse_family_openai(model, expected):
    assert _parse_family_openai(model) == expected


@pytest.mark.parametrize(
    "model",
    [
        "ft:gpt-5",
        "gpt-5-image",
        "gpt-5-image-mini",
        "gpt-5.1-codex-mini",
        "gpt-4o",
        "o3",
        "text-embedding-3-small",
    ],
)
def test_parse_family_openai_rejects_unsupported_models(model):
    assert _parse_family_openai(model) is None


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4", _Family(("anthropic", "opus"), (4, 0))),
        ("claude-opus-4-7", _Family(("anthropic", "opus"), (4, 7))),
        ("claude-sonnet-4-6", _Family(("anthropic", "sonnet"), (4, 6))),
        ("claude-haiku-5", _Family(("anthropic", "haiku"), (5, 0))),
    ],
)
def test_parse_family_anthropic(model, expected):
    assert _parse_family_anthropic(model) == expected


@pytest.mark.parametrize(
    "model",
    [
        "claude-3-opus-latest",
        "claude-3-5-haiku-latest",
        "claude-v2",
        "claude-2",
        "claude-sonnet-4-6-latest",
    ],
)
def test_parse_family_anthropic_rejects_unsupported_models(model):
    assert _parse_family_anthropic(model) is None


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gemini-3.5-flash", _Family(("gemini", "flash"), (3, 5))),
        ("gemini-3-flash", _Family(("gemini", "flash"), (3, 0))),
        ("gemini-3.1-flash-lite", _Family(("gemini", "flash-lite"), (3, 1))),
        ("gemini-2.5-pro-preview", _Family(("gemini", "pro"), (2, 5))),
        ("gemini-flash-latest", _Family(("gemini", "flash"), (0, 0))),
        ("gemini-pro-latest", _Family(("gemini", "pro"), (0, 0))),
        ("gemini-flash-lite-latest", _Family(("gemini", "flash-lite"), (0, 0))),
    ],
)
def test_parse_family_gemini(model, expected):
    assert _parse_family_gemini(model) == expected


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3-pro-image-preview",
        "gemini-2.5-flash-image",
        "gemini-pro",
        "gemini-embedding-001",
        "gemini-live-2.5-flash-preview",
        "gemini-flash-1.5",
        "gemma-3",
    ],
)
def test_parse_family_gemini_rejects_unsupported_models(model):
    assert _parse_family_gemini(model) is None


@pytest.mark.parametrize(
    ("model", "provider_id", "expected"),
    [
        ("gemini-3.5-flash", "google", "gemini-3-flash-preview"),
        ("gemini-3.1-flash-lite", "google", "gemini-2.5-flash-lite"),
        ("gemini-flash-latest", "google", "gemini-3-flash-preview"),
        ("gemini-pro-latest", "google", "gemini-3.1-pro-preview"),
        ("gemini-flash-lite-latest", "google", "gemini-2.5-flash-lite"),
        ("claude-opus-4-7", "anthropic", "claude-opus-4-6"),
        ("claude-sonnet-4-6", "anthropic", "claude-sonnet-4-6"),
        ("claude-haiku-5", "anthropic", "claude-haiku-4-5"),
        ("gpt-5.5", "openai", "gpt-5.2"),
        ("gpt-5.5-mini", "openai", "gpt-5-mini"),
        ("totally-fake-model", "openai", None),
        ("text-embedding-3-small", "openai", None),
    ],
)
def test_find_pricing_fallback(model, provider_id, expected):
    _find_pricing_fallback.cache_clear()

    assert _find_pricing_fallback(model, provider_id) == expected


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "claude-opus-4-7",
        "gpt-5.5",
    ],
)
def test_calc_token_cost_fallback(model):
    result = calc_token_cost(
        {"model": model, "usage": {"input_tokens": 1000, "output_tokens": 100}}
    )

    assert result is not None
    assert result["total_cost"] > 0


def test_calc_token_cost_fallback_returns_none_for_unknown_model():
    assert (
        calc_token_cost(
            {
                "model": "totally-fake-model",
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )
        is None
    )


def test_calc_token_cost_fallback_keeps_local_free():
    assert (
        calc_token_cost(
            {
                "model": LOCAL_MODEL,
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )["total_cost"]
        == 0.0
    )


def test_fallback_logging(caplog):
    models_module._LOGGED_FALLBACKS.clear()

    with caplog.at_level(logging.INFO, logger="solstone.think.models"):
        calc_token_cost(
            {
                "model": "gemini-3.5-flash",
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )
        calc_token_cost(
            {
                "model": "gemini-3.5-flash",
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )

    fallback_logs = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("pricing: family-fallback")
    ]
    assert fallback_logs == [
        "pricing: family-fallback gemini-3.5-flash -> gemini-3-flash-preview"
    ]

    with caplog.at_level(logging.INFO, logger="solstone.think.models"):
        calc_token_cost(
            {
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )

    fallback_logs = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("pricing: family-fallback")
    ]
    assert fallback_logs == [
        "pricing: family-fallback gemini-3.5-flash -> gemini-3-flash-preview",
        "pricing: family-fallback claude-opus-4-7 -> claude-opus-4-6",
    ]


# ---------------------------------------------------------------------------
# get_usage_cost tests
# ---------------------------------------------------------------------------


def test_get_usage_cost_nonexistent_day(use_fixtures_journal):
    """Test that nonexistent day returns zeros."""
    result = get_usage_cost("19000101")
    assert result == {"requests": 0, "tokens": 0, "cost": 0.0}


def test_get_usage_cost_day_total(use_fixtures_journal):
    """Test aggregating all entries for a day."""
    # 20250823 has test entries with gemini models
    result = get_usage_cost("20250823")
    assert result["requests"] > 0
    assert isinstance(result["tokens"], int)
    assert isinstance(result["cost"], float)


def test_iter_token_log_preserves_type_field(use_fixtures_journal):
    """Token log iterator should preserve top-level type field."""
    entries = list(iter_token_log("20250823"))
    generate_entries = [entry for entry in entries if entry.get("type") == "generate"]

    assert generate_entries
    assert any(
        entry.get("context") == "think.detect_created.classify_new_file"
        for entry in generate_entries
    )


def test_get_usage_cost_context_filter(use_fixtures_journal):
    """Test filtering by context prefix."""
    # Filter to test contexts
    result = get_usage_cost("20250823", context="tests.test_gemini")
    assert result["requests"] > 0

    # Filter to non-matching context should return zeros
    result_empty = get_usage_cost("20250823", context="nonexistent.context")
    assert result_empty["requests"] == 0


def test_get_usage_cost_segment_filter(use_fixtures_journal):
    """Test filtering by segment key."""
    # Fixture data includes one entry tagged with segment 143022_300
    result = get_usage_cost("20250823", segment="143022_300")
    assert result["requests"] == 1
    assert result["tokens"] == 7000
    assert result["cost"] > 0.0


def test_get_usage_cost_combined_filters(use_fixtures_journal):
    """Test combined segment and context filters."""
    # With both filters, entries must match both
    result = get_usage_cost(
        "20250823",
        segment="nonexistent",
        context="tests.test_gemini",
    )
    # Segment doesn't exist, so no matches
    assert result["requests"] == 0


# ---------------------------------------------------------------------------
# log_token_usage normalization tests
# ---------------------------------------------------------------------------


def test_log_token_usage_computes_total_tokens(tmp_path, monkeypatch):
    """total_tokens is computed from input+output when missing (Codex CLI format)."""
    import json

    from solstone.think.models import log_token_usage

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    # Codex CLI format: no total_tokens
    log_token_usage(
        model="gpt-5.2",
        usage={"input_tokens": 1000, "output_tokens": 200},
        context="test",
    )

    log_file = tmp_path / "tokens" / (__import__("time").strftime("%Y%m%d") + ".jsonl")
    entry = json.loads(log_file.read_text().strip())
    assert entry["usage"]["total_tokens"] == 1200
    assert entry["usage"]["input_tokens"] == 1000
    assert entry["usage"]["output_tokens"] == 200


def test_log_token_usage_preserves_existing_total_tokens(tmp_path, monkeypatch):
    """total_tokens is preserved when already present and non-zero."""
    import json

    from solstone.think.models import log_token_usage

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    log_token_usage(
        model="gpt-5.2",
        usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1500},
        context="test",
    )

    log_file = tmp_path / "tokens" / (__import__("time").strftime("%Y%m%d") + ".jsonl")
    entry = json.loads(log_file.read_text().strip())
    assert entry["usage"]["total_tokens"] == 1500


def test_log_token_usage_maps_cached_input_tokens(tmp_path, monkeypatch):
    """cached_input_tokens (Codex CLI format) maps to cached_tokens."""
    import json

    from solstone.think.models import log_token_usage

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    log_token_usage(
        model="gpt-5.2",
        usage={
            "input_tokens": 1000,
            "cached_input_tokens": 800,
            "output_tokens": 200,
        },
        context="test",
    )

    log_file = tmp_path / "tokens" / (__import__("time").strftime("%Y%m%d") + ".jsonl")
    entry = json.loads(log_file.read_text().strip())
    assert entry["usage"]["cached_tokens"] == 800
    assert entry["usage"]["total_tokens"] == 1200


def test_log_token_usage_passes_through_reasoning_tokens(tmp_path, monkeypatch):
    """reasoning_tokens from provider-normalized usage are preserved in log."""
    import json

    from solstone.think.models import log_token_usage

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    # Normalized usage from Google provider (the bug: reasoning_tokens were dropped)
    log_token_usage(
        model="gemini-3-flash-preview",
        usage={
            "input_tokens": 13319,
            "output_tokens": 969,
            "total_tokens": 37878,
            "reasoning_tokens": 23590,
        },
        context="test",
    )

    log_file = tmp_path / "tokens" / (__import__("time").strftime("%Y%m%d") + ".jsonl")
    entry = json.loads(log_file.read_text().strip())
    assert entry["usage"]["reasoning_tokens"] == 23590
    assert entry["usage"]["total_tokens"] == 37878
    assert entry["usage"]["input_tokens"] == 13319
    assert entry["usage"]["output_tokens"] == 969


def test_log_token_usage_passes_through_cache_creation_tokens(tmp_path, monkeypatch):
    """cache_creation_tokens from Anthropic provider are preserved in log."""
    import json

    from solstone.think.models import log_token_usage

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    log_token_usage(
        model="claude-sonnet-4-5",
        usage={
            "input_tokens": 5000,
            "output_tokens": 1000,
            "total_tokens": 6000,
            "cached_tokens": 3000,
            "cache_creation_tokens": 2000,
        },
        context="test",
    )

    log_file = tmp_path / "tokens" / (__import__("time").strftime("%Y%m%d") + ".jsonl")
    entry = json.loads(log_file.read_text().strip())
    assert entry["usage"]["cache_creation_tokens"] == 2000
    assert entry["usage"]["cached_tokens"] == 3000


def test_log_token_usage_logs_append_failure(tmp_path, monkeypatch, caplog):
    import builtins

    from solstone.think.models import log_token_usage

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    real_open = builtins.open

    def fail_token_open(path, *args, **kwargs):
        if str(path).endswith(".jsonl"):
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_token_open)

    with caplog.at_level(logging.WARNING, logger="solstone.think.models"):
        log_token_usage(
            model="gpt-5.2",
            usage={"input_tokens": 1000, "output_tokens": 200},
            context="test",
        )

    assert "failed to log token usage" in caplog.text


class TestModelSupports:
    def test_opus_4_7_temperature_not_supported(self):
        assert model_supports(CLAUDE_OPUS_4, "temperature") is False

    def test_sonnet_4_6_temperature_supported(self):
        assert model_supports(CLAUDE_SONNET_4, "temperature") is True

    def test_unlisted_param_defaults_supported(self):
        assert model_supports(CLAUDE_OPUS_4, "max_tokens") is True

    def test_unlisted_model_defaults_supported(self):
        assert model_supports("gpt-5.5", "temperature") is True


class TestValidateSchema:
    def test_valid_instance(self):
        schema = {
            "type": "object",
            "properties": {"field": {"type": "string"}},
            "required": ["field"],
        }

        result = _validate_schema('{"field": "ok"}', schema)

        assert result == {"valid": True, "errors": []}

    def test_schema_violation_type(self):
        schema = {
            "type": "object",
            "properties": {"field": {"type": "integer"}},
            "required": ["field"],
        }

        result = _validate_schema('{"field": "ok"}', schema)

        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["path"] == "/field"
        assert result["errors"][0]["constraint"] == "type"
        assert result["errors"][0]["message"]

    def test_schema_violation_required(self):
        schema = {"type": "object", "required": ["field"]}

        result = _validate_schema("{}", schema)

        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["path"] == ""
        assert result["errors"][0]["constraint"] == "required"

    def test_multiple_violations(self):
        schema = {
            "type": "object",
            "properties": {
                "field": {"type": "integer"},
                "other": {"type": "string"},
            },
            "required": ["field", "other"],
        }

        result = _validate_schema('{"field": "bad"}', schema)

        assert result["valid"] is False
        assert len(result["errors"]) == 2
        assert {error["constraint"] for error in result["errors"]} == {
            "required",
            "type",
        }

    def test_parse_failure(self):
        schema = {"type": "object"}

        result = _validate_schema("{", schema)

        assert result["valid"] is False
        assert result["errors"] == [
            {
                "path": "",
                "constraint": "json_parse",
                "message": result["errors"][0]["message"],
            }
        ]

    def test_json_pointer_escape(self):
        schema = {
            "type": "object",
            "properties": {
                "a/b": {
                    "type": "object",
                    "properties": {"c~d": {"type": "integer"}},
                }
            },
        }

        result = _validate_schema('{"a/b": {"c~d": "bad"}}', schema)

        assert result["valid"] is False
        assert result["errors"][0]["path"] == "/a~1b/c~0d"

    def test_warning_logged_for_violations(self, caplog):
        schema = {
            "type": "object",
            "properties": {"field": {"type": "integer"}},
        }

        with caplog.at_level(logging.WARNING):
            _validate_schema('{"field": "bad"}', schema)

        assert any(
            record.levelno == logging.WARNING
            and "schema_validation:" in record.getMessage()
            for record in caplog.records
        )

    def test_invalid_schema_does_not_raise(self):
        result = _validate_schema('{"field": "ok"}', {"type": "not-a-real-type"})

        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["constraint"] == "schema_validation"


class TestGenerateJsonSchemaPlumbing:
    def test_finish_reason_predicate_json_rejects_any_non_stop(self):
        error = finish_reason_error(
            {"text": "{}", "finish_reason": "content_filter"},
            json_output=True,
        )

        assert isinstance(error, IncompleteJSONError)

    def test_finish_reason_predicate_plain_text_rejects_length(self):
        error = finish_reason_error(
            {"text": "partial", "finish_reason": "max_tokens"},
            json_output=False,
        )

        assert isinstance(error, IncompleteTextError)
        assert error.reason_code == "incomplete_text_length"

    def test_finish_reason_predicate_plain_text_rejects_non_length(self):
        error = finish_reason_error(
            {"text": "", "finish_reason": "content_filter"},
            json_output=False,
        )

        assert isinstance(error, ProviderResponseInvalidError)
        assert error.reason_code == "provider_response_invalid"

    def test_validate_json_response_plain_text_leniency_is_unchanged(self):
        models_module._validate_json_response(
            {"text": "partial", "finish_reason": "max_tokens"},
            json_output=False,
        )

    def test_generate_forces_json_output_with_schema(self):
        schema = {"type": "object"}
        provider_module = SimpleNamespace(
            run_generate=MagicMock(return_value={"text": "{}", "finish_reason": "stop"})
        )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
        ):
            result = generate("hello", "test.context", json_schema=schema)

        assert result == "{}"
        call_kwargs = provider_module.run_generate.call_args.kwargs
        assert call_kwargs["json_output"] is True
        assert call_kwargs["json_schema"] == schema

    def test_agenerate_forces_json_output_with_schema(self):
        schema = {"type": "object"}
        provider_module = SimpleNamespace(
            run_agenerate=AsyncMock(
                return_value={"text": "{}", "finish_reason": "stop"}
            )
        )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
        ):
            result = asyncio.run(agenerate("hello", "test.context", json_schema=schema))

        assert result == "{}"
        call_kwargs = provider_module.run_agenerate.call_args.kwargs
        assert call_kwargs["json_output"] is True
        assert call_kwargs["json_schema"] == schema

    def test_generate_with_result_adds_schema_validation(self):
        provider_module = SimpleNamespace(
            run_generate=MagicMock(return_value={"text": "{}", "finish_reason": "stop"})
        )
        validation = {"valid": True, "errors": []}

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
            patch("solstone.think.models._validate_schema", return_value=validation),
        ):
            result = generate_with_result(
                "hello",
                "test.context",
                json_schema={"type": "object"},
            )

        assert result["schema_validation"] == validation

    def test_agenerate_with_result_adds_schema_validation(self):
        provider_module = SimpleNamespace(
            run_agenerate=AsyncMock(
                return_value={"text": "{}", "finish_reason": "stop"}
            )
        )
        validation = {"valid": True, "errors": []}

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
            patch("solstone.think.models._validate_schema", return_value=validation),
        ):
            result = asyncio.run(
                agenerate_with_result(
                    "hello",
                    "test.context",
                    json_schema={"type": "object"},
                )
            )

        assert result["schema_validation"] == validation

    def test_generate_with_result_returns_failed_schema_validation_without_raising(
        self,
    ):
        provider_module = SimpleNamespace(
            run_generate=MagicMock(
                return_value={"text": '{"field": "bad"}', "finish_reason": "stop"}
            )
        )
        validation = {
            "valid": False,
            "errors": [{"path": "/field", "constraint": "type", "message": "bad"}],
        }

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
            patch("solstone.think.models._validate_schema", return_value=validation),
        ):
            result = generate_with_result(
                "hello",
                "test.context",
                json_schema={"type": "object"},
            )

        assert result["schema_validation"] == validation

    def test_generate_with_result_omits_schema_validation_without_schema(self):
        provider_module = SimpleNamespace(
            run_generate=MagicMock(return_value={"text": "{}", "finish_reason": "stop"})
        )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
            patch("solstone.think.models._validate_schema") as mock_validate_schema,
        ):
            result = generate_with_result("hello", "test.context")

        assert "schema_validation" not in result
        mock_validate_schema.assert_not_called()

    def test_generate_and_agenerate_do_not_surface_schema_validation(self):
        sync_provider = SimpleNamespace(
            run_generate=MagicMock(return_value={"text": "{}", "finish_reason": "stop"})
        )
        async_provider = SimpleNamespace(
            run_agenerate=AsyncMock(
                return_value={"text": "{}", "finish_reason": "stop"}
            )
        )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=sync_provider,
            ),
            patch(
                "solstone.think.models._validate_schema",
                return_value={"valid": True, "errors": []},
            ) as mock_validate_schema,
        ):
            sync_result = generate(
                "hello", "test.context", json_schema={"type": "object"}
            )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=async_provider,
            ),
            patch(
                "solstone.think.models._validate_schema",
                return_value={"valid": True, "errors": []},
            ) as mock_async_validate,
        ):
            async_result = asyncio.run(
                agenerate("hello", "test.context", json_schema={"type": "object"})
            )

        assert sync_result == "{}"
        assert async_result == "{}"
        mock_validate_schema.assert_called_once()
        mock_async_validate.assert_called_once()

    def test_generate_raises_schema_validation_error_with_structured_fields(self):
        long_text = '{"field": "' + ("x" * 300) + '"}'
        provider_module = SimpleNamespace(
            run_generate=MagicMock(
                return_value={"text": long_text, "finish_reason": "stop"}
            )
        )
        schema = {
            "type": "object",
            "properties": {"field": {"type": "integer"}},
        }

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
        ):
            with pytest.raises(SchemaValidationError) as exc_info:
                generate("hello", "test.context", json_schema=schema)

        exc = exc_info.value
        assert exc.text == long_text
        assert exc.errors[0]["constraint"] == "type"
        assert len(exc.preview) == 200
        assert exc.preview.endswith("...")
        assert "x" * 250 not in str(exc)

    def test_agenerate_raises_schema_validation_error(self):
        provider_module = SimpleNamespace(
            run_agenerate=AsyncMock(
                return_value={"text": '{"field": "bad"}', "finish_reason": "stop"}
            )
        )
        schema = {
            "type": "object",
            "properties": {"field": {"type": "integer"}},
        }

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
        ):
            with pytest.raises(SchemaValidationError) as exc_info:
                asyncio.run(agenerate("hello", "test.context", json_schema=schema))

        assert exc_info.value.text == '{"field": "bad"}'
        assert exc_info.value.errors[0]["constraint"] == "type"

    def test_generate_empty_schema_response_raises_schema_validation_error(self):
        provider_module = SimpleNamespace(
            run_generate=MagicMock(return_value={"text": "", "finish_reason": "stop"})
        )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
        ):
            with pytest.raises(SchemaValidationError) as exc_info:
                generate("hello", "test.context", json_schema={"type": "object"})

        assert exc_info.value.text == ""
        assert exc_info.value.preview == ""
        assert exc_info.value.errors[0]["constraint"] == "json_parse"

    def test_truncation_raises_before_schema_validation(self):
        provider_module = SimpleNamespace(
            run_generate=MagicMock(return_value={"text": "{}", "finish_reason": "stop"})
        )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
            patch(
                "solstone.think.models._validate_json_response",
                side_effect=IncompleteJSONError("max_tokens", "{}"),
            ),
            patch("solstone.think.models._validate_schema") as mock_validate_schema,
        ):
            with pytest.raises(IncompleteJSONError):
                generate_with_result(
                    "hello", "test.context", json_schema={"type": "object"}
                )

        mock_validate_schema.assert_not_called()


class TestDefaultProviderTimeout:
    def test_default_timeout_forwards_to_all_generate_entrypoints(self):
        provider_module = SimpleNamespace(
            run_generate=MagicMock(
                return_value={"text": "ok", "finish_reason": "stop"}
            ),
            run_agenerate=AsyncMock(
                return_value={"text": "ok", "finish_reason": "stop"}
            ),
        )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
        ):
            generate("hello", "test.context")
            generate_with_result("hello", "test.context")
            asyncio.run(agenerate("hello", "test.context"))

        assert (
            provider_module.run_generate.call_args_list[0].kwargs["timeout_s"]
            == DEFAULT_PROVIDER_TIMEOUT_S
        )
        assert (
            provider_module.run_generate.call_args_list[1].kwargs["timeout_s"]
            == DEFAULT_PROVIDER_TIMEOUT_S
        )
        assert (
            provider_module.run_agenerate.call_args.kwargs["timeout_s"]
            == DEFAULT_PROVIDER_TIMEOUT_S
        )

    def test_explicit_timeout_wins_for_all_generate_entrypoints(self):
        provider_module = SimpleNamespace(
            run_generate=MagicMock(
                return_value={"text": "ok", "finish_reason": "stop"}
            ),
            run_agenerate=AsyncMock(
                return_value={"text": "ok", "finish_reason": "stop"}
            ),
        )

        with (
            patch(
                "solstone.think.models.resolve_provider", return_value=("fake", "model")
            ),
            patch(
                "solstone.think.providers.get_provider_module",
                return_value=provider_module,
            ),
        ):
            generate("hello", "test.context", timeout_s=5)
            generate_with_result("hello", "test.context", timeout_s=5)
            asyncio.run(agenerate("hello", "test.context", timeout_s=5))

        assert provider_module.run_generate.call_args_list[0].kwargs["timeout_s"] == 5
        assert provider_module.run_generate.call_args_list[1].kwargs["timeout_s"] == 5
        assert provider_module.run_agenerate.call_args.kwargs["timeout_s"] == 5


def test_request_health_recheck_emits_callosum_request():
    from solstone.think.providers.state import request_recheck

    with patch(
        "solstone.think.providers.state.callosum_send", return_value=True
    ) as send:
        request_recheck()

    send.assert_called_once_with(
        "supervisor",
        "request",
        cmd=["journal", "providers", "check", "--targeted"],
    )


def test_request_health_recheck_does_not_raise_on_send_failure(caplog):
    from solstone.think.providers.state import request_recheck

    with (
        patch(
            "solstone.think.providers.state.callosum_send", return_value=False
        ) as send,
        caplog.at_level(logging.WARNING),
    ):
        request_recheck()

    send.assert_called_once()
    assert "request_health_recheck: callosum_send returned false" in caplog.text
