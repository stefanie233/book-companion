/* Book Companion — sentence search (two modes: proximity + chapter). */

import { S } from './state.js';
import { escapeHtml, getChapterName } from './utils.js';
import { loadChunk } from './reader.js';
import * as EpubReader from './epub-reader.js';

// ── Mode toggle ───────────────────────────────────────────────────

export function updateSearchButtonStates() {
  const btnProx = document.getElementById('btnProximitySearch');
  const btnCh = document.getElementById('btnChapterSearch');
  const proxActive = S.searchPanelOpen && S.searchMode === 'proximity';
  const chActive = S.searchPanelOpen && S.searchMode === 'chapter';
  btnProx.classList.toggle('active', proxActive);
  btnCh.classList.toggle('active', chActive);
}

export function setSearchMode(mode) {
  const resultsEl = document.getElementById('searchResults');
  const infoEl = document.getElementById('searchInfo');

  // Toggle: clicking the active mode again → close panel, restore default
  if (S.searchPanelOpen && mode === S.searchMode) {
    S.searchPanelOpen = false;
    resultsEl.classList.remove('visible');
    infoEl.classList.remove('visible');
    if (mode === 'chapter') {
      S.searchMode = 'proximity';
      S.selectedSearchChapter = null;
    }
    updateSearchButtonStates();
    return;
  }

  // Switch to (or activate) the requested mode
  S.searchMode = mode;
  S.searchPanelOpen = true;
  S.selectedSearchChapter = null;
  updateSearchButtonStates();

  // Re-run search if there's a query
  const query = document.getElementById('searchInput').value.trim();
  if (query) {
    searchBook();
  } else {
    if (mode === 'chapter') {
      showChapterSelector();
    } else {
      resultsEl.classList.add('visible');
      infoEl.classList.remove('visible');
      document.getElementById('searchLeft').innerHTML =
        '<div class="search-none">输入关键词开始搜索</div>';
      document.getElementById('searchRight').innerHTML =
        '<div class="search-none">输入关键词开始搜索</div>';
    }
  }
}

// ── Main search ───────────────────────────────────────────────────

export async function searchBook() {
  const input = document.getElementById('searchInput');
  const query = input.value.trim();
  const resultsEl = document.getElementById('searchResults');
  const infoEl = document.getElementById('searchInfo');

  if (!query) {
    resultsEl.classList.remove('visible');
    infoEl.classList.remove('visible');
    S.searchPanelOpen = false;
    updateSearchButtonStates();
    return;
  }
  if (!S.currentBook) return;

  S.searchPanelOpen = true;
  updateSearchButtonStates();
  S.searchQuery = query;
  await loadSearchOverview();
}

// ── Load search results ───────────────────────────────────────────

async function loadSearchOverview() {
  const resultsEl = document.getElementById('searchResults');
  const infoEl = document.getElementById('searchInfo');
  const leftEl = document.getElementById('searchLeft');
  const rightEl = document.getElementById('searchRight');

  resultsEl.classList.add('visible');
  infoEl.classList.remove('visible');
  rightEl.innerHTML = '<div class="search-none">加载中...</div>';
  leftEl.innerHTML = '';

  try {
    const overviewUrl = S.API + '/api/books/' + S.currentBook + '/search/overview?q='
      + encodeURIComponent(S.searchQuery) + '&current=' + S.currentChapter;
    const resp = await fetch(overviewUrl);
    const data = await resp.json();
    if (data.error) {
      rightEl.innerHTML = '<div class="search-none">' + data.error + '</div>';
      leftEl.innerHTML = '';
      return;
    }

    let chapters = data.chapters || [];
    S.searchChapterList = chapters;

    infoEl.textContent = '共 ' + data.total_hits + ' 处命中，分布在 ' + chapters.length + ' 章';
    infoEl.classList.add('visible');

    if (!chapters.length) {
      rightEl.innerHTML = '<div class="search-none">未找到</div>';
      leftEl.innerHTML = '<div class="search-none">未找到 "' + escapeHtml(S.searchQuery) + '"</div>';
      return;
    }

    renderChapterPanel(chapters, S.selectedSearchChapter);

    if (S.searchMode === 'proximity') {
      const nearest = chapters[0].chapter_index;
      await loadChapterSentences(nearest);
    } else {
      await loadChapterSentences(S.selectedSearchChapter);
    }
  } catch (e) {
    rightEl.innerHTML = '<div class="search-none" style="color:#ff6b6b">搜索失败</div>';
    leftEl.innerHTML = '';
  }
}

// ── Chapter panel (right side) ────────────────────────────────────

