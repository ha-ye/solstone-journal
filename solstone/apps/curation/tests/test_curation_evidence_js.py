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

        function anchorHrefs(html) {
          return Array.from(html.matchAll(/<a [^>]*href="([^"]+)"/g)).map((match) => match[1]);
        }

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
            count: 1,
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
        assert.deepStrictEqual(anchorHrefs(one.bodyHtml), [
          '/app/timeline/20260701',
          '/app/transcripts/20260701#090000_300',
        ]);
        assert(one.bodyHtml.includes('>20260701</a>'));
        assert(one.bodyHtml.includes('>090000_300</a>'));
        assert(one.bodyHtml.includes('<span class="ev-meta">archon</span>'));
        assert(!/<a [^>]*>archon<\\/a>/.test(one.bodyHtml));

        const counted = evidence.buildEvidenceDrawerProps({
          kind: 'facet_candidate',
          key: 'counted',
          evidence: {
            count: 5,
            samples: [
              { day: '20260701', stream: 'archon', segment: '090000_300' },
              { day: '20260702', stream: 'archon', segment: '100000_300' },
              { day: '20260703', stream: 'archon', segment: '110000_300' },
            ],
          },
        });
        assert.strictEqual(counted.line, '3 of 5');

        const two = evidence.buildEvidenceDrawerProps({
          kind: 'entity_candidate',
          key: 'kognova',
          evidence: {
            count: 2,
            samples: [
              { day: '20260701', stream: 'archon', segment: '090000_300' },
              { day: '20260702', stream: 'archon', segment: '100000_300' },
            ],
          },
        });
        assert.strictEqual(two.id, 'curation-evidence:entity_candidate:kognova');
        assert.strictEqual(two.line, '2 pieces');
        assert(two.bodyHtml.includes('/app/transcripts/20260702#100000_300'));

        const missingSegment = evidence.buildEvidenceDrawerProps({
          kind: 'facet_candidate',
          key: 'missing-segment',
          evidence: {
            count: 1,
            samples: [
              { day: '20260701', stream: 'archon' },
            ],
          },
        });
        assert.deepStrictEqual(anchorHrefs(missingSegment.bodyHtml), []);
        assert(missingSegment.bodyHtml.includes('20260701 · archon'));

        const missingDay = evidence.buildEvidenceDrawerProps({
          kind: 'facet_candidate',
          key: 'missing-day',
          evidence: {
            count: 1,
            samples: [
              { stream: 'archon', segment: '090000_300' },
            ],
          },
        });
        assert.deepStrictEqual(anchorHrefs(missingDay.bodyHtml), []);
        assert(missingDay.bodyHtml.includes('archon · 090000_300'));

        const escaped = evidence.buildEvidenceDrawerProps({
          kind: 'facet_candidate',
          key: 'escape',
          evidence: {
            count: 1,
            samples: [
              { day: '2026<"0701', stream: 'a"b', segment: '09<"00' },
            ],
          },
        });
        assert(escaped.bodyHtml.includes('href="/app/timeline/2026&lt;&quot;0701"'));
        assert(escaped.bodyHtml.includes('>2026&lt;&quot;0701</a>'));
        assert(escaped.bodyHtml.includes('href="/app/transcripts/2026&lt;&quot;0701#09&lt;&quot;00"'));
        assert(escaped.bodyHtml.includes('>09&lt;&quot;00</a>'));
        assert(escaped.bodyHtml.includes('<span class="ev-meta">a&quot;b</span>'));
      """
    )
    result = subprocess.run(
        ["node", "-e", script, str(CURATION_EVIDENCE_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
