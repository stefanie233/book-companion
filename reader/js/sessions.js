/* Liubai — session (chat history) management. */

import { S } from './state.js';
import { escapeHtml, formatSessionTime } from './utils.js';
import { getWelcomeMessage } from './greetings.js';
import { addChat, createVoiceBubble, createThinkRow, createInterruptNote,
         stickChatToBottom, renderTurnTraceFromSteps } from './chat.js';
import { refreshUsage } from './context-usage.js';

const SESSION_MODE_LABELS = {
  immersive: '沉浸',
  overview: '俯瞰',
  divergent: '发散',
  roleplay: '扮演',
  writing: '写作'
};

// 写作页这条线上全是写作会话，meta 里的「写作」没有信息量：换成会话
// 绑定的作品名（没绑作品的归「空项目」）。其它页面照旧显示模式。
function sessionModeLabel(s) {
  if (s.mode === 'writing' && isWritingLine()) return s.work_title || '空项目';
  return SESSION_MODE_LABELS[s.mode] || s.mode;
}

// ── Sub-nav state ─────────────────────────────────────────────────
let _cachedSessions = [];
let _currentSubnavTab = 'all';    // 'all' | 'books' | 'characters' | 'writing-all' | 'writing-works'
let _drillBookId = null;          // null = show group list; string = drilled into a specific group
let _drillCharName = null;        // null = show character list; string = drilled into a specific character
let _drillWorkId = null;          // 写作页：null = 作品分组列表；string = 已进入某本/'__empty__'
let _searchTerm = '';             // current search query
let _searchTimeout = null;        // debounce timer
let _listOffset = 0;              // pagination offset
let _listHasMore = true;          // whether there are more sessions to load
const _pageSize = 50;

// Click-vs-dblclick arbitration: a click on a session item schedules a
// delayed load; a dblclick on the title cancels the pending load so a
// rename intent doesn't also import the session into the chat view.
var _pendingClickLoad = null;      // setTimeout handle
const _clickLoadDelayMs = 250;

// Monotonic load token. Two loads can be in flight at once (rapid branch
// switching, restoreViewState + a click); only the newest may write state.
// Without it the *last response* wins instead of the *last click*, and the
// rendered transcript can end up paired with another session's id.
let _loadSeq = 0;

function scheduleClickLoad(sid) {
  if (_pendingClickLoad) clearTimeout(_pendingClickLoad);
  _pendingClickLoad = setTimeout(function () {
    _pendingClickLoad = null;
    loadSession(sid);
  }, _clickLoadDelayMs);
}

function cancelPendingClickLoad() {
  if (_pendingClickLoad) {
    clearTimeout(_pendingClickLoad);
    _pendingClickLoad = null;
  }
}

// Tree-state for fork hierarchy
const _childrenCache = new Map();    // sessionId -> SessionInfo[]
const _expandedSessions = new Set(); // expanded session ids

// 会话导航栏打开时默认落在哪个二级页签 —— 跟着当前界面走。
// 三个界面的会话线各自独立（见 .claude/docs/reader.md 决策 84），
// 打开面板先看见自己那批，少点一次切换。
function defaultSubnavTab() {
  if (window._currentTab === 'reader') return 'books';
  if (window._currentTab === 'write') return 'writing-works';
  return 'all';
}

export function toggleSessionPanel() {
  const panel = document.getElementById('sessionPanel');
  if (panel.classList.contains('visible')) {
    panel.classList.remove('visible');
  } else {
    panel.classList.add('visible');
    // Reset sub-nav to default on open：每个界面预先选中自己那条会话线 ——
    // 书架页看「全部」，阅读页看「按书籍」，写作页看「按作品」。
    // 只影响打开时的预选页签，页签顺序不变，其他页签也不锁。
    _currentSubnavTab = defaultSubnavTab();
    _drillBookId = null;
    _drillCharName = null;
    _drillWorkId = null;
    _searchTerm = '';
    _listOffset = 0;
    _listHasMore = true;
    var searchInput = document.getElementById('sessionSearchInput');
    if (searchInput) searchInput.value = '';
    applySessionChrome();
    updateSubnavActive();
    loadSessionList(true);
  }
}

/** 写作页的会话面板只留「全部 / 按作品」两个页签，别的页签不适用。 */
function isWritingLine() {
  return window._currentTab === 'write';
}

function isWritingTab(tab) {
  return tab === 'writing-all' || tab === 'writing-works';
}

function applySessionChrome() {
  const nav = document.getElementById('sessionSubnav');
  if (!nav) return;
  // 用 class 而不是 hidden：.session-subnav 自带 display:flex，
  // 会盖掉 UA 的 [hidden]{display:none}（之前页签就是这么漏出来的）。
  nav.classList.toggle('is-writing', isWritingLine());
}

/**
 * 切页时调用：页签跟着界面走；面板若正开着，按新页面重新取一遍会话——
 * 否则在写作页打开的列表会留在主页面上，写作会话就漏出来了。
 */
function refreshSessionChrome() {
  applySessionChrome();
  const panel = document.getElementById('sessionPanel');
  if (!panel || !panel.classList.contains('visible')) return;
  _currentSubnavTab = defaultSubnavTab();
  _drillBookId = null;
  _drillCharName = null;
  _drillWorkId = null;
  _searchTerm = '';
  _listOffset = 0;
  _listHasMore = true;
  var searchInput = document.getElementById('sessionSearchInput');
  if (searchInput) searchInput.value = '';
  updateSubnavActive();
  loadSessionList(true);
}

window.refreshSessionChrome = refreshSessionChrome;

// ── Sub-nav tab switching ─────────────────────────────────────────

export function switchSubnavTab(tab) {
  _currentSubnavTab = tab;
  _drillBookId = null;
  _drillCharName = null;
  _drillWorkId = null;
  _searchTerm = '';
  var searchInput = document.getElementById('sessionSearchInput');
  if (searchInput) searchInput.value = '';
  updateSubnavActive();
  renderSessionView();
}

function updateSubnavActive() {
  const tabs = document.querySelectorAll('#sessionSubnav .subnav-tab');
  tabs.forEach(t => {
    t.classList.toggle('active', t.dataset.tab === _currentSubnavTab);
  });
}

// ── Fetch & render ────────────────────────────────────────────────

export async function loadSessionList(reset) {
  // Build search bar on first call
  if (!document.getElementById('sessionSearchRow')) {
    var listEl = document.getElementById('sessionList');
    var searchRow = document.createElement('div');
    searchRow.id = 'sessionSearchRow';
    searchRow.className = 'session-search-row';
    searchRow.innerHTML = '<input id="sessionSearchInput" class="session-search-input" placeholder="搜索会话内容..." autocomplete="off">';
    listEl.parentNode.insertBefore(searchRow, listEl);
    document.getElementById('sessionSearchInput').addEventListener('input', function () {
      _searchTerm = this.value.trim();
      clearTimeout(_searchTimeout);
      _searchTimeout = setTimeout(function () { loadSessionList(true); }, 300);
    });
  }

  var listEl = document.getElementById('sessionList');
  // Show spinner only on reset; "load more" shows the button itself
  if (reset) {
    _listOffset = 0;
    _listHasMore = true;
    _childrenCache.clear();
    _expandedSessions.clear();
    listEl.innerHTML = '<div class="session-empty">加载中...</div>';
  }

  // 会话按页面分线（决策 84）：写作页只取写作会话，其它页把写作会话
  // 整个排除——列表和搜索都要带这个条件，搜索不能成为后门。
  var lineParam = isWritingLine() ? '&mode=writing' : '&exclude_mode=writing';

  // ── Search path ──────────────────────────────────────────────
  if (_searchTerm) {
    try {
      var searchResp = await fetch(S.API + '/api/sessions/search?q=' + encodeURIComponent(_searchTerm) + '&limit=30' + lineParam);
      var searchData = await searchResp.json();
      if (searchData.error) {
        listEl.innerHTML = '<div class="session-empty">' + escapeHtml(searchData.error) + '</div>';
        return;
      }
      var results = searchData.results || [];
      if (!results.length) {
        listEl.innerHTML = '<div class="session-empty">未找到匹配的会话</div>';
        return;
      }
      // Merge with cached sessions for metadata
      var cacheById = {};
      _cachedSessions.forEach(function (s) { cacheById[s.id] = s; });
      listEl.innerHTML = results.map(function (r) {
        var cached = cacheById[r.session_id];
        // 写作页的搜索结果：作品名放 meta 行，标题上不挂胶囊
        var label = cached && !isWritingLine() ? (cached.book_title || '') : '';
        var modeText = (r.mode === 'writing' && isWritingLine())
          ? ((cached && cached.work_title) || '空项目')
          : (SESSION_MODE_LABELS[r.mode] || r.mode);
        var snippetHtml = (r.snippets || []).map(function (s) {
          return '<div class="si-snippet">' + escapeHtml(s) + '</div>';
        }).join('');
        return '<div class="session-item' + (r.session_id === S.currentSessionId ? ' active' : '') + '"'
          + ' onclick="loadSession(\'' + r.session_id + '\')">'
          + (label ? '<span class="si-book">' + escapeHtml(label) + '</span>' : '')
          + (cached && cached.character_name ? '<span class="si-char"><i data-lucide="drama"></i> ' + escapeHtml(cached.character_name) + '</span>' : '')
          + '<div class="si-title"><span class="si-title-text">' + escapeHtml(r.title || '(无标题)') + '</span></div>'
          + snippetHtml
          + '<div class="si-meta">'
          + '<span class="si-mode">' + escapeHtml(modeText) + '</span>'
          + '<span>' + (r.time_updated ? formatSessionTime(r.time_updated) : '') + '</span>'
          + '</div></div>';
      }).join('');
      return;
    } catch (e) {
      listEl.innerHTML = '<div class="session-empty">搜索失败</div>';
      return;
    }
  }

  // ── Normal list path (with pagination) ────────────────────────
  try {
    var resp = await fetch(S.API + '/api/sessions/list?limit=' + _pageSize + '&offset=' + _listOffset + '&root_only=true' + lineParam);
    var data = await resp.json();
    if (data.error) {
      listEl.innerHTML = '<div class="session-empty">' + escapeHtml(data.error) + '</div>';
      return;
    }
    var sessions = data.sessions || [];
    _listHasMore = sessions.length >= _pageSize;
    if (reset) {
      _cachedSessions = sessions;
    } else {
      // Append: deduplicate by id
      var existingIds = {};
      _cachedSessions.forEach(function (s) { existingIds[s.id] = true; });
      sessions.forEach(function (s) {
        if (!existingIds[s.id]) { _cachedSessions.push(s); existingIds[s.id] = true; }
      });
    }
    renderSessionView();
  } catch (e) {
    listEl.innerHTML = '<div class="session-empty">加载失败</div>';
  }
}

function renderSessionView() {
  // 写作页只有自己那组页签（全部 / 按作品）；若状态还停在别的页签上
  // （切页时面板关着就走了捷径），先归位再渲染。
  if (isWritingLine()) {
    if (!isWritingTab(_currentSubnavTab)) {
      _currentSubnavTab = 'writing-works';
      updateSubnavActive();
    }
    renderWritingTab();
    return;
  }
  if (isWritingTab(_currentSubnavTab)) {
    _currentSubnavTab = 'all';
    updateSubnavActive();
  }
  if (_currentSubnavTab === 'all') {
    renderAllSessions();
  } else if (_currentSubnavTab === 'characters') {
    renderCharactersTab();
  } else {
    renderBooksTab();
  }
}

// ── "全部" tab — flat list with book labels ─────────────────────

function renderAllSessions() {
  var listEl = document.getElementById('sessionList');
  // 写作会话不属于主页/阅读页的「全部」；服务端已经排除，这里再兜一层
  //（面板在切换页签前缓存过写作列表时也不会漏出来）。
  var rows = _cachedSessions.filter(function (s) { return s.mode !== 'writing'; });
  if (!rows.length) {
    listEl.innerHTML = '<div class="session-empty">暂无历史会话</div>';
    return;
  }
  var html = rows.map(function (s) { return renderSessionItem(s); }).join('');
  // "Load more" button when there are more sessions to fetch
  if (_listHasMore && !_searchTerm) {
    html += '<div class="session-load-more" onclick="loadMoreSessions()">── 加载更多 ──</div>';
  }
  listEl.innerHTML = html;
}

function loadMoreSessions() {
  _listOffset = _cachedSessions.length;
  loadSessionList(false);
}

// ── 写作页：按作品收录的会话 ─────────────────────────────────────
// 写作页只留两个页签：「全部」是这条会话线的平铺列表（按时间，每行带
// 作品标签），「按作品」是作品卡片，点进去才是这一本的对话。会话绑在
// 作品上（sessions.work_id）；没绑作品的老会话与「空项目」里聊出来的
// 会话归到「空项目」组。

