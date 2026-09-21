/* Book Companion — pure utility functions. */

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
 * Look up a chapter name from the chapter <select> element.
 */
export function getChapterName(chIdx) {
  const chSelect = document.getElementById('chapterSelect');
  if (chSelect && chSelect.options[chIdx]) {
    return chSelect.options[chIdx].text;
  }
  return '第' + chIdx + '章';
}
