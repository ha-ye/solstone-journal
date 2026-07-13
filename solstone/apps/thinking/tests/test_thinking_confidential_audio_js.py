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
let failPut = false;
let failRefresh = false;

async function api(path, options) {
  calls.push({path, options});
  if (failPut) throw new Error('put detail');
  return {success: true};
}
async function refreshProviders() {
  refreshes += 1;
  if (failRefresh) throw new Error('refresh detail');
}
function setMessage(id, message, tone = '') {
  messages.push({id, message, tone});
}
function renderConfidentialSetup() {
  rerenders += 1;
}
function reset() {
  calls.length = 0;
  messages.length = 0;
  refreshes = 0;
  rerenders = 0;
  failPut = false;
  failRefresh = false;
}
function assertConfigCall(call, enabled, label) {
  assert(call.path === '/app/settings/api/config', `${label} settings URL`);
  assert(call.options.method === 'PUT', `${label} settings method`);
  assert(
    JSON.stringify(JSON.parse(call.options.body)) === JSON.stringify({
      section: 'transcribe',
      data: {confidential_audio: enabled},
    }),
    `${label} settings body`,
  );
}

async function main() {
  await setConfidentialAudio(false);

  assert(calls.length === 1, 'success call count');
  assertConfigCall(calls[0], false, 'success');
  assert(refreshes === 1, 'refresh after success');
  assert(rerenders === 0, 'no snapback on success');
  assert(JSON.stringify(messages) === JSON.stringify([
    {id: 'confidentialLaneOperation', message: '', tone: ''},
  ]), 'success clears status');

  reset();
  failPut = true;
  await setConfidentialAudio(true);

  assert(calls.length === 1, 'put error call count');
  assertConfigCall(calls[0], true, 'put error');
  assert(refreshes === 0, 'no refresh after put error');
  assert(rerenders === 1, 'snapback after put error');
  assert(JSON.stringify(messages) === JSON.stringify([
    {id: 'confidentialLaneOperation', message: '', tone: ''},
    {id: 'confidentialLaneOperation', message: 'put detail', tone: 'error'},
  ]), 'put error surfaces message');

  reset();
  failRefresh = true;
  await setConfidentialAudio(true);

  assert(calls.length === 1, 'refresh error call count');
  assertConfigCall(calls[0], true, 'refresh error');
  assert(refreshes === 1, 'refresh attempted after put success');
  assert(rerenders === 0, 'no stale snapback after refresh error');
  assert(JSON.stringify(messages) === JSON.stringify([
    {id: 'confidentialLaneOperation', message: '', tone: ''},
    {id: 'confidentialLaneOperation', message: 'refresh detail', tone: 'error'},
  ]), 'refresh error surfaces message');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )
