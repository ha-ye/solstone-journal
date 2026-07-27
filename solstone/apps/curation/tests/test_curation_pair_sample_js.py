# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKSPACE_HTML = REPO_ROOT / "solstone" / "apps" / "curation" / "workspace.html"


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace_start = source.index("{", start)
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(brace_start, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {"'", '"', "`"}:
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"could not extract {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_speaker_candidate_pair_sample_suppresses_negative_cluster_label() -> None:
    source = WORKSPACE_HTML.read_text(encoding="utf-8")
    functions = "\n\n".join(
        _extract_function(source, name)
        for name in ("escapeHtml", "sampleAudioHtml", "speakerCandidatePairSampleHtml")
    )
    script = (
        textwrap.dedent(
            """
            const assert = require('assert');

            global.document = {
              createElement() {
                return {
                  _text: '',
                  set textContent(value) {
                    this._text = value == null ? '' : String(value);
                  },
                  get innerHTML() {
                    return this._text
                      .replace(/&/g, '&amp;')
                      .replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;');
                  },
                };
              },
            };
            """
        )
        + "\n"
        + functions
        + "\n"
        + textwrap.dedent(
            """
            const fixture = {
              day: 'day-alpha',
              stream: 'stream-alpha',
              segment_key: 'segment-alpha',
              source: 'source-alpha',
              audio_url: '/audio/sample-alpha.wav',
            };

            function renderWith(clusterLabel) {
              const sample = { ...fixture };
              if (clusterLabel !== undefined) sample.cluster_label = clusterLabel;
              return speakerCandidatePairSampleHtml(sample);
            }

            const deleted = renderWith(undefined);
            assert(!deleted.includes('0'));
            assert(!deleted.includes('3'));

            const neg = renderWith(-1);
            const zero = renderWith(0);
            const three = renderWith(3);

            assert.strictEqual(neg, deleted);
            assert.notStrictEqual(zero, deleted);
            assert.notStrictEqual(three, deleted);
            assert.strictEqual(zero.split('0').join('#'), three.split('3').join('#'));
            """
        )
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_speaker_candidate_pair_sample_labels_nonnegative_cluster_label() -> None:
    source = WORKSPACE_HTML.read_text(encoding="utf-8")
    functions = "\n\n".join(
        _extract_function(source, name)
        for name in ("escapeHtml", "sampleAudioHtml", "speakerCandidatePairSampleHtml")
    )
    script = (
        textwrap.dedent(
            """
            const assert = require('assert');

            global.document = {
              createElement() {
                return {
                  _text: '',
                  set textContent(value) {
                    this._text = value == null ? '' : String(value);
                  },
                  get innerHTML() {
                    return this._text
                      .replace(/&/g, '&amp;')
                      .replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;');
                  },
                };
              },
            };
            """
        )
        + "\n"
        + functions
        + "\n"
        + textwrap.dedent(
            """
            const fixture = {
              day: 'day-alpha',
              stream: 'stream-alpha',
              segment_key: 'segment-alpha',
              source: 'source-alpha',
              audio_url: '/audio/sample-alpha.wav',
            };

            function renderWith(clusterLabel) {
              const sample = { ...fixture };
              if (clusterLabel !== undefined) sample.cluster_label = clusterLabel;
              return speakerCandidatePairSampleHtml(sample);
            }

            const deleted = renderWith(undefined);
            const neg = renderWith(-1);

            assert(renderWith(3).includes('· cluster 3'));
            assert(renderWith(0).includes('· cluster 0'));
            assert(!neg.includes('cluster'));
            assert.strictEqual(neg, deleted);
            """
        )
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
