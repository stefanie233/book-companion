/* BookGraph Progress UI — floating "Dynamic Island" pill + event feed.

 * Provides a top-center floating pill that shows extraction progress:
 *   - Collapsed:  🕸 {book_title} · {done}/{total} · {pct}%
 *   - Expanded:   phase dots + progress bar + scrolling todo-list event feed
 *   - Complete:   green pill → fades out after 3s
 *
 * Shared by graph-panel.js (modal) and graph-viz.js (graph page).
 * All DOM is created dynamically and mounted on document.body.
 */

import { S } from './state.js';
import { escapeHtml } from './utils.js';

// ══════════════════════════════════════════════════════════════════════
// Module-level state
// ══════════════════════════════════════════════════════════════════════

let eventSource = null;         // active EventSource (null when idle)
let currentBookId = null;       // book_id currently being tracked
let currentBookTitle = '';      // book title
let extractionStats = null;     // latest cumulative stats from SSE
let startTime = null;           // Date.now() when extraction started
let batchTimings = [];          // last 5 batch durations (ms) for ETA
let lastBatchTime = null;       // Date.now() when current batch began
let isComplete = false;         // true after 'complete' event received

// ══════════════════════════════════════════════════════════════════════
// DOM helpers — lazy-create the floating pill once
// ══════════════════════════════════════════════════════════════════════

function ensurePillElement() {
  if (document.getElementById('graphFloatingPill')) return;

  const pill = document.createElement('div');
  pill.id = 'graphFloatingPill';
  pill.className = 'graph-floating-pill collapsed';
  pill.innerHTML = `
    <div class="gfp-header" id="gfpHeader">
      <span class="gfp-icon"><i data-lucide="network"></i></span>
      <span class="gfp-title" id="gfpTitle">…</span>
      <span class="gfp-progress" id="gfpProgress">0/0 · 0%</span>
      <span class="gfp-chevron"><i data-lucide="chevron-right"></i></span>
    </div>
    <div class="gfp-body" id="gfpBody">
      <div class="gfp-phase-list" id="gfpPhaseList"></div>
      <div class="gfp-bar-wrap">
        <div class="gfp-bar" id="gfpBar" style="transform:scaleX(0)"></div>
      </div>
      <div class="gfp-event-feed" id="gfpEventFeed"></div>
      <div class="gfp-footer" id="gfpFooter">
        <span id="gfpEta"></span>
        <span id="gfpCumulative"></span>
      </div>
    </div>
  `;
  document.body.appendChild(pill);

  // Click header to toggle expand/collapse
  document.getElementById('gfpHeader').addEventListener('click', toggleGraphPill);
}

function getPill() {
  return document.getElementById('graphFloatingPill');
}

// ══════════════════════════════════════════════════════════════════════
// Collapse / expand
// ══════════════════════════════════════════════════════════════════════

