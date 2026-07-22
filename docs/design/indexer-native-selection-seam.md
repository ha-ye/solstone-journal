# Config-Gated Native Indexer Selection Seam

This is the review-gate design for routing selected `journal indexer` write-only
invocations to `solstone-core indexer`. It does not implement the seam.

## Decisions And Scope

- Config lives in top-level `config/journal.json` under `core.indexer` and
  `core.indexer_on_decline`.
- Valid `core.indexer` values are `python` and `rust`.
- Valid `core.indexer_on_decline` values are `abort` and `fallback`.
- Do not add these keys to `solstone/think/journal_default.json`. They are a
  two-release-lifetime migration control deleted in N+2. Adding them to every
  fresh journal would require a later migration just to remove them. Absent key
  now defaults native-eligible writes to Rust on hosts covered by the probe
  module's solstone-core package predicate, while uncovered hosts keep Python.
- The only implementation seam is the new sibling module
  `solstone/think/indexer/native_seam.py`, imported by
  `solstone/think/indexer/cli.py`.
- `cli.py::main()` changes its return contract to `int | None`. Existing Python
  branches keep returning `None`; native terminal paths return the native exit
  code as `int`.
- `main()` calls the seam once, after the current no-op help guard and before
  the existing Python write blocks.
- If `args.query is not None`, the seam returns `None` before reading selection
  config. Query-only and mixed write+query invocations are entirely Python.

## Call Point

`cli.py::main()` should keep the existing parse, `require_solstone()`, journal
resolution, and no-op help behavior. Immediately after the no-op guard:

- call `native_seam.maybe_run_native_indexer(args, journal)`;
- if it returns an `int`, return that value from `main()`;
- if it returns `None`, continue into the existing Python implementation.

This keeps one chokepoint and lets fallback-on-decline reuse the existing Python
branches without duplicating reset, rebuild, rescan, or query code.

## Routing Table

Selection is read once per launch for write-only operation sets. It is not
re-read after a native decline or before fallback.

| Operation set | Config and host state | Route |
| --- | --- | --- |
| Bare `journal indexer` with no operation flags and no query | Any, not read | Existing `parser.print_help()`, return `None` |
| Query-only, including `-q foo` or interactive `-q` | Any, not read | Python |
| Mixed write+query, including `--rescan -q foo` | Any, not read | Python |
| Pure native-eligible writes | `core.indexer = "python"` | Python |
| Pure native-eligible writes | `core.indexer = "rust"` | Native path, subject to handshake gate |
| Pure native-eligible writes | `core.indexer` unset and host covered by the probe module's solstone-core package predicate | Native path, subject to handshake gate |
| Pure native-eligible writes | `core.indexer` unset and host not covered by the probe module's solstone-core package predicate | Python |
| `--rescan-file PATH` combined with `--rescan` or `--rescan-full` | Any, not read | Python |
| Any write-only invocation | invalid `core.indexer` | Print config error, return `core_handshake.EX_CONFIG` |
| Any write-only invocation | invalid present `core.indexer_on_decline` | Print config error with resolved provenance, return `core_handshake.EX_CONFIG` |

The `--rescan-file PATH` plus `--rescan` or `--rescan-full` combination is not
native-eligible. The seam returns `None`, Python runs, and the invocation keeps
today's byte-identical behavior: argparse `parser.error()` exits 2 after any
earlier `--reset` and `--rebuild-edges` work has already run. The flag-off path
must be byte-identical, and an invalid invocation must not change its error
surface just because a migration key is set.

When both `--rescan` and `--rescan-full` are set, emit only `--rescan-full`.
When only `--rescan` is set, emit `--rescan`. One scan flag is deterministic and
matches Python's effective semantics: `rescan_full` is the full-scan boolean.

## Config Validation

Use `read_journal_config(journal)` from `solstone.think.journal_config`. Do not
catch `CorruptConfigError`.

Query invocations never read selection config because they never select an
implementation. A read-only query must not fail on a migration key it does not
use.

Resolution rules:

- present non-object `core` is a host-independent config error;
- present `core.indexer` is validated before coverage is checked;
- explicit `core.indexer = "python"`: selected implementation is `python`;
- explicit `core.indexer = "rust"`: selected implementation is `rust`;
- absent `core.indexer`: call the injected coverage checker; covered hosts
  resolve to `rust`, and uncovered hosts resolve to `python`;
- absent `core.indexer_on_decline`: policy is `abort`;
- explicit `abort` or `fallback`: policy is that value;
- any other present `core.indexer_on_decline` value is a config error, even if
  the selected implementation is Python;
- invalid decline-policy errors render the resolved selection provenance.

Config errors return `core_handshake.EX_CONFIG`.

## Native Tail

Build native argv from the parsed argparse `Namespace`, never raw argv.

Native argv shape:

- executable: `str(helper_locator())`, defaulting to
  `helper_path_for_executable()`;
- command: `indexer`;
- explicit journal: `--journal`, then the already resolved `journal` string from
  `get_journal()`;
- operation flags, in Python operation order: `--reset`, `--rebuild-edges`,
  scan-file-or-scan flags.

Drop argparse-only flags by construction: no `--verbose`, no `--debug`, no
query/filter/pagination flags.

For `--rescan-file`, normalize before tail construction using existing Python
helpers:

- `journal_path = Path(journal).resolve()`;
- absolute input: `Path(args.rescan_file).resolve()`;
- relative input: `resolve_journal_path(journal_path, args.rescan_file).resolve()`;
- pass the resulting absolute path string to native `--rescan-file`.

This matches Python `index_file()` resolution while avoiding the native relative
path divergence where `chronicle/20240101/...` would be kept verbatim. Absolute
paths are equivalent across Python and Rust because both resolve/canonicalize
the journal and input path, then strip the `chronicle/` prefix for paths under
the chronicle root.

## Empty-Tail Guard

The seam must raise a runtime guard if no native operation flag was built before
launching `solstone-core`. This catches seam bugs where `rust` is selected but
the native tail contains only `indexer --journal PATH`; native would otherwise
print usage and exit 0 without indexing.

Failure mode: raise `RuntimeError` before spawning native. The message is:

`{provenance}, but found no native-supported operation flags to pass. This is a seam bug; set core.indexer to 'python' to revert.`

## Handshake Policy

The seam must reuse `solstone.think.core_handshake`; do not add another binary
discovery path.

Default call:

- `check_solstone_core_handshake()` before launching native;
- `helper_path_for_executable()` to locate the binary for the actual native argv.

Test seams exposed by `native_seam`:

- `config_reader`, defaulting to `read_journal_config`;
- `handshake_checker`, defaulting to `check_solstone_core_handshake`;
- `helper_locator`, defaulting to `helper_path_for_executable`;
- `native_runner`, defaulting to `subprocess.run`.
- `coverage_checker`, defaulting to the seam's probe-backed coverage wrapper.

Handshake outcomes under explicit `core.indexer = "rust"` or covered-host
absent `core.indexer`:

- `ok`: run native;
- `skip`: print/log a rust-selected abort and return `core_handshake.EX_CONFIG`;
- `fail`: print/log a rust-selected abort and return `core_handshake.EX_CONFIG`.

This intentionally differs from `supervisor.py`, where `skip` is logged and the
supervisor continues. The supervisor is checking optional install skew at service
startup. Here the user explicitly selected `rust` for this command, so a skipped
or failed handshake means the selected implementation cannot run. Falling back
to Python would silently ignore the selected implementation.

## Subprocess Shape

Use `subprocess.run` with:

- argv: `[str(helper_path), "indexer", "--journal", journal, *operation_flags]`;
- `check=False`;
- `cwd=None` so the child inherits the current working directory;
- `env=None` so the child inherits the existing environment unchanged;
- no mutation of `SOLSTONE_JOURNAL`;
- no `capture_output`;
- no `stdout` or `stderr` override;
- no `start_new_session`;
- no `process_group`;
- no timeout in the seam.

When the command is queued by the supervisor, the parent `journal indexer`
process is already spawned by `ManagedProcess.spawn()` with `process_group=0`.
The native process must remain an ordinary descendant in that process group so
runner process-tree termination and health-log streaming continue to work.

## Exit-Code Mapping

| Native return code | `core.indexer_on_decline = "abort"` | `core.indexer_on_decline = "fallback"` |
| --- | --- | --- |
| 0 | Return 0. Native stdout/stderr already inherited. | Return 0. Native stdout/stderr already inherited. |
| 64 | Print usage-error message. Return 64. Never rerun Python. | Print usage-error message. Return 64. Never rerun Python. |
| 69 | Print decline-abort message. Return 69. | Print decline-fallback warning. Return `None` so `cli.py` continues into the existing Python write path. Python status surfaces. |
| 75 | Print tempfail message. Return 75. Never rerun Python. | Print tempfail message. Return 75. Never rerun Python. |
| negative return code | Print signal-death tempfail message. Return 75. Never rerun Python. | Print signal-death tempfail message. Return 75. Never rerun Python. |
| any other positive nonzero | Print generic native-failed message. Return the native code. Never rerun Python. | Print generic native-failed message. Return the native code. Never rerun Python. |

If `subprocess.run` raises `OSError` after a successful handshake, treat it as a
native tempfail: print/log a launch-failed message and return 75. Do not rerun
Python.

## Exact Stderr Strings

All owner-facing messages go to stderr. Also log them at error level, except the
69 fallback warning, which logs at warning level.

Provenance clauses:

- explicit: `journal indexer selected implementation {selected!r} from config key core.indexer`
- covered + absent: `journal indexer defaulted to implementation 'rust' because config key core.indexer is unset and solstone-core is packaged for this platform`
- uncovered + absent: `journal indexer defaulted to implementation 'python' because config key core.indexer is unset and solstone-core is not packaged for this platform`

Invalid `core.indexer`:

`journal indexer selected implementation 'invalid' from config key core.indexer; found {value!r}; expected 'python' or 'rust'. Set core.indexer to 'python' to revert.`

Invalid `core` section:

`journal indexer selected implementation 'invalid' from config key core.indexer, but config section core has invalid value {value!r}; expected an object. Set core.indexer to 'python' to revert.`

Invalid `core.indexer_on_decline`:

`{provenance}, but config key core.indexer_on_decline has invalid value {value!r}; expected 'abort' or 'fallback'. Set core.indexer to 'python' to revert.`

Handshake `skip` under rust:

`{provenance}, but solstone-core handshake returned 'skip': {message}. Set core.indexer to 'python' to revert.`

Handshake `fail` under rust:

`{provenance}, but solstone-core handshake returned 'fail': {message}. Set core.indexer to 'python' to revert.`

Native decline 69 under abort:

`{provenance}, but solstone-core indexer declined this input with exit 69. Set core.indexer_on_decline to 'fallback' to retry unsupported inputs through Python, or set core.indexer to 'python' to revert.`

Native decline 69 under fallback:

`{provenance}, but solstone-core indexer declined this input with exit 69; falling back to Python because core.indexer_on_decline is 'fallback'. Set core.indexer to 'python' to revert.`

Native usage error 64:

`{provenance}, but solstone-core indexer exited 64 (usage error). This is a seam argument-construction bug; set core.indexer to 'python' to revert.`

Native tempfail 75:

`{provenance}, but solstone-core indexer exited 75 (temporary failure). Set core.indexer to 'python' to revert.`

Native launch `OSError` mapped to 75:

`{provenance}, but launching solstone-core indexer failed: {error}. Set core.indexer to 'python' to revert.`

Native signal death mapped to 75:

`{provenance}, but solstone-core indexer died from signal {signal_number} (returncode {returncode}); treating as temporary failure. Set core.indexer to 'python' to revert.`

Other native nonzero:

`{provenance}, but solstone-core indexer exited {returncode}. Set core.indexer to 'python' to revert.`

Empty-tail seam bug:

`{provenance}, but found no native-supported operation flags to pass. This is a seam bug; set core.indexer to 'python' to revert.`

## PORTING.md Replacement Text

Replace the final paragraph of `docs/PORTING.md` section `Unsupported Inputs`
with:

> The first declined-exit wave is the config-gated indexer selection seam in the
> Python `journal indexer` wrapper. When the seam routes a write-only invocation
> to `solstone-core indexer`, the native indexer returns 69 when it declines an
> unsupported input. The wrapper handles that code according to
> `config/journal.json` key `core.indexer_on_decline`: `abort` reports the
> decline and exits 69, while `fallback` reruns the same operation on the Python
> indexer. Usage errors (64) and temporary failures (75) are never retried in
> Python. Signal death is normalized to temporary failure (75). The supervisor
> intentionally keeps mapping non-zero scheduled-task exits to `error`;
> abort-by-default makes that classification correct, and decline visibility
> lives in the wrapper's stderr and logs.

Add this subsection after `Unsupported Inputs`:

### Indexer Selection Seam

`journal indexer` has a temporary Python/native selection seam for the native
indexer migration. It reads `config/journal.json` once at command launch.
Query-only and mixed write+query invocations stay on Python for the whole
invocation and do not read selection config.

For write-only native-eligible invocations, explicit `core.indexer = "python"`
runs the Python indexer and remains the rollback switch. Explicit
`core.indexer = "rust"` selects the sibling `solstone-core indexer` binary
everywhere and keeps its loud handshake-failure behavior. When `core.indexer` is
unset, hosts covered by the probe module's solstone-core package predicate
default to Rust; uncovered hosts keep Python.

Backup-restore full rescans, direct `index_file()` callers, chat stream appends,
importers, day-accumulator writes, and index-mutating deletes bypass
`journal indexer` and stay on the Python indexer during the dual window.

The seam normalizes `--rescan-file` to an absolute path with the same Python
journal-path resolver used by `index_file()` before passing it to native. This
keeps `chronicle/`-prefixed relative paths from being interpreted differently by
the Rust relative-path resolver.

