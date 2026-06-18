// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(() => {
  const state = {
    providers: window.THINKING?.providers || {},
    keys: window.THINKING?.keys || {},
    localModels: [],
    localAvailability: null,
    scout: null,
    selectedByoProvider: '',
    byoMode: 'pick',
    pendingSwitchTarget: '',
  };
  const copy = window.THINKING_COPY || {};
  const scoutCopy = copy.scout || {};
  const scoutTerminalPhases = new Set(['invited', 'requested', 'ended', 'repair_needed']);
  const scoutPollIntervalMs = 1500;
  const scoutPollMaxMs = 15 * 60 * 1000;
  const views = new Set(['main', 'scout-setup', 'byo-setup', 'local-setup', 'lane-switch']);
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
  const providerTerms = {
    anthropic: 'https://www.anthropic.com/legal/commercial-terms',
    google: 'https://ai.google.dev/gemini-api/terms',
    openai: 'https://openai.com/policies/row-terms-of-use',
  };

  function $(id) {
    return document.getElementById(id);
  }

  function setText(id, message) {
    const el = $(id);
    if (!el) return;
    el.textContent = message || '';
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

  function setLink(id, url, text) {
    const el = $(id);
    if (!el) return;
    el.hidden = !url;
    el.href = url || '';
    el.textContent = url ? text : '';
  }

  function setHidden(id, hidden) {
    const el = $(id);
    if (!el) return;
    el.hidden = !!hidden;
  }

  function setButtonState(id, visible, disabled) {
    const button = $(id);
    if (!button) return;
    button.hidden = !visible;
    button.disabled = !!disabled;
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

  function showView(name, options = {}) {
    const target = views.has(name) ? name : 'main';
    document.querySelectorAll('#providers [data-view]').forEach((section) => {
      section.hidden = section.dataset.view !== target;
    });
    const nextHash = `#${target}`;
    if (window.location.hash !== nextHash) {
      if (options.replace) {
        window.history.replaceState(null, '', nextHash);
      } else {
        window.history.pushState(null, '', nextHash);
      }
    }
  }

  function viewFromHash() {
    const hash = window.location.hash.replace(/^#/, '');
    return views.has(hash) ? hash : 'main';
  }

  function providerLabel(provider) {
    return providerLabels[provider] || provider || 'provider';
  }

  function scoutLabel(stateName) {
    return (scoutCopy.state_labels || {})[stateName] || stateName || 'unknown';
  }

  function configuredProviders() {
    return Object.entries(state.keys.api_keys || {})
      .filter(([, configured]) => !!configured)
      .map(([provider]) => provider);
  }

  function selectedByoProvider() {
    const select = $('byoProvider');
    return state.selectedByoProvider || select?.value || defaultByoProvider();
  }

  function defaultByoProvider() {
    const generateProvider = state.providers.generate?.provider || '';
    const cogitateProvider = state.providers.cogitate?.provider || '';
    if (providerEnv[generateProvider]) return generateProvider;
    if (providerEnv[cogitateProvider]) return cogitateProvider;
    return configuredProviders()[0] || 'anthropic';
  }

  function laneProvider(lane) {
    if (lane === 'local') return 'local';
    if (lane === 'scout') return 'google';
    return selectedByoProvider();
  }

  function localReadiness() {
    const readiness = state.providers.ai_readiness?.local;
    if (readiness) {
      return {
        status: readiness.status || '',
        reason: readiness.reason_code || '',
        summary: readiness.summary || '',
        detail: readiness.detail || '',
      };
    }
    if (state.localAvailability?.available === true) {
      return {status: 'ready', reason: 'ready', summary: '', detail: ''};
    }
    if (state.localAvailability?.available === false) {
      return {
        status: 'blocked',
        reason: 'availability_unavailable',
        summary: state.localAvailability.reason || '',
        detail: '',
      };
    }
    return {status: '', reason: '', summary: '', detail: ''};
  }

  function localIsReady() {
    return state.providers.ai_readiness?.local?.status === 'ready';
  }

  function localIsGpuBlocked() {
    const reason = localReadiness().reason;
    return reason === 'gpu_unavailable' || reason === 'gpu_probe_failed';
  }

  function activeBrain() {
    const lane = state.providers.active_lane?.lane || 'advanced';
    const generateProvider = state.providers.generate?.provider || '';
    const cogitateProvider = state.providers.cogitate?.provider || '';
    const byoProvider = generateProvider !== 'local' ? generateProvider : cogitateProvider;
    const scoutUsable = !!state.providers.scout_enabled;
    const byoUsable = Object.values(state.keys.api_keys || {}).some(Boolean);
    const advancedUsable = lane === 'advanced' && !!generateProvider && !!cogitateProvider;

    if (lane === 'scout' && scoutUsable) {
      return {kind: 'scout', providerLabel: 'Gemini', label: 'solstone scout'};
    }
    if (lane === 'byo' && byoUsable) {
      return {kind: 'byo', provider: byoProvider, providerLabel: providerLabel(byoProvider)};
    }
    if (lane === 'local' && localIsReady()) {
      return {kind: 'local', providerLabel: 'Local'};
    }
    if (advancedUsable) {
      return {
        kind: 'advanced',
        generateProvider,
        cogitateProvider,
        providerLabel: 'Advanced split',
      };
    }
    return {kind: 'none', providerLabel: ''};
  }

  function laneIsUsable(lane) {
    if (lane === 'scout') return !!state.providers.scout_enabled;
    if (lane === 'byo') return configuredProviders().length > 0;
    if (lane === 'local') return localIsReady();
    return false;
  }

  function relativeTime(iso) {
    if (!iso) return '';
    const stamp = Date.parse(iso);
    if (Number.isNaN(stamp)) return '';
    const seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000));
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hours ago`;
    return shortDate(iso);
  }

  function shortDate(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}).toLowerCase();
  }

  function renderGlance() {
    const brain = activeBrain();
    const glance = $('brainGlance');
    if (glance) glance.classList.toggle('none', brain.kind === 'none');
    if (brain.kind === 'scout') {
      setText('thinkingActiveLane', 'sol is thinking with');
      setText('thinkingActiveValue', 'solstone scout');
      setText('thinkingActiveDetail', 'token set up through the scout program — stays in your journal');
    } else if (brain.kind === 'byo') {
      setText('thinkingActiveLane', 'sol is thinking with');
      setText('thinkingActiveValue', `your own key · ${brain.providerLabel}`);
      setText('thinkingActiveDetail', 'a key you added — stays in your journal, never shared');
    } else if (brain.kind === 'local') {
      setText('thinkingActiveLane', 'sol is thinking with');
      setText('thinkingActiveValue', 'a local model');
      setText('thinkingActiveDetail', 'runs in your journal — your data never leaves');
    } else if (brain.kind === 'advanced') {
      setText('thinkingActiveLane', 'sol is thinking with');
      setText('thinkingActiveValue', 'an advanced split');
      setText(
        'thinkingActiveDetail',
        `generate uses ${providerLabel(brain.generateProvider)}; cogitate uses ${providerLabel(brain.cogitateProvider)}`,
      );
    } else {
      setText('thinkingActiveLane', "sol can't think yet");
      setText('thinkingActiveValue', 'no provider chosen');
      setText(
        'thinkingActiveDetail',
        "sol can keep your journal — but it can't answer you until you pick one below.",
      );
    }
  }

  function setCardActive(lane, active) {
    const card = $(`lane-${lane}`);
    if (!card) return;
    card.classList.toggle('active', active);
    const tag = $(`${lane}ActiveTag`);
    if (tag) tag.hidden = !active;
  }

  function setPill(id, label, tone = '') {
    const pill = $(id);
    if (!pill) return;
    pill.textContent = label;
    pill.classList.toggle('hot', tone === 'hot');
    pill.classList.toggle('bad', tone === 'bad');
  }

  function renderMainLanes() {
    const brain = activeBrain();
    setText(
      'forkHint',
      brain.kind === 'none'
        ? "pick one — it's free with scout, or bring your own."
        : 'one at a time — the one with the dot is active right now.',
    );
    const scoutState = state.scout?.state || (state.providers.scout_enabled ? 'on' : 'off');
    setCardActive('scout', brain.kind === 'scout');
    setPill('scoutLaneStatus', scoutLabel(scoutState), brain.kind === 'scout' ? 'hot' : '');
    let scoutTrail = 'become a scout →';
    if (scoutState === 'requested') scoutTrail = 'check status →';
    if (scoutState === 'invited') scoutTrail = "you're in — turn on scout →";
    if (scoutState === 'on') scoutTrail = 'manage →';
    if (scoutState === 'ended') scoutTrail = 'check scout →';
    if (scoutState === 'manual_key_present') scoutTrail = 'review key →';
    if (scoutState === 'repair_needed') scoutTrail = 'repair →';
    setText('scoutLaneTrail', scoutTrail);

    const configured = configuredProviders();
    const activeByo = brain.kind === 'byo';
    const byoProvider = activeByo ? brain.provider : configured[0] || defaultByoProvider();
    setCardActive('byo', activeByo);
    setPill('byoLanePill', activeByo ? 'active' : 'off', activeByo ? 'hot' : '');
    if (activeByo) {
      setText('byoLaneDescription', 'your key from Claude, Gemini, or GPT — your billing, your control. stays in your journal.');
      setText('byoLaneStatus', `using ${providerLabel(byoProvider)} · manage →`);
    } else if (configured.length > 0) {
      setText('byoLaneDescription', 'your key from Claude, Gemini, or GPT — your billing, your control. stays in your journal.');
      setText('byoLaneStatus', `manage ${providerLabel(byoProvider)} key →`);
    } else {
      setText('byoLaneDescription', 'your key from Claude, Gemini, or GPT — treated equally. starts working right away.');
      setText('byoLaneStatus', 'add a key →');
    }

    const local = localReadiness();
    const localCard = $('lane-local');
    const localActive = brain.kind === 'local';
    const gpuBlocked = localIsGpuBlocked();
    setCardActive('local', localActive);
    if (localCard) {
      localCard.classList.toggle('greyed', gpuBlocked);
      localCard.setAttribute('aria-disabled', gpuBlocked ? 'true' : 'false');
    }
    if (localActive) {
      setPill('localLanePill', 'active', 'hot');
      setText('localLaneDescription', 'a model runs right in your journal — your data never leaves. this computer can run one.');
      setText('localLaneStatus', 'manage →');
    } else if (gpuBlocked) {
      setPill('localLanePill', 'unavailable', 'bad');
      const desc = $('localLaneDescription');
      if (desc) {
        desc.textContent = "this computer can't run a local model yet — it needs a supported GPU. ";
        const link = document.createElement('a');
        link.className = 'textlink';
        link.href = 'https://services.solstone.app/docs/local-requirements';
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = 'minimum requirements ↗';
        desc.appendChild(link);
      }
      setText('localLaneStatus', 'not available');
    } else if (local.status === 'ready') {
      setPill('localLanePill', 'off');
      setText('localLaneDescription', 'a model runs right in your journal — your data never leaves. this computer can run one.');
      setText('localLaneStatus', 'turn on local →');
    } else {
      setPill('localLanePill', 'off');
      setText('localLaneDescription', local.summary || 'checking whether this computer can run a local model.');
      setText('localLaneStatus', 'set up →');
    }
  }

  function checkstripTimeSegment(iso) {
    const time = relativeTime(iso);
    return time ? ` · ${time}` : '';
  }

  function renderScoutCheckstrip(scout) {
    const strip = $('scoutCheckstrip');
    const actions = scout.actions || {};
    const operation = scout.operation;
    const operationActive = !!operation && !scoutTerminalPhases.has(operation.phase);
    const connectionErrors = new Set(['unauthorized', 'not_found', 'unreachable', 'tls_failed', 'malformed']);
    let text = '';
    let visible = false;
    let checkVisible = false;

    if (connectionErrors.has(scout.check_error)) {
      text = "couldn't check over your scout connection · try again";
      visible = true;
      checkVisible = !!actions.check;
    } else if (scout.check_error === 'no_credential') {
      visible = false;
    } else if (scout.checked === true && scout.state === 'requested') {
      text = `checked over your scout connection${checkstripTimeSegment(scout.checked_at)} — still reviewing`;
      visible = true;
      checkVisible = !!actions.check;
    } else if (scout.checked === true && scout.state === 'invited') {
      text = `checked over your scout connection${checkstripTimeSegment(scout.checked_at)} — you're in 🎉`;
      visible = true;
      checkVisible = !!actions.check;
    }

    if (scout.state === 'on') {
      visible = false;
      checkVisible = false;
    }

    if (strip) strip.hidden = !visible;
    setText('scoutCheckstripText', text);
    setButtonState('scoutCheck', checkVisible, operationActive || !checkVisible);
  }

  function renderScoutNotice(scoutState) {
    const notice = $('scoutNotice');
    if (!notice) return;
    notice.textContent = '';
    if (scoutState === 'requested') {
      notice.append(
        document.createTextNode(
          "nothing's set up yet, and nothing left your journal. want to start now? ",
        ),
      );
      const byo = document.createElement('button');
      byo.type = 'button';
      byo.className = 'textlink';
      byo.textContent = 'add your own key.';
      byo.addEventListener('click', () => {
        state.byoMode = 'pick';
        renderByo();
        showView('byo-setup');
      });
      notice.appendChild(byo);
      return;
    }
    if (scoutState === 'invited') {
      notice.textContent = 'turning this on opens your browser to share your scout token with this journal. your questions are processed by a cloud provider, stored only briefly, never used for training.';
      return;
    }
    if (scoutState === 'on') {
      notice.textContent = 'the token is never shown here — it lives in your journal and you never have to touch it. the scout program only sets up the token; it never sees what you ask.';
      return;
    }
    if (scoutState === 'manual_key_present') {
      notice.textContent = scoutCopy.manual_key_block || 'a Gemini key you manage is already set.';
      return;
    }
    if (scoutState === 'repair_needed') {
      notice.textContent = 'scout needs a fresh check before it can be used here.';
      return;
    }
    notice.textContent = 'nothing is set up yet. scout can cover Gemini when you join the program.';
  }

  function renderScout() {
    const scout = state.scout;
    if (!scout) {
      setPill('scoutSetupPill', 'checking');
      setText('scoutSetupSub', 'checking scout');
      setText('scoutSetupMeta', '');
      setMessage('scoutLaneOperation', '');
      setLink('scoutLaneOperationLink', '', '');
      setHidden('scoutCheckstrip', true);
      setButtonState('scoutEnable', false, true);
      setButtonState('scoutRefresh', false, true);
      setButtonState('scoutDisable', false, true);
      setButtonState('scoutCheck', false, true);
      return;
    }

    const scoutState = scout.state || 'off';
    const label = scoutLabel(scoutState);
    setPill('scoutSetupPill', label, scoutState === 'on' ? 'hot' : '');
    setText('scoutSetupTitle', 'scout');

    const sublines = {
      off: 'off — pick scout if you want us to cover Gemini',
      requested: "your request is in — we'll show your invite here the moment you're in",
      invited: "you're in — turn it on so sol can think",
      on: 'on — sol can think',
      ended: 'scout ended — check again if this looks wrong',
      manual_key_present: scoutCopy.manual_key_block || 'a Gemini key you manage is already set.',
      repair_needed: 'repair needed — try again from Thinking',
    };
    setText('scoutSetupSub', sublines[scoutState] || scout.guidance || label);

    const provenance = scout.provenance || {};
    const setupDate = shortDate(provenance.key_created_at || provenance.enabled_at);
    setText(
      'scoutSetupMeta',
      scoutState === 'on'
        ? `token set up in your journal${setupDate ? ` · ${setupDate}` : ''}`
        : '',
    );

    renderScoutCheckstrip(scout);
    renderScoutNotice(scoutState);

    const operation = scout.operation;
    const operationActive = !!operation && !scoutTerminalPhases.has(operation.phase);
    const actions = scout.actions || {};
    setButtonState('scoutEnable', !!actions.enable && !operationActive, !actions.enable);
    setButtonState(
      'scoutRefresh',
      !!actions.refresh && scoutState !== 'on',
      operationActive || !actions.refresh || scoutState === 'on',
    );
    setButtonState('scoutDisable', !!actions.disable, operationActive || !actions.disable);

    if (operation) {
      const phase = operation.phase || '';
      const phaseLabel = scoutLabel(phase);
      const operationGuidance = operation.guidance || '';
      setMessage(
        'scoutLaneOperation',
        operationGuidance ? `${phaseLabel} — ${operationGuidance}` : phaseLabel,
        phase === 'repair_needed' ? 'error' : '',
      );
      setLink(
        'scoutLaneOperationLink',
        operation.portal_url || '',
        scoutCopy.consent_cta || 'continue to approve →',
      );
    } else {
      setMessage('scoutLaneOperation', '');
      setLink('scoutLaneOperationLink', '', '');
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

  function setSelectedByoProvider(provider) {
    state.selectedByoProvider = provider || defaultByoProvider();
    const select = $('byoProvider');
    if (select) select.value = state.selectedByoProvider;
  }

  function renderByo() {
    if (!state.selectedByoProvider) setSelectedByoProvider(defaultByoProvider());
    const provider = selectedByoProvider();
    const validation = state.keys.key_validation?.[provider];
    const configured = !!state.keys.api_keys?.[provider];
    const googleBlocked = provider === 'google' && state.keys.scout_enabled;
    const pickMode = state.byoMode === 'pick';

    setHidden('byoPickPanel', !pickMode);
    setHidden('byoProviderGrid', !pickMode);
    setHidden('byoPastePanel', pickMode);
    setText('byoBackLink', pickMode ? '‹ thinking' : '‹ pick a different provider');

    document.querySelectorAll('[data-provider-card]').forEach((card) => {
      const cardProvider = card.dataset.providerCard;
      const picked = cardProvider === provider;
      card.classList.toggle('active', picked);
      card.classList.toggle('greyed', cardProvider === 'google' && state.keys.scout_enabled);
      const pill = $(`prov-${cardProvider}-pill`);
      if (pill) {
        pill.textContent = picked ? 'selected' : (state.keys.api_keys?.[cardProvider] ? 'saved' : 'pick');
        pill.classList.toggle('hot', picked);
      }
    });

    setText('prov-google-desc', state.keys.scout_enabled
      ? 'Gemini is managed by scout right now. choose Claude or GPT for your own key.'
      : 'use a Google AI Studio key.');
    setText('byoPasteTitle', `paste your ${providerLabel(provider)} key`);
    setText('byoKeyLabel', `your ${providerLabel(provider)} key`);
    setText(
      'byoKeyHint',
      'it stays in your journal — the scout program never sets it up or sees it. paste it once; sol uses it from here.',
    );
    const terms = $('byoTermsLine');
    if (terms) {
      terms.textContent = `your questions will be processed by ${providerLabel(provider)}, stored only temporarily for processing, and never used for training. `;
      const link = document.createElement('a');
      link.className = 'textlink';
      link.href = providerTerms[provider] || providerTerms.anthropic;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'terms ↗';
      terms.appendChild(link);
    }

    $('byoSaveKey').disabled = googleBlocked;
    $('byoClearKey').disabled = !configured;
    if (validation && validation.valid === false) {
      setMessage(
        'byoLaneStatus',
        `${providerLabel(provider)}: ${validation.reason_code || validation.error || 'invalid'}`,
        'error',
      );
    } else if (googleBlocked) {
      setMessage('byoLaneStatus', 'Gemini is managed by scout; choose Claude or GPT.', 'error');
    } else {
      setMessage(
        'byoLaneStatus',
        configured ? `${providerLabel(provider)} key saved — replace, clear, or validate it here` : 'paste a key to use this provider',
        configured ? 'ok' : '',
      );
    }
  }

  function localCopy() {
    const local = localReadiness();
    const reason = local.reason;
    if (local.status === 'ready' || reason === 'ready') {
      return {
        pill: 'off',
        title: 'local',
        sub: 'this computer can run a local model',
        message: '',
        notice: 'your data never leaves your journal — the model runs on this computer, offline. no cloud, no key, no billing.',
        activate: true,
        bootstrap: false,
        tone: '',
      };
    }
    if (reason === 'gpu_unavailable') {
      return {
        pill: 'unavailable',
        title: 'local',
        sub: "this computer can't run one yet",
        message: '',
        notice: "this computer doesn't have a supported GPU, so a local model would be too slow to use. sol can still think for free with scout or your own key.",
        activate: false,
        bootstrap: false,
        tone: 'bad',
      };
    }
    if (reason === 'gpu_probe_failed') {
      return {
        pill: 'unavailable',
        title: 'local',
        sub: "this computer can't run one yet",
        message: '',
        notice: "couldn't check this computer's GPU. sol can still think for free with scout or your own key.",
        activate: false,
        bootstrap: false,
        tone: 'bad',
      };
    }
    if (reason === 'local_model_installing') {
      return {
        pill: 'setting up',
        title: 'local',
        sub: 'setting up a local model…',
        message: local.detail || local.summary || '',
        notice: 'local thinking will stay in your journal once setup finishes.',
        activate: false,
        bootstrap: false,
        tone: '',
      };
    }
    if (reason === 'local_model_loading') {
      return {
        pill: 'starting',
        title: 'local',
        sub: 'starting the local model…',
        message: local.detail || local.summary || '',
        notice: 'local thinking will stay in your journal once the model is ready.',
        activate: false,
        bootstrap: false,
        tone: '',
      };
    }
    if (reason === 'local_model_missing') {
      return {
        pill: 'setup needed',
        title: 'local',
        sub: 'local model files are not installed yet',
        message: local.detail || local.summary || '',
        notice: 'install the selected model before turning on local thinking.',
        activate: false,
        bootstrap: true,
        tone: '',
      };
    }
    if (reason === 'local_endpoint_unreachable') {
      return {
        pill: 'not ready',
        title: 'local',
        sub: "your local endpoint didn't answer",
        message: local.detail || local.summary || '',
        notice: 'check the endpoint in advanced controls, then try again.',
        activate: false,
        bootstrap: false,
        tone: 'bad',
      };
    }
    if (reason === 'local_server_unhealthy') {
      return {
        pill: 'not ready',
        title: 'local',
        sub: "local thinking isn't ready yet",
        message: local.detail || local.summary || '',
        notice: 'check again after the local service settles.',
        activate: false,
        bootstrap: false,
        tone: 'bad',
      };
    }
    if (reason === 'ram_insufficient') {
      return {
        pill: 'unavailable',
        title: 'local',
        sub: 'this computer needs more memory for local thinking',
        message: '',
        notice: 'sol can still think for free with scout or your own key.',
        activate: false,
        bootstrap: false,
        tone: 'bad',
      };
    }
    return {
      pill: 'checking',
      title: 'local',
      sub: local.summary || state.localAvailability?.reason || 'checking local readiness.',
      message: '',
      notice: '',
      activate: false,
      bootstrap: false,
      tone: '',
    };
  }

  function renderLocal() {
    const local = localCopy();
    setPill('localSetupPill', local.pill, local.tone);
    setText('localSetupTitle', local.title);
    setText('localSetupSub', local.sub);
    setMessage('localSetupMessage', local.message, local.tone === 'bad' ? 'error' : '');
    setText('localNotice', local.notice);
    setButtonState('localBootstrap', local.bootstrap, !local.bootstrap);
    setButtonState('localActivate', local.activate, !local.activate);
    setButtonState('localRefresh', true, false);
    const links = $('localSetupLinks');
    if (links) {
      links.textContent = '';
      if (local.tone === 'bad') {
        const requirements = document.createElement('a');
        requirements.className = 'textlink';
        requirements.href = 'https://services.solstone.app/docs/local-requirements';
        requirements.target = '_blank';
        requirements.rel = 'noopener noreferrer';
        requirements.textContent = 'minimum requirements ↗';
        const scout = document.createElement('button');
        scout.type = 'button';
        scout.className = 'textlink';
        scout.dataset.openView = 'scout-setup';
        scout.textContent = ' scout';
        scout.addEventListener('click', () => showView('scout-setup'));
        const byo = document.createElement('button');
        byo.type = 'button';
        byo.className = 'textlink';
        byo.dataset.openView = 'byo-setup';
        byo.textContent = ' your own key';
        byo.addEventListener('click', () => showView('byo-setup'));
        links.append(requirements, document.createTextNode(' or use'), scout, document.createTextNode(' /'), byo);
      }
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

  function renderLaneSwitch() {
    const brain = activeBrain();
    const target = state.pendingSwitchTarget || '';
    const targetProvider = target === 'byo' ? selectedByoProvider() : laneProvider(target);
    const currentLabel = brain.kind === 'none' ? 'no provider chosen' : brain.providerLabel || brain.kind;
    const targetLabel = target === 'byo'
      ? `your own key · ${providerLabel(targetProvider)}`
      : target === 'scout'
        ? 'solstone scout'
        : target === 'local'
          ? 'local'
          : target;
    setText('switchCurrentLabel', currentLabel);
    setText('switchTargetLabel', targetLabel);
    if (target === 'byo') {
      setText('switchNote', `sol will think with ${targetLabel} from now on. your ${providerLabel(targetProvider)} key stays saved in your journal — switch back anytime without re-pasting it.`);
    } else if (target === 'scout') {
      setText('switchNote', 'sol will think with solstone scout from now on. your scout token stays saved in your journal — switch back anytime.');
    } else if (target === 'local') {
      setText('switchNote', 'sol will think with local from now on. local setup stays saved in your journal — switch back anytime.');
    } else {
      setText('switchNote', 'you can switch back anytime.');
    }
    const primary = $('switchConfirmPrimary');
    if (primary) {
      primary.dataset.switchLane = target;
      primary.textContent = `switch to ${targetLabel || 'lane'}`;
    }
    const cancel = $('switchCancel');
    if (cancel) {
      cancel.textContent = `keep using ${currentLabel}`;
    }
  }

  function renderAll() {
    renderGlance();
    renderMainLanes();
    renderAdvanced();
    renderByo();
    renderLocalEndpoint();
    renderScout();
    renderLocal();
    renderLaneSwitch();
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
    renderMainLanes();
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

  function openConsentTab(operation) {
    const url = operation?.portal_url;
    if (url) window.open(url, '_blank', 'noopener');
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

  async function activateLane(target) {
    const brain = activeBrain();
    if (brain.kind !== 'none' && brain.kind !== target && laneIsUsable(target)) {
      state.pendingSwitchTarget = target;
      renderLaneSwitch();
      showView('lane-switch');
      return;
    }
    await switchLane(target);
    showView('main');
  }

  async function enableScout() {
    setMessage('scoutLaneOperation', '');
    let start;
    try {
      start = await api('api/scout/enable', {method: 'POST'});
    } catch (err) {
      setMessage('scoutLaneOperation', err.message, 'error');
      return;
    }
    openConsentTab(start?.operation);

    const operation = await pollScoutUntilTerminal();
    const phase = operation?.phase;
    if (state.scout?.state === 'on' || phase === 'invited') {
      await switchLane('scout');
      await Promise.all([refreshScout(), refreshProviders(), refreshKeys()]);
      showView('main');
      return;
    }
    if (phase === 'repair_needed') {
      setMessage(
        'scoutLaneOperation',
        operation?.guidance || 'Scout needs repair — try again from Thinking.',
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
    const start = await api('api/scout/refresh', {method: 'POST'});
    openConsentTab(start?.operation);
    await pollScoutUntilTerminal();
    if (state.scout?.state === 'on') {
      await Promise.all([refreshProviders(), refreshKeys()]);
    }
    renderScout();
    renderMainLanes();
  }

  async function checkScout() {
    state.scout = await api('api/scout/check', {method: 'POST'});
    renderScout();
    renderMainLanes();
  }

  async function disableScout() {
    const result = await api('api/scout/disable', {method: 'POST'});
    state.scout = result.status || state.scout;
    await Promise.all([refreshScout(), refreshProviders(), refreshKeys()]);
    showView('main');
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
    await switchLane('byo');
    await Promise.all([refreshProviders(), refreshKeys()]);
    showView('main');
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
    await Promise.all([refreshProviders(), refreshLocalAvailability()]);
  }

  function openLane(lane) {
    if (lane === 'local' && localIsGpuBlocked()) return;
    if (laneIsUsable(lane) && activeBrain().kind !== lane) {
      activateLane(lane).catch((err) => setMessage(`${lane}LaneStatus`, err.message, 'error'));
      return;
    }
    if (lane === 'byo') {
      state.byoMode = configuredProviders().length > 0 || activeBrain().kind === 'byo' ? 'paste' : 'pick';
      renderByo();
    }
    showView(`${lane}-setup`);
  }

  function bindOpenView(el) {
    el.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const lane = el.dataset.lane || el.closest('[data-lane]')?.dataset.lane;
      if (lane) {
        openLane(lane);
      } else {
        showView(el.dataset.openView || 'main');
      }
    });
    el.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      const lane = el.dataset.lane || el.closest('[data-lane]')?.dataset.lane;
      if (lane) {
        openLane(lane);
      } else {
        showView(el.dataset.openView || 'main');
      }
    });
  }

  function bind() {
    document.querySelectorAll('[data-open-view]').forEach(bindOpenView);
    document.querySelectorAll('[data-byo-provider]').forEach((button) => {
      button.addEventListener('click', () => {
        setSelectedByoProvider(button.dataset.byoProvider);
        state.byoMode = 'paste';
        renderByo();
      });
    });
    document.querySelectorAll('[data-switch-lane]').forEach((button) => {
      button.addEventListener('click', () => {
        const lane = button.dataset.switchLane;
        if (!lane) return;
        switchLane(lane)
          .then(() => showView('main'))
          .catch((err) => setMessage(`${lane}LaneStatus`, err.message, 'error'));
      });
    });
    $('byoProvider')?.addEventListener('change', () => {
      state.selectedByoProvider = $('byoProvider')?.value || defaultByoProvider();
      state.byoMode = 'paste';
      renderByo();
      renderMainLanes();
    });
    $('byoBackLink')?.addEventListener('click', () => {
      if (state.byoMode === 'paste') {
        state.byoMode = 'pick';
        renderByo();
        return;
      }
      showView('main');
    });
    $('byoSaveKey')?.addEventListener('click', () => saveByoKey().catch((err) => setMessage('byoLaneStatus', err.message, 'error')));
    $('byoClearKey')?.addEventListener('click', () => clearByoKey().catch((err) => setMessage('byoLaneStatus', err.message, 'error')));
    $('byoValidateKey')?.addEventListener('click', () => validateKeys().catch((err) => setMessage('byoLaneStatus', err.message, 'error')));
    $('scoutEnable')?.addEventListener('click', () => enableScout().catch((err) => setMessage('scoutLaneOperation', err.message, 'error')));
    $('scoutRefresh')?.addEventListener('click', () => refreshScoutOp().catch((err) => setMessage('scoutLaneOperation', err.message, 'error')));
    $('scoutDisable')?.addEventListener('click', () => disableScout().catch((err) => setMessage('scoutLaneOperation', err.message, 'error')));
    $('scoutCheck')?.addEventListener('click', () => checkScout().catch((err) => setMessage('scoutLaneOperation', err.message, 'error')));
    $('localRefresh')?.addEventListener('click', () => Promise.all([
      refreshProviders(),
      refreshLocalAvailability(),
    ]).catch((err) => setMessage('localSetupMessage', err.message, 'error')));
    $('localBootstrap')?.addEventListener('click', () => startLocalBootstrap().catch((err) => setMessage('localSetupMessage', err.message, 'error')));
    $('localActivate')?.addEventListener('click', () => activateLane('local').catch((err) => setMessage('localSetupMessage', err.message, 'error')));
    $('localModelSelect')?.addEventListener('change', () => Promise.all([
      refreshLocalAvailability(),
      refreshProviders(),
    ]).catch((err) => setMessage('localSetupMessage', err.message, 'error')));
    $('field-generate-provider')?.addEventListener('change', (event) => saveAdvanced('generate', 'provider', event.target.value).catch((err) => setMessage('advancedStatus', err.message, 'error')));
    $('field-cogitate-provider')?.addEventListener('change', (event) => saveAdvanced('cogitate', 'provider', event.target.value).catch((err) => setMessage('advancedStatus', err.message, 'error')));
    $('field-generate-tier')?.addEventListener('change', (event) => saveAdvanced('generate', 'tier', event.target.value).catch((err) => setMessage('advancedStatus', err.message, 'error')));
    $('field-cogitate-tier')?.addEventListener('change', (event) => saveAdvanced('cogitate', 'tier', event.target.value).catch((err) => setMessage('advancedStatus', err.message, 'error')));
    $('field-google-backend')?.addEventListener('change', () => saveGoogleBackend().catch((err) => setMessage('vertexStatus', err.message, 'error')));
    $('vertexSave')?.addEventListener('click', () => saveVertexCredentials().catch((err) => setMessage('vertexStatus', err.message, 'error')));
    $('vertexClear')?.addEventListener('click', () => clearVertexCredentials().catch((err) => setMessage('vertexStatus', err.message, 'error')));
    $('localEndpointSave')?.addEventListener('click', () => saveLocalEndpoint().catch((err) => setMessage('localEndpointStatus', err.message, 'error')));
    $('localEndpointClear')?.addEventListener('click', () => clearLocalEndpoint().catch((err) => setMessage('localEndpointStatus', err.message, 'error')));
    window.addEventListener('hashchange', () => showView(viewFromHash(), {replace: true}));
  }

  async function init() {
    bind();
    setSelectedByoProvider(defaultByoProvider());
    renderAll();
    showView(viewFromHash(), {replace: true});
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
