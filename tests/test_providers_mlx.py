# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image


def _provider(monkeypatch):
    import solstone.think.providers.mlx as provider

    monkeypatch.setattr(provider, "_module_level_cache", None)
    return provider


def _install_mlx_stub(monkeypatch, *, load_exc=None, text="ok"):
    mlx_module = types.ModuleType("mlx_vlm")
    mlx_module.__path__ = []
    model = SimpleNamespace(config=SimpleNamespace(model_type="qwen3_5"))
    processor = SimpleNamespace(tokenizer=object())
    load_mock = MagicMock(side_effect=load_exc)
    if load_exc is None:
        load_mock = MagicMock(return_value=(model, processor))
    template_mock = MagicMock(return_value="templated")
    result = SimpleNamespace(
        text=text,
        prompt_tokens=7,
        generation_tokens=3,
        total_tokens=10,
    )
    generate_mock = MagicMock(return_value=result)
    mlx_module.load = load_mock
    mlx_module.apply_chat_template = template_mock
    mlx_module.generate = generate_mock

    structured_module = types.ModuleType("mlx_vlm.structured")
    logits_processor = object()
    build_schema_mock = MagicMock(return_value=logits_processor)
    structured_module.build_json_schema_logits_processor = build_schema_mock

    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_module)
    monkeypatch.setitem(sys.modules, "mlx_vlm.structured", structured_module)
    return SimpleNamespace(
        module=mlx_module,
        model=model,
        processor=processor,
        load=load_mock,
        template=template_mock,
        generate=generate_mock,
        result=result,
        structured=structured_module,
        build_schema=build_schema_mock,
        logits_processor=logits_processor,
    )


def test_registration():
    from solstone.think.providers import (
        PROVIDER_METADATA,
        PROVIDER_REGISTRY,
        get_provider_module,
    )

    assert "mlx" in PROVIDER_REGISTRY
    assert PROVIDER_METADATA["mlx"] == {
        "label": "MLX (Local, Apple Silicon)",
        "env_key": "",
    }
    assert get_provider_module("mlx").__name__ == "solstone.think.providers.mlx"


def test_module_import_is_mlx_vlm_free(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_vlm", None)
    import solstone.think.providers.mlx as provider

    provider = importlib.reload(provider)

    assert not hasattr(provider, "mlx_vlm")


@pytest.mark.parametrize(
    ("system", "machine", "ram_gb", "mlx_present", "expected"),
    [
        ("Linux", "arm64", 32, True, (False, "not running on macOS")),
        ("Darwin", "x86_64", 32, True, (False, "not running on Apple Silicon")),
        (
            "Darwin",
            "arm64",
            8,
            True,
            (False, "insufficient RAM (need 16 GB, have 8 GB)"),
        ),
        ("Darwin", "arm64", 32, False, (False, "mlx-vlm package not installed")),
        ("Darwin", "arm64", 32, True, (True, "")),
    ],
)
def test_is_mlx_available_parameterized(
    monkeypatch, system, machine, ram_gb, mlx_present, expected
):
    provider = _provider(monkeypatch)
    monkeypatch.setattr(provider.platform, "system", lambda: system)
    monkeypatch.setattr(provider.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        provider.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=ram_gb * 1024**3),
    )
    if mlx_present:
        monkeypatch.setitem(sys.modules, "mlx_vlm", types.ModuleType("mlx_vlm"))
    else:
        monkeypatch.setitem(sys.modules, "mlx_vlm", None)

    assert provider.is_mlx_available() == expected


@pytest.mark.parametrize("image_count", [1, 2])
def test_image_actually_reaches_mlx_vlm(monkeypatch, image_count):
    provider = _provider(monkeypatch)
    stub = _install_mlx_stub(monkeypatch)
    images = [Image.new("RGB", (8, 8)) for _ in range(image_count)]

    provider.run_generate(contents=["hi", *images])

    assert stub.template.call_args.kwargs["num_images"] == image_count
    passed_images = stub.generate.call_args.kwargs["image"]
    assert [id(image) for image in passed_images] == [id(image) for image in images]


def test_schema_mode_passes_logits_processor_and_raw_text(monkeypatch):
    provider = _provider(monkeypatch)
    stub = _install_mlx_stub(monkeypatch, text='{"ok": true}')
    schema = {"type": "object"}

    result = provider.run_generate("hi", json_schema=schema)

    stub.build_schema.assert_called_once_with(stub.processor.tokenizer, schema)
    assert stub.generate.call_args.kwargs["logits_processors"] == [
        stub.logits_processor
    ]
    assert result["text"] == '{"ok": true}'


def test_schema_mode_returns_invalid_json_verbatim(monkeypatch):
    provider = _provider(monkeypatch)
    _install_mlx_stub(monkeypatch, text="{")

    result = provider.run_generate("hi", json_schema={"type": "object"})

    assert result["text"] == "{"


def test_no_schema_path_does_not_build_logits_processor(monkeypatch):
    provider = _provider(monkeypatch)
    stub = _install_mlx_stub(monkeypatch)

    provider.run_generate("hi", json_schema=None)

    stub.build_schema.assert_not_called()
    assert "logits_processors" not in stub.generate.call_args.kwargs


def test_text_only_uses_no_images(monkeypatch):
    provider = _provider(monkeypatch)
    stub = _install_mlx_stub(monkeypatch, text="plain")

    result = provider.run_generate(contents=["just text"])

    assert stub.template.call_args.kwargs["num_images"] == 0
    assert stub.generate.call_args.kwargs["image"] is None
    assert result["text"] == "plain"


def test_run_cogitate_raises_unsupported():
    from solstone.think.providers import mlx

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(mlx.run_cogitate(config={}))

    assert "vision" in str(exc_info.value)
    assert "v1" in str(exc_info.value)


def test_model_snapshot_missing_error_translated(monkeypatch):
    from huggingface_hub.errors import LocalEntryNotFoundError

    provider = _provider(monkeypatch)
    _install_mlx_stub(monkeypatch, load_exc=LocalEntryNotFoundError("missing"))

    with pytest.raises(provider.ModelSnapshotMissingError) as exc_info:
        provider.run_generate("hi")

    assert "model snapshot not present" in str(exc_info.value)


def test_other_load_errors_pass_through(monkeypatch):
    provider = _provider(monkeypatch)
    _install_mlx_stub(monkeypatch, load_exc=RuntimeError("disk full"))

    with pytest.raises(RuntimeError, match="disk full"):
        provider.run_generate("hi")


def test_cache_reuse(monkeypatch):
    provider = _provider(monkeypatch)
    stub = _install_mlx_stub(monkeypatch)

    provider.run_generate("one")
    provider.run_generate("two")

    stub.load.assert_called_once()
