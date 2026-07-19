# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SETTINGS_RENDER_JS = (
    REPO_ROOT / "solstone" / "apps" / "settings" / "static" / "settings.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_settings_render_helpers() -> None:
    script = textwrap.dedent(
        """
        const assert = require('assert');
        const render = require(process.argv[1]);

        class Element {
          constructor(tag, ownerDocument) {
            this.tagName = tag;
            this.ownerDocument = ownerDocument;
            this.children = [];
            this.dataset = {};
            this.attrs = {};
            this.style = {
              setProperty: (name, value) => {
                this.style[name] = value;
              },
            };
            this.textContent = '';
            this.className = '';
            this.hidden = false;
            this.checked = false;
            this.value = '';
            this.href = '';
            this.id = '';
            this.listeners = {};
          }

          appendChild(child) {
            this.children.push(child);
            child.parentNode = this;
            return child;
          }

          replaceChildren() {
            this.children = [];
            this.textContent = '';
          }

          setAttribute(name, value) {
            this.attrs[name] = String(value);
          }

          addEventListener(name, fn) {
            this.listeners[name] = fn;
          }
        }

        const radios = ['on_tap', 'always', 'never'].map((value) => {
          const input = new Element('input');
          input.value = value;
          return input;
        });
        const copyEl = new Element('span');
        copyEl.dataset.copy = 'chat_copy.CHAT_THINKING_OPT_ON_TAP';
        const detail = new Element('section');
        detail.id = 'settings-facet-detail-view';

        const doc = {
          cookie: '',
          createElement(tag) {
            return new Element(tag, this);
          },
          getElementById(id) {
            return id === 'settings-facet-detail-view' ? detail : null;
          },
          querySelectorAll(selector) {
            if (selector === 'input[name="thinking_surfaces"]') return radios;
            if (selector === '[data-copy]') return [copyEl];
            if (selector === '[data-copy-attr]') return [];
            return [];
          },
        };
        detail.ownerDocument = doc;

        render.applyThinkingSurfaces(doc, 'always');
        assert.deepStrictEqual(radios.map((radio) => radio.checked), [false, true, false]);

        render.applyCopy(doc, {
          chat_copy: { CHAT_THINKING_OPT_ON_TAP: 'ask when needed' },
        });
        assert.strictEqual(copyEl.textContent, 'ask when needed');

        render.renderFacetDetail(
          detail,
          {
            facet: 'work',
            config: {
              title: 'Work',
              color: '#123456',
              emoji: 'W',
              muted: true,
            },
          },
          {
            FACET_DETAIL_SUCCESS_HEADING: '{title} is ready',
            FACET_DETAIL_VALUE_FRAMING: 'Use {title} for focused work.',
            FACET_DETAIL_PRIMARY_CTA: 'Open {title} entities',
            FACET_DETAIL_SECONDARY_CTA: 'Back to facets',
            FACET_DETAIL_TERTIARY_ESCAPE: 'Back to settings',
          },
        );

        function textTree(node) {
          return [node.textContent, ...node.children.map(textTree)].join(' ');
        }

        function find(node, predicate) {
          if (predicate(node)) return node;
          for (const child of node.children) {
            const found = find(child, predicate);
            if (found) return found;
          }
          return null;
        }

        assert(textTree(detail).includes('Work is ready'));
        assert(textTree(detail).includes('Use Work for focused work.'));
        const primary = find(detail, (node) => node.id === 'facetDetailPrimary');
        assert(primary);
        assert.strictEqual(primary.href, '/app/entities/');
        assert.strictEqual(primary.dataset.facetSlug, 'work');

        assert.strictEqual(render.redactionRulesLine(0), '0 rules');
        assert.strictEqual(render.redactionRulesLine(1), '1 rule');
        assert.strictEqual(render.redactionRulesLine(3), '3 rules');
        assert.strictEqual(render.visionCategoriesLine(0), '0 categories');
        assert.strictEqual(render.visionCategoriesLine(1), '1 category');
        assert.strictEqual(render.visionCategoriesLine(7), '7 categories');
        assert.strictEqual(render.streamOverridesLine(0), '0 overrides');
        assert.strictEqual(render.streamOverridesLine(1), '1 override');
        assert.strictEqual(render.streamOverridesLine(4), '4 overrides');
        assert.strictEqual(render.buildStreamOverridesDrawerProps([], {}), null);
        assert.strictEqual(render.buildStreamOverridesDrawerProps(null, {}), null);
        assert.strictEqual(render.buildStreamOverridesDrawerProps(undefined, {}), null);

        const configuredStreamProps = render.buildStreamOverridesDrawerProps(
          [{ name: 'desktop' }, { name: 'mobile' }],
          { desktop: { raw_media: 'processed', raw_media_days: null } },
        );
        assert(configuredStreamProps);
        assert.strictEqual(configuredStreamProps.label, 'per-stream overrides');
        assert.strictEqual(configuredStreamProps.line, '1 override');
        assert(configuredStreamProps.bodyHtml.includes('override the global retention mode for individual streams.'));
        assert(configuredStreamProps.bodyHtml.includes('id="streamOverridesList"'));

        const unconfiguredStreamProps = render.buildStreamOverridesDrawerProps(
          [{ name: 'desktop' }, { name: 'mobile' }],
          {},
        );
        assert.strictEqual(unconfiguredStreamProps.line, '0 overrides');

        const staleStreamProps = render.buildStreamOverridesDrawerProps(
          [{ name: 'desktop' }, { name: 'mobile' }],
          { tablet: { raw_media: 'processed', raw_media_days: null } },
        );
        assert.strictEqual(staleStreamProps.line, '0 overrides');
      """
    )
    result = subprocess.run(
        ["node", "-e", script, str(SETTINGS_RENDER_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
