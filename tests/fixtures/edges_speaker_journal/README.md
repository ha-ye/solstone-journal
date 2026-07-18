# Speaker Edge Differential Corpus

This tree is the speaker-family edge corpus for Python-to-Rust indexer
differentials. It intentionally emits real `spoke-with` and `mentioned` rows,
including positive mentions by entity `name` and `aka`, blocked/self/unresolved
negative mentions, Unicode word-boundary negatives, and `.npz` stem precedence.

After building the native debug binary, run it with:

```bash
.venv/bin/python tests/verify_indexer_differential.py \
  --journal tests/fixtures/edges_speaker_journal \
  --a ".venv/bin/journal indexer --rescan-full" \
  --b "core/target/debug/solstone-core indexer --rescan-full" \
  --mode functional \
  --copy-mode full
```

For this corpus, speaker-family review is exact full-row parity for
`source IN ('speaker', 'mention')`; do not treat the generic 0.95 edge-overlap
gate as sufficient coverage for these rows.