function renderChapterPanel(chapters, activeChIdx) {
  const rightEl = document.getElementById('searchRight');
  let header;
  if (S.searchMode === 'chapter' && activeChIdx === null) {
    header = '<i data-lucide="book-open"></i> 全文顺序 (点击章节缩小)';
  } else if (S.searchMode === 'chapter') {
    header = '\u{1F4CB} 已选章节 (点击切换)';
  } else {
    header = '\u{1F4D1} 章节命中 (就近)';
  }

  let items = '';
  if (S.searchMode === 'chapter') {
    const allActive = activeChIdx === null;
    items += '<div class="search-chapter-item' + (allActive ? ' active' : '') + '"'
      + ' onclick="selectSearchChapter(null)">'
      + '<span class="ci-name"><i data-lucide="book-open"></i> 全文顺序</span>'
      + '<span class="ci-count">ALL</span>'
      + '</div>';
  }
  items += chapters.map(ch => {
    const chName = ch.chapter_title
      ? ch.chapter_title.slice(0, 16)
      : '第' + ch.chapter_index + '章';
    const isActive = ch.chapter_index === activeChIdx;
    const isCurrent = ch.chapter_index === S.currentChapter;
    return '<div class="search-chapter-item' + (isActive ? ' active' : '') + '"'
      + ' onclick="selectSearchChapter(' + ch.chapter_index + ')">'
      + '<span class="ci-name">' + escapeHtml(chName) + '</span>'
      + (isCurrent ? '<span class="ci-current">当前</span>' : '')
      + '<span class="ci-count">' + ch.hit_count + '</span>'
      + '</div>';
  }).join('');

  rightEl.innerHTML = '<div class="search-panel-header">' + header + '</div>' + items;
}

// ── Click a chapter in the right panel ────────────────────────────

export async function selectSearchChapter(chIdx) {
  S.selectedSearchChapter = chIdx;
  renderChapterPanel(S.searchChapterList, chIdx);
  S.chapterSentenceOffset = 0;
  await loadChapterSentences(chIdx);
}

// ── Load sentences (left panel) ───────────────────────────────────
// chIdx === null  → sequential full-book search
// chIdx === int   → single chapter

async function loadChapterSentences(chIdx) {
  const leftEl = document.getElementById('searchLeft');
  leftEl.innerHTML = '<div class="search-none">加载中...</div>';
  S.chapterSentenceOffset = 0;
  S.searchLoading = false;

  try {
    const url = buildSentenceUrl(chIdx, 0, 20);
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) {
      leftEl.innerHTML = '<div class="search-none">' + data.error + '</div>';
      return;
    }
    S.searchHasMore = data.has_more;
    S.searchTotal = data.total;
    renderSentencePanel(data.sentences || [], chIdx, data.has_more, data.total, true);
  } catch (e) {
    leftEl.innerHTML = '<div class="search-none" style="color:#ff6b6b">加载失败</div>';
  }
}

function buildSentenceUrl(chIdx, offset, limit) {
  let url = S.API + '/api/books/' + S.currentBook + '/search/chapter?q='
    + encodeURIComponent(S.searchQuery) + '&offset=' + offset + '&limit=' + limit;
  if (chIdx !== null) {
    url += '&chapter=' + chIdx;
  }
  return url;
}

function renderSentencePanel(sentences, chIdx, hasMore, total, isFresh) {
  const leftEl = document.getElementById('searchLeft');

  const panelTitle = chIdx === null
    ? '<i data-lucide="book-open"></i> 全文顺序 · ' + total + ' 处'
    : escapeHtml(getChapterName(chIdx)) + ' · ' + total + ' 处';

  const html = sentences.map(r => buildSentenceHtml(r)).join('');

  if (isFresh) {
    let content = '<div class="search-panel-header">' + panelTitle + '</div>';
    if (!sentences.length) {
      content += '<div class="search-none">' + (chIdx === null ? '未找到' : '本章未找到') + '</div>';
    } else {
      content += html;
    }
    if (hasMore) {
      const shown = sentences.length;
      content += '<div class="search-load-more" onclick="loadMoreSentences('
        + (chIdx === null ? 'null' : chIdx) + ')">加载更多 (' + shown + '/' + total + ')</div>';
    }
    leftEl.innerHTML = content;
  } else {
    // Append mode
    const oldBtn = leftEl.querySelector('.search-load-more');
    if (oldBtn) oldBtn.remove();
    leftEl.insertAdjacentHTML('beforeend', html);
    if (hasMore) {
      const shown = S.chapterSentenceOffset + sentences.length;
      leftEl.insertAdjacentHTML('beforeend',
        '<div class="search-load-more" onclick="loadMoreSentences('
        + (chIdx === null ? 'null' : chIdx) + ')">加载更多 (' + shown + '/' + total + ')</div>');
    }
  }
}

function buildSentenceHtml(r) {
  const sent = escapeHtml(r.sentence);
  const lowerSent = r.sentence.toLowerCase();
  const lowerQ = S.searchQuery.toLowerCase();
  const idx = lowerSent.indexOf(lowerQ);
  let hlSent = sent;
  if (idx >= 0) {
    const before = sent.slice(0, idx);
    const match = sent.slice(idx, idx + S.searchQuery.length);
    const after = sent.slice(idx + S.searchQuery.length);
    hlSent = before + '<mark class="search-hl">' + match + '</mark>' + after;
  }
  const chName = r.chapter_title
    ? r.chapter_title.slice(0, 12)
    : '第' + r.chapter_index + '章';
  return '<div class="search-result-item" onclick="jumpToSearchResult('
    + r.chapter_index + ',' + r.chunk_index + ')" title="点击跳转">'
    + '<div class="ch-label"><span>' + escapeHtml(chName) + '</span> · 段落' + (r.chunk_index + 1) + '</div>'
    + hlSent
    + '</div>';
}

