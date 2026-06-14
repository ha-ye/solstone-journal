// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(() => {
  const state = {
    providers: window.THINKING?.providers || {},
    keys: window.THINKING?.keys || {},
    localModels: [],
    localAvailability: null,
    scout: null,
  };
  const copy = window.THINKING_COPY || {};
  const scoutCopy = copy.scout || {};
  const scoutTerminalPhases = new Set(['invited', 'requested', 'ended', 'repair_needed']);
  const scoutPollIntervalMs = 1500;
  const scoutPollMaxMs = 15 * 60 * 1000;
  const providerEnv = {
    anthropic: 'ANTHROPIC_API_KEY',
    google: 'GOOGLE_API_KEY',
    openai: 'OPENAI_API_KEY',
  };
  const providerLabels = copy.provider_labels || {
    anthropic: 'Claude',
    google: 'Gemini',
    openai: 'GPT',
    local: 'Local',
  };

  function $(id) {
    return document.getElementById(id);
  }

  function setMessage(id, message, tone = '') {
    const el = $(id);
    if (!el) return;
    el.textContent = message || '';
    if (tone) {
      el.dataset.tone = tone;
    } else {
      el.removeAttribute('data-tone');
    }
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        ...(options.body ? {'Content-Type': 'application/json'} : {}),
        ...(options.headers || {}),
      },
    });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.detail || payload.error || 'request failed');
    }
    return payload;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function activeLaneLabel(lane) {
    return (copy.active_lane_labels || {})[lane] || lane || 'unknown';
  }

  function laneProvider(lane) {
    if (lane === 'local') return 'local';
    if (lane === 'scout') return 'google';
    return $('byoProvider')?.value || 'anthropic';
  }

  function laneStatus(lane) {
    const active = state.providers.active_lane?.lane;
    if (active === lane) return 'active';
    if (lane === 'scout') {
      return state.providers.scout_enabled ? 'available' : 'not ready';
    }
    if (lane === 'local') {
      return localStatusText();
    }
    const provider = laneProvider('byo');
    return state.keys.api_keys?.[provider] ? 'available' : 'key needed';
  }

  function localStatusText() {
    const readiness = state.providers.ai_readiness?.local;
    const reason = readiness?.reason_code;
    if (reason === 'gpu_probe_failed') return "GPU check couldn't finish";
    if (reason === 'gpu_unavailable') return 'GPU acceleration unavailable';
    if (reason === 'ram_insufficient') return 'more memory needed';
    if (state.localAvailability?.available === false) {
      return state.localAvailability.reason || 'setup unavailable';
    }
    if (readiness?.state === 'ready' || readiness?.status === 'ready') return 'available';
    const local = state.providers.local || {};
    if (local.install_state && local.install_state !== 'installed') {
      return local.install_state;
    }
    return state.providers.provider_status?.local?.cogitate_ready ? 'available' : 'not ready';
  }

  function renderActiveLane() {
    const active = state.providers.active_lane?.lane || 'advanced';
    $('thinkingActiveLane').textContent = activeLaneLabel(active);
    const detail = active === 'advanced'
      ? 'generate and cogitate are split in advanced settings'
      : '';
    setMessage('thinkingActiveDetail', detail);
    document.querySelectorAll('.thinking-lane').forEach((lane) => {
      lane.dataset.active = lane.dataset.lane === active ? 'true' : 'false';
    });
    setMessage('scoutLaneStatus', laneStatus('scout'));
    setMessage('byoLaneStatus', laneStatus('byo'));
    setMessage('localLaneStatus', laneStatus('local'));
  }

  function scoutLabel(stateName) {
    return (scoutCopy.state_labels || {})[stateName] || stateName || 'unknown';
  }

  function setButtonState(id, visible, disabled) {
    const button = $(id);
    if (!button) return;
    button.hidden = !visible;
    button.disabled = !!disabled;
  }

  function renderScout() {
    const scout = state.scout;
    if (!scout) {
      setMessage('scoutLaneStatus', laneStatus('scout'));
      setMessage('scoutLaneOperation', '');
      setButtonState('scoutEnable', false, true);
      setButtonState('scoutRefresh', false, true);
      setButtonState('scoutDisable', false, true);
      setButtonState('scoutCheck', false, true);
      const switchButton = document.querySelector('#lane-scout [data-switch-lane="scout"]');
      if (switchButton) {
        switchButton.hidden = true;
        switchButton.disabled = true;
      }
      return;
    }

    const scoutState = scout.state || '';
    const label = scoutLabel(scoutState);
    const guidance = scout.guidance || (scoutCopy.resting_guidance || {})[scoutState] || '';
    setMessage('scoutLaneStatus', guidance ? `${label} - ${guidance}` : label);

    const operation = scout.operation;
    const operationActive = !!operation && !scoutTerminalPhases.has(operation.phase);
    const actions = scout.actions || {};
    setButtonState('scoutEnable', !!actions.enable, operationActive || !actions.enable);
    setButtonState('scoutRefresh', !!actions.refresh, operationActive || !actions.refresh);
    setButtonState('scoutDisable', !!actions.disable, operationActive || !actions.disable);
    setButtonState('scoutCheck', !!actions.check, operationActive || !actions.check);

    const switchButton = document.querySelector('#lane-scout [data-switch-lane="scout"]');
    if (switchButton) {
      switchButton.hidden = scoutState !== 'on';
      switchButton.disabled = scoutState !== 'on';
    }

    if (operation) {
      const phase = operation.phase || '';
      const phaseLabel = scoutLabel(phase);
      const operationGuidance = operation.guidance || '';
      setMessage(
        'scoutLaneOperation',
        operationGuidance ? `${phaseLabel} - ${operationGuidance}` : phaseLabel,
        phase === 'repair_needed' ? 'error' : '',
      );
    } else {
      setMessage('scoutLaneOperation', '');
    }
  }

  function populateProviderSelect(select, selected) {
    if (!select) return;
    select.innerHTML = '';
    for (const provider of state.providers.providers || []) {
      const option = document.createElement('option');
      option.value = provider.name;
      option.textContent = providerLabels[provider.name] || provider.label || provider.name;
      select.appendChild(option);
    }
    select.value = selected || '';
  }

  function renderAdvanced() {
    populateProviderSelect($('field-generate-provider'), state.providers.generate?.provider);
    populateProviderSelect($('field-cogitate-provider'), state.providers.cogitate?.provider);
    if ($('field-generate-tier')) {
      $('field-generate-tier').value = String(state.providers.generate?.tier || 2);
    }
    if ($('field-cogitate-tier')) {
      $('field-cogitate-tier').value = String(state.providers.cogitate?.tier || 2);
    }
    if ($('field-google-backend')) {
      $('field-google-backend').value = state.providers.google_backend || 'auto';
    }
  }

  function renderKeys() {
    const provider = $('byoProvider')?.value || 'anthropic';
    const validation = state.keys.key_validation?.[provider];
    const configured = !!state.keys.api_keys?.[provider];
    if (provider === 'google' && state.keys.scout_enabled) {
      setMessage('byoLaneStatus', 'Gemini is managed by Scout; choose Claude or GPT.', 'error');
      $('byoSaveKey').disabled = true;
      return;
    }
    $('byoSaveKey').disabled = false;
    if (validation && validation.valid === false) {
      setMessage(
        'byoLaneStatus',
        `${providerLabels[provider] || provider}: ${validation.reason_code || validation.error || 'invalid'}`,
        'error',
      );
    } else {
      setMessage(
        'byoLaneStatus',
        configured ? `${providerLabels[provider] || provider} key saved` : 'key needed',
        configured ? 'ok' : '',
      );
    }
  }

  function renderLocalEndpoint() {
    const endpoint = state.providers.local_override || {};
    if ($('localEndpointUrl')) $('localEndpointUrl').value = endpoint.endpoint_url || '';
    if ($('localEndpointModel')) $('localEndpointModel').value = endpoint.served_model_id || '';
  }

  function renderLocalModels() {
    const select = $('localModelSelect');
    if (!select) return;
    select.innerHTML = '';
    for (const model of state.localModels) {
      const option = document.createElement('option');
      option.value = model.name;
      option.textContent = model.label || model.name;
      select.appendChild(option);
    }
  }

  function renderAll() {
    renderActiveLane();
    renderAdvanced();
    renderKeys();
    renderLocalEndpoint();
    renderScout();
  }

  async function refreshProviders() {
    const model = $('localModelSelect')?.value;
    const suffix = model ? `?local_model=${encodeURIComponent(model)}` : '';
    state.providers = await api(`api/providers${suffix}`);
    renderAll();
  }

  async function refreshKeys() {
    state.keys = await api('api/keys');
    renderAll();
  }

  async function refreshScout() {
    state.scout = await api('api/scout');
    renderScout();
  }

  async function pollScoutUntilTerminal() {
    const started = Date.now();
    while (Date.now() - started < scoutPollMaxMs) {
      await refreshScout();
      const operation = state.scout?.operation;
      if (!operation || scoutTerminalPhases.has(operation.phase)) return operation || null;
      await sleep(scoutPollIntervalMs);
    }
    await refreshScout();
    return state.scout?.operation || null;
  }

  async function refreshLocalModels() {
    state.localModels = await api('api/local/models');
    renderLocalModels();
  }

  async function refreshLocalAvailability() {
    const model = $('localModelSelect')?.value || '';
    const suffix = model ? `?model=${encodeURIComponent(model)}` : '';
    state.localAvailability = await api(`api/local/availability${suffix}`);
    renderAll();
  }

  async function switchLane(lane) {
    const payload = {lane};
    if (lane === 'byo') {
      payload.provider = laneProvider('byo');
    }
    state.providers = await api('api/providers', {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    renderAll();
  }

  async function enableScout() {
    setMessage('scoutLaneOperation', '');
    try {
      await api('api/scout/enable', {method: 'POST'});
    } catch (err) {
      setMessage('scoutLaneOperation', err.message, 'error');
      return;
    }

    const operation = await pollScoutUntilTerminal();
    const phase = operation?.phase;
    if (state.scout?.state === 'on' || phase === 'invited') {
      await switchLane('scout');
      await Promise.all([refreshScout(), refreshProviders(), refreshKeys()]);
      return;
    }
    if (phase === 'repair_needed') {
      setMessage(
        'scoutLaneOperation',
        operation?.guidance || 'Scout needs repair; try again from Thinking.',
        'error',
      );
      return;
    }
    if (phase === 'requested') {
      setMessage(
        'scoutLaneOperation',
        operation?.guidance || state.scout?.guidance || 'Scout is waiting for approval.',
      );
    }
  }

  async function refreshScoutOp() {
    await api('api/scout/refresh', {method: 'POST'});
    await pollScoutUntilTerminal();
    if (state.scout?.state === 'on') {
      await Promise.all([refreshProviders(), refreshKeys()]);
    }
    renderScout();
  }

  async function checkScout() {
    state.scout = await api('api/scout/check', {method: 'POST'});
    renderScout();
  }

  async function disableScout() {
    const result = await api('api/scout/disable', {method: 'POST'});
    state.scout = result.status || state.scout;
    await Promise.all([refreshScout(), refreshProviders(), refreshKeys()]);
  }

  async function saveByoKey() {
    const provider = laneProvider('byo');
    const value = $('byoKeyInput')?.value || '';
    const result = await api('api/keys', {
      method: 'PUT',
      body: JSON.stringify({env_var: providerEnv[provider], value}),
    });
    state.keys = result;
    if ($('byoKeyInput')) $('byoKeyInput').value = '';
    renderAll();
  }

  async function clearByoKey() {
    const provider = laneProvider('byo');
    const result = await api('api/keys', {
      method: 'PUT',
      body: JSON.stringify({env_var: providerEnv[provider], value: ''}),
    });
    state.keys = result;
    renderAll();
  }

  async function validateKeys() {
    const result = await api('api/validate-keys', {method: 'POST'});
    state.keys.key_validation = result.key_validation || {};
    renderAll();
  }

  async function saveAdvanced(agentType, field, value) {
    const payload = {[agentType]: {[field]: field === 'tier' ? Number(value) : value}};
    state.providers = await api('api/providers', {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    setMessage('advancedStatus', 'saved', 'ok');
    renderAll();
  }

  async function saveGoogleBackend() {
    state.providers = await api('api/providers', {
      method: 'PUT',
      body: JSON.stringify({google_backend: $('field-google-backend')?.value || 'auto'}),
    });
    setMessage('vertexStatus', 'backend saved', 'ok');
    renderAll();
  }

  async function saveVertexCredentials() {
    const value = $('vertexCredsInput')?.value || '';
    state.providers = await api('api/providers', {
      method: 'PUT',
      body: JSON.stringify({vertex_credentials: value}),
    });
    if ($('vertexCredsInput')) $('vertexCredsInput').value = '';
    setMessage('vertexStatus', 'credentials saved', 'ok');
    renderAll();
  }

  async function clearVertexCredentials() {
    state.providers = await api('api/providers', {
      method: 'PUT',
      body: JSON.stringify({vertex_credentials: ''}),
    });
    setMessage('vertexStatus', 'credentials cleared', 'ok');
    renderAll();
  }

  async function saveLocalEndpoint() {
    const payload = {
      endpoint_url: $('localEndpointUrl')?.value || '',
      served_model_id: $('localEndpointModel')?.value || '',
    };
    const credential = $('localEndpointCredential')?.value;
    if (credential) payload.credential = credential;
    const result = await api('api/local/endpoint', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    state.providers.local_override = result.local_endpoint || {};
    if ($('localEndpointCredential')) $('localEndpointCredential').value = '';
    setMessage('localEndpointStatus', 'endpoint saved', 'ok');
    renderLocalEndpoint();
  }

  async function clearLocalEndpoint() {
    const result = await api('api/local/endpoint', {method: 'DELETE'});
    state.providers.local_override = result.local_endpoint || {};
    setMessage('localEndpointStatus', 'endpoint cleared', 'ok');
    renderLocalEndpoint();
  }

  async function startLocalBootstrap() {
    const model = $('localModelSelect')?.value || '';
    await api(`api/local/bootstrap?model=${encodeURIComponent(model)}`, {method: 'POST'});
    await refreshProviders();
  }

  function bind() {
    document.querySelectorAll('[data-switch-lane]').forEach((button) => {
      button.addEventListener('click', () => switchLane(button.dataset.switchLane).catch((err) => {
        setMessage(`${button.dataset.switchLane}LaneStatus`, err.message, 'error');
      }));
    });
    $('byoProvider')?.addEventListener('change', () => {
      renderKeys();
      renderActiveLane();
    });
    $('byoSaveKey')?.addEventListener('click', () => saveByoKey().catch((err) => setMessage('byoLaneStatus', err.message, 'error')));
    $('byoClearKey')?.addEventListener('click', () => clearByoKey().catch((err) => setMessage('byoLaneStatus', err.message, 'error')));
    $('byoValidateKey')?.addEventListener('click', () => validateKeys().catch((err) => setMessage('byoLaneStatus', err.message, 'error')));
    $('scoutEnable')?.addEventListener('click', () => enableScout().catch((err) => setMessage('scoutLaneOperation', err.message, 'error')));
    $('scoutRefresh')?.addEventListener('click', () => refreshScoutOp().catch((err) => setMessage('scoutLaneOperation', err.message, 'error')));
    $('scoutDisable')?.addEventListener('click', () => disableScout().catch((err) => setMessage('scoutLaneOperation', err.message, 'error')));
    $('scoutCheck')?.addEventListener('click', () => checkScout().catch((err) => setMessage('scoutLaneOperation', err.message, 'error')));
    $('localRefresh')?.addEventListener('click', () => refreshProviders().catch((err) => setMessage('localLaneStatus', err.message, 'error')));
    $('localBootstrap')?.addEventListener('click', () => startLocalBootstrap().catch((err) => setMessage('localLaneStatus', err.message, 'error')));
    $('localModelSelect')?.addEventListener('change', () => Promise.all([
      refreshLocalAvailability(),
      refreshProviders(),
    ]).catch((err) => setMessage('localLaneStatus', err.message, 'error')));
    $('field-generate-provider')?.addEventListener('change', (event) => saveAdvanced('generate', 'provider', event.target.value).catch((err) => setMessage('advancedStatus', err.message, 'error')));
    $('field-cogitate-provider')?.addEventListener('change', (event) => saveAdvanced('cogitate', 'provider', event.target.value).catch((err) => setMessage('advancedStatus', err.message, 'error')));
    $('field-generate-tier')?.addEventListener('change', (event) => saveAdvanced('generate', 'tier', event.target.value).catch((err) => setMessage('advancedStatus', err.message, 'error')));
    $('field-cogitate-tier')?.addEventListener('change', (event) => saveAdvanced('cogitate', 'tier', event.target.value).catch((err) => setMessage('advancedStatus', err.message, 'error')));
    $('field-google-backend')?.addEventListener('change', () => saveGoogleBackend().catch((err) => setMessage('vertexStatus', err.message, 'error')));
    $('vertexSave')?.addEventListener('click', () => saveVertexCredentials().catch((err) => setMessage('vertexStatus', err.message, 'error')));
    $('vertexClear')?.addEventListener('click', () => clearVertexCredentials().catch((err) => setMessage('vertexStatus', err.message, 'error')));
    $('localEndpointSave')?.addEventListener('click', () => saveLocalEndpoint().catch((err) => setMessage('localEndpointStatus', err.message, 'error')));
    $('localEndpointClear')?.addEventListener('click', () => clearLocalEndpoint().catch((err) => setMessage('localEndpointStatus', err.message, 'error')));
  }

  async function init() {
    bind();
    renderAll();
    try {
      await refreshLocalModels();
      await refreshLocalAvailability();
      await Promise.all([refreshProviders(), refreshKeys(), refreshScout()]);
    } catch (err) {
      setMessage('thinkingActiveDetail', err.message, 'error');
    }
  }

  init();
})();