function expandPill() {
  const pill = getPill();
  if (pill) pill.classList.remove('collapsed');
  pill.classList.add('expanded');
  // Scroll to the active (in-progress) event
  requestAnimationFrame(() => {
    const active = document.querySelector('.gfp-event-icon.active');
    if (active) {
      active.closest('.gfp-event').scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  });
}

function collapsePill() {
  const pill = getPill();
  if (pill) {
    pill.classList.remove('expanded');
    pill.classList.add('collapsed');
  }
}

function toggleGraphPill() {
  // Don't toggle if complete (header is not interactive in complete state)
  const pill = getPill();
  if (!pill || pill.classList.contains('complete')) return;
  if (pill.classList.contains('expanded')) {
    collapsePill();
  } else {
    expandPill();
  }
}

// ══════════════════════════════════════════════════════════════════════
// Public API
// ══════════════════════════════════════════════════════════════════════

/**
 * Start tracking extraction progress for a book.
 * Creates the floating pill, connects to SSE, and manages lifecycle.
 *
 * @param {string} bookId
 * @param {string} bookTitle
 */
export function startExtractionProgress(bookId, bookTitle) {
  // If already tracking this book, just expand
  if (currentBookId === bookId && eventSource && !isComplete) {
    expandPill();
    return;
  }

  // Close any previous extraction tracking
  dismissPill();

  currentBookId = bookId;
  currentBookTitle = bookTitle;
  isComplete = false;
  extractionStats = null;
  startTime = Date.now();
  batchTimings = [];
  lastBatchTime = null;

  ensurePillElement();

  // Reset DOM state
  const pill = getPill();
  pill.classList.remove('complete', 'fading', 'expanded');
  pill.classList.add('collapsed');
  document.getElementById('gfpTitle').textContent = bookTitle || bookId;
  document.getElementById('gfpProgress').textContent = '启动...';
  document.getElementById('gfpBar').style.transform = 'scaleX(0)';
  document.getElementById('gfpBar').classList.remove('has-failed');
  document.getElementById('gfpPhaseList').innerHTML = '';
  document.getElementById('gfpEventFeed').innerHTML = '';
  document.getElementById('gfpEta').textContent = '';
  document.getElementById('gfpCumulative').textContent = '';

  // Auto-expand
  expandPill();

  // Connect SSE
  connectSSE(bookId);
}

/**
 * Dismiss the floating pill (close SSE + remove DOM).
 */
export function dismissPill() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  currentBookId = null;
  currentBookTitle = '';
  isComplete = false;

  const pill = getPill();
  if (pill) pill.remove();
}

/**
 * Check whether an extraction is currently being tracked.
 */
export function isExtractionRunning(bookId) {
  return currentBookId === bookId && eventSource !== null && !isComplete;
}

// ══════════════════════════════════════════════════════════════════════
// SSE connection
// ══════════════════════════════════════════════════════════════════════

let reconnectTimer = null;
let reconnectAttempts = 0;
const MAX_RECONNECT = 5;

function connectSSE(bookId) {
  if (eventSource) eventSource.close();
  if (reconnectTimer) clearTimeout(reconnectTimer);

  const url = S.API + '/api/graph/progress/' + bookId;
  eventSource = new EventSource(url);

  eventSource.onmessage = function (event) {
    reconnectAttempts = 0;  // reset on successful message
    try {
      const data = JSON.parse(event.data);
      handleEvent(data);
    } catch (e) {
      console.warn('[graph-progress-ui] bad SSE data:', e);
    }
  };

  eventSource.onerror = function () {
    eventSource.close();
    eventSource = null;

    if (isComplete) return;  // normal close after complete

    reconnectAttempts++;
    if (reconnectAttempts <= MAX_RECONNECT) {
      const delay = Math.min(1000 * reconnectAttempts, 10000);
      updateHeaderProgress(null, null, '<i data-lucide="triangle-alert"></i> 重连中...');
      reconnectTimer = setTimeout(() => connectSSE(bookId), delay);
    } else {
      updateHeaderProgress(null, null, '<i data-lucide="triangle-alert"></i> 连接中断');
    }
  };
}

// ══════════════════════════════════════════════════════════════════════
// Event handler — dispatches by event type
// ══════════════════════════════════════════════════════════════════════

function handleEvent(data) {
  switch (data.type) {

    // ── Phase events ──────────────────────────────────────────────
    case 'context_set':
      addPhaseDot('ContextSetter', 'done',
        data.genre ? `${data.genre}` : '世界观边界');
      addFeedEvent('done', 'ContextSetter',
        data.genre
          ? `已确定世界观边界 · ${data.genre}`
          : '已确定世界观边界',
        data.suggested_kinds && data.suggested_kinds.length
          ? '实体类型: ' + data.suggested_kinds.join(', ')
          : '');
      break;

    case 'scout_done':
      addPhaseDot('Scout', 'done',
        `${data.scout_batches || data.narrative_units || '?'} 叙事单元`
        + (data.excluded_chunks ? `, ${data.excluded_chunks} 排除` : ''));
      addFeedEvent('done', 'Scout',
        `已规划 ${data.scout_batches || '?'} 个批次`
        + (data.narrative_units ? ` · ${data.narrative_units} 叙事单元` : '')
        + (data.excluded_chunks ? ` · 排除 ${data.excluded_chunks} chunks` : ''),
        '');
      // Add pending batch placeholders based on scout plan
      if (data.scout_batches && data.scout_batches > 0) {
        prefillPendingBatches(data.scout_batches);
      }
      break;

    case 'scopes_planned':
      // 集类书才有这个事件：各篇自成一体，批次不跨篇（单域不发，见 scope.py）
      addPhaseDot('叙事域', 'done', `${data.scope_count} 篇`);
      addFeedEvent('done', '叙事域',
        `识别为集类 · ${data.scope_count} 篇各自成域`,
        (data.labels || []).join(' · '));
      break;

    case 'batch_unverified':
      // 「沉默不是成功」：没验干净的批次要在进度条上看得见，不能混在一堆
      // 「完成」里冒充收敛。
      addFeedEvent('warn', '未验证',
        `第 ${data.batch_index} 批未通过验证 · ${data.verify?.verdict || '?'}`,
        `回炉 ${data.verify?.rounds || 0} 轮，残余 ${data.verify?.residual || 0} 条`);
      break;

    case 'content_filtered':
      addFeedEvent('done', 'ContentFilter',
        `过滤 ${data.excluded_count} chunks`,
        data.content_warnings && data.content_warnings.length
          ? data.content_warnings.map(cw =>
              `${cw.chunk_count}个: ${cw.reason}`).join(' · ')
          : '');
      break;

    // ── Batch events ──────────────────────────────────────────────
    case 'batch_start':
      lastBatchTime = Date.now();
      addPhaseDot('提取', 'active');
      upsertBatchEvent(data.batch_index, data.batch_index, data.total_batches,
        data.step_label, 'active', '', '');
      updateProgress(data);
      break;

    case 'batch_done':
      if (lastBatchTime) {
        batchTimings.push(Date.now() - lastBatchTime);
        if (batchTimings.length > 5) batchTimings.shift();
        lastBatchTime = null;
      }
      upsertBatchEvent(data.batch_index, data.batch_index, data.total_batches,
        data.step_label, 'done', '',
        formatBatchStats(data.detail));
      updateProgress(data);
      break;

    case 'batch_failed':
      if (lastBatchTime) lastBatchTime = null;
      upsertBatchEvent(data.batch_index, data.batch_index, data.total_batches,
        data.step_label, 'failed', '',
        data.detail && data.detail.error_reason
          ? formatErrorReason(data.detail.error_reason)
          : '');
      updateProgress(data);
      break;

    case 'batch_skipped':
      upsertBatchEvent(data.batch_index, data.batch_index, data.total_batches,
        data.step_label || `Batch ${data.batch_index}`, 'done',
        '(已跳过)', '');
      updateProgress(data);
      break;

    // ── Resume ────────────────────────────────────────────────────
    case 'resume':
      updateHeaderProgress(data.done_chunks, data.total_chunks);
      addFeedEvent('done', 'Resume',
        `续接: ${data.done_chunks}/${data.total_chunks} 已完成`, '');
      break;

    // ── Terminal events ───────────────────────────────────────────
    case 'complete':
      handleComplete(data);
      break;

    case 'error':
      addFeedEvent('failed', 'Error', data.message || '未知错误', '');
      updateHeaderProgress(null, null, '<i data-lucide="triangle-alert"></i> 提取失败');
      break;

    // ── Legacy progress type (from DB polling fallback) ───────────
    case 'progress':
      handleLegacyProgress(data);
      break;

    default:
      // Unknown event — best-effort: treat as feed item
      if (data.batch_index && data.step_label) {
        const icon = data.status === 'done' ? 'done' :
          data.status === 'failed' ? 'failed' :
          data.status === 'in_progress' ? 'active' : 'pending';
        upsertBatchEvent(data.batch_index, data.batch_index, data.total_batches,
          data.step_label, icon, '',
          data.detail ? formatBatchStats(data.detail) : '');
      }
      updateProgress(data);
  }
}

