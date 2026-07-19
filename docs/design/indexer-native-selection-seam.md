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
  is the honest default path for existing and new journals.
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

| Operation set | Config state | Route |
| --- | --- | --- |
| Bare `journal indexer` with no operation flags and no query | Any, not read | Existing `parser.print_help()`, return `None` |
| Query-only, including `-q foo` or interactive `-q` | Any, not read | Python |
| Mixed write+query, including `--rescan -q foo` | Any, not read | Python |
| Pure write: `--reset` only | section absent, key absent, or `core.indexer = "python"` | Python |
| Pure write: `--rebuild-edges` only | section absent, key absent, or `core.indexer = "python"` | Python |
| Pure write: `--rescan` | section absent, key absent, or `core.indexer = "python"` | Python |
| Pure write: `--rescan-full` | section absent, key absent, or `core.indexer = "python"` | Python |
| Pure write: `--rescan-file PATH` | section absent, key absent, or `core.indexer = "python"` | Python |
| Pure composed writes: any subset of `--reset`, `--rebuild-edges`, plus `--rescan` and/or `--rescan-full` | section absent, key absent, or `core.indexer = "python"` | Python |
| Pure composed writes: any subset of `--reset`, `--rebuild-edges`, plus `--rescan-file PATH` | section absent, key absent, or `core.indexer = "python"` | Python |
| Any valid pure write above | `core.indexer = "rust"` | Native |
| `--rescan-file PATH` combined with `--rescan` or `--rescan-full` | `core.indexer = "rust"` | Python |
| Any write-only invocation | invalid `core.indexer` | Print config error, return `core_handshake.EX_CONFIG` |
| Any write-only invocation | invalid present `core.indexer_on_decline` | Print config error, return `core_handshake.EX_CONFIG` |

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

- absent `core` section: selected implementation is `python`, decline policy is
  `abort`;
- present `core` without `indexer`: selected implementation is `python`;
- explicit `core.indexer = "python"`: selected implementation is `python`;
- explicit `core.indexer = "rust"`: selected implementation is `rust`;
- absent `core.indexer_on_decline`: policy is `abort`;
- explicit `abort` or `fallback`: policy is that value;
- any other present value is a config error, even if the selected implementation
  is Python;
- present non-object `core` is a config error equivalent to being unable to read
  `core.indexer`.

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

`journal indexer selected implementation 'rust' from config key core.indexer, but found no native-supported operation flags to pass. This is a seam bug; set core.indexer to 'python' to revert.`

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

Handshake outcomes under `core.indexer = "rust"`:

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

Invalid `core.indexer`:

`journal indexer selected implementation 'invalid' from config key core.indexer; found {value!r}; expected 'python' or 'rust'. Set core.indexer to 'python' to revert.`

Invalid `core.indexer_on_decline`:

`journal indexer selected implementation {selected!r} from config key core.indexer, but config key core.indexer_on_decline has invalid value {value!r}; expected 'abort' or 'fallback'. Set core.indexer to 'python' to revert.`

Handshake `skip` under rust:

`journal indexer selected implementation 'rust' from config key core.indexer, but solstone-core handshake returned 'skip': {message}. Set core.indexer to 'python' to revert.`

Handshake `fail` under rust:

`journal indexer selected implementation 'rust' from config key core.indexer, but solstone-core handshake returned 'fail': {message}. Set core.indexer to 'python' to revert.`

Native decline 69 under abort:

`journal indexer selected implementation 'rust' from config key core.indexer, but solstone-core indexer declined this input with exit 69. Set core.indexer_on_decline to 'fallback' to retry unsupported inputs through Python, or set core.indexer to 'python' to revert.`

Native decline 69 under fallback:

`journal indexer selected implementation 'rust' from config key core.indexer, but solstone-core indexer declined this input with exit 69; falling back to Python because core.indexer_on_decline is 'fallback'. Set core.indexer to 'python' to revert.`

