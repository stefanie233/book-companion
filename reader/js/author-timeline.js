/* Author timeline — 一根轴，上下两半（作品 / 生平）。
 *
 * 设计定案见 docs/authorgraph/README.md §五：
 *   - **一根水平轴**（年份），不是两条平行轴
 *   - 上半 = 作品轨（work/publication），下半 = 生平轨（life_event/relation）
 *   - **任一半允许空** —— 张景只有作品、没有生平，下半就是空的。
 *     「空」是对的，不要为了好看补内容。
 *
 * 无年份的条目不进轴（轴的位置就是年份），单独放「时间未知」。
 *
 * 版式是编辑式的：年代做底纹、轴当尺子、条目是卡片。
 * 画布可拖动、可缩放 —— 54 年挤 17 条生平，不放大是读不清的。
 */

import { S } from './state.js';
import { escapeHtml } from './utils.js';

// 每年多少像素。**量出来的**：54 年 × 22 ≈ 1520，正好铺满 1680 屏的舞台，
// 于是初始 fit 的比例 ≈ 0.99、正文 ≈ 11.4px —— 一屏看全，且读得清。
// 给到 34 时画布 2246px，fit 只有 0.68，字缩到 7.8px：**看全了，但看不清**。
const PX_PER_YEAR = 66;          // 22 × 3 —— 轴间距加宽三倍
const MIN_WIDTH = 1400;
const PAD = 200;                 // 两侧留白，够放半张卡片
const LANE_H = 150;              // 一层卡片占的高度（三行大字 + 间距）
const CARD_W = 340;              // 卡片宽（也是分道时算的碰撞宽度）
const CARD_H = 130;              // 三行 20px 文字 + 内边距
const CLUSTER_W = 210;           // 聚合胶囊的宽 —— 比卡片窄，它只是索引
const CLUSTER_H = 80;
// 链式聚合最多横跨多远。没有这道闸，1964…1989 每 3 年一条会连成一坨。
const CLUSTER_SPAN = 900;
const MAX_LANES = 14;
const MIN_ZOOM = 0.26;
const MAX_ZOOM = 4;              // 到 4× 时聚合阈值归零 = 完全散开
const BASE_UP = 60;              // 上半第一层离轴的距离
const BASE_DOWN = 96;            // 下半要让开轴上那排年份
const MIN_HALF = 240;
const TOP_PAD = 72;              // 画布顶部留给年代标签

const SOURCE_LABEL = { corpus: '书里', web: '网页', graph: '书库', model: '推测' };

/* ══ 入口 ═════════════════════════════════════════════════════════════ */

/** 图谱页「作者简介」入口：按当前选中的书取作者，再开同一根轴。
 *
 *  **一本书一位作者是常态** —— 所以「只有一位」是主路径，不是兜底分支；
 *  合著（《道德经》张景/张松辉）才需要先选人。
 */
export async function openGraphAuthor() {
  const sel = document.getElementById('graphBookSelect');
  const bookId = sel && sel.value ? sel.value : '';
  if (!bookId) {
    // 不用 alert：它会**阻塞渲染进程**（无头浏览器里直接卡死整页）。
    // 轻轻提示一下选择框就够了。
    const sel2 = document.getElementById('graphBookSelect');
    if (sel2) {
      sel2.classList.add('atl-nudge');
      sel2.focus();
      setTimeout(function () { sel2.classList.remove('atl-nudge'); }, 1400);
    }
    return;
  }
  showView();
  const body = document.getElementById('atlBody');
  let authors = [];
  try {
    const resp = await fetch(S.API + '/api/authors/' + bookId);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    // 没语料也没编年的作者不列 —— 点进去只剩一个名字，不如不列。
    authors = (data.authors || []).filter(function (a) {
      return ((a.corpus || 0) + (a.records || 0)) > 0;
    });
  } catch (e) {
    body.innerHTML = '<div class="rp-error">加载失败：' + escapeHtml(e.message) + '</div>';
    return;
  }
  if (!authors.length) {
    body.innerHTML = '<div class="rp-empty">这本书还没有采到作者信息。'
      + '<br>生平与作品来自序跋采集 —— 对这本书跑一次作者采集后再来看。</div>';
    return;
  }
  if (authors.length === 1) {
    openDossierOrTimeline(bookId, authors[0].author_id);
    return;
  }
  body.innerHTML = authorPicker(bookId, authors);
}

/** 作者档案是默认入口；档案模块没加载（或旧页面缓存）时退回时间轴。
 *  两个模块不互相 import —— 通过 window 上的入口解耦，避免循环依赖。 */
function openDossierOrTimeline(bookId, authorId) {
  if (typeof window.openAuthorDossier === 'function') {
    window.openAuthorDossier(bookId, authorId);
  } else {
    showView();
    loadTimeline(bookId, authorId);
  }
}

