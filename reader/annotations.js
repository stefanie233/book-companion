/* Liubai Reader — native annotation layer (收藏/批注二分版).

   收藏 (type='highlight'): 选区 → 深棕粗下划线，走 epub.js annotations
     'underline' 类型（section 懒加载注入免费获得）。
   批注 (type='note'):    选区 → 句末 chevron marker（morphicons 弹簧 morph：
     点击翻上展开气泡、收起翻回；文本不加标记），重放经 onSectionRendered 钩子。
   引用: 选区挂到聊天输入框 selectionTag（agent+阅读的招牌入口）。

   一切交互只活在 native EPUB 渲染（iframe）作用域内:
   - 选区检测: rendition.on('selected', cfiRange, contents)——fork 同 tick
     还 emit 'selectedRange'（原生 Range），浮动条零跨文档抓选区。
   - 定位: 回放/跳转以 cfi_range 为主；selected_text 仅展示与召回。
   - 换书: rendition 随 initNative 轮换，桥接回调自动指向新书。
*/

import { S } from './js/state.js';
import { showSelectionRef } from './js/chat.js';
import { createMorph } from './lib/morphicons.dom.js';
import {
  isNative, getRendition, getBook, getNativeBookId,
  onBookReady, onSectionRendered,
} from './js/epub-reader.js';

/* __BLOCK2__ */

// ── State ────────────────────────────────────────────────────────────

// Lucide chevron 路径（d 字符串直接喂 createMorph）：全等对 → Procrustes
// 解出纯 180° 旋转，弹簧半圈翻面即"展开/收起"方向语义
const CMT_ICON_DOWN = 'm6 9 6 6 6-6';
const CMT_ICON_UP = 'm15 18-6-6 6-6';
const SVG_NS = 'http://www.w3.org/2000/svg';

let stableIdCache = {};            // bookId → stable_book_id
let _sel = null;                   // 活动选区 {cfi, range, text, contents, doc, rect}
let _collections = {};             // cfi_range → annotation dict（本书收藏索引）
let _commentGroups = new Map();    // cfi_range → 批注 rec[]（marker 聚合制：同段多条共存）
let _pendingGroupCfi = null;       // 气泡内"新增"时的挂载组（无活动选区）
let _bubbleCfi = null;             // 当前气泡对应的组键（角标会被 section 重渲染换掉，认组不认节点）
let _openMarker = null;            // 当前展开气泡的 marker（翻态锚点，至多一个）
let _underlineCfi = null;          // 当前预览下划线所属 cfi（我们加的才记，收藏线不归这里撤）
const _markerState = new WeakMap(); // marker → { morph, open }

// CFI → spine index：base 形如 epubcfi(/6/8!...，末段 /2n → index n-1。
// contents.range 不校验 spine base——结构相似的其他 section 也会凑巧
// 命中（实测 section 6 误试 section 7 的 CFI 还抛 IndexSizeError），必须先校验。
function _cfiSpineIndex(cfi) {
  const m = String(cfi).split('!')[0].match(/\/(\d+)$/);
  if (!m) return -1;
  const n = parseInt(m[1], 10);
  return n > 0 && n % 2 === 0 ? n / 2 - 1 : -1;
}
let _commentDocs = new WeakSet();  // 已挂"点击空白收条"的 iframe document
let _menuBlockedDocs = new WeakSet(); // 已屏蔽原生右键菜单的 iframe document
let _legacyAnns = [];              // 无 cfi_range 的古早批注（按原文现找位置）
let _boxPanes = new WeakMap();     // view 容器 → { iframe, svg, ro }（框选自绘层）
const _BOX_SVG_NS = SVG_NS;
const _POP_ANIM = { duration: 170, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' };

/* __BLOCK3__ */

// ── Selection hook（浮动条入口）─────────────────────────────────────

function bindSelectionHook() {
  const rnd = getRendition();
  if (!rnd) return;
  rnd.on('selected', (cfiRange, contents) => {
    if (!cfiRange || !contents) return;
    // 同 tick 抓原生 Range + 选中文本（emit 后浏览器不清选区）
    let range = null;
    try {
      const sel = contents.window.getSelection();
      if (sel && sel.rangeCount && !sel.getRangeAt(0).collapsed) {
        range = sel.getRangeAt(0).cloneRange();
      }
    } catch (_) { /* 沙箱边界 → getRange 兜底 */ }
    if (!range) {
      const bk = getBook();
      if (!bk) return;
      try { bk.getRange(cfiRange).then(r => r && _onNativeSelection(cfiRange, r, contents)); return; }
      catch (_) { return; }
    }
    _onNativeSelection(cfiRange, range, contents);
  });
}

function _onNativeSelection(cfi, range, contents) {
  const text = (range.toString() || '').trim();
  if (!text || text.length < 2 || !S.currentBook) return;
  // 跨 section 选区：range 与 cfi 可能异头，首版不特判（章节边界拖选罕见）
  const rect = _rangeToViewportRect(range, contents);
  if (!rect) return;
  _sel = { cfi, range, text, contents, doc: range.startContainer.ownerDocument, rect };
  // Structural coordinates for the chat pipeline: the selection is "already
  // seen" evidence. `contents.sectionIndex` is the epub spine position; the
  // backend maps it to DB chapter(s). Do not guess chapter/chunk here —
  // spine != chapter_index in books with front matter or split chapters.
  S.selectionSection = contents.sectionIndex;
  S.selectionCfi = cfi;
  S.selectionChapter = null;
  S.selectionChunk = null;
  // Wider probe around the selection: a word like "杨修" appears in several
  // chunks; ±context lets the backend pick the one the reader selected.
  try {
    const r2 = range.cloneRange();
    r2.setStart(range.startContainer,
      Math.max(0, (range.startOffset || 0) - 40));
    r2.setEnd(range.endContainer,
      Math.min((range.endContainer.nodeValue || '').length,
        (range.endOffset || 0) + 120));
    S.selectionAnchorText = (r2.toString() || text).trim().slice(0, 200);
  } catch (_) {
    S.selectionAnchorText = text;
  }
  // 已在收藏/批注标记内 → 编辑态
  const inCollect = _closestMark(range, '.bc-underline');
  if (inCollect) { _showBar(rect, 'collect-edit', _sel); }
  else { _showBar(rect, 'new', _sel); }
  _armDismissOnDocPointer(contents);
}

/* __BLOCK4__ */

// ── Floating action bar（主文档层，坐标由 iframe 换算）──────────────

function _getBar() {
  let bar = document.getElementById('bcSelBar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'bcSelBar';
    bar.className = 'bc-sel-bar';
    document.body.appendChild(bar);
    bar.addEventListener('mousedown', e => e.preventDefault());  // 保住 iframe 选区
    bar.addEventListener('click', e => {
      const btn = e.target.closest('button[data-act]');
      if (btn) _onBarAction(btn.dataset.act, btn.dataset.style);
    });
  }
  return bar;
}

function _showBar(rect, mode, sel) {
  const bar = _getBar();
  const inCollect = mode === 'collect-edit';
  bar.innerHTML = inCollect
    ? `<button data-act="uncollect"><i data-lucide="x"></i> 取消收藏</button>
       <button data-act="material" title="把这段原文收进全局素材库的待归类"><i data-lucide="package-plus"></i> 收录为素材</button>`
    : `<button data-act="collect"><i data-lucide="bookmark"></i> 收藏</button>
       <button data-act="material" title="把这段原文收进全局素材库的待归类"><i data-lucide="package-plus"></i> 收录为素材</button>
       <button data-act="comment"><i data-lucide="message-square"></i> 批注</button>
       <button data-act="quote"><i data-lucide="pin"></i> 引用</button>
       <button data-act="copy"><i data-lucide="copy"></i> 复制</button>
       <div class="bc-style-pop" role="menu" aria-label="收藏样式">
         <button data-act="collect" data-style="box" role="menuitem"
                 title="框选：整段选区一个框，跨段落也圈在一起">
           <i data-lucide="square-dashed"></i><span>框选</span>
         </button>
         <button data-act="collect" data-style="underline" role="menuitem"
                 title="下划线：贴着每行文字画一条线">
           <i data-lucide="underline"></i><span>下划线</span>
         </button>
       </div>`;
  bar.style.display = 'flex';
  _placeAbove(bar, rect);
  if (!inCollect) _wireStylePop(bar);
  if (window.lucide) window.lucide.createIcons();
}

/* __BLOCK5A__ */

// ── 收藏样式菜单（悬停「收藏」向上弹出）─────────────────────────────
// 菜单挂成浮条的子节点：mouseleave 按 DOM 祖先判定，指针从按钮移到菜单
// 上不会触发浮条的收起，比另起一个 fixed 浮层省掉一套同步坐标的逻辑。
function _wireStylePop(bar) {
  const pop = bar.querySelector('.bc-style-pop');
  const btn = bar.querySelector('button[data-act="collect"]');
  if (!pop || !btn) return;
  let showT = 0, hideT = 0;
  const arm = () => {
    clearTimeout(hideT);
    clearTimeout(showT);
    showT = setTimeout(() => _stylePopOpen(bar, pop), 70);
  };
  const disarm = () => {
    clearTimeout(showT);
    clearTimeout(hideT);
    hideT = setTimeout(() => _popClose(pop), 160);
  };
  btn.addEventListener('mouseenter', arm);
  btn.addEventListener('mouseleave', disarm);
  btn.addEventListener('focus', arm);
  // 指针进菜单：撤销全部定时器（含还在等待展示的那次）
  pop.addEventListener('mouseenter', () => { clearTimeout(showT); clearTimeout(hideT); });
  pop.addEventListener('mouseleave', disarm);
}

function _stylePopOpen(bar, pop) {
  if (pop.classList.contains('open')) return;
  pop.classList.add('open');
  // 菜单对准「收藏」那颗按钮，而不是浮条正中（菜单讲的就是这一颗按钮）。
  const btn = bar.querySelector('button[data-act="collect"]:not([data-style])');
  const br = bar.getBoundingClientRect();
  if (btn) {
    const bb = btn.getBoundingClientRect();
    const w = pop.offsetWidth || 116;
    const half = w / 2;
    const min = Math.max(half + 8, br.left + half);
    const max = Math.min(window.innerWidth - half - 8, br.right - half);
    let center = bb.left + bb.width / 2;
    if (max >= min) center = Math.max(min, Math.min(center, max));
    pop.style.left = (center - br.left - bar.clientLeft) + 'px';
  }
  // 浮条本身可能被放到选区下方（上方没空间），菜单再往上就会飞出屏幕。
  const ph = pop.offsetHeight || 0;
  pop.classList.toggle('below', br.top - ph - 10 < 8);
  _popAnimate(pop, true);
}

function _popClose(pop) {
  if (!pop.classList.contains('open')) return;
  if (_prefersReduced() || typeof pop.animate !== 'function') {
    pop.classList.remove('open');
    return;
  }
  const y = pop.classList.contains('below') ? -4 : 4;
  try {
    pop.getAnimations().forEach(a => a.cancel());
    const anim = pop.animate([
      { opacity: 1, transform: 'translateX(-50%) translateY(0) scale(1)' },
      { opacity: 0, transform: `translateX(-50%) translateY(${y}px) scale(.97)` },
    ], { duration: 110, easing: 'ease-in' });
    anim.onfinish = () => pop.classList.remove('open');
  } catch (_) { pop.classList.remove('open'); }
}

function _prefersReduced() {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; }
  catch (_) { return false; }
}

function _popAnimate(pop, open) {
  if (!open || _prefersReduced() || typeof pop.animate !== 'function') return;
  const from = pop.classList.contains('below') ? -6 : 6;
  try {
    pop.getAnimations().forEach(a => a.cancel());
    pop.animate([
      { opacity: 0, transform: `translateX(-50%) translateY(${from}px) scale(.96)` },
      { opacity: 1, transform: 'translateX(-50%) translateY(0) scale(1)' },
    ], _POP_ANIM);
  } catch (_) { /* 动画只是锦上添花 */ }
}

/* __BLOCK5__ */

function _hideBar() {
  const bar = document.getElementById('bcSelBar');
  if (bar) bar.style.display = 'none';
}

function _onBarAction(act, style) {
  if (act === 'uncollect-ann') { _uncollectByCfi(_getBar().dataset.cfi); return; }
  if (!_sel) return;
  if (act === 'collect') {
    // 触屏没有 hover：点「收藏」先把样式菜单摆出来，再点一次款式才落库。
    if (!style && window.matchMedia && window.matchMedia('(hover: none)').matches) {
      const bar = _getBar();
      const pop = bar.querySelector('.bc-style-pop');
      if (pop) { _stylePopOpen(bar, pop); return; }
    }
    createCollection(style);
  }
  else if (act === 'uncollect') _uncollectAtSelection();
  else if (act === 'comment') {
    // 聚合制：同段已有批注 → 直接展开组气泡（浏览 + 新增），不另起组
    const existing = _commentGroups.get(_sel.cfi);
    const marker = existing && existing.length
      ? _findGroupMarker(_sel.cfi) : null;
    if (marker) { showGroupBubble(_sel.cfi, marker); _hideBar(); }
    else openNotePanel(null);
  }
  else if (act === 'material') intakeMaterial();
  else if (act === 'quote') quoteToChat();
  else if (act === 'copy') copySelection();
}

// ── Bar actions ─────────────────────────────────────────────────────

async function createCollection(style) {
  if (!_sel || !S.currentBook) return;
  const kind = style === 'box' ? 'box' : 'underline';
  const body = _annotationPayload({
    type: 'highlight', style: kind, cfi_range: _sel.cfi,
  });
  try {
    const resp = await fetch(S.API + '/api/annotations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.id) {
      const rec = { id: data.id, ...body };
      _collections[rec.cfi_range] = rec;
      // 同段原来是另一种样式时，账本覆盖后整层重画自然就换过来了
      _redrawMarksAll();
      _hideBar();
    } else { console.error('收藏失败:', data.error); }
  } catch (e) { console.error('收藏失败:', e); }
}

/* __BLOCK6__ */

async function _uncollectAtSelection() {
  if (!_sel) return;
  await _uncollectByCfi(_sel.cfi);
}

async function _uncollectByCfi(cfi) {
  const rec = _collections[cfi];
  if (!rec || !rec.id) { _hideBar(); return; }
  try {
    const resp = await fetch(S.API + '/api/annotations/' + rec.id, { method: 'DELETE' });
    const data = await resp.json();
    if (data.deleted) {
      delete _collections[cfi];
      _redrawMarksAll();            // 账本删掉后整层重画
    }
  } catch (e) { console.error('取消收藏失败:', e); }
  _hideBar();
}

function quoteToChat() {
  if (!_sel) return;
  S.selectionSource = 'book';
  S.lastSelection = _sel.text;
  S.lastRange = null;   // iframe Range 对主文档聊天无意义
  showSelectionRef(_sel.text, '书中');
  _hideBar();
}

async function copySelection() {
  if (!_sel) return;
  try { await navigator.clipboard.writeText(_sel.text); }
  catch (_) { /* 剪贴板权限拒绝时静默——选区仍在，可手动复制 */ }
  _hideBar();
}

/* __BLOCK6B__ */

// ── 收录为素材（阅读页 → 写作页）─────────────────────────────────────
// 待归类卡：选中文字逐字落 待归类/《书名》-时间戳.md。动画负责确认感：纸团从
// 选区内随机一点沿抛物线飞向写作导航图标，图标被砸一下。素材库由后端写，
// 前端不等结果就先起飞，请求失败只在控制台报（图标照常收尾）。

async function intakeMaterial() {
  if (!_sel || !S.currentBook) return;
  const sel = _sel;
  const from = _randomPointInSelection(sel);
  const target = document.getElementById('navWrite');
  _hideBar();
  const ball = _spawnPaperBall(from);
  const flight = ball && target
    ? _flyPaperBall(ball, target, from) : Promise.resolve();

  const payload = {
    book_id: S.currentBook,
    excerpt: sel.text,
    cfi_range: sel.cfi,
  };
  // native 模式下 S.currentChapter 未必跟着选区走；只在 chunk 模式信它。
  if (S.renderMode === 'chunk') {
    payload.chapter_index = S.currentChapter;
    payload.chunk_index = S.currentChunk;
  }
  let data = null;
  try {
    const resp = await fetch(S.API + '/api/writing/materials/intake', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    data = await resp.json();
    if (data && data.path && window.WritingUI
        && typeof window.WritingUI.refreshLibrary === 'function') {
      // 待归类卡落进 待归类：写作页的素材库树与待整理数当场对齐，不用等下次
      // 切页——切回写作页时 open() 在「作品没变」那条分支会早退。
      window.WritingUI.refreshLibrary();
    }
  } catch (e) { console.error('收录为素材失败:', e); }

  await flight;
  if (ball) ball.remove();
  _navWriteHit(target);
  if (!data || !data.path) console.error('收录为素材失败:', data);
  return data;
}

function _randomPointInSelection(sel) {
  let frame = null;
  try { frame = sel.contents.window.frameElement; } catch (_) { /* fallback */ }
  const box = frame ? frame.getBoundingClientRect() : { left: 0, top: 0 };
  const rects = [];
  try {
    for (const r of sel.range.getClientRects() || []) {
      if (r.width <= 4 || r.height <= 4) continue;
      const left = box.left + r.left, top = box.top + r.top;
      if (top < 0 || left < 0 || top > window.innerHeight || left > window.innerWidth) continue;
      rects.push({ left, top, width: r.width, height: r.height });
    }
  } catch (_) { /* 取不到行框就退到整块 */ }
  const r = rects.length
    ? rects[Math.floor(Math.random() * rects.length)]
    : (sel.rect || { left: window.innerWidth / 2, top: window.innerHeight / 2, width: 0, height: 0 });
  const x = r.left + Math.random() * (r.width || 1);
  const y = r.top + Math.random() * (r.height || 1);
  return {
    x: Math.min(Math.max(x, 6), window.innerWidth - 6),
    y: Math.min(Math.max(y, 6), window.innerHeight - 6),
  };
}

function _spawnPaperBall(point) {
  const ball = document.createElement('div');
  ball.className = 'bc-paper-ball';
  ball.setAttribute('aria-hidden', 'true');
  ball.style.left = point.x + 'px';
  ball.style.top = point.y + 'px';
  document.body.appendChild(ball);
  return ball;
}

// 二次贝塞尔采样成 keyframes：控制点抬到两点的中垂线上方，就是一条抛物线。
function _flyPaperBall(ball, target, from) {
  if (_prefersReduced() || typeof ball.animate !== 'function') {
    return Promise.resolve();
  }
  const tr = target.getBoundingClientRect();
  const to = { x: tr.left + tr.width / 2, y: tr.top + tr.height / 2 };
  const dx = to.x - from.x, dy = to.y - from.y;
  const dist = Math.hypot(dx, dy) || 1;
  const lift = Math.max(60, Math.min(220, dist * 0.45));
  const cx = from.x + dx * 0.5;
  const cy = from.y + dy * 0.5 - lift;
  const steps = 26;
  const frames = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps, mt = 1 - t;
    const x = mt * mt * from.x + 2 * mt * t * cx + t * t * to.x;
    const y = mt * mt * from.y + 2 * mt * t * cy + t * t * to.y;
    frames.push({
      offset: t,
      transform: `translate(${x - from.x}px, ${y - from.y}px) rotate(${t * 540}deg) scale(${1 - 0.22 * t})`,
      opacity: t > 0.9 ? 1 - (t - 0.9) / 0.1 : 1,
    });
  }
  let anim;
  try {
    anim = ball.animate(frames, {
      duration: 780, easing: 'linear', fill: 'forwards',
    });
  } catch (_) { return Promise.resolve(); }
  return Promise.resolve(anim.finished).catch(() => {});
}

function _navWriteHit(target) {
  if (!target) return;
  target.classList.remove('nav-hit');
  void target.offsetWidth;          // 重新触发同一段动画
  target.classList.add('nav-hit');
  setTimeout(() => target.classList.remove('nav-hit'), 720);
}

/* __BLOCK7__ */

// ── Comment panel（主文档层批注面板：新建 / 编辑双模式）─────────────

let _editingComment = null;   // 编辑模式时的批注 dict

function _getNotePanel() {
  let panel = document.getElementById('bcNotePanel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'bcNotePanel';
    panel.className = 'bc-note-panel';
    panel.innerHTML = `
      <textarea id="bcNoteText" placeholder="写点批注...（支持 Markdown）" rows="4"></textarea>
      <div class="bc-note-actions">
        <button data-act="cancel">取消</button>
        <button data-act="save" class="primary">确定</button>
      </div>`;
    document.body.appendChild(panel);
    panel.addEventListener('mousedown', e => e.preventDefault());
    panel.addEventListener('click', e => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      if (btn.dataset.act === 'cancel') closeNotePanel();
      else if (btn.dataset.act === 'save') saveComment();
    });
    panel.addEventListener('keydown', e => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) saveComment();
      if (e.key === 'Escape') closeNotePanel();
    });
  }
  return panel;
}

/* __BLOCK8__ */

function openNotePanel(existing, anchorRect) {
  const panel = _getNotePanel();
  _editingComment = existing || null;
  document.getElementById('bcNoteText').value = _editingComment ? (_editingComment.note_text || '') : '';
  const rect = anchorRect || (_sel && _sel.rect);
  panel.style.display = 'flex';
  if (rect) _placeBelow(panel, rect);
  document.getElementById('bcNoteText').focus();
}

function closeNotePanel() {
  const panel = document.getElementById('bcNotePanel');
  if (panel) panel.style.display = 'none';
  _editingComment = null;
  _pendingGroupCfi = null;
  // 编辑时保留下来的预览下划线到这里收尾（气泡先一步收起，面板是最后
  // 一层浮层）；新建批注的路径没有下划线，这里是空操作。
  _hideAnnotationUnderline();
}

async function saveComment() {
  const text = document.getElementById('bcNoteText').value.trim();
  if (!text) { closeNotePanel(); return; }
  if (_pendingGroupCfi) { await _appendToGroup(_pendingGroupCfi, text); return; }
  if (_editingComment) { await _updateComment(_editingComment, text); return; }
  if (!_sel || !S.currentBook) return;
  const body = _annotationPayload({ type: 'note', note_text: text, cfi_range: _sel.cfi });
  try {
    const resp = await fetch(S.API + '/api/annotations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.id) {
      const rec = { id: data.id, ...body };
      const list = _commentGroups.get(rec.cfi_range) || [];
      list.push(rec);
      _commentGroups.set(rec.cfi_range, list);
      if (!_findGroupMarker(rec.cfi_range)) {   // DOM 实查防重（组已在屏 → 只归档）
        _renderGroupMarker(rec.cfi_range, _sel.range);
      }
      closeNotePanel();
      _hideBar();
    } else { console.error('批注失败:', data.error); }
  } catch (e) { console.error('批注失败:', e); }
}

// 气泡内"新增"：无活动选区，定位/原文等字段复用组内首条
async function _appendToGroup(cfi, text) {
  const list = _commentGroups.get(cfi) || [];
  const first = list[0] || {};
  const body = {
    book_id: S.currentBook,
    chapter_index: first.chapter_index,
    chunk_index: first.chunk_index,
    selected_text: first.selected_text,
    context_before: first.context_before,
    context_after: first.context_after,
    type: 'note', note_text: text, cfi_range: cfi,
  };
  try {
    const resp = await fetch(S.API + '/api/annotations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.id) {
      list.push({ id: data.id, ...body });
      _refreshGroupBubble(cfi);
    } else { console.error('批注失败:', data.error); }
  } catch (e) { console.error('批注失败:', e); }
  closeNotePanel();
}

/* __BLOCK9__ */

async function _updateComment(rec, text) {
  try {
    const resp = await fetch(S.API + '/api/annotations/' + rec.id, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note_text: text }),
    });
    const data = await resp.json();
    if (data.updated) {
      rec.note_text = text;
      _refreshCommentBubble(rec);
      closeNotePanel();
    }
  } catch (e) { console.error('批注更新失败:', e); }
}

async function deleteComment(annId) {
  if (!window.confirm('删除这条批注？原文里的标记会一起消失。')) return;
  try {
    const resp = await fetch(S.API + '/api/annotations/' + annId, { method: 'DELETE' });
    const data = await resp.json();
    if (data.deleted) {
      // 组内移除该条；组空 → 摘 marker（组非空保留，气泡刷新）
      for (const [cfi, recs] of _commentGroups) {
        const idx = recs.findIndex(r => r.id === annId);
        if (idx < 0) continue;
        recs.splice(idx, 1);
        if (!recs.length) { _commentGroups.delete(cfi); _removeGroupMarker(cfi); }
        else _refreshGroupBubble(cfi);
        break;
      }
    }
  } catch (e) { console.error('批注删除失败:', e); }
  _hideBubble();
}

// 按 cfi 摘除组 marker（marker 在 iframe 文档，需遍历 contents）
function _removeGroupMarker(cfi) {
  const rnd = getRendition();
  const list = (rnd && rnd.getContents()) || [];
  for (const c of list) {
    const d = c.document;
    if (!d) continue;
    d.querySelectorAll(`.bc-ann-cmt[data-cfi="${cfi}"]`).forEach(m => {
      const st = _markerState.get(m);
      if (st) { st.morph.destroy(); _markerState.delete(m); }
      if (_openMarker === m) _openMarker = null;
      m.remove();
    });
  }
}

/* __BLOCK10__ */

// ── Comment marker（句末 ▾，iframe 文档内插入）──────────────────────

// marker 翻态：open=true 翻上（气泡已展开）、false 翻回；同一时刻至多一个展开
function _setMarkerOpen(marker, open) {
  const st = _markerState.get(marker);
  if (!st) return;
  if (open && !st.open) {
    if (_openMarker && _openMarker !== marker) _setMarkerOpen(_openMarker, false);
    _openMarker = marker;
    st.open = true;
    st.morph.morphTo(CMT_ICON_UP);
  } else if (!open && st.open) {
    if (_openMarker === marker) _openMarker = null;
    st.open = false;
    st.morph.morphTo(CMT_ICON_DOWN);
  }
}

// 组锚点 marker：data-cfi 挂组键，点击展开该组全部批注
function _renderGroupMarker(cfi, range) {
  const doc = range.startContainer.ownerDocument;
  if (!doc) return null;
  const marker = doc.createElement('span');
  marker.className = 'bc-ann-cmt';
  marker.setAttribute('data-cfi', cfi);
  marker.title = '查看批注';
  const svg = doc.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  const path = doc.createElementNS(SVG_NS, 'path');
  svg.appendChild(path);
  marker.appendChild(svg);
  // collapse 到选区末尾插入 → 选中字段的句末（"4"和"5"之间）
  const r = range.cloneRange();
  r.collapse(false);
  try { r.insertNode(marker); } catch (_) { return null; }
  _markerState.set(marker, {
    morph: createMorph(path, CMT_ICON_DOWN, { reducedMotion: 'user' }),
    open: false,
  });
  // 再点一次同一个角标 = 收起（和点空白、Esc 同一条收尾路径）。
  // 角标自己就是开关，不用再去找气泡里的关闭键。
  marker.addEventListener('click', e => {
    e.stopPropagation();
    const st = _markerState.get(marker);
    if (st && st.open) { _hideBubble(); return; }
    showGroupBubble(cfi, marker);
  });
  return marker;
}

// 组气泡：按条呈现 + 新增入口。
// 不再贴原文引用：点开时正文里那道蓝下划线（_showAnnotationUnderline）
// 已经指回被批注的句子，气泡里再抄一遍是冗余，还占掉首屏。
function showGroupBubble(cfi, marker) {
  const recs = _commentGroups.get(cfi) || [];
  _bubbleCfi = cfi;
  const bubble = _getBubble();
  bubble.innerHTML = `
    <div class="bc-bubble-list"></div>
    <button data-act="add" class="bc-bubble-add">+ 新增批注</button>`;
  const listEl = bubble.querySelector('.bc-bubble-list');
  for (const rec of recs) {
    const item = document.createElement('div');
    item.className = 'bc-bubble-item';
    item.innerHTML = `
      <div class="bc-bubble-text"></div>
      <div class="bc-bubble-actions">
        <button data-act="edit" data-id="${rec.id}">编辑</button>
        <button data-act="del" data-id="${rec.id}" class="danger">删除</button>
      </div>`;
    item.querySelector('.bc-bubble-text').textContent = rec.note_text || '';
    listEl.appendChild(item);
  }
  const rect = _elementToViewportRect(marker);
  bubble.style.display = 'block';
  _placeBelow(bubble, rect);
  _setMarkerOpen(marker, true);
  _showAnnotationUnderline(cfi);
  // 气泡挂在主文档，正文在 iframe：给这个 iframe 文档补上「点空白收起」。
  // 不挂在选区钩子里也就意味着没拖过选区的页面同样能收起。
  _armDismissOnDocPointer({ document: marker.ownerDocument });
  bubble.onclick = e => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'add') {
      _pendingGroupCfi = cfi;
      _hideBubble(true);          // 编辑期间保留下划线，面板关了再撤
      openNotePanel(null, rect);
    } else if (btn.dataset.act === 'edit') {
      const rec = recs.find(r => r.id === btn.dataset.id);
      if (rec) { _hideBubble(true); openNotePanel(rec, rect); }
    } else if (btn.dataset.act === 'del') {
      deleteComment(btn.dataset.id);
    }
  };
}

/* __BLOCK11__ */

function _getBubble() {
  let bubble = document.getElementById('bcNoteBubble');
  if (!bubble) {
    bubble = document.createElement('div');
    bubble.id = 'bcNoteBubble';
    bubble.className = 'bc-note-bubble';
    document.body.appendChild(bubble);
  }
  return bubble;
}

function _hideBubble(keepUnderline) {
  const bubble = document.getElementById('bcNoteBubble');
  // 只有「本来开着」才算一次收气泡：焦点模式用 _bcOverlayClosedAt 把这一下
  // 点击让给浮层，关了空气泡的点击不该被吞。
  const wasOpen = !!(bubble && bubble.style.display === 'block');
  if (bubble) bubble.style.display = 'none';
  _bubbleCfi = null;
  if (_openMarker) _setMarkerOpen(_openMarker, false);   // 气泡收起 → marker 翻回
  if (!keepUnderline) _hideAnnotationUnderline();
  if (wasOpen) window._bcOverlayClosedAt = performance.now();
}

// ── 批注预览下划线 ────────────────────────────────────────────────
// 点开 marker 时把当初被批注的那段文字画上蓝线：气泡里不再抄原文，
// 正文里这道线就是「这条批注指的是哪句话」。线也画在自绘层里（_underlineCfi
// 是账本，_redrawMarksInContents 负责画），section 重渲染会自动补回来。

function _showAnnotationUnderline(cfi) {
  if (!cfi || _underlineCfi === cfi) return;
  _underlineCfi = cfi;
  _redrawMarksAll();
}

function _hideAnnotationUnderline() {
  if (!_underlineCfi) return;
  _underlineCfi = null;
  _redrawMarksAll();
}

// 组内容变化（新增/编辑/删除后）重渲染气泡——保持开着的气泡实时反映
function _refreshGroupBubble(cfi) {
  const bubble = document.getElementById('bcNoteBubble');
  if (!bubble || bubble.style.display === 'none') return;
  const marker = _findGroupMarker(cfi);
  if (marker) showGroupBubble(cfi, marker);
}

function _findGroupMarker(cfi) {
  const rnd = getRendition();
  const list = (rnd && rnd.getContents()) || [];
  for (const c of list) {
    const m = c.document && c.document.querySelector(`.bc-ann-cmt[data-cfi="${cfi}"]`);
    if (m) return m;
  }
  return null;
}

/* __BLOCK12__ */

// ── Replay（回放：收藏懒注入 + 批注 marker 重放）────────────────────

async function _loadBookAnnotations() {
  const bookId = getNativeBookId() || S.currentBook;
  if (!bookId) return;
  _collections = {};
  _commentGroups = new Map();
  _legacyAnns = [];
  _openMarker = null;   // 旧 rendition 的 marker 随 iframe 销毁（WeakMap 引用自动回收）
  try {
    let sid = stableIdCache[bookId];
    if (!sid) {
      const ctxResp = await fetch(S.API + '/api/books/' + bookId + '/context');
      const ctx = await ctxResp.json();
      sid = ctx.stable_book_id;
      if (sid) stableIdCache[bookId] = sid;
    }
    if (!sid) return;
    const resp = await fetch(S.API + '/api/annotations?stable_book_id=' + sid + '&limit=200');
    const data = await resp.json();
    const anns = data.annotations || [];
    const rnd = getRendition();
    for (const ann of anns) {
      if (!ann.cfi_range) {
        // 古早记录（v16 之前）只存了原文和章块号，没有 CFI，正文里定位
        // 不了——先登记，等 section 渲染出来按原文现找位置，见
        // _resolveLegacyInContents。
        if (ann.type === 'note' || ann.type === 'highlight' || ann.type === 'both') {
          _legacyAnns.push(ann);
        }
        continue;
      }
      if (ann.type === 'highlight') {
        _collections[ann.cfi_range] = ann;
      } else if (ann.type === 'note') {
        const list = _commentGroups.get(ann.cfi_range) || [];
        list.push(ann);
        _commentGroups.set(ann.cfi_range, list);
      }
    }
  } catch (e) { console.error('标注加载失败:', e); }
  _replayCommentsAllContents();
  _replayMarksAll();
  // continuous 渐进渲染与 fetch 无时序保证：首次全量重放可能踩在"文档
  // 未就绪（contents.range 返回 null）/view 尚未渲染"的空档上，而之后
  // 没有滚动就没有 rendered 再触发——按退避节奏分轮补插。DOM 实查幂等，
  // 已插上的组直接跳过。
  const backoff = [500, 1000, 2000, 3000, 5000];
  for (let i = 0; i < backoff.length; i++) {
    setTimeout(() => { _replayCommentsAllContents(); _replayMarksAll(); }, backoff[i]);
  }
}

// 收藏重放全量入口（rendered 版本按 section 过滤；此处兜底全窗口）
function _replayMarksAll() {
  const rnd = getRendition();
  if (!rnd) return;
  try { for (const c of rnd.getContents() || []) _replayMarksInContents(c); }
  catch (_) { /* best-effort */ }
}

/* __BLOCK13__ */

// 收藏的两种呈现都在自绘层里画（见 _redrawMarksInContents），不再走
// epub.js 的 annotations.add：
//   · 它的 Underline 对跨段选区会退化成「每段一个整栏块框」——实测一条
//     460 字的批注，预览线全是顶格的黑线；
//   · 它的 add 只替换账本、不摘旧 mark，重放会一层层叠粗。
// 自己画：逐文本节点切 range 拿到紧贴文字的行框，画线 + 透明命中线。

// 点击已有下划线 → 编辑态浮动条（取消收藏）
function showCollectEditBar(ann) {
  const rec = _collections[ann.cfi_range || ann.cfi] || ann;
  _sel = null;
  const bar = _getBar();
  bar.innerHTML = `<button data-act="uncollect-ann"><i data-lucide="x"></i> 取消收藏</button>`;
  bar.style.display = 'flex';
  bar.style.left = '50%';
  bar.style.transform = 'translateX(-50%)';
  bar.style.top = '12px';
  bar.dataset.annId = rec.id;
  bar.dataset.cfi = rec.cfi_range;
  if (window.lucide) window.lucide.createIcons();
}

/* __BLOCK13A__ */

// ── 框选（style='box'）：一个框圈住整次选区 ─────────────────────────
// epub.js 的标注层只有 highlight / underline / mark 三种形状，且 Underline
// 是逐行画线，画不出「跨三段的同一个框」。框选改为在同一个 view 容器里
// 叠一层自己的 svg：把 range.getClientRects() 的每行矩形取并集，画一个
// 圆角矩形——用户选了三段，框就把三段一起圈进去。
//
// 坐标系：容器是 position:relative，iframe 内部不滚动（epub.js 让 iframe
// 高度等于内容高度），所以 range 的 clientRect 减去 iframe 在容器里的偏移
// 就是 svg 坐标。比复用 fork 里 Pane 那套自计算的坐标直白得多。
//
// 重画时机：section 渲染 / view 重建（iframe 换了就重挂 svg）/
// iframe 尺寸变化（字号、版心、窗口变化都会走 ResizeObserver）。

function _annStyle(ann) {
  return ann && ann.style === 'box' ? 'box' : 'underline';
}

function _boxPaneFor(contents) {
  let iframe = null;
  try { iframe = contents.window.frameElement; } catch (_) { return null; }
  const container = iframe && iframe.parentElement;
  if (!container) return null;
  let rec = _boxPanes.get(container);
  if (rec && rec.iframe !== iframe) {
    // view 重建：旧 svg 属于旧 iframe，别让它留在新内容上面
    try { rec.ro && rec.ro.disconnect(); } catch (_) { /* 已 detach */ }
    if (rec.svg && rec.svg.parentNode) rec.svg.parentNode.removeChild(rec.svg);
    rec = null;
    _boxPanes.delete(container);
  }
  if (!rec) {
    const svg = document.createElementNS(_BOX_SVG_NS, 'svg');
    svg.setAttribute('class', 'bc-box-pane');
    svg.setAttribute('aria-hidden', 'true');
    container.appendChild(svg);
    rec = { iframe, svg, ro: null };
    if (typeof ResizeObserver !== 'undefined') {
      rec.ro = new ResizeObserver(() => { _redrawMarksInContents(contents); });
      try { rec.ro.observe(iframe); } catch (_) { /* 不支持则只在 rendered 时画 */ }
    }
    _boxPanes.set(container, rec);
  }
  try {
    const cr = container.getBoundingClientRect();
    const ir = iframe.getBoundingClientRect();
    rec.svg.style.left = (ir.left - cr.left - container.clientLeft) + 'px';
    rec.svg.style.top = (ir.top - cr.top - container.clientTop) + 'px';
    rec.svg.style.width = ir.width + 'px';
    rec.svg.style.height = ir.height + 'px';
  } catch (_) { /* 几何拿不到就按 0,0 画，下一轮再修正 */ }
  return rec;
}

// 一个 section 的收藏层整层重画：框选画框、下划线画线，外加点开气泡时的
// 批注预览线。数量小（一本书的收藏 + 一条预览），整层清掉重画最不容易出状态。
function _redrawMarksInContents(contents) {
  if (!contents || !contents.document || typeof contents.range !== 'function') return;
  const rec = _boxPaneFor(contents);
  if (!rec) return;
  while (rec.svg.firstChild) rec.svg.removeChild(rec.svg.firstChild);
  for (const ann of Object.values(_collections)) {
    if (!ann || !ann.cfi_range) continue;
    if (_cfiSpineIndex(ann.cfi_range) !== contents.sectionIndex) continue;
    if (_annStyle(ann) === 'box') _drawBoxRect(contents, rec.svg, ann);
    else _drawUnderlineLines(contents, rec.svg, ann.cfi_range, 'bc-underline',
                             () => showCollectEditBar(ann), ann.cfi_range);
  }
  // 批注预览线：点开角标时临时画在被批注的文字下
  if (_underlineCfi && _cfiSpineIndex(_underlineCfi) === contents.sectionIndex) {
    const coll = _collections[_underlineCfi];
    if (!coll || _annStyle(coll) === 'box') {
      _drawUnderlineLines(contents, rec.svg, _underlineCfi, 'bc-ann-underline', null, '');
    }
  }
}

function _redrawMarksAll() {
  const rnd = getRendition();
  if (!rnd) return;
  let list = [];
  try { list = rnd.getContents() || []; } catch (_) { return; }
  for (const c of list) _redrawMarksInContents(c);
}

// 跨块的 Range，Chrome 的 getClientRects() 会给出「整段块框」（一行短段也
// 被撑到整栏宽）。逐文本节点切成子 range 再取行框，就能拿到紧贴文字的那一行。
function _lineRectsOfRange(range) {
  const doc = range && range.startContainer && range.startContainer.ownerDocument;
  if (!doc || !doc.body) return [];
  const out = [];
  const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = node.nodeValue || '';
    if (!text.trim()) continue;
    let touches = false;
    try { touches = range.intersectsNode(node); } catch (_) { touches = false; }
    if (!touches) continue;
    const s = node === range.startContainer ? range.startOffset : 0;
    const e = node === range.endContainer ? range.endOffset : text.length;
    if (e <= s) continue;
    const r = doc.createRange();
    r.setStart(node, s);
    r.setEnd(node, e);
    let rects = [];
    try { rects = Array.from(r.getClientRects() || []); } catch (_) { continue; }
    for (const rect of rects) {
      if (rect && rect.width > 0.5 && rect.height > 0.5) out.push(rect);
    }
  }
  return out;
}

// 下划线：每行一条线；命中线是透明的宽描边，点线 = 走收藏编辑态
function _drawUnderlineLines(contents, svg, cfi, cls, onClick, dataCfi) {
  let range = null;
  try { range = contents.range(cfi); } catch (_) { return null; }
  if (!range) return null;
  const rects = _lineRectsOfRange(range);
  if (!rects.length) return null;
  const g = document.createElementNS(_BOX_SVG_NS, 'g');
  g.setAttribute('class', cls);
  if (dataCfi) g.setAttribute('data-cfi', dataCfi);
  for (const r of rects) {
    const y = r.bottom - 1;
    const line = document.createElementNS(_BOX_SVG_NS, 'line');
    line.setAttribute('class', 'bc-line');
    line.setAttribute('x1', r.left); line.setAttribute('x2', r.right);
    line.setAttribute('y1', y); line.setAttribute('y2', y);
    g.appendChild(line);
    if (onClick) {
      const hit = document.createElementNS(_BOX_SVG_NS, 'line');
      hit.setAttribute('class', 'bc-line-hit');
      hit.setAttribute('x1', r.left); hit.setAttribute('x2', r.right);
      hit.setAttribute('y1', y); hit.setAttribute('y2', y);
      g.appendChild(hit);
    }
  }
  if (onClick) g.addEventListener('click', e => { e.stopPropagation(); onClick(); });
  svg.appendChild(g);
  return g;
}

function _drawBoxRect(contents, svg, ann) {
  let range = null;
  try { range = contents.range(ann.cfi_range); } catch (_) { return; }
  if (!range) return;
  let rects = [];
  try { rects = Array.from(range.getClientRects() || []); } catch (_) { return; }
  rects = rects.filter(r => r && r.width > 0.5 && r.height > 0.5);
  if (!rects.length) return;
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  for (const r of rects) {
    left = Math.min(left, r.left);
    top = Math.min(top, r.top);
    right = Math.max(right, r.right);
    bottom = Math.max(bottom, r.bottom);
  }
  // 留一点呼吸位，别让文字贴着边框；跨段时中缝的空白也一并圈进去
  left -= 4; right += 4; top -= 3; bottom += 3;
  const w = right - left, h = bottom - top;
  if (!(w > 0 && h > 0)) return;

  const g = document.createElementNS(_BOX_SVG_NS, 'g');
  g.setAttribute('class', 'bc-box-group');
  g.setAttribute('data-cfi', ann.cfi_range);
  // 命中区：只有边框附近可点（pointer-events:stroke + 透明宽描边），
  // 框内正文保持可选中——里面还要能划词引用/复制。
  const hit = document.createElementNS(_BOX_SVG_NS, 'rect');
  hit.setAttribute('class', 'bc-box-hit');
  hit.setAttribute('x', left); hit.setAttribute('y', top);
  hit.setAttribute('width', w); hit.setAttribute('height', h);
  hit.setAttribute('rx', 5); hit.setAttribute('ry', 5);
  hit.addEventListener('click', e => { e.stopPropagation(); showCollectEditBar(ann); });

  const box = document.createElementNS(_BOX_SVG_NS, 'rect');
  box.setAttribute('class', 'bc-box');
  box.setAttribute('x', left); box.setAttribute('y', top);
  box.setAttribute('width', w); box.setAttribute('height', h);
  box.setAttribute('rx', 5); box.setAttribute('ry', 5);

  g.appendChild(hit);
  g.appendChild(box);
  svg.appendChild(g);
}

/* __BLOCK14__ */

// 全量重放入口（_loadBookAnnotations 尾部 + rendered 均走单 contents 版本）
function _replayCommentsAllContents() {
  const rnd = getRendition();
  if (!rnd) return;
  try { for (const c of rnd.getContents() || []) _replayCommentsInContents(c); }
  catch (_) { /* best-effort */ }
}

// 批注重放：对本 contents 文档逐组解析 CFI 插 marker。
// 两个实测前提：
//  1) fork 的 book.getRange 返回孤儿 document 上的 Range（与 view 渲染的
//     contents.document 永远 MISMATCH）——必须用 contents.range(cfi) 在
//     view 自己的文档内解析。
//  2) continuous 视口轮换会销毁重建 iframe，任何"账本"都会与真实状态
//     脱节（插上→被清→账仍在→永不恢复，实测）。幂等以 DOM 为真值源：
//     每 rendered 实查 marker 是否存活，缺了才插。
function _replayCommentsInContents(contents) {
  if (!contents || !contents.document || !contents.range) return;
  // 每个渲染出来的 section 都挂上「点空白收气泡」：气泡可能从 A 段开出来，
  // 而用户在下一次点击落在 B 段的 iframe 里——那边没挂就收不掉。
  _armDismissOnDocPointer(contents);
  _suppressNativeMenu(contents);
  for (const [cfi] of _commentGroups) {
    if (_cfiSpineIndex(cfi) !== contents.sectionIndex) continue;   // 防跨 section 误命中
    if (contents.document.querySelector(`.bc-ann-cmt[data-cfi="${cfi}"]`)) continue;
    try {
      const range = contents.range(cfi);
      if (!range) continue;
      const fresh = _renderGroupMarker(cfi, range);
      // 气泡正开着这一组：新角标直接接管，翻上 + 成为跟随锚
      if (fresh && _bubbleCfi === cfi) {
        const bubble = document.getElementById('bcNoteBubble');
        if (bubble && bubble.style.display === 'block') {
          _openMarker = fresh;
          _setMarkerOpen(fresh, true);
        }
      }
    } catch (e) { console.warn('[ann-replay] 解析失败', cfi, e); }
  }
  // 古早批注没有 CFI，只能在这一段渲染出来之后按原文找位置。
  _resolveLegacyInContents(contents);
}

// ── 古早批注：没有 CFI，就按原文现找一个 ────────────────────────────────
// v16 之前存的是 selected_text + (chapter, chunk)，没有 cfi_range，回放时
// 只能整条跳过——正文里既没有角标也没有下划线，只剩列表里那一条。这里在
// 渲染出来的 section 里把那段字找回来，用 contents.cfiFromRange 现算一个
// CFI，之后就走和新批注完全相同的一条路（marker / 预览下划线 / 删除）。
// 不回写数据库：每次加载重算一次文本扫描，代价可忽略；书重导入改过原文
// 时自然就找不到，也不会留下一条错误的坐标。

function _findTextRange(doc, ann) {
  const needle = String(ann.selected_text || '').trim();
  if (!needle || !doc || !doc.createTreeWalker) return null;
  const before = String(ann.context_before || '');
  const after = String(ann.context_after || '');
  const hits = [];
  const walker = doc.createTreeWalker(doc.body || doc.documentElement,
                                      NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode()) && hits.length < 40) {
    const text = node.nodeValue || '';
    let at = text.indexOf(needle);
    while (at >= 0 && hits.length < 40) {
      const ctxOk = (!before || text.slice(Math.max(0, at - before.length), at)
        .endsWith(before))
        && (!after || text.slice(at + needle.length).startsWith(after));
      hits.push({ node, at, ctxOk });
      at = text.indexOf(needle, at + 1);
    }
  }
  if (!hits.length) return null;
  // 优先上下文对得上的那处；只有一处命中才认；同文多处又没上下文就不猜。
  const pick = hits.find(h => h.ctxOk) || (hits.length === 1 ? hits[0] : null);
  if (!pick) return null;
  const range = doc.createRange();
  range.setStart(pick.node, pick.at);
  range.setEnd(pick.node, pick.at + needle.length);
  return range;
}

function _resolveLegacyInContents(contents) {
  if (!_legacyAnns.length || !contents || !contents.document) return;
  if (typeof contents.cfiFromRange !== 'function') return;
  for (const ann of _legacyAnns) {
    if (ann.cfi_range) continue;              // 已经解析过，走常规重放
    const range = _findTextRange(contents.document, ann);
    if (!range) continue;
    let cfi = '';
    try { cfi = contents.cfiFromRange(range) || ''; } catch (_) { continue; }
    if (!cfi) continue;
    ann.cfi_range = cfi;
    const asNote = ann.type === 'note'
      || (ann.type === 'both' && !!ann.note_text);
    if (!asNote) {
      _collections[cfi] = ann;
      _redrawMarksInContents(contents);
      continue;
    }
    const list = _commentGroups.get(cfi) || [];
    list.push(ann);
    _commentGroups.set(cfi, list);
    if (!contents.document.querySelector(`.bc-ann-cmt[data-cfi="${cfi}"]`)) {
      _renderGroupMarker(cfi, range);
    }
  }
}

// 角标节点还挂在「当前活着的 iframe」里吗？section 重建后旧节点仍留在自己
// 那个已经脱挂的 document 里，单看 isConnected 认不出来。
function _markerLive(m) {
  if (!m || !m.isConnected) return false;
  const win = m.ownerDocument && m.ownerDocument.defaultView;
  const frame = win && win.frameElement;
  return !!(frame && frame.isConnected && frame.ownerDocument === document);
}

// 收藏层重放：线/框都画在自绘 svg 上，view 重建后跟着 section 重画。
function _replayMarksInContents(contents) {
  _redrawMarksInContents(contents);
}

/* __BLOCK15__ */

// ── Geometry & helpers ───────────────────────────────────────────────

// iframe 内 Range → 主文档视口 rect（iframe 内容不滚动，视口坐标=文档坐标）
function _rangeToViewportRect(range, contents) {
  try {
    const inner = range.getBoundingClientRect();
    if (!inner || (!inner.width && !inner.height)) return null;
    const frame = contents.window && contents.window.frameElement;
    if (!frame) return null;
    const fr = frame.getBoundingClientRect();
    return {
      top: fr.top + inner.top, bottom: fr.top + inner.bottom,
      left: fr.left + inner.left, right: fr.left + inner.right,
      width: inner.width, height: inner.height,
    };
  } catch (_) { return null; }
}

function _elementToViewportRect(el) {
  try {
    const inner = el.getBoundingClientRect();
    const frame = el.ownerDocument.defaultView &&
      el.ownerDocument.defaultView.frameElement;
    if (!frame) return null;
    const fr = frame.getBoundingClientRect();
    return {
      top: fr.top + inner.top, bottom: fr.top + inner.bottom,
      left: fr.left + inner.left, right: fr.left + inner.right,
      width: inner.width, height: inner.height,
    };
  } catch (_) { return null; }
}

/* __BLOCK16__ */

function _placeAbove(el, rect) {
  el.style.left = '0px';
  el.style.transform = 'none';
  const w = el.offsetWidth || 200;
  const left = Math.max(8, Math.min(rect.right - w / 2, window.innerWidth - w - 8));
  el.style.left = left + 'px';
  const h = el.offsetHeight || 36;
  if (rect.top - h - 8 > 0) el.style.top = (rect.top - h - 8) + 'px';   // 选区上方
  else el.style.top = (rect.bottom + 8) + 'px';                          // 顶部放不下 → 下方
}

