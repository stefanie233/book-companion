// Story map — a view over writing.db, not a second source of truth.
//
// Layout is deterministic: chapters on a timeline, promises above it, volume
// bands behind. Dragging is deliberately absent in v1 — a moved node must not
// look like a changed fact.

const NS = 'http://www.w3.org/2000/svg';

function node(tag, attrs = {}, parent) {
  const el = document.createElementNS(NS, tag);
  Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
  if (parent) parent.appendChild(el);
  return el;
}

function text(parent, x, y, content, attrs = {}) {
  const el = node('text', { x, y, ...attrs }, parent);
  el.textContent = content;
  return el;
}

export function renderStoryMap(host, data, onOpenChapter) {
  host.textContent = '';
  const chapters = (data.nodes || []).filter((n) => n.kind === 'chapter')
    .sort((a, b) => a.chapter_index - b.chapter_index);
  if (!chapters.length) {
    host.textContent = '还没有章节。建一章之后，这里会长出故事地图。';
    return;
  }
  const promises = (data.nodes || []).filter((n) => n.kind === 'promise');
  const edges = data.edges || [];

  const width = Math.max(720, chapters.length * 92 + 160);
  const height = 400;
  const svg = node('svg', {
    viewBox: `0 0 ${width} ${height}`,
    width, height, role: 'img', 'aria-label': '故事地图',
  }, host);
  svg.style.minWidth = `${width}px`;

  const xOf = new Map();
  chapters.forEach((chapter, i) => {
    xOf.set(chapter.id, 90 + i * 92);
  });

  // Volume bands: derive grouping from contain edges — never recompute it here.
  const parentOf = new Map();
  edges.filter((e) => e.kind === 'contains')
    .forEach((e) => parentOf.set(e.to, e.from));
  const bands = new Map();
  chapters.forEach((chapter) => {
    const parent = parentOf.get(chapter.id) || 'work';
    if (!bands.has(parent)) bands.set(parent, []);
    bands.get(parent).push(chapter);
  });
  bands.forEach((members, parent) => {
    if (parent === 'work') return;
    const xs = members.map((c) => xOf.get(c.id));
    const left = Math.min(...xs) - 38;
    const right = Math.max(...xs) + 38;
    node('rect', {
      x: left, y: 196, width: right - left, height: 112, rx: 12,
      class: 'wmap-band',
    }, svg);
    text(svg, left + 10, 188, parent.replace('vol:', '卷 '),
      { class: 'wmap-band-label' });
  });

  // Promise nodes: spread above the timeline, tie to the chapter they touch.
  const px = new Map();
  promises.forEach((promise, i) => {
    const spread = promises.length > 1 ? i / (promises.length - 1) : 0.5;
    px.set(promise.id, 110 + spread * (width - 220));
  });

  edges.forEach((edge) => {
    // Edge endpoints are node ids already (`ch:…` / `pm:…`) — never re-derive.
    if (edge.kind === 'contains') return;
    if (edge.kind === 'must_payoff' && edge.from.startsWith('ch:')) {
      const x1 = xOf.get(edge.from);
      const x2 = px.get(edge.to);
      if (x1 == null || x2 == null) return;
      node('path', {
        d: `M ${x1} 236 Q ${(x1 + x2) / 2} 130 ${x2} 104`,
        class: 'wmap-edge wmap-edge-payoff',
      }, svg);
    }
    if (edge.kind === 'planted_in' && edge.to.startsWith('ch:')) {
      const x1 = px.get(edge.from);
      const x2 = xOf.get(edge.to);
      if (x1 == null || x2 == null) return;
      node('path', {
        d: `M ${x1} 112 Q ${(x1 + x2) / 2} 170 ${x2} 226`,
        class: 'wmap-edge wmap-edge-plant',
      }, svg);
    }
  });

  chapters.forEach((chapter) => {
    const x = xOf.get(chapter.id);
    const dot = node('circle', {
      cx: x, cy: 252, r: 13,
      class: `wmap-chapter wmap-status-${chapter.status || 'planned'}`,
    }, svg);
    dot.style.cursor = 'pointer';
    dot.onclick = () => onOpenChapter(chapter.chapter_index);
    const tip = node('title', {}, dot);
    tip.textContent = `第${chapter.chapter_index}章 ${chapter.label || ''}`
      + `\n${chapter.summary || '（暂无摘要）'}`
      + (chapter.tension == null ? '' : `\n紧张度 ${chapter.tension}`);
    text(svg, x, 286, String(chapter.chapter_index),
      { class: 'wmap-chapter-num', 'text-anchor': 'middle' });
    if (chapter.tension != null) {
      text(svg, x, 220, String(chapter.tension),
        { class: 'wmap-tension', 'text-anchor': 'middle' });
    }
  });

  promises.forEach((promise) => {
    const x = px.get(promise.id);
    const dot = node('circle', {
      cx: x, cy: 92, r: 10,
      class: 'wmap-promise',
    }, svg);
    const tip = node('title', {}, dot);
    tip.textContent = `${promise.label}\n状态 ${promise.status || ''}`
      + `\n权重 ${promise.weight ?? ''}`;
    const label = promise.label || '';
    text(svg, x, 74, label.length > 10 ? `${label.slice(0, 9)}…` : label,
      { class: 'wmap-promise-label', 'text-anchor': 'middle' });
  });

  text(svg, 20, 26, data.work ? data.work.title : '故事地图',
    { class: 'wmap-title' });
  node('line', { x1: 20, y1: 360, x2: width - 20, y2: 360,
    class: 'wmap-legend-line wmap-edge-payoff' }, svg);
  text(svg, 20, 382, '橙线：本章须兑现　灰线：承诺埋设于该章　点章节跳转',
    { class: 'wmap-legend' });
}
