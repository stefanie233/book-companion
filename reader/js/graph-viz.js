/* Book Companion — knowledge graph visualization (Cytoscape.js).

 * Provides: interactive graph canvas, type-wheel filter, node detail panel.
 * Activated by the "图谱" sidebar tab.
 */

import { S } from './state.js';
import { escapeHtml, escapeHtmlAttr } from './utils.js';

// ══════════════════════════════════════════════════════════════════════
// Constants
// ══════════════════════════════════════════════════════════════════════

// 暖暗底和谐色板：降饱和 + 微暖偏移，与 #1b1815 画布同族
const KIND_COLORS = {
  character: '#7EB6E6', event: '#E5A65D', location: '#85B98E',
  concept: '#C3A1D8', item: '#B0A696', chapter: '#8695AB',
};

// 每种类型的浅一档描边色（同族提亮，制造细腻层次）
const KIND_BORDERS = {
  character: '#A8D0F0', event: '#F0C48E', location: '#A9D4B0',
  concept: '#DBC4EA', item: '#CFC7BA', chapter: '#AAB8CC',
};

const ACCENT = '#00afff';

// ══════════════════════════════════════════════════════════════════════
// Edge styling helpers — 边"属于"它的两个端点
// ══════════════════════════════════════════════════════════════════════

// 两个 hex 颜色按比例混合
function mixHex(a, b, t) {
  const pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
  const r = Math.round(((pa >> 16) & 255) * (1 - t) + ((pb >> 16) & 255) * t);
  const g = Math.round(((pa >> 8) & 255) * (1 - t) + ((pb >> 8) & 255) * t);
  const bl = Math.round((pa & 255) * (1 - t) + (pb & 255) * t);
  return '#' + ((1 << 24) + (r << 16) + (g << 8) + bl).toString(16).slice(1);
}

// core 节点球体高光：中心提亮 22% -> 边缘原色，暗底上的立体材质感
const CORE_GRADIENTS = {
  character: [mixHex(KIND_COLORS.character, '#ffffff', 0.22), KIND_COLORS.character],
  event: [mixHex(KIND_COLORS.event, '#ffffff', 0.22), KIND_COLORS.event],
  location: [mixHex(KIND_COLORS.location, '#ffffff', 0.22), KIND_COLORS.location],
  concept: [mixHex(KIND_COLORS.concept, '#ffffff', 0.22), KIND_COLORS.concept],
  item: [mixHex(KIND_COLORS.item, '#ffffff', 0.22), KIND_COLORS.item],
  chapter: [mixHex(KIND_COLORS.chapter, '#ffffff', 0.22), KIND_COLORS.chapter],
};

const EDGE_BASE_GRAY = '#4a443c';

// 常态：端点色 35% 混入暖灰 —— 协调、哑光、可辨来源
function edgeGradient(ele) {
  const s = KIND_COLORS[ele.source().data('kind')] || EDGE_BASE_GRAY;
  const t = KIND_COLORS[ele.target().data('kind')] || EDGE_BASE_GRAY;
  return [mixHex(EDGE_BASE_GRAY, s, 0.35), mixHex(EDGE_BASE_GRAY, t, 0.35)];
}

// 悬停：端点色 75% —— 鲜活的 identity 闪现
function edgeGradientVivid(ele) {
  const s = KIND_COLORS[ele.source().data('kind')] || EDGE_BASE_GRAY;
  const t = KIND_COLORS[ele.target().data('kind')] || EDGE_BASE_GRAY;
  return [mixHex(EDGE_BASE_GRAY, s, 0.75), mixHex(EDGE_BASE_GRAY, t, 0.75)];
}

// 对数宽度：低权重差异拉开，高权重不臃肿
function edgeWidth(ele) {
  const c = ele.data('edge_count') || 1;
  return 0.9 + Math.log2(c + 1) * 1.05;
}

const KIND_LABELS = {
  character: '人物', event: '事件', location: '地点',
  concept: '概念', item: '物品', chapter: '章节',
};

const LAYOUT_LABELS = { force: '力导向', breadthfirst: '层级' };

// fcose 引擎可用性（lib/cytoscape-fcose.js 加载成功时注册）
const HAS_FCOSE = typeof window.cytoscapeFcose === 'function';
if (HAS_FCOSE && window.cytoscape) {
  window.cytoscape.use(window.cytoscapeFcose);
}

// 力导向引擎名：fcose 优先，缺失时回退内置 cose
const FORCE_ENGINE = HAS_FCOSE ? 'fcose' : 'cose';

// ── 初始布局：全量高质量结算 ─────────────────────────────────
const FORCE_LAYOUT_FULL = FORCE_ENGINE === 'fcose' ? {
  name: 'fcose', quality: 'proof', randomize: true,
  animate: true, animationDuration: 800, animationEasing: 'ease-out',
  nodeRepulsion: 12000,
  idealEdgeLength: 120,
  edgeElasticity: 0.45,
  gravity: 0.28,
  nodeSeparation: 140,
  packComponents: true,
  numIter: 2600,
  fit: false,
} : {
  name: 'cose', animate: 'end', animationDuration: 650, animationEasing: 'ease-out',
  nodeRepulsion: function () { return 38000; },
  idealEdgeLength: function () { return 150; },
  edgeElasticity: function () { return 40; },
  nodeOverlap: 50, gravity: 0.08, numIter: 3500,
  coolingFactor: 0.995, randomize: true, componentSpacing: 170, fit: false,
};

// ── 增量布局：已废弃。B 方案下 free 事件改用弹簧归位，不跑 fcose 松弛。
// 保留 FORCE_LAYOUT_RELAX 常量定义已移除，如需回退 C 方案（果冻松弛）可从 git 恢复。

const LAYOUT_DEFAULTS = {
  'force': FORCE_LAYOUT_FULL,
  'breadthfirst': {
    name: 'breadthfirst', directed: false, circle: false,
    spacingFactor: 1.5, animate: true, animationDuration: 500, fit: false,
  },
};

// ══════════════════════════════════════════════════════════════════════
// Internal state (not exported, not on window)
// ══════════════════════════════════════════════════════════════════════

let cy = null;                  // Cytoscape singleton
let currentLayout = 'force';
let typeWheelState = {};        // { character: 'selected'|'connected'|'hidden', ... }
let selectedNodeId = null;      // currently open in detail panel
let currentGraphBookId = null;  // bookId currently rendered
let booksWithGraph = [];        // [{ book_id, title, node_count }]
let graphLoading = false;       // guard against concurrent loadGraphForBook calls

// ══════════════════════════════════════════════════════════════════════
// One-time page init (idempotent)
// ══════════════════════════════════════════════════════════════════════

export async function initGraphPage() {
  if (cy) return;  // already initialised

  const container = document.getElementById('graphCanvas');
  if (!container) return;

  // Cytoscape not loaded yet?
  if (!window.cytoscape) {
    console.warn('Cytoscape.js not loaded');
    return;
  }

  cy = window.cytoscape({
    container: container,
    style: [
      // ── Node base：标签居下 + 非核心压暗沉背景 ────────────────
      { selector: 'node', style: {
        'label': 'data(label)',
        'font-size': 'mapData(size, 14, 36, 9, 13)',
        'font-weight': 500,
        'text-valign': 'bottom',
        'text-halign': 'center',
        'text-margin-y': 7,
        'color': '#d8d2c8',
        'text-outline-color': '#1b1815',
        'text-outline-width': 3,
        'width': 'data(size)',
        'height': 'data(size)',
        'border-width': 1.5,
        'border-opacity': 0.9,
        'background-blacken': 0.12,
        'transition-property': 'opacity, text-opacity, border-width, overlay-opacity, underlay-opacity',
        'transition-duration': 220,
      }},
      // ── Node kinds（填充 + 同族浅描边） ─────────────────────
      { selector: 'node[kind="character"]', style: { 'background-color': KIND_COLORS.character, 'border-color': KIND_BORDERS.character }},
      { selector: 'node[kind="event"]',      style: { 'background-color': KIND_COLORS.event,      'border-color': KIND_BORDERS.event }},
      { selector: 'node[kind="location"]',   style: { 'background-color': KIND_COLORS.location,   'border-color': KIND_BORDERS.location }},
      { selector: 'node[kind="concept"]',    style: { 'background-color': KIND_COLORS.concept,    'border-color': KIND_BORDERS.concept }},
      { selector: 'node[kind="item"]',       style: { 'background-color': KIND_COLORS.item,       'border-color': KIND_BORDERS.item }},
      { selector: 'node[kind="chapter"]',    style: { 'background-color': KIND_COLORS.chapter,    'border-color': KIND_BORDERS.chapter }},
      // ── 核心节点：球体高光（radial-gradient）+ 光晕，鲜亮不压暗 ─
      { selector: 'node.core[kind="character"]', style: { 'underlay-color': KIND_COLORS.character, 'background-fill': 'radial-gradient', 'background-gradient-stop-colors': CORE_GRADIENTS.character, 'background-blacken': 0 }},
      { selector: 'node.core[kind="event"]',      style: { 'underlay-color': KIND_COLORS.event,      'background-fill': 'radial-gradient', 'background-gradient-stop-colors': CORE_GRADIENTS.event, 'background-blacken': 0 }},
      { selector: 'node.core[kind="location"]',   style: { 'underlay-color': KIND_COLORS.location,   'background-fill': 'radial-gradient', 'background-gradient-stop-colors': CORE_GRADIENTS.location, 'background-blacken': 0 }},
      { selector: 'node.core[kind="concept"]',    style: { 'underlay-color': KIND_COLORS.concept,    'background-fill': 'radial-gradient', 'background-gradient-stop-colors': CORE_GRADIENTS.concept, 'background-blacken': 0 }},
      { selector: 'node.core[kind="item"]',       style: { 'underlay-color': KIND_COLORS.item,       'background-fill': 'radial-gradient', 'background-gradient-stop-colors': CORE_GRADIENTS.item, 'background-blacken': 0 }},
      { selector: 'node.core[kind="chapter"]',    style: { 'underlay-color': KIND_COLORS.chapter,    'background-fill': 'radial-gradient', 'background-gradient-stop-colors': CORE_GRADIENTS.chapter, 'background-blacken': 0 }},
      { selector: 'node.core', style: {
        'underlay-opacity': 0.2, 'underlay-padding': 'mapData(size, 14, 36, 6, 12)', 'underlay-shape': 'ellipse',
      }},
      // ── 抓取反馈：光晕亮起 + 描边加粗（"被捏住"的触感） ────
      { selector: 'node.grabbed', style: {
        'underlay-color': ACCENT, 'underlay-opacity': 0.32,
        'underlay-padding': 12, 'underlay-shape': 'ellipse',
        'border-width': 2.5, 'z-index': 999,
      }},
      // ── 拖动张力：被拖拽节点的关联边"绷紧" ─────────────────
      { selector: 'edge.tension', style: {
        'line-fill': 'solid',
        'line-color': ACCENT, 'opacity': 0.85,
        'width': 'mapData(edge_count, 1, 10, 1.8, 4.2)',
        'z-index': 20,
      }},
      // ── Hover / 选中 / 详情高亮（统一 accent） ─────────────
      { selector: 'node.hover', style: { 'border-width': 2.5 }},
      { selector: 'node:selected', style: {
        'border-width': 2.5, 'border-color': ACCENT, 'border-opacity': 1,
      }},
      { selector: 'node.detail-open', style: {
        'border-width': 2.5, 'border-color': ACCENT, 'border-opacity': 1,
        'overlay-color': ACCENT, 'overlay-opacity': 0.14, 'overlay-padding': 7,
      }},
      // ── Edge base：两端节点色的降饱和渐变 + 对数宽度 ──────
      { selector: 'edge', style: {
        'width': edgeWidth,
        'line-fill': 'linear-gradient',
        'line-gradient-stop-colors': edgeGradient,
        'curve-style': 'bezier',
        'control-point-step-size': 26,
        'opacity': 0.75,
        'label': 'data(label)', 'font-size': '10px', 'color': '#a8a098',
        'text-opacity': 0,
        'text-background-color': '#141210', 'text-background-opacity': 0.9,
        'text-background-padding': '3px', 'text-background-shape': 'roundrectangle',
        'transition-property': 'opacity, text-opacity, line-color, width',
        'transition-duration': 180,
      }},
      { selector: 'edge.hover', style: {
        'text-opacity': 1,
        'opacity': 1,
        'line-gradient-stop-colors': edgeGradientVivid,
        'width': 'mapData(edge_count, 1, 10, 1.8, 4.4)',
      }},
      // ── 类型轮盘过滤（类化，避免与聚焦样式打架） ────────────
      { selector: '.tw-hidden', style: { 'display': 'none' }},
      { selector: 'node.tw-dim', style: { 'opacity': 0.2 }},
      { selector: 'edge.tw-dim', style: { 'opacity': 0.12 }},
      { selector: 'edge.tw-lit', style: { 'opacity': 0.6 }},
      // ── 邻居聚焦 ──────────────────────────────────────────
      { selector: '.dimmed', style: { 'opacity': 0.12, 'text-opacity': 0, 'events': 'no' }},
      { selector: 'edge.focus-edge', style: {
        'line-fill': 'solid',
        'line-color': ACCENT, 'opacity': 0.9, 'text-opacity': 1, 'z-index': 10,
        'width': 'mapData(edge_count, 1, 10, 1.5, 3.5)',
      }},
    ],
    layout: LAYOUT_DEFAULTS['force'],
    wheelSensitivity: 0.12,
    minZoom: 0.25,
    maxZoom: 6,
    // 视口内性能：大图拖动/缩放时降低渲染负担
    textureOnViewport: true,
    hideEdgesOnViewport: false,
    motionBlur: false,
  });

  // ── Event handlers ───────────────────────────────────────────────

  // Node click → detail panel
  cy.on('tap', 'node', function (evt) {
    evt.stopPropagation();
    const node = evt.target;
    openNodeDetail(node);
  });

  // Background click → close detail
  cy.on('tap', function (evt) {
    if (evt.target === cy) closeGraphDetail();
  });

  // Edge hover → show label
  cy.on('mouseover', 'edge', function (evt) {
    evt.target.addClass('hover');
  });
  cy.on('mouseout', 'edge', function (evt) {
    evt.target.removeClass('hover');
  });

  // Edge tap → show relationship detail
  cy.on('tap', 'edge', function (evt) {
    evt.stopPropagation();
    const edge = evt.target;
    openEdgeDetail(edge);
  });

  // ── Label visibility：两级阈值，核心节点标签更"顽固" ────────
  let _labelsVisible = true;
  let _coreLabelsVisible = true;
  const LABEL_ZOOM_ALL = 0.55;
  const LABEL_ZOOM_CORE = 0.3;
  cy.on('zoom', function () {
    const z = cy.zoom();
    const showAll = z >= LABEL_ZOOM_ALL;
    const showCore = z >= LABEL_ZOOM_CORE;
    if (showAll !== _labelsVisible || showCore !== _coreLabelsVisible) {
      _labelsVisible = showAll;
      _coreLabelsVisible = showCore;
      cy.style()
        .selector('node')
        .style('text-opacity', showAll ? 1 : 0)
        .selector('node.core')
        .style('text-opacity', showCore ? 1 : 0)
        .update();
    }
  });

  // Node hover → 描边加粗 + 关联边点亮 + cursor（Obsidian 式邻里呼应）
  cy.on('mouseover', 'node', function (evt) {
    const n = evt.target;
    n.addClass('hover');
    if (!selectedNodeId) {
      n.connectedEdges().forEach(function (e) {
        if (!e.hasClass('tw-hidden') && !e.hasClass('dimmed')) e.addClass('hover');
      });
    }
    container.style.cursor = 'pointer';
  });
  cy.on('mouseout', 'node', function (evt) {
    evt.target.removeClass('hover');
    evt.target.connectedEdges().forEach(function (e) { e.removeClass('hover'); });
    container.style.cursor = 'default';
  });

  // ── Drag physics：抓取反馈 + 排斥推挤 + 松手弹性松弛 ────────
  let _dragState = null;

  cy.on('grab', 'node', function (evt) {
    const n = evt.target;
    _dragState = {
      node: n,
      radius: 200,
      strength: 4000,
      originPos: {},  // nodeId → {x, y} 原位快照，仅存被推开的节点
    };
    // 触感反馈：被捏住的光晕 + 关联边绷紧
    n.addClass('grabbed');
    n.connectedEdges().forEach(function (e) {
      if (!e.hasClass('tw-hidden') && !e.hasClass('dimmed')) e.addClass('tension');
    });
    container.style.cursor = 'grabbing';
  });

  cy.on('drag', 'node', function (evt) {
    if (!_dragState) return;
    const dpos = evt.target.position();

    cy.nodes().forEach(function (other) {
      if (other.id() === evt.target.id()) return;
      if (other.style('display') === 'none') return;
      if (other.hasClass('detail-open')) return;
      if (other.hasClass('dimmed')) return;

      const opos = other.position();
      const dx = opos.x - dpos.x;
      const dy = opos.y - dpos.y;
      const dist = Math.sqrt(dx * dx + dy * dy);

      if (dist < _dragState.radius && dist > 1) {
        // 首次推开前保存原位（后续 drag 帧不重复保存）
        if (!_dragState.originPos[other.id()]) {
          _dragState.originPos[other.id()] = { x: opos.x, y: opos.y };
        }
        const force = _dragState.strength / (dist * dist);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        other.position({
          x: opos.x + fx,
          y: opos.y + fy,
        });
      }
    });
  });

  cy.on('free', 'node', function (evt) {
    if (!_dragState) return;
    var ds = _dragState;
    _dragState = null;

    // 解除触感反馈
    evt.target.removeClass('grabbed');
    cy.elements().removeClass('tension');
    container.style.cursor = 'default';

    // B 方案：被推开的邻里弹簧归位，不跑全局松弛。
    // 保留捏住推开的触感反馈，但松手后邻里回到原位、图不重排。
    var entries = Object.entries(ds.originPos);
    for (var i = 0; i < entries.length; i++) {
      var id = entries[i][0];
      var pos = entries[i][1];
      var node = cy.getElementById(id);
      if (node.length > 0) {
        node.animate({
          position: pos,
          duration: 500,
          easing: 'ease-out',
        });
      }
    }
  });

  // Resize → re-fit
  window.addEventListener('resize', onGraphResize);

  // Populate book selector
  await populateBookSelector();

  // Mark page as visited so switchTab doesn't re-init
  window._graphPageVisited = true;
}

