/* Liubai Reader - notes & bookmarks: independent page + mini floating panel.
 *
 * Two surfaces:
 *   - #page-notes  : full-screen tab (sidebar "📝 笔记"). Notes + bookmarks subtabs,
 *                    search/filter/sort/export. Entry point is switchTab('notes').
 *   - #miniNotesPanel : small floating panel opened from the reader toolbar ("📋 浮窗").
 *                    Shows the current book's notes sorted by chapter, highlights the
 *                    chapter the reader is currently on.
 */

import { S } from './js/state.js';
import { escapeHtml } from './js/utils.js';
import { jumpToPosition } from './js/app.js';
// 左栏书籍顺序复用书架页的排序（同源比较器 + 同一份持久化偏好，
// 笔记页的排序菜单只是这份偏好的另一个入口，两页永远同序）
import { sortBooks, loadShelfSort, setSort as setShelfSort, SORT_KEYS, SORT_LABELS } from './js/shelf.js';

// ── State ────────────────────────────────────────────────────
let notesPanelState = { minimized: false, x: null, y: null };
let miniPanelWasOpen = false;  // set while hidden by tab switch, cleared by manual close
let _miniNotesSeq = 0;         // 浮窗请求序号：换书/重开时作废在途的旧书响应
let bookTitleMap = {};  // stable_book_id -> title (built from /api/books)
let currentSubtab = 'notes';  // 'notes' | 'bookmarks'

// 笔记页的视图现场：切走再回来（点一条笔记去原文，再点侧栏回来）不能把
// 读者扔回顶部——滚动位置、子页签、排序、筛选、搜索词一起记住。
// sessionStorage 而不是内存变量：刷新页面也还在；关掉标签页就清空，符合预期。
const NOTES_VIEW_KEY = 'bc.notes.view';
let notesView = {
  scrollTop: 0,
  subtab: 'notes',
  sort: 'book-asc',
  book: '',
  query: '',
};
try {
  const raw = sessionStorage.getItem(NOTES_VIEW_KEY);
  if (raw) Object.assign(notesView, JSON.parse(raw) || {});
} catch (e) { /* 隐私模式 / 存储被禁：退回纯内存 */ }
/** 旧会话里章节排序存成无方向的 chapter；按当时的行为归到升序。 */
function normalizeNotesSort(mode) {
  if (!mode) return 'book-asc';
  return mode === 'chapter' ? 'chapter-asc' : mode;
}
let notesSortMode = normalizeNotesSort(notesView.sort);
let restoreScrollOnNextRender = false;
let noteById = new Map();  // 当前列表的原始记录（编辑态要拿未渲染的 note_text 回填）
let bookMeta = {};         // stable_book_id -> /api/books 的整行（左栏封面/排序要用）
let railCounts = {};       // stable_book_id -> 当前页签下的条数

function saveNotesView(patch) {
  if (patch) Object.assign(notesView, patch);
  try { sessionStorage.setItem(NOTES_VIEW_KEY, JSON.stringify(notesView)); } catch (e) {}
}

function currentPageScrollTop() {
  const body = document.querySelector('#page-notes .notes-page-body');
  return body ? body.scrollTop : 0;
}

/** 只在「进入笔记页」这一跳里恢复位置；搜索/筛选造成的重渲染不许把读者拽走。 */
function restorePageScroll() {
  if (!restoreScrollOnNextRender) return;
  restoreScrollOnNextRender = false;
  const body = document.querySelector('#page-notes .notes-page-body');
  if (!body) return;
  const y = notesView.scrollTop || 0;
  requestAnimationFrame(() => { body.scrollTop = y; });
}

// ── Mini floating panel (current book notes) ─────────────────

function getOrCreateMiniPanel() {
  let panel = document.getElementById('miniNotesPanel');
  if (panel) return panel;

  panel = document.createElement('div');
  panel.id = 'miniNotesPanel';
  panel.className = 'notes-mini-panel';
  panel.innerHTML = `
    <div class="notes-mini-header" id="miniNotesHeader">
      <span class="notes-mini-title"><i data-lucide="message-square"></i> 本书笔记</span>
      <button onclick="closeNotesPanel()" title="关闭"><i data-lucide="x"></i></button>
    </div>
    <div class="notes-mini-body notes-list" id="miniNotesList">
      <div class="notes-empty">加载中...</div>
    </div>
  `;
  document.body.appendChild(panel);

  // Dragging (viewport-clamped)
  const header = document.getElementById('miniNotesHeader');
  let drag = false, ox, oy;
  header.addEventListener('mousedown', function (e) {
    if (e.target.tagName === 'BUTTON') return;
    drag = true;
    ox = e.clientX - panel.offsetLeft;
    oy = e.clientY - panel.offsetTop;
    header.style.cursor = 'grabbing';
    e.preventDefault();
  });
  document.addEventListener('mousemove', function (e) {
    if (!drag) return;
    let nx = e.clientX - ox;
    let ny = e.clientY - oy;
    const pw = panel.offsetWidth;
    nx = Math.max(-pw + 40, Math.min(window.innerWidth - 40, nx));
    ny = Math.max(0, Math.min(window.innerHeight - 40, ny));
    panel.style.left = nx + 'px';
    panel.style.top = ny + 'px';
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
  });
  document.addEventListener('mouseup', function () {
    if (!drag) return;
    drag = false;
    header.style.cursor = 'move';
  });

  return panel;
}

function openNotesPanel() {
  const panel = getOrCreateMiniPanel();
  panel.style.display = '';
  loadMiniNotes();
}

function closeNotesPanel() {
  const panel = document.getElementById('miniNotesPanel');
  if (panel) panel.style.display = 'none';
  miniPanelWasOpen = false;
}

// Mini panel is a reading-context widget. app.switchTab calls this on every
// tab switch: false hides the fixed-position panel so it doesn't float over
// unrelated pages (e.g. the theme preview column), true restores it if it was
// open before the switch (manual close clears the flag — see closeNotesPanel).
function setMiniPanelVisible(show) {
  const panel = document.getElementById('miniNotesPanel');
  if (!panel) return;
  if (show) {
    if (miniPanelWasOpen) {
      panel.style.display = '';
      loadMiniNotes();  // refresh: book/chapter context may have moved while hidden
    }
  } else if (panel.style.display !== 'none') {
    miniPanelWasOpen = true;
    panel.style.display = 'none';
  }
}

// 换书时浮窗还开着就立刻换成新书的笔记——浮窗是阅读现场的一部分，
// 不能停在上本书的内容上等下一次点击。selectBook() 派发自定义事件，
// 下拉、书架打开、笔记跳转三条路都覆盖（程序化切书没有 DOM change）。
window.addEventListener('bc:book-changed', function () {
  const panel = document.getElementById('miniNotesPanel');
  if (!panel || panel.style.display === 'none') return;
  loadMiniNotes();
});

