# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
import shlex
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from solstone.think.cogitate_contract import (
    COGITATE_ACCESS_TIERS,
    capabilities_for_access_tier,
)

MAX_TURNS = 60
DEFAULT_READ_CALL_BUDGET = 200

_SOL_INVOCATION_RE = re.compile(r"(^sol\s|\bsol call\b)")
_JOURNAL_COMMANDS = {"identity", "routines", "health", "talent"}
_SHELL_CONTROL_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "<<", "$(", "`"}
_WRITE_TOOLS = {"write_file", "replace"}
_READ_TOOLS = {"read_file", "glob", "list_directory", "grep_search"}
_SUBMIT_TIERS = tuple(
    tier for tier in COGITATE_ACCESS_TIERS if capabilities_for_access_tier(tier).submit
)
_SUPPORT_SEND_VERBS = {"create", "reply", "attach", "feedback"}


def _is_approved_journal_invocation(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    if len(tokens) < 2 or tokens[0] != "journal" or tokens[1] not in _JOURNAL_COMMANDS:
        return False

    for token in tokens:
        if token in _SHELL_CONTROL_TOKENS or "$(" in token or "`" in token:
            return False

    return True


class MaxTurnsExhausted(RuntimeError):
    """Raised when the SDK tool loop exceeds its turn ceiling."""


class CogitatePolicy:
    """In-process policy gate for cogitate tool calls."""

    def __init__(
        self,
        *,
        allowed_roots: list[Path],
        access_tier: str,
        outbound_approval: str | None = None,
    ) -> None:
        self.allowed_roots = [
            Path(root).expanduser().resolve() for root in allowed_roots
        ]
        self.access_tier = access_tier
        self.submit_allowed = capabilities_for_access_tier(access_tier).submit
        self.outbound_approval = outbound_approval

    def check(self, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        if tool in _WRITE_TOOLS:
            return False, f"policy_deny: {tool} not allowed for read-only talents"

        if tool == "run_shell_command":
            command = str(args.get("command", ""))
            is_sol_invocation = _SOL_INVOCATION_RE.search(command) is not None
            is_approved_journal_invocation = _is_approved_journal_invocation(command)
            if not (is_sol_invocation or is_approved_journal_invocation):
                return (
                    False,
                    "policy_deny: run_shell_command restricted to sol"
                    " or approved journal invocations",
                )
            if is_sol_invocation:
                send_verb = _support_send_verb(command)
                if not self.submit_allowed:
                    if send_verb:
                        required = " or ".join(_SUBMIT_TIERS)
                        return (
                            False,
                            f"policy_deny: 'sol call support {send_verb}' requires "
                            f"access_tier {required!r}; "
                            f"this run is {self.access_tier!r}",
                        )
                    if _support_command_has_shell_control(command):
                        return (
                            False,
                            "policy_deny: support commands may not be chained or "
                            "wrapped with shell-control operators "
                            "(run them one at a time)",
                        )
                elif send_verb and not self.outbound_approval:
                    return (
                        False,
                        f"policy_deny: 'sol call support {send_verb}' requires a "
                        "per-send owner approval; this run was not launched with one",
                    )
            return True, "ok"

        if tool in _READ_TOOLS:
            return True, "ok"

        return True, "ok"


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _support_send_verb(command: str) -> str | None:
    tokens = _command_tokens(command)
    for index in range(len(tokens) - 3):
        if tokens[index : index + 3] != ["sol", "call", "support"]:
            continue
        verb = tokens[index + 3]
        if verb in _SUPPORT_SEND_VERBS:
            return verb
    return None


def _support_command_has_shell_control(command: str) -> bool:
    lower_command = command.lower()
    if "support" not in lower_command:
        return False
    return any(token in command for token in _SHELL_CONTROL_TOKENS)


def _normalize_day(day: date | str) -> str:
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    if day:
        return str(day)
    return datetime.now().strftime("%Y%m%d")


def _day_value(day: str) -> date:
    return datetime.strptime(day, "%Y%m%d").date()


def _expand_day_placeholders(value: str, day: str) -> str:
    base_day = _day_value(day)

    def replace(match: re.Match[str]) -> str:
        offset = int(match.group("offset") or 0)
        return (base_day - timedelta(days=offset)).strftime("%Y%m%d")

    return re.sub(r"<day(?:-(?P<offset>\d+))?>", replace, value)


def resolve_read_scope(
    talent_config: dict[str, Any],
    day: date | str,
    span: int = 0,
) -> list[str]:
    day_str = _normalize_day(day)
    configured_scope = talent_config.get("read_scope")
    if configured_scope:
        return [
            _expand_day_placeholders(str(scope), day_str) for scope in configured_scope
        ]

    effective_span = int(talent_config.get("read_scope_span", span or 0) or 0)
    if effective_span <= 0:
        return [f"chronicle/{day_str}"]

    base_day = _day_value(day_str)
    return [
        f"chronicle/{(base_day - timedelta(days=offset)).strftime('%Y%m%d')}"
        for offset in range(effective_span, -1, -1)
    ]
