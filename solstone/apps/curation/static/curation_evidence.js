// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function (global) {
  'use strict';

  // --- owner-facing strings ---
  const DRAWER_LABEL = 'evidence';
  const PIECE_SINGULAR = 'piece';
  const PIECE_PLURAL = 'pieces';
  const COUNT_OF = 'of';
  const META_SEPARATOR = ' · ';
  // --- end owner-facing strings ---

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    })[char]);
  }

  function samplesFor(item) {
    const samples = item?.evidence?.samples;
    return Array.isArray(samples) ? samples : [];
  }

  function totalCountFor(item, sampleCount) {
    const count = Math.trunc(Number(item?.evidence?.count));
    return Number.isFinite(count) && count > sampleCount ? count : sampleCount;
  }

  function formatEvidenceLine(displayedCount, totalCount) {
    if (displayedCount < totalCount) {
      return `${displayedCount} ${COUNT_OF} ${totalCount}`;
    }
    if (totalCount === 1) {
      return `1 ${PIECE_SINGULAR}`;
    }
    return `${totalCount} ${PIECE_PLURAL}`;
  }

  function sampleMeta(sample) {
    return [sample.stream, sample.segment || sample.segment_key]
      .map((part) => String(part ?? '').trim())
      .filter(Boolean)
      .join(META_SEPARATOR);
  }

  function renderSample(sample) {
    const meta = sampleMeta(sample);
    const metaHtml = meta ? `<span class="ev-meta">${escapeHtml(meta)}</span>` : '';
    return '<li class="drawer-evidence-row">' +
      `<span class="drawer-evidence-title">${escapeHtml(sample.day)}</span>` +
      metaHtml +
      '</li>';
  }

  function buildEvidenceDrawerProps(item, options = {}) {
    const samples = samplesFor(item);
    const sampleCount = samples.length;
    if (sampleCount === 0) return null;
    const totalCount = totalCountFor(item, sampleCount);
    const requestedMax = Number(options.maxSamples);
    const maxSamples = Number.isInteger(requestedMax) && requestedMax > 0
      ? requestedMax
      : sampleCount;
    const displayedSamples = samples.slice(0, maxSamples);
    const bodyHtml = `<ul class="drawer-evidence">${displayedSamples.map(renderSample).join('')}</ul>`;
    return {
      id: `curation-evidence:${String(item?.kind ?? '')}:${String(item?.key ?? '')}`,
      open: Boolean(options.open),
      label: DRAWER_LABEL,
      line: formatEvidenceLine(displayedSamples.length, totalCount),
      bodyHtml,
    };
  }

  const CurationEvidence = {
    buildEvidenceDrawerProps,
    formatEvidenceLine,
  };
  global.CurationEvidence = CurationEvidence;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = CurationEvidence;
  }
})(typeof window !== 'undefined' ? window : globalThis);
