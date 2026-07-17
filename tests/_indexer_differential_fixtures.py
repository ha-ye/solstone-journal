# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Functional-oracle constants for the indexer differential harness.

The thresholds here define what "functionally equivalent" means for search
behavior rather than byte-for-byte SQLite contents:

- full-text query equivalence is measured by top-10 path-set Jaccard, with the
  strongest three hits on each side required to survive in the other side's
  top-10.
- edge equivalence is measured by `(src, dst, kind)` triple Jaccard, with exact
  kind-set and directed/weight agreement for shared triples.
- files and chunk coverage are exact set comparisons after dropping cache-only
  file watermarks and normalizing nullable metadata representation.

Each fixture case records the observed reference totals from the real fixture
index plus a rationale so changes are reviewable when fixture content changes.
"""

from __future__ import annotations

FULLTEXT_TOP10_JACCARD_MIN = 0.90
FULLTEXT_TOP_K = 10
FULLTEXT_SUBSET_K = 3
EDGE_TRIPLE_OVERLAP_MIN = 0.95
FUNCTIONAL_EMPTY_SENTINEL = ""
FUNCTIONAL_FILE_EXCLUDED_PATHS = (
    "entity_search:__count__",
    "entity_search:__mtime__",
)
INDEX_DB_EXCLUSION_RELS = (
    "indexer/journal.sqlite",
    "indexer/journal.sqlite-wal",
    "indexer/journal.sqlite-shm",
)

FULLTEXT_QUERY_CASES = (
    {
        "name": "single_term_authentication",
        "query": "authentication",
        "filters": {},
        "reference_total": 8,
        "reference_distinct_paths": 6,
        "rationale": "single-term fixture query from authentication-module content",
    },
    {
        "name": "multi_word_authentication_module",
        "query": "authentication AND module",
        "filters": {},
        "reference_total": 5,
        "reference_distinct_paths": 5,
        "rationale": "explicit AND query over known authentication module content",
    },
    {
        "name": "quoted_jwt_token",
        "query": '"JWT token"',
        "filters": {},
        "reference_total": 2,
        "reference_distinct_paths": 2,
        "rationale": "quoted phrase query over JWT token fixture content",
    },
    {
        "name": "prefix_auth",
        "query": "auth*",
        "filters": {},
        "reference_total": 14,
        "reference_distinct_paths": 11,
        "rationale": "prefix wildcard query over authentication/auth fixture terms",
    },
    {
        "name": "work_news_authentication",
        "query": "authentication",
        "filters": {"facet": "work", "agent": "news"},
        "reference_total": 2,
        "reference_distinct_paths": 1,
        "rationale": "query plus real work/news metadata filters",
    },
    {
        "name": "fastapi_20240102",
        "query": "FastAPI",
        "filters": {"day_from": "20240102", "day_to": "20240102"},
        "reference_total": 3,
        "reference_distinct_paths": 3,
        "rationale": "query plus explicit date-range filters, no temporal phrase",
    },
)

METADATA_FILTER_CASES = (
    {
        "name": "work_news_all",
        "query": "",
        "filters": {"facet": "work", "agent": "news"},
        "reference_total": 3,
        "reference_distinct_paths": 2,
        "rationale": "filter-only path-set case for real facet and agent values",
    },
    {
        "name": "work_news_authentication",
        "query": "authentication",
        "filters": {"facet": "work", "agent": "news"},
        "reference_total": 2,
        "reference_distinct_paths": 1,
        "rationale": "metadata path-set case combining query, facet, and agent",
    },
    {
        "name": "fastapi_20240102",
        "query": "FastAPI",
        "filters": {"day_from": "20240102", "day_to": "20240102"},
        "reference_total": 3,
        "reference_distinct_paths": 3,
        "rationale": "metadata path-set case for explicit date-range filters",
    },
)
