/* Liubai — 沉浸阅读（focus mode）。
 *
 * 进入 / 退出都靠「空点阅读页」：单击正文（不是拖选、不是点链接/按钮、
 * 没有浮层打开）切换模式。进入刻意延后一小拍再执行，等双击的第二下：
 * 随手双击正文的用户不会被拽进沉浸模式——第二下 pointerdown 会取消
 * 待进入，第二下 click 的 detail=2 也会被拒。
 *
 * 隐藏只靠 <html class="reading-focus">，不写 inline style：退出时聊天
 * 面板的 collapsed、笔记浮窗的显隐都原样回来，不需要逐项还原。
 *
 * 退出规则：除侧栏导航外，阅读页上任意普通点击。沉浸态下侧栏本来就
 * 藏起来了，所以实际是「点哪儿都退」；链接、批注 marker、滚动拖拽、
 * 浮层不在「普通点击」之列。
 */

import { S } from './state.js';
import { renderBookProgress } from './reader.js';
import * as EpubReader from './epub-reader.js';

// 进入延迟：必须大于常见双击间隔，否则双击的第一下就已经进模式了。
// 300ms 兼顾手感（不会觉得点了没反应）与防误触。
const ENTER_DELAY_MS = 300;
// 用来关闭浮层的那一次点击，不能顺手进出模式。搜索面板的关闭发生在
// pointerdown（早于 click），这里靠时间戳把同一手势认回来。
const OVERLAY_GRACE_MS = 500;
const CLICK_MOVE_TOLERANCE = 5;      // px：超过就是拖拽，不是「点一下」

// 这些元素上的点击有自己的语义（打开链接 / 批注 / 按钮），不参与进出。
const INTERACTIVE = 'a, button, input, textarea, select, label,'
  + ' [role="button"], [contenteditable="true"], .bc-ann-cmt';

let _gesture = null;      // {x, y, doc, overlay, sel}
let _enterTimer = 0;
let _enterSeq = 0;
let _bound = false;

export function isFocusMode() {
  return document.documentElement.classList.contains('reading-focus');
}

// ── 基础判定 ──────────────────────────────────────────────────────

function _readerActive() {
  const page = document.getElementById('page-reader');
  return !!(page && page.classList.contains('active'));
}

function _hasSelection(doc) {
  try {
    const sel = doc.getSelection ? doc.getSelection() : null;
    return !!(sel && !sel.isCollapsed && String(sel).trim());
  } catch (_) {
    return false;
  }
}

function _visible(el) {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0
    && getComputedStyle(el).visibility !== 'hidden';
}

/** 有没有挡住阅读页的浮层。搜索面板「展开即覆盖」；笔记浮窗、批注
 *  操作条同理。浮层开着时点击先归浮层，这一下不进出模式。 */
function _overlayOpen() {
  if (S.searchPanelOpen) return true;
  if (_visible(document.getElementById('miniNotesPanel'))) return true;
  if (_visible(document.getElementById('bcSelBar'))) return true;
  const note = document.querySelector('.bc-note-panel, .bc-note-bubble');
  return _visible(note);
}

function _inReaderArea(target) {
  const page = document.getElementById('page-reader');
  return !!(page && target && page.contains(target));
}

/** 阅读区（正文与它的留白），不含阅读器头部/搜索栏。 */
function _isReadingSurface(target) {
  if (!target || !target.closest) return false;
  if (target.closest('.reader-header')) return false;
  return !!target.closest('#bookPage, #bookPageNative, .left');
}

function _overlayGraceActive() {
  const t = window._bcOverlayClosedAt || 0;
  return performance.now() - t < OVERLAY_GRACE_MS;
}

function _cancelEnter() {
  if (!_enterTimer) return;
  clearTimeout(_enterTimer);
  _enterTimer = 0;
  _enterSeq++;
}

// ── 手势判定 ──────────────────────────────────────────────────────

