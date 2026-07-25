# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
CHAT_RENDER_JS = REPO_ROOT / "solstone" / "convey" / "static" / "chat_render.js"


def test_chat_render_sol_bubble_uses_markdown_renderer(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        return

    script = tmp_path / "chat-render-markdown-test.js"
    script.write_text(
        textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync(process.argv[2], 'utf8');

            class Element {
              constructor(tag) {
                this.tagName = tag;
                this.children = [];
                this.dataset = {};
                this.attrs = {};
                this.className = '';
                this.id = '';
                this.title = '';
                this._textContent = '';
                this.innerHTML = '';
                this.parentNode = null;
                this.isFragment = false;
              }

              get textContent() {
                if (!this.children.length) return this._textContent;
                return this.children.map((child) => child.textContent || '').join('');
              }

              set textContent(value) {
                this._textContent = String(value ?? '');
              }

              appendChild(child) {
                if (child && child.isFragment) {
                  child.children.slice().forEach((grandchild) => this.appendChild(grandchild));
                  child.children = [];
                  return child;
                }
                this.children.push(child);
                child.parentNode = this;
                return child;
              }

              setAttribute(name, value) {
                this.attrs[name] = String(value);
              }
            }

            const markdownCalls = [];

            function escapeHtml(value) {
              return String(value ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
            }

            const document = {
              createElement(tag) {
                return new Element(tag);
              },
              createDocumentFragment() {
                const fragment = new Element('#fragment');
                fragment.isFragment = true;
                return fragment;
              },
              createTextNode(value) {
                return { textContent: String(value ?? ''), parentNode: null };
              },
            };
            const window = {
              AppServices: {
                renderMarkdown(raw) {
                  markdownCalls.push(raw);
                  return '<p>' + escapeHtml(raw).replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>') + '</p>';
                },
              },
            };
            const context = { console, document, window };
            vm.createContext(context);
            vm.runInContext(source, context);

            function hasClass(node, className) {
              return String(node?.className || '').split(/\\s+/).includes(className);
            }

            function find(node, predicate) {
              if (!node) return null;
              if (predicate(node)) return node;
              for (const child of node.children || []) {
                const found = find(child, predicate);
                if (found) return found;
              }
              return null;
            }

            const renderer = context.window.solChatRender;
            const solSource = [
              '**hello**',
              '',
              '- one',
              '',
              '```js',
              'console.log("x")',
              '```',
            ].join('\\n');
            const ownerSource = '**hello**';
            const ctx = {
              ownerName: 'Owner',
              agentName: 'Sol',
              id: '',
              timeFormatter: { format() { return '9:00 AM'; } },
            };

            const solItem = renderer.renderEventItem(
              { kind: 'sol_message', ts: 1, text: solSource, use_id: 'use-markdown' },
              { ...ctx, id: 'event-sol' },
            );
            const ownerItem = renderer.renderEventItem(
              { kind: 'owner_message', ts: 2, text: ownerSource },
              { ...ctx, id: 'event-owner' },
            );

            const solBubble = find(solItem, (node) => hasClass(node, 'chat-bubble--sol'));
            const solBody = find(solBubble, (node) => hasClass(node, 'chat-bubble-text'));
            assert(solBody, 'sol bubble body was not rendered');
            assert(hasClass(solBody, 'chat-bubble-text--markdown'), 'sol body should use markdown class');
            assert(solBody.innerHTML.includes('<strong>hello</strong>'), 'sol markdown was not rendered');
            assert.strictEqual(solBody.textContent, '');
            assert.notStrictEqual(solBody.textContent, solSource);

            const ownerBubble = find(ownerItem, (node) => hasClass(node, 'chat-bubble--owner'));
            const ownerBody = find(ownerBubble, (node) => hasClass(node, 'chat-bubble-text'));
            assert(ownerBody, 'owner bubble body was not rendered');
            assert.strictEqual(ownerBody.textContent, ownerSource);
            assert.strictEqual(ownerBody.innerHTML, '');
            assert.strictEqual(
              find(ownerBubble, (node) => hasClass(node, 'chat-bubble-text--markdown')),
              null,
            );

            assert.strictEqual(solBubble.attrs['aria-label'], 'Sol: ' + solSource);
            assert(solBubble.attrs['aria-label'].includes('**hello**'));
            assert.strictEqual(ownerBubble.attrs['aria-label'], 'Owner: ' + ownerSource);

            function provenanceTextFor(origin) {
              const item = renderer.renderEventItem(
                { kind: 'sol_message', ts: 3, text: 'origin', use_id: 'use-origin', origin },
                { ...ctx, id: 'event-origin' },
              );
              const provenance = find(item, (node) => hasClass(node, 'chat-origin-provenance'));
              assert(provenance, 'origin provenance was not rendered');
              return provenance.textContent;
            }

            assert.strictEqual(
              provenanceTextFor({
                request_id: 'r1',
                trigger_talent: 'read',
                dedupe: 'morning-plan',
                since_ts: 1781803200000,
                ts: 1,
              }),
              'trigger talent readdedupe morning-plansince Jun 18, 2026, 17:20:00 UTC',
            );
            assert.strictEqual(
              provenanceTextFor({
                request_id: 'r2',
                trigger_talent: 'read',
                dedupe: '',
                since_ts: 0,
                ts: 1,
              }),
              'trigger talent read',
            );
            assert.strictEqual(
              provenanceTextFor({
                request_id: 'r3',
                trigger_talent: 'read',
                since_ts: 'not-a-number',
                ts: 1,
              }),
              'trigger talent read',
            );
            assert.deepStrictEqual(markdownCalls, [solSource, 'origin', 'origin', 'origin']);
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [node, str(script), str(CHAT_RENDER_JS)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
