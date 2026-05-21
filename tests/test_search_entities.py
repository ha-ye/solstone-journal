# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import pytest

from solstone.think.indexer.journal import (
    scan_journal,
    search_entities,
)


@pytest.fixture(autouse=True)
def indexed_journal(journal_copy):
    scan_journal(str(journal_copy), full=True)


class TestSearchEntities:
    def test_by_type_person(self):
        results = search_entities(entity_type="Person")
        assert isinstance(results, list)
        for r in results:
            assert r["type"] == "Person"

    def test_by_type_company(self):
        results = search_entities(entity_type="Company")
        for r in results:
            assert r["type"] == "Company"

    def test_by_facet(self):
        results = search_entities(facet="work")
        assert isinstance(results, list)

    def test_by_query(self):
        results = search_entities(query="Alice")
        assert isinstance(results, list)
        assert any(r["name"] in {"Alice Johnson", "Alice"} for r in results)

    def test_all_entities(self):
        results = search_entities()
        assert isinstance(results, list)
        assert len(results) > 0

    def test_result_structure(self):
        results = search_entities()
        if results:
            r = results[0]
            assert "entity_id" in r
            assert "name" in r
            assert "type" in r

    def test_limit(self):
        results = search_entities(limit=3)
        assert len(results) <= 3
