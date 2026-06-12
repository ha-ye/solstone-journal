// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function () {
  const copy = window.SERVICES_COPY || {};
  const state = Object.assign({}, window.SERVICES_INITIAL || {});
  const rows = new Map();
  const terminalPhases = new Set(['enabled', 'pending', 'revoked', 'error']);
  const actionLabels = copy.action_labels || {};
  const stateLabels = copy.state_labels || {};

  function labelFor(value) {
    return stateLabels[value] || value || '';
  }

  function servicePath(service, action) {
    return `/app/services/${encodeURIComponent(service)}/${action}`;
  }

  function operationActive(operation) {
    return operation && !terminalPhases.has(operation.phase);
  }

  function setText(element, value) {
    if (!element) return;
    element.textContent = value || '';
    element.hidden = !value;
  }

  function renderProvenance(container, provenance) {
    if (!container) return;
    container.replaceChildren();
    const entries = [];
    if (provenance && provenance.since_label) {
      entries.push(['since', provenance.since_label]);
    }
    if (provenance && provenance.enabled_at) {
      entries.push(['enabled', provenance.enabled_at]);
    }
    if (provenance && provenance.key_created_at) {
      entries.push(['key', provenance.key_created_at]);
    }
    if (provenance && provenance.key_fingerprint_sha256) {
      entries.push(['fingerprint', provenance.key_fingerprint_sha256]);
    }
    if (provenance && provenance.checked_at) {
      entries.push(['checked', provenance.checked_at]);
    }
    for (const item of entries) {
      const wrap = document.createElement('div');
      const term = document.createElement('dt');
      const desc = document.createElement('dd');
      term.textContent = item[0];
      desc.textContent = item[1];
      wrap.append(term, desc);
      container.append(wrap);
    }
    container.hidden = entries.length === 0;
  }

  function renderNotice(target, operation) {
    if (!target) return;
    target.replaceChildren();
    target.hidden = true;
    if (!operation || operation.browser_open_succeeded !== false || !operation.portal_url) {
      return;
    }
    target.append(document.createTextNode(labelFor('browser_open_failed') + ' '));
    const link = document.createElement('a');
    link.href = operation.portal_url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = actionLabels.open_link || '';
    target.append(link);
    target.hidden = false;
  }

  function renderActions(row, payload) {
    const actions = payload.actions || {};
    const active = operationActive(payload.operation);
    for (const button of row.querySelectorAll('[data-action]')) {
      const action = button.getAttribute('data-action');
      button.textContent = actionLabels[action] || action;
      button.hidden = !actions[action];
      button.disabled = active;
    }
  }

  function renderRow(service) {
    const row = rows.get(service);
    const payload = state[service];
    if (!row || !payload) return;
    const operation = payload.operation;
    const phase = operation ? operation.phase : payload.state;
    const guidance = operation && operation.guidance ? operation.guidance : payload.guidance;
    const elapsed = operationActive(operation)
      ? ` ${Math.max(0, Math.round((operation.elapsed_ms || 0) / 1000))}s`
      : '';

    setText(row.querySelector('[data-role="state"]'), labelFor(phase) + elapsed);
    setText(row.querySelector('[data-role="guidance"]'), guidance || '');
    renderNotice(row.querySelector('[data-role="notice"]'), operation);
    renderProvenance(row.querySelector('[data-role="provenance"]'), payload.provenance || {});
    renderActions(row, payload);
    if (operationActive(operation)) {
      pollUntilTerminal(service);
    }
  }

  async function readStatus(service) {
    const response = await fetch(servicePath(service, 'status'), {
      headers: { Accept: 'application/json' },
    });
    const data = await response.json();
    if (!response.ok) {
      throw data;
    }
    state[service] = data;
    renderRow(service);
    return data;
  }

  function pollUntilTerminal(service) {
    window.setTimeout(async function () {
      try {
        const data = await readStatus(service);
        if (operationActive(data.operation)) {
          pollUntilTerminal(service);
        }
      } catch (_err) {
        const current = state[service] || {};
        current.operation = {
          kind: 'enable',
          phase: 'error',
          guidance: labelFor('load_failed'),
          retryable: true,
          browser_open_succeeded: null,
          portal_url: null,
          elapsed_ms: 0,
        };
        state[service] = current;
        renderRow(service);
      }
    }, 800);
  }

  async function postAction(service, action) {
    const response = await fetch(servicePath(service, action), {
      method: 'POST',
      headers: { Accept: 'application/json' },
    });
    const data = await response.json();
    if (!response.ok) {
      const current = state[service] || { service, provenance: {}, actions: {} };
      current.operation = {
        kind: action,
        phase: data.reason_code === 'service_busy' ? 'busy' : 'error',
        guidance: data.error || '',
        retryable: false,
        browser_open_succeeded: null,
        portal_url: null,
        elapsed_ms: 0,
      };
      state[service] = current;
      renderRow(service);
      return;
    }
    if (data.status) {
      state[service] = data.status;
    } else {
      state[service] = Object.assign({}, state[service] || {}, data);
    }
    renderRow(service);
  }

  function bind() {
    for (const row of document.querySelectorAll('[data-service-row]')) {
      const service = row.getAttribute('data-service-row');
      rows.set(service, row);
      row.addEventListener('click', function (event) {
        const button = event.target.closest('[data-action]');
        if (!button || button.disabled || button.hidden) return;
        postAction(service, button.getAttribute('data-action'));
      });
      renderRow(service);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
