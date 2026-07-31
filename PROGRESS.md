# SPL service Rust conversion — lane progress

## Current state

- Worktree: `spl-rust-lane-a` at `5ed4dacb8`.
- Frozen interface contract, seam inventory, and decided scope were reread after scope-check
  correction #2. Scope numbering is canonical: U1 HPKE, U2 blob receive, U3 buffering /
  admission / health, U4 relay client, U5 supervisor, U6 cutover.
- The initial direct U3 draft was rejected after the supervisor clarified that direct work fails
  the experiment. The delegated admission + health rewrite is accepted; buffer and base-mode
  HPKE delegate returns are under review. Base-mode HPKE is accepted; the buffer unit is held
  on a discovered zero-window divergence.
- U3 admission + health and U1 base-mode HPKE are accepted after focused gate reruns and lane
  review. The U3 A4 error-path mutation experiment remains pending with blob receive; the U1
  Python-generated parity corpus remains pending before Python deletion.
- U1 auth-mode HPKE is accepted after independent review: the RFC 9180 oracle opens and exports
  the expected bytes, focused lint/test/iOS gates are green, and its sender-SPKI failure remains
  distinct. U2's corrected HPKE blob-frame unit and U4 relay-health state are accepted after
  combined test, lint, and iOS gate reruns. The U5 pure posture/token gate is accepted after
  exact-posture, redaction, cache-invalidating state-transition, test, and lint review. U2
  archive safety and U4 relay control are accepted after parity corrections and a 41-test
  combined crate rerun. U2 ledger parsing and content-type units plus U4 reconnect backoff are
  accepted after a 58-test combined crate rerun. U4 tunnel routing and U5 lifecycle
  transition state are accepted after a 65-test combined crate rerun. The corrected U1 CLI
  parser is accepted after it restored the `-v`/`-d` service surface while retaining the
  no-key-on-argv rule for HPKE. U4 loopback piping is accepted after a 67-test crate rerun
  proved initial-prefix replay and reverse forwarding. U4 tunnel-status classification and U5
  read-only link-state/token loading are accepted after the 79-test combined crate rerun; U2
  authenticated blob preparation is accepted after its real auth-HPKE/archive/exporter
  round-trip. The corrected U2 fresh-ledger reader is accepted after a focused three-test
  rerun: an already-running reader observes Python's incomplete-to-complete browser record
  immediately, and file disappearance/non-list content fail closed.
- The corrected U5 shutdown function is accepted after a focused two-test rerun: it awaits
  `stop()` for tunnel cleanup, then aborts and awaits the separately-owned listen task. The
  full A7 live-WebSocket test remains pending with relay-client composition; this unit records
  the necessary task-cancellation half and makes no false claim that `stop()` closed a socket.
- The supervisor resolved both held blockers: U2 uses split `WsByteSource`/`WsByteSink` halves
  without changing U3, and the HPKE CLI uses fixed-count u32be field framing with redacted
  error classes. Resume U2 via delegated sink, receiver, and CLI framing units. AC-4 fixture
  generation is supervisor-owned; U6 remains blocked until the supervisor supplies its fixture
  commit.
- The delegated U2 `WsByteSink` sibling is accepted after its returned strict-lint correction.
  It uses the accepted U3 `impl Future + Send` public trait shape with no waiver, preserving
  the read-only reader boundary for `receive_blob` and U4's concurrent pipe.
- The delegated U1 pure HPKE CLI framing unit is accepted after workspace formatting and crate
  gates: bounded u32be fields, fixed operation counts, PKCS#8/SPKI key boundaries, and
  class-only error output are ready for binary dispatch without secret-bearing process I/O.
- The delegated U2 split-transport receiver is accepted after fresh-eyes review and its
  returned product-path test addition. It preserves all three refusal wire shapes, fresh
  fail-closed ledger lookup, per-sender release, real auth-HPKE/archive ingestion, and exact
  authenticated ACK mappings for `ok`/`collision`/`duplicate`. The focused SPL crate gate is
  green with 91 tests. U4's focused split WS-to-loopback pipe function is in review next; the
  full relay client and U5 supervisor composition remain unaccepted.
- AC-4's supervisor-generated parity corpus is committed in its own pre-cutover commit. It was
  generated from `adf2e9c5f`; the Python oracle sources are byte-identical at that commit, this
  lane base, and `c3b7cc6e5`. The static vectors prove Python-sealed → Rust-opens only because
  HPKE sealing is randomized; the reverse remains the supervisor's live differential. The
  short/long header rows are parser vectors, never socket-wire vectors. U6 may use the corpus
  when U1–U5 are accepted, but remains blocked on those orchestration units.