async function loadMiniNotes() {
  const list = document.getElementById('miniNotesList');
  if (!list) return;
  if (!S.currentBook) {
    _miniNotesSeq++;          // 没有书也要作废在途响应
    list.innerHTML = '<div class="notes-empty">未打开书籍</div>';
    return;
  }
  const seq = ++_miniNotesSeq;
  const bookId = S.currentBook;
  list.innerHTML = '<div class="notes-empty">加载中...</div>';
  try {
    const ctxResp = await fetch(S.API + '/api/books/' + bookId + '/context');
    const ctx = await ctxResp.json();
    if (seq !== _miniNotesSeq || S.currentBook !== bookId) return;
    const sid = ctx.stable_book_id;
    if (!sid) { list.innerHTML = '<div class="notes-empty">无笔记</div>'; return; }

    // 浮窗是「本书笔记」：先按书 + 类型取（正常服务端会同时过滤两个条件），
    // 前端再按稳定书号过一遍。旧进程的 `?type=` 分支曾经忽略 stable_book_id，
    // 把全库笔记倒进来——一旦在响应里看到别的书，就退到 book-scoped 分支
    // 重取（这个分支从很早就按书过滤），再在前端筛笔记。
    const qs = 'stable_book_id=' + encodeURIComponent(sid);
    const resp = await fetch(S.API + '/api/annotations?' + qs + '&type=note&limit=200');
    const data = await resp.json();
    // 已经换书/重开：这一份是上一本书的响应，丢掉，不能写进新书的浮窗。
    if (seq !== _miniNotesSeq || S.currentBook !== bookId) return;
    let rows = data.annotations || [];
    if (rows.some(n => n.stable_book_id && n.stable_book_id !== sid)) {
      const resp2 = await fetch(S.API + '/api/annotations?' + qs + '&limit=200');
      const data2 = await resp2.json();
      if (seq !== _miniNotesSeq || S.currentBook !== bookId) return;
      rows = data2.annotations || [];
    }
    let notes = rows.filter(
      n => (n.type === 'note' || n.type === 'both') && n.stable_book_id === sid
    );
    if (!notes.length) {
      list.innerHTML = '<div class="notes-empty">本书暂无笔记</div>';
      return;
    }
    notes.sort((a, b) =>
      (a.chapter_index || 0) - (b.chapter_index || 0) ||
      (a.chunk_index || 0) - (b.chunk_index || 0)
    );
    const curCh = S.currentChapter || 0;
    list.innerHTML = notes.map(n => {
      const isCurrent = (n.chapter_index || 0) === curCh;
      return `
        <div class="note-card${isCurrent ? ' current' : ''}"
             onclick="jumpToAnnotation('${n.id}','${n.book_id || ''}',${n.chapter_index || 0},${n.chunk_index || 0},'${escapeHtml(n.cfi_range || '')}')">
          <button class="note-del" data-ann-id="${n.id}" title="删除这条批注"><i data-lucide="trash-2"></i></button>
          <div class="note-quote">${escapeHtml((n.selected_text || '').slice(0, 60))}${(n.selected_text || '').length > 60 ? '...' : ''}</div>
          ${n.note_text ? `<div class="note-text">${renderNoteMarkdown(n.note_text)}</div>` : ''}
          <div class="note-meta">第${(n.chapter_index || 0) + 1}章${isCurrent ? ' · 当前' : ''}</div>
        </div>
      `;
    }).join('');
  } catch (e) {
    list.innerHTML = '<div class="notes-empty" style="color:#ff6b6b">加载失败</div>';
  }
}

// ── Independent page rendering ───────────────────────────────

// 把记住的视图状态写回 DOM，不触发加载（加载由 renderNotesPage 统一收口）。
function applyNotesView() {
  notesSortMode = normalizeNotesSort(notesView.sort || notesSortMode);
  currentSubtab = notesView.subtab === 'bookmarks' ? 'bookmarks' : 'notes';

  const q = document.getElementById('notesSearchInput');
  if (q) q.value = notesView.query || '';
  const clearBtn = document.getElementById('notesSearchClear');
  if (clearBtn) clearBtn.style.display = notesView.query ? '' : 'none';

  syncNotesTypeSwitch();
  document.getElementById('notesList').style.display = currentSubtab === 'notes' ? '' : 'none';
  document.getElementById('bookmarksList').style.display = currentSubtab === 'bookmarks' ? '' : 'none';
  updateSortButtons();
}