function writingGroupKey(s) {
  return s.work_id || '__empty__';
}

function renderWritingTab() {
  var listEl = document.getElementById('sessionList');
  var rows = _cachedSessions.filter(function (s) { return s.mode === 'writing'; });
  if (!rows.length) {
    listEl.innerHTML = '<div class="session-empty">暂无写作会话</div>';
    return;
  }

  // 「全部」：写作页这条线上的所有会话，平铺按时间倒序
  if (_currentSubnavTab === 'writing-all') {
    var flat = rows.slice().sort(function (a, b) {
      return (b.time_updated || 0) - (a.time_updated || 0);
    }).map(function (s) { return renderSessionItem(s); }).join('');
    if (_listHasMore && !_searchTerm) {
      flat += '<div class="session-load-more" onclick="loadMoreSessions()">── 加载更多 ──</div>';
    }
    listEl.innerHTML = flat;
    return;
  }

  var groups = new Map();
  rows.forEach(function (s) {
    var key = writingGroupKey(s);
    if (!groups.has(key)) {
      groups.set(key, {
        key: key,
        title: key === '__empty__' ? '空项目' : (s.work_title || '未知作品'),
        sessions: []
      });
    }
    groups.get(key).sessions.push(s);
  });

  // 钻进去：这一本（或空项目）的对话列表
  if (_drillWorkId !== null) {
    var group = groups.get(_drillWorkId);
    var label = group ? group.title
      : (_drillWorkId === '__empty__' ? '空项目' : '作品');
    var members = group ? group.sessions : [];
    var html = '<div class="session-back" onclick="drillWorkOut()"><i data-lucide="arrow-left"></i> 返回作品列表<span style="color:var(--text-muted);margin-left:8px;font-size:0.6875rem;">'
      + escapeHtml(label) + ' · ' + members.length + '条会话</span></div>';
    if (!members.length) {
      html += '<div class="session-empty">暂无会话</div>';
    } else {
      html += members.map(function (s) { return renderSessionItem(s); }).join('');
    }
    if (_listHasMore && !_searchTerm) {
      html += '<div class="session-load-more" onclick="loadMoreSessions()">── 加载更多 ──</div>';
    }
    listEl.innerHTML = html;
    return;
  }

  // 分组列表：最近聊过的作品在前，空项目垫底
  var sorted = Array.from(groups.values()).sort(function (a, b) {
    if (a.key === '__empty__') return 1;
    if (b.key === '__empty__') return -1;
    return (b.sessions[0].time_updated || 0) - (a.sessions[0].time_updated || 0);
  });
  var cards = sorted.map(function (g) {
    var icon = g.key === '__empty__'
      ? '<i data-lucide="sparkles"></i>'
      : '<i data-lucide="pen-line"></i>';
    return '<div class="session-book-group" onclick="drillWorkIn(\''
      + escapeHtml(g.key) + '\')">'
      + '<span class="sbg-icon">' + icon + '</span>'
      + '<div class="sbg-info">'
      + '<div class="sbg-title">' + escapeHtml(g.title) + '</div>'
      + '<div class="sbg-count">' + g.sessions.length + '条会话</div>'
      + '</div>'
      + '<span class="sbg-arrow">›</span>'
      + '</div>';
  }).join('');
  if (_listHasMore && !_searchTerm) {
    cards += '<div class="session-load-more" onclick="loadMoreSessions()">── 加载更多 ──</div>';
  }
  listEl.innerHTML = cards;
}

function renderSessionItem(s, depth) {
  depth = depth || 0;
  var title = escapeHtml(s.title || '(无标题)');
  var modeLabel = escapeHtml(sessionModeLabel(s));
  var isActive = s.id === S.currentSessionId;
  var time = formatSessionTime(s.time_updated);
  // Book label — show for sessions with project_id, "主会话" for home-page sessions.
  // 写作会话不属于任何书：旧库里的 project_id 是历史遗留，不展示；
  // 作品名放 meta 行（替换「写作」），不在标题上再挂一个胶囊。
  var bookLabel = s.mode === 'writing'
    ? ''
    : (s.book_title
      ? '<span class="si-book">' + escapeHtml(s.book_title) + '</span>'
      : (s.project_id ? '' : '<span class="si-book si-book-home">主会话</span>'));
  // Character badge for role-play sessions
  var charBadge = s.character_name
    ? '<span class="si-char"><i data-lucide="drama"></i> ' + escapeHtml(s.character_name) + '</span>'
    : '';

  // Tree expand indicator (only show if session has fork children)
  var hasChildren = (s.children_count && s.children_count > 0)
    || (_childrenCache.has(s.id) && _childrenCache.get(s.id).length > 0);
  var isExpanded = _expandedSessions.has(s.id);
  var expandBtn = hasChildren
    ? '<button class="si-expand" onclick="event.stopPropagation();toggleChildren(\'' + s.id + '\')" title="展开/折叠分叉">' + (isExpanded ? '<i data-lucide="chevron-down"></i>' : '<i data-lucide="play"></i>') + '</button>'
    : '<span class="si-expand-placeholder"></span>';

  var html = '<div class="session-item' + (isActive ? ' active' : '') + (depth > 0 ? ' si-child' : '') + '"'
    + ' style="--child-depth:' + depth + '"'
    + ' title="快速双击进行重命名"'
    + ' onclick="scheduleClickLoad(\'' + s.id + '\')">'
    + (bookLabel ? bookLabel : '')
    + (charBadge ? charBadge : '')
    // title 只能是纯文本：属性里塞 <i data-lucide> 会在第二个引号处截断，
    // 后半截变成行内可见的文字。
    + '<div class="si-title" ondblclick="event.stopPropagation();cancelPendingClickLoad();startRename(event,\'' + s.id + '\')">'
    + expandBtn
    + '<span class="si-title-text">' + title + '</span>'
    + '<span class="si-rename-btn" onclick="event.stopPropagation();startRename(event,\'' + s.id + '\')" title="快速双击进行重命名"><i data-lucide="pencil"></i></span>'
    + '</div>'
    + '<div class="si-meta">'
    + '<span class="si-mode">' + modeLabel + '</span>'
    + '<span>' + s.message_count + '条</span>'
    + '<span>' + time + '</span>'
    + '<button class="si-archive" onclick="event.stopPropagation();archiveSession(event,\'' + s.id + '\')" title="归档"><i data-lucide="archive"></i></button>'
    + '<button class="si-del" onclick="deleteSession(event,\'' + s.id + '\')" title="删除"><i data-lucide="x"></i></button>'
    + '</div>'
    + '</div>';

  // Recursively render expanded children
  if (isExpanded && _childrenCache.has(s.id)) {
    html += _childrenCache.get(s.id).map(function (c) { return renderSessionItem(c, depth + 1); }).join('');
  }

  return html;
}