// ── Legacy progress events (DB fallback) ────────────────────────────

function handleLegacyProgress(data) {
  const stats = data.stats || {};
  const done = stats.chunks_done || 0;
  const total = stats.chunks_total || 1;
  const detail = data.detail || {};

  let icon = 'pending';
  if (data.status === 'done') icon = 'done';
  else if (data.status === 'failed') icon = 'failed';
  else if (data.status === 'in_progress') icon = 'active';

  upsertBatchEvent(data.batch_index, data.batch_index, total,
    data.step_label, icon, '',
    formatBatchStats(detail));

  updateHeaderProgress(done, total);
  document.getElementById('gfpBar').style.transform =
    'scaleX(' + (done / total) + ')';
  updateFooterStats(stats);
}

// ══════════════════════════════════════════════════════════════════════
// UI update helpers
// ══════════════════════════════════════════════════════════════════════

function updateProgress(data) {
  const done = data.done_chunks != null ? data.done_chunks
    : (data.stats && data.stats.chunks_done != null ? data.stats.chunks_done : null);
  const total = data.total_chunks != null ? data.total_chunks
    : (data.stats && data.stats.chunks_total != null ? data.stats.chunks_total : null);

  if (done != null && total != null) {
    updateHeaderProgress(done, total);
    const pct = Math.round(done / total * 100);
    const bar = document.getElementById('gfpBar');
    if (bar) {
      bar.style.transform = 'scaleX(' + (pct / 100) + ')';
      // Check for failed chunks
      if (data.stats && data.stats.chunks_failed > 0) {
        bar.classList.add('has-failed');
      }
    }
    updateEta(done, total);
  }

  if (data.stats) {
    updateFooterStats(data.stats);
  }
  if (data.final_stats) {
    updateFooterStats(data.final_stats);
  }
}

function updateHeaderProgress(done, total, customText) {
  const el = document.getElementById('gfpProgress');
  if (!el) return;
  if (customText) {
    el.textContent = customText;
  } else if (done != null && total != null) {
    const pct = Math.round(done / total * 100);
    el.textContent = `${done}/${total} · ${pct}%`;
  }
}

function updateFooterStats(stats) {
  extractionStats = stats;
  const el = document.getElementById('gfpCumulative');
  if (!el || !stats) return;
  const parts = [];
  if (stats.nodes) parts.push(`${stats.nodes}实体`);
  if (stats.edges) parts.push(`${stats.edges}关系`);
  if (stats.aliases) parts.push(`${stats.aliases}别名`);
  if (stats.quotes) parts.push(`${stats.quotes || 0}台词`);
  el.innerHTML = '<i data-lucide="bar-chart-3"></i> ' + (parts.length ? parts.join(' · ') : '—');
}

function updateEta(done, total) {
  const el = document.getElementById('gfpEta');
  if (!el) return;

  if (done >= total) {
    const elapsed = startTime ? formatDuration(Date.now() - startTime) : '';
    el.innerHTML = elapsed ? `<i data-lucide="timer"></i> ${elapsed}` : '';
    return;
  }

  // Estimate from recent batch timings
  const avgMs = batchTimings.length > 0
    ? batchTimings.reduce((a, b) => a + b, 0) / batchTimings.length
    : null;

  const elapsed = startTime ? formatDuration(Date.now() - startTime) : '';
  if (avgMs && avgMs > 0) {
    const remaining = Math.round((total - done) * avgMs);
    el.innerHTML = `<i data-lucide="timer"></i> ${elapsed} · 预计剩余 ${formatDuration(remaining)}`;
  } else if (elapsed) {
    el.innerHTML = `<i data-lucide="timer"></i> ${elapsed}`;
  }
}

