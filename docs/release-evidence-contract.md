# Release Evidence Contract Maintenance

Scope: the retained candidate ledger at `target/release-evidence/<version>/ledger.json`,
written and validated by `scripts/release_ledger.py`. Read this before changing
the ledger's registered shape.

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

## The contract is a versioned registry

`LEDGER_SCHEMA_REGISTRY` in `scripts/release_ledger.py` owns the retained ledger
shape by `schema_version`. Each registered version declares one whole shape:
the top-level key set plus the `models`, `nvattest`, and `policy_run` sub-key
sets.

- the writer stamps `CURRENT_LEDGER_SCHEMA_VERSION` and asserts its own output
  equals that version's registry entry (`ledger top-level key set drifted`)
- the reader resolves the retained ledger's `schema_version`, then asserts the
  retained top-level and sub-key sets against that version's registry entry
- the reader also checks that the resolved version declares every top-level key
  required by the current consumer path

Both checks are strict set equality, so a missing key and an unexpected extra key
both fail closed.

The old trap was that writer and reader moved in the same commit, so any key-set
change was self-consistent by construction. **The only thing such a change broke
was a candidate retained before it.** That is now guarded in two places: a test
asserts each registered version's declared shape against a literal enumeration,
and a committed frozen fixture proves current code still accepts a real retained
version-1 ledger. A green `make ci` is still not evidence that a specific
retained candidate on disk is publishable: the guards catch edits to registered
shapes and frozen fixtures, but they do not revalidate retained bytes that are
not committed as fixtures.

The same applies to the sub-key sets validated the same way — `models`,
`nvattest`, and `policy_run`. The top level is where this first bit, but adding a
required sub-key breaks already-cut candidates identically. Everything below
covers the whole shape, not just the top level.

## `schema_version` is dispatched, not a free escape hatch

`CURRENT_LEDGER_SCHEMA_VERSION` is `1`, and version `1` is the only product
schema registered today. The reader branches on `schema_version` now, but that
does not make a bump a free escape hatch: a new value must be registered, tested,
and tolerated intentionally while the old version stays registered.

So bumping it is its own coordinated change across every reader that pins the
retained-ledger schema value. Grep for `schema_version` before assuming a bump is
local. The receipt validators in `scripts/release_install_smoke.py`,
`scripts/record_macos_native_wheel.py`, and `scripts/release_nvattest_proof.py`
pin their own artifact schemas, not the retained ledger's schema, and do not
move automatically merely because the retained ledger schema moves.

## Changing the registered shape

Adding or removing a registered retained-ledger shape member is a **breaking
change to already-retained candidates**, not an additive one. Pick one of these
and state which in the change description:

1. **Version and tolerate.** Append a new registered `schema_version`, bump the
   writer to stamp it, and make the reader accept the previous version, treating
   the new key or keys as absent. Every validator that pins the retained-ledger
   schema value must move in the same change.
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

Keep at least one **frozen** retained ledger fixture per registered
`schema_version`: committed bytes that the reader must still accept, and that are
never regenerated.

Provenance is not what makes a fixture work — freezing is. A fixture produced by
today's writer becomes a pre-change artifact the moment anyone edits the shape,
which is exactly when you need it. What defeats it is regenerating it to make a
failing test pass, so pin its digest and treat any change to those bytes as a
reviewable event rather than a mechanical fixup.

The stronger guard sits next to it: assert each registered version's declared
shape against a literal enumeration in the test. That fires on the shape edit
itself rather than on a fixture's bytes, so it can name the correct alternative —
append a new version and keep the old one registered — at the moment of the
mistake.

The fixture test has three distinct alarms. A shape-literal failure means a
registered version's declared shape changed; append a new version and keep the
old one registered. A digest failure means the frozen bytes changed; restore the
fixture instead of regenerating it. A validator failure means current code now
rejects a ledger it previously accepted; use option 1 or option 2 above.

When option 1 is taken, that frozen fixture is the regression test. When option 2
is taken, replace it in the same change and say why.

## Related

- `AGENTS.md` § Release rail — what `--candidate` and `--recover` do, and the
  transparency publication step
- `docs/PORTING.md` § Release candidate rail — the same rail summarized alongside
  the porting gates
- `docs/journal-format-contract-maintenance.md` — the sibling discipline for
  journal at-rest formats, which has a version-negotiation story of its own