// ── "按书籍" tab — grouped book cards / drill-down ──────────────

function renderBooksTab() {
  const listEl = document.getElementById('sessionList');

  // 写作会话归「写作」页签，不参与书籍分组（旧库里它们可能带 project_id）。
  const readingSessions = _cachedSessions.filter(s => s.mode !== 'writing');

  // Drill-down: showing sessions for a specific book
  if (_drillBookId !== null) {
    const bookSessions = readingSessions.filter(s => {
      const pid = s.project_id || '';
      if (_drillBookId === '__home__') return !pid;
      return pid === _drillBookId;
    });
    const label = _drillBookId === '__home__' ? '主会话' : (bookSessions[0]?.book_title || '书籍');
    let html = '<div class="session-back" onclick="drillBookOut()"><i data-lucide="arrow-left"></i> 返回书籍列表<span style="color:var(--text-muted);margin-left:8px;font-size:0.6875rem;">'
      + escapeHtml(label) + ' · ' + bookSessions.length + '条会话</span></div>';
    if (!bookSessions.length) {
      html += '<div class="session-empty">暂无会话</div>';
    } else {
      html += bookSessions.map(s => renderSessionItem(s)).join('');
    }
    listEl.innerHTML = html;
    return;
  }

  // Book list: group sessions by project_id
  if (!readingSessions.length) {
    listEl.innerHTML = '<div class="session-empty">暂无阅读会话</div>';
    return;
  }

  const groups = new Map();  // key → { title, sessions[] }
  for (const s of readingSessions) {
    const pid = s.project_id || '';
    const key = pid || '__home__';
    if (!groups.has(key)) {
      groups.set(key, {
        title: pid ? (s.book_title || '未知书籍') : '主会话',
        pid: pid,
        sessions: []
      });
    }
    groups.get(key).sessions.push(s);
  }

  // Sort: main sessions last, then by title
  const sorted = [...groups.values()].sort((a, b) => {
    if (a.pid === '') return 1;
    if (b.pid === '') return -1;
    return a.title.localeCompare(b.title, 'zh');
  });

  listEl.innerHTML = sorted.map(g => {
    const icon = g.pid ? '<i data-lucide="book-open"></i>' : '<i data-lucide="message-square"></i>';
    const safePid = g.pid || '__home__';
    return '<div class="session-book-group" onclick="drillBookIn(\'' + safePid + '\')">'
      + '<span class="sbg-icon">' + icon + '</span>'
      + '<div class="sbg-info">'
      + '<div class="sbg-title">' + escapeHtml(g.title) + '</div>'
      + '<div class="sbg-count">' + g.sessions.length + '条会话</div>'
      + '</div>'
      + '<span class="sbg-arrow">›</span>'
      + '</div>';
  }).join('');
}

// ── "按角色" tab — grouped character cards / drill-down ──────────

function renderCharactersTab() {
  var listEl = document.getElementById('sessionList');

  // Drill-down: showing sessions for a specific character
  if (_drillCharName !== null) {
    var charSessions = _cachedSessions.filter(function (s) {
      return (s.character_name || '') === _drillCharName;
    });
    var html = '<div class="session-back" onclick="drillCharOut()"><i data-lucide="arrow-left"></i> 返回角色列表<span style="color:var(--text-muted);margin-left:8px;font-size:0.6875rem;">'
      + escapeHtml(_drillCharName) + ' · ' + charSessions.length + '条会话</span></div>';
    if (!charSessions.length) {
      html += '<div class="session-empty">暂无会话</div>';
    } else {
      html += charSessions.map(function (s) { return renderSessionItem(s); }).join('');
    }
    listEl.innerHTML = html;
    return;
  }

  // Character list: group sessions by character_name
  var roleSessions = _cachedSessions.filter(function (s) { return s.character_name; });
  if (!roleSessions.length) {
    listEl.innerHTML = '<div class="session-empty">暂无角色会话<br><span style="color:var(--text-muted);font-size:var(--fs-caption)">使用扮演模式与角色对话后，会话会出现在这里</span></div>';
    return;
  }

  var groups = new Map();  // character_name → sessions[]
  roleSessions.forEach(function (s) {
    var cn = s.character_name || '';
    if (!groups.has(cn)) groups.set(cn, []);
    groups.get(cn).push(s);
  });

  var sorted = Array.from(groups.entries()).sort(function (a, b) {
    return a[0].localeCompare(b[0], 'zh');
  });

  listEl.innerHTML = sorted.map(function (entry) {
    var cn = entry[0];
    var sessions = entry[1];
    return '<div class="session-book-group" onclick="drillCharIn(\'' + escapeHtml(cn) + '\')">'
      + '<span class="sbg-icon"><i data-lucide="drama"></i></span>'
      + '<div class="sbg-info">'
      + '<div class="sbg-title">' + escapeHtml(cn) + '</div>'
      + '<div class="sbg-count">' + sessions.length + '条会话</div>'
      + '</div>'
      + '<span class="sbg-arrow">›</span>'
      + '</div>';
  }).join('');
}

// ── Drill navigation (called from HTML onclick) ─────────────────

export function drillBookIn(bookId) {
  _drillBookId = bookId;
  renderSessionView();
}

export function drillBookOut() {
  _drillBookId = null;
  renderSessionView();
}

export function drillCharIn(charName) {
  _drillCharName = charName;
  renderSessionView();
}

export function drillCharOut() {
  _drillCharName = null;
  renderSessionView();
}

export function drillWorkIn(workId) {
  _drillWorkId = workId;
  renderSessionView();
}

export function drillWorkOut() {
  _drillWorkId = null;
  renderSessionView();
}

// ── Load / delete / new session ──────────────────────────────────

/** 会话属于哪条会话线。三个界面的提示词不同，会话不能串线续写。 */
function sessionSurface(data) {
  if (data.mode === 'writing') return 'write';
  // 扮演是首页的功能：哪怕带着书（角色扮演要书），也归首页线。
  if (data.mode === 'roleplay') return 'home';
  // 阅读页的会话一定带 project_id（后端用 book_id 落 project_id）；
  // 没有书的沉浸/俯瞰/发散会话来自主页。
  return data.project_id ? 'reader' : 'home';
}

