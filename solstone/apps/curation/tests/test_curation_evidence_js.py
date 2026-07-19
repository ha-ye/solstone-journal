# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CURATION_EVIDENCE_JS = (
    REPO_ROOT / "solstone" / "apps" / "curation" / "static" / "curation_evidence.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_curation_evidence_drawer_props() -> None:
    script = textwrap.dedent(
        """
        const assert = require('assert');
        const evidence = require(process.argv[1]);

        assert.strictEqual(
          evidence.buildEvidenceDrawerProps({
            kind: 'facet_candidate',
            key: 'empty',
            evidence: { samples: [] },
          }),
          null,
        );

        const one = evidence.buildEvidenceDrawerProps({
          kind: 'facet_candidate',
          key: 'focus',
          evidence: {
            samples: [
              { day: '20260701', stream: 'archon', segment: '090000_300' },
            ],
          },
        });
        assert.strictEqual(one.id, 'curation-evidence:facet_candidate:focus');
        assert.strictEqual(one.open, false);
        assert.strictEqual(one.label, 'evidence');
        assert.strictEqual(one.line, '1 piece');
        assert(one.bodyHtml.includes('<ul class="drawer-evidence">'));
        assert(one.bodyHtml.includes('20260701'));
        assert(one.bodyHtml.includes('archon · 090000_300'));

        const counted = evidence.buildEvidenceDrawerProps({
          kind: 'facet_candidate',
          key: 'counted',
          evidence: {
            count: 3,
            samples: [
              { day: '20260701', stream: 'archon', segment: '090000_300' },
            ],
          },
        });
        assert.strictEqual(counted.line, '1 of 3');

        const two = evidence.buildEvidenceDrawerProps({
          kind: 'entity_candidate',
          key: 'kognova',
          evidence: {
            samples: [
              { day: '20260701', stream: 'archon', segment: '090000_300' },
              { day: '20260702', stream: 'watch', segment_key: '100000_300' },
            ],
          },
        });
        assert.strictEqual(two.id, 'curation-evidence:entity_candidate:kognova');
        assert.strictEqual(two.line, '2 pieces');
        assert(two.bodyHtml.includes('watch · 100000_300'));

        const capped = evidence.buildEvidenceDrawerProps({
          kind: 'facet_candidate',
          key: 'many',
          evidence: {
            samples: [
              { day: '20260701' },
              { day: '20260702' },
              { day: '20260703' },
            ],
          },
        }, { maxSamples: 2, open: true });
        assert.strictEqual(capped.open, true);
        assert.strictEqual(capped.line, '2 of 3');
        assert(capped.bodyHtml.includes('20260701'));
        assert(capped.bodyHtml.includes('20260702'));
        assert(!capped.bodyHtml.includes('20260703'));

        const escaped = evidence.buildEvidenceDrawerProps({
          kind: 'facet_candidate',
          key: 'escape',
          evidence: {
            samples: [
              { day: '<day&>', stream: 'a"b', segment: "c'd" },
            ],
          },
        });
        assert(escaped.bodyHtml.includes('&lt;day&amp;&gt;'));
        assert(escaped.bodyHtml.includes('a&quot;b · c&#39;d'));
      """
    )
    result = subprocess.run(
        ["node", "-e", script, str(CURATION_EVIDENCE_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