/** 合著时的选人页。走同一张 overlay，不另开一个浮层。 */
function authorPicker(bookId, authors) {
  let html = '<div class="rp-hint">这本书有 ' + authors.length
    + ' 位作者，选一位看他的生平与作品。</div><div class="rp-char-list">';
  authors.forEach(function (a) {
    const meta = [a.nationality,
      (a.birth_year || a.death_year)
        ? ((a.birth_year || '?') + '–' + (a.death_year || '?')) : '',
      a.role === 'co-author' ? '合著' : ''].filter(Boolean).join(' · ');
    html += '<div class="rp-char-card" onclick="window.pickGraphAuthor(\''
      + escapeHtml(bookId) + '\',\'' + escapeHtml(a.author_id) + '\')">'
      + '<div class="rp-char-name">' + escapeHtml(a.canonical_name) + '</div>'
      + (meta ? '<div class="rp-char-desc">' + escapeHtml(meta) + '</div>' : '')
      + '</div>';
  });
  return html + '</div>';
}

export function pickGraphAuthor(bookId, authorId) {
  openDossierOrTimeline(bookId, authorId);
}

/** 档案页的「全屏时间轴」入口 —— 旧视图保留，不删。 */
export function openAuthorTimelineView(bookId, authorId) {
  showView();
  loadTimeline(bookId, authorId);
}

/** 进入作者视图：**换掉图谱页的画布内容**，而不是盖一层浮层。
 *
 *  同一个页面、同一条工具条、同一片画布 —— 所以没有任何「模态」的痕迹：
 *  没有遮罩、没有居中对话框、没有把 body 滚动锁死。
 */
function showView() {
  const view = document.getElementById('authorTimelineView');
  if (!view) return null;
  if (view.hidden) {
    view.hidden = false;
    const layout = document.getElementById('graphPageLayout');
    if (layout) layout.style.display = 'none';
    const page = document.getElementById('page-graph');
    if (page) page.classList.add('atl-mode');
    const btn = document.getElementById('graphAuthorBtn');
    if (btn) btn.classList.add('active');
  }
  const title = document.getElementById('atlTitle');
  if (title) title.textContent = '作者时间轴';
  const sub = document.getElementById('atlSub');
  if (sub) sub.textContent = '';
  document.getElementById('atlBody').innerHTML =
    '<div class="rp-loading">读台账…</div>';
  return view;
}

/** 回到图谱：把画布内容换回去，页面本身一动不动。 */
export function closeAuthorTimeline() {
  const view = document.getElementById('authorTimelineView');
  if (view) view.hidden = true;
  const layout = document.getElementById('graphPageLayout');
  if (layout) layout.style.display = '';
  const page = document.getElementById('page-graph');
  if (page) page.classList.remove('atl-mode');
  const btn = document.getElementById('graphAuthorBtn');
  if (btn) btn.classList.remove('active');
  if (_reflowT) { clearTimeout(_reflowT); _reflowT = null; }
  _stage = null; _canvas = null; _data = null;
}

async function loadTimeline(bookId, authorId) {
  // 沉浸模式带读者当前章 → 施同一条剧透门；全局模式不带 = 显式的全集视角
  const gated = S.currentMode === 'roleplay' && S.currentMaxChapter != null;
  const q = gated ? '?max_chapter=' + S.currentMaxChapter : '';
  const body = document.getElementById('atlBody');
  body.innerHTML = '<div class="rp-loading">读台账…</div>';
  try {
    const resp = await fetch(
      S.API + '/api/authors/' + bookId + '/' + authorId + '/timeline' + q);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    render(data);
  } catch (e) {
    body.innerHTML = '<div class="rp-error">加载失败：' + escapeHtml(e.message) + '</div>';
  }
}

/* ══ 画布：平移 / 缩放 ═════════════════════════════════════════════════
 *
 * 用 transform 而不是改 left/top：文字是矢量的，缩放后仍然清晰，
 * 而且拖动只改一个属性，不会触发整棵子树重排。
 */

let view = { x: 0, y: 0, k: 1 };
let _stage = null;
let _canvas = null;
let _cw = 0, _ch = 0;
let _moved = false;          // 刚拖过 —— 用来吞掉那一次 click
let _data = null;            // 当前这份台账，reflow 要拿它重排
let _axisY = 0;              // 轴在画布里的 y —— 重排时靠它锚住视觉位置
let _clusterYears = 0;       // 当前生效的聚合阈值（年）
let _fitDetail = 0;          // 全貌视角下**装得下**的最细一档
let _reflowT = null;

function applyView() {
  if (!_canvas) return;
  _canvas.style.transform = 'translate3d(' + view.x.toFixed(1) + 'px,'
    + view.y.toFixed(1) + 'px,0) scale(' + view.k.toFixed(4) + ')';
  const z = document.getElementById('atlZoomVal');
  if (z) z.textContent = Math.round(view.k * 100) + '%';
  updateMini();
}