The selection keys are intentionally absent from `journal_default.json`. They
are a two-release-lifetime migration control: release N kept Python as the
absent-key default and allowed opt-in Rust; release N+1 defaults absent-key
native-eligible writes to Rust on covered hosts while still honoring explicit
Python and keeping uncovered hosts on Python; release N+2 may delete the Python
orchestration path and remove `core.indexer` / `core.indexer_on_decline` only
after a completed normal alpha interval and an explicit uncovered-host
disposition.

## Seam Module Docstring Content

Use this content for `solstone/think/indexer/native_seam.py`:

> Config-gated native-indexer selection for the journal indexer CLI.
>
> This module is the only command-level seam between the Python indexer and
> `solstone-core indexer`. It is deliberately narrower than the full in-process
> indexer API: backup-restore full rescans, per-file `index_file()` calls from
> segment finish, chat stream appends, importers, and day-accumulator writes,
> plus index-mutating deletes such as observer prune, share-delete, and
> entity-merge edge folds, bypass `journal indexer` and stay on the Python
> indexer during the dual window.
>
> Selection is read once from `config/journal.json` at command launch. Explicit
> `python` stays on Python and explicit `rust` selects the native path. When
> `core.indexer` is unset, write-only native-eligible invocations default to
> Rust on hosts covered by the probe module's solstone-core package predicate
> and to Python on uncovered hosts. Query and mixed write+query invocations stay
> in Python.

Known bypass references for the implementation review:

- backup restore calls `scan_journal(..., full=True)` directly in
  `solstone/think/backup/restore.py`;
- segment repair/reindex calls `index_file()` directly in
  `solstone/think/segment.py`;
- chat stream appends call `index_file()` directly in
  `solstone/convey/chat_stream.py`;
- importers call `index_file()` directly in `solstone/think/importers/cli.py`;
- day accumulator calls `index_file()` directly in
  `solstone/think/day_accumulator.py`;
- observer prune and share-delete call index-mutating helpers directly in
  `solstone/apps/observer/prune.py` and
  `solstone/apps/observer/share_delete.py`;
- entity merge folds edge rows directly in `solstone/think/entities/merge.py`.

## Test Plan

Each dispatch test must pair the expected implementation's positive observable
with the absence of the other implementation's observable. Python dispatch is
proved by recorded `reset_journal_index`, `rebuild_edges`, `index_file`, or
`scan_journal` stubs on `indexer_cli`. Native dispatch is proved by a recorded
native-runner argv. A seam return value alone is not dispatch proof.

Existing Python write-path CLI tests pin explicit
`{"core": {"indexer": "python"}}` through the CLI helper so zero-edge hint and
root task-log assertions continue to test the Python implementation rather than
selection.

Add a CLI selection matrix that monkeypatches `indexer_cli.maybe_run_native_indexer`
with a thin wrapper around the real seam while injecting only coverage,
handshake, helper, and native-runner boundaries:

- covered host plus absent `core.indexer`: record native argv and no Python
  write call;
- uncovered host plus absent `core.indexer`: record Python write call and prove
  handshake, helper, and native runner are not called;
- explicit `core.indexer = "python"`: record Python write call and prove
  coverage, handshake, helper, and native runner are not called;
- explicit `core.indexer = "rust"`: record native argv and prove coverage is not
  called.

Seam unit tests cover the coverage-aware absent default for both absent top-level
`core` and present empty `core`, single write flags, native compositions, explicit
selection bypassing coverage, default-provenance handshake failures, default
69-abort and 69-fallback handling, 64/75/signal/launch-failure mappings,
invalid decline under covered and uncovered absent-key hosts, and the rendered
empty-tail exception.

`test_run_command_propagates_native_nonzero_indexer_return` remains the
`sol_cli.run_command()` propagation check: it uses explicit rust, stubs native to
return a unique nonzero, and asserts that `main()` returns the same integer
instead of laundering it through `None -> 0`.

## Implementation Sequence

1. Add `solstone/think/indexer/native_seam.py` with SPDX header, docstring,
   config resolution, routing predicate, path normalization, handshake, native
   subprocess run, and exit-code handling.
2. Update `solstone/think/indexer/cli.py::main()` return type and add the single
   seam call after the no-op help guard.
3. Add focused unit tests for `native_seam.py`, plus one `run_command()` exit
   propagation test.
4. Update `docs/PORTING.md` with the replacement text above.
5. Run focused tests through `hop check`, then broader indexer/sol tests.

## Risks And Open Questions

- `core.indexer_on_decline = "fallback"` can duplicate earlier native side
  effects before rerunning Python if native declines after completing reset or
  rebuild. That is intentional: fallback means rerun the same operation set on
  Python and let Python's status surface.