Native usage error 64:

`journal indexer selected implementation 'rust' from config key core.indexer, but solstone-core indexer exited 64 (usage error). This is a seam argument-construction bug; set core.indexer to 'python' to revert.`

Native tempfail 75:

`journal indexer selected implementation 'rust' from config key core.indexer, but solstone-core indexer exited 75 (temporary failure). Set core.indexer to 'python' to revert.`

Native launch `OSError` mapped to 75:

`journal indexer selected implementation 'rust' from config key core.indexer, but launching solstone-core indexer failed: {error}. Set core.indexer to 'python' to revert.`

Native signal death mapped to 75:

`journal indexer selected implementation 'rust' from config key core.indexer, but solstone-core indexer died from signal {signal_number} (returncode {returncode}); treating as temporary failure. Set core.indexer to 'python' to revert.`

Other native nonzero:

`journal indexer selected implementation 'rust' from config key core.indexer, but solstone-core indexer exited {returncode}. Set core.indexer to 'python' to revert.`

Empty-tail seam bug:

`journal indexer selected implementation 'rust' from config key core.indexer, but found no native-supported operation flags to pass. This is a seam bug; set core.indexer to 'python' to revert.`

## PORTING.md Replacement Text

Replace the final paragraph of `docs/PORTING.md` section `Unsupported Inputs`
with:

> The first declined-exit wave is the config-gated indexer selection seam in the
> Python `journal indexer` wrapper. `solstone-core indexer` returns 69 when the
> native indexer declines an unsupported input. The wrapper handles that code
> according to `config/journal.json` key `core.indexer_on_decline`: `abort`
> reports the decline and exits 69, while `fallback` reruns the same operation
> on the Python indexer. Usage errors (64) and temporary failures (75) are never
> retried in Python. Signal death is normalized to temporary failure (75). The
> supervisor intentionally keeps mapping non-zero scheduled-task exits to
> `error`; abort-by-default makes that classification correct, and decline
> visibility lives in the wrapper's stderr and logs.

Add this subsection after `Unsupported Inputs`:

### Indexer Selection Seam

`journal indexer` has a temporary Python/native selection seam for the native
indexer migration. It reads `config/journal.json` once at command launch. An
absent `core` section, absent `core.indexer`, and explicit
`core.indexer = "python"` all run the Python indexer. `core.indexer = "rust"`
runs the sibling `solstone-core indexer` binary for write-only invocations,
passing `--journal <path>` explicitly and constructing operation flags from the
parsed argparse namespace. Query-only and mixed write+query invocations stay on
Python for the whole invocation and do not read selection config.

The seam normalizes `--rescan-file` to an absolute path with the same Python
journal-path resolver used by `index_file()` before passing it to native. This
keeps `chronicle/`-prefixed relative paths from being interpreted differently by
the Rust relative-path resolver.

The selection keys are intentionally absent from `journal_default.json`. They
are a two-release-lifetime migration control: release N keeps Python as the
absent-key default and allows opt-in Rust; release N+1 flips the absent-key
default to Rust while still honoring explicit Python; release N+2 deletes the
Python orchestration path and removes `core.indexer` / `core.indexer_on_decline`
selection.

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
> Selection is read once from `config/journal.json` at command launch. Absent
> selection and explicit `python` continue in Python. `rust` runs the native
> binary only for write-only command invocations; query and mixed write+query
> invocations stay in Python.

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

Each test must use distinct observable values: native stubs return a visible
integer return code and record argv; Python stubs record calls and return through
the existing `None` path. A test passes only if the expected implementation's
observable appears and the other implementation's observable is absent.

- `test_absent_core_section_runs_python`: config reader returns no `core`;
  Python `scan_journal` stub records `python`; native runner raises if called.
- `test_absent_indexer_key_runs_python`: config reader returns `{"core": {}}`;
  same observables as above.
- `test_explicit_python_runs_python`: config reader returns
  `{"core": {"indexer": "python"}}`; same observables as above.
- `test_rust_rescan_invokes_native_with_explicit_journal`: config selects rust;
  handshake returns ok; helper locator returns fake path; native runner records
  `[helper, "indexer", "--journal", journal, "--rescan"]`; Python stubs raise if
  called.
- `test_rust_tail_drops_verbose_debug_and_query_filters`: Namespace has
  `verbose`, `debug`, and filter fields; native argv contains only native flags.
- `test_rust_composed_write_order`: Namespace has reset, rebuild, and full
  rescan; native argv orders `--reset`, `--rebuild-edges`, `--rescan-full`.
- `test_rust_prefers_rescan_full_when_both_scan_flags_are_set`: Namespace has
  both scan booleans true; native argv emits `--rescan-full` and does not emit
  `--rescan`.
- `test_rust_rescan_file_normalizes_chronicle_prefixed_relative_to_absolute`:
  input is `chronicle/20240101/talents/flow.md`; native argv receives an
  absolute path under journal `chronicle/`.
- `test_rust_rescan_file_with_rescan_stays_python`: config selects rust, but
  Namespace has `rescan_file` plus `rescan` or `rescan_full`; seam returns
  `None`, native runner raises if called, and the existing Python path preserves
  today's parser-error surface and partial side-effect ordering.
- `test_query_only_rust_selection_stays_python_without_reading_config`: query is
  present; config reader and native runner raise if called; search stubs prove
  Python path.
- `test_mixed_write_query_rust_selection_stays_python_without_reading_config`:
  `--rescan -q foo`; native runner raises if called; Python scan and search
  stubs both record calls.
- `test_invalid_indexer_value_returns_ex_config`: config has
  `{"core": {"indexer": "go"}}`; assert return `core_handshake.EX_CONFIG` and
  exact stderr.
- `test_invalid_indexer_on_decline_value_returns_ex_config`: config has invalid
  decline policy; assert return `core_handshake.EX_CONFIG` and exact stderr.
- `test_handshake_skip_under_rust_aborts`: handshake returns
  `CoreHandshakeResult("skip", "reason")`; assert return
  `core_handshake.EX_CONFIG`, exact stderr, and no native runner call.
- `test_handshake_fail_under_rust_aborts`: same for `fail`.
- `test_native_decline_abort_returns_69_without_python`: native runner returns
  69; policy abort; assert return 69, exact stderr, and no Python stub call.
- `test_native_decline_fallback_continues_to_python`: native runner returns 69;
  policy fallback; seam returns `None`; existing Python write stub records the
  same operation.
- `test_native_usage_error_64_never_fallbacks`: policy fallback but native
  returns 64; assert return 64 and no Python stub call.
- `test_native_tempfail_75_never_fallbacks`: policy fallback but native returns
  75; assert return 75 and no Python stub call.
- `test_native_signal_death_maps_to_tempfail`: native runner returns a negative
  return code such as -9; assert return 75, exact signal-death stderr, and no
  Python stub call.
- `test_native_other_nonzero_returns_code`: native returns 12; assert return 12
  and no Python stub call.
- `test_empty_tail_raises_runtime_error`: call tail builder with rust selection and
  no operation flags after bypassing the normal no-op guard; assert
  `RuntimeError` with the exact empty-tail message.
- `test_run_command_propagates_native_nonzero_indexer_return`: set `sys.argv` to
  a pure write invocation, stub native return code to a unique nonzero such as
  75, call `solstone.think.sol_cli.run_command("solstone.think.indexer")`, and
  assert the same code is returned. This proves `main()` returns an int and is
  not laundered through `None -> 0`.

Existing `tests/test_indexer_cli.py` should remain valid because it ignores
`main()`'s return value and asserts stubbed Python side effects. Add native seam
unit tests beside it rather than broadening the argparse harness unnecessarily.

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
