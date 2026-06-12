# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Model-free harness for real cogitate finalization intents.

`emit_final` registers the explicit final tool for scheduled/output runs,
`FinishTool` relies on the provider's built-in finish tool, and `quiet` is a
prompt-prose intent represented by a side-effect talent whose runtime
registration outcome is still `emit_final` -- there is no third
tool-registration.
"""

import pytest

from solstone.think.cogitate_contract import (
    COGITATE_READ_TOOL_NAMES,
    COGITATE_RUNTIME_PREAMBLE,
    capabilities_for_access_tier,
    expects_emit_final,
)
from solstone.think.providers.cli import assemble_prompt
from solstone.think.talent import get_talent, get_talent_configs

# Scheduled-cadence vocabulary used ONLY for the structural class guards below.
# The finalization decision itself is ALWAYS delegated to expects_emit_final() --
# never re-implement the bool(output_path) or schedule-in-set selector here.
EMIT_FINAL_SCHEDULES = {"daily", "weekly", "activity"}


@pytest.mark.parametrize(
    ("name", "guard", "expected_finalizer"),
    [
        (
            "weekly_reflection",
            lambda c: c.get("schedule") in EMIT_FINAL_SCHEDULES,
            "emit_final",
        ),
        (
            "exec",
            lambda c: (
                c.get("schedule") not in EMIT_FINAL_SCHEDULES
                and not c.get("output_path")
            ),
            "FinishTool",
        ),
    ],
    ids=["emit_final-scheduled", "finishtool-unscheduled"],
)
def test_cogitate_finalization_class_assembles_on_contract(
    name,
    guard,
    expected_finalizer,
):
    cogitate_names = set(get_talent_configs(type="cogitate"))
    assert name in cogitate_names, (
        f"{name} is no longer discovered as a cogitate talent by frontmatter type"
    )
    config = get_talent(name)
    assert guard(config), f"{name} no longer satisfies its finalization-class property"

    caps = capabilities_for_access_tier(config.get("access_tier", "normal"))
    tool_surface = [
        *(["sol"] if caps.sol else []),
        *(COGITATE_READ_TOOL_NAMES if caps.reads else ()),
    ]
    assert tool_surface == ["sol", *COGITATE_READ_TOOL_NAMES]

    body, system = assemble_prompt(config, sol_tool_name="sol")
    assert isinstance(body, str)
    assert system is not None
    assert system.startswith(COGITATE_RUNTIME_PREAMBLE)

    helper_finalizer = "emit_final" if expects_emit_final(config) else "FinishTool"
    assert helper_finalizer == expected_finalizer