/** 缩略条上的取景框。画布比舞台窄时整条藏起来 —— 没得选的时候不装。 */
function updateMini() {
  const mini = document.getElementById('atlMini');
  const win = document.getElementById('atlMiniWin');
  if (!mini || !win || !_stage || !_canvas) return;
  const CW = _canvas.offsetWidth;
  mini.hidden = CW * view.k <= _stage.clientWidth + 3;
  if (mini.hidden) return;
  const visW = _stage.clientWidth / view.k;
  const left = Math.max(0, Math.min(CW, -view.x / view.k));
  const right = Math.max(left, Math.min(CW, -view.x / view.k + visW));
  win.style.left = ((left / CW) * 100).toFixed(2) + '%';
  win.style.width = (Math.max(2, ((right - left) / CW) * 100)).toFixed(2) + '%';
}

/** 换一档聚合：重建画布内容与高度，**不动视图变换**（调用方负责锚定）。 */
function applyDetail(years) {
  if (!_data || !_canvas) return;
  const L = layout(_data, years);
  _canvas.innerHTML = sceneHtml(_data, L);
  _canvas.style.height = L.canvasH + 'px';
  _axisY = L.axisY;
  _clusterYears = years;
  const sum = document.querySelector('.atl-summary');
  if (sum) sum.outerHTML = summary(_data, L.clusters > 0);
}

/** 「看全貌」那一档能给到的最细节的聚合粒度：从 0 年往上退，
 *  退到整条轴装得进舞台为止。默认视角（1:1）不走这里。 */
function computeFitDetail(pw, ph) {
  if (!_data) return 0;
  const W = Math.max(MIN_WIDTH,
    (_data.span.max - _data.span.min) * PX_PER_YEAR + PAD * 2);
  const kW = Math.min(1, pw / W);
  const steps = [0, 1, 2, 3, 4, 5, 6, 8, 10, 14];
  for (let i = 0; i < steps.length; i++) {
    if (layout(_data, steps[i]).canvasH * kW <= ph - 6) return steps[i];
  }
  return steps[steps.length - 1];
}

/** 默认视角：**1:1 阅读**。
 *
 *  字号翻倍、轴距三倍之后，整条轴不可能再一屏装下 —— 硬要塞进去，
 *  等于把刚放大的字又缩回去（k≈0.38，20px 变 7.6px）。所以默认就是 100%：
 *  从左读起、轴垂直居中，横向拖动看后面。看全貌是双击的事。
 */
export function atlHome() {
  if (!_stage || !_canvas) return;
  const pw = _stage.clientWidth, ph = _stage.clientHeight;
  if (!pw || !ph) return;
  // 全貌档只在缩小时用；100% 就是**一条都不聚合**。
  _fitDetail = computeFitDetail(pw, ph);
  if (_clusterYears !== 0) applyDetail(0); else _clusterYears = 0;
  _cw = _canvas.offsetWidth; _ch = _canvas.offsetHeight;
  if (!_cw || !_ch) return;
  view.k = 1;
  view.x = 0;
  // 纵向：轴居中（上下两半都看得见）。画布比舞台矮就正中摆放，
  // 高就夹住不让它露出画布外的空白。
  view.y = _ch <= ph ? (ph - _ch) / 2
    : Math.min(0, Math.max(ph - _ch, ph / 2 - _axisY));
  applyView();
}

/** 看全貌：整条轴刚好装进舞台并居中（双击 / 点百分比）。 */
export function atlFit() {
  if (!_stage || !_canvas) return;
  const pw = _stage.clientWidth, ph = _stage.clientHeight;
  if (!pw || !ph) return;
  _fitDetail = computeFitDetail(pw, ph);
  if (_fitDetail !== _clusterYears) {
    applyDetail(_fitDetail);
  } else {
    _clusterYears = _fitDetail;
  }
  _cw = _canvas.offsetWidth; _ch = _canvas.offsetHeight;
  if (!_cw || !_ch) return;
  view.k = Math.min(1, Math.min(pw / _cw, ph / _ch));
  view.x = (pw - _cw * view.k) / 2;
  view.y = (ph - _ch * view.k) / 2;
  applyView();
}

/** 缩放之后：阈值跟着变，但**轴在屏幕上的位置不能动** ——
 *  否则一松手整条时间轴上下跳一下。 */
function reflow() {
  if (!_data || !_canvas) return;
  // 聚合只属于「缩到能看见整条轴」那一档。缩得越小越聚合，回到 95% 以上就全散。
  // 中间线性过渡，两端都不跳。
  const kw = Math.min(1, _stage.clientWidth / _canvas.offsetWidth);
  const span = Math.max(0.05, 0.95 - kw);
  const k = view.k;
  const years = k >= 0.95 ? 0
    : Math.max(0, Math.min(_fitDetail,
        Math.round(_fitDetail * (0.95 - k) / span)));
  if (years === _clusterYears) return;
  const axisScreen = view.y + _axisY * view.k;
  applyDetail(years);
  view.y = axisScreen - _axisY * view.k;
  applyView();
}

function scheduleReflow() {
  if (_reflowT) clearTimeout(_reflowT);
  _reflowT = setTimeout(function () { _reflowT = null; reflow(); }, 200);
}

/** 以舞台内某点为锚缩放 —— 锚点下的内容不动，视觉上才不「跳」。 */
function zoomAt(cx, cy, factor) {
  if (!_stage) return;
  const r = _stage.getBoundingClientRect();
  const mx = cx - r.left, my = cy - r.top;
  const k2 = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, view.k * factor));
  if (Math.abs(k2 - view.k) < 1e-4) return;
  view.x = mx - (mx - view.x) * (k2 / view.k);
  view.y = my - (my - view.y) * (k2 / view.k);
  view.k = k2;
  applyView();
}

/** 点聚合胶囊 = 放大到这一簇。放大后阈值变小，组会自己散开。 */
function zoomToCluster(el) {
  if (!_stage) return;
  const x = parseFloat(el.dataset.x);
  if (isNaN(x)) return;
  const r = _stage.getBoundingClientRect();
  const sx = r.left + view.x + x * view.k;
  const sy = r.top + view.y + _axisY * view.k;
  animateZoomTo(sx, sy, Math.min(MAX_ZOOM, Math.max(view.k * 2, view.k + 0.7)));
}

/** 平滑缩放到某个屏幕锚点。动画结束才重排 —— 中途换内容会打架。 */
function animateZoomTo(cx, cy, targetK) {
  if (!_stage) return;
  const r = _stage.getBoundingClientRect();
  const mx = cx - r.left, my = cy - r.top;
  const from = { x: view.x, y: view.y, k: view.k };
  const to = {
    k: targetK,
    x: mx - (mx - from.x) * (targetK / from.k),
    y: my - (my - from.y) * (targetK / from.k),
  };
  const t0 = performance.now(), dur = 360;
  const step = function (t) {
    const p = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    view.x = from.x + (to.x - from.x) * e;
    view.y = from.y + (to.y - from.y) * e;
    view.k = from.k + (to.k - from.k) * e;
    applyView();
    if (p < 1) requestAnimationFrame(step); else reflow();
  };
  requestAnimationFrame(step);
}

export function atlZoom(dir) {
  if (!_stage) return;
  const r = _stage.getBoundingClientRect();
  zoomAt(r.left + r.width / 2, r.top + r.height / 2, dir > 0 ? 1.25 : 0.8);
  scheduleReflow();
}

function bindStage() {
  _stage = document.getElementById('atlStage');
  _canvas = document.getElementById('atlCanvas');
  if (!_stage || !_canvas) return;
  // 换内容时把上一轮的手势状态清掉（节点是新的，监听器绑在新的 stage 上）
  let dragging = false, sx = 0, sy = 0, vx = 0, vy = 0;
  let pressEl = null;         // 按下时命中的元素 —— 点击判定靠它，不靠 click

  _stage.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    // 工具条和缩略条在舞台里面，但它们不是画布：各自收自己的手势
    if (e.target.closest('.atl-tools') || e.target.closest('.atl-mini')) return;
    dragging = true; _moved = false;
    pressEl = e.target;
    sx = e.clientX; sy = e.clientY; vx = view.x; vy = view.y;
    try { _stage.setPointerCapture(e.pointerId); } catch (_) { /* 老浏览器 */ }
    _stage.classList.add('atl-grabbing');
  });
  _stage.addEventListener('pointermove', function (e) {
    if (!dragging) return;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) _moved = true;
    view.x = vx + dx; view.y = vy + dy;
    applyView();
  });
  /** 真正的「点击」：按在哪个元素上、松手时没移动过，才算点。 */
  const tap = function (el) {
    if (!el) return;
    // 点完先把压暗撤掉：放大后鼠标还停在原处，压暗会让整屏发灰
    if (_canvas) _canvas.classList.remove('atl-focusing');
    const c = el.closest('.atl-cluster');
    if (c) { zoomToCluster(c); return; }
    const card = el.closest('.atl-card');
    if (card && card.dataset.url) window.open(card.dataset.url, '_blank');
  };
  const stop = function (e) {
    if (!dragging) return;
    dragging = false;
    _stage.classList.remove('atl-grabbing');
    if (e && e.pointerId != null) {
      try { _stage.releasePointerCapture(e.pointerId); } catch (_) { /* 已释放 */ }
    }
    if (e && e.type === 'pointerup' && !_moved) tap(pressEl);
    pressEl = null;
  };
  _stage.addEventListener('pointerup', stop);
  _stage.addEventListener('pointercancel', stop);
  // 拖过之后紧跟的那一次 click 是「想平移」不是「想点链接」，吞掉。
  _stage.addEventListener('click', function (e) {
    if (!_moved) return;
    _moved = false;
    e.preventDefault(); e.stopPropagation();
  }, true);
  _stage.addEventListener('wheel', function (e) {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.12 : 1 / 1.12);
    scheduleReflow();
  }, { passive: false });
  // 缩略条：点哪跳哪，按住可以拖
  const mini = document.getElementById('atlMiniTrack');
  if (mini) {
    let mdrag = false;
    const jump = function (e) {
      if (!_canvas) return;
      const r = mini.getBoundingClientRect();
      const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      view.x = _stage.clientWidth / 2 - f * _canvas.offsetWidth * view.k;
      applyView();
    };
    mini.addEventListener('pointerdown', function (e) {
      e.stopPropagation();
      mdrag = true;
      try { mini.setPointerCapture(e.pointerId); } catch (_) { /* 老浏览器 */ }
      jump(e);
    });
    mini.addEventListener('pointermove', function (e) { if (mdrag) jump(e); });
    const mstop = function () { mdrag = false; };
    mini.addEventListener('pointerup', mstop);
    mini.addEventListener('pointercancel', mstop);
  }

  // 悬停某一条时把其余卡片压暗 —— 密集区里靠这一下分离前景
  _stage.addEventListener('mouseover', function (e) {
    if (e.target.closest('.atl-node')) _canvas.classList.add('atl-focusing');
  });
  _stage.addEventListener('mouseleave', function () {
    if (_canvas) _canvas.classList.remove('atl-focusing');
  });
  _stage.addEventListener('dblclick', function (e) {
    if (e.target.closest('.atl-card')) return;
    atlFit();
  });
}

/** 窗口尺寸变了：**不动用户选的缩放**，也不跳成「看全貌」
 *  （以前在 resize 里调 atlFit()，窗口一变就掉到 29%，字谁都读不了）。
 *  纵向重新按「轴居中」摆 —— 舞台是随窗口伸缩的，钉住旧屏幕坐标
 *  会让轴在窗口变矮时掉到底边上。 */
function onViewportResize() {
  if (!_stage || !_canvas) return;
  const pw = _stage.clientWidth, ph = _stage.clientHeight;
  if (!pw || !ph) return;
  _fitDetail = computeFitDetail(pw, ph);
  _cw = _canvas.offsetWidth; _ch = _canvas.offsetHeight;
  if (!_cw || !_ch) return;
  view.y = _ch <= ph ? (ph - _ch) / 2
    : Math.min(0, Math.max(ph - _ch, ph / 2 - _axisY));
  if (_cw * view.k > pw) {
    view.x = Math.min(0, Math.max(pw - _cw * view.k, view.x));
  }
  applyView();
  scheduleReflow();
}

window.addEventListener('resize', function () {
  const view = document.getElementById('authorTimelineView');
  if (view && !view.hidden) onViewportResize();
});

// Esc = 回到图谱。只在作者视图真的开着时接管，不抢别的层的 Esc。
document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape') return;
  const view = document.getElementById('authorTimelineView');
  if (view && !view.hidden) closeAuthorTimeline();
});

/* ══ 渲染 ═════════════════════════════════════════════════════════════ */

/** 把年份映射成像素 x。span 退化成单点时放中间。 */
function toX(year, lo, hi, width) {
  const usable = width - PAD * 2;
  if (hi <= lo) return PAD + usable / 2;
  return PAD + ((year - lo) / (hi - lo)) * usable;
}

/** 按年份接近度分组。**链式**：相邻两条够近就连起来 ——
 *  但整组不许横跨太远，否则「每 3 年一条」会一路连成一坨。
 *  阈值 `years` 随缩放变：放大 = 阈值变小 = 组散开。
 */
function groupItems(items, lo, hi, width, years) {
  const sorted = items.slice().sort((a, b) => a.year - b.year);
  const out = [];
  let cur = [], firstX = 0;
  const flush = function () {
    if (!cur.length) return;
    if (cur.length === 1) {
      out.push({ year: cur[0].year, items: cur, count: 1 });
    } else {
      const ys = cur.map(function (c) { return c.year; });
      out.push({ year: (ys[0] + ys[ys.length - 1]) / 2,
                 from: ys[0], to: ys[ys.length - 1],
                 items: cur, count: cur.length });
    }
    cur = [];
  };
  sorted.forEach(function (it) {
    const x = toX(it.year, lo, hi, width);
    if (cur.length) {
      // years <= 0 = **完全不聚合**。不能写成「同年就并」：
      // 那样同一年里的几条永远散不开，放到 4 倍还挤在一起（张松辉 1996–2014 就是这么卡住的）。
      const far = years <= 0
        || it.year - cur[cur.length - 1].year > years;
      const wide = x - firstX > CLUSTER_SPAN;
      if (far || wide) flush();
    }
    if (!cur.length) firstX = x;
    cur.push({ year: it.year, x: x, item: it });
  });
  flush();
  return out;
}