function _beginGesture(e, doc) {
  // 任何一次新的按下都作废待进入的计时：第二下双击必须能拦住第一下。
  _cancelEnter();
  if (e.button !== 0) { _gesture = null; return; }
  _gesture = {
    x: e.clientX,
    y: e.clientY,
    doc: doc || document,
    overlay: _overlayOpen(),
    sel: _hasSelection(doc || document),
  };
}

function _qualifies(e, doc) {
  const g = _gesture;
  if (!g || g.doc !== doc) return false;
  if (g.overlay || g.sel) return false;
  if (e.button !== 0) return false;
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return false;
  // 拖拽（选字/滚屏）不是「点一下」
  if (Math.hypot(e.clientX - g.x, e.clientY - g.y) > CLICK_MOVE_TOLERANCE) {
    return false;
  }
  // 松手时仍有选区：这次点击是在选字，不是空点
  if (_hasSelection(doc)) return false;
  const t = e.target;
  if (t && t.closest && t.closest(INTERACTIVE)) return false;
  return true;
}

function _dismissMiniPanel(target, doc) {
  const mini = document.getElementById('miniNotesPanel');
  if (!_visible(mini)) return;
  if (doc === document && target && mini.contains(target)) return;
  if (typeof window.closeNotesPanel === 'function') window.closeNotesPanel();
}

// ── 进入 / 退出 ───────────────────────────────────────────────────

/** chunk 模式没有 CFI：按滚动比例记个近似锚，重排后按比例还回去。
 *  宽度变化会改换行，比例不是像素级精确，但足够落在同一屏附近。 */
function _captureChunkScroll() {
  if (S.renderMode === 'native') return null;
  const el = document.getElementById('bookPage');
  if (!el) return null;
  const max = el.scrollHeight - el.clientHeight;
  if (max <= 0) return { ratio: 0 };
  return { ratio: el.scrollTop / max };
}

function _restoreChunkScroll(anchor) {
  if (!anchor) return;
  requestAnimationFrame(function () {
    const el = document.getElementById('bookPage');
    if (!el) return;
    const max = el.scrollHeight - el.clientHeight;
    el.scrollTop = max > 0 ? Math.round(anchor.ratio * max) : 0;
  });
}

/** 改完布局后的收尾。native 要重排 + 按 CFI 回到原处；chunk 按比例。
 *  返回 Promise：过渡要等它落位后才揭开 focus-reveal（正文淡入），
 *  否则会先露出旧排版、再跳成新排版。 */
function _afterLayout(chunkAnchor) {
  return new Promise(function (resolve) {
    requestAnimationFrame(function () {
      if (S.renderMode === 'native') {
        // 正文列宽（版心，主题页可调）也是 mode 的一部分，先重放主题 CSS 再重排
        EpubReader.applyThemeFromDom();
        const settle = Promise.resolve(EpubReader.resizeNative());
        // 上限：极端慢的补载也不能把进场卡住；超时按当前状态拍快照。
        // 收敛后再让一帧：iframe 的 display 是异步渲染，快照晚一帧更稳。
        Promise.race([settle, _delay(SETTLE_CAP_MS)]).then(function () {
          requestAnimationFrame(function () { resolve(); });
        }, function () {
          requestAnimationFrame(function () { resolve(); });
        });
      } else {
        _restoreChunkScroll(chunkAnchor);
        requestAnimationFrame(function () { resolve(); });
      }
    });
  });
}

// ── 进出过渡（View Transitions）────────────────────────────────────
// 书页在 CSS 里挂 view-transition-name（style.css）：浏览器把它当共享
// 元素，把旧快照从原阅读区几何插值放大到全屏，退出时反向；组内新旧
// 快照交叉淡化。chrome 的隐现由 root 的交叉淡出承担。
//
// ⚠️ VT 的 pending 阶段（回调 promise 未 resolve）会**冻结 rAF**——实测
// 只触发 1 次。而 native 重排链（_waitLayoutStableThen/display/收敛）
// 全靠 rAF，放在回调里 await 会把过渡永久卡在 pending：页面冻结、点击
// 落不到 iframe、iframe 也不重排。所以回调只做同步类切换、立即返回；
// 重排放到 `ready`（新快照已拍、动画开始，rAF 恢复）之后。
// 代价是新快照拍不到重排后的正文，因此过渡期间给正文盖一层 `focus-reveal`
// （只露书页底色），等动画与重排都完成后再淡入——
// 视觉语言变成「窗口放大 → 文字从新排版里浮出来」。
// 不支持该 API 或系统要求减少动态时直接切类，行为与旧版一致。
const SETTLE_CAP_MS = 450;      // 等 native 重排收敛的上限
const ROLLOVER_MS = 900;        // ready 迟迟不来时的兜底：先重排
let _vt = null;                 // 在途过渡（连点/切页时 skip）

function _delay(ms) {
  return new Promise(function (r) { setTimeout(r, ms); });
}

function _reducedMotion() {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (_) { return false; }
}

/** 一次模式切换。apply 必须是同步 DOM 变更；settle 是重排/落位。 */
function _runModeChange(apply, settle) {
  const start = document.startViewTransition;
  const removeVeil = function () {
    document.documentElement.classList.remove('focus-reveal');
  };
  if (typeof start !== 'function' || _reducedMotion()) {
    return Promise.resolve().then(apply).then(settle).catch(function () {});
  }
  // 连点：上一轮直接跳到终态再开新一轮（DOM 已在目标状态，安全）
  if (_vt) { try { _vt.skipTransition(); } catch (_) { /* 已结束 */ } }
  document.documentElement.classList.remove('focus-reveal');
  let t;
  try {
    t = start.call(document, function () {
      // pending 阶段：只做同步变更，绝不能 await（rAF 被冻结）
      document.documentElement.classList.add('focus-reveal');
      try { apply(); } catch (_) { /* 一次 DOM 变更失败不该把过渡打死 */ }
      return Promise.resolve();
    });
  } catch (_) {
    removeVeil();
    return Promise.resolve().then(apply).then(settle).catch(function () {});
  }
  _vt = t;
  // 新快照拍完、动画开始 → rAF 恢复，这时候才重排
  const settled = new Promise(function (resolve) {
    let started = false;
    const go = function () {
      if (started) return;
      started = true;
      Promise.resolve().then(settle).catch(function () {}).then(resolve);
    };
    t.ready.then(go, go);
    setTimeout(go, ROLLOVER_MS);
  });
  return Promise.all([t.finished.catch(function () {}), settled]).then(function () {
    // 已被新一轮切换接管时不能揭开：那会提前露出上一轮的重排结果
    if (_vt !== t) return;
    removeVeil();
    _vt = null;
  });
}

export function enterFocusMode() {
  if (isFocusMode() || !S.currentBook || !_readerActive()) return;
  _cancelEnter();
  const chunkAnchor = _captureChunkScroll();
  EpubReader.stashViewport();       // native：先记 CFI（chunk 模式空操作）
  // 用当前语义坐标先填一次示数：native 的示数平时靠滚动同步刷新，
  // 刚进入时如果用户还没滚过，底栏不至于空着。
  renderBookProgress(S.currentChapter, S.currentChunk);
  _runModeChange(function () {
    document.documentElement.classList.add('reading-focus');
    // 焦点留在被藏起来的输入框上会继续收键盘事件
    const active = document.activeElement;
    if (active && typeof active.blur === 'function') active.blur();
  }, function () {
    return _afterLayout(chunkAnchor);
  });
}

export function exitFocusMode() {
  if (!isFocusMode()) return;
  _cancelEnter();
  const chunkAnchor = _captureChunkScroll();
  EpubReader.stashViewport();
  _runModeChange(function () {
    document.documentElement.classList.remove('reading-focus');
  }, function () {
    return _afterLayout(chunkAnchor);
  });
}

/** 切走阅读页时静默收起：马上 display:none，不做重排/视口恢复
 *  （switchTab 自己会 stash + 回来后 restore）。只把 iframe 里的版心
 *  复原，免得 150ms 后 _watchTheme 再补一次重排。 */
