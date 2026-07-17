# Python to Rust Porting Doctrine

This document is for engineers and coding agents porting solstone behavior from
Python into the Rust workspace under `core/`. It records the wave-0 rules before
any behavior moves.

## Workspace Scope

The Rust workspace lives at `core/`. Wave 0 contains a thin `solstone-core` bin
and the `solstone-core-cli` adapter library. No behavior from `solstone/` has
moved yet.

Rust crates use edition 2024, `rust-version = "1.85"`, and
`license = "AGPL-3.0-only"` inherited from `core/Cargo.toml`. Every `.rs` file
starts with the two-line `//` SPDX header used by `AGENTS.md`.

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
