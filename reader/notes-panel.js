/* Book Companion Reader - notes & bookmarks: independent page + mini floating panel.
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
import { jumpToCfi } from './js/epub-reader.js';

// ── State ────────────────────────────────────────────────────
let notesPanelState = { minimized: false, x: null, y: null };
let miniPanelWasOpen = false;  // set while hidden by tab switch, cleared by manual close
let notesSortMode = 'book-asc';  // book-asc|book-desc|time-desc|time-asc|length-desc|length-asc|chapter
let bookTitleMap = {};  // stable_book_id -> title (built from /api/books)
let currentSubtab = 'notes';  // 'notes' | 'bookmarks'

// ── Mini floating panel (current book notes) ─────────────────

function getOrCreateMiniPanel() {
  let panel = document.getElementById('miniNotesPanel');
  if (panel) return panel;

  panel = document.createElement('div');
  panel.id = 'miniNotesPanel';
  panel.className = 'notes-mini-panel';
  panel.innerHTML = `
    <div class="notes-mini-header" id="miniNotesHeader">
      <span class="notes-mini-title"><i data-lucide="message-square"></i> 本章批注</span>
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

async function loadMiniNotes() {
  const list = document.getElementById('miniNotesList');
  if (!list) return;
  if (!S.currentBook) {
    list.innerHTML = '<div class="notes-empty">未打开书籍</div>';
    return;
  }
  list.innerHTML = '<div class="notes-empty">加载中...</div>';
  try {
    const ctxResp = await fetch(S.API + '/api/books/' + S.currentBook + '/context');
    const ctx = await ctxResp.json();
    const sid = ctx.stable_book_id;
    if (!sid) { list.innerHTML = '<div class="notes-empty">无笔记</div>'; return; }

    const resp = await fetch(S.API + '/api/annotations?stable_book_id=' + sid + '&type=note&limit=200');
    const data = await resp.json();
    // Backend get_annotations_by_type returns type=X OR type='both'; for notes both is wanted.
    let notes = (data.annotations || []).filter(n => n.type === 'note' || n.type === 'both');
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
      const color = n.color || '#FFD54F';
      const isCurrent = (n.chapter_index || 0) === curCh;
      return `
        <div class="note-card${isCurrent ? ' current' : ''}" style="border-left-color:${color}"
             onclick="jumpToAnnotation('${n.id}','${n.book_id || ''}',${n.chapter_index || 0},${n.chunk_index || 0},'${escapeHtml(n.cfi_range || '')}')">
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

function renderNotesPage() {
  loadBookFilter();
  if (currentSubtab === 'notes') loadNotesList();
  else loadBookmarksList();
}

function switchNotesSubtab(sub) {
  currentSubtab = sub;
  document.querySelectorAll('.notes-subtabs .subtab').forEach(b => {
    b.classList.toggle('active', b.dataset.subtab === sub);
  });
  document.getElementById('notesList').style.display = sub === 'notes' ? '' : 'none';
  document.getElementById('bookmarksList').style.display = sub === 'bookmarks' ? '' : 'none';
  // Color filter only applies to notes
  const colorFilter = document.getElementById('notesFilterColor');
  if (colorFilter) colorFilter.style.display = sub === 'notes' ? '' : 'none';
  if (sub === 'notes') loadNotesList();
  else loadBookmarksList();
}

async function loadNotesList() {
  const list = document.getElementById('notesList');
  if (!list) return;
  const bookFilter = document.getElementById('notesFilterBook').value;
  const colorFilter = document.getElementById('notesFilterColor').value;
  const query = (document.getElementById('notesSearchInput').value || '').trim().toLowerCase();

  list.innerHTML = '<div class="notes-empty">加载中...</div>';

  try {
    const resp = await fetch(S.API + '/api/annotations?type=note&limit=500');
    const data = await resp.json();
    let notes = (data.annotations || []).filter(n => n.type === 'note' || n.type === 'both');
    if (bookFilter) notes = notes.filter(n => n.stable_book_id === bookFilter);
    if (colorFilter) notes = notes.filter(n => n.color === colorFilter);
    if (query) {
      notes = notes.filter(n =>
        (n.selected_text || '').toLowerCase().includes(query) ||
        (n.note_text || '').toLowerCase().includes(query)
      );
    }
    notes = sortNotesList(notes);

    if (!notes.length) {
      list.innerHTML = '<div class="notes-empty">' + (query ? '无匹配笔记' : '暂无笔记。选中文字后在阅读器点「<i data-lucide="sticky-note"></i> 笔记」创建。') + '</div>';
      return;
    }
    list.innerHTML = notes.map(n => {
      const isOrphaned = n.match_status === 'orphaned';
      const color = n.color || '#FFD54F';
      const timeStr = n.time_updated
        ? new Date(n.time_updated).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
        : '';
      const bookTitle = bookTitleMap[n.stable_book_id] || '';
      return `
        <div class="note-card ${isOrphaned ? 'orphaned' : ''}" style="border-left-color:${color}"
             onclick="jumpToAnnotation('${n.id}','${n.book_id || ''}',${n.chapter_index || 0},${n.chunk_index || 0},'${escapeHtml(n.cfi_range || '')}')"
             title="${isOrphaned ? '等待书籍重新导入' : '点击跳转原文'}">
          <div class="note-quote">${escapeHtml((n.selected_text || '').slice(0, 120))}${(n.selected_text || '').length > 120 ? '...' : ''}</div>
          ${n.note_text ? `<div class="note-text">${renderNoteMarkdown(n.note_text)}</div>` : ''}
          <div class="note-meta"><span style="color:${color}">●</span> ${bookTitle ? bookTitle.slice(0, 12) + ' · ' : ''}第${(n.chapter_index || 0) + 1}章 · ${timeStr}</div>
        </div>
      `;
    }).join('');
  } catch (e) {
    list.innerHTML = '<div class="notes-empty" style="color:#ff6b6b">加载失败</div>';
  }
}

async function loadBookmarksList() {
  const list = document.getElementById('bookmarksList');
  if (!list) return;
  const bookFilter = document.getElementById('notesFilterBook').value;
  const query = (document.getElementById('notesSearchInput').value || '').trim().toLowerCase();

  list.innerHTML = '<div class="notes-empty">加载中...</div>';

  try {
    const resp = await fetch(S.API + '/api/annotations?type=highlight&limit=500');
    const data = await resp.json();
    // 收藏 tab：type=highlight（下划线收藏）。both（旧高亮+笔记混合）也计入——
    // 旧数据在两个 tab 各自可见，不再产生新 both。
    let bms = (data.annotations || []).filter(n => n.type === 'highlight' || n.type === 'both');
    if (bookFilter) bms = bms.filter(n => n.stable_book_id === bookFilter);
    if (query) bms = bms.filter(n => (n.selected_text || '').toLowerCase().includes(query));
    // Sort: by book (using bookTitleMap), then chapter, then chunk
    bms.sort((a, b) => {
      const ta = (bookTitleMap[a.stable_book_id] || '').toLowerCase();
      const tb = (bookTitleMap[b.stable_book_id] || '').toLowerCase();
      if (ta !== tb) return ta < tb ? -1 : 1;
      return (a.chapter_index || 0) - (b.chapter_index || 0) || (a.chunk_index || 0) - (b.chunk_index || 0);
    });

    if (!bms.length) {
      list.innerHTML = '<div class="notes-empty">' + (query ? '无匹配收藏' : '暂无收藏。选中文字后在阅读器点「<i data-lucide="bookmark"></i> 收藏」创建。') + '</div>';
      return;
    }
    list.innerHTML = bms.map(n => {
      const isOrphaned = n.match_status === 'orphaned';
      const bookTitle = bookTitleMap[n.stable_book_id] || '';
      return `
        <div class="bookmark-card ${isOrphaned ? 'orphaned' : ''}"
             onclick="jumpToAnnotation('${n.id}','${n.book_id || ''}',${n.chapter_index || 0},${n.chunk_index || 0},'${escapeHtml(n.cfi_range || '')}')"
             title="${isOrphaned ? '等待书籍重新导入' : '点击跳转原文'}">
          <span class="bookmark-icon"><i data-lucide="underline"></i></span>
          <div class="bookmark-quote">${escapeHtml((n.selected_text || '').slice(0, 150))}${(n.selected_text || '').length > 150 ? '...' : ''}</div>
          <div class="bookmark-meta">${bookTitle ? bookTitle.slice(0, 12) + ' · ' : ''}第${(n.chapter_index || 0) + 1}章</div>
        </div>
      `;
    }).join('');
  } catch (e) {
    list.innerHTML = '<div class="notes-empty" style="color:#ff6b6b">加载失败</div>';
  }
}

// ── Sort ──────────────────────────────────────────────────────

function sortNotesList(notes) {
  const mode = notesSortMode;
  if (mode === 'chapter') {
    return notes.sort((a, b) =>
      (a.chapter_index || 0) - (b.chapter_index || 0) ||
      (a.chunk_index || 0) - (b.chunk_index || 0)
    );
  }
  if (mode === 'book-asc' || mode === 'book-desc') {
    const dir = mode === 'book-asc' ? 1 : -1;
    return notes.sort((a, b) => {
      const ta = (bookTitleMap[a.stable_book_id] || '').toLowerCase();
      const tb = (bookTitleMap[b.stable_book_id] || '').toLowerCase();
      return ta < tb ? -dir : ta > tb ? dir : 0;
    });
  }
  if (mode === 'length-desc' || mode === 'length-asc') {
    const dir = mode === 'length-desc' ? -1 : 1;
    return notes.sort((a, b) => dir * ((a.selected_text || '').length - (b.selected_text || '').length));
  }
  const asc = mode === 'time-asc';
  return notes.sort((a, b) => {
    const ta = a.time_updated || a.time_created || 0;
    const tb = b.time_updated || b.time_created || 0;
    return asc ? ta - tb : tb - ta;
  });
}

function setNotesSort(mode) {
  if (notesSortMode.startsWith(mode)) {
    if (mode === 'chapter') return;
    notesSortMode = notesSortMode.endsWith('-asc') ? mode + '-desc' : mode + '-asc';
  } else {
    if (mode === 'book') notesSortMode = 'book-asc';
    else if (mode === 'chapter') notesSortMode = 'chapter';
    else notesSortMode = mode + '-desc';
  }
  updateSortButtons();
  if (currentSubtab === 'notes') loadNotesList();
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
  if (btnBook) btnBook.innerHTML = mode === 'book-desc' ? '<i data-lucide="book-open"></i> Z->A' : '<i data-lucide="book-open"></i> A->Z';
  if (btnTime) btnTime.innerHTML = mode === 'time-asc' ? '<i data-lucide="clock"></i> 最早' : '<i data-lucide="clock"></i> 最新';
  if (btnLength) btnLength.innerHTML = mode === 'length-asc' ? '<i data-lucide="ruler"></i> 短->长' : '<i data-lucide="ruler"></i> 长->短';
  const bookFilter = document.getElementById('notesFilterBook').value;
  if (btnBook) btnBook.style.display = bookFilter ? 'none' : '';
}

// ── Search / filter ───────────────────────────────────────────

function onNotesSearchInput() {
  const input = document.getElementById('notesSearchInput');
  const clearBtn = document.getElementById('notesSearchClear');
  if (clearBtn) clearBtn.style.display = input.value ? '' : 'none';
  if (window._notesSearchTimer) clearTimeout(window._notesSearchTimer);
  window._notesSearchTimer = setTimeout(() => {
    if (currentSubtab === 'notes') loadNotesList();
    else loadBookmarksList();
  }, 300);
}

function clearNotesSearch() {
  document.getElementById('notesSearchInput').value = '';
  document.getElementById('notesSearchClear').style.display = 'none';
  if (currentSubtab === 'notes') loadNotesList();
  else loadBookmarksList();
}

function onBookFilterChange() {
  updateSortButtons();
  if (currentSubtab === 'notes') loadNotesList();
  else loadBookmarksList();
}

function onNotesFilterChange() {
  loadNotesList();
}

function renderNoteMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  html = html.replace(/\*(.+?)\*/g, '<i>$1</i>');
  html = html.replace(/`(.+?)`/g, '<code>$1</code>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

// ── Navigation ────────────────────────────────────────────────

function jumpToAnnotation(annId, bookId, chapterIdx, chunkIdx, cfi) {
  if (!bookId) {
    alert('此注释的原文在当前版本中已不存在。重新导入书籍后可恢复。');
    return;
  }
  window.switchTab('reader');
  if (S.currentBook !== bookId) {
    document.getElementById('bookSelect').value = bookId;
    window.selectBook().then(() => _navToAnnotation(chapterIdx, chunkIdx, cfi));
  } else {
    _navToAnnotation(chapterIdx, chunkIdx, cfi);
  }
}

function _navToAnnotation(chapterIdx, chunkIdx, cfi) {
  // native 模式 + CFI → 精确跳转；失败或无 CFI 退回 chunk 语义跳转
  if (S.renderMode === 'native' && cfi) {
    jumpToCfi(cfi).then(ok => {
      if (ok) return;
      alert('精确跳转失败，回退章节定位');
      navigateToChunk(chapterIdx, chunkIdx);
      window.loadChunk();
    });
    return;
  }
  navigateToChunk(chapterIdx, chunkIdx);
  window.loadChunk();
}

function navigateToChunk(chapterIdx, chunkIdx) {
  S.currentChapter = chapterIdx;
  document.getElementById('chapterSelect').value = chapterIdx;
  S.currentChunk = chunkIdx;
  document.getElementById('chunkPos').textContent = (chunkIdx + 1) + ' / ...';
  window.loadChunk();
}

// ── Export ────────────────────────────────────────────────────

function exportNotes() {
  const cards = document.querySelectorAll('#notesList .note-card');
  if (!cards.length) { alert('暂无笔记可导出'); return; }
  let md = '# 读书笔记\n\n';
  cards.forEach(card => {
    const quote = card.querySelector('.note-quote')?.textContent || '';
    const note = card.querySelector('.note-text')?.textContent || '';
    md += `> ${quote}\n\n`;
    if (note) md += `${note}\n\n`;
    md += `---\n\n`;
  });
  md += `\n导出时间: ${new Date().toLocaleString('zh-CN')}\n`;
  const blob = new Blob([md], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'reading-notes.md';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── Book filter ───────────────────────────────────────────────

async function loadBookFilter() {
  try {
    const resp = await fetch(S.API + '/api/books');
    const data = await resp.json();
    const sel = document.getElementById('notesFilterBook');
    sel.innerHTML = '<option value="">全部书籍</option>';
    bookTitleMap = {};
    if (data.books) {
      data.books.forEach(b => {
        const sid = b.stable_book_id;
        if (sid) {
          bookTitleMap[sid] = b.title;
          const opt = document.createElement('option');
          opt.value = sid;
          opt.textContent = b.title.slice(0, 25);
          sel.appendChild(opt);
        }
      });
    }
    updateSortButtons();
  } catch (e) {}
}

// ── Init ──────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function () {
  // Pre-create the mini panel (hidden until reader toolbar "📋 浮窗" click)
  getOrCreateMiniPanel();
  document.getElementById('miniNotesPanel').style.display = 'none';
});

// ── HTML onclick access ───────────────────────────────────────
window.openNotesPanel = openNotesPanel;
window.closeNotesPanel = closeNotesPanel;
window.setMiniPanelVisible = setMiniPanelVisible;
window.switchNotesSubtab = switchNotesSubtab;
window.renderNotesPage = renderNotesPage;
window.onNotesSearchInput = onNotesSearchInput;
window.clearNotesSearch = clearNotesSearch;
window.onBookFilterChange = onBookFilterChange;
window.onNotesFilterChange = onNotesFilterChange;
window.setNotesSort = setNotesSort;
window.jumpToAnnotation = jumpToAnnotation;
window.loadNotesList = loadNotesList;
window.exportNotes = exportNotes;
