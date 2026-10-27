/* Book Companion Reader — native annotation layer (收藏/批注二分版).

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
let _openMarker = null;            // 当前展开气泡的 marker（翻态锚点，至多一个）
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
let _registeredCfis = new Set();   // 本 rendition 已注册的收藏 cfi（防双注册 → 双线叠加）
let _regRendition = null;          // _registeredCfis 所属 rendition（换书自动重置）

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
      if (btn) _onBarAction(btn.dataset.act);
    });
  }
  return bar;
}

function _showBar(rect, mode, sel) {
  const bar = _getBar();
  const inCollect = mode === 'collect-edit';
  bar.innerHTML = inCollect
    ? `<button data-act="uncollect"><i data-lucide="x"></i> 取消收藏</button>`
    : `<button data-act="collect"><i data-lucide="bookmark"></i> 收藏</button>
       <button data-act="comment"><i data-lucide="message-square"></i> 批注</button>
       <button data-act="quote"><i data-lucide="pin"></i> 引用</button>
       <button data-act="copy"><i data-lucide="copy"></i> 复制</button>`;
  bar.style.display = 'flex';
  _placeAbove(bar, rect);
  if (window.lucide) window.lucide.createIcons();
}

/* __BLOCK5__ */

function _hideBar() {
  const bar = document.getElementById('bcSelBar');
  if (bar) bar.style.display = 'none';
}

function _onBarAction(act) {
  if (act === 'uncollect-ann') { _uncollectByCfi(_getBar().dataset.cfi); return; }
  if (!_sel) return;
  if (act === 'collect') createCollection();
  else if (act === 'uncollect') _uncollectAtSelection();
  else if (act === 'comment') {
    // 聚合制：同段已有批注 → 直接展开组气泡（浏览 + 新增），不另起组
    const existing = _commentGroups.get(_sel.cfi);
    const marker = existing && existing.length
      ? _findGroupMarker(_sel.cfi) : null;
    if (marker) { showGroupBubble(_sel.cfi, marker); _hideBar(); }
    else openNotePanel(null);
  }
  else if (act === 'quote') quoteToChat();
  else if (act === 'copy') copySelection();
}

// ── Bar actions ─────────────────────────────────────────────────────

async function createCollection() {
  if (!_sel || !S.currentBook) return;
  const body = _annotationPayload({
    type: 'highlight', style: 'underline', cfi_range: _sel.cfi,
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
      // fork 的 annotations.add 自带立即 attach（views().forEach 同 section 即画）
      _registerUnderline(rec);
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
      const rnd = getRendition();
      if (rnd && rnd.annotations) {
        try { rnd.annotations.remove(cfi, 'underline'); } catch (_) { /* 已不在 */ }
      }
      _registeredCfis.delete(cfi);
      delete _collections[cfi];
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
  marker.addEventListener('click', e => { e.stopPropagation(); showGroupBubble(cfi, marker); });
  return marker;
}

// 组气泡：原文引用（组内首条）+ 按条呈现 + 新增入口
function showGroupBubble(cfi, marker) {
  const recs = _commentGroups.get(cfi) || [];
  const bubble = _getBubble();
  bubble.innerHTML = `
    <div class="bc-bubble-quote"></div>
    <div class="bc-bubble-list"></div>
    <button data-act="add" class="bc-bubble-add">+ 新增批注</button>`;
  bubble.querySelector('.bc-bubble-quote').textContent = recs[0] ? recs[0].selected_text : '';
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
  bubble.onclick = e => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'add') {
      _pendingGroupCfi = cfi;
      _hideBubble();
      openNotePanel(null, rect);
    } else if (btn.dataset.act === 'edit') {
      const rec = recs.find(r => r.id === btn.dataset.id);
      if (rec) { _hideBubble(); openNotePanel(rec, rect); }
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

function _hideBubble() {
  const bubble = document.getElementById('bcNoteBubble');
  if (bubble) bubble.style.display = 'none';
  if (_openMarker) _setMarkerOpen(_openMarker, false);   // 气泡收起 → marker 翻回
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
      if (!ann.cfi_range) continue;   // 旧记录无 CFI：仅列表可见，不回放
      if (ann.type === 'highlight' && rnd && rnd.annotations) {
        _collections[ann.cfi_range] = ann;
        _registerUnderline(ann);
      } else if (ann.type === 'note') {
        const list = _commentGroups.get(ann.cfi_range) || [];
        list.push(ann);
        _commentGroups.set(ann.cfi_range, list);
      }
    }
  } catch (e) { console.error('标注加载失败:', e); }
  _replayCommentsAllContents();
  // continuous 渐进渲染与 fetch 无时序保证：首次全量重放可能踩在"文档
  // 未就绪（contents.range 返回 null）/view 尚未渲染"的空档上，而之后
  // 没有滚动就没有 rendered 再触发——按退避节奏分轮补插。DOM 实查幂等，
  // 已插上的组直接跳过。
  const backoff = [500, 1000, 2000, 3000, 5000];
  for (let i = 0; i < backoff.length; i++) {
    setTimeout(() => { _replayCommentsAllContents(); _replayUnderlinesAll(); }, backoff[i]);
  }
}

// 收藏重放全量入口（rendered 版本按 section 过滤；此处兜底全窗口）
function _replayUnderlinesAll() {
  const rnd = getRendition();
  if (!rnd) return;
  try { for (const c of rnd.getContents() || []) _replayUnderlinesInContents(c); }
  catch (_) { /* best-effort */ }
}

/* __BLOCK13__ */

function _registerUnderline(ann) {
  const rnd = getRendition();
  if (!rnd || !rnd.annotations) return;
  if (rnd !== _regRendition) { _registeredCfis = new Set(); _regRendition = rnd; }
  if (_registeredCfis.has(ann.cfi_range)) return;   // 防重：重复注册 = SVG 线叠加变粗
  try {
    // 样式走主文档 .bc-underline line CSS（SVG Pane 渲染在主文档，styles 参数对 line 无效）
    rnd.annotations.add('underline', ann.cfi_range, { id: ann.id },
      () => showCollectEditBar(ann), 'bc-underline');
    _registeredCfis.add(ann.cfi_range);
  } catch (e) { console.warn('收藏注册失败:', e); }
}

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
  for (const [cfi] of _commentGroups) {
    if (_cfiSpineIndex(cfi) !== contents.sectionIndex) continue;   // 防跨 section 误命中
    if (contents.document.querySelector(`.bc-ann-cmt[data-cfi="${cfi}"]`)) continue;
    try {
      const range = contents.range(cfi);
      if (!range) continue;
      _renderGroupMarker(cfi, range);
    } catch (e) { console.warn('[ann-replay] 解析失败', cfi, e); }
  }
}

// 收藏线重放：fork 的 annotations.add 无重挂机制——线随 view 容器销毁。
// rendered 即 view 新建，对本 section 的收藏无条件重注册（旧线已死，
// add 覆盖同 hash 不叠加；_registeredCfis 先放行再补记）。
function _replayUnderlinesInContents(contents) {
  const rnd = getRendition();
  if (!rnd || !rnd.annotations) return;
  for (const ann of Object.values(_collections)) {
    if (_cfiSpineIndex(ann.cfi_range) !== contents.sectionIndex) continue;
    _registeredCfis.delete(ann.cfi_range);
    _registerUnderline(ann);
  }
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

// 点击 iframe 穑白收浮动条（WeakSet 去重——同一 doc 只挂一次）
function _armDismissOnDocPointer(contents) {
  const doc = contents && contents.document;
  if (!doc || _commentDocs.has(doc)) return;
  _commentDocs.add(doc);
  doc.addEventListener('pointerdown', () => {
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
  _replayUnderlinesInContents(contents);
});

// 主文档侧关闭：滚动（capture 收宿主 scroller）、Esc、点击主文档空穈
document.addEventListener('scroll', () => { _hideBar(); _hideBubble(); }, { capture: true, passive: true });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { _hideBar(); _hideBubble(); closeNotePanel(); }
});
document.addEventListener('mousedown', e => {
  if (e.target.closest('#bcSelBar') || e.target.closest('#bcNotePanel')
    || e.target.closest('#bcNoteBubble')) return;
  _hideBar(); _hideBubble();
});

/* __BLOCK19__ */

// 外部模块（notes-panel 跳转）经动态 import 复用 CFI 跳转
window.bcAnnotationJump = async function (cfi) {
  const { jumpToCfi } = await import('./js/epub-reader.js');
  return jumpToCfi(cfi);
};


















