# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import base64
import importlib
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image


def _openai_provider():
    return importlib.reload(importlib.import_module("solstone.think.providers.openai"))


def _make_openai_response(text="Hello"):
    response = MagicMock()
    response.output_text = text
    response.status = "completed"
    response.incomplete_details = None
    response.usage = None
    response.output = []
    return response


def _png_bytes(size: tuple[int, int] = (4, 3)) -> bytes:
    image = Image.new("RGB", size, color="red")
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _decode_openai_image_part(part):
    prefix, b64 = part["image_url"].split(",", 1)
    assert prefix.startswith("data:")
    assert prefix.endswith(";base64")
    return prefix[5:-7], Image.open(io.BytesIO(base64.b64decode(b64)))


class TestParseModelEffort:
    def test_no_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2") == ("gpt-5.2", None)

    def test_high_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-high") == ("gpt-5.2", "high")

    def test_low_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-low") == ("gpt-5.2", "low")

    def test_medium_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-medium") == ("gpt-5.2", "medium")

    def test_none_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-none") == ("gpt-5.2", "none")

    def test_xhigh_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-xhigh") == ("gpt-5.2", "xhigh")

    def test_unknown_suffix_not_stripped(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-turbo") == (
            "gpt-5.2-turbo",
            None,
        )

    def test_non_gpt_model_passthrough(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("claude-sonnet-4-5") == (
            "claude-sonnet-4-5",
            None,
        )


class TestBuildInput:
    def test_string_input(self):
        provider = _openai_provider()
        assert provider._build_input("hello") == ("hello", None)

    def test_string_with_system(self):
        provider = _openai_provider()
        assert provider._build_input("hello", "sys") == ("hello", "sys")

    def test_list_of_parts(self):
        provider = _openai_provider()
        assert provider._build_input(["part1", "part2"]) == ("part1\npart2", None)

    def test_message_list(self):
        provider = _openai_provider()
        message = [{"role": "user", "content": "hi"}]
        assert provider._build_input(message) == (message, None)

    def test_non_string(self):
        provider = _openai_provider()
        assert provider._build_input(42) == ("42", None)


class TestExtractUsage:
    def test_extract_usage_with_details(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.total_tokens = 150
        mock_response.usage.input_tokens_details.cached_tokens = 20
        mock_response.usage.output_tokens_details.reasoning_tokens = 10

        assert provider._extract_usage(mock_response) == {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cached_tokens": 20,
            "reasoning_tokens": 10,
        }

    def test_extract_usage_missing(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.usage = None
        assert provider._extract_usage(mock_response) is None

    def test_extract_usage_without_details(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.total_tokens = 150
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None

        assert provider._extract_usage(mock_response) == {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }


class TestNormalizeFinishReason:
    def test_completed(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.status = "completed"
        assert provider._normalize_finish_reason(mock_response) == "stop"

    def test_incomplete_max_tokens(self):
        provider = _openai_provider()
        incomplete_details = MagicMock()
        incomplete_details.reason = "max_output_tokens"
        mock_response = MagicMock()
        mock_response.status = "incomplete"
        mock_response.incomplete_details = incomplete_details
        assert provider._normalize_finish_reason(mock_response) == "max_tokens"

    def test_incomplete_content_filter(self):
        provider = _openai_provider()
        incomplete_details = MagicMock()
        incomplete_details.reason = "content_filter"
        mock_response = MagicMock()
        mock_response.status = "incomplete"
        mock_response.incomplete_details = incomplete_details
        assert provider._normalize_finish_reason(mock_response) == "content_filter"

    def test_incomplete_without_details(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.status = "incomplete"
        mock_response.incomplete_details = None
        assert provider._normalize_finish_reason(mock_response) == "max_tokens"

    def test_failed(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.status = "failed"
        assert provider._normalize_finish_reason(mock_response) == "error"


class TestExtractThinking:
    def test_reasoning_summary_extracted(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        reasoning_item = MagicMock()
        reasoning_item.type = "reasoning"
        summary = MagicMock()
        summary.text = "Let me think..."
        reasoning_item.summary = [summary]
        mock_response.output = [reasoning_item]

        assert provider._extract_thinking(mock_response) == [
            {"summary": "Let me think..."},
        ]

    def test_no_reasoning_items(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.output = [MagicMock(type="message")]
        assert provider._extract_thinking(mock_response) is None

    def test_empty_output(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.output = []
        assert provider._extract_thinking(mock_response) is None


class TestRunGenerate:
    def test_basic_generate(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello world"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            result = provider.run_generate("hello", model="gpt-5.2")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["model"] == "gpt-5.2"
        assert called_kwargs["input"] == "hello"
        assert called_kwargs["max_output_tokens"] == 16384
        assert "instructions" not in called_kwargs
        assert result["text"] == "Hello world"
        assert result["finish_reason"] == "stop"
        assert result["thinking"] is None
        assert result["usage"] == {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }

    def test_run_generate_records_resolved_model_version(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = _make_openai_response("Hello world")
        mock_response.model = "gpt-5-2025-08-07"
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            result = provider.run_generate("hello", model="gpt-5")

        assert result["model"] == "gpt-5-2025-08-07"
        assert result["usage"]["model_version"] == "gpt-5-2025-08-07"

    def test_run_generate_model_version_falls_back_to_requested(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = _make_openai_response("Hello world")
        mock_response.model = MagicMock()
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            result = provider.run_generate("hello", model="gpt-5")

        assert result["model"] == "gpt-5"
        assert "model_version" not in result["usage"]

    def test_structured_messages_passthrough(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello world"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(messages, system_instruction="Be helpful")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["input"] == messages
        assert called_kwargs["instructions"] == "Be helpful"

    def test_image_parts_build_structured_input(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock(return_value=_make_openai_response())
        image = Image.new("RGB", (5, 4), color="blue")

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(["before", image, "after"], model="gpt-5.2")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        message = called_kwargs["input"][0]
        assert message["role"] == "user"
        parts = message["content"]
        assert [part["type"] for part in parts] == [
            "input_text",
            "input_image",
            "input_text",
        ]
        assert parts[0]["text"] == "before"
        assert parts[2]["text"] == "after"
        assert parts[1]["detail"] == "auto"
        media_type, decoded = _decode_openai_image_part(parts[1])
        assert media_type == "image/png"
        assert decoded.size == image.size
        assert decoded.format == "PNG"

    def test_png_bytes_part_builds_data_url(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock(return_value=_make_openai_response())
        data = _png_bytes((6, 3))

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(["prompt", data], model="gpt-5.2")

        parts = mock_client.responses.create.call_args.kwargs["input"][0]["content"]
        media_type, decoded = _decode_openai_image_part(parts[1])
        assert media_type == "image/png"
        assert decoded.size == (6, 3)
        assert decoded.format == "PNG"

    def test_bad_bytes_raise_before_create(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()

        with (
            patch(
                "solstone.think.providers.openai._get_openai_client",
                return_value=mock_client,
            ),
            pytest.raises(ValueError) as exc_info,
        ):
            provider.run_generate(["prompt", b"not-an-image"], model="gpt-5.2")

        assert "bytes" in str(exc_info.value)
        assert "not-an-image" in str(exc_info.value)
        assert mock_client.responses.create.call_count == 0

    def test_cmyk_image_raises_before_create(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        image = Image.new("CMYK", (2, 2))

        with (
            patch(
                "solstone.think.providers.openai._get_openai_client",
                return_value=mock_client,
            ),
            pytest.raises(ValueError) as exc_info,
        ):
            provider.run_generate(["prompt", image], model="gpt-5.2")

        assert "Image" in str(exc_info.value)
        assert "CMYK" in str(exc_info.value)
        assert mock_client.responses.create.call_count == 0

    def test_with_effort_suffix(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", model="gpt-5.2-high")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["model"] == "gpt-5.2"
        assert called_kwargs["reasoning"] == {"effort": "high"}

    def test_with_json_output(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", model="gpt-5.2", json_output=True)

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {"format": {"type": "json_object"}}

    def test_no_schema_format_unchanged(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(
                "hello",
                model="gpt-5.2",
                json_output=True,
                json_schema=None,
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {"format": {"type": "json_object"}}

    def test_with_schema_format_shape(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response
        schema = {"type": "object"}

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", model="gpt-5.2", json_schema=schema)

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {
            "format": {
                "type": "json_schema",
                "name": "response",
                "schema": schema,
                "strict": True,
            }
        }

    def test_schema_title_becomes_name(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(
                "hello",
                model="gpt-5.2",
                json_schema={"title": "MyThing", "type": "object"},
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"]["format"]["name"] == "MyThing"

    def test_schema_bad_title_falls_back(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(
                "hello",
                model="gpt-5.2",
                json_schema={"title": "bad name with spaces", "type": "object"},
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"]["format"]["name"] == "response"

    def test_with_system_instruction(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", system_instruction="Be helpful")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["instructions"] == "Be helpful"

    def test_with_timeout(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", timeout_s=30.0)

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["timeout"] == 30.0


class TestRunAgenerate:
    def test_basic_agenerate(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello world"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            result = asyncio.run(provider.run_agenerate("hello", model="gpt-5.2"))

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["model"] == "gpt-5.2"
        assert called_kwargs["input"] == "hello"
        assert called_kwargs["max_output_tokens"] == 16384
        assert result["text"] == "Hello world"
        assert result["finish_reason"] == "stop"
        assert result["thinking"] is None

    def test_with_thinking(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        reasoning_item = MagicMock()
        reasoning_item.type = "reasoning"
        summary = MagicMock()
        summary.text = "Let me think..."
        reasoning_item.summary = [summary]
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = [reasoning_item]
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            result = asyncio.run(provider.run_agenerate("hello", model="gpt-5.2"))

        assert result["thinking"] == [{"summary": "Let me think..."}]

    def test_async_multi_image_parts_preserve_order(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock(return_value=_make_openai_response())
        first = Image.new("RGB", (3, 2), color="red")
        second = Image.new("RGB", (4, 5), color="green")

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate(
                    ["prompt", first, second],
                    model="gpt-5.2",
                )
            )

        parts = mock_client.responses.create.call_args.kwargs["input"][0]["content"]
        assert [part["type"] for part in parts] == [
            "input_text",
            "input_image",
            "input_image",
        ]
        assert parts[0]["text"] == "prompt"
        first_media_type, first_decoded = _decode_openai_image_part(parts[1])
        second_media_type, second_decoded = _decode_openai_image_part(parts[2])
        assert first_media_type == "image/png"
        assert second_media_type == "image/png"
        assert first_decoded.size == first.size
        assert second_decoded.size == second.size
        assert first_decoded.format == "PNG"
        assert second_decoded.format == "PNG"

    def test_no_schema_format_unchanged(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate(
                    "hello",
                    model="gpt-5.2",
                    json_output=True,
                    json_schema=None,
                )
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {"format": {"type": "json_object"}}

    def test_with_schema_format_shape(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response
        schema = {"type": "object"}

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate("hello", model="gpt-5.2", json_schema=schema)
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {
            "format": {
                "type": "json_schema",
                "name": "response",
                "schema": schema,
                "strict": True,
            }
        }

    def test_schema_title_becomes_name(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate(
                    "hello",
                    model="gpt-5.2",
                    json_schema={"title": "MyThing", "type": "object"},
                )
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"]["format"]["name"] == "MyThing"

    def test_schema_bad_title_falls_back(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate(
                    "hello",
                    model="gpt-5.2",
                    json_schema={"title": "bad name with spaces", "type": "object"},
                )
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"]["format"]["name"] == "response"
