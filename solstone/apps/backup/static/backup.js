// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function () {
  const copy = window.BACKUP_COPY || {};
  let state = Object.assign({}, window.BACKUP_INITIAL || {});
  let currentRecoveryDisplay = '';
  let pollTimer = null;

  const root = document.querySelector('[data-backup-root]');
  if (!root) return;

  const phaseLabels = copy.phase_labels || {};
  const actionLabels = copy.action_labels || {};
  const destinationLabels = (copy.destination && copy.destination.reason_labels) || {};
  const operationLabels = copy.operation_reason_labels || {};
  const statusLabels =
    (copy.management && copy.management.status_labels) || {};
  const terminalPhases = new Set(['done', 'error']);

  function panel(name) {
    return root.querySelector(`[data-backup-panel="${name}"]`);
  }

  function showPanel(name) {
    for (const item of root.querySelectorAll('[data-backup-panel]')) {
      item.hidden = item.getAttribute('data-backup-panel') !== name;
    }
  }

  function setText(selector, value) {
    const element = root.querySelector(selector);
    if (element) element.textContent = value || '';
  }

  function operationActive(operation) {
    return operation && !terminalPhases.has(operation.phase);
  }

  function labelForPhase(phase) {
    return phaseLabels[phase] || phase || '';
  }

  function reasonLabel(reason) {
    return operationLabels[reason] || destinationLabels[reason] || copy.error_intro || '';
  }

  function formatTime(value) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
      return statusLabels.not_yet || '';
    }
    try {
      return new Date(value * 1000).toLocaleString();
    } catch (_err) {
      return statusLabels.not_yet || '';
    }
  }

  function renderOperation() {
    const operation = state.operation;
    const banner = root.querySelector('[data-operation-banner]');
    if (!banner) return;
    if (!operation) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    setText('[data-operation-phase]', labelForPhase(operation.phase));
    setText('[data-operation-error]', reasonLabel(operation.reason_code));
  }

  function renderStatus() {
    root.setAttribute(
      'data-state',
      operationActive(state.operation) ? state.operation.phase : state.enabled ? 'done' : 'empty',
    );
    setText('[data-last-backup]', formatTime(state.last_backup && state.last_backup.time));
    setText('[data-last-prune]', formatTime(state.last_prune && state.last_prune.time));
    const retention = state.retention || {};
    for (const input of root.querySelectorAll('[data-retention-field]')) {
      const key = input.getAttribute('data-retention-field');
      if (key && retention[key] != null) input.value = retention[key];
    }
    renderOperation();
  }

  function applyPayload(payload) {
    if (!payload) return;
    const next = Object.assign({}, payload);
    delete next.success;
    state = Object.assign({}, state, next);
    renderStatus();
  }

  async function readJson(response) {
    const payload = await response.json();
    if (!response.ok) throw payload;
    return payload;
  }

  async function postJson(path, body) {
    const options = {
      method: 'POST',
      headers: { Accept: 'application/json' },
    };
    if (body) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    return readJson(await fetch(path, options));
  }

  async function refreshStatus() {
    const payload = await readJson(
      await fetch('/app/backup/status', { headers: { Accept: 'application/json' } }),
    );
    applyPayload(payload);
    return payload;
  }

  function showMessage(selector, value) {
    const element = root.querySelector(selector);
    if (!element) return;
    element.textContent = value || '';
    element.hidden = !value;
  }

  function showError(selector, err) {
    showMessage(selector, reasonLabel(err && err.reason_code) || (err && err.error) || '');
  }

  function renderRecoveryGrid(display) {
    currentRecoveryDisplay = display || '';
    const grid = root.querySelector('[data-recovery-grid]');
    if (!grid) return;
    grid.replaceChildren();
    for (const group of currentRecoveryDisplay.split(/\s+/).filter(Boolean)) {
      const block = document.createElement('code');
      block.setAttribute('data-recovery-block', '');
      block.textContent = group;
      grid.append(block);
    }
  }

  async function generateRecoveryKey() {
    const payload = await postJson('/app/backup/keys/generate');
    renderRecoveryGrid(payload.recovery_key_display || '');
    return payload;
  }

  async function revealRecoveryKey() {
    const payload = await postJson('/app/backup/recovery-key/reveal');
    renderRecoveryGrid(payload.recovery_key_display || '');
    return payload;
  }

  async function copyRecoveryKey() {
    if (!currentRecoveryDisplay || !navigator.clipboard) return;
    await navigator.clipboard.writeText(currentRecoveryDisplay);
  }

  function syncBackendFields(prefix) {
    const select = root.querySelector(`[data-field="${prefix ? prefix + '_' : ''}backend"]`);
    const value = select ? select.value : 's3';
    const attr = prefix ? 'data-restore-backend-fields' : 'data-backend-fields';
    for (const group of root.querySelectorAll(`[${attr}]`)) {
      group.hidden = group.getAttribute(attr) !== value;
    }
  }

  function formValue(form, name) {
    const field = form.elements[name];
    return field && typeof field.value === 'string' ? field.value.trim() : '';
  }

  function destinationBody(form) {
    const backend = formValue(form, 'backend') || 's3';
    const credentials = {};
    if (backend === 's3') {
      credentials.access_key_id = formValue(form, 'access_key_id');
      credentials.secret_access_key = formValue(form, 'secret_access_key');
    } else {
      credentials.account_id = formValue(form, 'account_id');
      credentials.account_key = formValue(form, 'account_key');
    }
    return {
      repository: formValue(form, 'repository'),
      backend,
      credentials,
    };
  }

  function pollUntilTerminal() {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(async function () {
      try {
        const payload = await refreshStatus();
        if (operationActive(payload.operation)) {
          pollUntilTerminal();
        } else if (
          payload.operation &&
          payload.operation.kind === 'rotate' &&
          payload.operation.phase === 'done' &&
          payload.recovery_key_confirmed === false
        ) {
          await revealRecoveryKey();
          showPanel('display');
        }
      } catch (_err) {
        const current = state.operation || { kind: 'status' };
        state.operation = Object.assign({}, current, {
          phase: 'error',
          reason_code: 'failed',
          elapsed_ms: 0,
        });
        renderStatus();
      }
    }, 800);
  }

  async function startOperation(path, body) {
    const payload = await postJson(path, body);
    applyPayload(payload);
    if (operationActive(payload.operation)) pollUntilTerminal();
    return payload;
  }

  async function saveDestination(form, targetSelector) {
    const payload = await postJson('/app/backup/destination', destinationBody(form));
    applyPayload(payload);
    const status = payload.destination_status || {};
    showMessage(targetSelector, destinationLabels[status.reason_code] || status.message || '');
    return payload;
  }

  function bindIntro() {
    root.addEventListener('click', async function (event) {
      const button = event.target.closest('[data-action]');
      if (!button || button.disabled) return;
      const action = button.getAttribute('data-action');
      try {
        if (action === 'start') showPanel('educate');
        if (action === 'show-restore') showPanel('restore');
        if (action === 'understand') {
          await generateRecoveryKey();
          showPanel('display');
        }
        if (action === 'continue-confirm') showPanel('confirm');
        if (action === 'see-key-again') {
          await revealRecoveryKey();
          showPanel('display');
        }
        if (action === 'copy-key' || action === 'save-password-manager') {
          await copyRecoveryKey();
        }
        if (action === 'enable-backup') {
          await startOperation('/app/backup/enable');
          showPanel('management');
        }
        if (action === 'backup-now') {
          applyPayload(await postJson('/app/backup/backup-now'));
        }
        if (action === 'view-key') {
          await revealRecoveryKey();
          showPanel('display');
        }
        if (action === 'rotate-key') await startOperation('/app/backup/recovery-key/rotate');
        if (action === 'teardown') {
          if (window.confirm((copy.management && copy.management.destructive_caption) || '')) {
            await startOperation('/app/backup/teardown');
          }
        }
        if (action === 'cancel-restore') showPanel(state.enabled ? 'management' : 'intro');
        if (action === 'use-byo') setMode('byo');
      } catch (err) {
        showError('[data-operation-error]', err);
      }
    });
  }

  function bindForms() {
    const confirmForm = root.querySelector('[data-confirm-form]');
    if (confirmForm) {
      confirmForm.addEventListener('submit', async function (event) {
        event.preventDefault();
        try {
          const entered = root.querySelector('[data-confirm-input]').value || '';
          const payload = await postJson('/app/backup/confirm', { recovery_key: entered });
          applyPayload(payload);
          showMessage('[data-confirm-error]', '');
          if (state.destination && state.destination.credentials_set) {
            await startOperation('/app/backup/enable');
            showPanel('management');
          } else {
            showPanel('destination');
          }
        } catch (err) {
          showError('[data-confirm-error]', err);
        }
      });
    }

    const destinationForm = root.querySelector('[data-destination-form]');
    if (destinationForm) {
      destinationForm.addEventListener('submit', async function (event) {
        event.preventDefault();
        try {
          await saveDestination(destinationForm, '[data-destination-status]');
        } catch (err) {
          showError('[data-destination-status]', err);
        }
      });
    }

    const retentionForm = root.querySelector('[data-retention-form]');
    if (retentionForm) {
      retentionForm.addEventListener('submit', async function (event) {
        event.preventDefault();
        const body = {};
        for (const input of retentionForm.querySelectorAll('[data-retention-field]')) {
          body[input.getAttribute('data-retention-field')] = input.value;
        }
        try {
          const payload = await postJson('/app/backup/retention', body);
          applyPayload(payload);
          showMessage('[data-retention-status]', phaseLabels.done || '');
        } catch (err) {
          showError('[data-retention-status]', err);
        }
      });
    }

    const restoreForm = root.querySelector('[data-restore-form]');
    if (restoreForm) {
      restoreForm.addEventListener('submit', async function (event) {
        event.preventDefault();
        const body = destinationBody(restoreForm);
        body.recovery_key = restoreForm.elements.recovery_key.value || '';
        try {
          await startOperation('/app/backup/restore', body);
          showMessage('[data-restore-status]', labelForPhase('restoring'));
        } catch (err) {
          showError('[data-restore-status]', err);
        }
      });
    }
  }

  function bindBackendSwitching() {
    const destinationBackend = root.querySelector('[data-field="backend"]');
    if (destinationBackend) {
      destinationBackend.addEventListener('change', function () {
        syncBackendFields('');
      });
    }
    const restoreBackend = root.querySelector('[data-field="restore_backend"]');
    if (restoreBackend) {
      restoreBackend.addEventListener('change', function () {
        syncBackendFields('restore');
      });
    }
    syncBackendFields('');
    syncBackendFields('restore');
  }

  function setMode(mode) {
    for (const button of root.querySelectorAll('.backup-mode')) {
      const selected = button.getAttribute('data-mode') === mode;
      button.classList.toggle('is-selected', selected);
      button.setAttribute('aria-checked', selected ? 'true' : 'false');
    }
    for (const item of root.querySelectorAll('[data-mode-panel]')) {
      item.hidden = item.getAttribute('data-mode-panel') !== mode;
    }
  }

  function bindModeSwitching() {
    for (const button of root.querySelectorAll('.backup-mode')) {
      button.addEventListener('click', function () {
        setMode(button.getAttribute('data-mode'));
      });
    }
  }

  function initialPanel() {
    if (operationActive(state.operation)) {
      pollUntilTerminal();
      return state.enabled ? 'management' : 'destination';
    }
    if (state.enabled) return 'management';
    return 'intro';
  }

  function bind() {
    bindIntro();
    bindForms();
    bindBackendSwitching();
    bindModeSwitching();
    renderStatus();
    showPanel(initialPanel());
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