function formatDuration(ms) {
  if (ms < 1000) return '0s';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  const rs = s % 60;
  return m + 'm' + (rs > 0 ? rs + 's' : '');
}

function formatBatchStats(detail) {
  if (!detail) return '';
  const parts = [];
  if (detail.new_nodes) parts.push(`+${detail.new_nodes}节点`);
  if (detail.new_edges) parts.push(`+${detail.new_edges}边`);
  if (detail.new_aliases) parts.push(`+${detail.new_aliases}别名`);
  if (detail.new_quotes) parts.push(`+${detail.new_quotes}台词`);
  if (detail.merge_candidates) parts.push(`±${detail.merge_candidates}合并候选`);
  return parts.join(' ');
}

function formatErrorReason(reason) {
  const map = {
    timeout: '超时',
    api_error: 'API 错误',
    parse_failed: '解析失败',
    exhausted_retries: '重试耗尽',
  };
  return map[reason] ? `(${map[reason]})` : `(${reason})`;
}

// ══════════════════════════════════════════════════════════════════════
// Phase dots
// ══════════════════════════════════════════════════════════════════════

function addPhaseDot(name, state, detail) {
  const list = document.getElementById('gfpPhaseList');
  if (!list) return;

  // Remove existing dot with same name
  const existing = list.querySelector(`[data-phase="${name}"]`);
  if (existing) existing.remove();

  const dot = document.createElement('span');
  dot.className = `gfp-phase-dot ${state}`;
  dot.dataset.phase = name;
  dot.innerHTML = state === 'done' ? `<i data-lucide="check"></i> ${name}` : `● ${name}`;
  if (detail) dot.title = detail;
  list.appendChild(dot);
}

// ══════════════════════════════════════════════════════════════════════
// Event feed — scrolling todo-list style
// ══════════════════════════════════════════════════════════════════════

let pendingBatchPlaceholders = 0;

function prefillPendingBatches(count) {
  pendingBatchPlaceholders = count;
  const feed = document.getElementById('gfpEventFeed');
  if (!feed) return;

  // Remove existing pending placeholders
  feed.querySelectorAll('.gfp-event.pending-placeholder').forEach(el => el.remove());

  let html = '';
  for (let i = 1; i <= count; i++) {
    html += `
      <div class="gfp-event pending-placeholder" data-batch="${i}">
        <span class="gfp-event-icon pending">○</span>
        <span class="gfp-event-label">Batch ${i}</span>
        <span class="gfp-event-stats"></span>
      </div>
      <div class="gfp-event-detail" data-batch-detail="${i}"></div>
    `;
  }
  feed.insertAdjacentHTML('beforeend', html);
}

