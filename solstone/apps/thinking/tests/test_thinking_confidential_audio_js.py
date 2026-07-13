# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking.tests.js_extract import extract_js_function

STATIC = Path(__file__).resolve().parents[1] / "static" / "thinking.js"


def _node_script(body: str) -> str:
    source = STATIC.read_text(encoding="utf-8")
    parts = [
        extract_js_function(source, "confidentialAudioSetting"),
        extract_js_function(source, "confidentialEgressLine"),
        extract_js_function(source, "confidentialAudioRender"),
        extract_js_function(source, "confidentialAudioDeferralLine"),
        extract_js_function(source, "setConfidentialAudio"),
        "function assert(condition, message) { if (!condition) throw new Error(message); }",
        f"const copy = {json.dumps(thinking_copy.thinking_copy_payload())};",
        body,
    ]
    return "\n".join(parts)


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "PASS" in result.stdout


def test_confidential_audio_helpers_cover_state_matrix() -> None:
    _run_node(
        _node_script(
            """
const confidentialCopy = copy.confidential;
const beats = confidentialCopy.setup.trust_beats;
const states = ['off', 'verifying', 'verified', 'failed', 'stale', 'unreachable'];
const deferralStates = new Set(['verifying', 'failed', 'stale', 'unreachable']);

assert(confidentialAudioSetting({}) === true, 'absent setting defaults on');

for (const enabled of [true, false]) {
  for (const stateName of states) {
    const activeLane = {confidential_audio: enabled};
    const attestation = {state: stateName};
    const render = confidentialAudioRender(activeLane, attestation, confidentialCopy);
    const egress = confidentialEgressLine(activeLane, beats);
    const deferral = confidentialAudioDeferralLine(activeLane, attestation, confidentialCopy);

    assert(render.hidden === (stateName === 'off'), `hidden state ${enabled} ${stateName}`);
    assert(render.on === enabled, `render setting ${enabled} ${stateName}`);
    assert(render.label === confidentialCopy.audio.label, `label ${enabled} ${stateName}`);
    assert(render.description === (enabled ? confidentialCopy.audio.on : confidentialCopy.audio.off), `description ${enabled} ${stateName}`);
    assert(render.note === confidentialCopy.audio.note, `note ${enabled} ${stateName}`);
    assert(egress === (enabled ? beats.egress_audio_on : beats.egress_audio_off), `egress ${enabled} ${stateName}`);
    assert(
      deferral === (enabled && deferralStates.has(stateName) ? confidentialCopy.audio.deferral : ''),
      `deferral ${enabled} ${stateName}`,
    );
  }
}
console.log('PASS');
"""
        )
    )


def test_set_confidential_audio_posts_settings_config_contract() -> None:
    _run_node(
        _node_script(
            """
const calls = [];
const messages = [];
let refreshes = 0;
let rerenders = 0;
let fail = false;

async function api(path, options) {
  calls.push({path, options});
  if (fail) throw new Error('route detail');
  return {success: true};
}
async function refreshProviders() {
  refreshes += 1;
}
function setMessage(id, message, tone = '') {
  messages.push({id, message, tone});
}
function renderConfidentialSetup() {
  rerenders += 1;
}

async function main() {
  await setConfidentialAudio(false);

  assert(calls.length === 1, 'success call count');
  assert(calls[0].path === '/app/settings/api/config', 'settings URL');
  assert(calls[0].options.method === 'PUT', 'settings method');
  assert(
    JSON.stringify(JSON.parse(calls[0].options.body)) === JSON.stringify({
      section: 'transcribe',
      data: {confidential_audio: false},
    }),
    'settings body',
  );
  assert(refreshes === 1, 'refresh after success');
  assert(rerenders === 0, 'no snapback on success');
  assert(JSON.stringify(messages) === JSON.stringify([
    {id: 'confidentialLaneOperation', message: '', tone: ''},
  ]), 'success clears status');

  fail = true;
  messages.length = 0;
  await setConfidentialAudio(true);

  assert(calls.length === 2, 'error call count');
  assert(
    JSON.stringify(JSON.parse(calls[1].options.body)) === JSON.stringify({
      section: 'transcribe',
      data: {confidential_audio: true},
    }),
    'error body',
  );
  assert(refreshes === 1, 'no refresh after error');
  assert(rerenders === 1, 'snapback after error');
  assert(JSON.stringify(messages) === JSON.stringify([
    {id: 'confidentialLaneOperation', message: '', tone: ''},
    {id: 'confidentialLaneOperation', message: 'route detail', tone: 'error'},
  ]), 'error surfaces message');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )
