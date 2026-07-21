# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
WHO_IS_THIS_JS = (
    REPO_ROOT / "solstone" / "apps" / "speakers" / "static" / "who_is_this.js"
)


def _node_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is not available")
    return node


DOM_STUB = r"""
const assert = require('assert');
const who = require(process.argv[1]);

class FakeEvent {
  constructor(type, props = {}) {
    this.type = type;
    this.key = props.key || '';
    this.shiftKey = Boolean(props.shiftKey);
    this.target = props.target || null;
    this.defaultPrevented = false;
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
}

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName || '').toUpperCase();
    this.ownerDocument = ownerDocument;
    this.parentNode = null;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.type = '';
    this.id = '';
    this._text = '';
    this.scrollTop = 0;
  }
  get firstChild() {
    return this.children[0] || null;
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join('');
  }
  set textContent(value) {
    this._text = String(value ?? '');
    this.children = [];
  }
  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name === 'class') this.className = text;
    if (name === 'id') this.id = text;
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_match, char) => char.toUpperCase());
      this.dataset[key] = text;
    }
  }
  getAttribute(name) {
    if (name === 'class') return this.className;
    if (name === 'id') return this.id;
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((item) => item !== child);
    child.parentNode = null;
    return child;
  }
  replaceChildren(...nodes) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this._text = '';
    nodes.forEach((node) => this.appendChild(node));
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  addEventListener(type, handler) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(handler);
  }
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    (this.listeners[event.type] || []).forEach((handler) => handler(event));
    return !event.defaultPrevented;
  }
  focus() {
    this.ownerDocument.activeElement = this;
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map((part) => part.trim()).filter(Boolean);
    const found = [];
    const visit = (node) => {
      node.children.forEach((child) => {
        if (selectors.some((part) => child.matches(part))) found.push(child);
        visit(child);
      });
    };
    visit(this);
    return found;
  }
  matches(selector) {
    if (selector === 'button:not([disabled])') {
      return this.tagName === 'BUTTON' && !this.disabled;
    }
    if (selector === 'input:not([disabled])') {
      return this.tagName === 'INPUT' && !this.disabled;
    }
    if (selector === 'a[href]') {
      return this.tagName === 'A' && this.getAttribute('href') !== null;
    }
    if (selector === '[tabindex]:not([tabindex="-1"])') {
      const tabindex = this.getAttribute('tabindex');
      return tabindex !== null && tabindex !== '-1';
    }
    if (selector.startsWith('.')) {
      const classes = selector.slice(1).split('.');
      const present = new Set(String(this.className || '').split(/\s+/).filter(Boolean));
      return classes.every((name) => present.has(name));
    }
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }
}

class FakeDocument {
  constructor() {
    this.body = new FakeElement('body', this);
    this.activeElement = null;
  }
  createElement(tagName) {
    return new FakeElement(tagName, this);
  }
}

function allByTag(root, tagName) {
  return root.querySelectorAll(tagName);
}

function text(root) {
  return root.textContent;
}

function click(node) {
  node.dispatchEvent(new FakeEvent('click'));
}

function input(node, value) {
  node.value = value;
  node.dispatchEvent(new FakeEvent('input'));
}

function makeCopy() {
  return {
    SPK_SHEET_TITLE: 'title',
    SPK_SHEET_LEDE_MANY: 'many {count}',
    SPK_SHEET_LEDE_ONE: 'one',
    SPK_SHELF_CANDIDATES: 'candidates',
    SPK_SHELF_NO_EVIDENCE: 'no evidence',
    SPK_EVIDENCE_SCREEN_MANY: 'screen {count}',
    SPK_EVIDENCE_SCREEN_ONE: 'screen one',
    SPK_EVIDENCE_MEETING_MANY: 'meeting {count}',
    SPK_EVIDENCE_MEETING_ONE: 'meeting one',
    SPK_SHELF_MENTIONS: 'mentions',
    SPK_ANCHOR: 'anchor',
    SPK_ANCHOR_HAS_VOICE: 'anchor voice',
    SPK_SEARCH_LABEL: 'search label',
    SPK_SEARCH_PLACEHOLDER: 'placeholder',
    SPK_THIS_IS_ME: 'me action',
    SPK_THIS_IS_ME_GUIDANCE: 'guidance',
    SPK_SEARCH_NO_RESULTS: 'missing {query}',
    SPK_CREATE_ROW: 'create {query}',
    SPK_NEAR_MATCH_BAND: 'near band',
    SPK_KEEP_SEPARATE_TITLE: 'different {name}',
    SPK_KEEP_SEPARATE_BODY: 'body {name}',
    SPK_KEEP_SEPARATE_CONFIRM: 'confirm new',
    SPK_KEEP_SEPARATE_DECLINE: 'decline {name}',
    SPK_PREVIEW_TITLE: 'preview {name}',
    SPK_PREVIEW_BODY_FRESH: 'fresh',
    SPK_PREVIEW_BODY_HAS_VOICE: 'has voice {name}',
    SPK_PREVIEW_FACTS: 'facts {statements} {conversations}',
    SPK_PREVIEW_CONFIRM: 'confirm {first_name}',
    SPK_PREVIEW_BACK: 'return action',
    SPK_RECEIPT_TITLE: 'receipt {name}',
    SPK_RECEIPT_BODY: 'receipt body {name}',
    SPK_RECEIPT_UNDO: 'undo action',
    SPK_UNDO_DONE: 'undo done',
    SPK_UNDO_PARTIAL: 'partial {restored} {skipped}',
    SPK_EXIT_NOT_PERSON: 'exit person',
    SPK_EXIT_NOT_NOW: 'exit later',
    SPK_NOT_PERSON_DONE: 'done person',
    SPK_NOT_NOW_DONE: 'done later',
    SPK_ACTION_WHO_IS_THIS: 'trigger',
    SPK_LOAD_ERROR: 'load error',
    SPK_SEARCH_ERROR: 'search error',
    SPK_CHECK_NAME_ERROR: 'check error',
    SPK_SAMPLE_UNAVAILABLE: 'unavailable',
    SPK_ACTION_RETRY: 'retry action',
  };
}

function presence(overrides = {}) {
  return {
    cluster_id: 7,
    facts: {
      statement_count: 9,
      conversation_count: 2,
      samples: [
        { day: '20260701', stream: 'stream-a', setting: 'room one', audio_url: null },
        { day: '20260702', stream: 'stream-b', setting: null, audio_url: '/audio/sample.flac' },
      ],
    },
    evidence_complete: true,
    candidates: {
      co_presence: [
        {
          entity_id: 'alice',
          name: 'Alice',
          has_voice: true,
          screen_conversations: 2,
          meeting_days: 1,
        },
      ],
      mention: [
        {
          entity_id: 'bob',
          name: 'Bob',
          has_voice: false,
          setting_conversations: 1,
          speaker_conversations: 0,
        },
      ],
    },
    ...overrides,
  };
}

function makeHarness(options = {}) {
  const doc = new FakeDocument();
  const calls = [];
  const logs = [];
  let requestIndex = 0;
  const apiJson = options.apiJson || ((url, request) => {
    calls.push({
      url,
      request,
      body: request?.body ? JSON.parse(request.body) : null,
    });
    if (url.includes('/presence')) return Promise.resolve(options.presence || presence());
    return Promise.resolve({});
  });
  const controller = who.init({
    mount: doc.body,
    copy: makeCopy(),
    context: { isDay: true },
    apiJson,
    logError: (error, meta) => logs.push({ error, meta }),
    formatDateShort: (day) => `weekday-${day}`,
    debounceMs: 0,
    requestIdFactory: () => `req-${++requestIndex}`,
    onThisIsMe: options.onThisIsMe,
    onIdentified: options.onIdentified,
    onDismissed: options.onDismissed,
  });
  const trigger = doc.createElement('button');
  doc.body.appendChild(trigger);
  return { doc, controller, trigger, calls, logs };
}
"""