// ══════════════════════════════════════════════════════════════════════
// Main entry: load graph for a book
// ══════════════════════════════════════════════════════════════════════

export async function loadGraphForBook(bookId) {
  if (!cy) return;  // Cytoscape not ready

  // ── Resolve bookId (null → auto-select first available) ──────────
  if (!bookId && booksWithGraph.length > 0) {
    bookId = booksWithGraph[0].book_id;
    const sel = document.getElementById('graphBookSelect');
    if (sel) sel.value = bookId;
  }

  // ── Guard: skip if already loading the same book ──────────────────
  if (graphLoading && bookId === currentGraphBookId) return;
  if (!bookId) {
    // Truly no graph data available anywhere
    const emptyState = document.getElementById('graphEmptyState');
    hideAllStates();
    if (emptyState) {
      emptyState.style.display = 'flex';
      emptyState.querySelector('p').textContent = '还没有已图谱化的书籍';
      const hint = emptyState.querySelector('.graph-empty-hint');
      if (hint) hint.innerHTML = '在书架中对书籍运行图谱分析（<i data-lucide="network"></i> 按钮），完成后即可在此查看。';
      const btn = document.getElementById('graphStartExtractBtn');
      if (btn) btn.style.display = 'none';
    }
    return;
  }

  // ── Skip if already showing this book ─────────────────────────────
  if (bookId === currentGraphBookId) return;

  graphLoading = true;
  const emptyState = document.getElementById('graphEmptyState');
  const errorState = document.getElementById('graphErrorState');

  try {
    // ── Check prefetch cache ──────────────────────────────────────────
    const cached = S.graphPrefetchCache[bookId];
    if (cached && cached.nodes && cached.nodes.length > 0) {
      hideAllStates();
      renderGraph(cached.nodes, cached.edges);
      currentGraphBookId = bookId;
      document.getElementById('graphBookSelect').value = bookId;
      return;
    }

    // ── Check book has graph at all ───────────────────────────────────
    const stats = await fetchGraphStats(bookId);
    if (!stats || stats.nodes === 0) {
      hideAllStates();
      if (emptyState) {
        emptyState.style.display = 'flex';
        emptyState.querySelector('p').textContent = '本书尚未提取知识图谱';
        const hint = emptyState.querySelector('.graph-empty-hint');
        if (hint) hint.textContent = '点击下方按钮开始提取。提取过程会分析全书内容，构建人物、地点、事件、关系的知识图谱。';
        const btn = document.getElementById('graphStartExtractBtn');
        if (btn) {
          btn.style.display = '';
          btn.onclick = function () { startExtractionForCurrentBook(bookId); };
        }
      }
      return;
    }

    // ── Fetch full graph ──────────────────────────────────────────────
    showLoading('加载图谱...');
    const graphResp = await fetch(
      S.API + '/api/graph/' + bookId + '/full?limit=500'
    );
    const graphData = await graphResp.json();
    if (graphData.error) throw new Error(graphData.error);

    if (!graphData.nodes || !graphData.nodes.length) {
      hideAllStates();
      if (emptyState) {
        emptyState.style.display = 'flex';
        emptyState.querySelector('p').textContent = '图谱中没有实体节点';
        const btn = document.getElementById('graphStartExtractBtn');
        if (btn) btn.style.display = 'none';
      }
      return;
    }

    // Cache for later
    S.graphPrefetchCache[bookId] = graphData;

    hideAllStates();
    renderGraph(graphData.nodes, graphData.edges);
    currentGraphBookId = bookId;
    document.getElementById('graphBookSelect').value = bookId;
  } catch (e) {
    hideAllStates();
    if (errorState) errorState.style.display = 'flex';
    console.error('loadGraphForBook:', e);
  } finally {
    graphLoading = false;
  }
}

// ── Helpers ─────────────────────────────────────────────────────────

async function fetchGraphStats(bookId) {
  try {
    const resp = await fetch(S.API + '/api/graph/stats/' + bookId);
    const data = await resp.json();
    return data.stats || null;
  } catch (e) { return null; }
}

function showLoading(msg) {
  const overlay = document.getElementById('graphLoadingOverlay');
  const spinner = overlay ? overlay.querySelector('span') : null;
  if (overlay) overlay.style.display = 'flex';
  if (spinner) spinner.textContent = msg || '加载图谱...';
  document.getElementById('graphEmptyState').style.display = 'none';
  document.getElementById('graphErrorState').style.display = 'none';
}

function hideAllStates() {
  const overlay = document.getElementById('graphLoadingOverlay');
  const emptyState = document.getElementById('graphEmptyState');
  const errorState = document.getElementById('graphErrorState');
  if (overlay) overlay.style.display = 'none';
  if (emptyState) emptyState.style.display = 'none';
  if (errorState) errorState.style.display = 'none';
}

// ══════════════════════════════════════════════════════════════════════
// Render: API data → Cytoscape elements → layout → type wheel
// ══════════════════════════════════════════════════════════════════════

function renderGraph(nodes, edges) {
  // ── Weighted degree for node sizing ──────────────────────────
  const degreeMap = {};
  for (const e of edges || []) {
    const w = e.edge_count || 1;
    degreeMap[e.source_id] = (degreeMap[e.source_id] || 0) + w;
    degreeMap[e.target_id] = (degreeMap[e.target_id] || 0) + w;
  }
  const allDegrees = Object.values(degreeMap).filter(function (d) { return d > 0; });
  const maxDeg = allDegrees.length > 0 ? Math.max.apply(null, allDegrees) : 1;

  // pow 0.55 映射：比 sqrt 更陡，小节点更小、大节点不臃肿（range 14–36）
  function nodeSize(deg) {
    if (!deg || maxDeg <= 1) return 14;
    var t = Math.pow(deg / maxDeg, 0.55);
    return 14 + t * 22;
  }

  // 核心节点：sqrt 归一化 ≥0.5 且至少 3 条关联 —— 加光晕、标签更"顽固"
  function isCoreNode(deg) {
    if (!deg || maxDeg <= 1 || deg < 3) return false;
    var t = Math.sqrt(deg / maxDeg);
    return t >= 0.5;
  }

  // Count kinds for type wheel
  const kindCounts = {};
  const elements = [];

  for (const n of nodes) {
    // 画布标签：≤10 字，不带章节后缀（章节信息留在详情面板）
    const label = (n.canonical_name || '').length > 10
      ? n.canonical_name.slice(0, 9) + '…'
      : (n.canonical_name || '');
    const deg = degreeMap[n.id] || 0;
    elements.push({
      group: 'nodes',
      classes: isCoreNode(deg) ? 'core' : '',
      data: {
        id: n.id,
        label: label,
        fullName: n.canonical_name,
        kind: n.kind,
        description: n.description || '',
        chapter: n.chapter,
        aliases: n.aliases || [],
        size: nodeSize(deg),
        degree: deg,
      },
    });
    kindCounts[n.kind] = (kindCounts[n.kind] || 0) + 1;
  }

  // Edges — merged by unordered pair; only include if both endpoints exist
  const nodeIdSet = new Set(nodes.map(n => n.id));
  for (const e of edges || []) {
    if (!nodeIdSet.has(e.source_id) || !nodeIdSet.has(e.target_id)) continue;
    // Format chapters: [ch3,5,7] or single [ch3]
    const chapters = e.chapters || [];
    let chSuffix = '';
    if (chapters.length === 1) {
      chSuffix = ' [ch' + (chapters[0] + 1) + ']';
    } else if (chapters.length > 1) {
      chSuffix = ' [ch' + chapters.map(c => c + 1).join(',') + ']';
    }
    elements.push({
      group: 'edges',
      data: {
        id: 'e-' + (e.id || Math.random().toString(36).slice(2, 10)),
        source: e.source_id,
        target: e.target_id,
        label: (e.kind || '关联') + chSuffix,
        kind: e.kind || '',
        kinds: e.kinds || [],
        chapter: e.chapter,
        chapters: chapters,
        edge_count: e.edge_count || 1,
      },
    });
  }

  // Replace all elements in Cytoscape
  cy.elements().remove();
  cy.add(elements);

  // ── 入场编排：节点错峰淡入，边随后浮现 ──────────────────────
  const allNodes = cy.nodes();
  const allEdges = cy.edges();
  cy.batch(function () {
    allNodes.forEach(function (n, i) {
      n.style({ 'opacity': 0, 'transition-delay': Math.min(i * 6, 360) + 'ms' });
    });
    allEdges.forEach(function (e) {
      e.style({ 'opacity': 0, 'transition-delay': '420ms' });
    });
  });
  requestAnimationFrame(function () {
    requestAnimationFrame(function () {
      allNodes.forEach(function (n) { n.style('opacity', 1); });
      allEdges.forEach(function (e) { e.style('opacity', 1); });
    });
  });
  // 入场结束后清理内联样式，避免遮挡聚焦 / 过滤的类样式
  setTimeout(function () {
    if (!cy) return;
    allNodes.forEach(function (n) { n.style({ 'opacity': null, 'transition-delay': null }); });
    allEdges.forEach(function (e) { e.style({ 'opacity': null, 'transition-delay': null }); });
  }, 1200);

  // Run layout（cose 结算动画）→ 布局稳定后平滑 fit
  const layout = cy.layout(LAYOUT_DEFAULTS[currentLayout]);
  layout.promiseOn('layoutstop').then(function () {
    cy.animate({
      fit: { eles: cy.elements(), padding: 40 },
    }, { duration: 450, easing: 'ease-in-out-cubic' });
  });
  layout.run();

  // Build type wheel
  populateTypeWheel(kindCounts);

  // Reset detail panel
  closeGraphDetail();
}

// ══════════════════════════════════════════════════════════════════════
// Type wheel
// ══════════════════════════════════════════════════════════════════════

function populateTypeWheel(kindCounts) {
  const wheel = document.getElementById('graphTypeWheel');
  if (!wheel) return;

  typeWheelState = {};
  const sorted = Object.entries(kindCounts).sort((a, b) => b[1] - a[1]);

  wheel.innerHTML = sorted.map(([kind, count]) => {
    typeWheelState[kind] = 'selected';
    const color = KIND_COLORS[kind] || '#90A4AE';
    const label = KIND_LABELS[kind] || kind;
    return `
      <div class="graph-type-row state-selected" data-kind="${escapeHtmlAttr(kind)}"
           onclick="window.onTypeWheelClick('${escapeHtmlAttr(kind)}')"
           title="点击切换：已选 → 关联 → 隐藏">
        <span class="kind-dot" style="background:${color}"></span>
        <span class="kind-label">${escapeHtml(label)}</span>
        <span class="kind-count">${count}</span>
      </div>
    `;
  }).join('');
}

export function onTypeWheelClick(kind) {
  if (!typeWheelState[kind]) return;

  // Cycle: selected → connected → hidden → selected
  const cycle = { selected: 'connected', connected: 'hidden', hidden: 'selected' };
  typeWheelState[kind] = cycle[typeWheelState[kind]];

  // Update DOM
  const row = document.querySelector(`.graph-type-row[data-kind="${escapeHtmlAttr(kind)}"]`);
  if (row) {
    row.classList.remove('state-selected', 'state-connected', 'state-hidden');
    row.classList.add('state-' + typeWheelState[kind]);
  }

  applyTypeWheelFilter();
}

function applyTypeWheelFilter() {
  if (!cy) return;

  const selectedKinds = new Set();
  const connectedKinds = new Set();
  for (const [kind, state] of Object.entries(typeWheelState)) {
    if (state === 'selected') selectedKinds.add(kind);
    if (state === 'connected') connectedKinds.add(kind);
  }

  // 类化样式（tw-hidden / tw-dim / tw-lit），避免内联样式遮挡邻居聚焦
  cy.batch(() => {
    cy.nodes().forEach(function (n) {
      const kind = n.data('kind');
      n.removeClass('tw-hidden tw-dim');
      if (selectedKinds.has(kind)) {
        // full visibility
      } else if (connectedKinds.has(kind)) {
        n.addClass('tw-dim');
      } else {
        n.addClass('tw-hidden');
      }
    });

    cy.edges().forEach(function (e) {
      const sk = e.source().data('kind');
      const tk = e.target().data('kind');
      const sourceShow = selectedKinds.has(sk) || connectedKinds.has(sk);
      const targetShow = selectedKinds.has(tk) || connectedKinds.has(tk);
      const anySelected = selectedKinds.has(sk) || selectedKinds.has(tk);

      e.removeClass('tw-hidden tw-dim tw-lit');
      if (sourceShow && targetShow) {
        e.addClass(anySelected ? 'tw-lit' : 'tw-dim');
      } else {
        e.addClass('tw-hidden');
      }
    });
  });
}

// ══════════════════════════════════════════════════════════════════════
// Neighborhood focus — 点击节点/边时，非关联元素沉入背景
// ══════════════════════════════════════════════════════════════════════

function applyNodeFocus(node) {
  const hood = node.closedNeighborhood();
  cy.batch(function () {
    cy.elements().addClass('dimmed').removeClass('focus-edge');
    hood.removeClass('dimmed');
    node.connectedEdges().addClass('focus-edge');
  });
}

function applyEdgeFocus(edge) {
  cy.batch(function () {
    cy.elements().addClass('dimmed').removeClass('focus-edge');
    edge.removeClass('dimmed').addClass('focus-edge');
    edge.source().removeClass('dimmed');
    edge.target().removeClass('dimmed');
  });
}

function clearFocus() {
  if (!cy) return;
  cy.elements().removeClass('dimmed focus-edge');
}

// ══════════════════════════════════════════════════════════════════════
// Edge detail panel
// ══════════════════════════════════════════════════════════════════════

function openEdgeDetail(edge) {
  if (!cy) return;
  selectedNodeId = null;
  cy.nodes().removeClass('detail-open');
  applyEdgeFocus(edge);

  const layout = document.getElementById('graphPageLayout');
  if (layout) layout.classList.add('detail-open');

  const nameEl = document.getElementById('graphDetailName');
  const bodyEl = document.getElementById('graphDetailBody');

  const data = edge.data();
  const sourceNode = edge.source();
  const targetNode = edge.target();
  const srcName = (sourceNode.data('fullName') || sourceNode.data('label') || '?');
  const tgtName = (targetNode.data('fullName') || targetNode.data('label') || '?');
  const srcKind = sourceNode.data('kind') || '';
  const tgtKind = targetNode.data('kind') || '';
  const srcColor = KIND_COLORS[srcKind] || '#90A4AE';
  const tgtColor = KIND_COLORS[tgtKind] || '#90A4AE';
  const srcLabel = KIND_LABELS[srcKind] || srcKind;
  const tgtLabel = KIND_LABELS[tgtKind] || tgtKind;
  const kinds = data.kinds || [];
  const chapters = data.chapters || [];
  const edgeCount = data.edge_count || 1;

  if (nameEl) nameEl.innerHTML = srcName + ' <i data-lucide="arrow-left-right"></i> ' + tgtName;

  let html = '';

  // Endpoint badges
  html += '<div class="detail-edge-endpoints">'
    + '<span class="detail-kind" style="background:' + srcColor + '22;color:' + srcColor + ';border:1px solid ' + srcColor + '44;cursor:pointer" onclick="window.focusGraphNode(\'' + escapeHtmlAttr(sourceNode.id()) + '\')">' + escapeHtml(srcLabel) + ': ' + escapeHtml(srcName) + '</span>'
    + '<span style="color:var(--text-muted);margin:0 4px"><i data-lucide="arrow-left-right"></i></span>'
    + '<span class="detail-kind" style="background:' + tgtColor + '22;color:' + tgtColor + ';border:1px solid ' + tgtColor + '44;cursor:pointer" onclick="window.focusGraphNode(\'' + escapeHtmlAttr(targetNode.id()) + '\')">' + escapeHtml(tgtLabel) + ': ' + escapeHtml(tgtName) + '</span>'
    + '</div>';

  // Stats
  html += '<div class="detail-edge-stats">'
    + '<span><i data-lucide="link"></i> ' + edgeCount + ' 次关联</span>';
  if (chapters.length > 0) {
    html += '<span><i data-lucide="book-open"></i> 第 ' + chapters.map(function (c) { return c + 1; }).join(', ') + ' 章</span>';
  }
  html += '</div>';

  // Relationship types
  if (kinds.length > 0) {
    html += '<div class="detail-section"><h4>关系类型</h4>';
    kinds.forEach(function (k) {
      html += '<span class="alias-tag" style="display:inline-block;margin:2px">' + escapeHtml(k) + '</span>';
    });
    html += '</div>';
  }

  if (bodyEl) bodyEl.innerHTML = html;
}

// ══════════════════════════════════════════════════════════════════════
// Node detail panel
// ══════════════════════════════════════════════════════════════════════

function openNodeDetail(node) {
  if (!cy) return;
  selectedNodeId = node.id();

  // Highlight the selected node
  cy.nodes().removeClass('detail-open');
  node.addClass('detail-open');
  applyNodeFocus(node);

  const layout = document.getElementById('graphPageLayout');
  if (layout) layout.classList.add('detail-open');

  const nameEl = document.getElementById('graphDetailName');
  const bodyEl = document.getElementById('graphDetailBody');

  const data = node.data();
  const kind = data.kind || '';
  const color = KIND_COLORS[kind] || '#90A4AE';
  const label = KIND_LABELS[kind] || kind;

  if (nameEl) nameEl.textContent = data.fullName || data.label || '';

  // Kind badge + chapter source
  let html = `
    <span class="detail-kind" style="background:${color}22;color:${color};border:1px solid ${color}44">${escapeHtml(label)}</span>
  `;
  if (data.chapter != null) {
    html += `<span class="detail-chapter"><i data-lucide="book-open"></i> 首次出场：第 ${data.chapter + 1} 章</span>`;
  }
  if (data.aliases && data.aliases.length) {
    html += '<div class="detail-aliases">别称：'
      + data.aliases.map(function (a) { return '<span class="alias-tag">' + escapeHtml(a) + '</span>'; }).join(' ')
      + '</div>';
  }
  if (data.description) {
    html += `<p class="detail-desc">${escapeHtml(data.description)}</p>`;
  }

  // Connected edges (merged — one edge per other-node)
  const connected = node.connectedEdges();
  if (connected.length) {
    html += '<div class="detail-section"><h4>关联关系 (' + connected.length + ')</h4>';

    const relList = [];
    connected.forEach(function (e) {
      const other = e.source().id() === node.id() ? e.target() : e.source();
      const oid = other.id();
      const edata = e.data();
      const kinds = edata.kinds || [];
      const chapters = edata.chapters || [];
      let relText = kinds.length > 0 ? kinds.join(' · ') : (edata.label || '关联');
      if (chapters.length === 1) {
        relText += ' [ch' + (chapters[0] + 1) + ']';
      } else if (chapters.length > 1) {
        relText += ' [ch' + chapters.map(function (c) { return c + 1; }).join(',') + ']';
      }
      relList.push({
        oid: oid,
        name: other.data('fullName') || other.data('label') || oid,
        kind: other.data('kind') || '',
        relText: relText,
        edgeCount: edata.edge_count || 1,
      });
    });

    // Sort by edge count descending
    relList.sort(function (a, b) { return b.edgeCount - a.edgeCount; });

    relList.forEach(function (rel) {
      const rkColor = KIND_COLORS[rel.kind] || '#90A4AE';
      html += `
        <div class="detail-edge-item" onclick="window.focusGraphNode('${escapeHtmlAttr(rel.oid)}')"
             title="点击定位到该节点（${rel.edgeCount}次关联）">
          <span class="kind-dot" style="background:${rkColor};width:8px;height:8px;border-radius:50%;flex-shrink:0"></span>
          <span class="detail-edge-name">${escapeHtml(rel.name)}</span>
          <span class="detail-edge-rel">${escapeHtml(rel.relText)}</span>
        </div>
      `;
    });
    html += '</div>';
  }

  if (bodyEl) bodyEl.innerHTML = html;
}

