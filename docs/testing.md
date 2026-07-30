# Testing

## Test Structure

- **Framework**: pytest; coverage reporting comes from `make test-cov`, `make verify`, or `make coverage`, not `make test` or `make ci`
- **Unit Tests**: live under `tests/` (and each app's `tests/` dir)
  - Fast, with mocked process/thread/clock/network/repository boundaries
  - No external API calls, real browser, heavyweight build, or shared fixture writes
  - Read `tests/fixtures/journal/` mock data; use `journal_copy` or `tmp_path`
    for any scan, index rebuild, or mutation
  - Test individual functions and modules
- **Integration Tests**: marked `@pytest.mark.integration`
  - Opt-in via `make test-integration`; excluded from `make test` and `make ci`
  - Real local processes/builds and persisted-index contracts
  - Still use disposable `tmp_path` state and never the owner's journal
- **Release Tests**: marked `@pytest.mark.release`
  - Run serially via `make test-release`; excluded from development `make test`
    and `make ci`
  - Cover release transactions, release entrypoints, packaging/install probes,
    and release-host tool contracts
  - `make release-checks` combines them with the real advisory-policy liveness
    and minisign gates; candidate construction runs that target automatically
- **Naming**: Files `test_*.py`, functions `test_*`
- **Fixtures**: Shared fixtures in `tests/conftest.py`

## Fixture Journal

```python
# The autouse set_test_journal_path fixture in tests/conftest.py does this
# for unit tests. Set it explicitly only when a test needs a different journal.
os.environ["SOLSTONE_JOURNAL"] = "tests/fixtures/journal"
# Now all journal operations work with test data
```

The `tests/fixtures/journal/` directory contains immutable mock input with sample
facets, agents, transcripts, and indexed data. Tests may read it directly. Any
test that writes, scans, or rebuilds journal/index state must use the
`journal_copy` fixture or a smaller journal under `tmp_path`.

## Running Tests

- `make test` runs all unit tests — `tests/` + every `solstone/apps/*/tests/`, in one parallel run
- `make test-cov` — the same suite with coverage reporting
- `make test-integration` — opt-in real local build/process and persisted-index contracts
- `make test-release` — serial release transactions and release-host probes
- `make release-checks` — complete candidate-host validation, including the
  release tests, advisory-policy liveness, and real minisign signing
- `make test-app APP=<name>` and `make test-only TEST=path` are the focused development loop
- `make coverage` to generate a coverage report
- `make ci` once on the settled final tree before merge or release (install checks plus the full unit suite)
- Always run `journal restart-convey` after editing `solstone/convey/` or `solstone/apps/` to reload code

## OpenAPI Verification Lanes

There are two API referees with different jobs:

- `tests/test_openapi_schemathesis.py` fuzzes a small allowlist from the committed
  native-client contract at `docs/openapi/convey-clients.json` against the Flask WSGI
  app. It runs with the normal unit suite because it uses an isolated tmp journal and
  does not use HTTP transport sockets.
- `make verify-api` checks SPA/API response baselines against a running sandbox.
  That lane verifies rendered baseline behavior, not the native-client OpenAPI
  contract.

Run the Schemathesis lane directly with:

```bash
make test-only TEST=tests/test_openapi_schemathesis.py
```

The operator live lane is:

```bash
make verify-schemathesis
```

`verify-schemathesis` starts a disposable sandbox, sets
`SOLSTONE_SCHEMATHESIS_LIVE=1`, and resolves the base URL through
`solstone.think.convey_client.resolve_base_url()`. Set
`SOLSTONE_SCHEMATHESIS_BASE_URL` to override the target. The live lane may grow to
include mutating routes because it targets disposable instances; do not run it as part
of ordinary unit CI.

The WSGI lane uses Schemathesis' default checks, but pins input generation to positive
cases only. That means checks such as status-code conformance, content-type
conformance, response headers, response schema, server-error rejection, and ignored-auth
behavior still run, while `negative_data_rejection` has no generated negative inputs in
this lane. The positive-only setting keeps the floor assertions meaningful: every
generated floor case must return 2xx.

Known findings are reported by this lane and triaged separately; the lane must not
auto-fix them or widen the contract to pass. A contract-vs-implementation mismatch is
triaged by asking which side is wrong. Current recorded findings:

- `GET /app/network/api/status` materializes link CA key material on a read path:
  `ca_dir().exists()` first creates `journal/link/ca`, then `_ca_fingerprint()` calls
  `load_or_generate_ca()` and writes `private.pem` plus `cert.pem`.
- `GET /app/network/api/status` reaches `_detect_lan_ip()`, which opens a UDP socket to
  `8.8.8.8:80` to read the kernel-selected source address. The WSGI test harness reaches
  that syscall.

## Worktree Development

Run the full stack (supervisor + callosum + sense + cortex + convey) against test fixture data:

```bash
make dev                    # Start stack (Ctrl+C to stop)
```

In a second terminal, hit endpoints:

```bash
export SOLSTONE_JOURNAL=tests/fixtures/journal
export PATH=$(pwd)/.venv/bin:$PATH
curl -s http://localhost:$(cat tests/fixtures/journal/health/convey.port)/
```

Notes:

- Agents won't execute without API keys — this is expected in worktrees
- Output artifacts go in `scratch/` (git-ignored)
- Service logs: `tests/fixtures/journal/health/<service>.log`
- `make dev` writes runtime artifacts (stats cache, health logs, task logs) into the fixtures journal — these are covered by `tests/fixtures/journal/.gitignore` and should never be committed
