/* Liubai — pure utility functions. */

/**
 * Escape HTML special characters.
 */
export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Escape string for safe embedding in HTML attribute values
 * (single-quoted or double-quoted).
 */
export function escapeHtmlAttr(str) {
  return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"');
}

/**
 * Chinese-aware case-insensitive string comparison.
 */
export function compareStringAsc(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return String(a).localeCompare(String(b), 'zh-CN', { sensitivity: 'base' });
}

/**
 * Format a millisecond timestamp for session list display.
 */
export function formatSessionTime(tsMs) {
  if (!tsMs) return '?';
  const d = new Date(tsMs);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((today - that) / 86400000);
  if (diffDays === 0) return d.toTimeString().slice(0, 5);
  if (diffDays === 1) return '昨天';
  if (diffDays < 7) return diffDays + '天前';
  if (d.getFullYear() === now.getFullYear())
    return (d.getMonth() + 1) + '-' + d.getDate();
  return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
}

/**
 * 全书阅读进度（按 chunk 序）。
 *
 * `offsets` 是 {chapter_index: 本章之前的 chunk 累计}，`total` 是全书
 * chunk 总数。返回 {pct, idx, total}；结构未知时返回 null。
 * chunk 长 400–1100 字，按段数算进度有少量局部误差，只用于进度示数。
 */
export function bookProgress(chapter, chunk, offsets, total) {
  const n = Number(total) || 0;
  if (!n) return null;
  const base = (offsets && offsets[chapter]) || 0;
  const idx = Math.min(base + (Number(chunk) || 0) + 1, n);
  let pct = Math.round((idx / n) * 100);
  if (pct < 1) pct = 1;          // 刚开书也显示 1%，不要停在 0%
  return { pct, idx, total: n };
}

/**
 * 把全书进度写进示数元素。右上角 (#chunkPos) 与沉浸阅读底栏
 * (#focusProgress) 共用这一份数字，避免两处各算各的、慢慢漂开。
 */
export function paintProgress(el, p) {
  if (!el) return;
  if (!p) { el.textContent = ''; el.removeAttribute('title'); return; }
  el.textContent = p.pct + '%';
  el.title = '全书 ' + p.idx + ' / ' + p.total + ' 段';
}

/**
 * Look up a chapter name from the chapter <select> element.
 */
export function getChapterName(chIdx) {
  const chSelect = document.getElementById('chapterSelect');
  if (chSelect && chSelect.options[chIdx]) {
    return chSelect.options[chIdx].text;
  }
  return '第' + chIdx + '章';
}
