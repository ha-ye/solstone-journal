# Release Evidence Contract Maintenance

Scope: the retained candidate ledger at `target/release-evidence/<version>/ledger.json`,
written and validated by `scripts/release_ledger.py`. Read this before changing
the ledger's top-level shape.

## Why this doc exists

A retained release candidate is meant to stay publishable after it is cut. The
candidate rail finalizes and validates it, and transparency publication is a
separate retryable step that runs afterward and never gates delivery. That
promise holds only while this repository's own reader still accepts the retained
bytes.

On 2026-07-27 two retained candidates, 1.0.16 and 1.0.17, failed recovery at
publish time. A commit merged after both were cut added a required top-level
ledger key that neither could carry, so both had to be published from worktrees
checked out at their own release tags. Nothing about either candidate had
changed; the reader had.

## The contract is a single symbol

`TOP_LEVEL_KEYS` in `scripts/release_ledger.py` is one frozenset used in two
directions:

- the writer asserts its own output equals it (`ledger top-level key set drifted`)
- the reader asserts a retained ledger equals it (`retained ledger top-level key
  set is invalid`)

Both checks are strict set equality, so a missing key and an unexpected extra key
both fail closed.

The consequence is the trap. Writer and reader always move in the same commit, so
any change to the key set is self-consistent by construction and the test suite
stays green. **The only thing such a change can break is a candidate retained
before it — and the suite holds none, because fixtures are regenerated from the
current writer.** A green `make ci` is not evidence of retained-candidate
compatibility, and no existing check will tell you otherwise.

## `schema_version` is not a compatibility mechanism today

`schema_version` is a member of `TOP_LEVEL_KEYS` and the writer emits the literal
`1`. Nothing in this repository branches on it: every occurrence across the
release scripts either writes `1` or asserts equality with `1`. It records a
version, it does not negotiate one.

So bumping it is its own coordinated change across every reader that pins the
value, not a free escape hatch. Grep for `schema_version` before assuming a bump
is local.

## Changing the top-level shape

Adding or removing a `TOP_LEVEL_KEYS` member is a **breaking change to
already-retained candidates**, not an additive one. Pick one of these and state
which in the change description:

1. **Version and tolerate.** Bump `schema_version` and make the reader accept the
   previous version, treating the new key or keys as absent. Every receipt
   validator that pins the old value must move in the same change.
2. **Declare the break.** State that candidates retained before the change can no
   longer be published and must be re-cut. This is only defensible when no
   unpublished retained candidate exists — enumerate `target/release-evidence/`
   and confirm every retained version already appears in the public chain before
   relying on it.

Either way, publish evidence for every outstanding retained candidate **before**
merging the change. An unpublished retained candidate is the thing at risk, and
it carries no expiry warning of its own.

If you find yourself needing to publish a candidate whose retained ledger the
current reader rejects, the recovery is to run the publisher from a worktree
checked out at that release's own tag, with the retained candidate and evidence
copied in. Historical validation is release-bound by design: the schema,
exception set, and asset classifier that apply are the ones current at the
manifest's `source_commit`, not the ones on the default branch today.

## The test that makes this visible

Keep at least one **frozen** retained ledger fixture: committed bytes captured
before the current key set, never regenerated, which the reader must still
accept. A fixture produced by the current writer cannot fail this way, so it
proves nothing here.

When option 1 is taken, that frozen fixture is the regression test. When option 2
is taken, replace it in the same change and say why.

## Related

- `AGENTS.md` § Release rail — what `--candidate` and `--recover` do, and the
  transparency publication step
- `docs/PORTING.md` § Release candidate rail — the same rail summarized alongside
  the porting gates
- `docs/journal-format-contract-maintenance.md` — the sibling discipline for
  journal at-rest formats, which has a version-negotiation story of its own