export function closeGraphDetail() {
  selectedNodeId = null;
  if (cy) {
    cy.nodes().removeClass('detail-open');
    clearFocus();
  }
  const layout = document.getElementById('graphPageLayout');
  if (layout) layout.classList.remove('detail-open');
}

export function toggleGraphDetail() {
  const layout = document.getElementById('graphPageLayout');
  if (!layout) return;
  layout.classList.toggle('detail-open');
}

export function focusGraphNode(nodeId) {
  if (!cy) return;
  const node = cy.getElementById(nodeId);
  if (node.length === 0) return;

  // Center + zoom to node
  cy.animate({
    center: { eles: node },
    zoom: Math.max(cy.zoom(), 0.8),
    duration: 400,
    easing: 'ease-in-out-cubic',
  });

  // Open detail panel
  openNodeDetail(node);
}

// ══════════════════════════════════════════════════════════════════════
// Layout toggle
// ══════════════════════════════════════════════════════════════════════

export function toggleGraphLayout() {
  if (!cy) return;
  currentLayout = currentLayout === 'force' ? 'breadthfirst' : 'force';
  const btn = document.getElementById('graphLayoutToggle');
  if (btn) {
    btn.innerHTML = '<i data-lucide="layout-grid"></i> 布局: ' + (LAYOUT_LABELS[currentLayout] || currentLayout);
    if (window.refreshIcons) window.refreshIcons();
  }

  const layout = cy.layout(LAYOUT_DEFAULTS[currentLayout]);
  layout.promiseOn('layoutstop').then(function () {
    cy.animate({
      fit: { eles: cy.elements(), padding: 40 },
    }, { duration: 450, easing: 'ease-in-out-cubic' });
  });
  layout.run();
}

// ══════════════════════════════════════════════════════════════════════
// Graph management view
// ══════════════════════════════════════════════════════════════════════

let _currentGraphView = 'graph';  // 'graph' | 'manage'

export function switchGraphView(view) {
  if (view === _currentGraphView) return;
  _currentGraphView = view;

  const tabs = document.querySelectorAll('#graphSecNav .graph-secnav-tab');
  tabs.forEach(function (t) { t.classList.toggle('active', t.dataset.view === view); });

  const layout = document.getElementById('graphPageLayout');
  const toolbar = document.getElementById('graphToolbar');
  const managePanel = document.getElementById('graphManagePanel');

  if (view === 'manage') {
    if (layout) layout.style.display = 'none';
    if (toolbar) toolbar.style.display = 'none';
    if (managePanel) managePanel.style.display = '';
    loadGraphManage();
  } else {
    if (layout) layout.style.display = '';
    if (toolbar) toolbar.style.display = '';
    if (managePanel) managePanel.style.display = 'none';
  }
}

