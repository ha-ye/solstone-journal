# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
NETWORK_RENDER_JS = (
    REPO_ROOT / "solstone" / "apps" / "network" / "static" / "network.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_network_render_copy_and_posture_helpers() -> None:
    script = textwrap.dedent(
        """
        const assert = require('assert');
        const render = require(process.argv[1]);

        function el(dataset = {}) {
          return {
            dataset,
            textContent: '',
            attrs: {},
            setAttribute(name, value) {
              this.attrs[name] = value;
            },
          };
        }

        const textEl = el({ copy: 'STATUS_SENTENCES.checking' });
        const attrEl = el({ copyAttr: 'placeholder:REACH_HOST_ADDRESS_PLACEHOLDER' });
        const multiAttrEl = el({
          copyAttr: 'data-byo-sub:APP_ONOFF_SUB_BYO; data-hosted-sub:APP_ONOFF_SUB_HOSTED',
        });
        const button = el();
        const root = {
          querySelectorAll(selector) {
            if (selector === '[data-copy]') return [textEl];
            if (selector === '[data-copy-attr]') return [attrEl, multiAttrEl];
            return [];
          },
          getElementById(id) {
            return id === 'link-pair-regenerate' ? button : null;
          },
        };
        const copy = {
          STATUS_SENTENCES: { checking: 'checking your journal…' },
          REACH_HOST_ADDRESS_PLACEHOLDER: '192.168.1.44:7657',
          APP_ONOFF_SUB_BYO: 'on — reachable over your own network',
          APP_ONOFF_SUB_HOSTED: 'on — reachable from anywhere',
          WINDOW_CLOSED_BUTTON: 'pairing window closed — open a new one',
          EXPIRED_BUTTON: 'this code expired — show a new one',
        };

        render.applyCopy(root, copy);
        assert.strictEqual(textEl.textContent, copy.STATUS_SENTENCES.checking);
        assert.strictEqual(attrEl.attrs.placeholder, copy.REACH_HOST_ADDRESS_PLACEHOLDER);
        assert.strictEqual(multiAttrEl.attrs['data-byo-sub'], copy.APP_ONOFF_SUB_BYO);
        assert.strictEqual(multiAttrEl.attrs['data-hosted-sub'], copy.APP_ONOFF_SUB_HOSTED);

        render.applyPosture(root, 'spl', copy);
        assert.strictEqual(button.textContent, copy.WINDOW_CLOSED_BUTTON);
        render.applyPosture(root, 'direct', copy);
        assert.strictEqual(button.textContent, copy.EXPIRED_BUTTON);
        """
    )
    result = subprocess.run(
        ["node", "-e", script, str(NETWORK_RENDER_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