/** 贪心分道。元素宽度不一样（卡片 / 聚合胶囊），所以按**边缘距离**判碰撞。 */
function assignLanes(groups, lo, hi, width) {
  const last = [];
  return groups.slice().sort((a, b) => a.year - b.year).map(function (g) {
    const w = g.count > 1 ? CLUSTER_W : CARD_W;
    const x = toX(g.year, lo, hi, width);
    let lv = 0;
    while (lv < MAX_LANES && last[lv]
           && x - last[lv].x < (last[lv].w + w) / 2 + 12) lv++;
    last[lv] = { x: x, w: w };
    return { g: g, level: lv, x: x };
  });
}

function maxLevelOf(lanes) {
  return lanes.reduce(function (m, p) { return Math.max(m, p.level); }, 0);
}

/** 半高 = 最远那层 + 卡片自身高度。算小一格就会把最外层的卡片裁掉。 */
function halfHeight(lanes, base) {
  return Math.max(MIN_HALF, base + maxLevelOf(lanes) * LANE_H + CARD_H + 30);
}

/** 纯排版：给定聚合阈值，算出画布几何。**不产生 HTML** ——
 *  自适应的那几档要反复量，量的时候不该顺手拼字符串。 */
function layout(d, years) {
  const lo = d.span.min, hi = d.span.max;
  const width = Math.max(MIN_WIDTH, (hi - lo) * PX_PER_YEAR + PAD * 2);
  const wl = assignLanes(groupItems(d.work || [], lo, hi, width, years),
                         lo, hi, width);
  const ll = assignLanes(groupItems(d.life || [], lo, hi, width, years),
                         lo, hi, width);
  const upH = halfHeight(wl, BASE_UP), downH = halfHeight(ll, BASE_DOWN);
  const axisY = TOP_PAD + upH;
  let clusters = 0;
  wl.concat(ll).forEach(function (p) { if (p.g.count > 1) clusters++; });
  return { lo, hi, width, wl, ll, axisY, canvasH: axisY + downH, clusters };
}

function sceneHtml(d, L) {
  let html = decades(L);
  html += ticks(L.lo, L.hi, L.width, L.axisY);
  html += '<div class="atl-axis" style="top:' + L.axisY + 'px"></div>';
  const draw = function (p, dir) {
    return p.g.count > 1 ? clusterNode(p, L.axisY, dir) : node(p, L.axisY, dir);
  };
  L.wl.forEach(function (p) { html += draw(p, 'up'); });
  L.ll.forEach(function (p) { html += draw(p, 'down'); });
  if (!(d.work || []).length) html += halfEmpty('作品', L.axisY / 2);
  if (!(d.life || []).length) {
    html += halfEmpty('生平', L.axisY + (L.canvasH - L.axisY) / 2);
  }
  return html;
}

const POSITION_ZH = {
  narrator: '叙述者', avatar: '借角色说话', editor: '整理者',
  recorder: '记录者', absent: '不在这本书里', unknown: '',
};

function render(d) {
  _data = d;
  document.getElementById('atlTitle').textContent = d.name || '作者时间轴';
  setSub(d);
  const body = document.getElementById('atlBody');
  const work = d.work || [], life = d.life || [];
  if (!work.length && !life.length) { body.innerHTML = emptyState(d); return; }
  // 先按「不聚合」铺出来，紧接着 atlFit() 会退到舞台装得下的那一档。
  // 两步在同一个任务里完成，用户看不到中间态。
  const L = layout(d, 0);
  body.innerHTML = summary(d, false)
    + '<div class="atl-stage" id="atlStage">'
    + '<div class="atl-canvas" id="atlCanvas" style="width:' + L.width
    + 'px;height:' + L.canvasH + 'px">' + sceneHtml(d, L) + '</div>'
    + zoomBar() + '</div>'
    + minimap(d, L)
    + undatedBlock(d);
  _axisY = L.axisY;
  _clusterYears = 0;
  bindStage();
  atlHome();
}

/** 头部副标题：这个人是谁 —— 国籍、生卒、书内身份。 */
function setSub(d) {
  const a = d.author || {};
  const life = (a.birth_year || a.death_year)
    ? (a.birth_year || '?') + '–' + (a.death_year || '?') : '';
  // `absent` 不往这里放：在「这本书的作者」页上写「不在这本书里」，
  // 说的是**叙事位置**，读者只会当成一句错话。
  const pos = (a.position && a.position !== 'absent')
    ? (POSITION_ZH[a.position] || '') : '';
  const bits = [a.nationality, life, pos,
                a.role === 'co-author' ? '合著' : ''].filter(Boolean);
  const sub = document.getElementById('atlSub');
  if (sub) sub.textContent = bits.join(' · ');
}

