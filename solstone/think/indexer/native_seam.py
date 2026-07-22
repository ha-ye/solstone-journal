# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Config-gated native-indexer selection for the journal indexer CLI.

This module is the only command-level seam between the Python indexer and
`solstone-core indexer`. It is deliberately narrower than the full in-process
indexer API: backup-restore full rescans, per-file `index_file()` calls from
segment finish, chat stream appends, importers, and day-accumulator writes, plus
index-mutating deletes such as observer prune, share-delete, and entity-merge
edge folds, bypass `journal indexer` and stay on the Python indexer during the
dual window.

Selection is read once from `config/journal.json` at command launch. Explicit
`python` stays on Python and explicit `rust` selects the native path. When
`core.indexer` is unset, write-only native-eligible invocations default to Rust
on hosts covered by the probe module's solstone-core package predicate and to
Python on uncovered hosts. Query and mixed write+query invocations stay in
Python.

When 69 fallback is enabled, the command reruns the full operation set in
Python; any native operations that completed before the decline are repeated.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from solstone.think import core_handshake, probe
from solstone.think.journal_config import read_journal_config
from solstone.think.utils import resolve_journal_path

logger = logging.getLogger(__name__)

EXIT_UNAVAILABLE = 69
EXIT_USAGE = 64
EXIT_TEMPFAIL = 75

INVALID_INDEXER_MESSAGE = (
    "journal indexer selected implementation 'invalid' from config key "
    "core.indexer; found {value!r}; expected 'python' or 'rust'. "
    "Set core.indexer to 'python' to revert."
)
INVALID_CORE_SECTION_MESSAGE = (
    "journal indexer selected implementation 'invalid' from config key "
    "core.indexer, but config section core has invalid value {value!r}; "
    "expected an object. Set core.indexer to 'python' to revert."
)
INVALID_DECLINE_MESSAGE = (
    "{provenance}, but config key core.indexer_on_decline has invalid value "
    "{value!r}; expected 'abort' or 'fallback'. Set core.indexer to 'python' to "
    "revert."
)
HANDSHAKE_SKIP_MESSAGE = (
    "{provenance}, but solstone-core handshake returned 'skip': {message}. Set "
    "core.indexer to 'python' to revert."
)
HANDSHAKE_FAIL_MESSAGE = (
    "{provenance}, but solstone-core handshake returned 'fail': {message}. Set "
    "core.indexer to 'python' to revert."
)
NATIVE_DECLINE_ABORT_MESSAGE = (
    "{provenance}, but solstone-core indexer declined this input with exit 69. "
    "Set core.indexer_on_decline to 'fallback' to retry unsupported inputs "
    "through Python, or set core.indexer to 'python' to revert."
)
NATIVE_DECLINE_FALLBACK_MESSAGE = (
    "{provenance}, but solstone-core indexer declined this input with exit 69; "
    "falling back to Python because core.indexer_on_decline is 'fallback'. Set "
    "core.indexer to 'python' to revert."
)
NATIVE_USAGE_MESSAGE = (
    "{provenance}, but solstone-core indexer exited 64 (usage error). This is a "
    "seam argument-construction bug; set core.indexer to 'python' to revert."
)
NATIVE_TEMPFAIL_MESSAGE = (
    "{provenance}, but solstone-core indexer exited 75 (temporary failure). Set "
    "core.indexer to 'python' to revert."
)
NATIVE_LAUNCH_FAILED_MESSAGE = (
    "{provenance}, but launching solstone-core indexer failed: {error}. Set "
    "core.indexer to 'python' to revert."
)
NATIVE_SIGNAL_MESSAGE = (
    "{provenance}, but solstone-core indexer died from signal {signal_number} "
    "(returncode {returncode}); treating as temporary failure. Set core.indexer "
    "to 'python' to revert."
)
NATIVE_OTHER_NONZERO_MESSAGE = (
    "{provenance}, but solstone-core indexer exited {returncode}. Set "
    "core.indexer to 'python' to revert."
)
EMPTY_TAIL_MESSAGE = (
    "{provenance}, but found no native-supported operation flags to pass. This "
    "is a seam bug; set core.indexer to 'python' to revert."
)

ConfigReader = Callable[[str | Path | None], dict[str, Any]]
HandshakeChecker = Callable[[], core_handshake.CoreHandshakeResult]
HelperLocator = Callable[[], Path]
NativeRunner = Callable[..., subprocess.CompletedProcess[Any]]
CoverageChecker = Callable[[], bool]


def _platform_has_core_coverage() -> bool:
    system, machine = probe.current_solstone_core_platform()
    return probe.is_solstone_core_covered_platform(system, machine)


def _provenance_clause(
    selected: str,
    *,
    explicit: bool,
    covered: bool | None,
) -> str:
    if explicit:
        return (
            f"journal indexer selected implementation {selected!r} from config key "
            "core.indexer"
        )
    if covered:
        return (
            f"journal indexer defaulted to implementation {selected!r} because "
            "config key core.indexer is unset and solstone-core is packaged for "
            "this platform"
        )
    return (
        f"journal indexer defaulted to implementation {selected!r} because config "
        "key core.indexer is unset and solstone-core is not packaged for this "
        "platform"
    )


