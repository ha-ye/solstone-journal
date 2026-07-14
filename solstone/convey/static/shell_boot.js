// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function () {
  const DAY_RE = /^\d{8}$/;

  function pathContext() {
    const parts = window.location.pathname.split('/');
    const isAppPath = parts[1] === 'app' && parts[2];
    const segment = isAppPath && parts[3] ? decodeURIComponent(parts[3]) : null;
    return {
      appName: isAppPath ? decodeURIComponent(parts[2]) : null,
      segment,
      day: segment && DAY_RE.test(segment) ? segment : null
    };
  }

  window.solPathContext = pathContext;

  function findApp(shell, appName) {
    return (shell.apps || []).find((app) => app.name === appName) || null;
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function copyText(key, fallback) {
    return window.CONVEY_COPY?.[key] || fallback;
  }

  function applyChromeCopy() {
    const statusLink = document.getElementById('status-pane-console-link');
    const consoleLabel = copyText('CONSOLE_LINK_LABEL', 'system messages');
    if (statusLink) {
      statusLink.textContent = consoleLabel;
      statusLink.setAttribute('aria-label', consoleLabel);
    }

    const consoleHeading = copyText('CONSOLE_HEADING', 'system messages');
    const title = document.getElementById('diagnostic-console-title');
    if (title) title.textContent = consoleHeading;
    const tabs = document.querySelector('.diagnostic-console-tabs');
    if (tabs) tabs.setAttribute('aria-label', consoleHeading);

    const actions = {
      clear: copyText('CONSOLE_ACTION_CLEAR', 'Clear'),
      'send-all': copyText('CONSOLE_ACTION_SEND_ALL', 'Send all')
    };
    for (const [action, label] of Object.entries(actions)) {
      const button = document.querySelector(`[data-diagnostic-action="${action}"]`);
      if (button) button.textContent = label;
    }
    const close = document.querySelector('[data-diagnostic-action="close"]');
    if (close) {
      close.setAttribute('aria-label', copyText('CONSOLE_ACTION_CLOSE', 'Close'));
    }

    const reportingOff = document.querySelector('[data-diagnostic-reporting-off]');
    if (reportingOff) {
      reportingOff.textContent = copyText(
        'CONSOLE_REPORTING_OFF',
        'I can show these messages, but sending reports is off.'
      );
    }

    const tabLabels = {
      all: copyText('CONSOLE_TAB_ALL', 'all'),
      error: copyText('CONSOLE_TAB_ERRORS', 'errors'),
      warning: copyText('CONSOLE_TAB_WARNINGS', 'warnings'),
      info: copyText('CONSOLE_TAB_INFO', 'info')
    };
    for (const [filter, label] of Object.entries(tabLabels)) {
      const button = document.querySelector(`[data-diagnostic-filter="${filter}"]`);
      const count = button?.querySelector('[data-diagnostic-count]');
      if (button) {
        button.textContent = `${label} `;
        if (count) button.appendChild(count);
      }
    }

    const empty = document.getElementById('diagnostic-console-empty');
    if (empty) {
      empty.textContent = copyText(
        'CONSOLE_EMPTY',
        "I haven't seen any system messages this session."
      );
    }
  }

  function showShellError(retry) {
    const target = document.getElementById('main-content') || document.body;
    if (window.SurfaceState) {
      target.innerHTML = window.SurfaceState.error({ retry: true });
    } else {
      target.innerHTML =
        '<div class="surface-state surface-state--error" role="alert">' +
        '<h2 class="surface-state-heading">Couldn\'t load this section</h2>' +
        '<p class="surface-state-desc">reload to try again.</p>' +
        '<button type="button" class="surface-state-retry">Try again</button>' +
        '</div>';
    }
    const button = target.querySelector('.surface-state-retry');
    if (button) {
      button.addEventListener('click', retry, { once: true });
    }
  }

  function applyBodyState(shell, app, day) {
    document.title = `${app.label} - journal`;
    document.body.classList.toggle('has-app-bar', !!app.app_bar);
    document.body.classList.toggle('has-date-nav', !!(app.date_nav && app.date_nav.mount === 'chrome' && day));
    const appBar = document.getElementById('appBar');
    if (appBar) {
      appBar.hidden = !app.app_bar;
    }
    const facetBar = document.querySelector('.facet-bar');
    if (facetBar) {
      facetBar.classList.toggle('facets-disabled', !app.facets_enabled);
    }

    const existing = document.getElementById('facet-theme');
    if (existing) existing.remove();
    if (!app.facets_enabled || !shell.selected_facet) return;
    const facet = (shell.facets || []).find((item) => item.name === shell.selected_facet);
    if (!facet || !facet.color) return;
    const style = document.createElement('style');
    style.id = 'facet-theme';
    style.textContent =
      ':root {' +
      `--facet-color: ${facet.color};` +
      `--facet-bg: ${facet.color}1a;` +
      `--facet-border: ${facet.color};` +
      '}';
    document.head.appendChild(style);
  }

  function renderMenu(shell, currentAppName) {
    const menu = document.querySelector('.menu-bar .menu-items');
    if (!menu) return;
    const apps = shell.apps || [];
    let lastStarredIndex = -1;
    apps.forEach((app, index) => {
      if (app.starred) lastStarredIndex = index;
    });
    menu.innerHTML = apps
      .map((app, index) => {
        const isCurrent = app.name === currentAppName;
        const isLastStarred = index === lastStarredIndex && lastStarredIndex >= 0;
        const icon = app.icon_svg || escapeHtml(app.icon);
        const label = escapeHtml(app.label);
        return (
          `<li class="menu-item${isCurrent ? ' current' : ''}${isLastStarred ? ' last-starred' : ''}" data-app-name="${escapeHtml(app.name)}" data-starred="${app.starred ? 'true' : 'false'}">` +
          `<a href="/app/${escapeHtml(app.name)}" class="menu-item-link"${isCurrent ? ' aria-current="page"' : ''} tabindex="${isCurrent ? '0' : '-1'}">` +
          `<span class="icon">${icon}</span>` +
          `<span class="label">${label}</span>` +
          '</a>' +
          `<button class="star-toggle" type="button" tabindex="-1" data-app-name="${escapeHtml(app.name)}" aria-label="star ${label}" aria-pressed="${app.starred ? 'true' : 'false'}">${app.starred ? '★' : '☆'}</button>` +
          `<button class="drag-handle" type="button" tabindex="-1" draggable="true" aria-label="reorder ${label}">⋮</button>` +
          '</li>'
        );
      })
      .join('');
  }

  function adjustDay(day, delta) {
    const year = parseInt(day.substring(0, 4), 10);
    const month = parseInt(day.substring(4, 6), 10) - 1;
    const dayNum = parseInt(day.substring(6, 8), 10);
    const date = new Date(year, month, dayNum);
    date.setDate(date.getDate() + delta);
    return (
      date.getFullYear() +
      String(date.getMonth() + 1).padStart(2, '0') +
      String(date.getDate()).padStart(2, '0')
    );
  }

  function renderDateNav(app, day) {
    const host = document.getElementById('date-nav-host');
    if (!host) return;
    host.innerHTML = '';
    // L4 deletes the mount flag and the notch together; until then mount:'content' routes an app to date-nav.js instead of the chrome notch.
    if (!app.date_nav || app.date_nav.mount !== 'chrome' || !day) return;

    const baseUrl = `/app/${app.name}/`;
    host.innerHTML =
      '<div class="date-nav">' +
      '<div class="date-nav-left">' +
      '<button class="date-nav-arrow" id="date-nav-prev" title="previous (←)">‹</button>' +
      '</div>' +
      `<button class="date-nav-label" id="date-nav-label" title="open month picker" aria-label="open month picker">${escapeHtml(window.formatDateShort(day))}</button>` +
      '<div class="date-nav-right">' +
      '<button class="date-nav-today" id="date-nav-today" title="jump to today (t)" aria-label="go to today">T</button>' +
      '<button class="date-nav-arrow" id="date-nav-next" title="next (→)">›</button>' +
      '</div>' +
      '<div class="month-picker"></div>' +
      '</div>';

    function navigate(nextDay) {
      window.location.href = `${baseUrl}${nextDay}`;
    }

    window.MonthPicker.registerDataProvider(app.name, async (month, facet) => {
      try {
        const raw = await window.apiJson(`${baseUrl}api/stats/${month}`);
        const values = Object.values(raw);
        if (values.length > 0 && typeof values[0] === 'object' && values[0] !== null) {
          const result = {};
          for (const [itemDay, facetCounts] of Object.entries(raw)) {
            result[itemDay] = facet
              ? facetCounts[facet] || 0
              : Object.values(facetCounts).reduce((a, b) => a + b, 0);
          }
          return { data: result, error: null };
        }
        return { data: raw, error: null };
      } catch (err) {
        return { data: null, error: err };
      }
    });

    window.MonthPicker.init({
      app: app.name,
      currentDay: day,
      container: '.month-picker',
      allowFutureDates: !!app.date_nav.allow_future
    });

    document.getElementById('date-nav-label').addEventListener('click', () => {
      window.MonthPicker.toggle();
    });
    document.getElementById('date-nav-prev').addEventListener('click', () => {
      if (window.MonthPicker.isOpen()) window.MonthPicker.navigateMonth(-1);
      else navigate(adjustDay(day, -1));
    });
    document.getElementById('date-nav-next').addEventListener('click', () => {
      if (window.MonthPicker.isOpen()) window.MonthPicker.navigateMonth(1);
      else navigate(adjustDay(day, 1));
    });
    document.getElementById('date-nav-today').addEventListener('click', () => {
      navigate(window.MonthPicker.getToday());
    });
    document.addEventListener('keydown', (event) => {
      if (event.target.matches('input, textarea, select')) return;
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        if (window.MonthPicker.isOpen()) window.MonthPicker.navigateMonth(-1);
        else navigate(adjustDay(day, -1));
      }
      if (event.key === 'ArrowRight') {
        event.preventDefault();
        if (window.MonthPicker.isOpen()) window.MonthPicker.navigateMonth(1);
        else navigate(adjustDay(day, 1));
      }
      if (event.key === 't' || event.key === 'T') {
        event.preventDefault();
        navigate(window.MonthPicker.getToday());
      }
    });
  }

  function seedGlobals(shell, app) {
    const chatBar = shell.chat_bar || {};
    window.facetsData = shell.facets || [];
    window.selectedFacet = app.facets_enabled ? shell.selected_facet : null;
    window.appFacetCounts = {};
    window.CONVEY_SETTINGS = {
      reportingEnabled: shell.settings?.reporting_enabled !== false
    };
    window.solChatBarSeed = chatBar.sol_request || null;
    window.solChatBarAttention = chatBar.attention || null;
    const input = document.getElementById('chatBarInput');
    if (input) {
      input.placeholder = chatBar.placeholder || 'Send a message...';
    }
  }

  async function loadBackground(app) {
    if (!app.background_url) return;
    try {
      const response = await fetch(app.background_url, { credentials: 'same-origin' });
      if (!response.ok) {
        throw new Error(`Request failed (HTTP ${response.status})`);
      }
      const code = await response.text();
      new Function(code)();
    } catch (err) {
      window.AppServices?.markBackgroundFailing?.(app.name, err);
      window.logError?.(err, { context: 'app-bg-register', app: app.name });
    }
  }

  async function boot() {
    const context = pathContext();
    applyChromeCopy();
    try {
      const shell = await window.apiJson('/api/shell');
      const app = findApp(shell, context.appName);
      if (!app) {
        throw new Error('Unknown app');
      }
      applyBodyState(shell, app, context.day);
      renderMenu(shell, app.name);
      seedGlobals(shell, app);
      renderDateNav(app, context.day);
      window.resolveSolShellReady(shell);

      for (const backgroundApp of shell.apps || []) {
        await loadBackground(backgroundApp);
      }

      if (!app.workspace_url) {
        throw new Error('Workspace unavailable');
      }
      await window.mountWorkspaceFragment(app.workspace_url, { appName: app.name });
    } catch (error) {
      if (window.logError) {
        window.logError(error, { context: 'shell-boot' });
      }
      showShellError(boot);
    }
  }

  boot();
})();
