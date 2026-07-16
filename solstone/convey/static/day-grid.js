// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function () {
  'use strict';

  const MONTH_SHORT = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
  ];
  const DAY_RE = /^\d{8}$/;
  const DAY_ATTR = 'data-daygrid-date';
  const stateByHost = new WeakMap();

  function dateNav() {
    const nav = window.DateNav;
    if (
      !nav ||
      typeof nav.heatIntensity !== 'function' ||
      typeof nav.countLabel !== 'function' ||
      typeof nav.coerceCount !== 'function'
    ) {
      throw new Error('DayGrid requires DateNav');
    }
    return nav;
  }

  function dateFromDay(day) {
    if (typeof day !== 'string' || !DAY_RE.test(day)) return null;
    const y = Number(day.slice(0, 4));
    const m = Number(day.slice(4, 6));
    const d = Number(day.slice(6, 8));
    const date = new Date(y, m - 1, d);
    if (
      date.getFullYear() !== y ||
      date.getMonth() !== m - 1 ||
      date.getDate() !== d
    ) {
      return null;
    }
    return date;
  }

  function validDay(day) {
    return typeof day === 'string' && DAY_RE.test(day) && Boolean(dateFromDay(day));
  }

  function dayString(date) {
    return String(date.getFullYear()).padStart(4, '0') +
      String(date.getMonth() + 1).padStart(2, '0') +
      String(date.getDate()).padStart(2, '0');
  }

  function addDays(day, delta) {
    const date = dateFromDay(day);
    if (!date) return null;
    date.setDate(date.getDate() + delta);
    return dayString(date);
  }

  function sundayOf(day) {
    const date = dateFromDay(day);
    if (!date) return null;
    date.setDate(date.getDate() - date.getDay());
    return dayString(date);
  }

  function daysInSpan(start, end) {
    if (!validDay(start) || !validDay(end) || start > end) return 0;
    let count = 1;
    let cursor = start;
    while (cursor !== end) {
      cursor = addDays(cursor, 1);
      if (!cursor) return 0;
      count += 1;
    }
    return count;
  }

  function own(map, key) {
    return Object.prototype.hasOwnProperty.call(map || {}, key);
  }

  function activeDayKeys(data) {
    const nav = dateNav();
    const out = new Set();
    for (const [day, value] of Object.entries(data?.days || {})) {
      if (validDay(day) && nav.coerceCount(value) > 0) out.add(day);
    }
    for (const [day, value] of Object.entries(data?.pending || {})) {
      if (validDay(day) && nav.coerceCount(value) > 0) out.add(day);
    }
    return Array.from(out).sort();
  }

  function gate(data, opts = {}) {
    const minSpanDays = Number(opts.minSpanDays ?? 70);
    const minActiveDays = Number(opts.minActiveDays ?? 14);
    const coverage = data?.coverage || null;
    const span = coverage ? daysInSpan(coverage.start, coverage.end) : 0;
    if (span < minSpanDays) return { ok: false, reason: 'span-too-short' };
    if (activeDayKeys(data).length < minActiveDays) {
      return { ok: false, reason: 'too-few-active-days' };
    }
    return { ok: true, reason: null };
  }

  function scrollTargetDay(data, today) {
    const coverage = data?.coverage || null;
    if (!coverage || !validDay(coverage.start) || !validDay(coverage.end)) return null;
    if (validDay(today) && today >= coverage.start && today <= coverage.end) {
      return today;
    }
    const active = activeDayKeys(data);
    return active.length ? active[active.length - 1] : coverage.end;
  }

  function maxRolledCount(data) {
    const nav = dateNav();
    return Object.values(data?.days || {}).reduce((max, value) => {
      return Math.max(max, nav.coerceCount(value));
    }, 0);
  }

  function todayString(now = new Date()) {
    return dayString(now);
  }

  function displayDay(day) {
    return `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}`;
  }

  function monthLabel(month) {
    const index = Number(month.slice(4, 6)) - 1;
    return `${MONTH_SHORT[index]} ${month.slice(0, 4)}`;
  }

  function firstOfMonth(day) {
    return `${day.slice(0, 6)}01`;
  }

  function lastOfMonth(day) {
    const date = dateFromDay(firstOfMonth(day));
    if (!date) return null;
    date.setMonth(date.getMonth() + 1);
    date.setDate(0);
    return dayString(date);
  }

  function addMonths(month, delta) {
    if (!/^\d{6}$/.test(String(month || ''))) return null;
    const date = new Date(Number(month.slice(0, 4)), Number(month.slice(4, 6)) - 1, 1);
    date.setMonth(date.getMonth() + delta);
    return String(date.getFullYear()).padStart(4, '0') +
      String(date.getMonth() + 1).padStart(2, '0');
  }

  function joinPath(appPath, value) {
    return `${String(appPath || '').replace(/\/+$/, '')}/${value}`;
  }

  function spanWeeks(gridStart, start, end) {
    const startSunday = sundayOf(start);
    const endSunday = sundayOf(end);
    if (!startSunday || !endSunday) return { start: 1, length: 1 };
    const startIndex = Math.floor((daysInSpan(gridStart, startSunday) - 1) / 7) + 1;
    const endIndex = Math.floor((daysInSpan(gridStart, endSunday) - 1) / 7) + 1;
    return { start: startIndex, length: Math.max(1, endIndex - startIndex + 1) };
  }

  function renderMonthLabels(data, config, gridStart) {
    const row = document.createElement('div');
    row.className = 'daygrid-months';
    if (!config.monthLinks) row.setAttribute('aria-hidden', 'true');

    const start = data.coverage.start;
    const end = data.coverage.end;
    let month = start.slice(0, 6);
    while (month <= end.slice(0, 6)) {
      const monthStart = `${month}01`;
      const monthEnd = lastOfMonth(monthStart);
      const labelStart = monthStart < start ? start : monthStart;
      const labelEnd = monthEnd && monthEnd > end ? end : monthEnd;
      if (labelEnd) {
        const weeks = spanWeeks(gridStart, labelStart, labelEnd);
        const label = config.monthLinks
          ? document.createElement('a')
          : document.createElement('span');
        label.textContent = monthLabel(month);
        label.style.gridColumnStart = String(weeks.start);
        label.style.gridColumnEnd = `span ${weeks.length}`;
        if (config.monthLinks) label.href = joinPath(config.appPath, month);
        row.appendChild(label);
      }
      const next = addMonths(month, 1);
      if (!next || next <= month) break;
      month = next;
    }
    return row;
  }

  function legend(host, { unit } = {}) {
    if (!host) throw new Error('DayGrid.legend requires a host element');
    dateNav();
    host.replaceChildren();

    const root = document.createElement('div');
    root.className = 'daygrid-legend';

    const scale = document.createElement('div');
    scale.className = 'daygrid-legend-scale';
    scale.setAttribute('aria-hidden', 'true');
    const less = document.createElement('span');
    less.textContent = 'less';
    scale.appendChild(less);
    [0, 0.33, 0.66, 1].forEach((heat) => {
      const swatch = document.createElement('span');
      swatch.className = 'daygrid-legend-swatch';
      swatch.style.setProperty('--daygrid-heat', String(heat));
      scale.appendChild(swatch);
    });
    const more = document.createElement('span');
    more.textContent = 'more';
    scale.appendChild(more);

    const pending = document.createElement('div');
    pending.className = 'daygrid-legend-pending';
    pending.innerHTML = '<span class="daygrid-legend-pending-mark" aria-hidden="true"></span><span>pending rollup</span>';

    root.append(scale, pending);
    host.appendChild(root);
    return root;
  }

  function cellLabel(day, count, unit, pending) {
    const label = dateNav().countLabel(count, unit);
    const suffix = pending ? ', rollup pending' : '';
    return `${displayDay(day)}: ${label}${suffix}`;
  }

  function buildCells(data, config, today) {
    const start = data.coverage.start;
    const end = data.coverage.end;
    const gridStart = sundayOf(start);
    const endSunday = sundayOf(end);
    const gridEnd = endSunday ? addDays(endSunday, 6) : null;
    if (!gridStart || !gridEnd) return null;

    const nav = dateNav();
    const maxCount = maxRolledCount(data);
    const cells = [];
    let cursor = gridStart;
    while (cursor <= gridEnd) {
      const inCoverage = cursor >= start && cursor <= end;
      const isRolled = inCoverage && own(data.days, cursor);
      const isPending = inCoverage && own(data.pending, cursor);
      const rawCount = isRolled ? data.days[cursor] : data.pending?.[cursor];
      const count = nav.coerceCount(rawCount);
      let cell;
      if (!inCoverage) {
        cell = document.createElement('span');
        cell.className = 'daygrid-cell daygrid-cell--pad';
        cell.setAttribute('aria-hidden', 'true');
      } else if ((isRolled || isPending) && count > 0) {
        cell = document.createElement('a');
        cell.className = 'daygrid-cell';
        cell.setAttribute(DAY_ATTR, cursor);
        cell.textContent = String(Number(cursor.slice(6, 8)));
        cell.href = joinPath(config.appPath, cursor);
        if (cursor === today) cell.classList.add('daygrid-cell--today');
        if (isRolled) {
          cell.classList.add('daygrid-cell--data');
          cell.style.setProperty('--daygrid-heat', String(nav.heatIntensity(count, maxCount)));
        } else {
          cell.classList.add('daygrid-cell--pending');
        }
        const label = cellLabel(cursor, count, config.unit, isPending && !isRolled);
        cell.setAttribute('aria-label', label);
        cell.title = label;
        cell.tabIndex = -1;
      } else {
        cell = document.createElement('span');
        cell.className = 'daygrid-cell daygrid-cell--empty';
        cell.setAttribute(DAY_ATTR, cursor);
        cell.setAttribute('role', 'button');
        cell.setAttribute('aria-disabled', 'true');
        cell.textContent = String(Number(cursor.slice(6, 8)));
        if (cursor === today) cell.classList.add('daygrid-cell--today');
        const label = cellLabel(cursor, 0, config.unit, false);
        cell.setAttribute('aria-label', label);
        cell.title = label;
        cell.tabIndex = -1;
      }
      cells.push({ day: cursor, element: cell, inCoverage });
      cursor = addDays(cursor, 1);
      if (!cursor) break;
    }
    return { cells, gridStart };
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function normalizeConfig(options) {
    const config = {
      data: options?.data || null,
      unit: options?.unit || {},
      mode: options?.mode || 'navigate',
      appPath: options?.appPath || '',
      monthLinks: Boolean(options?.monthLinks),
      today: validDay(options?.today) ? options.today : todayString(),
    };
    if (config.mode !== 'navigate') {
      throw new Error('DayGrid.mount supports mode "navigate"');
    }
    if (!config.appPath) {
      throw new Error('DayGrid.mount requires appPath');
    }
    return config;
  }

  function mount(host, options = {}) {
    if (!host) throw new Error('DayGrid.mount requires a host element');
    dateNav();

    const previous = stateByHost.get(host);
    if (previous) previous.abort.abort();
    host.replaceChildren();

    const config = normalizeConfig(options);
    const data = config.data;
    const coverage = data?.coverage || null;
    if (!coverage || !validDay(coverage.start) || !validDay(coverage.end)) {
      stateByHost.delete(host);
      return null;
    }

    const abort = new AbortController();
    const signal = abort.signal;
    const built = buildCells(data, config, config.today);
    if (!built) return null;
    const targetDay = scrollTargetDay(data, config.today);

    const root = document.createElement('div');
    root.className = 'daygrid';
    root.__dayGridScrollTarget = targetDay || '';

    const scroller = document.createElement('div');
    scroller.className = 'daygrid-scroller';
    scroller.tabIndex = -1;

    const body = document.createElement('div');
    body.className = 'daygrid-body';
    body.appendChild(renderMonthLabels(data, config, built.gridStart));

    const grid = document.createElement('div');
    grid.className = 'daygrid-track';
    for (const item of built.cells) grid.appendChild(item.element);
    body.appendChild(grid);
    scroller.appendChild(body);

    const peek = document.createElement('div');
    peek.className = 'daygrid-peek';
    peek.hidden = true;
    root.append(scroller, peek);
    host.appendChild(root);

    const focusable = built.cells.filter((item) => item.inCoverage);
    const byDay = new Map(focusable.map((item) => [item.day, item]));
    let active = byDay.get(targetDay) || focusable[0] || null;
    let peekCell = null;

    function applyTabStop(next, shouldFocus) {
      if (!next) return;
      if (active) active.element.tabIndex = -1;
      active = next;
      active.element.tabIndex = 0;
      if (shouldFocus) active.element.focus();
    }

    function dayForMove(currentDay, key) {
      const date = dateFromDay(currentDay);
      if (!date) return currentDay;
      if (key === 'ArrowLeft') return addDays(currentDay, -7) || currentDay;
      if (key === 'ArrowRight') return addDays(currentDay, 7) || currentDay;
      if (key === 'ArrowUp') return date.getDay() === 0 ? currentDay : addDays(currentDay, -1) || currentDay;
      if (key === 'ArrowDown') return date.getDay() === 6 ? currentDay : addDays(currentDay, 1) || currentDay;
      return currentDay;
    }

    function moveFocus(key) {
      if (!active) return;
      const target = dayForMove(active.day, key);
      applyTabStop(byDay.get(target) || byDay.get(active.day), true);
    }

    function hidePeek() {
      peek.hidden = true;
      peekCell = null;
    }

    function showPeek(cell) {
      const day = cell.getAttribute(DAY_ATTR);
      if (!day) return;
      const isRolled = own(data.days, day);
      const isPending = own(data.pending, day);
      const count = isRolled ? data.days[day] : data.pending?.[day] || 0;
      peek.textContent = cellLabel(day, count, config.unit, isPending && !isRolled);
      peek.hidden = false;
      peekCell = cell;
      const rootRect = root.getBoundingClientRect();
      const cellRect = cell.getBoundingClientRect();
      const left = clamp(
        cellRect.left + cellRect.width / 2 - rootRect.left,
        12,
        Math.max(12, rootRect.width - 12)
      );
      peek.style.left = `${left}px`;
      peek.style.top = `${Math.max(0, scroller.offsetTop - 34)}px`;
    }

    root.addEventListener('keydown', (event) => {
      if (!event.target.closest(`.daygrid-cell[${DAY_ATTR}]`)) return;
      if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
        event.preventDefault();
        moveFocus(event.key);
        return;
      }
      if (
        (event.key === 'Enter' || event.key === ' ') &&
        event.target.closest('.daygrid-cell--empty')
      ) {
        event.preventDefault();
      }
    }, { signal });

    root.addEventListener('click', (event) => {
      if (event.target.closest('.daygrid-cell--empty')) {
        event.preventDefault();
        event.stopPropagation();
      }
    }, { signal });

    root.addEventListener('focusin', (event) => {
      const cell = event.target.closest(`.daygrid-cell[${DAY_ATTR}]`);
      if (cell) {
        const item = byDay.get(cell.getAttribute(DAY_ATTR));
        if (item) applyTabStop(item, false);
        showPeek(cell);
      }
    }, { signal });

    root.addEventListener('focusout', (event) => {
      if (!root.contains(event.relatedTarget)) hidePeek();
    }, { signal });

    root.addEventListener('mouseover', (event) => {
      const cell = event.target.closest(`.daygrid-cell[${DAY_ATTR}]`);
      if (cell) showPeek(cell);
    }, { signal });

    root.addEventListener('mouseout', (event) => {
      if (!root.contains(event.relatedTarget)) hidePeek();
    }, { signal });

    scroller.addEventListener('scroll', () => {
      if (peekCell && !peek.hidden) showPeek(peekCell);
    }, { signal, passive: true });

    applyTabStop(active, false);

    const targetElement = targetDay ? root.querySelector(`[${DAY_ATTR}="${targetDay}"]`) : null;
    if (targetElement) {
      requestAnimationFrame(() => {
        const rawLeft = targetElement.offsetLeft - (scroller.clientWidth / 2) + (targetElement.clientWidth / 2);
        scroller.scrollLeft = clamp(rawLeft, 0, Math.max(0, scroller.scrollWidth - scroller.clientWidth));
      });
    }

    stateByHost.set(host, { abort });
    return root;
  }

  window.DayGrid = Object.freeze({ mount, legend, gate });
})();