export function leaveFocusModeSilently() {
  _cancelEnter();
  // 在途过渡先跳到终态：切走阅读页时不该还挂着一轮全屏动画
  if (_vt) { try { _vt.skipTransition(); } catch (_) { /* 已结束 */ } _vt = null; }
  document.documentElement.classList.remove('focus-reveal');
  if (!isFocusMode()) return;
  document.documentElement.classList.remove('reading-focus');
  try { EpubReader.applyThemeFromDom(); } catch (_) { /* 阅读页即将隐藏 */ }
}

// ── 事件 ─────────────────────────────────────────────────────────

function _handleClick(e, doc) {
  doc = doc || document;
  if (!_readerActive()) return;

  // 刚被用来关浮层的手势（搜索面板）→ 这一下不进出
  if (_overlayGraceActive()) return;

  const fromContent = doc !== document;
  if (!fromContent && !_inReaderArea(e.target)) return;

  // 浮层优先：先关笔记浮窗，这一下不进入；其余浮层自己有 dismiss 逻辑。
  if (_gesture && _gesture.overlay) {
    _dismissMiniPanel(e.target, doc);
    return;
  }

  if (isFocusMode()) {
    if (e.detail > 1 || !_qualifies(e, doc)) return;
    exitFocusMode();
    return;
  }

  // 进入只认正文区（头部/搜索栏/聊天区都不算「空点阅读页」）
  if (!fromContent && !_isReadingSurface(e.target)) return;
  if (e.detail > 1 || !_qualifies(e, doc)) return;

  const seq = ++_enterSeq;
  _enterTimer = setTimeout(function () {
    _enterTimer = 0;
    if (seq !== _enterSeq || isFocusMode()) return;
    if (!_readerActive() || _overlayOpen()) return;
    enterFocusMode();
  }, ENTER_DELAY_MS);
}

function _onHostPointerDown(e) {
  _beginGesture(e, document);
}

function _onHostClick(e) {
  // 用掉就作废：没有新 pointerdown 的程序性 click 不能复用上一次的坐标。
  try { _handleClick(e, document); } finally { _gesture = null; }
}

function _onContentPointerDown(e, doc) {
  _beginGesture(e, doc);
}

function _onContentClick(e, doc) {
  try { _handleClick(e, doc); } finally { _gesture = null; }
}

function _onKeyDown(e) {
  if (e.key !== 'Escape') return;
  // 批注气泡 / 编辑面板里的 Esc 是「关掉这一层」，不是「退出沉浸」。
  // 面板的 keydown 自己会处理；这里让路，否则一次 Esc 会连阅读模式一起退。
  const t = e.target;
  if (t && t.closest && t.closest(
      '.bc-note-panel, .bc-note-bubble, textarea, input, select,'
      + ' [contenteditable="true"]')) return;
  if (isFocusMode()) {
    e.preventDefault();
    exitFocusMode();
    return;
  }
  _cancelEnter();
}

function _bind() {
  if (_bound) return;
  _bound = true;
  // window 捕获阶段：保证在搜索面板的 document 捕获监听之前记下
  // 「浮层开着」的快照。
  window.addEventListener('pointerdown', _onHostPointerDown, true);
  window.addEventListener('click', _onHostClick, true);
  window.addEventListener('keydown', _onKeyDown, true);
  // 点了之后马上滚轮/触摸滚动 = 人在读，不是在切模式；撤销待进入。
  window.addEventListener('wheel', _cancelEnter, { passive: true, capture: true });
  window.addEventListener('touchmove', _cancelEnter, { passive: true, capture: true });
  // 正文 iframe 里的事件不冒泡到父文档，走 epub-reader 的桥。
  EpubReader.onContentPointerDown(_onContentPointerDown);
  EpubReader.onContentClick(_onContentClick);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _bind);
} else {
  _bind();
}

// ── HTML onclick / 控制台 access ─────────────────────────────────
window.isFocusMode = isFocusMode;
window.enterFocusMode = enterFocusMode;
window.exitFocusMode = exitFocusMode;