- The delegated U4 split WS-to-loopback pipe is accepted after fresh-eyes review. It replays
  `drain_buffer()` before forwarding, uses the transport halves concurrently, limits TCP→WS
  frames to 64 KiB, writes local EOF after a WS close, and cancels the opposite direction on
  first completion. Its 93-SPL/17-HPKE crate gate is green; the complete relay client,
  dispatch handoff, and live seam tests remain unaccepted.
- The delegated U5 service supervisor loop is accepted after fresh-eyes review. Its injected
  runtime seam covers 5-second production polling, exact posture/token startup, asymmetric
  posture read failures, the resettable one-shot missing-token notice, fatal unexpected run
  completion, and stop→abort/await cleanup plus the final Callosum hook. The actual strict SPL
  clippy command and the 101-test crate gate are green. It awaits the concrete U4 relay client
  factory and real service process wiring; neither is claimed complete.
- The delegated U4 Tokio WebSocket adapter is accepted after fresh-eyes review. It preserves
  the required token query plus bearer header, has no protocol-library size cap, splits once
  into U3/U2-compatible byte halves, preserves binary/text bytes, and reduces all connection
  errors to token-free classes. The exact strict clippy, iOS, and 103-test SPL gates are green.
- The delegated U1 process-I/O dispatch is accepted after fresh-eyes review. `spl hpke` now
  uses the frozen u32be framed request/response protocol over standard I/O with a 480 MiB total
  input cap plus one-byte overflow refusal, class-only stderr failures, and no key material on
  argv. `spl service` intentionally remains a fixed `spl: unavailable` / exit-69 interim path:
  it is not service composition and must never be treated as cutover completion.
- Checkpoint gates after the correction-driven units: `cargo fmt --all -- --check`, strict
  combined clippy, and combined locked tests are green (85 SPL + 12 HPKE); both HPKE and SPL
  libraries pass the explicit `aarch64-apple-ios` check without an exclusion; `cargo deny`
  and the Rust 1.95 locked check are green. Final gate evidence must be rerun after the
  remaining orchestration work.

## Instrumentation

### Rejections

- **U3 direct draft — rejected.** It was written by the lane owner before the supervisor's
  correction. The code had focused green tests, but it generated none of the experiment's
  delegated-authoring evidence. This is a process/brief defect, not a claim that the behaviour
  was wrong. Delegated rewrites now replace it.
- **U4 relay control — returned.** The first review found an error-classification gap; the
  revision then failed the first compiled integration gate because its string and numeric
  branches returned different types. The writer is repairing the typed return rather than the
  lane owner bypassing the delegated unit. The repaired return passed 41 combined tests and
  lint. This is a detector firing as intended.
- **U1 CLI parser — returned.** The first parser return correctly protected HPKE secrets from
  argv but omitted the existing service `-v`/`-d` surface required by C8. This was a brief
  omission caught against `service.py`; the corrected parser passed 27 focused tests.
- **U5 state/token reader — returned.** The first return read `tokens/account.json` directly
  beneath the journal instead of the frozen `link/tokens/account.json` location. The writer is
  correcting the on-disk path; its next return also needed Python's primary-token truthiness
  before legacy fallback. Integration then exposed a direct unused import and test-only
  `assert!(false)` arms under the crate's no-panic clippy gate. This is a contract-review
  catch plus a brief defect: the initial unit brief did not require the crate gate.
- **U2 split write half — returned.** The first return used public `async fn` trait methods
  verbatim from the frozen illustrative signature. Strict clippy rejected the implicit future
  bounds. The return must match U3's public `impl Future + Send` seam with no lint waiver.
  This is a lane-brief omission caught by review, not supervisor contract rework.
- **U1 HPKE CLI framing — returned.** The first return used standalone formatting that did not
  follow workspace import ordering. The workspace formatter and strict crate gates are required
  before acceptance; this is a lane-brief omission, not contract rework.
- **U2 blob receive — returned.** The first receiver return correctly covered its refusal
  shapes and admission release, but did not exercise an authenticated, archive-valid accepted
  transfer or either observable ACK status. Fresh-eyes review caught the omission; the delegate
  added an auth-mode HPKE end-to-end test for `ok`/`collision` → `ACK(0x00)` and
  `duplicate` → `ACK(0x01)`. This is a delegate test-coverage omission, not contract rework.
