# Entities Detection Activation

Activate the dormant `entities:detection` segment talent as best-effort work: it
is dispatched for active changed segments, but its missing or failed completion
must not block segment/day completion.

## Decisions

### X1 - Non-gating representation

Add a module-level constant in `solstone/think/pipeline_health.py` immediately
after `SEGMENT_FLOOR_TALENTS` (:35), mirroring its type:

```python
SEGMENT_NONGATING_TALENTS: tuple[str, ...] = ("entities:detection",)
```

The one-line edit to the `segment_fully_thought` dispatched loop (:795-797)
becomes:

```python
for name in sorted(progress.dispatched):
    if name in SEGMENT_NONGATING_TALENTS:
        continue
    if name not in progress.completed:
        return False, f"dispatched:{name}"
```

Rationale: this is the SINGLE exemption seam. No other site
(`classify_segment_completion`, backlog why-axis, badge, daily.updated gate)
needs editing because those readers already consult this verdict. The backlog
why-axis must not special-case detection separately or it would diverge from
the completion verdict.

### X2 - Dispatch insertion

In `run_segment_sense` (`solstone/think/thinking.py`), insert an unconditional
add for `entities:detection` RIGHT AFTER the `timeline:segment_summary` block
(after :1191) and BEFORE the conditional `screen` block (:1194). Mirror the
segment_summary no_config pattern exactly, including the stream kwarg:

```python
detection_name = "entities:detection"
detection_config = _cfg(detection_name)
if detection_config:
    agents_to_run.append((detection_name, detection_config))
else:
    _log_skip(
        detection_name,
        "no_config",
        f"{detection_name} config not found",
        mode=target_schedule,
        day=day,
        segment=segment,
        **({"stream": stream} if stream else {}),
    )
```

Rationale: this gives a stable dispatch order:
floors -> `timeline:segment_summary` -> `entities:detection` ->
conditional `screen` / `speaker_attribution`.

### X3 - Disjointness guard

Add a test-only invariant assertion, not a runtime assert:
`set(SEGMENT_NONGATING_TALENTS).isdisjoint(SEGMENT_FLOOR_TALENTS)`.

Place it in `tests/test_segment_completion.py` near the pure
`segment_fully_thought` tests. That file already imports and exercises the
segment-completion contract; the invariant protects exactly that contract.

### X4 - Failure stays diagnosable

Do not use backlog as the diagnostic target for `entities:detection` failures.
After X1, `segment_fully_thought` returns `(True, None)` for detection-only
non-completion, and `_segment_backlog_units` intentionally skips ok verdicts.
Expecting a backlog `WHY_FAILED` unit would conflict with AC9.

At the pipeline-health/fold seam, add a `tests/test_pipeline_health.py` test
that builds a segment with completed floor talents plus
`talent.dispatch`/`talent.fail` for `entities:detection`. Assert:

- `segment_fully_thought(lookup_segment_progress(...)) == (True, None)`.
- `read_terminal_states(day)[TerminalUnit(mode="segment", name="entities:detection", stream="default", segment=segment, ...)].latest_event == "fail"` and preserves `reason_code` / `provider` / `model`.
- `summarize_pipeline_day(day)["talents"]["failed_list"]` and anomalies still include the `entities:detection` failure.

At the hook seam, do not add duplicate coverage. Existing
`solstone/apps/entities/tests/test_detection.py::test_post_process_records_substrate_failure`
already monkeypatches `upsert_detection_segment` to raise and asserts
`detection_outcome.json` has `errored == 1` and the error string.

## Acceptance Criteria to Tests

| AC | Test target | Mirror / assertion |
|---|---|---|
| AC1 dispatch-always | New `tests/test_think_segment.py` parametrized test for `live=False` and `live=True` | Add `entities:detection` to `_segment_configs`; mirror `test_conditional_screen_dispatch`; expose `sense`, floors, timeline, detection; assert exact order `sense`, floors, `timeline:segment_summary`, `entities:detection`. |
| AC2 reconcile-only-with-candidates / self-skip-no-outcome | Existing `solstone/apps/entities/tests/test_detection.py::test_pre_process_skip_taxonomy` plus optional thin assertion | Existing test covers `no_candidates` self-skip. Mirror is imperfect for "no outcome": it does not currently assert `detection_outcome.json` absence. Add only if AC requires explicit file absence; no AC should assert upsert on an entity-less segment. |
| AC3 not-a-floor + disjointness | New `tests/test_segment_completion.py` invariant test | Assert `SEGMENT_NONGATING_TALENTS` is disjoint from `SEGMENT_FLOOR_TALENTS`. |
| AC4 failure-does-not-peg + stream-tagged variant | New `tests/test_segment_completion.py` tests | Mirror `test_segment_fully_thought_requires_dispatched_completion` and `test_stream_keyed_dispatch_blocks_without_terminal`, replacing `screen` with `entities:detection` and asserting `(True, None)`. |
| AC5 forward-only no retroactive dirtying | Existing `tests/test_segment_completion.py::test_segment_fully_thought_does_not_require_rolling_talents` | Existing complete floor-talents progress has no detection dispatch and already asserts `(True, None)`. Add a name-specific test only if reviewers want explicit detection wording. |
| AC6 settled-history regression | New `tests/test_segment_completion.py` classification test | Mirror `test_dropped_empty_modality_segment_is_not_counted`: seed a complete pre-activation segment with no detection dispatch; assert `classify_segment_completion(...).blockers == []` and `not_thought == 0`. Do not test marker touch here. |
| AC7 failure-stays-diagnosable | New `tests/test_pipeline_health.py` plus existing hook test | Use X4: verdict ok, `read_terminal_states` exposes fail, `summarize_pipeline_day` failed list/anomaly exposes fail; hook telemetry already covered by `test_post_process_records_substrate_failure`. |
| AC8 redundant/idle unchanged | Existing `tests/test_think_segment.py::test_idle_segment_returns_early` and `test_redundant_skips_writeups_and_writes_continuation` | Both assert only `sense` spawns. X2 insertion is after idle/redundant returns, so no expected dispatch change. |
| AC9 backlog absence | New `tests/test_pipeline_health.py` and, if strict, `tests/test_segment_completion.py` | Mirror `test_read_backlog_view_dispatch_without_terminal_is_pending_not_in_progress`, replacing `screen` with `entities:detection`; assert backlog day is `complete` with empty `why`. For `read_segment_backlog`, assert per-day blockers stay empty and `not_thought == 0`. |

AC10 baselines and AC11 green tree are validation steps, not unit tests:
verify the three known baselines do not change, then run the focused tests and
the normal project verification target.

## Flags

- AC2's existing mirror proves the pre-hook self-skip but not explicit absence
  of `detection_outcome.json`; add one thin assertion only if the AC needs that
  literal file guarantee.
- AC7 and AC9 intentionally split diagnostics from backlog: detection failures
  remain visible through terminal/pipeline summary telemetry, while backlog
  remains absent because detection is non-gating.