function summary(d, hasClusters) {
  const nW = (d.work || []).length, nL = (d.life || []).length;
  const bits = [];
  bits.push('<span class="atl-chip"><i class="atl-swatch work"></i>作品 ' + nW + '</span>');
  bits.push('<span class="atl-chip"><i class="atl-swatch life"></i>生平 ' + nL + '</span>');
  if (d.gate == null) {
    bits.push('<span class="atl-chip is-warn">全集视角 · 不设剧透门</span>');
  } else {
    bits.push('<span class="atl-chip">截至 ' + d.gate + ' 年</span>');
  }
  if (d.not_shown) {
    bits.push('<span class="atl-chip is-warn">另有 ' + d.not_shown + ' 条未展示</span>');
  }
  return '<div class="atl-summary">' + bits.join('')
    + '<span class="atl-hint">拖动平移 · 滚轮缩放 · 双击看全貌'
    + (hasClusters ? ' · 点数字展开' : '') + '</span></div>';
}

/** 年代带的步长随跨度变 —— 短跨度的「年代」是 5 年，不是 10 年。 */
function eraStep(span) {
  if (span > 160) return 50;
  if (span > 60) return 20;
  if (span > 20) return 10;
  return 5;
}

/** 年代底纹：一条竖线 + 交替浅带 + 年代标签。
 *
 *  标签**贴着轴走**，不放画布顶部：画布上半是「未来的作品」预留的空间，
 *  默认视角根本看不到那么高，放顶部等于白写。
 */
function decades(L) {
  const step = eraStep(L.hi - L.lo);
  let html = '';
  const start = Math.floor(L.lo / step) * step;
  let i = 0;
  for (let y = start; y <= L.hi + step; y += step, i++) {
    const x = toX(y, L.lo, L.hi, L.width);
    if (i % 2 === 0) {
      const x2 = toX(y + step, L.lo, L.hi, L.width);
      html += '<div class="atl-band" style="left:' + x.toFixed(1)
        + 'px;width:' + Math.max(0, x2 - x).toFixed(1) + 'px"></div>';
    }
    html += '<div class="atl-grid" style="left:' + x.toFixed(1)
      + 'px;height:' + L.canvasH + 'px"></div>';
    html += '<div class="atl-decade" style="left:' + x.toFixed(1)
      + 'px;top:' + (L.axisY - 38) + 'px">' + y + '</div>';
  }
  return html;
}

/** 轴下的尺子：细刻按年，整数年标字。 */
function ticks(lo, hi, width, axisY) {
  const span = Math.max(1, hi - lo);
  const minor = span > 120 ? 5 : span > 60 ? 2 : 1;
  const major = span > 160 ? 50 : span > 60 ? 10 : span > 20 ? 5 : 1;
  let html = '';
  for (let y = Math.ceil(lo / minor) * minor; y <= hi; y += minor) {
    const x = toX(y, lo, hi, width);
    const isMajor = y % major === 0;
    html += '<div class="atl-tick' + (isMajor ? ' is-major' : '')
      + '" style="left:' + x.toFixed(1) + 'px;top:' + axisY + 'px"></div>';
    if (isMajor) {
      html += '<div class="atl-year" style="left:' + x.toFixed(1)
        + 'px;top:' + (axisY + 12) + 'px">' + y + '</div>';
    }
  }
  return html;
}

/** 一个节点：**圆点钉在轴上**（它标的是年份），卡片分层，中间用竖线连。 */
function node(p, axisY, dir) {
  const it = p.g.items[0].item;
  const up = dir === 'up';
  const off = (up ? BASE_UP : BASE_DOWN) + p.level * LANE_H;
  const src = SOURCE_LABEL[it.source] || it.source || '';
  const title = it.statement + (src ? '（' + src + '）' : '');
  // 链接走 data-url 而不是 inline onclick：stage 上有 setPointerCapture，
  // 它会把随后的 click **重定向到 stage**，挂在卡片上的 onclick 永远不会触发。
  const href = it.url ? ' data-url="' + escapeHtml(it.url) + '"' : '';
  return '<div class="atl-node atl-' + dir + (it.url ? ' atl-has-link' : '')
    + '" style="left:' + p.x.toFixed(1) + 'px;top:' + axisY + 'px"'
    + ' title="' + escapeHtml(title) + '">'
    + '<span class="atl-dot"></span>'
    + '<span class="atl-stem" style="height:' + Math.max(2, off) + 'px"></span>'
    + '<div class="atl-card" style="' + (up ? 'bottom' : 'top') + ':'
    + off + 'px"' + href + '>'
    + '<span class="atl-card-text">' + escapeHtml(it.statement) + '</span>'
    + '</div></div>';
}

/** 聚合：一簇挤得太近的条目合成一个带数字的胶囊。
 *  点它放大 —— 阈值随缩放变小，组自然就散开了。 */