// ── Load more ─────────────────────────────────────────────────────

export async function loadMoreSentences(chIdx) {
  if (S.searchLoading || !S.searchHasMore) return;
  S.searchLoading = true;
  S.chapterSentenceOffset += 20;

  const leftEl = document.getElementById('searchLeft');
  const oldBtn = leftEl.querySelector('.search-load-more');
  if (oldBtn) oldBtn.textContent = '加载中...';

  try {
    const url = buildSentenceUrl(chIdx, S.chapterSentenceOffset, 20);
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) { S.searchLoading = false; return; }

    S.searchHasMore = data.has_more;
    S.searchTotal = data.total;
    renderSentencePanel(data.sentences || [], chIdx, data.has_more, data.total, false);
  } catch (e) {
    // silent fail
  }
  S.searchLoading = false;
}

// ── Scroll-based lazy loading ─────────────────────────────────────

export function onSearchLeftScroll() {
  if (S.searchLoading || !S.searchHasMore) return;
  const leftEl = document.getElementById('searchLeft');
  if (leftEl.scrollTop + leftEl.clientHeight >= leftEl.scrollHeight - 60) {
    loadMoreSentences(S.selectedSearchChapter);
  }
}

// ── Chapter selector (mode 2: pick chapter before search) ─────────

function showChapterSelector() {
  const resultsEl = document.getElementById('searchResults');
  const infoEl = document.getElementById('searchInfo');
  const leftEl = document.getElementById('searchLeft');
  const rightEl = document.getElementById('searchRight');

  const chSelect = document.getElementById('chapterSelect');
  const options = chSelect ? chSelect.options : [];

  let html = '<div class="search-panel-header">\u{1F4CB} 选择章节 (可选)</div>';
  html += '<div class="search-chapter-select selected"'
    + ' onclick="pickSearchChapter(null)">'
    + '● <i data-lucide="book-open"></i> 全文顺序搜索</div>';
  for (let i = 0; i < options.length; i++) {
    const val = parseInt(options[i].value);
    const text = options[i].text;
    html += '<div class="search-chapter-select"'
      + ' onclick="pickSearchChapter(' + val + ')">'
      + '○ '
      + escapeHtml(text)
      + '</div>';
  }

  leftEl.innerHTML = '<div class="search-none">输入关键词搜索，或点击右侧选择章节</div>';
  rightEl.innerHTML = html;
  S.searchPanelOpen = true;
  updateSearchButtonStates();
  resultsEl.classList.add('visible');
  infoEl.classList.remove('visible');
}

export function pickSearchChapter(chIdx) {
  S.selectedSearchChapter = chIdx;
  showChapterSelector();
  const query = document.getElementById('searchInput').value.trim();
  if (query) {
    loadSearchOverview();
  }
}

// ── Jump ──────────────────────────────────────────────────────────

export function jumpToSearchResult(chapterIdx, chunkIdx) {
  document.getElementById('searchResults').classList.remove('visible');
  document.getElementById('searchInfo').classList.remove('visible');
  S.currentChapter = chapterIdx;
  document.getElementById('chapterSelect').value = chapterIdx;
  S.currentChunk = chunkIdx;
  document.getElementById('chunkPos').textContent = (chunkIdx + 1) + ' / ...';
  if (S.renderMode === 'native') {
    // 原生模式：跳到语义坐标 (chapter, chunk) 对应位置
    EpubReader.jumpToAnchor(chapterIdx, chunkIdx);
    return;
  }
  loadChunk();
  setTimeout(highlightSearchTerm, 300);
}

function highlightSearchTerm() {
  const query = document.getElementById('searchInput').value.trim();
  if (!query) return;
  const page = document.getElementById('bookPage');
  const walker = document.createTreeWalker(page, NodeFilter.SHOW_TEXT, null, false);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const idx = node.textContent.toLowerCase().indexOf(query.toLowerCase());
    if (idx >= 0) {
      const before = node.textContent.slice(0, idx);
      const match = node.textContent.slice(idx, idx + query.length);
      const after = node.textContent.slice(idx + query.length);
      const frag = document.createDocumentFragment();
      if (before) frag.appendChild(document.createTextNode(before));
      const mark = document.createElement('mark');
      mark.className = 'search-hl';
      mark.textContent = match;
      frag.appendChild(mark);
      if (after) frag.appendChild(document.createTextNode(after));
      node.parentNode.replaceChild(frag, node);
      break;
    }
  }
}

// ── HTML onclick access ───────────────────────────────────────────
window.setSearchMode = setSearchMode;
window.searchBook = searchBook;
window.selectSearchChapter = selectSearchChapter;
window.loadMoreSentences = loadMoreSentences;
window.pickSearchChapter = pickSearchChapter;
window.jumpToSearchResult = jumpToSearchResult;