function upsertBatchEvent(batchIndex, _batchIdxDisp, totalBatches, stepLabel, icon, suffix, stats) {
  const feed = document.getElementById('gfpEventFeed');
  if (!feed) return;

  // Try to find existing event row for this batch
  let row = feed.querySelector(`.gfp-event[data-batch="${batchIndex}"]`);
  let detailRow = feed.querySelector(`[data-batch-detail="${batchIndex}"]`);

  if (!row) {
    // Create new row
    const div = document.createElement('div');
    div.className = 'gfp-event';
    div.dataset.batch = batchIndex;
    div.innerHTML = `
      <span class="gfp-event-icon ${icon}">${iconToChar(icon)}</span>
      <span class="gfp-event-label">${escapeHtml(stepLabel || `Batch ${batchIndex}`)}${suffix ? ' ' + suffix : ''}</span>
      <span class="gfp-event-stats">${escapeHtml(stats)}</span>
    `;
    // Insert before first pending-placeholder, or append
    const placeholder = feed.querySelector('.pending-placeholder');
    if (placeholder) {
      feed.insertBefore(div, placeholder);
    } else {
      feed.appendChild(div);
    }
    // Add detail row
    if (stats) {
      const dRow = document.createElement('div');
      dRow.className = 'gfp-event-detail';
      dRow.dataset.batchDetail = batchIndex;
      dRow.textContent = stats;
      feed.insertBefore(dRow, placeholder || null);
    }
  } else {
    // Update existing
    const iconEl = row.querySelector('.gfp-event-icon');
    if (iconEl) {
      iconEl.className = `gfp-event-icon ${icon}`;
      iconEl.textContent = iconToChar(icon);
    }
    const labelEl = row.querySelector('.gfp-event-label');
    if (labelEl) {
      labelEl.innerHTML = escapeHtml(stepLabel || `Batch ${batchIndex}`)
        + (suffix ? ' <span style="color:var(--text-secondary)">' + escapeHtml(suffix) + '</span>' : '');
    }
    const statsEl = row.querySelector('.gfp-event-stats');
    if (statsEl) statsEl.textContent = stats;
    if (detailRow) detailRow.textContent = stats;
  }

  // Remove matching placeholder if this batch is now real
  const placeholder = feed.querySelector(`.pending-placeholder[data-batch="${batchIndex}"]`);
  if (placeholder && icon !== 'pending') {
    const pDetail = feed.querySelector(`[data-batch-detail="${batchIndex}"]`);
    placeholder.remove();
    if (pDetail && pDetail.classList.contains('pending-placeholder')) pDetail.remove();
  }

  // Scroll to active batch
  if (icon === 'active') {
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}

function iconToChar(icon) {
  switch (icon) {
    case 'done': return '<i data-lucide="check"></i>';
    case 'active': return '⟳';
    case 'failed': return '<i data-lucide="x"></i>';
    case 'warn': return '<i data-lucide="alert-triangle"></i>';
    case 'pending': return '○';
    default: return '·';
  }
}

function addFeedEvent(icon, label, text, detail) {
  const feed = document.getElementById('gfpEventFeed');
  if (!feed) return;

  const div = document.createElement('div');
  div.className = 'gfp-event';
  div.innerHTML = `
    <span class="gfp-event-icon ${icon}">${iconToChar(icon)}</span>
    <span class="gfp-event-label">${escapeHtml(label)}: ${escapeHtml(text)}</span>
    <span class="gfp-event-stats">${escapeHtml(detail)}</span>
  `;
  feed.appendChild(div);
  div.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

// ══════════════════════════════════════════════════════════════════════
// Completion
// ══════════════════════════════════════════════════════════════════════

function handleComplete(data) {
  isComplete = true;

  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }

  const stats = data.stats || data.final_stats || extractionStats || {};
  const done = stats.chunks_done || stats.chunks_total || 0;
  const total = stats.chunks_total || done;

  // Update header to completion
  document.getElementById('gfpProgress').innerHTML = '<i data-lucide="check"></i> 完成 · 100%';
  document.getElementById('gfpTitle').textContent = currentBookTitle || data.book_id;

  // Mark bar full
  const bar = document.getElementById('gfpBar');
  if (bar) {
    bar.style.transform = 'scaleX(1)';
    bar.classList.remove('has-failed');
  }

  // Collapse the body — only header visible in complete state
  collapsePill();

  // Add complete class (green background)
  const pill = getPill();
  if (pill) {
    pill.classList.add('complete');

    // After 3s, start fade-out
    setTimeout(() => {
      if (pill) {
        pill.classList.add('fading');
        pill.addEventListener('animationend', () => {
          pill.remove();
          // Reset module state
          currentBookId = null;
          currentBookTitle = '';
          isComplete = false;
          extractionStats = null;
        }, { once: true });
      }
    }, 3000);
  }

  // Update footer
  updateFooterStats(stats);
}

// ══════════════════════════════════════════════════════════════════════
// Window exports (for HTML onclick and cross-module access)
// ══════════════════════════════════════════════════════════════════════

window.startExtractionProgress = startExtractionProgress;
window.dismissGraphPill = dismissPill;
window.toggleGraphPill = toggleGraphPill;
window.isExtractionRunning = isExtractionRunning;
