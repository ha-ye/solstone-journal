# Speaker Edge Differential Corpus

This tree is the speaker-family edge corpus for Python-to-Rust indexer
differentials. It intentionally emits real `spoke-with` and `mentioned` rows,
including positive mentions by entity `name` and `aka`, blocked/self/unresolved
negative mentions, Unicode word-boundary negatives, and `.npz` stem precedence.

After building the native debug binary, run the functional differential with:

```bash
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/solstone-speaker-edge-diff.XXXXXX")
.venv/bin/python tests/verify_indexer_differential.py \
  --journal tests/fixtures/edges_speaker_journal \
  --a ".venv/bin/journal indexer --rescan-full" \
  --b "core/target/debug/solstone-core indexer --rescan-full" \
  --work-dir "$WORK_DIR" \
  --mode functional \
  --copy-mode full
```

Functional mode compares edge triples plus directed/weight at the generic edge
overlap threshold. It does not compare `source`, `path`, `anchor`, `label`,
names, `day`, `facet`, or `ts`.

For this corpus, speaker-family review also requires exact full-row parity for
`source IN ('speaker', 'mention')`. After the functional run above, compare those
rows explicitly with:

```bash
WORK_DIR="$WORK_DIR" .venv/bin/python - <<'PY'
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

columns = [
    "src",
    "dst",
    "kind",
    "directed",
    "src_name",
    "dst_name",
    "day",
    "facet",
    "source",
    "path",
    "anchor",
    "label",
    "ts",
    "weight",
]
work_dir = Path(os.environ["WORK_DIR"])
query = (
    f"SELECT {', '.join(columns)} FROM edges "
    "WHERE source IN ('speaker', 'mention') "
    f"ORDER BY {', '.join(columns)}"
)


def rows(side: str):
    db = work_dir / side / "journal" / "indexer" / "journal.sqlite"
    with sqlite3.connect(db) as conn:
        return conn.execute(query).fetchall()


left = rows("left")
right = rows("right")
print(f"left rows: {len(left)} {dict(Counter(row[8] for row in left))}")
print(f"right rows: {len(right)} {dict(Counter(row[8] for row in right))}")
print(f"full-row parity: {left == right}")
if left != right:
    print("only left:")
    for row in sorted(set(left) - set(right)):
        print(row)
    print("only right:")
    for row in sorted(set(right) - set(left)):
        print(row)
    sys.exit(1)
PY
```