/** 左栏顶部的批注 / 收藏开关。 */
function syncNotesTypeSwitch() {
  document.querySelectorAll('.notes-type-switch .notes-type-btn').forEach(b => {
    const active = b.dataset.subtab === currentSubtab;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

async function renderNotesPage() {
  closeNotesBookSort();
  applyNotesView();
  await loadBookMeta();     // 封面/书名/书架排序都靠它
  await loadRailCounts(currentSubtab === 'notes' ? 'note' : 'highlight');
  renderBookRail();
  restoreScrollOnNextRender = true;
  if (currentSubtab === 'notes') await loadNotesList();
  else await loadBookmarksList();
}

async function switchNotesSubtab(sub) {
  currentSubtab = sub;
  saveNotesView({ subtab: sub, scrollTop: 0 });
  // 两个页签共用同一个滚动容器，换页签必须显式回顶
  const body = document.querySelector('#page-notes .notes-page-body');
  if (body) body.scrollTop = 0;
  closeNotesBookSort();
  syncNotesTypeSwitch();
  document.getElementById('notesList').style.display = sub === 'notes' ? '' : 'none';
  document.getElementById('bookmarksList').style.display = sub === 'bookmarks' ? '' : 'none';
  // 泳道计数跟着页签变（有笔记没收藏的书会从栏里消失）
  await loadRailCounts(sub === 'notes' ? 'note' : 'highlight');
  renderBookRail();
  if (sub === 'notes') loadNotesList();
  else loadBookmarksList();
}

/** 一条笔记的卡片 HTML。分组模式和扁平模式共用，靠 opts 决定 meta 里还写不写书/章。 */
function noteCardHtml(n, opts) {
  opts = opts || {};
  const isOrphaned = n.match_status === 'orphaned';
  const timeStr = n.time_updated
    ? new Date(n.time_updated).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : '';
  const bookTitle = bookTitleMap[n.stable_book_id] || '';
  const metaParts = [];
  if (opts.showBook !== false && bookTitle) metaParts.push(bookTitle.slice(0, 12));
  if (opts.showChapter !== false) metaParts.push('第' + ((n.chapter_index || 0) + 1) + '章');
  if (timeStr) metaParts.push(timeStr);
  return `
    <div class="note-card ${isOrphaned ? 'orphaned' : ''}"
         data-ann-id="${n.id}"
         onclick="onNoteCardClick(event,'${n.id}','${n.book_id || ''}',${n.chapter_index || 0},${n.chunk_index || 0},'${escapeHtml(n.cfi_range || '')}')"
         title="${isOrphaned ? '等待书籍重新导入' : '点击跳转原文'}">
      <div class="note-quote">${escapeHtml((n.selected_text || '').slice(0, 120))}${(n.selected_text || '').length > 120 ? '...' : ''}</div>
      <div class="note-text"${n.note_text ? '' : ' style="display:none"'}>${n.note_text ? renderNoteMarkdown(n.note_text) : ''}</div>
      <div class="note-editor">
        <textarea class="note-editor-input" rows="3" placeholder="写点批注…（支持 Markdown）">${escapeHtml(n.note_text || '')}</textarea>
        <div class="note-editor-bar">
          <div class="note-editor-actions">
            <span class="note-editor-error"></span>
            <button class="note-edit-cancel">取消</button>
            <button class="note-edit-save">保存</button>
          </div>
        </div>
      </div>
      <div class="note-meta">
        <span class="note-meta-text">${metaParts.join(' · ')}</span>
        <span class="note-actions">
          <button class="note-edit-btn" title="编辑这条笔记"><i data-lucide="pencil"></i></button>
          <button class="note-del" data-ann-id="${n.id}" title="删除这条批注"><i data-lucide="trash-2"></i></button>
        </span>
      </div>
    </div>
  `;
}

/**
 * 按书 → 章分组。只在按书/按章排序时使用；时间和字数排序要的是全局顺序。
 * hideBookHead：左栏已经选中某一本时，书名和条数都在泳道上，列表里不再重复。
 */
function renderGroupedNotes(notes, hideBookHead, mode) {
  const bookDir = mode === 'book-desc' ? -1 : 1;
  const chapterDir = mode === 'chapter-desc' ? -1 : 1;
  const groups = new Map();
  for (const n of notes) {
    const key = n.stable_book_id || '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(n);
  }
  const ordered = [...groups.entries()].sort((a, b) => {
    const ta = (bookTitleMap[a[0]] || '').toLowerCase();
    const tb = (bookTitleMap[b[0]] || '').toLowerCase();
    const cmp = ta < tb ? -1 : ta > tb ? 1 : 0;
    if (cmp) return bookDir * cmp;
    // 同名书保持稳定次序，不随方向翻转
    return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0;
  });
  return ordered.map(([sid, list]) => {
    list.sort((a, b) => chapterDir * (
      (a.chapter_index || 0) - (b.chapter_index || 0) ||
      (a.chunk_index || 0) - (b.chunk_index || 0)));
    const chapters = new Map();
    for (const n of list) {
      const key = n.chapter_index == null ? -1 : n.chapter_index;
      if (!chapters.has(key)) chapters.set(key, []);
      chapters.get(key).push(n);
    }
    const chapterEntries = [...chapters.entries()].sort(
      (a, b) => chapterDir * (a[0] - b[0]));
    const body = chapterEntries.map(([ci, items]) => {
      const title = items[0].chapter_title || (ci >= 0 ? '第' + (ci + 1) + '章' : '未定位章节');
      return `
        <div class="notes-chapter-head">${escapeHtml(title)}</div>
        ${items.map(n => noteCardHtml(n, { showBook: false, showChapter: false })).join('')}
      `;
    }).join('');
    if (hideBookHead) return `<section class="notes-group">${body}</section>`;
    return `
      <section class="notes-group">
        <div class="notes-group-head">
          <span class="notes-group-title">《${escapeHtml(bookTitleMap[sid] || '未知书籍')}》</span>
          <span class="notes-group-count">${list.length} 条</span>
        </div>
        ${body}
      </section>
    `;
  }).join('');
}

// ── Paged loading (server-side filter + sort + count) ─────────

const NOTES_PAGE_SIZE = 50;
// 已加载的条目累积在这里，「加载更多」是 append 而不是替换。
let notesPage = { kind: 'note', items: [], total: 0, loading: false };

function notesFilterState() {
  return {
    book: notesView.book || '',
    query: (document.getElementById('notesSearchInput')?.value || '').trim(),
  };
}

function notesHasFilters(f) {
  return !!(f.book || f.query);
}

function annotationPageUrl(kind, offset, f, limit) {
  const p = new URLSearchParams();
  p.set('type', kind);
  p.set('limit', String(limit || NOTES_PAGE_SIZE));
  p.set('offset', String(offset));
  // 两个页签共用同一套排序按钮与同一份方向。
  // asc 发旧键 chapter：老进程也认它，新进程把 chapter 当 asc；
  // chapter-desc 只有新进程支持（需重启服务）。
  p.set('sort', notesSortMode === 'chapter-asc' ? 'chapter' : notesSortMode);
  if (f.book) p.set('stable_book_id', f.book);
  if (f.query) p.set('q', f.query);
  return S.API + '/api/annotations?' + p.toString();
}

function notesEmptyHtml(kind, f) {
  const noun = kind === 'note' ? '笔记' : '收藏';
  if (f.query) {
    return `<div class="notes-empty">
      <div class="notes-empty-title">没有符合条件的${noun}</div>
      <div class="notes-empty-hint">当前有搜索词${f.book ? '，且限定了书籍' : ''}，换个词或放宽范围试试。</div>
      <button class="notes-empty-btn" data-act="clear-filters">清除筛选</button>
    </div>`;
  }
  if (f.book) {
    const bookTitle = bookTitleMap[f.book] || '这本';
    return `<div class="notes-empty">
      <div class="notes-empty-title">《${escapeHtml(bookTitle)}》还没有${noun}</div>
      <div class="notes-empty-hint">在阅读页选中一段文字，浮动条里点「${noun}」就能留下一笔。</div>
      <button class="notes-empty-btn" data-act="all-books">查看全部书籍</button>
    </div>`;
  }
  return `<div class="notes-empty">
    <div class="notes-empty-title">还没有${noun}</div>
    <div class="notes-empty-hint">在阅读页选中一段文字，浮动条里点「${noun}」就能留下一笔。</div>
    <button class="notes-empty-btn" data-act="go-reader">去阅读</button>
  </div>`;
}

function notesMoreHtml(loaded, total) {
  if (loaded >= total) return '';
  return `<div class="notes-more">
    <button class="notes-more-btn">加载更多（已显示 ${loaded} / ${total}）</button>
  </div>`;
}

function updateNotesTotal(total, shown) {
  const el = document.getElementById('notesTotal');
  if (!el) return;
  el.textContent = total ? `共 ${total} 条${shown < total ? ` · 已显示 ${shown}` : ''}` : '';
}

function bookmarkCardHtml(n) {
  const isOrphaned = n.match_status === 'orphaned';
  const bookTitle = bookTitleMap[n.stable_book_id] || '';
  return `
    <div class="bookmark-card ${isOrphaned ? 'orphaned' : ''}"
         onclick="jumpToAnnotation('${n.id}','${n.book_id || ''}',${n.chapter_index || 0},${n.chunk_index || 0},'${escapeHtml(n.cfi_range || '')}')"
         title="${isOrphaned ? '等待书籍重新导入' : '点击跳转原文'}">
      <button class="note-del bookmark-del" data-ann-id="${n.id}" title="取消收藏"><i data-lucide="trash-2"></i></button>
      <span class="bookmark-icon"><i data-lucide="underline"></i></span>
      <div class="bookmark-quote">${escapeHtml((n.selected_text || '').slice(0, 150))}${(n.selected_text || '').length > 150 ? '...' : ''}</div>
      <div class="bookmark-meta">${bookTitle ? bookTitle.slice(0, 12) + ' · ' : ''}第${(n.chapter_index || 0) + 1}章</div>
    </div>
  `;
}

/** 老后端兜底：服务端还不认 q/sort 时在前端做同样的过滤与排序。 */
function legacyFilterSort(rows, kind, f) {
  const wantType = kind === 'note' ? 'note' : 'highlight';
  let out = rows.filter(n => n.type === wantType || n.type === 'both');
  if (f.book) out = out.filter(n => n.stable_book_id === f.book);
  if (f.query) {
    const q = f.query.toLowerCase();
    out = out.filter(n =>
      (n.selected_text || '').toLowerCase().includes(q) ||
      (n.note_text || '').toLowerCase().includes(q));
  }
  const mode = notesSortMode;
  const byBook = (a, b) => {
    const ta = (bookTitleMap[a.stable_book_id] || '').toLowerCase();
    const tb = (bookTitleMap[b.stable_book_id] || '').toLowerCase();
    return ta < tb ? -1 : ta > tb ? 1 : 0;
  };
  const byChapter = (a, b) =>
    (a.chapter_index || 0) - (b.chapter_index || 0) ||
    (a.chunk_index || 0) - (b.chunk_index || 0);
  if (mode.startsWith('chapter')) {
    // 章节排序：书保持 A→Z，章内按方向翻（chunk 一起翻，读起来是完整倒序）
    const dir = mode === 'chapter-desc' ? -1 : 1;
    out.sort((a, b) => byBook(a, b) || dir * byChapter(a, b));
  } else if (mode === 'book-asc' || mode === 'book-desc') {
    const dir = mode === 'book-desc' ? -1 : 1;
    out.sort((a, b) => dir * byBook(a, b) || byChapter(a, b));
  } else if (mode === 'length-desc' || mode === 'length-asc') {
    const dir = mode === 'length-desc' ? -1 : 1;
    out.sort((a, b) => dir * ((a.selected_text || '').length - (b.selected_text || '').length));
  } else {
    const asc = mode === 'time-asc';
    out.sort((a, b) => {
      const ta = a.time_updated || a.time_created || 0;
      const tb = b.time_updated || b.time_created || 0;
      return asc ? ta - tb : tb - ta;
    });
  }
  return out;
}

/** 只重画列表（不重新取数）：删除、撤销、加载更多之后都走这里。 */
function renderNotesPageList() {
  const kind = notesPage.kind;
  const list = document.getElementById(kind === 'note' ? 'notesList' : 'bookmarksList');
  if (!list) return;
  const f = notesFilterState();
  let html = '';
  if (!notesPage.items.length) {
    html = notesEmptyHtml(kind, f);
  } else if (kind === 'note') {
    const grouped = notesSortMode.startsWith('book') || notesSortMode.startsWith('chapter');
    const oneBook = !!f.book;
    html = grouped
      ? renderGroupedNotes(notesPage.items, oneBook, notesSortMode)
      : notesPage.items.map(n => noteCardHtml(n, { showBook: !oneBook })).join('');
  } else {
    html = notesPage.items.map(bookmarkCardHtml).join('');
  }
  list.innerHTML = html + notesMoreHtml(notesPage.items.length, notesPage.total);
  // 撤销窗口里重画列表：待删的卡片要重新藏起来，否则会「删了又回来」
  pendingDeletes.forEach((entry, annId) => {
    if (entry.surface !== 'page') return;
    const del = list.querySelector('.note-del[data-ann-id="' + annId + '"]');
    const card = del ? del.closest('.note-card, .bookmark-card') : null;
    if (card) card.style.display = 'none';
  });
  updateNotesTotal(notesPage.total, notesPage.items.length);
  if (window.lucide && list.querySelector('i[data-lucide]')) window.lucide.createIcons();
}

let notesReqSeq = 0;

async function loadAnnotationPage(kind, opts) {
  const append = !!(opts && opts.append);
  const list = document.getElementById(kind === 'note' ? 'notesList' : 'bookmarksList');
  if (!list) return;
  // 只有「加载更多」要防连点；筛选/搜索的重新加载不能被在飞的旧请求挡掉
  if (append && notesPage.loading) return;
  const seq = ++notesReqSeq;
  if (!append) {
    notesPage.items = [];
    notesPage.total = 0;
  }
  notesPage.kind = kind;
  notesPage.loading = true;

  const f = notesFilterState();
  const offset = notesPage.items.length;
  if (!append) list.innerHTML = '<div class="notes-empty">加载中...</div>';

  try {
    let resp = await fetch(annotationPageUrl(kind, offset, f));
    let data = await resp.json();
    if (seq !== notesReqSeq) return;   // 已有更新的请求，这份响应作废
    // 兼容老进程：还没重启的服务不认 q/color/sort，也不回 total。
    // 这时退回前端过滤；一次拉 500 条（与改造前一致），免得只能看到前 50 条。
    // 重启服务后不会再走这里。
    const legacy = typeof data.total !== 'number';
    if (legacy && offset === 0) {
      resp = await fetch(annotationPageUrl(kind, 0, f, 500));
      data = await resp.json();
      if (seq !== notesReqSeq) return;
    }
    let rows = data.annotations || [];
    if (legacy) rows = legacyFilterSort(rows, kind, f);
    notesPage.total = legacy ? rows.length : data.total;
    notesPage.items = append ? notesPage.items.concat(rows) : rows;
    noteById = new Map(notesPage.items.map(n => [n.id, n]));
    renderNotesPageList();
  } catch (e) {
    if (seq === notesReqSeq) {
      list.innerHTML = '<div class="notes-empty" style="color:#ff6b6b">加载失败</div>';
    }
  } finally {
    if (seq === notesReqSeq) notesPage.loading = false;
    restorePageScroll();
  }
}

function loadNotesList(opts) { return loadAnnotationPage('note', opts); }
function loadBookmarksList(opts) { return loadAnnotationPage('highlight', opts); }

/** 筛选清空后回到「全部书籍」第一页。 */
function clearNotesFilters() {
  const q = document.getElementById('notesSearchInput');
  if (q) q.value = '';
  const clearBtn = document.getElementById('notesSearchClear');
  if (clearBtn) clearBtn.style.display = 'none';
  saveNotesView({ book: '', query: '', scrollTop: 0 });
  renderBookRail();
  updateSortButtons();
  if (currentSubtab === 'notes') loadNotesList();
  else loadBookmarksList();
}

function setNotesSort(mode) {
  if (mode === 'chapter') {
    // 第一次点保持原来的升序（章节↑），再点一次翻成倒序（章节↓）
    if (notesSortMode.startsWith('chapter')) {
      notesSortMode = notesSortMode === 'chapter-asc' ? 'chapter-desc' : 'chapter-asc';
    } else {
      notesSortMode = 'chapter-asc';
    }
  } else if (notesSortMode.startsWith(mode)) {
    notesSortMode = notesSortMode.endsWith('-asc') ? mode + '-desc' : mode + '-asc';
  } else {
    if (mode === 'book') notesSortMode = 'book-asc';
    else notesSortMode = mode + '-desc';
  }
  saveNotesView({ sort: notesSortMode });
  updateSortButtons();
  // 收藏页签现在也有排序，两个页签都要按新方向重取
  if (currentSubtab === 'notes') loadNotesList();
  else loadBookmarksList();
}

function updateSortButtons() {
  const mode = notesSortMode;
  const base = mode.replace(/-asc|-desc/, '');
  document.querySelectorAll('#notesSortRow .sort-btn').forEach(b => b.classList.remove('active'));
  const activeBtn = document.getElementById('sortBtn' + base.charAt(0).toUpperCase() + base.slice(1));
  if (activeBtn) activeBtn.classList.add('active');
  const btnBook = document.getElementById('sortBtnBook');
  const btnTime = document.getElementById('sortBtnTime');
  const btnLength = document.getElementById('sortBtnLength');
  const btnChapter = document.getElementById('sortBtnChapter');
  if (btnBook) btnBook.innerHTML = mode === 'book-desc' ? '<i data-lucide="book-open"></i> Z->A' : '<i data-lucide="book-open"></i> A->Z';
  if (btnTime) btnTime.innerHTML = mode === 'time-asc' ? '<i data-lucide="clock"></i> 最早' : '<i data-lucide="clock"></i> 最新';
  if (btnLength) btnLength.innerHTML = mode === 'length-asc' ? '<i data-lucide="ruler"></i> 短->长' : '<i data-lucide="ruler"></i> 长->短';
  if (btnChapter) btnChapter.innerHTML = mode === 'chapter-desc'
    ? '<i data-lucide="list-ordered"></i> 章节↓'
    : '<i data-lucide="list-ordered"></i> 章节↑';
  // 左栏选中某一本时，「按书排序」没有意义（那一本本来就是唯一一组）
  if (btnBook) btnBook.style.display = notesView.book ? 'none' : '';
}

// ── Search / filter ───────────────────────────────────────────

function onNotesSearchInput() {
  const input = document.getElementById('notesSearchInput');
  const clearBtn = document.getElementById('notesSearchClear');
  if (clearBtn) clearBtn.style.display = input.value ? '' : 'none';
  if (window._notesSearchTimer) clearTimeout(window._notesSearchTimer);
  window._notesSearchTimer = setTimeout(() => {
    saveNotesView({ query: input.value.trim() });
    if (currentSubtab === 'notes') loadNotesList();
    else loadBookmarksList();
  }, 300);
}

function clearNotesSearch() {
  document.getElementById('notesSearchInput').value = '';
  document.getElementById('notesSearchClear').style.display = 'none';
  saveNotesView({ query: '' });
  if (currentSubtab === 'notes') loadNotesList();
  else loadBookmarksList();
}

function renderNoteMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  html = html.replace(/\*(.+?)\*/g, '<i>$1</i>');
  html = html.replace(/`(.+?)`/g, '<code>$1</code>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

// ── Delete ────────────────────────────────────────────────────

// 删除改成「先藏起来，5 秒后真删」：误删有得撤销，也就不必每次弹 confirm。
// 5 秒内点撤销 → 卡片原地恢复，DELETE 根本不发；超时才落库。
const DELETE_UNDO_MS = 5000;
let pendingDeletes = new Map();  // annId -> { timer, card, surface, dismiss }

function showNotesToast(text, undoFn, ms) {
  let host = document.getElementById('notesToastHost');
  if (!host) {
    host = document.createElement('div');
    host.id = 'notesToastHost';
    host.className = 'notes-toast-host';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = 'notes-toast';
  const span = document.createElement('span');
  span.className = 'notes-toast-text';
  span.textContent = text;
  el.appendChild(span);
  let timer = null;
  function dismiss() {
    if (timer) clearTimeout(timer);
    el.remove();
  }
  if (undoFn) {
    const btn = document.createElement('button');
    btn.className = 'notes-toast-undo';
    btn.textContent = '撤销';
    btn.addEventListener('click', function () { undoFn(); dismiss(); });
    el.appendChild(btn);
  }
  host.appendChild(el);
  timer = setTimeout(dismiss, ms || DELETE_UNDO_MS);
  return dismiss;
}

/** 删掉一条批注/收藏。正文层的角标与下划线等真正落库后再由 annotations.js 收掉。 */
function deleteAnnotation(annId) {
  if (!annId || pendingDeletes.has(annId)) return;
  // 同一条批注可能同时出现在笔记页和浮窗里：按可见的那张卡片判断来源，
  // 两张都藏起来，撤销时也一起放回来。
  const cards = Array.from(document.querySelectorAll('.note-del[data-ann-id="' + annId + '"]'))
    .map(btn => btn.closest('.note-card, .bookmark-card'))
    .filter((c, i, arr) => c && arr.indexOf(c) === i);
  const visibleCard = cards.find(c => c.getClientRects().length > 0) || cards[0] || null;
  const surface = visibleCard && visibleCard.closest('#miniNotesList') ? 'mini' : 'page';
  const isBookmark = !!(visibleCard && visibleCard.classList.contains('bookmark-card'));
  cards.forEach(c => { c.style.display = 'none'; });
  const card = visibleCard;

  const entry = {
    card,
    surface,
    timer: setTimeout(() => commitDelete(annId), DELETE_UNDO_MS),
    dismiss: null,
  };
  entry.dismiss = showNotesToast(isBookmark ? '已取消收藏' : '已删除这条笔记', () => undoDelete(annId));
  pendingDeletes.set(annId, entry);
}

function undoDelete(annId) {
  const entry = pendingDeletes.get(annId);
  if (!entry) return;
  clearTimeout(entry.timer);
  pendingDeletes.delete(annId);
  // 卡片还在 DOM 里就原地放出来；期间列表已经重画过（切页签/筛选）就重画一次
  const cards = Array.from(document.querySelectorAll('.note-del[data-ann-id="' + annId + '"]'))
    .map(btn => btn.closest('.note-card, .bookmark-card'))
    .filter((c, i, arr) => c && arr.indexOf(c) === i);
  if (cards.length && cards.every(c => c.isConnected)) {
    cards.forEach(c => { c.style.display = ''; });
  } else if (entry.surface === 'mini') {
    loadMiniNotes();
  } else {
    renderNotesPageList();
  }
}

async function commitDelete(annId) {
  const entry = pendingDeletes.get(annId) || {};
  pendingDeletes.delete(annId);
  try {
    const resp = await fetch(S.API + '/api/annotations/' + annId, { method: 'DELETE' });
    const data = await resp.json();
    if (!data.deleted) throw new Error(data.error || '未知原因');
  } catch (e) {
    // 没删成：卡片放回去，别让读者以为已经没了
    if (entry.card && entry.card.isConnected) entry.card.style.display = '';
    showNotesToast('删除失败：' + (e.message || '未知原因'), null, 4000);
    return;
  }
  if (window.bcAnnotationDeleted) window.bcAnnotationDeleted(annId);
  if (entry.card && entry.card.isConnected) entry.card.remove();

  // 浮窗开着就刷新它（同一条在两边都挂过）
  const mini = document.getElementById('miniNotesPanel');
  if (mini && mini.style.display !== 'none') loadMiniNotes();

  if (entry.surface === 'mini') return;
  // 页码要跟着收缩：删掉一行后服务端结果也少一行，offset 用新的长度才不跳条
  notesPage.items = notesPage.items.filter(n => n.id !== annId);
  notesPage.total = Math.max(0, notesPage.total - 1);
  noteById.delete(annId);
  renderNotesPageList();
}

// ── Inline edit ───────────────────────────────────────────────

/** 卡片主体点击 = 跳原文；点编辑/删除/编辑器内部不跳。 */
function onNoteCardClick(e, annId, bookId, chapterIdx, chunkIdx, cfi) {
  if (e.target && e.target.closest && e.target.closest('.note-editor, .note-edit-btn, .note-del')) return;
  saveNotesView({ scrollTop: currentPageScrollTop() });
  jumpToAnnotation(annId, bookId, chapterIdx, chunkIdx, cfi);
}

function openNoteEditor(card) {
  if (!card) return;
  // 同一时间只开一个编辑器，避免两个 textarea 各自挂着一份未保存的草稿
  document.querySelectorAll('#notesList .note-card.editing').forEach(c => {
    if (c !== card) closeNoteEditor(c);
  });
  const rec = noteById.get(card.dataset.annId);
  card.classList.add('editing');
  const input = card.querySelector('.note-editor-input');
  if (input) {
    input.value = (rec && rec.note_text) || '';
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

function closeNoteEditor(card) {
  if (!card) return;
  card.classList.remove('editing');
  const err = card.querySelector('.note-editor-error');
  if (err) err.textContent = '';
  const rec = noteById.get(card.dataset.annId);
  const input = card.querySelector('.note-editor-input');
  if (input) input.value = (rec && rec.note_text) || '';
}

function applySavedNote(card, text) {
  const textEl = card.querySelector('.note-text');
  if (textEl) {
    textEl.innerHTML = text ? renderNoteMarkdown(text) : '';
    textEl.style.display = text ? '' : 'none';
  }
  const rec = noteById.get(card.dataset.annId);
  if (rec) rec.note_text = text;
}

async function saveNoteEditor(card) {
  if (!card || card.dataset.saving === '1') return;
  const id = card.dataset.annId;
  const input = card.querySelector('.note-editor-input');
  if (!input) return;
  const text = input.value;
  const btn = card.querySelector('.note-edit-save');
  const err = card.querySelector('.note-editor-error');
  if (err) err.textContent = '';
  card.dataset.saving = '1';
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch(S.API + '/api/annotations/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note_text: text }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.updated) throw new Error(data.error || '保存失败');
    applySavedNote(card, text);
    card.classList.remove('editing');
    const mini = document.getElementById('miniNotesPanel');
    if (mini && mini.style.display !== 'none') loadMiniNotes();
  } catch (e) {
    if (err) err.textContent = e.message || '保存失败';
  } finally {
    delete card.dataset.saving;
    if (btn) btn.disabled = false;
  }
}

// 编辑/保存/取消都走事件委托：卡片自己挂着整块 onclick，
// 这些点击不能冒到「跳原文」那一层。
document.addEventListener('click', function (e) {
  const t = e.target;
  if (!t || !t.closest) return;
  const editBtn = t.closest('.note-edit-btn');
  if (editBtn) {
    e.preventDefault();
    e.stopPropagation();
    openNoteEditor(editBtn.closest('.note-card'));
    return;
  }
  const card = t.closest('.note-card.editing');
  if (!card) return;
  if (t.closest('.note-edit-save')) { saveNoteEditor(card); return; }
  if (t.closest('.note-edit-cancel')) { closeNoteEditor(card); }
});

// 快捷键挂在整张编辑中的卡片上，不只 textarea：焦点落在保存/取消按钮上时，
// Esc 也该能退出。
document.addEventListener('keydown', function (e) {
  const card = e.target && e.target.closest
    ? e.target.closest('#notesList .note-card.editing') : null;
  if (!card) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    closeNoteEditor(card);
  } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    saveNoteEditor(card);
  }
});

// ── Navigation ────────────────────────────────────────────────

function jumpToAnnotation(annId, bookId, chapterIdx, chunkIdx, cfi) {
  if (!bookId) {
    alert('此注释的原文在当前版本中已不存在。重新导入书籍后可恢复。');
    return;
  }
  // 切 tab / 切书 / 作废在途恢复 / 落点全部交给 jumpToPosition（见其注释）：
  // 这里曾自己 switchTab('reader') + 定位，会被 switchTab 的两条副作用吃掉
  // —— refreshBooks 的异步 selectBook 把视口恢复到 DB 进度，restoreViewport
  // 延迟 2 帧 display 暂存锚。两者都晚于本次跳转，表现为"点了没反应"。
  jumpToPosition(bookId, chapterIdx, chunkIdx, cfi).then(ok => {
    if (!ok) alert('跳转失败：书籍可能已被删除或尚未导入。');
    else showBackToNotes();
  });
}

/** 从笔记跳进正文后，在阅读页头部留一个回程入口，不用再去侧栏里找。 */
function showBackToNotes() {
  // 跳转是异步的：落点还没到、读者已经切走时，别把回程入口挂到别的页上
  const page = document.getElementById('page-reader');
  if (!page || !page.classList.contains('active')) return;
  let btn = document.getElementById('backToNotesBtn');
  if (!btn) {
    const controls = document.querySelector('#page-reader .controls');
    if (!controls) return;
    btn = document.createElement('button');
    btn.id = 'backToNotesBtn';
    btn.title = '回到阅读历程';
    btn.innerHTML = '<i data-lucide="arrow-left"></i> 返回笔记';
    btn.addEventListener('click', backToNotes);
    controls.appendChild(btn);
    if (window.lucide) window.lucide.createIcons();
  }
  btn.style.display = '';
}

function hideBackToNotes() {
  const btn = document.getElementById('backToNotesBtn');
  if (btn) btn.style.display = 'none';
}

function backToNotes() {
  hideBackToNotes();
  if (window.switchTab) window.switchTab('notes');
}

// 从侧栏离开阅读页就不再需要回程入口；回来时不该挂着一个过期的按钮。
document.addEventListener('click', function (e) {
  if (e.target && e.target.closest && e.target.closest('.sidebar .tab')) hideBackToNotes();
}, true);

// 「加载更多」/ 空状态按钮：走捕获阶段，免得被卡片自己的 onclick 抢走。
document.addEventListener('click', function (e) {
  const t = e.target;
  if (!t || !t.closest) return;
  const more = t.closest('.notes-more-btn');
  if (more) {
    e.preventDefault();
    e.stopPropagation();
    if (notesPage.kind === 'note') loadNotesList({ append: true });
    else loadBookmarksList({ append: true });
    return;
  }
  const act = t.closest('.notes-empty-btn');
  if (act) {
    e.preventDefault();
    e.stopPropagation();
    if (act.dataset.act === 'clear-filters' || act.dataset.act === 'all-books') clearNotesFilters();
    else if (act.dataset.act === 'go-reader' && window.switchTab) window.switchTab('reader');
  }
}, true);

// 左栏泳道：点哪本就把列表收窄到哪本，「全部书籍」清空选择。
document.addEventListener('click', function (e) {
  const lane = e.target && e.target.closest ? e.target.closest('.notes-lane') : null;
  if (!lane) return;
  e.preventDefault();
  e.stopPropagation();
  selectNotesBook(lane.dataset.book || '');
}, true);

// ── Export ────────────────────────────────────────────────────

/**
 * 导出当前页签、当前筛选条件下的全部条目。
 * 从服务端拿完整数据再按「书 → 章」组结构，而不是读已经渲染的 DOM——
 * 分页之后 DOM 只有当前页，读 DOM 会静默导出半截，且丢掉书名/章名/时间。
 */
async function exportNotes() {
  const kind = currentSubtab === 'notes' ? 'note' : 'highlight';
  const f = notesFilterState();
  const p = new URLSearchParams();
  p.set('type', kind);
  p.set('limit', '100000');
  p.set('offset', '0');
  p.set('sort', 'chapter');
  if (f.book) p.set('stable_book_id', f.book);
  if (f.query) p.set('q', f.query);

  let rows = [];
  try {
    const resp = await fetch(S.API + '/api/annotations?' + p.toString());
    const data = await resp.json();
    rows = data.annotations || [];
  } catch (e) {
    alert('导出失败：' + e.message);
    return;
  }
  if (!rows.length) {
    alert(kind === 'note' ? '当前没有可导出的笔记' : '当前没有可导出的收藏');
    return;
  }

  const title = kind === 'note' ? '# 读书笔记' : '# 阅读收藏';
  let md = title + '\n\n';
  const scope = [];
  if (f.book) scope.push('《' + (bookTitleMap[f.book] || '未知书籍') + '》');
  if (f.query) scope.push('搜索：' + f.query);
  if (scope.length) md += `> 导出范围：${scope.join(' · ')}\n\n`;

  let lastBook = null;
  let lastChapter = null;
  for (const n of rows) {
    if (n.stable_book_id !== lastBook) {
      lastBook = n.stable_book_id;
      lastChapter = null;
      md += `## 《${bookTitleMap[n.stable_book_id] || '未知书籍'}》\n\n`;
    }
    const chapterLabel = n.chapter_title || ((n.chapter_index || 0) + 1) + ' 章';
    if (chapterLabel !== lastChapter) {
      lastChapter = chapterLabel;
      md += `### ${chapterLabel}\n\n`;
    }
    const quote = (n.selected_text || '').replace(/\s+/g, ' ').trim();
    md += `> ${quote}\n\n`;
    if (kind === 'note' && n.note_text) md += `${n.note_text}\n\n`;
    if (n.time_updated) {
      md += `<sub>${new Date(n.time_updated).toLocaleString('zh-CN')}</sub>\n\n`;
    }
    md += '---\n\n';
  }
  md += `\n导出时间: ${new Date().toLocaleString('zh-CN')}\n`;

  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (kind === 'note' ? '读书笔记' : '阅读收藏') + '.md';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── Book rail（左栏书籍泳道）───────────────────────────────────

async function loadBookMeta() {
  try {
    const resp = await fetch(S.API + '/api/books');
    const data = await resp.json();
    bookTitleMap = {};
    bookMeta = {};
    (data.books || []).forEach(b => {
      const sid = b.stable_book_id;
      if (!sid) return;
      bookTitleMap[sid] = b.title;
      bookMeta[sid] = b;
    });
  } catch (e) {}
}

/** 当前页签下每本书有多少条。泳道上的「N 条」用它。 */
async function loadRailCounts(kind) {
  try {
    const resp = await fetch(S.API + '/api/annotations?type=' + kind + '&counts=1');
    const data = await resp.json();
    if (data.counts) { railCounts = data.counts; return; }
    // 老进程还不认 counts=1：退回按一页批注自己数（过渡代码，重启后不走到）
    const page = await fetch(S.API + '/api/annotations?type=' + kind + '&limit=500');
    const rows = (await page.json()).annotations || [];
    const counts = {};
    rows.forEach(r => {
      if (r.stable_book_id) counts[r.stable_book_id] = (counts[r.stable_book_id] || 0) + 1;
    });
    railCounts = counts;
  } catch (e) {
    railCounts = {};
  }
}

function coverHtml(b) {
  if (b.has_cover) {
    return `<img class="notes-lane-cover" src="${S.API}/api/books/${b.id}/cover?thumb=1"
                 alt="" loading="lazy" decoding="async"
                 onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
            <span class="notes-lane-cover notes-lane-cover-ph" style="display:none">${escapeHtml((b.title || '').slice(0, 2))}</span>`;
  }
  return `<span class="notes-lane-cover notes-lane-cover-ph">${escapeHtml((b.title || '').slice(0, 2))}</span>`;
}

// ── 书籍排序菜单（与书架页共用同一份偏好）────────────────────
const BOOK_SORT_ICONS = {
  time: 'calendar-plus',
  title: 'type',
  author: 'user-round',
  recent: 'clock',
  custom: 'list-ordered',
};

function buildNotesBookSortPanel(panel) {
  panel.innerHTML = SORT_KEYS.map(k => `
    <button type="button" class="notes-booksort-option" data-key="${k}"
            onclick="setNotesBookSort('${k}')">
      <i data-lucide="${BOOK_SORT_ICONS[k] || 'arrow-up-down'}"></i>
      <span class="notes-booksort-label">${SORT_LABELS[k]}</span>
      <span class="notes-booksort-dir"></span>
    </button>`).join('');
  if (window.lucide) window.lucide.createIcons();
}

/** 只改激活态与方向箭头，不重建整个面板 —— 重建会在 click 冒泡途中
 *  把被点的按钮摘出 DOM，window 的「点外面收起」监听随后就认不出它。 */
function updateNotesBookSortUI() {
  const label = document.getElementById('notesBookSortLabel');
  if (label) label.textContent = SORT_LABELS[S.shelfSort.key] || '排序';
  const panel = document.getElementById('notesBookSortPanel');
  if (!panel) return;
  if (!panel.childElementCount) buildNotesBookSortPanel(panel);
  const { key, dir } = S.shelfSort;
  panel.querySelectorAll('.notes-booksort-option').forEach(opt => {
    const k = opt.dataset.key;
    const active = k === key;
    const isCustom = k === 'custom';
    opt.classList.toggle('active', active);
    opt.title = active
      ? (isCustom ? '自定义顺序固定，整理请去书架页' : '再点一次切换正序 / 倒序')
      : '按' + (SORT_LABELS[k] || '') + '排序';
    const dirEl = opt.querySelector('.notes-booksort-dir');
    if (!dirEl) return;
    const icon = active && !isCustom
      ? (dir === 'asc' ? 'chevron-up' : 'chevron-down')
      : '';
    if (dirEl.dataset.icon === icon) return;
    dirEl.dataset.icon = icon;
    dirEl.innerHTML = icon ? `<i data-lucide="${icon}"></i>` : '';
    if (window.lucide) window.lucide.createIcons();
  });
}

function toggleNotesBookSort() {
  const wrap = document.getElementById('notesBookSortWrap');
  if (!wrap) return;
  if (wrap.classList.toggle('open')) updateNotesBookSortUI();
}

function closeNotesBookSort() {
  const wrap = document.getElementById('notesBookSortWrap');
  if (wrap) wrap.classList.remove('open');
}

/** 书架页的排序偏好是唯一真源：换 key 或对同一 key 翻方向都由 setSort 处理。
 *  自定义例外：手工序列固定正序，再点不翻，也不画方向箭头。 */
function setNotesBookSort(key) {
  if (!SORT_KEYS.includes(key)) return;
  setShelfSort(key);
  renderBookRail();
  updateNotesBookSortUI();
}

function renderBookRail() {
  const list = document.getElementById('notesRailList');
  const allMeta = document.getElementById('notesLaneAllMeta');
  const allLane = document.getElementById('notesLaneAll');
  if (!list) return;

  let selected = notesView.book || '';
  // 选中的书被删了：栏里已经没有它，选择退回「全部书籍」，别留一个空选中态
  if (selected && !bookMeta[selected]) {
    selected = '';
    saveNotesView({ book: '' });
  }
  const total = Object.values(railCounts).reduce((a, b) => a + b, 0);
  if (allMeta) allMeta.textContent = total ? `${total} 条` : '暂无';
  if (allLane) allLane.classList.toggle('active', !selected);

  // 只有当前页签下有内容的书才进栏；顺序直接复用书架页的排序。
  loadShelfSort();
  const sortLabel = document.getElementById('notesBookSortLabel');
  if (sortLabel) sortLabel.textContent = SORT_LABELS[S.shelfSort.key] || '排序';
  const books = Object.values(bookMeta)
    .filter(b => (railCounts[b.stable_book_id] || 0) > 0);
  const sorted = sortBooks(books);

  // 选中那本在当前页签下可能一条也没有（有笔记没收藏）：把它留在栏里，
  // 标 0 条，别让选中态凭空消失。
  if (selected && !sorted.some(b => b.stable_book_id === selected) && bookMeta[selected]) {
    sorted.push(bookMeta[selected]);
  }

  list.innerHTML = sorted.map(b => {
    const sid = b.stable_book_id;
    const count = railCounts[sid] || 0;
    return `
      <button type="button" class="notes-lane${sid === selected ? ' active' : ''}"
              data-book="${escapeHtml(sid)}" title="${escapeHtml(b.title || '')}">
        ${coverHtml(b)}
        <span class="notes-lane-body">
          <span class="notes-lane-title">${escapeHtml(b.title || '')}</span>
          <span class="notes-lane-meta">${count} 条</span>
        </span>
      </button>
    `;
  }).join('');
  if (window.lucide) window.lucide.createIcons();
}

function selectNotesBook(sid) {
  sid = sid || '';
  // 泳道的 click 在捕获阶段 stopPropagation，排序菜单收不到那次冒泡，
  // 所以这里显式收起，避免选完书菜单还盖在栏上。
  closeNotesBookSort();
  if ((notesView.book || '') === sid) return;
  saveNotesView({ book: sid, scrollTop: 0 });
  const body = document.querySelector('#page-notes .notes-page-body');
  if (body) body.scrollTop = 0;
  renderBookRail();
  updateSortButtons();
  if (currentSubtab === 'notes') loadNotesList();
  else loadBookmarksList();
}

// ── Init ──────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function () {
  // Pre-create the mini panel (hidden until reader toolbar "📋 浮窗" click)
  getOrCreateMiniPanel();
  document.getElementById('miniNotesPanel').style.display = 'none';

  // 滚动位置节流落盘：滚动事件本身很密，150ms 足够，也不怕切走时丢现场
  // （点卡片跳原文前还会同步存一次，见 onNoteCardClick）。
  const body = document.querySelector('#page-notes .notes-page-body');
  if (body) {
    let timer = null;
    body.addEventListener('scroll', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { saveNotesView({ scrollTop: body.scrollTop }); }, 150);
    }, { passive: true });
  }
});

// 书籍排序菜单：点菜单外面或按 Esc 收起。触发按钮与选项都在 wrap 里，
// 由 closest 放行，不会「开了又立刻关」。
document.addEventListener('click', function (e) {
  const wrap = document.getElementById('notesBookSortWrap');
  if (!wrap || !wrap.classList.contains('open')) return;
  if (e.target && e.target.closest && e.target.closest('#notesBookSortWrap')) return;
  closeNotesBookSort();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeNotesBookSort();
});

// 删除按钮走捕获阶段：卡片自己挂着「点击跳转原文」的 onclick，先拦下这一下，
// 免得删完顺手又把书翻走。
document.addEventListener('click', function (e) {
  const btn = e.target && e.target.closest
    ? e.target.closest('.note-del') : null;
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  deleteAnnotation(btn.dataset.annId);
}, true);

// ── HTML onclick access ───────────────────────────────────────
window.openNotesPanel = openNotesPanel;
window.closeNotesPanel = closeNotesPanel;
window.setMiniPanelVisible = setMiniPanelVisible;
window.switchNotesSubtab = switchNotesSubtab;
window.renderNotesPage = renderNotesPage;
window.onNotesSearchInput = onNotesSearchInput;
window.clearNotesSearch = clearNotesSearch;
window.setNotesSort = setNotesSort;
window.jumpToAnnotation = jumpToAnnotation;
window.onNoteCardClick = onNoteCardClick;
window.deleteAnnotation = deleteAnnotation;
window.loadNotesList = loadNotesList;
window.exportNotes = exportNotes;
window.toggleNotesBookSort = toggleNotesBookSort;
window.setNotesBookSort = setNotesBookSort;
window.closeNotesBookSort = closeNotesBookSort;
