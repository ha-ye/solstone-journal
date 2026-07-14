// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function () {
  const DAY_RE = /^\d{8}$/;
  const TRANSCRIPTS_DAY_RE = /^\/app\/transcripts\/(\d{8})(?:\/)?$/;
  const MS_PER_DAY = 24 * 60 * 60 * 1000;
  const WEEKDAYS = [
    'Sunday',
    'Monday',
    'Tuesday',
    'Wednesday',
    'Thursday',
    'Friday',
    'Saturday'
  ];
  const WEEKDAYS_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = [
    'January',
    'February',
    'March',
    'April',
    'May',
    'June',
    'July',
    'August',
    'September',
    'October',
    'November',
    'December'
  ];
  const MONTHS_SHORT = [
    'Jan',
    'Feb',
    'Mar',
    'Apr',
    'May',
    'Jun',
    'Jul',
    'Aug',
    'Sep',
    'Oct',
    'Nov',
    'Dec'
  ];

  let mountAbort = null;
  let parsedDayInitialized = false;

  const state = {
    appName: null,
    config: null,
    day: null,
    host: null,
    root: null,
    heading: null,
    trigger: null,
    warning: null,
    popover: null,
    grid: null,
    title: null,
    prev: null,
    next: null,
    panelPrev: null,
    panelNext: null,
    open: false,
    zoom: 'days',
    month: null,
    year: null,
    coverage: null,
    months: {},
    indexLoaded: false,
    indexInflight: null,
    indexError: null,
    monthCache: new Map(),
    monthInflight: new Map(),
    warningVisible: false,
    facet: null,
    currentMax: 0
  };

  function parseDayString(value) {
    if (!DAY_RE.test(String(value || ''))) return null;
    const text = String(value);
    const year = Number(text.slice(0, 4));
    const month = Number(text.slice(4, 6));
    const day = Number(text.slice(6, 8));
    const date = new Date(year, month - 1, day);
    if (
      date.getFullYear() !== year ||
      date.getMonth() !== month - 1 ||
      date.getDate() !== day
    ) {
      return null;
    }
    return text;
  }

  function parseDayFromPath(pathname) {
    const match = String(pathname || '').match(TRANSCRIPTS_DAY_RE);
    return match ? parseDayString(match[1]) : null;
  }

  function DateNavDay() {
    if (parsedDayInitialized) return state.day;
    return parseDayFromPath(window.location.pathname);
  }

  function dateFromDay(day) {
    const normalized = parseDayString(day);
    if (!normalized) return null;
    return new Date(
      Number(normalized.slice(0, 4)),
      Number(normalized.slice(4, 6)) - 1,
      Number(normalized.slice(6, 8))
    );
  }

  function localMidnight(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
  }

  function dayDelta(day, now) {
    const target = dateFromDay(day);
    if (!target) return null;
    return Math.round((localMidnight(target) - localMidnight(now)) / MS_PER_DAY);
  }

  function dayString(date) {
    return (
      String(date.getFullYear()) +
      String(date.getMonth() + 1).padStart(2, '0') +
      String(date.getDate()).padStart(2, '0')
    );
  }

  function monthString(date) {
    return (
      String(date.getFullYear()) +
      String(date.getMonth() + 1).padStart(2, '0')
    );
  }

  function addDays(day, delta) {
    const date = dateFromDay(day);
    if (!date) return null;
    date.setDate(date.getDate() + delta);
    return dayString(date);
  }

  function addMonths(month, delta) {
    if (!/^\d{6}$/.test(String(month || ''))) return null;
    const date = new Date(Number(month.slice(0, 4)), Number(month.slice(4, 6)) - 1, 1);
    date.setMonth(date.getMonth() + delta);
    return monthString(date);
  }

  function headingLabel(day, now = new Date()) {
    const target = dateFromDay(day);
    if (!target) return '';
    const delta = dayDelta(day, now);
    if (delta === 0) return 'Today';
    if (delta === -1) return 'Yesterday';
    if (delta === 1) return 'Tomorrow';
    if (delta >= -6 && delta <= -2) return `Last ${WEEKDAYS[target.getDay()]}`;

    let label = `${WEEKDAYS[target.getDay()]}, ${MONTHS[target.getMonth()]} ${target.getDate()}`;
    if (target.getFullYear() !== now.getFullYear()) {
      label += `, ${target.getFullYear()}`;
    }
    return label;
  }

  function controlLabel(day, now = new Date()) {
    const target = dateFromDay(day);
    if (!target) return '';
    let label = `${WEEKDAYS_SHORT[target.getDay()]}, ${MONTHS_SHORT[target.getMonth()]} ${target.getDate()}`;
    if (target.getFullYear() !== now.getFullYear()) {
      label += ` '${String(target.getFullYear()).slice(-2)}`;
    }
    return label;
  }

  function countLabel(count, unit) {
    const noun = unit || {};
    if (count === 1) return `1 ${noun.one}`;
    if (count > 0) return `${count} ${noun.other}`;
    return noun.none || '';
  }

  function heatIntensity(value, max) {
    const numeric = Number(value || 0);
    const maximum = Number(max || 0);
    if (numeric <= 0 || maximum <= 0) return 0;
    return 0.15 + 0.85 * (Math.log1p(numeric) / Math.log1p(maximum));
  }

  function yearTotals(months) {
    const totals = {};
    Object.entries(months || {}).forEach(([month, value]) => {
      if (!/^\d{6}$/.test(month)) return;
      const year = month.slice(0, 4);
      totals[year] = (totals[year] || 0) + Number(value || 0);
    });
    return totals;
  }

  function openingMonth(indexPayload, now = new Date()) {
    const end = indexPayload?.coverage?.end;
    if (parseDayString(end)) return end.slice(0, 6);
    return monthString(now);
  }

  function logDateNavError(error, context) {
    if (window.logError) {
      window.logError(error, context);
      return;
    }
    if (window.console && typeof window.console.error === 'function') {
      window.console.error(error, context);
    }
  }

  function apiBase() {
    return `/app/${state.appName || 'transcripts'}/`;
  }

  function indexPayload() {
    return { coverage: state.coverage, months: state.months };
  }

  function normalizeIndexPayload(payload) {
    return {
      coverage: payload?.coverage || null,
      months: payload?.months || {}
    };
  }

  async function fetchIndex(force = false) {
    if (state.indexInflight) return state.indexInflight;
    if (!force && state.indexLoaded) {
      return { data: indexPayload(), error: state.indexError };
    }

    state.indexInflight = (async () => {
      try {
        const payload = normalizeIndexPayload(
          await window.apiJson(`${apiBase()}api/index`)
        );
        state.coverage = payload.coverage;
        state.months = payload.months;
        state.indexLoaded = true;
        state.indexError = null;
        state.warningVisible = false;
        updateLabels();
        return { data: payload, error: null };
      } catch (error) {
        state.indexError = error;
        state.warningVisible = true;
        updateLabels();
        logDateNavError(error, {
          context: 'date-nav:index',
          url: `${apiBase()}api/index`
        });
        return {
          data: state.indexLoaded ? indexPayload() : null,
          error
        };
      } finally {
        state.indexInflight = null;
      }
    })();
    return state.indexInflight;
  }

  async function fetchMonth(month, force = false) {
    if (!/^\d{6}$/.test(String(month || ''))) {
      return { data: {}, error: null };
    }
    if (state.monthInflight.has(month)) return state.monthInflight.get(month);
    if (!force && state.monthCache.has(month)) return state.monthCache.get(month);

    const request = (async () => {
      try {
        const data = await window.apiJson(`${apiBase()}api/stats/${month}`);
        const result = { data: data || {}, error: null };
        state.monthCache.set(month, result);
        state.warningVisible = false;
        updateLabels();
        return result;
      } catch (error) {
        state.warningVisible = true;
        updateLabels();
        const stale = state.monthCache.get(month) || null;
        return { data: stale ? stale.data : {}, error };
      } finally {
        state.monthInflight.delete(month);
      }
    })();
    state.monthInflight.set(month, request);
    return request;
  }

  function prefetchAdjacentMonths(month) {
    const previous = addMonths(month, -1);
    const next = addMonths(month, 1);
    if (previous) fetchMonth(previous).catch(() => {});
    if (next) fetchMonth(next).catch(() => {});
  }

  function getApp(shell, appName) {
    const apps = shell?.apps || window.solShellData?.apps || [];
    return apps.find((app) => app.name === appName) || null;
  }

  function selectedUnit() {
    return state.config?.unit || {};
  }

  function resetMountState() {
    if (mountAbort) mountAbort.abort();
    mountAbort = null;
    state.appName = null;
    state.config = null;
    state.day = null;
    state.host = null;
    state.root = null;
    state.heading = null;
    state.trigger = null;
    state.warning = null;
    state.popover = null;
    state.grid = null;
    state.title = null;
    state.prev = null;
    state.next = null;
    state.panelPrev = null;
    state.panelNext = null;
    state.open = false;
    state.zoom = 'days';
    state.month = null;
    state.year = null;
    state.coverage = null;
    state.months = {};
    state.indexLoaded = false;
    state.indexInflight = null;
    state.indexError = null;
    state.monthCache = new Map();
    state.monthInflight = new Map();
    state.warningVisible = false;
    state.facet = null;
    state.currentMax = 0;
    parsedDayInitialized = false;
  }

  function renderShell() {
    state.host.innerHTML =
      '<div class="date-nav-content" data-date-nav-root>' +
      '<div class="date-nav-content__bar">' +
      '<button class="date-nav-content__arrow" type="button" data-date-nav-prev aria-label="previous day">‹</button>' +
      '<button class="date-nav-content__trigger" type="button" data-date-nav-trigger aria-haspopup="dialog" aria-expanded="false">' +
      '<span data-date-nav-label></span>' +
      '<span class="date-nav-content__warning" data-date-nav-warning aria-hidden="true" hidden>!</span>' +
      '</button>' +
      '<button class="date-nav-content__arrow" type="button" data-date-nav-next aria-label="next day">›</button>' +
      '</div>' +
      '<div class="date-nav-content__popover" data-date-nav-popover role="dialog" hidden>' +
      '<div class="date-nav-content__panelbar">' +
      '<button class="date-nav-content__panel-arrow" type="button" data-date-nav-panel-prev aria-label="previous">‹</button>' +
      '<button class="date-nav-content__panel-title" type="button" data-date-nav-title aria-label="change date level"></button>' +
      '<button class="date-nav-content__panel-arrow" type="button" data-date-nav-panel-next aria-label="next">›</button>' +
      '</div>' +
      '<div class="date-nav-content__grid" data-date-nav-grid role="grid"></div>' +
      '</div>' +
      '</div>';

    state.root = state.host.querySelector('[data-date-nav-root]');
    state.trigger = state.host.querySelector('[data-date-nav-trigger]');
    state.warning = state.host.querySelector('[data-date-nav-warning]');
    state.popover = state.host.querySelector('[data-date-nav-popover]');
    state.grid = state.host.querySelector('[data-date-nav-grid]');
    state.title = state.host.querySelector('[data-date-nav-title]');
    state.prev = state.host.querySelector('[data-date-nav-prev]');
    state.next = state.host.querySelector('[data-date-nav-next]');
    state.panelPrev = state.host.querySelector('[data-date-nav-panel-prev]');
    state.panelNext = state.host.querySelector('[data-date-nav-panel-next]');
    updateLabels();
  }

  function updateLabels() {
    if (state.heading) state.heading.textContent = headingLabel(state.day);
    const label = state.host?.querySelector('[data-date-nav-label]');
    if (label) label.textContent = controlLabel(state.day) || 'Date';
    if (state.warning) state.warning.hidden = !state.warningVisible;
    if (state.trigger) {
      state.trigger.setAttribute('aria-expanded', String(state.open));
      state.trigger.classList.toggle('date-nav-content__trigger--warning', state.warningVisible);
    }
    updateArrowState();
  }

  function compareDay(left, right) {
    if (!left || !right) return 0;
    return left.localeCompare(right);
  }

  function compareMonth(left, right) {
    if (!left || !right) return 0;
    return left.localeCompare(right);
  }

  function canNavigateDay(delta) {
    if (!state.day || !state.coverage) return false;
    if (delta < 0) return compareDay(state.day, state.coverage.start) > 0;
    if (delta > 0) return compareDay(state.day, state.coverage.end) < 0;
    return false;
  }

  function canNavigatePanel(delta) {
    if (!state.coverage) return false;
    if (state.zoom === 'days') {
      const startMonth = state.coverage.start.slice(0, 6);
      const endMonth = state.coverage.end.slice(0, 6);
      if (delta < 0) return compareMonth(state.month, startMonth) > 0;
      if (delta > 0) return compareMonth(state.month, endMonth) < 0;
    }
    if (state.zoom === 'months') {
      const startYear = Number(state.coverage.start.slice(0, 4));
      const endYear = Number(state.coverage.end.slice(0, 4));
      if (delta < 0) return state.year > startYear;
      if (delta > 0) return state.year < endYear;
    }
    return false;
  }

  function updateArrowState() {
    if (!state.prev || !state.next) return;
    const usePanel = state.open;
    state.prev.disabled = usePanel ? !canNavigatePanel(-1) : !canNavigateDay(-1);
    state.next.disabled = usePanel ? !canNavigatePanel(1) : !canNavigateDay(1);
    if (state.panelPrev && state.panelNext) {
      state.panelPrev.disabled = !canNavigatePanel(-1);
      state.panelNext.disabled = !canNavigatePanel(1);
    }
  }

  function navigateTo(day) {
    const normalized = parseDayString(day);
    if (!normalized || !state.appName) return;
    window.location.href = `/app/${state.appName}/${normalized}`;
  }

  function navigateDay(delta) {
    if (!canNavigateDay(delta)) return;
    const nextDay = addDays(state.day, delta);
    if (nextDay) navigateTo(nextDay);
  }

  function movePanel(delta) {
    if (!canNavigatePanel(delta)) return;
    if (state.zoom === 'days') {
      state.month = addMonths(state.month, delta);
      state.year = Number(state.month.slice(0, 4));
    } else if (state.zoom === 'months') {
      state.year += delta;
      state.month = `${state.year}${String(Number(state.month.slice(4, 6))).padStart(2, '0')}`;
    }
    renderPanel();
  }

  function showPanel() {
    state.open = true;
    state.zoom = 'days';
    fetchIndex().then((result) => {
      const payload = result.data || indexPayload();
      state.month = state.day ? state.day.slice(0, 6) : openingMonth(payload);
      state.year = Number(state.month.slice(0, 4));
      renderPanel();
      focusInitialCell();
    });
    renderPanel();
  }

  function hidePanel({ restoreFocus = true } = {}) {
    state.open = false;
    renderPanel();
    if (restoreFocus && state.trigger) state.trigger.focus();
  }

  function togglePanel() {
    if (state.open) hidePanel();
    else showPanel();
  }

  function titleLabel() {
    if (state.zoom === 'years') return 'Years';
    if (state.zoom === 'months') return String(state.year);
    const date = new Date(Number(state.month.slice(0, 4)), Number(state.month.slice(4, 6)) - 1, 1);
    return `${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
  }

  function maxPositive(values) {
    return values.reduce((max, value) => Math.max(max, Number(value || 0)), 0);
  }

  function clearGrid() {
    state.grid.innerHTML = '';
  }

  function renderCell({ value, label, count, selected = false, disabled = false, kind }) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'date-nav-content__cell';
    button.dataset.dateNavCell = kind;
    button.dataset.value = value;
    button.setAttribute('role', 'gridcell');
    button.setAttribute('aria-label', `${label}, ${countLabel(count, selectedUnit())}`);
    button.textContent = label;
    button.style.setProperty('--intensity', String(heatIntensity(count, state.currentMax || 0)));
    button.disabled = disabled;
    button.tabIndex = disabled ? -1 : -1;
    if (selected) button.classList.add('date-nav-content__cell--selected');
    if (count <= 0) button.classList.add('date-nav-content__cell--empty');
    const meta = document.createElement('span');
    meta.className = 'date-nav-content__cell-count';
    meta.textContent = countLabel(count, selectedUnit());
    button.appendChild(meta);
    return button;
  }

  function appendRows(cells, columns) {
    clearGrid();
    state.grid.className = `date-nav-content__grid date-nav-content__grid--${columns}`;
    state.grid.dataset.cols = String(columns);
    for (let index = 0; index < cells.length; index += columns) {
      const row = document.createElement('div');
      row.className = 'date-nav-content__row';
      row.setAttribute('role', 'row');
      cells.slice(index, index + columns).forEach((cell) => row.appendChild(cell));
      state.grid.appendChild(row);
    }
    normalizeRovingTabindex();
  }

  function renderYears() {
    const totals = yearTotals(state.months);
    const years = Object.keys(totals).sort();
    if (years.length === 0) years.push(String(new Date().getFullYear()));
    const max = maxPositive(years.map((year) => totals[year] || 0));
    state.currentMax = max;
    appendRows(
      years.map((year) => {
        const count = totals[year] || 0;
        return renderCell({
          value: year,
          label: year,
          count,
          selected: Number(year) === state.year,
          disabled: count <= 0,
          kind: 'year'
        });
      }),
      3
    );
  }

  function renderMonths() {
    const cells = [];
    const counts = [];
    for (let month = 1; month <= 12; month += 1) {
      const key = `${state.year}${String(month).padStart(2, '0')}`;
      counts.push(Number(state.months[key] || 0));
    }
    state.currentMax = maxPositive(counts);
    for (let month = 1; month <= 12; month += 1) {
      const key = `${state.year}${String(month).padStart(2, '0')}`;
      const count = Number(state.months[key] || 0);
      cells.push(
        renderCell({
          value: key,
          label: MONTHS_SHORT[month - 1],
          count,
          selected: key === state.month,
          disabled: count <= 0,
          kind: 'month'
        })
      );
    }
    appendRows(cells, 3);
  }

  function renderDays() {
    const month = state.month || openingMonth(indexPayload());
    state.month = month;
    state.year = Number(month.slice(0, 4));
    const cached = state.monthCache.get(month);
    if (!cached) {
      fetchMonth(month).then(() => {
        if (state.open && state.zoom === 'days' && state.month === month) {
          renderPanel();
          prefetchAdjacentMonths(month);
        }
      });
    } else {
      prefetchAdjacentMonths(month);
    }

    const data = cached?.data || {};
    const year = Number(month.slice(0, 4));
    const monthIndex = Number(month.slice(4, 6)) - 1;
    const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
    const values = [];
    for (let day = 1; day <= daysInMonth; day += 1) {
      values.push(Number(data[`${month}${String(day).padStart(2, '0')}`] || 0));
    }
    state.currentMax = maxPositive(values);

    const cells = [];
    for (let day = 1; day <= daysInMonth; day += 1) {
      const key = `${month}${String(day).padStart(2, '0')}`;
      const count = Number(data[key] || 0);
      cells.push(
        renderCell({
          value: key,
          label: String(day),
          count,
          selected: key === state.day,
          disabled: count <= 0,
          kind: 'day'
        })
      );
    }
    appendRows(cells, 7);
  }

  function renderPanel() {
    if (!state.popover || !state.grid) return;
    state.popover.hidden = !state.open;
    if (state.trigger) state.trigger.setAttribute('aria-expanded', String(state.open));
    updateArrowState();
    if (!state.open) return;
    if (!state.month) state.month = state.day ? state.day.slice(0, 6) : openingMonth(indexPayload());
    if (!state.year) state.year = Number(state.month.slice(0, 4));
    state.title.textContent = titleLabel();
    if (state.zoom === 'years') renderYears();
    else if (state.zoom === 'months') renderMonths();
    else renderDays();
  }

  function focusableCells() {
    return Array.from(state.grid?.querySelectorAll('[data-date-nav-cell]:not(:disabled)') || []);
  }

  function normalizeRovingTabindex() {
    const cells = focusableCells();
    if (cells.length === 0) return;
    const selected = cells.find((cell) => cell.classList.contains('date-nav-content__cell--selected'));
    cells.forEach((cell) => {
      cell.tabIndex = cell === (selected || cells[0]) ? 0 : -1;
    });
  }

  function focusInitialCell() {
    const selected = state.grid?.querySelector('.date-nav-content__cell--selected:not(:disabled)');
    const target = selected || focusableCells()[0];
    if (target) target.focus();
  }

  function focusGridCell(cells, index) {
    const target = cells[index];
    if (!target) return;
    cells.forEach((cell) => {
      cell.tabIndex = -1;
    });
    target.tabIndex = 0;
    target.focus();
  }

  function gridColumnCount() {
    return Number(state.grid?.dataset.cols) || 7;
  }

  function moveGridFocus(event, offset) {
    const cells = focusableCells();
    const currentIndex = cells.indexOf(event.target);
    if (currentIndex < 0 || cells.length === 0) return;
    event.preventDefault();
    const nextIndex = Math.max(0, Math.min(cells.length - 1, currentIndex + offset));
    focusGridCell(cells, nextIndex);
  }

  function handleGridKeydown(event) {
    if (!event.target.matches('[data-date-nav-cell]')) return;
    if (
      ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' ', 'Escape'].includes(event.key)
    ) {
      event.stopPropagation();
    }
    if (event.key === 'ArrowRight') moveGridFocus(event, 1);
    if (event.key === 'ArrowLeft') moveGridFocus(event, -1);
    if (event.key === 'ArrowDown') moveGridFocus(event, gridColumnCount());
    if (event.key === 'ArrowUp') moveGridFocus(event, -gridColumnCount());
    if (event.key === 'Home') {
      const cells = focusableCells();
      if (cells.length > 0) {
        event.preventDefault();
        focusGridCell(cells, 0);
      }
    }
    if (event.key === 'End') {
      const cells = focusableCells();
      if (cells.length > 0) {
        event.preventDefault();
        focusGridCell(cells, cells.length - 1);
      }
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      event.target.click();
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      hidePanel();
    }
  }

  function handleRootClick(event) {
    const trigger = event.target.closest('[data-date-nav-trigger]');
    if (trigger) {
      togglePanel();
      return;
    }

    if (event.target.closest('[data-date-nav-prev]')) {
      if (state.open) movePanel(-1);
      else navigateDay(-1);
      return;
    }
    if (event.target.closest('[data-date-nav-next]')) {
      if (state.open) movePanel(1);
      else navigateDay(1);
      return;
    }
    if (event.target.closest('[data-date-nav-panel-prev]')) {
      movePanel(-1);
      return;
    }
    if (event.target.closest('[data-date-nav-panel-next]')) {
      movePanel(1);
      return;
    }
    if (event.target.closest('[data-date-nav-title]')) {
      if (state.zoom === 'days') state.zoom = 'months';
      else if (state.zoom === 'months') state.zoom = 'years';
      renderPanel();
      focusInitialCell();
      return;
    }

    const cell = event.target.closest('[data-date-nav-cell]');
    if (!cell || cell.disabled) return;
    const value = cell.dataset.value;
    if (cell.dataset.dateNavCell === 'year') {
      state.year = Number(value);
      state.month = `${value}01`;
      state.zoom = 'months';
      renderPanel();
      focusInitialCell();
      return;
    }
    if (cell.dataset.dateNavCell === 'month') {
      state.month = value;
      state.year = Number(value.slice(0, 4));
      state.zoom = 'days';
      renderPanel();
      focusInitialCell();
      return;
    }
    if (cell.dataset.dateNavCell === 'day') {
      navigateTo(value);
    }
  }

  function handleDocumentClick(event) {
    if (!state.open || !state.root) return;
    if (!state.root.contains(event.target)) hidePanel({ restoreFocus: false });
  }

  function isTypingTarget(target) {
    if (!target || !target.matches) return false;
    return target.matches('input, textarea, select, [contenteditable="true"]');
  }

  function handleDocumentKeydown(event) {
    if (!state.config || isTypingTarget(event.target)) return;
    if (state.root && state.root.contains(event.target) && event.target.matches('[data-date-nav-cell]')) return;
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (state.open) movePanel(-1);
      else navigateDay(-1);
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      if (state.open) movePanel(1);
      else navigateDay(1);
    }
    if (event.key === 't' || event.key === 'T') {
      event.preventDefault();
      navigateTo(dayString(new Date()));
    }
  }

  function handleFacetSwitch(event) {
    // Transcripts has facets disabled, so this refetch path is unexercised in L1.
    state.facet = event.detail?.facet || null;
    state.monthCache.clear();
    fetchIndex(true).then(() => {
      if (state.open) renderPanel();
    });
  }

  function mountContentDateNav(shell, appName) {
    const app = getApp(shell, appName);
    const host = document.querySelector('[data-date-nav]');
    const heading = document.querySelector('[data-date-nav-heading]');
    const config = app?.date_nav || null;

    if (host && config?.mount === 'chrome') {
      logDateNavError(new Error('chrome date_nav config found with content date nav host'), {
        context: 'date-nav:mount-mismatch',
        app: appName
      });
      return;
    }
    if (!config || config.mount !== 'content') return;
    if (!host) {
      logDateNavError(new Error('content date_nav config missing workspace host'), {
        context: 'date-nav:missing-host',
        app: appName
      });
      return;
    }

    mountAbort = new AbortController();
    state.appName = appName;
    state.config = config;
    state.day = parseDayFromPath(window.location.pathname);
    parsedDayInitialized = true;
    state.host = host;
    state.heading = heading;
    state.month = state.day ? state.day.slice(0, 6) : null;
    state.year = state.month ? Number(state.month.slice(0, 4)) : null;

    renderShell();
    state.root.addEventListener('click', handleRootClick, { signal: mountAbort.signal });
    state.root.addEventListener('keydown', handleGridKeydown, { signal: mountAbort.signal });
    document.addEventListener('click', handleDocumentClick, { signal: mountAbort.signal });
    window.addEventListener('facet.switch', handleFacetSwitch, { signal: mountAbort.signal });
    fetchIndex(true).then(() => {
      updateLabels();
      if (state.open) renderPanel();
    });
  }

  function handleWorkspaceMounted(event) {
    const appName = event.detail?.appName || null;
    resetMountState();
    if (!appName) return;
    if (window.solShellData) {
      mountContentDateNav(window.solShellData, appName);
      return;
    }
    if (window.whenShellReady) {
      window.whenShellReady((shell) => {
        mountContentDateNav(shell, appName);
      });
    }
  }

  document.addEventListener('keydown', handleDocumentKeydown);
  document.addEventListener('workspace:mounted', handleWorkspaceMounted);

  window.DateNav = {
    day: DateNavDay,
    parseDayFromPath,
    headingLabel,
    controlLabel,
    heatIntensity,
    yearTotals,
    countLabel,
    openingMonth
  };
})();