def _run_node(body: str) -> None:
    node = _node_or_skip()
    script = DOM_STUB + "\n" + textwrap.dedent(body)
    result = subprocess.run(
        [node, "-e", script, str(WHO_IS_THIS_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_who_is_this_accessibility_contract() -> None:
    _run_node(
        """
        (async () => {
          const { doc, controller, trigger } = makeHarness();
          await controller.open({ cluster: { cluster_id: 7 }, trigger });

          const dialog = doc.body.querySelector('.spk-who-dialog');
          assert.strictEqual(trigger.getAttribute('aria-haspopup'), 'dialog');
          assert.strictEqual(trigger.getAttribute('aria-expanded'), 'true');
          assert.strictEqual(dialog.getAttribute('role'), 'dialog');
          assert.strictEqual(dialog.getAttribute('aria-modal'), 'true');
          assert.strictEqual(dialog.getAttribute('aria-labelledby'), 'spkWhoTitle');
          assert.strictEqual(dialog.getAttribute('tabindex'), '-1');
          assert(doc.activeElement.matches('.spk-who-person-action'));

          const focusable = controller.focusableElements();
          controller.handleDialogKeydown(new FakeEvent('keydown', {
            key: 'Tab',
            target: focusable[focusable.length - 1],
          }));
          assert.strictEqual(doc.activeElement, focusable[0]);

          controller.handleDialogKeydown(new FakeEvent('keydown', {
            key: 'Tab',
            shiftKey: true,
            target: focusable[0],
          }));
          assert.strictEqual(doc.activeElement, focusable[focusable.length - 1]);

          controller.handleDialogKeydown(new FakeEvent('keydown', {
            key: 'Escape',
            target: dialog,
          }));
          assert.strictEqual(trigger.getAttribute('aria-expanded'), 'false');
          assert.strictEqual(doc.activeElement, trigger);
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )


def test_who_is_this_main_lede_samples_and_evidence_states() -> None:
    _run_node(
        """
        (async () => {
          const { doc, controller, trigger } = makeHarness();
          await controller.open({ cluster: { cluster_id: 7 }, trigger });
          const bodyText = text(doc.body);
          assert(bodyText.includes('many 2'));
          assert(bodyText.includes('weekday-20260701 · room one'));
          assert(bodyText.includes('weekday-20260702 · stream-b'));
          assert(bodyText.includes('unavailable'));
          assert(bodyText.includes('candidates'));
          assert(bodyText.includes('screen 2'));
          assert(bodyText.includes('anchor voice'));
          assert(bodyText.includes('mentions'));
          assert(!doc.body.querySelector('.spk-who-mentions').textContent.includes('screen'));

          const emptyHarness = makeHarness({
            presence: presence({
              candidates: { co_presence: [], mention: [] },
              facts: { statement_count: 1, conversation_count: 1, samples: [] },
            }),
          });
          await emptyHarness.controller.open({
            cluster: { cluster_id: 8 },
            trigger: emptyHarness.trigger,
          });
          assert(text(emptyHarness.doc.body).includes('one'));
          assert(text(emptyHarness.doc.body).includes('no evidence'));
          assert(emptyHarness.doc.activeElement.matches('.spk-who-search-input'));

          const incompleteHarness = makeHarness({
            presence: presence({ evidence_complete: false }),
          });
          await incompleteHarness.controller.open({
            cluster: { cluster_id: 9 },
            trigger: incompleteHarness.trigger,
          });
          assert(text(incompleteHarness.doc.body).includes('load error'));
          assert(incompleteHarness.doc.activeElement.matches('.spk-who-retry'));
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )


def test_who_is_this_sample_unavailable_paths() -> None:
    _run_node(
        """
        (async () => {
          const { doc, controller, trigger } = makeHarness();
          await controller.open({ cluster: { cluster_id: 7 }, trigger });

          assert.strictEqual(doc.body.querySelectorAll('.spk-who-sample-unavailable').length, 1);
          const audio = doc.body.querySelector('audio');
          assert(audio);
          const sampleTextBefore = audio.parentNode.textContent;
          audio.dispatchEvent(new FakeEvent('error'));

          assert.strictEqual(doc.body.querySelectorAll('.spk-who-sample-unavailable').length, 2);
          assert.strictEqual(audio.hidden, true);
          assert(audio.parentNode.textContent.includes(sampleTextBefore));
          assert(audio.parentNode.textContent.includes('unavailable'));
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )


def test_who_is_this_search_latest_query_wins_and_text_safety() -> None:
    _run_node(
        """
        (async () => {
          const resolvers = {};
          const apiJson = (url, request) => {
            if (url.includes('/presence')) {
              return Promise.resolve(presence({
                candidates: { co_presence: [], mention: [] },
                facts: { statement_count: 1, conversation_count: 1, samples: [] },
              }));
            }
            if (url.includes('/people/search')) {
              return new Promise((resolve) => { resolvers[url] = resolve; });
            }
            return Promise.resolve({});
          };
          const { doc, controller, trigger } = makeHarness({ apiJson });
          await controller.open({ cluster: { cluster_id: 7 }, trigger });
          const search = doc.body.querySelector('.spk-who-search-input');
          input(search, 'old');
          input(search, '"><script>');
          const urls = Object.keys(resolvers);
          assert.strictEqual(urls.length, 2);

          resolvers[urls[1]]({
            query: '"><script>',
            people: [
              {
                entity_id: 'mal',
                name: '<img src=x onerror=alert(1)>',
                has_voice: false,
              },
            ],
          });
          await Promise.resolve();
          resolvers[urls[0]]({
            query: 'old',
            people: [{ entity_id: 'old', name: 'Old Result', has_voice: false }],
          });
          await Promise.resolve();

          const bodyText = text(doc.body);
          assert(bodyText.includes('<img src=x onerror=alert(1)>'));
          assert(bodyText.includes('create "><script>'));
          assert(!bodyText.includes('Old Result'));
          assert.strictEqual(allByTag(doc.body, 'script').length, 0);
          assert.strictEqual(allByTag(doc.body, 'img').length, 0);
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )


def test_who_is_this_resolve_branching_keep_separate_and_request_ids() -> None:
    _run_node(
        """
        (async () => {
          const calls = [];
          const apiJson = (url, request) => {
            calls.push({ url, body: request?.body ? JSON.parse(request.body) : null });
            if (url.includes('/presence')) return Promise.resolve(presence());
            if (url.includes('/identify')) {
              return Promise.resolve({
                status: 'identified',
                entity_name: 'Alicia New',
                operation_id: 'idop_one',
              });
            }
            return Promise.resolve({});
          };
          const { doc, controller, trigger } = makeHarness({ apiJson });
          await controller.open({ cluster: { cluster_id: 7 }, trigger });

          controller.enterPreview({
            mode: 'attach',
            entity_id: 'alice',
            name: 'Alice Example',
            has_voice: false,
          });
          assert.strictEqual(controller.requestId, 'req-1');
          controller.enterPreview({
            mode: 'attach',
            entity_id: 'alice',
            name: 'Alice Example',
            has_voice: false,
          });
          assert.strictEqual(controller.requestId, 'req-1');
          controller.enterPreview({
            mode: 'attach',
            entity_id: 'bob',
            name: 'Bob Example',
            has_voice: false,
          });
          assert.strictEqual(controller.requestId, 'req-2');

          controller.handleResolveResult('Alicia New', {
            status: 'ambiguous',
            candidates: [
              { id: 'alice', name: 'Alice Near' },
              { id: 'ally', name: 'Ally Near' },
            ],
          });
          assert(text(doc.body).includes('near band'));
          click(doc.body.querySelector('.spk-who-create-row'));
          assert(text(doc.body).includes('different Alice Near'));
          click(doc.body.querySelector('.spk-who-keep-decline'));
          assert(text(doc.body).includes('preview Alice Near'));

          controller.handleResolveResult('Alicia New', {
            status: 'no_match',
            candidates: [
              { id: 'alice', name: 'Alice Near' },
              { id: 'ally', name: 'Ally Near' },
            ],
          });
          click(doc.body.querySelector('.spk-who-create-row'));
          click(doc.body.querySelector('.spk-who-keep-confirm'));
          assert(text(doc.body).includes('preview Alicia New'));
          click(doc.body.querySelector('.spk-who-confirm'));
          await Promise.resolve();

          const commit = calls.find((call) => call.body?.reviewed_near_match_entity_ids);
          assert.deepStrictEqual(commit.body.reviewed_near_match_entity_ids, ['alice', 'ally']);
          assert.strictEqual(commit.body.create_new, true);
          assert.strictEqual(commit.body.request_id, controller.requestId);

          controller.handleResolveResult('Alicia New', {
            status: 'no_match',
            candidates: [],
          });
          assert(text(doc.body).includes('preview Alicia New'));

          controller.handleResolveResult('Broken', { status: 'partial' });
          assert(text(doc.body).includes('check error'));
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )


def test_who_is_this_commit_failure_uses_load_error_and_resolve_uses_check_error() -> (
    None
):
    _run_node(
        """
        (async () => {
          const apiJson = (url, request) => {
            if (url.includes('/presence')) return Promise.resolve(presence());
            if (url.includes('/identify') && request?.body?.includes('"resolve_only":true')) {
              return Promise.reject(new Error('resolve failed'));
            }
            if (url.includes('/identify')) return Promise.reject(new Error('commit failed'));
            return Promise.resolve({});
          };
          const { doc, controller, trigger, logs } = makeHarness({ apiJson });
          await controller.open({ cluster: { cluster_id: 7 }, trigger });
          await controller.resolveCreateName('Alicia');
          assert(text(doc.body).includes('check error'));

          controller.enterPreview({
            mode: 'attach',
            entity_id: 'alice',
            name: 'Alice',
            has_voice: false,
          });
          await controller.commitPreview();
          assert(text(doc.body).includes('load error'));
          assert.strictEqual(logs.length, 2);
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )


def test_who_is_this_receipt_undo_full_partial_and_dismissals() -> None:
    _run_node(
        """
        (async () => {
          let presenceLoads = 0;
          const calls = [];
          const apiJson = (url, request) => {
            const body = request?.body ? JSON.parse(request.body) : null;
            calls.push({ url, body });
            if (url.includes('/presence')) {
              presenceLoads += 1;
              return Promise.resolve(presence());
            }
            if (url.includes('/identify/undo')) {
              return Promise.resolve({
                status: 'undone',
                undo_report: {
                  labels: { restored_count: 1, skipped_count: 0 },
                  corrections: { restored_count: 1, skipped_count: 0 },
                  voiceprints: { restored_count: 1, skipped_count: 0 },
                  tracker: { restored_count: 1, skipped_count: 0 },
                  sentinel: { restored_count: 1, skipped_count: 0 },
                  entity: {
                    restored_count: 1,
                    skipped_count: 0,
                    blocked_categories: [],
                    keep_separate_sources_removed_count: 99,
                  },
                },
              });
            }
            if (url.includes('/dismiss')) {
              return Promise.resolve({ status: 'dismissed', disposition: body.disposition });
            }
            if (url.includes('/identify')) {
              return Promise.resolve({
                status: 'identified',
                entity_name: 'Alice',
                operation_id: 'idop_one',
              });
            }
            return Promise.resolve({});
          };
          const dismissed = [];
          const { doc, controller, trigger } = makeHarness({
            apiJson,
            onDismissed: (payload) => dismissed.push(payload),
          });
          await controller.open({ cluster: { cluster_id: 7 }, trigger });
          controller.enterPreview({
            mode: 'attach',
            entity_id: 'alice',
            name: 'Alice',
            has_voice: false,
          });
          await controller.commitPreview();
          assert(text(doc.body).includes('receipt Alice'));
          await controller.undoReceipt();
          assert.strictEqual(presenceLoads, 2);
          assert(text(doc.body).includes('undo done'));

          const partial = who.summarizeUndoReport({
            status: 'undone',
            undo_report: {
              labels: { restored_count: 1, skipped_count: 2 },
              corrections: { restored_count: 1, skipped_count: 0 },
              voiceprints: { restored_count: 1, skipped_count: 0 },
              tracker: { restored_count: 1, skipped_count: 0 },
              sentinel: { restored_count: 1, skipped_count: 0 },
              entity: {
                restored_count: 1,
                skipped_count: 0,
                blocked_categories: ['keep_separate'],
                keep_separate_sources_removed_count: 99,
              },
            },
          });
          assert.deepStrictEqual(partial, {
            restored: 6,
            skipped: 2,
            blocked_categories: ['keep_separate'],
            fully_restored: false,
          });

          await controller.dismissCluster('not_a_person');
          await controller.dismissCluster('quiet');
          assert.deepStrictEqual(
            calls.filter((call) => call.url.includes('/dismiss')).map((call) => call.body.disposition),
            ['not_a_person', 'quiet'],
          );
          assert.strictEqual(dismissed.length, 2);
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )


def test_who_is_this_this_is_me_callback_and_back_preserves_main_state() -> None:
    _run_node(
        """
        (async () => {
          const callbacks = [];
          const { doc, controller, trigger } = makeHarness({
            onThisIsMe: (payload) => callbacks.push(payload),
          });
          await controller.open({ cluster: { cluster_id: 7 }, trigger });
          controller.mainState.query = 'alice';
          controller.mainState.people = [{ entity_id: 'alice', name: 'Alice', has_voice: false }];
          controller.mainState.searchComplete = true;
          controller.body.scrollTop = 42;
          controller.enterPreview({
            mode: 'attach',
            entity_id: 'alice',
            name: 'Alice',
            has_voice: false,
          });
          click(doc.body.querySelector('.spk-who-preview-return'));
          assert.strictEqual(doc.body.querySelector('.spk-who-search-input').value, 'alice');
          assert(text(doc.body).includes('Alice'));
          assert.strictEqual(controller.body.scrollTop, 42);

          click(doc.body.querySelector('.spk-who-this-is-me'));
          assert.strictEqual(callbacks.length, 1);
          assert.strictEqual(callbacks[0].clusterId, '7');
          assert.strictEqual(trigger.getAttribute('aria-expanded'), 'false');
          assert.strictEqual(doc.activeElement, trigger);
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )
