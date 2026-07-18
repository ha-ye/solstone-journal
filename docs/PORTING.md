# Python to Rust Porting Doctrine

This document is for engineers and coding agents porting solstone behavior from
Python into the Rust workspace under `core/`. It records the wave-0 rules before
any behavior moves.

## Workspace Scope

The Rust workspace lives at `core/`. It contains a thin `solstone-core` bin,
the `solstone-core-cli` adapter library, and subsystem crates such as
`solstone-core-journal` as Python behavior is ported.

Rust crates use edition 2024, `rust-version = "1.87"`, and
`license = "AGPL-3.0-only"` inherited from `core/Cargo.toml`. Every `.rs` file
starts with the two-line `//` SPDX header used by `AGENTS.md`.

## Mobile Readiness

Rust subsystem logic should stay eligible for the iOS canary unless a host-only
adapter makes that impossible. The native markdown indexer keeps discovery,
metadata, segment parsing, stream-marker reads, and markdown chunking in
`solstone-core-indexer`, which remains covered by `check-rust-ios`.
`solstone-core-indexer-store` is excluded because its bundled-C SQLite build
cannot cross-compile from the Linux host. That exclusion is for the storage
adapter, not for the indexer logic. The eventual iOS path is to link the system
`libsqlite3` that iOS ships instead of bundling SQLite, then return the store
crate to the iOS gate.

## Owner Timezone

The Python owner-timezone fallback is effectively `identity.timezone` from
`config/journal.json`, then UTC. The apparent host-local branches in
`get_owner_timezone()` are dead because CPython `astimezone()` returns a
fixed-offset `datetime.timezone` without a `.key`. Reproducing host-local time
in Rust would diverge from Python behavior.

## Layering

`solstone-core` is a process shell only: it reads `std::env::args()`, writes
stdout or stderr, and returns process exit codes.

`solstone-core-cli` is the CLI adapter. It takes an argv slice as input and
returns a typed outcome. It never reads `std::env`, never prints, and never
exits.

Subsystem libraries added in later waves take config and paths as parameters,
own no process-global state, and do not parse argv. The "no argv parsing in core
logic" rule binds these subsystem libraries.

## Error And Type Mapping

Python exceptions become `Result` errors at the Rust boundary. A port should
name the error cases it can emit; it should not collapse expected failures into
strings or panics.

Python `None` becomes `Option`. Truthiness becomes explicit predicates or
comparisons. A port must not rely on implicit emptiness checks when the Python
source distinguished empty, missing, and false values.

Python context managers and `__del__` cleanup become RAII ownership and `Drop`
where cleanup is unconditional. Fallible cleanup remains explicit because `Drop`
cannot return an error.

Monkeypatching, dynamic dispatch, decorators, middleware, and import-time side
effects become explicit seams. Before porting code with any of these concerns,
inventory the concern and add a conformance test that fails when the concern is
absent; absence is otherwise invisible in a diff.

## Data Boundaries

Python integers are arbitrary precision. Rust ports use `i64` for JSON-facing
integers unless a specific writer documents another width. Overflow is a
`Result` error at the boundary, never a silent wrap or debug-only assertion.
JSON integers outside `i64` are rejected at parse.

Python `str` maps to UTF-8 `String` or `&str`. Python `bytes` maps to
`Vec<u8>`. Filesystem paths map to `PathBuf` or `OsStr`; POSIX paths are not
guaranteed to be UTF-8, so ports must not use `.to_str().unwrap()`.

## Porting Instruments

`scripts/build_core_fixtures.py` generates Rust-facing fixtures under
`core/fixtures/`.

`tests/verify_indexer_differential.py` runs the indexer differential harness and
writes its report under the harness work directory unless `--report` is supplied.

## JSON And Hashing

Canonical JSON is a per-writer contract, not a repository default. A Rust port
inherits the ordering and separators of the specific writer it replaces. Examples
with explicit sorted output today include `solstone/think/talent_provenance.py`,
`solstone/think/data_state.py`, `solstone/think/readiness.py`, and
`solstone/think/steward.py`.

`solstone/think/talent_provenance.py` computes identity hashes from the exact
string returned by `_canonical_json`. Byte drift changes the SHA-256 identity.
Two traps matter:

- Float exponent spelling: Python's `repr`-backed JSON formatting emits `1e+30`.
  Rust's standard JSON float formatting emits `1e30`. Same value, different
  bytes, different SHA-256.
- Non-finite values: Python emits bare `NaN` and `Infinity` tokens, which are
  not valid JSON. Rust's standard JSON serializers refuse to emit them, so a
  payload Python hashes today cannot round-trip through a conforming Rust writer.

Hashed canonical payloads therefore carry no floats and no non-finite values. If
a future port must hash a float, it owes a byte-exact Python `repr` emitter plus
a conformance test.

There is a pre-existing Python hazard: `_canonical_json` does not reject
non-finite values. A non-finite value can enter a hashed identity today. This
lode documents that hazard but does not change Python behavior.

## Unsupported Inputs

Ports use this vocabulary for unsupported behavior:

- `on-unsupported = abort`: fail loudly; this is the default.
- `on-unsupported = abort-silent`: decline without noisy logging when a caller
  has explicitly requested quiet refusal.
- `on-unsupported = fallback`: use the Python path while the port is active.

The reserved declined process exit code is 69, matching the sysexits meaning
"service unavailable"; it means this port declines to handle the input. It is
distinct from existing success, usage, empty-input, and temporary-failure codes.

This is not self-enforcing yet. `solstone/think/supervisor.py` currently maps
every non-zero scheduled-task exit except the empty-input sentinel to `error`.
The first wave that emits declined exits must add the scheduler branch.

## Dual Paths And Shims

The repository no-shims rule still stands. During an active port, a config-gated
old/new selection is a deliberate, time-boxed, per-change exception. Each dual
path needs a named deletion schedule. Do not add fallback aliases,
deprecated-parameter handling, or compatibility re-exports.

## Version Lockstep

`scripts/render_packaging.py` keeps Python leaf packages and Cargo metadata in
lockstep with the root `pyproject.toml` version. The current lockstep assumes
`X.Y.Z`. A Python pre-release such as `0.9.0rc1` is not a valid Cargo version;
before tagging one, add and test an explicit translation rule.

## Journal Resolution Decisions

The first behavior port is `get_journal_info()` / `get_journal()` from
`solstone/think/utils.py`, backed by `solstone/think/user_config.py`.

1. **MSRV is 1.87 for safe home fallback.** Rust 1.85 still deprecates
   `std::env::home_dir()` under `-D warnings`; Rust 1.87 undeprecates it. The
   journal resolver uses the hybrid shape: literal `HOME` when present, and
   `std::env::home_dir()` only when `HOME` is absent. This avoids a hand-rolled
   unsafe `getpwuid_r` implementation.
2. **No unsafe passwd FFI.** Keeping the old 1.85 floor would require libc
   fallback code with buffer sizing and retry behavior for a home-directory
   lookup. That defect surface is not justified for this port.
3. **Home normalization follows `str(Path.home() / "journal")`, not just
   `os.path.expanduser("~")`.** The port reproduces the observed layers needed
   by `user_config.default_journal()`: present-but-empty `HOME` becomes `/`,
   trailing slashes are stripped with an or-root fallback, repeated separators
   and `.` components are collapsed lexically, exactly two leading slashes are
   preserved, `..` is not collapsed, and `.` joined with `journal` renders as
   `journal`. If the expanded home still starts with `~`, the port raises the
   same home-unavailable error as Python's pathlib guard. This is not a general
   pathlib port.
4. **Config stripping is Python stripping.** Rust `str::trim()` is not equivalent
   to Python `str.strip()` because Python also strips U+001C..U+001F. Journal
   config values use a small Python-compatible strip helper. Environment values
   are never stripped.
5. **TOML parsing uses `toml_edit` 0.22 parse-only.** The latest TOML crates
   track TOML 1.1 behavior such as accepting `\e`, which Python `tomllib`
   rejects. `toml_edit = 0.22.27` with only the `parse` feature matches the
   `tomllib` cases this port needs and keeps the lock cost smaller than the
   `toml` facade.
6. **Unit vector tests do not mutate process env.** The shared JSON vectors
   carry raw `HOME` / `SOLSTONE_JOURNAL` inputs, config bytes, checkout-root
   state, and observed Python outcomes. Rust unit tests replay those cases by
   passing values directly to library functions. Subprocess binary tests may use
   `Command::env` and `env_remove`.
7. **The binary wires no source-checkout root.** A native binary in a venv has no
   meaningful Python checkout root, so `solstone-core journal-path` deliberately
   resolves only CLI override, env, config, and default. The library still keeps
   the four Python resolver sources: `env`, `config`, `source`, and `default`.
8. **The binary label vocabulary is a superset.** `journal-path --journal PATH`
   is a binary-surface override with no Python equivalent in `get_journal_info()`.
   It short-circuits the library resolver and prints label `cli`; the library
   `Source` enum does not add a fifth variant.
9. **Non-UTF-8 env paths stay as paths.** The Rust API accepts `OsStr` /
   `PathBuf` and has Rust-only Unix tests for non-UTF-8 env paths. The shared
   JSON vector file is UTF-8 and does not encode arbitrary env bytes.
10. **Create errors are structural and shape-equivalent.** Directory creation
    errors carry source label, path, and `io::Error` fields. Their display shape
    mirrors Python's `could not create journal directory ({source}): {path}: ...`,
    but the OS-error text is not byte-equivalent to Python's `OSError`. Nothing
    consumes that message programmatically.
11. **No improvements to path meaning.** The port does no tilde expansion,
    canonicalization, resolving, absolutization, caching, new env vars, or
    config-gated dual path. `~/journal` from config remains a literal relative
    path.
