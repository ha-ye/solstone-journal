# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
TIMELINE_PROVENANCE_JS = (
    REPO_ROOT / "solstone" / "apps" / "timeline" / "static" / "timeline_provenance.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_timeline_provenance_formatter() -> None:
    script = textwrap.dedent(
        """
        const assert = require('assert');

        global.relativeTime = (ms) => {
          const minutes = Math.floor(ms / 60000);
          if (minutes < 60) return `${minutes} minutes`;
          const hours = Math.floor(minutes / 60);
          if (hours < 24) return `${hours} hours`;
          const days = Math.floor(hours / 24);
          return `${days} days`;
        };

        const provenance = require(process.argv[1]);

        function pad2(value) {
          return String(value).padStart(2, '0');
        }

        function expectedTitle(generatedAt) {
          const date = new Date(generatedAt * 1000);
          const hh = pad2(date.getHours());
          const mm = pad2(date.getMinutes());
          const y = date.getFullYear();
          const mo = pad2(date.getMonth() + 1);
          const da = pad2(date.getDate());
          return `rolled up at ${hh}:${mm} on ${y}-${mo}-${da}`;
        }

        function assertRendered(generatedAt, nowMs, model, expectedText) {
          const html = provenance.renderDayProvenance(generatedAt, model, nowMs);
          assert(html.includes(`>${expectedText}</p>`));
          assert(html.includes(`title="${expectedTitle(generatedAt)}"`));
          const title = html.match(/title="([^"]+)"/)[1];
          assert(/^rolled up at \\d{2}:\\d{2} on \\d{4}-\\d{2}-\\d{2}$/.test(title));
        }

        const generatedAt = 1770033600;
        assert.strictEqual(provenance.renderDayProvenance(null, 'model'), '');
        assert.strictEqual(provenance.renderDayProvenance(generatedAt, ''), '');

        assertRendered(
          generatedAt,
          generatedAt * 1000 + 45 * 60 * 1000,
          'test-model',
          'rolled up 45 minutes ago · test-model',
        );
        assertRendered(
          generatedAt,
          generatedAt * 1000 + 3 * 60 * 60 * 1000,
          'test-model',
          'rolled up 3 hours ago · test-model',
        );
        assertRendered(
          generatedAt,
          generatedAt * 1000 + 2 * 24 * 60 * 60 * 1000,
          'test-model',
          'rolled up 2 days ago · test-model',
        );

        const escaped = provenance.renderDayProvenance(
          generatedAt,
          '<model&>',
          generatedAt * 1000 + 45 * 60 * 1000,
        );
        assert(escaped.includes('rolled up 45 minutes ago · &lt;model&amp;&gt;'));
      """
    )
    result = subprocess.run(
        ["node", "-e", script, str(TIMELINE_PROVENANCE_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