function _placeBelow(el, rect) {
  el.style.left = '0px';
  el.style.transform = 'none';
  const w = el.offsetWidth || 280;
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - w - 8));
  el.style.left = left + 'px';
  const h = el.offsetHeight || 140;
  if (rect.bottom + h + 8 < window.innerHeight) el.style.top = (rect.bottom + 8) + 'px';
  else if (rect.top - h - 8 > 0) el.style.top = (rect.top - h - 8) + 'px';
  else el.style.top = '8px';
}

/* __BLOCK17__ */

function _closestMark(range, selector) {
  try {
    const el = range.startContainer.parentElement;
    return el && el.closest ? el.closest(selector) : null;
  } catch (_) { return null; }
}

// POST body 公共部分：语义定位走 /api/sync 桥维护的 (chapter, chunk)
function _annotationPayload(extra) {
  let ctxBefore = '', ctxAfter = '';
  try {
    const node = _sel.range.startContainer;
    if (node.nodeType === Node.TEXT_NODE) {
      ctxBefore = (node.textContent || '').slice(
        Math.max(0, _sel.range.startOffset - 40), _sel.range.startOffset);
      ctxAfter = (node.textContent || '').slice(
        _sel.range.endOffset, Math.min(node.textContent.length, _sel.range.endOffset + 40));
    }
  } catch (_) { /* context 缺失不阻塞 */ }
  return {
    book_id: S.currentBook,
    chapter_index: S.currentChapter,
    chunk_index: S.currentChunk,
    selected_text: _sel.text,
    context_before: ctxBefore,
    context_after: ctxAfter,
    ...extra,
  };
}

/* __BLOCK18__ */

// 正文区域屏蔽浏览器原生右键菜单。
// Edge 的 mini menu（复制 / 搜索 / 更多操作 / 隐藏菜单）会在选区上直接弹出来，
// 位置正好盖住我们自己的浮动条——阅读页的右键动作已由浮动条接管（收藏/批注/
// 引用/复制），原生菜单在这里只会添乱。只作用于书页正文，聊天气泡、笔记、
// 写作页等其它区域的右键不受影响。
function _suppressNativeMenu(contents) {
  const doc = contents && contents.document;
  if (!doc || _menuBlockedDocs.has(doc)) return;
  _menuBlockedDocs.add(doc);
  doc.addEventListener('contextmenu', e => {
    e.preventDefault();
  }, { capture: true });
}

// 点击 iframe 空白收浮动条 / 收起气泡（WeakSet 去重——同一 doc 只挂一次）
function _armDismissOnDocPointer(contents) {
  const doc = contents && contents.document;
  if (!doc || _commentDocs.has(doc)) return;
  _commentDocs.add(doc);
  doc.addEventListener('pointerdown', (e) => {
    // 点另一个 marker 是「换一个气泡」，不是「点空白」：交给 marker 自己
    // 的 click 处理（showGroupBubble 会收起旧气泡、翻回旧角标）。
    if (e.target && e.target.closest && e.target.closest('.bc-ann-cmt')) return;
    _hideBar(); _hideBubble();
  }, { capture: true });
}

// ── Init ─────────────────────────────────────────────────────────────

onBookReady(async () => {
  if (!isNative()) return;
  bindSelectionHook();          // rendition 换书后重挂（on 是 per-rendition 实例）
  await _loadBookAnnotations();
});

onSectionRendered(contents => {
  _replayCommentsInContents(contents);
  _replayMarksInContents(contents);
});

// 主文档侧：正文容器（chunk 版书页 + native 宿主）同样不弹原生菜单；
// iframe 里的正文由 _suppressNativeMenu 在自己的 document 上拦。
document.addEventListener('contextmenu', e => {
  const t = e.target;
  if (t && t.closest && t.closest('#bookPage, #bookPageNative')) e.preventDefault();
}, { capture: true });

// 主文档侧关闭：滚动（capture 收宿主 scroller）、Esc、点击主文档空穈。
// 滚动只收浮动条，不收气泡：气泡是「读批注」的浮层，滚一点看看上下文是
// 常态；让它跟着角标走，角标滚出视口（锚没了）再收。
let _bubbleFollowRaf = 0;
function _followOpenBubble() {
  if (_bubbleFollowRaf) return;
  _bubbleFollowRaf = requestAnimationFrame(() => {
    _bubbleFollowRaf = 0;
    const bubble = document.getElementById('bcNoteBubble');
    if (!bubble || bubble.style.display !== 'block' || !_openMarker) return;
    let marker = _openMarker;
    // section 重渲染会把角标元素整个换掉（continuous 轮换 / 面板开合 / 长滚动后
    // 的卸载重建）：旧节点还在自己的老 document 里，isConnected 仍是 true，
    // 但它的 iframe 已经不在主文档。认组（`_bubbleCfi`）不认节点。
    if (!_markerLive(marker)) {
      const live = _bubbleCfi ? _findGroupMarker(_bubbleCfi) : null;
      if (live) {
        _openMarker = live;
        _setMarkerOpen(live, true);
        marker = live;
      } else {
        return;          // 这一段暂时没渲染出来：气泡停在原处，等它回来再跟
      }
    }
    const rect = _elementToViewportRect(marker);
    // iframe 正在重建时拿到的是全零矩形（老 frame 已经脱离文档），下一帧再说
    if (!rect || (!rect.width && !rect.height)) return;
    // 角标滚出视口：气泡停在最后一处，**不收起**。收起的入口只有
    // 「点空白 / 再点角标 / Esc」——滚动是阅读动作，不是关闭动作。
    if (rect.bottom <= 8 || rect.top >= window.innerHeight - 8) return;
    _placeBelow(bubble, rect);
  });
}

document.addEventListener('scroll', () => { _hideBar(); _followOpenBubble(); },
  { capture: true, passive: true });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { _hideBar(); _hideBubble(); closeNotePanel(); }
});
document.addEventListener('mousedown', e => {
  if (e.target.closest('#bcSelBar') || e.target.closest('#bcNotePanel')
    || e.target.closest('#bcNoteBubble')) return;
  _hideBar(); _hideBubble();
});

/* __BLOCK19__ */

// 别处（笔记页 / 浮窗）删掉一条批注后同步正文层：线、角标、气泡立刻收掉，
// 不必等下一次 section 重渲染。
window.bcAnnotationDeleted = function (annId) {
  for (const [cfi, rec] of Object.entries(_collections)) {
    if (!rec || rec.id !== annId) continue;
    delete _collections[cfi];
    break;
  }
  for (const [cfi, recs] of _commentGroups) {
    const idx = recs.findIndex(r => r.id === annId);
    if (idx < 0) continue;
    recs.splice(idx, 1);
    if (!recs.length) { _commentGroups.delete(cfi); _removeGroupMarker(cfi); }
    else _refreshGroupBubble(cfi);
    break;
  }
  const legacyIdx = _legacyAnns.findIndex(r => r.id === annId);
  if (legacyIdx >= 0) _legacyAnns.splice(legacyIdx, 1);
  _redrawMarksAll();       // 收藏层不在 epub.js 账本里，按 _collections 重画
};

// 外部模块（notes-panel 跳转）经动态 import 复用 CFI 跳转
window.bcAnnotationJump = async function (cfi) {
  const { jumpToCfi } = await import('./js/epub-reader.js');
  return jumpToCfi(cfi);
};


















