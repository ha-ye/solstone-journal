// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function () {
  const BACKUP_COPY = {
    "service_name": "encrypted backup",
    "brand_lock": "your journal is always private, only yours.",
    "intro": {
      "title": "encrypted backup",
      "subtitle": "make an encrypted copy of your journal somewhere safe — only you can read it.",
      "bullets": [
        "end-to-end encrypted",
        "optional, always",
        "delete anytime"
      ],
      "optional": "your journal lives on your device; backup is optional.",
      "steps": "you'll save a recovery key, then choose where your backup lives."
    },
    "educate": {
      "stakes": "if you lose your recovery key, no one can recover your journal — not even sol pbc."
    },
    "key": {
      "theft_honesty": "anyone with your recovery key can read everything in your backup — store it like a master password.",
      "pm_caution": "only store your recovery key in a password manager you trust. sol pbc doesn't recommend a specific one.",
      "save_password_manager": "save to my password manager",
      "copy_label": "copy",
      "continue": "continue",
      "clipboard_caveat": "copying puts your recovery key on the clipboard — clear it after you save it."
    },
    "confirm": {
      "prompt": "enter the recovery key you just recorded.",
      "escape": "see key again"
    },
    "destination": {
      "repository_hint": "the restic repository for your bucket — e.g. s3:s3.amazonaws.com/your-bucket",
      "object_lock_warning": "don't enable Compliance-mode Object Lock on the bucket — it conflicts with backup pruning and lock cleanup. if you need immutability, use Governance mode.",
      "object_lock_summary": "bucket setup notes",
      "field_labels": {
        "repository": "repository",
        "backend": "backend",
        "s3": "S3",
        "b2": "B2",
        "access_key_id": "access key id",
        "secret_access_key": "secret access key",
        "b2_key_id": "key id",
        "b2_application_key": "application key"
      },
      "reason_labels": {
        "repo_exists": "destination is reachable and already set up.",
        "repo_missing": "destination is reachable and needs setup.",
        "auth_failed": "the destination rejected the key or credentials. check the recovery key and destination details.",
        "locked": "the destination is busy. try again shortly.",
        "timeout": "the destination took too long to respond. try again shortly.",
        "unreachable": "i couldn't reach the destination. check the repository path and try again."
      },
      "modes": {
        "byo": {
          "title": "your own",
          "desc": "your bucket, your credentials. the default.",
          "note": "sol pbc is never in the path."
        },
        "hosted": {
          "title": "operated by sol pbc",
          "desc": "sol pbc runs the off-device part for you.",
          "note": "sol pbc only ever holds an encrypted copy it can't read.",
          "cta": "set up backup →"
        }
      }
    },
    "hosted": {
      "setup_hint": "turning this on sets up encrypted backup, operated by sol pbc — you turn it on on the services page that opens, then come back here. your journal stays on your device; only the encrypted copy goes to storage sol pbc operates, and sol pbc can never read it.",
      "restore_hint": "restore the encrypted copy sol pbc keeps for you — enter your recovery key, then turn it on on the services page.",
      "location_label": "operated by sol pbc",
      "manage_label": "manage in your services →",
      "manage_url": "https://services.solstone.app/services/backup"
    },
    "management": {
      "destructive_action": "turn off & delete backup",
      "destructive_caption": "this deletes all your backup data. no new backups will be created.",
      "teardown_gate_lead": "{days} days of recordings ({size}) exist only in this backup. deleting the backup deletes them everywhere, forever.",
      "teardown_gate_unavailable_lead": "can't verify what exists only in this backup right now. deleting the backup may destroy recordings that exist nowhere else.",
      "teardown_confirm_phrase": "delete",
      "teardown_confirm_prompt": "type delete to confirm",
      "teardown_restore_first_action": "restore everything first",
      "retention_hint": "how many recent copies to keep at each interval.",
      "status_labels": {
        "last_backup": "last backup",
        "last_prune": "last prune",
        "storage_used": "storage used",
        "snapshot_history": "snapshot history",
        "not_available": "not yet available",
        "not_yet": "not yet",
        "enabled": "on",
        "disabled": "off",
        "destination": "where your backup lives",
        "retention": "retention",
        "setup": "set up your recovery key"
      },
      "retention_labels": {
        "hourly": "hourly",
        "daily": "daily",
        "weekly": "weekly",
        "monthly": "monthly"
      }
    },
    "restore": {
      "expectation": "a large restore can take a while. you can leave this page open while it runs."
    },
    "offload": {
      "title": "media offload",
      "stakes": "after offload, your backup holds the only copy of your older recordings. if you lose your recovery key, no one can recover them — not even sol pbc.",
      "stalled_lead": "offload is paused: your backup isn't working. nothing has been deleted.",
      "backup_only_label": "in your backup",
      "restore_expectation": "restoring {size} from your backup — a large restore can take a while.",
      "disable_note": "offloading stops. recordings already in your backup stay there — protected and restorable.",
      "unavailable_lead": "can't read offload status right now.",
      "action_error": "media offload couldn't finish. check backup setup, then try again.",
      "enable_hint": "choose how much older media can leave this device after backup verification.",
      "not_ready": "turn on encrypted backup and confirm your recovery key before using media offload.",
      "labels": {
        "budget_gb": "raw media budget",
        "floor_gb": "device free-space floor",
        "raw_media": "on this device",
        "device_free": "device free",
        "device_total": "device total",
        "last_offload": "last offload",
        "last_verify": "last verification",
        "last_restore": "last restore",
        "days": "days with media in backup",
        "gb_suffix": "GB"
      },
      "actions": {
        "enable": "turn on media offload",
        "save": "save limits",
        "disable": "turn off media offload",
        "restore_day": "restore this day"
      },
      "messages": {
        "saved": "saved",
        "empty_days": "no offloaded media yet.",
        "degraded": "some offload ledger entries could not be read."
      },
      "stall_reason_labels": {
        "backup_not_ready": "encrypted backup needs to finish setup before media offload can run.",
        "backup_failing": "encrypted backup needs a healthy recent copy before media offload can run.",
        "verification_missing": "backup verification needs to run before media offload can start.",
        "verification_overdue": "backup verification is overdue. media offload will wait for a fresh verification.",
        "verification_failed": "backup verification failed. media offload will wait for a healthy verification.",
        "locked": "media offload is waiting for backup maintenance to finish.",
        "archive_failed": "media offload could not add older media to encrypted backup.",
        "confirm_failed": "media offload could not verify the backed-up media.",
        "confirm_tool_failed": "media offload could not run the verification tool.",
        "unexpected_error": "media offload stopped unexpectedly. try again after backup maintenance runs."
      },
      "restore_reason_labels": {
        "auth_failed": "encrypted backup rejected the recovery key or credentials.",
        "backup_not_ready": "encrypted backup is not ready to restore media.",
        "failed": "media restore could not finish.",
        "insufficient_free_space": "this device needs more free space before restoring media.",
        "ledger_degraded": "media restore is paused because the offload ledger needs repair.",
        "locked": "media restore is waiting for backup maintenance to finish.",
        "missing_file_after_restore": "media restore finished, but a file was still missing.",
        "nothing_to_restore": "nothing to restore for that day.",
        "repo_missing": "encrypted backup could not find the repository.",
        "restic_unavailable": "the backup tool is not available yet.",
        "rclone_unavailable": "the storage access tool is not available yet.",
        "segment_missing": "that day is no longer available locally.",
        "timeout": "media restore took too long. try again later.",
        "verification_failed": "restored media did not match the backup checksum."
      }
    },
    "phase_labels": {
      "setting_up": "setting up your backup…",
      "restoring": "restoring your journal…",
      "rotating": "making a new recovery key…",
      "tearing_down": "turning off…",
      "done": "done",
      "degraded": "restored, but not verified",
      "error": "couldn't finish",
      "loading": "loading…",
      "empty": "not set up yet"
    },
    "operation_reason_labels": {
      "backup_busy": "another backup task is already running. try again in a moment.",
      "backup_not_confirmed": "confirm your recovery key before turning on backup.",
      "backup_operation_failed": "i couldn't finish that backup action. check the recovery key and destination, then try again.",
      "backup_unavailable": "i couldn't ask the background service to start a backup. start it, then try again.",
      "invalid_key": "that recovery key didn't unlock the backup. re-enter the key from your saved copy.",
      "invalid_config_value": "use non-negative whole numbers, then save again.",
      "invalid_operation_for_state": "finish the current backup setup step, then try again.",
      "invalid_request_value": "check the destination details and try again.",
      "restic_unavailable": "i couldn't prepare the backup tool. try again after setup finishes.",
      "repo_missing": "i couldn't find a backup repository at that destination.",
      "auth_failed": "that recovery key didn't unlock the backup. check the key first, then the destination details.",
      "locked": "the destination is busy. try again shortly.",
      "timeout": "the destination took too long to respond. try again shortly.",
      "failed": "i couldn't finish the backup action. check the recovery key and destination, then try again.",
      "incomplete": "the backup action didn't finish. you can try again.",
      "integrity_failed": "your journal was restored to this device, but the backup copy failed its integrity check and may be damaged.",
      "integrity_unverified": "your journal was restored to this device, but the integrity check couldn't run (the backup was busy or timed out), so the backup copy is unverified.",
      "missing_required_field": "fill in the required fields, then try again.",
      "recovery_key_mismatch": "that didn't match your recovery key. re-enter the key from your saved copy.",
      "expired": "the approval took too long. try again.",
      "malformed": "the response couldn't be read. update your journal, then try again.",
      "network_error": "the services page couldn't be reached. check your connection, then try again.",
      "broker_unreachable": "encrypted backup couldn't be reached. check your connection, then try again.",
      "broker_error": "encrypted backup didn't return usable settings. try again shortly.",
      "hosted_entitlement_inactive": "set up backup on the services page that opens, then try again."
    },
    "action_labels": {
      "start": "get started",
      "understand": "i understand",
      "save_destination": "save destination",
      "enable": "turn on backup",
      "backup_now": "back up now",
      "view_key": "view recovery key",
      "rotate_key": "regenerate recovery key",
      "teardown": "turn off & delete backup",
      "save_retention": "save retention",
      "restore": "restore",
      "try_again": "try again",
      "cancel": "cancel"
    },
    "error_intro": "start with the recovery key. if it still fails, check the destination details."
  };
  const copy = BACKUP_COPY;
  const BYTES_PER_GB = 1000000000;
  let state = {};
  let offloadState = { status: 'loading', payload: null };
  let currentRecoveryDisplay = '';
  let pollTimer = null;

  const root = document.querySelector('[data-backup-root]');
  if (!root) return;

  function logMissingCopy(path) {
    const error = new Error(`missing backup copy path: ${path}`);
    if (window.logError) {
      window.logError(error, { context: 'backup copy render', path });
    } else if (window.console && window.console.error) {
      window.console.error(error);
    }
  }

  function copyValue(source, path) {
    let cursor = source;
    for (const part of path.split('.')) {
      if (cursor == null || typeof cursor !== 'object' || !(part in cursor)) {
        logMissingCopy(path);
        return undefined;
      }
      cursor = cursor[part];
    }
    if (cursor === undefined) logMissingCopy(path);
    return cursor;
  }

  function applyTextCopy(target, selector, attr, setter, source) {
    for (const element of target.querySelectorAll(selector)) {
      const path = element.getAttribute(attr);
      const value = path ? copyValue(source, path) : undefined;
      if (value !== undefined) setter(element, String(value));
    }
  }

  function applyCopy(target, source) {
    applyTextCopy(target, '[data-copy]', 'data-copy', (element, value) => {
      element.textContent = value;
    }, source);
    applyTextCopy(target, '[data-copy-href]', 'data-copy-href', (element, value) => {
      element.setAttribute('href', value);
    }, source);
    applyTextCopy(target, '[data-copy-aria-label]', 'data-copy-aria-label', (element, value) => {
      element.setAttribute('aria-label', value);
    }, source);
  }

  function renderIntroBullets(target, source) {
    const list = target.querySelector('[data-copy-list="intro.bullets"]');
    if (!list) return;
    const bullets = copyValue(source, 'intro.bullets');
    list.replaceChildren();
    if (!Array.isArray(bullets)) {
      logMissingCopy('intro.bullets');
      return;
    }
    for (const bullet of bullets) {
      const item = document.createElement('li');
      item.textContent = String(bullet);
      list.append(item);
    }
  }

  function renderRetentionGrid(target, source) {
    const grid = target.querySelector('[data-retention-grid]');
    if (!grid) return;
    const labels = copyValue(source, 'management.retention_labels');
    grid.replaceChildren();
    if (!labels || typeof labels !== 'object' || Array.isArray(labels)) {
      logMissingCopy('management.retention_labels');
      return;
    }
    for (const [key, labelText] of Object.entries(labels)) {
      const label = document.createElement('label');
      const text = document.createElement('span');
      text.textContent = String(labelText);
      const input = document.createElement('input');
      input.setAttribute('name', key);
      input.setAttribute('data-retention-field', key);
      input.setAttribute('type', 'number');
      input.setAttribute('min', '0');
      input.setAttribute('step', '1');
      label.append(text, input);
      grid.append(label);
    }
  }

  const phaseLabels = copy.phase_labels || {};
  const actionLabels = copy.action_labels || {};
  const destinationLabels = (copy.destination && copy.destination.reason_labels) || {};
  const operationLabels = copy.operation_reason_labels || {};
  const managementCopy = copy.management || {};
  const statusLabels = managementCopy.status_labels || {};
  const hostedCopy = copy.hosted || {};
  const offloadCopy = copy.offload || {};
  const offloadLabels = offloadCopy.labels || {};
  const offloadMessages = offloadCopy.messages || {};
  const offloadStallLabels = offloadCopy.stall_reason_labels || {};
  const offloadRestoreLabels = offloadCopy.restore_reason_labels || {};
  const offloadRouteErrorReasons = new Set([
    'invalid_config_value',
    'backup_not_confirmed',
    'invalid_operation_for_state',
    'backup_busy',
  ]);
  const terminalPhases = new Set(['done', 'error', 'needs_subscription', 'degraded', 'refused']);

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

  function managedMode() {
    return state.enabled || state.mode === 'operated';
  }

  function labelForPhase(phase) {
    return phaseLabels[phase] || phase || '';
  }

  function reasonLabel(reason) {
    return operationLabels[reason] || destinationLabels[reason] || copy.error_intro || '';
  }

  function offloadActionError(err) {
    const reason = err && err.reason_code;
    if (offloadRouteErrorReasons.has(reason) && operationLabels[reason]) {
      return operationLabels[reason];
    }
    return offloadCopy.action_error || '';
  }

  function maybeOpenPortal(payload) {
    const operation = payload && payload.operation;
    if (operation && operation.portal_url) {
      window.open(operation.portal_url, '_blank', 'noopener');
    }
  }

  function renderHostedLocation() {
    const section = root.querySelector('[data-hosted-location-section]');
    if (!section) return;
    const hosted = state.hosted || {};
    const operated = state.mode === 'operated' && hosted.bound;
    section.hidden = !operated;
    if (operated) {
      setText('[data-hosted-location]', hostedCopy.location_label || '');
    }
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

  function formatDay(value) {
    if (typeof value !== 'string' || value.length !== 8) return value || '';
    return `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
  }

  function formatGbInput(bytes) {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes <= 0) {
      return '';
    }
    const value = bytes / BYTES_PER_GB;
    if (Number.isInteger(value)) return String(value);
    return String(Math.round(value * 10) / 10);
  }

  function formatBytes(bytes) {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) {
      return statusLabels.not_available || '';
    }
    const suffix = offloadLabels.gb_suffix || '';
    const value = bytes / BYTES_PER_GB;
    const rounded = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
    return `${rounded.toLocaleString()}${suffix ? ' ' + suffix : ''}`;
  }

  function gbToBytes(value) {
    const parsed = Number.parseFloat(value);
    if (!Number.isFinite(parsed) || parsed <= 0) return 0;
    return Math.round(parsed * BYTES_PER_GB);
  }

  function offloadReady() {
    return state.enabled === true && state.recovery_key_confirmed === true;
  }

  function reasonFromOffloadMap(labels, path, reason) {
    if (!reason) return '';
    const label = labels[reason];
    if (!label) logMissingCopy(`${path}.${reason}`);
    return label || '';
  }

  function offloadStallReasonLabel(reason) {
    return reasonFromOffloadMap(offloadStallLabels, 'offload.stall_reason_labels', reason);
  }

  function offloadRestoreReasonLabel(reason) {
    return reasonFromOffloadMap(offloadRestoreLabels, 'offload.restore_reason_labels', reason);
  }

  function formatOffloadResult(result, lookup) {
    if (!result || !result.status) return statusLabels.not_yet || '';
    const parts = [formatTime(result.time)];
    if (result.status !== 'ok') {
      const reason = lookup(result.reason);
      if (reason) parts.push(reason);
    }
    return parts.filter(Boolean).join(' · ');
  }

  function teardownConfirmPhrase() {
    return managementCopy.teardown_confirm_phrase || '';
  }

  function teardownInputValue() {
    const input = root.querySelector('[data-teardown-input]');
    return input && typeof input.value === 'string' ? input.value : '';
  }

  function teardownConfirmSatisfied() {
    const phrase = teardownConfirmPhrase();
    return phrase !== '' && teardownInputValue() === phrase;
  }

  function updateTeardownConfirmState() {
    const button = root.querySelector('[data-action="teardown-confirm"]');
    if (button) button.disabled = !teardownConfirmSatisfied();
  }

  function backupOnlyTotalsForTeardown() {
    if (offloadState.status !== 'ready') return null;
    const payload = offloadState.payload || {};
    const backupOnly = payload.backup_only;
    if (!backupOnly || typeof backupOnly !== 'object' || Array.isArray(backupOnly)) return null;
    const days = backupOnly.total_days;
    const bytes = backupOnly.total_bytes;
    if (typeof days !== 'number' || !Number.isFinite(days) || days < 0) return null;
    if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return null;
    return { days, size: formatBytes(bytes) };
  }

  function renderTeardownGate(totals) {
    const stakes = root.querySelector('[data-teardown-stakes]');
    if (!stakes) return;
    if (totals === null) {
      stakes.textContent = managementCopy.teardown_gate_unavailable_lead || '';
      return;
    }
    stakes.textContent = (managementCopy.teardown_gate_lead || '')
      .replace('{days}', totals.days.toLocaleString())
      .replace('{size}', totals.size);
  }

  function showTeardownGate() {
    const gate = root.querySelector('[data-teardown-gate]');
    if (gate) gate.hidden = false;
    updateTeardownConfirmState();
  }

  function resetTeardownGate() {
    const gate = root.querySelector('[data-teardown-gate]');
    const input = root.querySelector('[data-teardown-input]');
    if (gate) gate.hidden = true;
    if (input) input.value = '';
    showMessage('[data-teardown-status]', '');
    updateTeardownConfirmState();
  }

  // /app/backup/teardown remains unguarded server-side exactly as shipped today;
  // this owner-authenticated local app keeps the gate as a page-level honesty
  // surface and does not change the server contract.
  async function openTeardownGate() {
    try {
      await refreshOffloadStatus();
      const totals = backupOnlyTotalsForTeardown();
      renderTeardownGate(totals);
    } catch (err) {
      if (window.logError) {
        window.logError(err, { context: 'backup teardown offload status failed' });
      }
      renderOffloadUnavailable();
      renderTeardownGate(null);
    }
    showTeardownGate();
  }

  function offloadConfigBody() {
    const budgetField = root.querySelector('[data-offload-budget-input]') || {};
    const floorField = root.querySelector('[data-offload-floor-input]') || {};
    return {
      budget_bytes: gbToBytes(budgetField.value),
      floor_bytes: gbToBytes(floorField.value),
    };
  }

  function renderOffloadDays(days) {
    const target = root.querySelector('[data-offload-days]');
    if (!target) return;
    const template = root.querySelector('[data-offload-day-template]');
    target.replaceChildren();
    if (!Array.isArray(days) || days.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'backup-note';
      empty.textContent = offloadMessages.empty_days || '';
      target.append(empty);
      return;
    }
    for (const day of days) {
      const clone = template.content.cloneNode(true);
      applyCopy(clone, copy);
      const row = clone.querySelector('.backup-offload-day');
      const details = clone.querySelector('[data-offload-day-detail]');
      const heading = clone.querySelector('strong[data-offload-day-value]');
      if (heading) heading.textContent = formatDay(day.day);
      const raw = clone.querySelector('[data-offload-day-raw-bytes]');
      if (raw) raw.textContent = formatBytes(day.raw_media_bytes || 0);
      const backupOnly = clone.querySelector('[data-offload-day-backup-only-bytes]');
      if (backupOnly) backupOnly.textContent = formatBytes(day.backup_only_bytes || 0);

      if (day.degraded) {
        const degraded = document.createElement('p');
        degraded.className = 'backup-warning';
        degraded.textContent = offloadMessages.degraded || '';
        if (details) details.append(degraded);
      }

      const button = clone.querySelector('[data-offload-day-restore]');
      const size = formatBytes(day.backup_only_bytes || 0);
      if (button) {
        button.setAttribute('data-offload-day-value', day.day || '');
        button.title = (offloadCopy.restore_expectation || '').replace('{size}', size);
        button.disabled = !day.backup_only_segments;
      }
      if (row) target.append(row);
    }
  }

  function renderOffload() {
    const section = root.querySelector('[data-offload-section]');
    if (!section) return;
    section.setAttribute('data-offload-state', offloadState.status);

    const ready = offloadReady();
    const unavailable = offloadState.status === 'unavailable';
    const payload = offloadState.payload || {};
    const offload = payload.offload || {};
    const enabled = ready && offload.enabled === true;
    const unavailableElement = root.querySelector('[data-offload-unavailable]');
    if (unavailableElement) unavailableElement.hidden = !unavailable;

    const readiness = root.querySelector('[data-offload-readiness]');
    if (readiness) readiness.hidden = ready || unavailable;
    const form = root.querySelector('[data-offload-enable-form]');
    const summary = root.querySelector('[data-offload-summary]');
    const tiering = root.querySelector('.backup-offload-tiering');
    if (form) form.hidden = unavailable;
    if (summary) summary.hidden = unavailable || offloadState.status === 'loading';
    if (tiering) tiering.hidden = !ready || unavailable || offloadState.status === 'loading';

    const budget = offload.budget_bytes || (payload.suggested_defaults && payload.suggested_defaults.budget_bytes);
    const floor = offload.floor_bytes || (payload.suggested_defaults && payload.suggested_defaults.floor_bytes);
    const budgetField = root.querySelector('[data-offload-budget-input]');
    const floorField = root.querySelector('[data-offload-floor-input]');
    if (budgetField && offloadState.status === 'ready') budgetField.value = formatGbInput(budget);
    if (floorField && offloadState.status === 'ready') floorField.value = formatGbInput(floor);
    for (const field of [budgetField, floorField]) {
      if (field) field.disabled = !ready || unavailable;
    }

    const enableButton = root.querySelector('[data-action="offload-enable"]');
    if (enableButton) enableButton.disabled = !ready || enabled || unavailable;
    const saveButton = root.querySelector('[data-action="offload-save"]');
    if (saveButton) saveButton.disabled = !ready || unavailable;
    const disableButton = root.querySelector('[data-offload-disable]');
    if (disableButton) disableButton.disabled = !enabled || unavailable;

    setText('[data-offload-raw-bytes]', formatBytes(payload.raw_media && payload.raw_media.total_bytes));
    setText('[data-offload-backup-only-bytes]', formatBytes(payload.backup_only && payload.backup_only.total_bytes));
    setText('[data-offload-device-free]', formatBytes(payload.device && payload.device.free_bytes));
    setText('[data-offload-device-total]', formatBytes(payload.device && payload.device.total_bytes));
    setText('[data-offload-last-run]', formatOffloadResult(payload.last_offload, offloadStallReasonLabel));
    setText('[data-offload-last-verify]', formatOffloadResult(payload.last_verification, () => ''));
    setText('[data-offload-last-restore]', formatOffloadResult(payload.last_restore, offloadRestoreReasonLabel));

    const stalled = payload.last_offload && payload.last_offload.status === 'stalled';
    const stallElement = root.querySelector('[data-offload-stall-reason]');
    if (stallElement) {
      stallElement.hidden = !stalled;
      if (stalled) {
        stallElement.textContent = [
          offloadCopy.stalled_lead || '',
          offloadStallReasonLabel(payload.last_offload.reason),
        ].filter(Boolean).join(' ');
      }
    }

    renderOffloadDays(unavailable || offloadState.status === 'loading' ? [] : payload.days);
  }

  function renderOperation() {
    const operation = state.operation;
    const banner = root.querySelector('[data-operation-banner]');
    if (!banner) return;
    if (!operation || operation.phase === 'needs_subscription') {
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
      operationActive(state.operation) ? state.operation.phase : managedMode() ? 'done' : 'empty',
    );
    setText('[data-last-backup]', formatTime(state.last_backup && state.last_backup.time));
    setText('[data-last-prune]', formatTime(state.last_prune && state.last_prune.time));
    const retention = state.retention || {};
    for (const input of root.querySelectorAll('[data-retention-field]')) {
      const key = input.getAttribute('data-retention-field');
      if (key && retention[key] != null) input.value = retention[key];
    }
    renderOperation();
    renderHostedLocation();
    renderOffload();
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

  function applyOffloadPayload(payload) {
    if (!validOffloadPayload(payload)) {
      const error = new Error('malformed backup offload status payload');
      if (window.logError) {
        window.logError(error, { context: 'backup offload status payload' });
      } else if (window.console && window.console.error) {
        window.console.error(error);
      }
      renderOffloadUnavailable();
      return false;
    }
    const next = Object.assign({}, payload || {});
    delete next.success;
    delete next.operation;
    offloadState = { status: 'ready', payload: next };
    renderOffload();
    return true;
  }

  function validOffloadPayload(payload) {
    return Boolean(
      payload &&
        typeof payload === 'object' &&
        !Array.isArray(payload) &&
        payload.offload &&
        typeof payload.offload === 'object' &&
        !Array.isArray(payload.offload) &&
        Array.isArray(payload.days),
    );
  }

  async function refreshOffloadStatus() {
    const payload = await readJson(
      await fetch('/app/backup/offload/status', { headers: { Accept: 'application/json' } }),
    );
    applyOffloadPayload(payload);
    return payload;
  }

  function renderOffloadUnavailable() {
    offloadState = { status: 'unavailable', payload: null };
    renderOffload();
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
        } else if (payload.operation && payload.operation.kind === 'teardown') {
          resetTeardownGate();
        } else if (payload.operation && payload.operation.kind === 'offload_restore') {
          try {
            await refreshOffloadStatus();
          } catch (err) {
            if (window.logError) {
              window.logError(err, { context: 'backup offload status after restore failed' });
            }
            renderOffloadUnavailable();
          }
        } else if (
          payload.operation &&
          (payload.operation.kind === 'enable_hosted' || payload.operation.kind === 'restore_hosted') &&
          payload.operation.phase === 'done'
        ) {
          showPanel('management');
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
        if (action === 'enable-hosted') {
          const payload = await startOperation('/app/backup/enable-hosted');
          maybeOpenPortal(payload);
        }
        if (action === 'backup-now') {
          applyPayload(await postJson('/app/backup/backup-now'));
        }
        if (action === 'view-key') {
          await revealRecoveryKey();
          showPanel('display');
        }
        if (action === 'rotate-key') await startOperation('/app/backup/recovery-key/rotate');
        if (action === 'teardown-open') await openTeardownGate();
        if (action === 'teardown-cancel') resetTeardownGate();
        if (action === 'teardown-confirm') {
          if (!teardownConfirmSatisfied()) return;
          const payload = await startOperation('/app/backup/teardown');
          if (payload.operation && payload.operation.kind === 'teardown' && !operationActive(payload.operation)) {
            resetTeardownGate();
          }
        }
        if (action === 'teardown-restore-first') {
          await startOperation('/app/backup/offload/restore', { all: true });
          resetTeardownGate();
        }
        if (action === 'cancel-restore') showPanel(managedMode() ? 'management' : 'intro');
        if (action === 'restore-hosted') {
          const field = root.querySelector('[data-restore-hosted-input]') || {};
          const entered = field.value || '';
          const payload = await startOperation('/app/backup/restore-hosted', { recovery_key: entered });
          maybeOpenPortal(payload);
        }
        if (action === 'offload-enable') {
          if (applyOffloadPayload(await postJson('/app/backup/offload/enable'))) {
            showMessage('[data-offload-config-status]', offloadMessages.saved || '');
          }
        }
        if (action === 'offload-save') {
          if (applyOffloadPayload(await postJson('/app/backup/offload/config', offloadConfigBody()))) {
            showMessage('[data-offload-config-status]', offloadMessages.saved || '');
          }
        }
        if (action === 'offload-disable') {
          if (applyOffloadPayload(await postJson('/app/backup/offload/disable'))) {
            showMessage('[data-offload-config-status]', offloadMessages.saved || '');
          }
        }
        if (action === 'offload-restore-day') {
          const day = button.getAttribute('data-offload-day-value');
          await startOperation('/app/backup/offload/restore', { day });
        }
      } catch (err) {
        if (action && action.startsWith('teardown-')) {
          showError('[data-teardown-status]', err);
        } else if (action && action.startsWith('offload-')) {
          showMessage('[data-offload-config-status]', offloadActionError(err));
        } else {
          showError('[data-operation-error]', err);
        }
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

    const teardownInput = root.querySelector('[data-teardown-input]');
    if (teardownInput) {
      teardownInput.addEventListener('input', updateTeardownConfirmState);
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
      return managedMode() ? 'management' : 'destination';
    }
    if (managedMode()) return 'management';
    return 'intro';
  }

  async function bind() {
    applyCopy(root, copy);
    renderIntroBullets(root, copy);
    renderRetentionGrid(root, copy);
    bindIntro();
    bindForms();
    bindBackendSwitching();
    bindModeSwitching();
    try {
      await refreshStatus();
    } catch (err) {
      if (window.logError) {
        window.logError(err, { context: 'backup initial status failed' });
      }
      renderStatus();
    }
    try {
      await refreshOffloadStatus();
    } catch (err) {
      if (window.logError) {
        window.logError(err, { context: 'backup offload initial status failed' });
      }
      renderOffloadUnavailable();
    }
    showPanel(initialPanel());
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