async function loadGraphManage() {
  const body = document.getElementById('graphManageBody');
  if (!body) return;
  body.innerHTML = '<div class="session-empty">加载中...</div>';

  try {
    const resp = await fetch(S.API + '/api/graph/books');
    const data = await resp.json();
    const books = data.books || [];

    if (!books.length) {
      body.innerHTML = '<div class="session-empty">暂无图谱数据。<br>在书架中对书籍运行图谱分析后，即可在此管理。</div>';
      return;
    }

    body.innerHTML = books.map(function (b) {
      var kindsHtml = '';
      if (b.kinds) {
        var kindLabels = { character: '人物', event: '事件', location: '地点', concept: '概念', item: '物品', chapter: '章节' };
        kindsHtml = Object.entries(b.kinds).map(function (kv) {
          return '<span>' + (kindLabels[kv[0]] || kv[0]) + ': ' + kv[1] + '</span>';
        }).join('');
      }

      var pct = b.chunks_total ? Math.round(b.chunks_done / b.chunks_total * 100) : 0;

      return '<div class="graph-manage-book-card">'
        + '<div class="gmb-header">'
        + '<span class="gmb-title">' + escapeHtml(b.title) + '<span class="gmb-author">' + escapeHtml(b.author || '') + '</span></span>'
        + '</div>'
        + '<div class="gmb-stats">'
        + '<span><i data-lucide="network"></i> ' + b.nodes + ' 节点</span>'
        + '<span><i data-lucide="link"></i> ' + (b.edges || 0) + ' 边</span>'
        + '<span><i data-lucide="tag"></i> ' + (b.aliases || 0) + ' 别名</span>'
        + '<span><i data-lucide="shuffle"></i> ' + (b.merge_candidates || 0) + ' 待合并</span>'
        + '</div>'
        + (kindsHtml ? '<div class="gmb-stats">' + kindsHtml + '</div>' : '')
        + '<div class="gmb-progress">'
        + '<span>提取进度: ' + b.chunks_done + ' / ' + b.chunks_total + ' (' + pct + '%)'
        + (b.chunks_failed ? ' · ' + b.chunks_failed + ' 失败' : '') + '</span>'
        + '<div class="gmb-progress-bar"><div class="gmb-progress-fill" style="transform:scaleX(' + (pct / 100) + ')"></div></div>'
        + '</div>'
        + '<div class="gmb-actions">'
        + (b.chunks_failed ? '<button class="gmb-retry-btn" onclick="window.retryFailedChunks(\'' + b.book_id + '\', \'' + escapeHtml(b.title).replace(/'/g, "\\'") + '\')"><i data-lucide="rotate-ccw"></i> 重试失败(' + b.chunks_failed + ')</button>' : '')
        + '<button class="gmb-delete-btn" onclick="window.deleteGraphData(\'' + b.book_id + '\', \'' + escapeHtml(b.title).replace(/'/g, "\\'") + '\')"><i data-lucide="trash-2"></i> 删除图谱</button>'
        + '</div>'
        + '</div>';
    }).join('');
  } catch (e) {
    body.innerHTML = '<div class="session-empty">加载失败: ' + e.message + '</div>';
  }
}

export async function deleteGraphData(bookId, bookTitle) {
  if (!confirm('确定删除《' + bookTitle + '》的知识图谱？\n\n节点、边、别名等所有图谱数据将被永久删除。\n重新导入书籍后可重新提取。')) return;

  try {
    const resp = await fetch(S.API + '/api/graph/' + encodeURIComponent(bookId), { method: 'DELETE' });
    const data = await resp.json();
    if (data.error) {
      alert('删除失败: ' + data.error);
      return;
    }
    // If currently viewing this book's graph, clear it
    if (currentGraphBookId === bookId) {
      if (cy) cy.elements().remove();
      currentGraphBookId = null;
      // Show empty state
      const emptyState = document.getElementById('graphEmptyState');
      hideAllStates();
      if (emptyState) { emptyState.style.display = 'flex'; }
    }
    // Refresh the management list
    loadGraphManage();
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

export async function retryFailedChunks(bookId, bookTitle) {
  if (!confirm('重新提取《' + bookTitle + '》失败的章节？\n\n已完成章节保持不变，仅重跑失败批次。')) return;

  try {
    const resp = await fetch(S.API + '/api/graph/' + encodeURIComponent(bookId) + '/retry-failed', { method: 'POST' });
    const data = await resp.json();
    if (data.error) {
      alert('重试失败: ' + data.error);
      return;
    }
    if (data.status === 'no_failed_chunks') {
      alert('没有失败章节需要重试。');
      loadGraphManage();
      return;
    }
    // Attach the progress capsule for this book
    if (typeof window.startExtractionProgress === 'function') {
      window.startExtractionProgress(bookId, data.book_title || bookTitle);
    }
    // Refresh the management list
    loadGraphManage();
  } catch (e) {
    alert('重试失败: ' + e.message);
  }
}

// ══════════════════════════════════════════════════════════════════════
// Book selector
// ══════════════════════════════════════════════════════════════════════

async function populateBookSelector() {
  const sel = document.getElementById('graphBookSelect');
  if (!sel) return;

  try {
    const resp = await fetch(S.API + '/api/graph/books');
    const data = await resp.json();
    booksWithGraph = data.books || [];

    sel.innerHTML = '<option value="">选择书籍...</option>';
    for (const b of booksWithGraph) {
      const opt = document.createElement('option');
      opt.value = b.book_id;
      opt.textContent = (b.title || b.book_id).slice(0, 30) + ' (' + (b.nodes || '?') + '节点)';
      sel.appendChild(opt);
    }
  } catch (e) {
    sel.innerHTML = '<option value="">加载失败</option>';
  }
}

export function onGraphBookChange() {
  const sel = document.getElementById('graphBookSelect');
  if (!sel || !sel.value) return;
  loadGraphForBook(sel.value);
}

export async function loadGraphForCurrentBook() {
  return loadGraphForBook(S.currentBook);
}

// ══════════════════════════════════════════════════════════════════════
// Extraction trigger — delegates to floating pill (graph-progress-ui.js)
// ══════════════════════════════════════════════════════════════════════

async function startExtractionForCurrentBook(bookId) {
  const emptyState = document.getElementById('graphEmptyState');
  const btn = document.getElementById('graphStartExtractBtn');
  if (btn) btn.style.display = 'none';

  try {
    const resp = await fetch(
      S.API + '/api/graph/init/' + bookId + '?batch_size=5',
      { method: 'POST' }
    );
    const data = await resp.json();

    if (data.error) {
      if (emptyState) emptyState.querySelector('p').textContent = '提取失败: ' + data.error;
      return;
    }

    // Start the floating pill progress UI
    if (typeof window.startExtractionProgress === 'function') {
      window.startExtractionProgress(bookId, data.book_title || '');
    }

    // Update empty state to point to the floating pill
    if (emptyState) {
      emptyState.querySelector('p').textContent = '提取中...进度已移至顶部悬浮窗';
      const hint = emptyState.querySelector('.graph-empty-hint');
      if (hint) hint.textContent = '点击顶部灵动岛查看实时事件流。提取完成后图谱将自动加载。';
    }

    // Poll for completion, then reload graph
    pollForGraphCompletion(bookId);
  } catch (e) {
    if (emptyState) emptyState.querySelector('p').textContent = '提取失败: ' + e.message;
  }
}

async function pollForGraphCompletion(bookId) {
  const maxPolls = 600; // 10 minutes at 1s intervals
  for (let i = 0; i < maxPolls; i++) {
    await new Promise(r => setTimeout(r, 1000));
    // Check if extraction is still running
    if (typeof window.isExtractionRunning === 'function' && !window.isExtractionRunning(bookId)) {
      // Extraction finished — check stats
      try {
        const statsResp = await fetch(S.API + '/api/graph/stats/' + bookId);
        const statsData = await statsResp.json();
        const stats = statsData.stats;
        if (stats && stats.chunks_done >= stats.chunks_total && stats.nodes > 0) {
          loadGraphForBook(bookId);
          return;
        }
      } catch (e) {
        // Retry
      }
    }
  }
  // Timeout — just try loading
  loadGraphForBook(bookId);
}

// ══════════════════════════════════════════════════════════════════════
// Resize handler
// ══════════════════════════════════════════════════════════════════════

let resizeTimer = null;
function onGraphResize() {
  if (!cy) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(function () {
    cy.resize();
    cy.fit(undefined, 40);
  }, 200);
}

// ══════════════════════════════════════════════════════════════════════
// Window exports (for HTML onclick and cross-module calls)
// ══════════════════════════════════════════════════════════════════════
window.initGraphPage = initGraphPage;
window.loadGraphForBook = loadGraphForBook;
window.loadGraphForCurrentBook = loadGraphForCurrentBook;
window.onGraphBookChange = onGraphBookChange;
window.toggleGraphLayout = toggleGraphLayout;
window.onTypeWheelClick = onTypeWheelClick;
window.closeGraphDetail = closeGraphDetail;
window.toggleGraphDetail = toggleGraphDetail;
window.focusGraphNode = focusGraphNode;
window.switchGraphView = switchGraphView;
window.deleteGraphData = deleteGraphData;
window.retryFailedChunks = retryFailedChunks;