function clusterNode(p, axisY, dir) {
  const g = p.g;
  const up = dir === 'up';
  const off = (up ? BASE_UP : BASE_DOWN) + p.level * LANE_H;
  const range = g.from === g.to ? String(g.from) : g.from + '–' + g.to;
  const tip = g.items.map(function (c) { return c.item.statement; })
    .slice(0, 5).join('\n');
  return '<div class="atl-node atl-cluster atl-' + dir + '"'
    + ' data-x="' + p.x.toFixed(1) + '"'
    + ' style="left:' + p.x.toFixed(1) + 'px;top:' + axisY + 'px"'
    + ' title="' + escapeHtml(tip) + '">'
    + '<span class="atl-cbadge">' + g.count + '</span>'
    + '<span class="atl-stem" style="height:' + Math.max(2, off) + 'px"></span>'
    + '<div class="atl-ccard" style="' + (up ? 'bottom' : 'top') + ':'
    + off + 'px">'
    + '<span class="atl-crange">' + range + '</span>'
    + '<span class="atl-croom">' + (up ? '作品' : '生平') + '</span>'
    + '</div></div>';
}

/** 空的那一半：如实说「没有」，不是拿装饰填满。 */
function halfEmpty(label, y) {
  return '<div class="atl-half-empty" style="top:' + Math.round(y) + 'px">'
    + '<span class="atl-half-rule"></span>' + label
    + ' · 这一半暂时没有带年份的记录'
    + '<span class="atl-half-rule"></span></div>';
}

/** 全貌缩略条：一屏装不下时，告诉你在整条轴的哪一段。 */
function minimap(d, L) {
  const mark = function (it, cls) {
    const f = (toX(it.year, L.lo, L.hi, L.width) / L.width) * 100;
    return '<i class="atl-mmark ' + cls + '" style="left:'
      + f.toFixed(3) + '%"></i>';
  };
  let marks = '';
  (d.work || []).forEach(function (it) { marks += mark(it, 'is-work'); });
  (d.life || []).forEach(function (it) { marks += mark(it, 'is-life'); });
  return '<div class="atl-mini" id="atlMini">'
    + '<div class="atl-mini-track" id="atlMiniTrack">'
    + '<span class="atl-mini-axis"></span>' + marks
    + '<div class="atl-mini-win" id="atlMiniWin"></div>'
    + '</div></div>';
}

function zoomBar() {
  return '<div class="atl-tools">'
    + '<button type="button" onclick="window.atlZoom(-1)" title="缩小">−</button>'
    + '<button type="button" id="atlZoomVal" class="atl-zoom-val"'
    + ' onclick="window.atlFit()" title="回到全貌">100%</button>'
    + '<button type="button" onclick="window.atlZoom(1)" title="放大">+</button>'
    + '</div>';
}

function undatedBlock(d) {
  const u = d.undated || {};
  const n = (u.work || 0) + (u.life || 0);
  if (!n) return '';
  const items = d.undated_items || [];
  let tail = '';
  if (items.length) {
    tail = '<div class="atl-undated-list" id="atlUndatedList" hidden>'
      + items.map(function (x) {
        const room = x.room === 'work' ? '作品' : '生平';
        return '<div class="atl-undated-item">'
          + '<span class="atl-undated-room">' + room + '</span>'
          + escapeHtml(x.statement) + '</div>';
      }).join('') + '</div>';
  }
  return '<div class="atl-undated">'
    + '<button type="button" class="atl-undated-toggle"'
    + ' onclick="window.atlToggleUndated()">'
    + '时间未知 · ' + n + ' 条'
    + (items.length ? '<span class="atl-caret">▾</span>'
      : '<span class="atl-undated-note">· 没有年份的条目上不了轴</span>')
    + '</button>' + tail + '</div>';
}

export function atlToggleUndated() {
  const el = document.getElementById('atlUndatedList');
  if (el) el.hidden = !el.hidden;
}

function emptyState(d) {
  const why = d.gate_reason === 'all_ahead'
    ? '这位作者的材料全在你还没读到的位置（比如卷末的代跋），'
      + '所以此刻一条都不展示 —— 这是剧透门在起作用，不是没数据。'
    : d.gate_reason === 'no_dated'
      ? '序跋里没有带年份的语料，算不出时间轴。'
      : '这位作者还没有任何带年份的条目。';
  return '<div class="rp-empty">' + escapeHtml(why) + '</div>';
}

window.openGraphAuthor = openGraphAuthor;
window.pickGraphAuthor = pickGraphAuthor;
window.openAuthorTimelineView = openAuthorTimelineView;
window.atlZoom = atlZoom;
window.atlFit = atlFit;
window.atlToggleUndated = atlToggleUndated;
window.closeAuthorTimeline = closeAuthorTimeline;