export async function loadSession(sid, opts) {
  opts = opts || {};
  // Click-vs-dblclick arbitration happens in scheduleClickLoad (session
  // panel). Direct callers (fork jump, branch switcher) bypass the delay.
  if (S._chatAbort) { S._chatAbort.abort(); S._chatAbort = null; }
  // 正在拉开一个已有会话：预选块先撤，别等消息渲染完才消失。
  const _prelude = document.getElementById('modePrelude');
  if (_prelude) _prelude.classList.remove('visible');
  const seq = ++_loadSeq;
  try {
    const resp = await fetch(S.API + '/api/sessions/' + sid);
    const data = await resp.json();
    if (seq !== _loadSeq) return;  // a newer load was requested — drop this one
    if (data.error) {
      window.clearTabSessionById && window.clearTabSessionById(sid);
      // 切页自动恢复时的失败不该弹窗（多半是会话已被删），安静回到新会话。
      if (opts.silent) { resetChatView(); return; }
      alert('加载失败: ' + data.error);
      return;
    }
    // 会话归它自己的页面：在别的页签点开它，先切过去再渲染。
    // 否则下一句话会带着错误页面的提示词发给 agent（后端也会新建会话）。
    const surface = sessionSurface(data);
    const current = window.getActiveChatTab ? window.getActiveChatTab() : 'home';
    if (surface !== current && typeof window.openSessionTab === 'function') {
      // 当前线的指针如果正指着这条（归属别的线的）会话，先摘掉，
      // 否则每次回到那一页都会重复同一个跳页。
      const line = window.tabSessionFor ? window.tabSessionFor(current) : null;
      if (line && line.id === data.id && window.clearTabSession) {
        window.clearTabSession(current);
      }
      window.openSessionTab(data.id, surface, data.project_id || '');
    }

    // From here on the visible conversation is this session. Any stream
    // still in flight belongs to the previous one and must not write into
    // the freshly rebuilt chatHistory.
    S.chatEpoch++;
    const log = document.getElementById('chatLog');
    log.innerHTML = '';
    S.chatHistory = [];
    S.chatLastTimestamp = 0;  // 重置：避免上个会话的阈值污染本会话历史渲染
    S.currentSessionId = data.id;
    // 分叉后会直接 loadSession(child)，但 sendMessage 只在「新建会话」时
    // 保存视图状态 —— 不在这里存，刷新时 bc_view 仍指着父会话。
    window.saveViewState && window.saveViewState();
    // 写作不是阅读模式：别把 S.currentMode 改成 'writing'，否则离开
    // 写作页后阅读请求会带着它。写作的 mode 由所在页签决定。
    if (surface !== 'write') S.currentMode = data.mode || S.currentMode;
    // 记进这条线（本日可恢复）——在别的页签点开时 openSessionTab 已经
    // 记过一次，这里统一用会话的真实归属再落一遍。
    window.recordTabSession && window.recordTabSession(
      surface, data.id, {
        bookId: data.project_id || '',
        workId: data.work_id || '',
      });
    // 写作会话绑在作品上：点开某本的对话，写作台跟着切到那本，
    // 否则下一句话的提示词和这段历史来自两个作品。
    if (surface === 'write' && window.WritingUI
        && typeof window.WritingUI.openWork === 'function') {
      window.WritingUI.openWork(data.work_id || '');
    }
    refreshUsage();  // ring follows the loaded session's watermark

    // Restore role-play state when loading a role-play session
    if (data.mode === 'roleplay' && data.character_name) {
      S.currentCharacterName = data.character_name;
      // 扮演的是角色还是作者 —— 不恢复这一项，续聊会按角色去图谱里解析，
      // 而「史铁生」在《我与地坛》里是叙述者「我」的别名，会**静默**
      // 换成另一个人（后端拿不到 role_kind 时会收到 'character'）。
      S.currentRoleKind = data.role_kind || 'character';
      S.currentRoleRef = data.role_ref || '';
      S.rpSelectedBookId = data.project_id || '';
      if (typeof window.setModeLabel === 'function') window.setModeLabel('roleplay');
      var rpLabel = document.querySelector('#roleplayEntryBtn .mode-label');
      if (rpLabel) rpLabel.textContent = data.character_name;
    } else {
      S.currentCharacterName = '';
      S.currentRoleKind = 'character';
      S.currentRoleRef = '';
      S.rpSelectedBookId = null;
    }

    var messages = data.messages || [];
    messages.forEach(function (m) {
      var role = m.role === 'assistant' ? 'agent' : m.role;

      // ── 1. Classify parts ──────────────────────────────────────
      var voiceInputParts = [];
      var voiceOutputParts = [];
      if (m.parts && m.parts.length > 0) {
        m.parts.forEach(function (p) {
          if (p.type === 'voice_input' && p.data && p.data.voice_url) {
            voiceInputParts.push(p);
          } else if (p.type === 'voice_output' && p.data && p.data.voice_url) {
            voiceOutputParts.push(p);
          }
        });
      }

      // ── 2. Voice input bubbles (user speech → audio + 转文字) ──
      // Rendered synchronously to preserve message order.
      // Pass transcript to createVoiceBubble so it renders the inline
      // "转文字" / "收起" toggle + right-click context menu.
      voiceInputParts.forEach(function (p) {
        var audioUrl = p.data.voice_url;
        var durMs = p.data.duration_ms || 0;
        var transcript = p.data.transcript || '';
        createVoiceBubble(role, audioUrl, Math.round(durMs / 1000), transcript, m.time_created);
        if (transcript) {
          S.chatHistory.push({ role: role, content: transcript });
        }
      });

      // ── 2b. 写作回合的时间线（回放）─────────────────────────────
      // 实时是「头 + 步骤流 + 气泡」；重载必须长成同一个样子，否则刷新
      // 之后工具调用就凭空消失了。最后一段正文留在气泡里，前面的旁白
      // 和工具行进时间线。
      var writeTrace = null;
      var writeFinalText = null;
      if (surface === 'write' && role === 'agent'
          && m.steps && m.steps.length) {
        // 只有「最后一次工具调用之后」的正文才是最终回答；撞轮次上限 /
        // 中断的回合可能停在工具上，那时最后一段正文只是旁白。
        var lastText = -1;
        var lastTool = -1;
        for (var si = m.steps.length - 1; si >= 0; si--) {
          var st = m.steps[si];
          if (lastTool < 0 && st.type === 'tool') lastTool = si;
          if (lastText < 0 && st.type === 'text' && st.text) lastText = si;
        }
        if (lastText > lastTool) writeFinalText = m.steps[lastText].text;
        var traceSteps = lastText > lastTool
          ? m.steps.slice(0, lastText) : m.steps;
        if (traceSteps.length) writeTrace = renderTurnTraceFromSteps(traceSteps);
      }

      // ── 3. Think rows (agent only, above text bubble) ──────────
      // One row per persisted thinking block == one LLM call. A multi-step
      // ReAct turn stores its rounds in call order; rendering the merged
      // `thinking` string collapsed them into a single row, which on reload
      // looked like only the last call's reasoning was kept. `thinking`
      // remains the fallback for sessions written by older servers.
      // 写作回合的思考行已经在时间线里，不再单独重复一份。
      if (role === 'agent' && !writeTrace) {
        var thinkParts = (m.thinkings && m.thinkings.length)
          ? m.thinkings
          : (m.thinking ? [m.thinking] : []);
        thinkParts.forEach(function (t) {
          document.getElementById('chatLog').appendChild(createThinkRow(t, null));
        });
      }

      // ── 4. Text content ────────────────────────────────────────
      // voice_input messages: transcript already shown above → skip
      // voice_output messages: text bubble is the main reply → keep
      var isVoiceInputMsg = voiceInputParts.length > 0;

      if (m.content) {
        if (role === 'user') {
          if (!isVoiceInputMsg) {
            var _userBubble = addChat('user', m.content, null, m.time_created);
            if (_userBubble && m.id) _userBubble.dataset.msgId = m.id;
            S.chatHistory.push({ role: 'user', content: m.content });
          }
        } else {
          var cleanContent = writeTrace ? (writeFinalText || '') : m.content;
          if (!m.thinking && !writeTrace) {
            // Backward compat: old sessions had <think> tags in content.
            // Extract thinking → render expandable thinkRow, then strip.
            var thinkRe = /<think>([\s\S]*?)<\/think>/g;
            var thinkMatch;
            while ((thinkMatch = thinkRe.exec(cleanContent)) !== null) {
              if (thinkMatch[1].trim()) {
                var oldThinkRow = createThinkRow(thinkMatch[1].trim(), null);
                document.getElementById('chatLog').appendChild(oldThinkRow);
              }
            }
            cleanContent = cleanContent.replace(/<think>[\s\S]*?<\/think>/g, '');
            cleanContent = cleanContent.trim();
          }
          if (cleanContent) {
            var _agentBubble = addChat('agent', cleanContent, null, m.time_created);
            if (_agentBubble && m.id) _agentBubble.dataset.msgId = m.id;
            S.chatHistory.push({ role: 'agent', content: cleanContent });
            if (writeTrace) {
              // addChat 返回的就是 .msg 行本身（不是里面的气泡），直接插在
              // 它前面：用户气泡 → 时间线 → 回复气泡。
              _agentBubble.parentNode.insertBefore(writeTrace.head, _agentBubble);
              _agentBubble.parentNode.insertBefore(writeTrace.trace, _agentBubble);
              writeTrace = null;
            }
          }
        }
      } else if (role === 'agent' && m.interrupted) {
        // 中断且一个字都没吐出来：只留标记，不生成空气泡。
      } else if (!m.parts || m.parts.length === 0) {
        // Fallback for old-format messages (no parts at all)
        if (role === 'user') {
          var _userBubble2 = addChat('user', m.content || '', null, m.time_created);
          if (_userBubble2 && m.id) _userBubble2.dataset.msgId = m.id;
          S.chatHistory.push({ role: 'user', content: m.content || '' });
        } else {
          var oldContent = m.content || '';
          if (!m.thinking) {
            // Same backward-compat: extract thinking from old sessions
            var oldThinkRe = /<think>([\s\S]*?)<\/think>/g;
            var oldThinkMatch;
            while ((oldThinkMatch = oldThinkRe.exec(oldContent)) !== null) {
              if (oldThinkMatch[1].trim()) {
                var oldThinkRow2 = createThinkRow(oldThinkMatch[1].trim(), null);
                document.getElementById('chatLog').appendChild(oldThinkRow2);
              }
            }
            oldContent = oldContent.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
          }
          if (oldContent) {
            var _agentBubble2 = addChat('agent', oldContent, null, m.time_created);
            if (_agentBubble2 && m.id) _agentBubble2.dataset.msgId = m.id;
            S.chatHistory.push({ role: 'agent', content: oldContent });
          }
        }
      }

      // 整轮没有最终正文（只有工具与旁白）：时间线照样要出现。
      if (writeTrace) {
        var _traceLog = document.getElementById('chatLog');
        _traceLog.appendChild(writeTrace.head);
        _traceLog.appendChild(writeTrace.trace);
      }

      // ── 4b. Interrupt marker (agent only) ──────────────────────
      // 注记不进正文，和流式时的 UI 保持一致。
      if (role === 'agent' && m.interrupted) {
        document.getElementById('chatLog').appendChild(createInterruptNote());
      }

      // ── 5. Voice output bubbles (agent TTS, after text) ────────
      voiceOutputParts.forEach(function (p) {
        var audioUrl = p.data.voice_url;
        var durMs = p.data.duration_ms || 0;
        createVoiceBubble(role, audioUrl, Math.round(durMs / 1000), null, m.time_created);
      });
    });
    if (S.chatHistory.length > 30) S.chatHistory = S.chatHistory.slice(-20);

    if (surface !== 'write' && data.mode && typeof window.setModeLabel === 'function') {
      // 写作会话不动阅读模式徽标（写作页的行由页签决定）。
      window.setModeLabel(data.mode);
    }
    if (surface === 'write' && data.writing_mode
        && typeof window.setWritingMode === 'function') {
      // 会话已冻结的写作模式：同步界面状态，但不重新显示预选。
      window.setWritingMode(data.writing_mode);
    }
    // 已有内容的会话不再提供模式预选：模式随会话一起冻结。
    if (window.updateModePrelude) window.updateModePrelude();

    // Close the panel after loading. A dblclick rename never reaches here
    // (cancelPendingClickLoad prevents the load); the .si-rename-input
    // check is a safety net for edge cases.
    if (!document.querySelector('.si-rename-input')) {
      document.getElementById('sessionPanel').classList.remove('visible');
    }
    stickChatToBottom(log);

    // Assign sequential indices to rendered message bubbles (used by fork
    // to compute fork_point_index and to locate the preceding user message).
    document.querySelectorAll('#chatLog > .msg').forEach(function (el, i) {
      el.dataset.msgIndex = String(i);
    });

    // Render branch switchers at fork points (parent + children).
    renderBranchSwitchers(data.id);

    // If this load was triggered by a fork action (重新输入/重新回复),
    // run the pending step now that the new session is rendered.
    if (window.handlePendingForkAction) window.handlePendingForkAction();
  } catch (e) {
    if (opts.silent) {
      window.clearTabSessionById && window.clearTabSessionById(sid);
      resetChatView();
      return;
    }
    alert('加载失败: ' + e.message);
  }
}

export async function deleteSession(event, sid) {
  event.stopPropagation();
  if (!confirm('确定删除这个会话？')) return;
  try {
    const resp = await fetch(S.API + '/api/sessions/' + sid, { method: 'DELETE' });
    const data = await resp.json();
    if (data.error) {
      alert('删除失败: ' + data.error);
      return;
    }
    // 先判断是不是当前视图：clearTabSessionById 会把 currentSessionId 抹掉。
    const _wasCurrent = sid === S.currentSessionId;
    // Remove from cache; 别的页签的会话线也不能再指着它。
    _cachedSessions = _cachedSessions.filter(s => s.id !== sid);
    if (!_wasCurrent && window.clearTabSessionById) window.clearTabSessionById(sid);
    if (_wasCurrent) {
      newSession();
    } else {
      renderSessionView();
    }
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

// ── Inline rename ─────────────────────────────────────────────────

export function startRename(event, sid) {
  var titleEl = event.target.closest('.si-title');
  if (!titleEl || titleEl.querySelector('input')) return;  // already editing
  var titleTextEl = titleEl.querySelector('.si-title-text');
  var oldTitle = titleTextEl ? titleTextEl.textContent : titleEl.textContent;
  titleEl.innerHTML = '<input class="si-rename-input" value="' + escapeHtml(oldTitle) + '" maxlength="200">';
  var input = titleEl.querySelector('input');
  input.focus();
  input.select();
  input.addEventListener('blur', function () { finishRename(sid, input); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { input.value = oldTitle; input.blur(); }
  });
}

async function finishRename(sid, input) {
  var newTitle = input.value.trim() || '(无标题)';
  // Update cache first, then redraw from renderSessionItem so the title row
  // keeps its expand indicator (startRename wiped the original innerHTML).
  // 分叉子会话只在 _childrenCache 里，两份缓存都要改，否则重绘会把名字打回旧的。
  function patch(list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === sid) { list[i].title = newTitle; return true; }
    }
    return false;
  }
  if (!patch(_cachedSessions)) _childrenCache.forEach(function (kids) { patch(kids); });
  renderSessionView();
  // Persist to server
  try {
    await fetch(S.API + '/api/sessions/' + sid + '/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: newTitle }),
    });
  } catch (e) { /* silent */ }
}