- **U1 HPKE process I/O — returned.** The first runner used an unbounded `read_to_end`, which
  could exhaust process memory before the field parser applied its cap. Fresh-eyes review
  returned it; the delegate replaced it with bounded chunked reads and an `EndlessReader` test
  proving it refuses the first byte beyond the aggregate limit. This is a delegate security
  correction, not contract rework.

### Explained twice

**HPKE dependency shape** had to be explained twice: the base-mode writer first stopped on an
invalid `hpke` feature set, then on mismatched `rand_core` feature/direct `p256` types. The
corrected brief is `hpke` `alloc` + `nistp` + `aes` + `getrandom`, plus direct `p256` 0.14 for
the seam's PKCS#8/SPKI boundary. This was a brief defect, not code rework.

### Frozen-contract ambiguities

- **HPKE crate feature/dependency shape:** the plan's availability claim named `hpke = 0.14.0`
  but did not state its feature names or the incompatible `p256`/`rand_core` major versions.
  The delegated base-mode writer stopped before coding and reported the exact compile failure.
  The workspace dependency is corrected to the crate's `alloc`/`nistp`/`aes`/`getrandom`
  feature set plus matching direct `p256`; this is a brief defect, not an interface-contract
  change.
- **HPKE subcommand field framing — resolved by supervisor:** u32be-length fields, a 96 MiB
  cap, exact five/four input fields and one/two output fields, plus a one-line fixed error
  class with no stdout. This was a contract defect, not a delegate rejection.
- **WebSocket progress zero values:** Python raises `ValueError` for `window_s <= 0` and allows
  a zero byte minimum; the Rust frozen error vocabulary contains only timeout/closed cases. The
  delegated buffer unit proposed `ProgressTimeout` for a zero window and bounded-read semantics
  for a zero minimum. This is held rather than silently accepted.
- **U2/U3 WebSocket ownership — resolved by supervisor:** the transport splits into distinct
  read and write halves. `BufferedWsReader<R>` retains its accepted read-only ownership and U2
  receives a sibling `WsByteSink` for `READY`/`ACK`/close. This was a contract defect, not a
  delegate rejection.
- **U3 source-close taxonomy:** `WsByteSource` exposes one `WsClosed` outcome for both clean
  EOF and a source-side read failure. U4's split pipe can therefore preserve the shared
  close-to-local-EOF behavior but cannot classify those two causes without changing an accepted
  U3 seam. This is logged rather than silently expanded; no owner-visible behavior requires a
  distinction today.
- **U4↔U2 blob dependencies — unresolved frozen seam:** `receive_blob` correctly requires a
  ledger, home upload private key, and observer-ingest implementation through `BlobDeps`, but
  the frozen `RelayClientConfig`/constructor lists no journal root, upload-key source, or ingest
  seam. U4 cannot truthfully dispatch `SBO1` without silently extending that constructor or
  guessing file/process ownership. The transport adapter is accepted; full relay-client dispatch
  is stopped at this explicit contract gap pending supervisor direction.

### Contract rework

- **U2 blob receive boundary:** scope-check correction #2 added the missing U2 contract and
  fixed the unit numbering. The earlier blob-frame placement return was caused by this frozen
  contract defect, not by a delegate failure; the corrected HPKE-crate implementation remains
  accepted and is excluded from the delegate-rejection count.
- **U4/U5 shutdown boundary:** the corrected observable contract is no live relay socket once
  the supervisor stops the client. `RelayClient::stop()` alone is insufficient; the service
  must cancel the listen-run task. This is pending implementation and seam A7 validation.
- **Fixtures:** AC-4 corpus generation and its provenance commit are supervisor-owned because
  this worktree has no Python fixture environment. The lane will not fabricate or block on it.
- **U3 buffered-wire reads:** scope-check correction #2's no-direct-slice rule also reached
  `peek` and `peek_bounded`. Their checked-range follow-up is accepted after seven focused
  buffer tests; it preserves U3 behavior and is contract rework, not a rejection.
- **U2 split transport and U1 CLI framing:** the supervisor rewrote both frozen sections after
  the lane stopped rather than inventing an escape hatch or wire format. Both are contract
  rework (ledger row 3), not delegate rejections; the U4 concurrent pipe makes the split
  independently necessary.

### Surgical direct fixes

- **Shared HPKE error enum:** added `InvalidSenderPublicKey` to the U1 base-mode error type so
  the delegated auth-mode unit can preserve its distinct sender-SPKI failure class. This spans
  the U1 base/auth function units and is an integration correction, not a behaviour change.
