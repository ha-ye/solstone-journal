#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Cogitate prompt command-span lint.

This is the prompt-side static mirror of ``solstone/think/cogitate_policy.py``.
It lints command-bearing code spans in every ``type == "cogitate"`` talent
prompt, applying the same command classification to prompt markdown that the
runtime policy applies to live tool calls.

The committed ``ALLOWLIST`` is keyed by ``(file, kind)`` with an allowed count.
This is a ``!=`` self-check: a live count above the allowlisted count fails as a
new/over violation, and a live count below the allowlisted count fails as a
stale entry. The gate iterates the union of discovered keys and allowlist keys
so stale entries for vanished files are still visited.

No live ``sol`` or ``journal`` command is invoked at lint time.

Known limitation: single-backtick inline spans are parsed; double-backtick spans
are not.

Exit codes:
  0 - clean
  1 - any over violation or stale allowlist entry
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

import frontmatter

import solstone.think.talent as talent

ROOT = Path(__file__).resolve().parent.parent

# Must equal solstone/think/cogitate_policy.py:_JOURNAL_COMMANDS
# (cogitate_policy.py:21). Duplicated here intentionally; no shared import.
ALLOWED_JOURNAL_COMMANDS = frozenset({"identity", "health", "talent"})

# Must equal solstone/think/cogitate_policy.py:_READ_TOOLS
# (cogitate_policy.py:24). Duplicated here intentionally; no shared import.
READ_TOOLS = frozenset({"read_file", "glob", "list_directory", "grep_search"})

# Claude Code tool names the cogitate runtime lacks. Case-sensitive: lowercase
# glob/grep_search are sanctioned read tools and must pass. Runtime tool names
# write_file/replace are intentionally absent; prompts may reference them in
# negative instructions.
CLI_AGENT_TOOLS = frozenset({"Read", "Edit", "Write", "Bash", "Glob", "Grep", "Agent"})

SHELL_READ_COMMANDS = frozenset({"cat", "ls", "head", "tail", "less", "more"})
SHELL_OPERATOR_CHARS = frozenset("();<>|&")
SHELL_WRAPPERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "dash",
        "env",
        "eval",
        "exec",
        "command",
        "xargs",
        "sudo",
        "python",
        "python3",
    }
)

_FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_EMBEDDED_SOL_JOURNAL_RE = re.compile(r"(?<![\w./-])(?:sol|journal)(?![\w/-])")
_PLACEHOLDER_RE = re.compile(r"<[+A-Za-z0-9_][+A-Za-z0-9_. -]*>")

# Curated, minimal: lowest-confidence class, NOT anchored in cogitate_policy.py.
# A false entry (flagging a flag the CLI accepts) is worse than omission.
# NOTE: 'facets --json' was a scope seed but is VALID
# (sol call facets list-candidates accepts --json, facets/call.py:40) -- omitted.
UNSUPPORTED_FLAGS: list[tuple[tuple[str, ...], str, str]] = []

ALLOWLIST: dict[tuple[str, str], int] = {}

JOURNAL_ALTERNATIVE = (
    "use `journal` with one of {identity, health, talent}, or use `sol`/`sol call`"
)
READ_ALTERNATIVE = (
    "use a bounded read tool: read_file, list_directory, glob, or grep_search"
)
PROMPT_CONTRACT = (
    "cogitate prompts must name only on-contract command forms: "
    "`sol`/`sol call`, approved `journal` subcommands, or bounded read tools"
)


