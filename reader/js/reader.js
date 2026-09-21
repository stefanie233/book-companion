/* Book Companion — reader: book selection, chunk loading, navigation. */

import { S } from './state.js';
import { escapeHtml } from './utils.js';
import { addChat } from './chat.js';
import { loadSessionList } from './sessions.js';
import { loadSettings } from './theme.js';
import * as EpubReader from './epub-reader.js';

let _currentBookTitle = '';  // cached for context bar updates in loadChunk

// ── Wheel-to-page (damped) ───────────────────────────────────────
// 滚到边界时界面停住,继续滚累积 deltaY,达到阈值才翻页。
// 阈值 = N 格(1 格 ≈ 100px deltaY),N 来自主题设置"翻页阻尼"。
// 用 wheel 而非 scroll:到底后 scrollTop 不再变化,scroll 事件无法累积,
// 短内容 / 边界后都翻不动。wheel 事件在边界后仍持续触发,可累积。

let _wheelAccum = 0;          // 边界处累积的 deltaY(向下为正)
let _lastWheelTime = 0;       // 上次 wheel 事件时间(判断手势连续性)
let _pageTurnDir = 0;         // 翻页后的连翻方向: +1=向下, -1=向上, 0=无
const _WHEEL_TICK_PX = 100;   // 1 格 ≈ 100px(Chrome/Windows 鼠标滚轮默认)
const _GESTURE_GAP_MS = 200;  // 两次 wheel 间隔超过此值视为手势停顿

function _getPageTurnThreshold() {
  try {
    var ticks = parseInt(loadSettings().pageTurnThreshold) || 2;
    return ticks * _WHEEL_TICK_PX;
  } catch (_) { return 2 * _WHEEL_TICK_PX; }
}

// 连续手势连翻,停顿恢复:
// 翻页后进入连翻状态(_pageTurnDir 记录方向),只要滚轮手势没停
// (间隔<_GESTURE_GAP_MS)且方向一致,继续滚可直接翻页,不要求滚到边界;
// 手势一停顿或反向,清除连翻状态,恢复"先滚到边界再翻"。
// 解决:向下翻页后 scrollTop 归零、atBottom=false,连续滚动被静默的问题。
function _onBookPageWheel(e) {
  // 原生渲染模式下 epub.js 接管滚动（#bookPage 已隐藏，双保险）
  if (S.renderMode === 'native') return;
  var el = document.getElementById('bookPage');
  if (!el) return;

  var now = Date.now();
  var gestureContinued = (now - _lastWheelTime) < _GESTURE_GAP_MS;
  _lastWheelTime = now;

  var slack = 4;
  var atBottom = (el.scrollHeight - el.clientHeight <= slack)
    || (el.scrollTop + el.clientHeight >= el.scrollHeight - slack);
  var atTop = el.scrollTop <= slack;
  var dir = e.deltaY > 0 ? 1 : (e.deltaY < 0 ? -1 : 0);

  // 边界翻页 OR 连翻(同向且手势连续)
  var canTurn = (dir > 0 && atBottom) || (dir < 0 && atTop)
    || (_pageTurnDir === dir && gestureContinued);

  if (canTurn) {
    _wheelAccum += Math.abs(e.deltaY);
    if (_wheelAccum >= _getPageTurnThreshold()) {
      _wheelAccum = 0;
      _pageTurnDir = dir;  // 记录连翻方向
      if (dir > 0) nextChunk(); else prevChunk();
    }
  } else {
    _wheelAccum = 0;
    // 反向或手势停顿,清除连翻状态
    if (_pageTurnDir !== dir || !gestureContinued) _pageTurnDir = 0;
  }
}

function _installScrollPagination() {
  var el = document.getElementById('bookPage');
  if (!el) return;
  _wheelAccum = 0;
  // 内容超过一屏 -> 需要浏览,清除连翻(避免长内容页面被连翻跳过)
  // 内容一屏装下 -> 保留连翻(短页面可连续翻)
  if (el.scrollHeight > el.clientHeight + 4) {
    _pageTurnDir = 0;
    _lastWheelTime = 0;
  }
  el.removeEventListener('wheel', _onBookPageWheel);
  el.addEventListener('wheel', _onBookPageWheel, { passive: true });
}

let _selectSeq = 0;   // selectBook 防重入：并发切书时旧响应直接作废

export async function selectBook() {
  const sel = document.getElementById('bookSelect');
  if (!sel.value) return;
  const seq = ++_selectSeq;
  // Compare against the last *successfully loaded* book, not currentBook —
  // openBookFromShelf / restoreViewState pre-set currentBook, which would
  // otherwise make bookChanged always false (stale session leaks across books).
  const bookChanged = !S._lastLoadedBook || S._lastLoadedBook !== sel.value;
  S.currentBook = sel.value;
  try {
    const resp = await fetch(S.API + '/api/books/' + S.currentBook + '/context');
    if (seq !== _selectSeq) return;      // 已有更新的切书请求，本次作废
    const ctx = await resp.json();
    if (ctx.error) { addChat('agent', ctx.error); return; }
    if (seq !== _selectSeq) return;

    const all = [...ctx.structure.front_matter, ...ctx.structure.body, ...ctx.structure.back_matter];
    const chSel = document.getElementById('chapterSelect');
    chSel.innerHTML = all.map(c =>
      `<option value="${c.chapter_index}">${c.type==='body'?'':'['+c.type+']'} ${(c.title||'Ch.'+c.chapter_index).slice(0,30)} (${c.chunks}段)</option>`
    ).join('');

    // Native EPUB render for epub books; anything else (MOBI/txt, or a
    // native-render failure — initNative returns false) falls back to the
    // chunk view. The chunk/context loading below is the fallback path.
    const isEpub = ctx.book && String(ctx.book.format || '').toLowerCase() === 'epub';
    if (isEpub && await EpubReader.initNative(S.currentBook, ctx.reading_progress)) {
      if (seq !== _selectSeq) { EpubReader.destroyNative(); return; }
      S.renderMode = 'native';
    } else {
      if (seq !== _selectSeq) return;
      S.renderMode = 'chunk';
      if (ctx.reading_progress) {
        S.currentChapter = ctx.reading_progress.chapter;
        S.currentChunk = ctx.reading_progress.chunk;
        chSel.value = S.currentChapter;
        document.getElementById('chunkPos').textContent = (S.currentChunk + 1) + ' / ...';
        loadChunk();
      } else {
        selectChapter();
      }
    }
    // When switching books: clear chat, reset session, start fresh.
    // Same-book restore (e.g. from view-state) keeps the session alive.
    if (bookChanged) {
      window.clearChatLog && window.clearChatLog();
      S.chatHistory = [];
      S.currentSessionId = '';
      loadSessionList();
    }
    // Only a *successful* load updates _lastLoadedBook — a failed fetch
    // leaves it at the previous book so the next attempt still detects the switch.
    if (seq !== _selectSeq) return;      // 状态落盘前最后一道校验
    S._lastLoadedBook = sel.value;
    // Update context bar — replaces the old addChat('打开了...') chat bubble.
    _currentBookTitle = ctx.book.title || '';
    window.updateContextBar && window.updateContextBar(_currentBookTitle, null);
    prefetchGraphForBook(S.currentBook);
  } catch (e) { console.error(e); }
}

// ── Fire-and-forget graph prefetch ──────────────────────────────
// Called after selectBook() succeeds. If this book has a knowledge
// graph, preloads the full graph data so switching to the "图谱"
// tab is instant.

async function prefetchGraphForBook(bookId) {
  if (!bookId || S.graphPrefetchPending[bookId] || S.graphPrefetchCache[bookId]) return;
  S.graphPrefetchPending[bookId] = true;
  try {
    const statsResp = await fetch(S.API + '/api/graph/stats/' + bookId);
    const statsData = await statsResp.json();
    if (!statsData.stats || statsData.stats.nodes === 0) return;
    const graphResp = await fetch(S.API + '/api/graph/' + bookId + '/full?limit=500');
    S.graphPrefetchCache[bookId] = await graphResp.json();
  } catch (_) { /* silent — graph tab will fetch on demand */ }
  finally { delete S.graphPrefetchPending[bookId]; }
}

export function selectChapter() {
  S.currentChapter = parseInt(document.getElementById('chapterSelect').value) || 0;
  S.currentChunk = 0;
  document.getElementById('chunkPos').textContent = '1 / ...';
  if (S.renderMode === 'native') {
    EpubReader.jumpToChapter(S.currentChapter);
    return;
  }
  loadChunk();
}

let _chunkSeq = 0;    // loadChunk 防重入：快速翻章/切书时旧章节响应作废

export async function loadChunk() {
  const pageInner = document.getElementById('bookPageInner');
  const page = document.getElementById('bookPage');
  const seq = ++_chunkSeq;
  pageInner.innerHTML = '加载中...';
  try {
    const resp = await fetch(
      // record=1：chunk 视图的翻页是用户真实阅读行为，进度落库；
      // epub-reader.js 的 anchor 请求不带此参数（skipRecord 语义）
      S.API + '/api/books/' + S.currentBook + '/chunk?chapter=' + S.currentChapter + '&chunk=' + S.currentChunk + '&record=1'
    );
    if (seq !== _chunkSeq) return;       // 旧响应不覆盖新内容
    const data = await resp.json();
    if (seq !== _chunkSeq) return;
    if (data.error) { pageInner.textContent = data.error; return; }
    pageInner.innerHTML = escapeHtml(data.content);
    renderHighlightsInDOM(page, data);
    S.totalChunks = data.total_chunks || 1;
    S.prevAnchor = data.prev_anchor;
    S.nextAnchor = data.next_anchor;
    S.currentChunk = data.chunk_index;
    document.getElementById('chunkPos').textContent = (S.currentChunk + 1) + ' / ' + S.totalChunks;
    updateMeta(data);
    // Update context bar with current chapter name
    if (window.updateContextBar && _currentBookTitle) {
      var chName = (document.getElementById('chapterSelect').selectedOptions[0] || {}).text || '';
      window.updateContextBar(_currentBookTitle, chName);
    }
    // Reset scroll position + install scroll-to-page listener
    page.scrollTop = 0;
    _installScrollPagination();
  } catch (e) { if (seq === _chunkSeq) page.textContent = '加载失败 — ' + e.message; }
}

function updateMeta(data) {
  const meta = document.getElementById('positionMeta');
  meta.textContent = `第 ${S.currentChapter} 章 · 段落 ${data.chunk_index + 1}/${data.total_chunks} · 共 ${data.char_count || 0} 字`;
}

export function prevChunk() {
  // 原生模式：连续滚动无 chunk 页，前后导航 = 上下章
  if (S.renderMode === 'native') { EpubReader.jumpToChapter(S.currentChapter - 1); return; }
  if (S.prevAnchor && (S.prevAnchor.chapter !== S.currentChapter || S.prevAnchor.chunk !== S.currentChunk)) {
    if (S.prevAnchor.chapter !== S.currentChapter) {
      S.currentChapter = S.prevAnchor.chapter;
      document.getElementById('chapterSelect').value = S.currentChapter;
    }
    S.currentChunk = S.prevAnchor.chunk;
    document.getElementById('chunkPos').textContent = (S.currentChunk + 1) + ' / ...';
    loadChunk();
  }
}

export function nextChunk() {
  // 原生模式：连续滚动无 chunk 页，前后导航 = 上下章
  if (S.renderMode === 'native') { EpubReader.jumpToChapter(S.currentChapter + 1); return; }
  if (S.nextAnchor && (S.nextAnchor.chapter !== S.currentChapter || S.nextAnchor.chunk !== S.currentChunk)) {
    if (S.nextAnchor.chapter !== S.currentChapter) {
      S.currentChapter = S.nextAnchor.chapter;
      document.getElementById('chapterSelect').value = S.currentChapter;
    }
    S.currentChunk = S.nextAnchor.chunk;
    document.getElementById('chunkPos').textContent = (S.currentChunk + 1) + ' / ...';
    loadChunk();
  }
}

// ── Placeholder for annotation integration ──────────────────────
// renderHighlightsInDOM is called by loadChunk() after content loads.
// If annotations.js is loaded, it overrides this stub.

export function renderHighlightsInDOM(pageEl, chunkData) {
  if (typeof window.applyHighlights === 'function') window.applyHighlights(pageEl, chunkData);
}

// ── Keyboard navigation ─────────────────────────────────────────
document.addEventListener('keydown', function (e) {
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft') prevChunk();
  if (e.key === 'ArrowRight') nextChunk();
});

// ── HTML onclick access ─────────────────────────────────────────
window.selectBook = selectBook;
window.selectChapter = selectChapter;
window.prevChunk = prevChunk;
window.nextChunk = nextChunk;
window.loadChunk = loadChunk;