// ── Fork ──────────────────────────────────────────────────────────
// Fork entry points now live in chat.js (重新输入 / 重新回复 on each bubble).
// The session panel is a management view only; tree expand/collapse below.

// ── Branch switcher ──────────────────────────────────────────────
// DeepSeek-style ‹ 1/N › control at every fork point. Switching loads the
// sibling session wholesale, so both the user turn and the agent reply
// change together; the backend then rebuilds context from that session id.

function renderBranchSwitchers(sessionId) {
  fetch(S.API + '/api/sessions/' + sessionId + '/branches')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data || !data.current) return;
      // The fetch may resolve after the user switched again. Rendering old
      // switchers into the new transcript is precisely the view/context
      // mismatch this control must never introduce.
      if (S.currentSessionId !== sessionId) return;

      // Switchers are derived state: clear and re-place them on every pass.
      // After a fork the first render can land on a transient row (inline
      // editor / pending agent bubble) and the post-send render then targets
      // the real row — keeping the old one would duplicate the control.
      document.querySelectorAll('#chatLog .branch-switcher-row')
        .forEach(function (el) { el.remove(); });

      // Group by fork_point_index. Every session only knows its own parent
      // and children, so siblings (other children of the same parent) are
      // what makes 3+ alternatives reachable at the same fork point.
      var groups = {};
      function addToGroup(idx, branch) {
        if (idx == null || idx < 0) return;
        var k = String(idx);
        if (!groups[k]) groups[k] = [];
        for (var i = 0; i < groups[k].length; i++) {
          if (groups[k][i].id === branch.id) return;  // dedupe
        }
        groups[k].push(branch);
      }
      var cur = {
        id: data.current.id,
        title: data.current.title,
        fork_point_index: data.current.fork_point_index,
        time_created: data.current.time_created,
        isCurrent: true
      };

      // Own fork point: the parent is the original continuation; add every
      // sibling that forked at the SAME index. Siblings that forked later
      // share the same prefix but are not alternatives at this position.
      if (data.parent) {
        var ownIdx = cur.fork_point_index;
        addToGroup(ownIdx, {
          id: data.parent.id, title: data.parent.title,
          time_created: data.parent.time_created
        });
        addToGroup(ownIdx, cur);
        (data.siblings || []).forEach(function (s) {
          if (s.fork_point_index === ownIdx) addToGroup(ownIdx, s);
        });
      }

      // Children: at each child's fork point, current is the original
      // continuation and each child is an alternative.
      (data.children || []).forEach(function (c) {
        addToGroup(c.fork_point_index, cur);
        addToGroup(c.fork_point_index, c);
      });

      var msgEls = document.querySelectorAll('#chatLog > .msg');
      if (!msgEls.length) return;
      // Ascending so an in-range anchor wins over a clamped one when two
      // groups compete for the same row.
      Object.keys(groups).sort(function (a, b) {
        return parseInt(a, 10) - parseInt(b, 10);
      }).forEach(function (k) {
        var idx = parseInt(k, 10);
        var branches = groups[k];
        if (branches.length < 2) return;  // single branch -> nothing to switch
        // The parent of an edit fork ends right before the diverging turn,
        // so the stored index can be one row past its transcript. Anchor at
        // the last row instead of dropping the switcher — otherwise that
        // side of the branch is unreachable from the UI.
        var target = (idx >= 0 && idx < msgEls.length)
          ? msgEls[idx]
          : msgEls[msgEls.length - 1];
        if (!target || target.querySelector('.branch-switcher')) return;
        // Stable order: original branch first (created earliest), then
        // alternatives in creation order — "1/N" always means "the one that
        // was already there".
        branches.sort(function (a, b) {
          return (a.time_created || 0) - (b.time_created || 0);
        });
        // Keep the switcher above the hover action bar so it sits right
        // under the bubble and stays visible without hovering.
        var actions = target.querySelector('.msg-actions');
        var switcher = buildSwitcher(branches);
        if (actions) target.insertBefore(switcher, actions);
        else target.appendChild(switcher);
      });
    })
    .catch(function () { /* silent - switchers are optional */ });
}

