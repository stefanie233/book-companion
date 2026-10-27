// Tension curve — one point per chapter, user anchors win over AI estimates.
// The chart is advisory only: it never edits prose.

const NS = 'http://www.w3.org/2000/svg';

function node(tag, attrs = {}, parent) {
  const el = document.createElementNS(NS, tag);
  Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
  if (parent) parent.appendChild(el);
  return el;
}

function label(parent, x, y, content, attrs = {}) {
  const el = node('text', { x, y, ...attrs }, parent);
  el.textContent = content;
  return el;
}

export function renderTensionCurve(host, data, { onSet } = {}) {
  host.textContent = '';
  const points = (data.points || []).filter((p) => p.chapter_index != null);
  if (!points.length) {
    host.textContent = '还没有章节，曲线没有可画的点。';
    return;
  }

  const width = Math.max(640, points.length * 64 + 120);
  const height = 300;
  const top = 40;
  const bottom = 250;
  const svg = node('svg', {
    viewBox: `0 0 ${width} ${height}`, width, height,
    role: 'img', 'aria-label': '紧张度曲线',
  }, host);
  svg.style.minWidth = `${width}px`;

  const xOf = (i) => 70 + i * 64;
  const yOf = (value) => bottom - (value / 100) * (bottom - top);

  [0, 25, 50, 75, 100].forEach((tick) => {
    const y = yOf(tick);
    node('line', { x1: 56, y1: y, x2: width - 24, y2: y,
      class: 'wt-grid' }, svg);
    label(svg, 48, y + 4, String(tick),
      { class: 'wt-axis', 'text-anchor': 'end' });
  });

  let path = '';
  points.forEach((point, i) => {
    if (point.value == null) return;
    path += `${path ? ' L' : 'M'} ${xOf(i)} ${yOf(point.value)}`;
  });
  if (path) {
    node('path', { d: path, class: 'wt-line' }, svg);
  }

  points.forEach((point, i) => {
    const x = xOf(i);
    label(svg, x, 274, String(point.chapter_index),
      { class: 'wt-axis', 'text-anchor': 'middle' });
    if (point.value == null) {
      node('circle', { cx: x, cy: bottom, r: 3, class: 'wt-missing' }, svg);
      return;
    }
    const dot = node('circle', {
      cx: x, cy: yOf(point.value), r: point.source === 'user' ? 6 : 5,
      class: `wt-dot wt-source-${point.source || 'ai'}`,
    }, svg);
    dot.style.cursor = 'pointer';
    const tip = node('title', {}, dot);
    tip.textContent = `第${point.chapter_index}章 紧张度 ${point.value}`
      + `（${point.source === 'user' ? '作者锚点' : 'AI 估算'}）`
      + (point.flags && point.flags.length ? `\n标记：${point.flags.join('、')}` : '');
    dot.onclick = () => {
      if (!onSet) return;
      const input = window.prompt(
        `第${point.chapter_index}章紧张度（0–100）`, String(point.value));
      if (input == null) return;
      const value = Number.parseInt(input, 10);
      if (Number.isNaN(value) || value < 0 || value > 100) {
        window.alert('请输入 0–100 的整数');
        return;
      }
      onSet(point.chapter_index, value);
    };
    if (point.flags && point.flags.includes('flat')) {
      node('circle', { cx: x, cy: yOf(point.value), r: 10,
        class: 'wt-flag-flat' }, svg);
    }
    if (point.flags && point.flags.includes('weak_volume_end')) {
      label(svg, x, yOf(point.value) - 14, '卷末弱',
        { class: 'wt-flag-label', 'text-anchor': 'middle' });
    }
  });

  if (data.hints && data.hints.length) {
    const list = document.createElement('div');
    list.className = 'wt-hints';
    data.hints.forEach((hint) => {
      const item = document.createElement('div');
      item.className = 'wt-hint';
      item.textContent = hint;
      list.appendChild(item);
    });
    host.appendChild(list);
  }
}