def command_tokens(command: str) -> list[str]:
    """Return shell-like tokens, falling back to whitespace splitting."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _shell_syntax_violation(command: str) -> bool:
    """Return True when command uses shell syntax outside quoted data."""
    if "$(" in command or "`" in command:
        return True
    if "\n" in command or "\r" in command:
        return True
    index = 0
    length = len(command)
    quote: str | None = None
    while index < length:
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
        elif quote == '"':
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char == '"':
                quote = None
        else:
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char in ("'", '"'):
                quote = char
            elif char in SHELL_OPERATOR_CHARS:
                return True
        index += 1
    # Diverges from the runtime scanner: prompt lint receives per-line spans from
    # fenced examples, so a legitimate multi-line quoted value can leave the
    # first line with an open quote. That is documentation, not shell composition.
    return False


def _wrapper_embeds_sol_or_journal(command: str, tokens: list[str]) -> bool:
    if not tokens or tokens[0] not in SHELL_WRAPPERS:
        return False
    return _EMBEDDED_SOL_JOURNAL_RE.search(command) is not None


def _raw_substitution_or_multiline(command: str) -> bool:
    return "$(" in command or "`" in command or "\n" in command or "\r" in command


def _mask_placeholders(command: str) -> str:
    return _PLACEHOLDER_RE.sub("ARG", command)


def _shell_composition_finding(command: str, tokens: list[str]) -> bool:
    if _raw_substitution_or_multiline(command):
        return True
    if _wrapper_embeds_sol_or_journal(command, tokens):
        return True
    if tokens[0] in {"sol", "journal"} and _shell_syntax_violation(
        _mask_placeholders(command)
    ):
        return True
    return False


def extract_command_spans(body: str) -> list[tuple[int, str]]:
    """Return ``(lineno, span_text)`` for fenced lines and inline code spans."""
    spans: list[tuple[int, int, str]] = []

    for match in _FENCED_BLOCK_RE.finditer(body):
        block_start_line = body[: match.start(1)].count("\n") + 1
        for offset, line in enumerate(match.group(1).splitlines()):
            stripped = line.strip()
            if stripped:
                spans.append(
                    (block_start_line + offset, match.start(1) + offset, stripped)
                )

    def blank_fence(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    inline_body = _FENCED_BLOCK_RE.sub(blank_fence, body)
    for match in _INLINE_CODE_RE.finditer(inline_body):
        spans.append(
            (
                inline_body[: match.start()].count("\n") + 1,
                match.start(),
                match.group(1).strip(),
            )
        )

    return [(lineno, span) for lineno, _order, span in sorted(spans)]


def _is_contiguous_subsequence(tokens: list[str], sequence: tuple[str, ...]) -> bool:
    if len(sequence) > len(tokens):
        return False
    return any(
        tuple(tokens[index : index + len(sequence)]) == sequence
        for index in range(len(tokens) - len(sequence) + 1)
    )


def _unsupported_flag_findings(tokens: list[str]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for verb_sequence, flag, alternative in UNSUPPORTED_FLAGS:
        if flag in tokens and _is_contiguous_subsequence(tokens, verb_sequence):
            findings.append(
                (
                    "unsupported-flag",
                    f"unsupported flag {flag!r} after {' '.join(verb_sequence)}; "
                    f"{alternative}",
                )
            )
    return findings


def classify_span(command: str) -> list[tuple[str, str]]:
    """Return ``(kind, detail)`` violations for one command-bearing span."""
    tokens = command_tokens(command)
    if not tokens:
        return []

    findings = _unsupported_flag_findings(tokens)

    if _shell_composition_finding(command, tokens):
        findings.append(
            (
                "shell-composition",
                "forbidden shell composition; use one `sol` or approved `journal` "
                "command without pipes, redirects, chaining, substitution, or wrappers",
            )
        )
        return findings

    if tokens[0] == "sol":
        return findings

    if tokens[0] in READ_TOOLS:
        return findings

    if (
        tokens[0] == "journal"
        and len(tokens) >= 2
        and tokens[1] not in ALLOWED_JOURNAL_COMMANDS
    ):
        findings.append(
            (
                "bare-journal",
                f"forbidden `journal {tokens[1]}`; {JOURNAL_ALTERNATIVE}",
            )
        )

    for token in tokens:
        for name in CLI_AGENT_TOOLS:
            if token == name or token.startswith(f"{name}("):
                findings.append(
                    (
                        "cli-agent-tool",
                        f"forbidden Claude Code tool `{name}`; {READ_ALTERNATIVE}",
                    )
                )

    if tokens[0] in SHELL_READ_COMMANDS:
        findings.append(
            (
                "shell-read",
                f"forbidden shell reader `{tokens[0]}`; {READ_ALTERNATIVE}",
            )
        )

    if tokens[0].startswith("journal/"):
        findings.append(
            (
                "raw-journal-path",
                f"forbidden raw journal path `{tokens[0]}`; {READ_ALTERNATIVE}",
            )
        )

    return findings


def lint_prompt(body: str) -> list[tuple[int, str, str]]:
    """Return sorted ``(lineno, kind, detail)`` violations for prompt body."""
    findings: list[tuple[int, str, str]] = []
    for lineno, span in extract_command_spans(body):
        for kind, detail in classify_span(span):
            findings.append((lineno, kind, detail))
    findings.sort()
    return findings


def discover_prompts() -> list[tuple[str, str]]:
    """Return ``(repo-relative-posix-path, body)`` for cogitate prompts."""
    prompts: list[tuple[str, str]] = []
    for info in talent.get_talent_configs(type="cogitate").values():
        path = Path(str(info["path"]))
        rel = Path(os.path.relpath(path, ROOT)).as_posix()
        body = frontmatter.load(path).content
        prompts.append((rel, body))
    return sorted(prompts)


def count_violations() -> dict[tuple[str, str], int]:
    """Map ``(posix-relpath, kind)`` -> occurrence count across prompts."""
    counts: dict[tuple[str, str], int] = {}
    for rel, body in discover_prompts():
        for _lineno, kind, _detail in lint_prompt(body):
            key = (rel, kind)
            counts[key] = counts.get(key, 0) + 1
    return counts


def evaluate(
    allowlist: dict[tuple[str, str], int],
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(over, stale, tracked)`` human-readable lines."""
    live: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for rel, body in discover_prompts():
        for lineno, kind, detail in lint_prompt(body):
            live.setdefault((rel, kind), []).append((lineno, detail))

    over: list[str] = []
    stale: list[str] = []
    tracked: list[str] = []

    for rel, kind in sorted(set(live) | set(allowlist)):
        key = (rel, kind)
        findings = sorted(live.get(key, []))
        count = len(findings)
        allowed = allowlist.get(key, 0)
        if count > allowed:
            lines = ", ".join(str(lineno) for lineno, _detail in findings)
            details = "; ".join(sorted({detail for _lineno, detail in findings}))
            over.append(
                f"{rel}: {kind} count {count} exceeds allowed {allowed} "
                f"at line(s) {lines}: {details} - {PROMPT_CONTRACT}; see "
                "the check_cogitate_prompts gate."
            )
        elif count < allowed:
            delete_hint = " (delete it)" if count == 0 else ""
            stale.append(
                f"{rel}: {kind} allowlisted at {allowed} but {count} live - "
                f"lower the entry to {count}{delete_hint} - "
                "check_cogitate_prompts ratchets toward empty; a cleaned prompt "
                "removes its entry."
            )
        elif allowed:
            tracked.append(f"{rel}: {count}/{allowed} {kind} (allowlisted)")

    return over, stale, tracked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cogitate prompt command-span lint")
    parser.parse_args(argv)

    over, stale, tracked = evaluate(ALLOWLIST)

    if tracked:
        print("cogitate-prompts: known violations (allowlisted):")
        for line in tracked:
            print(f"  {line}")
        print()

    if over or stale:
        if over:
            print("cogitate-prompts: NEW violations:", file=sys.stderr)
            for line in over:
                print(f"  {line}", file=sys.stderr)
            print(file=sys.stderr)
        if stale:
            print("cogitate-prompts: STALE allowlist entries:", file=sys.stderr)
            for line in stale:
                print(f"  {line}", file=sys.stderr)
            print(file=sys.stderr)
        print(
            f"{PROMPT_CONTRACT}; lower stale allowlist counts as prompts are cleaned.",
            file=sys.stderr,
        )
        return 1

    print("cogitate-prompts: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