function buildSwitcher(branches) {
  var row = document.createElement('div');
  row.className = 'branch-switcher-row';
  var wrap = document.createElement('div');
  wrap.className = 'branch-switcher';
  var currentIdx = 0;
  for (var i = 0; i < branches.length; i++) {
    if (branches[i].isCurrent) { currentIdx = i; break; }
  }

  function makeBtn(icon, title, disabled, delta) {
    var btn = document.createElement('button');
    btn.className = 'bs-btn';
    btn.innerHTML = '<i data-lucide="' + icon + '"></i>';
    btn.title = title;
    btn.disabled = disabled;
    btn.setAttribute('aria-label', title);
    btn.onclick = function (e) { e.stopPropagation(); jump(delta); };
    return btn;
  }

  var label = document.createElement('span');
  label.className = 'bs-label';
  label.textContent = (currentIdx + 1) + '/' + branches.length;
  label.title = branches[currentIdx].title || '';

  function jump(delta) {
    var ni = currentIdx + delta;
    if (ni < 0 || ni >= branches.length) return;
    if (branches[ni].id === S.currentSessionId) return;
    if (window.loadSession) window.loadSession(branches[ni].id);
  }

  wrap.appendChild(makeBtn('chevron-left',
    currentIdx > 0 ? ('上一分支：' + (branches[currentIdx - 1].title || '')) : '已是第一个分支',
    currentIdx <= 0, -1));
  wrap.appendChild(label);
  wrap.appendChild(makeBtn('chevron-right',
    currentIdx < branches.length - 1
      ? ('下一分支：' + (branches[currentIdx + 1].title || ''))
      : '已是最后一个分支',
    currentIdx >= branches.length - 1, 1));
  row.appendChild(wrap);
  return row;
}

// ── Tree expand/collapse children ───────────────────────────────

export async function toggleChildren(sid) {
  if (_expandedSessions.has(sid)) {
    _expandedSessions.delete(sid);
    renderSessionView();
    return;
  }
  // Lazy-load children on first expand
  if (!_childrenCache.has(sid)) {
    try {
      var resp = await fetch(S.API + '/api/sessions/' + sid + '/children');
      var data = await resp.json();
      _childrenCache.set(sid, data.children || []);
    } catch (e) {
      _childrenCache.set(sid, []);
    }
  }
  if (_childrenCache.get(sid).length === 0) return;  // no children, don't expand
  _expandedSessions.add(sid);
  renderSessionView();
}

// ── Archive ───────────────────────────────────────────────────────

export async function archiveSession(event, sid) {
  event.stopPropagation();
  if (!confirm('确定归档这个会话？归档后可从列表中隐藏。')) return;
  try {
    var resp = await fetch(S.API + '/api/sessions/' + sid + '/archive', { method: 'POST' });
    var data = await resp.json();
    if (data.error) { alert('归档失败: ' + data.error); return; }
    var _wasCurrent = sid === S.currentSessionId;
    _cachedSessions = _cachedSessions.filter(function (s) { return s.id !== sid; });
    if (!_wasCurrent && window.clearTabSessionById) window.clearTabSessionById(sid);
    if (_wasCurrent) { newSession(); }
    else { renderSessionView(); }
  } catch (e) { alert('归档失败: ' + e.message); }
}

// ── New session ───────────────────────────────────────────────────

/** 把聊天区重置成空白新会话（不动所属会话线的持久化指针）。 */
export function resetChatView() {
  if (S._chatAbort) { S._chatAbort.abort(); S._chatAbort = null; }
  // Drop any in-flight load and mute streams that belong to the old view.
  _loadSeq++;
  S.chatEpoch++;
  const log = document.getElementById('chatLog');
  if (log) {
    log.innerHTML = '<div class="msg agent">' + getWelcomeMessage() + '</div>';
    // 新会话回到「跟随底部」，别继承上一段会话里用户上滚留下的停顿。
    stickChatToBottom(log);
  }
  S.chatHistory = [];
  S.chatLastTimestamp = 0;
  S.currentSessionId = '';
  if (window.updateModePrelude) window.updateModePrelude();
  refreshUsage();
}

export function newSession() {
  // 「+」按钮：当前页这条线也丢掉，下次进来是全新的。
  if (window.clearTabSession && window.getActiveChatTab) {
    window.clearTabSession(window.getActiveChatTab());
  }
  resetChatView();
  // Hide context bar — new sessions on the home page have no book context.
  window.hideContextBar && window.hideContextBar();
  document.getElementById('sessionPanel').classList.remove('visible');
  window.saveViewState && window.saveViewState();
}

// ── HTML onclick access ───────────────────────────────────────────
window.toggleSessionPanel = toggleSessionPanel;
window.loadSession = loadSession;
window.deleteSession = deleteSession;
window.newSession = newSession;
window.resetChatView = resetChatView;
window.switchSubnavTab = switchSubnavTab;
window.drillBookIn = drillBookIn;
window.drillBookOut = drillBookOut;
window.drillCharIn = drillCharIn;
window.drillCharOut = drillCharOut;
window.drillWorkIn = drillWorkIn;
window.drillWorkOut = drillWorkOut;
window.startRename = startRename;
window.scheduleClickLoad = scheduleClickLoad;
window.cancelPendingClickLoad = cancelPendingClickLoad;
window.toggleChildren = toggleChildren;
window.archiveSession = archiveSession;
window.loadMoreSessions = loadMoreSessions;
// After a fork the child is loaded before the pending turn is re-sent, so
// the message the switcher anchors to does not exist yet. chat.js calls
// this again once the rows are appended.
window.renderBranchSwitchers = renderBranchSwitchers;
