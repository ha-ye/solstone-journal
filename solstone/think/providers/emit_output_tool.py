# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys
from typing import Any

TOOL_DESCRIPTION = """Emits the final artifact body for a talent run that writes an output file.

Use this tool when:
- You have finished gathering and synthesizing the requested artifact
- The artifact should be saved exactly as markdown or text output

The content argument is the artifact body itself.
- Include the complete markdown or text that should be saved
- Do NOT summarize what you did
- Do NOT describe the artifact instead of providing it
- Do NOT include phrases like "the markdown is below", "here is the briefing", or "I created the file"
- Do NOT wrap the artifact in commentary before or after the content
"""


# Lazy cache for the openhands-derived EmitOutput* classes. The classes have to
# live at module level (i.e. without `<locals>` in their __qualname__ and
# discoverable as attributes on this module) because openhands-sdk persists tool
# events to disk and re-validates them via `Event.model_validate_json`, which
# rejects subclasses whose qualname contains "<locals>". OpenHands is installed
# on demand, so define the classes lazily and promote them into this module.
_EMIT_OUTPUT_TYPES: dict[str, Any] = {}


def _ensure_emit_output_types() -> dict[str, Any]:
    if _EMIT_OUTPUT_TYPES:
        return _EMIT_OUTPUT_TYPES

    from openhands.sdk.tool import ToolAnnotations, ToolDefinition, ToolExecutor
    from openhands.sdk.tool.schema import Action, Observation
    from pydantic import Field

    class EmitOutputAction(Action):
        content: str = Field(description="Complete final artifact body to save.")

    class EmitOutputObservation(Observation):
        pass

    class EmitOutputExecutor(ToolExecutor):
        def __call__(
            self,
            action: Any,
            conversation: Any = None,
        ) -> Any:
            del conversation
            return EmitOutputObservation.from_text(text=action.content)

    class EmitOutputTool(ToolDefinition[EmitOutputAction, EmitOutputObservation]):
        name = "emit_output"

        @classmethod
        def create(cls, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

    # Promote the closure-defined classes onto this module so they look
    # module-level to openhands-sdk's serialization machinery. Without
    # this, `__qualname__` carries `<locals>` and re-deserializing tool
    # events fails inside stuck_detector with
    # "Local classes not supported".
    module = sys.modules[__name__]
    for cls in (
        EmitOutputAction,
        EmitOutputObservation,
        EmitOutputExecutor,
        EmitOutputTool,
    ):
        cls.__module__ = __name__
        cls.__qualname__ = cls.__name__
        setattr(module, cls.__name__, cls)

    _EMIT_OUTPUT_TYPES.update(
        EmitOutputAction=EmitOutputAction,
        EmitOutputObservation=EmitOutputObservation,
        EmitOutputExecutor=EmitOutputExecutor,
        EmitOutputTool=EmitOutputTool,
        ToolAnnotations=ToolAnnotations,
    )
    return _EMIT_OUTPUT_TYPES


def build_emit_output_tools() -> list[Any]:
    types = _ensure_emit_output_types()
    emit_output_action = types["EmitOutputAction"]
    emit_output_observation = types["EmitOutputObservation"]
    emit_output_executor_cls = types["EmitOutputExecutor"]
    emit_output_tool_cls = types["EmitOutputTool"]
    tool_annotations = types["ToolAnnotations"]

    tool = emit_output_tool_cls(
        description=TOOL_DESCRIPTION,
        action_type=emit_output_action,
        observation_type=emit_output_observation,
        executor=emit_output_executor_cls(),
        annotations=tool_annotations(
            title="emit_output",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    return [tool]