def maybe_run_native_indexer(
    args: argparse.Namespace,
    journal: str,
    *,
    config_reader: ConfigReader = read_journal_config,
    handshake_checker: HandshakeChecker = core_handshake.check_solstone_core_handshake,
    helper_locator: HelperLocator = core_handshake.helper_path_for_executable,
    native_runner: NativeRunner = subprocess.run,
    coverage_checker: CoverageChecker = _platform_has_core_coverage,
) -> int | None:
    """Run native indexer when selected, else continue in Python."""
    if args.query is not None:
        return None

    if not _has_write_operation(args):
        return None

    if args.rescan_file and (args.rescan or args.rescan_full):
        return None

    selected, decline_policy, provenance, error_message = _resolve_config(
        config_reader(journal),
        coverage_checker=coverage_checker,
    )
    if error_message is not None:
        _emit_error(error_message)
        return core_handshake.EX_CONFIG

    if selected == "python":
        return None

    handshake = handshake_checker()
    if handshake.status == "skip":
        _emit_error(
            HANDSHAKE_SKIP_MESSAGE.format(
                provenance=provenance,
                message=handshake.message,
            )
        )
        return core_handshake.EX_CONFIG
    if handshake.status == "fail":
        _emit_error(
            HANDSHAKE_FAIL_MESSAGE.format(
                provenance=provenance,
                message=handshake.message,
            )
        )
        return core_handshake.EX_CONFIG

    operation_flags = _build_operation_flags(args, journal)
    if not operation_flags:
        raise RuntimeError(EMPTY_TAIL_MESSAGE.format(provenance=provenance))

    helper_path = helper_locator()
    argv = [str(helper_path), "indexer", "--journal", journal, *operation_flags]
    try:
        completed = native_runner(argv, check=False)
    except OSError as exc:
        _emit_error(
            NATIVE_LAUNCH_FAILED_MESSAGE.format(
                provenance=provenance,
                error=exc,
            )
        )
        return EXIT_TEMPFAIL

    return _map_native_returncode(completed.returncode, decline_policy, provenance)


def _has_write_operation(args: argparse.Namespace) -> bool:
    return bool(
        args.reset
        or args.rebuild_edges
        or args.rescan
        or args.rescan_full
        or args.rescan_file
    )


def _resolve_config(
    config: dict[str, Any],
    *,
    coverage_checker: CoverageChecker,
) -> tuple[str, str, str, str | None]:
    core = config.get("core", {})
    if not isinstance(core, dict):
        return (
            "invalid",
            "abort",
            "",
            INVALID_CORE_SECTION_MESSAGE.format(value=core),
        )

    if "indexer" in core:
        selected = core["indexer"]
        if selected not in ("python", "rust"):
            return (
                "invalid",
                "abort",
                "",
                INVALID_INDEXER_MESSAGE.format(value=selected),
            )
        provenance = _provenance_clause(selected, explicit=True, covered=None)
    else:
        covered = coverage_checker()
        selected = "rust" if covered else "python"
        provenance = _provenance_clause(selected, explicit=False, covered=covered)

    decline_policy = core.get("indexer_on_decline", "abort")
    if decline_policy not in ("abort", "fallback"):
        return (
            selected,
            "abort",
            provenance,
            INVALID_DECLINE_MESSAGE.format(
                provenance=provenance,
                value=decline_policy,
            ),
        )

    return selected, decline_policy, provenance, None


def _build_operation_flags(args: argparse.Namespace, journal: str) -> list[str]:
    flags: list[str] = []
    if args.reset:
        flags.append("--reset")
    if args.rebuild_edges:
        flags.append("--rebuild-edges")

    if args.rescan_file:
        flags.extend(
            ["--rescan-file", _normalize_rescan_file(journal, args.rescan_file)]
        )
    elif args.rescan_full:
        flags.append("--rescan-full")
    elif args.rescan:
        flags.append("--rescan")

    return flags


def _normalize_rescan_file(journal: str, file_path: str) -> str:
    journal_path = Path(journal).resolve()
    path = Path(file_path)
    if path.is_absolute():
        return str(path.resolve())
    return str(resolve_journal_path(journal_path, file_path).resolve())


def _map_native_returncode(
    returncode: int,
    decline_policy: str,
    provenance: str,
) -> int | None:
    if returncode == 0:
        return 0
    if returncode < 0:
        _emit_error(
            NATIVE_SIGNAL_MESSAGE.format(
                provenance=provenance,
                signal_number=abs(returncode),
                returncode=returncode,
            )
        )
        return EXIT_TEMPFAIL
    if returncode == EXIT_USAGE:
        _emit_error(NATIVE_USAGE_MESSAGE.format(provenance=provenance))
        return EXIT_USAGE
    if returncode == EXIT_UNAVAILABLE:
        if decline_policy == "fallback":
            _emit_warning(NATIVE_DECLINE_FALLBACK_MESSAGE.format(provenance=provenance))
            return None
        _emit_error(NATIVE_DECLINE_ABORT_MESSAGE.format(provenance=provenance))
        return EXIT_UNAVAILABLE
    if returncode == EXIT_TEMPFAIL:
        _emit_error(NATIVE_TEMPFAIL_MESSAGE.format(provenance=provenance))
        return EXIT_TEMPFAIL

    _emit_error(
        NATIVE_OTHER_NONZERO_MESSAGE.format(
            provenance=provenance,
            returncode=returncode,
        )
    )
    return returncode


def _emit_error(message: str) -> None:
    print(message, file=sys.stderr)
    logger.error(message)


def _emit_warning(message: str) -> None:
    print(message, file=sys.stderr)
    logger.warning(message)
