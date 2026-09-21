/* Book Companion — session (chat history) management. */

import { S } from './state.js';
import { escapeHtml, formatSessionTime } from './utils.js';
import { getWelcomeMessage } from './greetings.js';
import { addChat, createVoiceBubble, createThinkRow } from './chat.js';
import { refreshUsage } from './context-usage.js';

const SESSION_MODE_LABELS = {
  immersive: '沉浸',
  overview: '俯瞰',
  divergent: '发散',
  roleplay: '扮演'
};

// ── Sub-nav state ─────────────────────────────────────────────────
let _cachedSessions = [];
let _currentSubnavTab = 'all';    // 'all' | 'books' | 'characters'
let _drillBookId = null;          // null = show group list; string = drilled into a specific group
let _drillCharName = null;        // null = show character list; string = drilled into a specific character
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

export function toggleSessionPanel() {
  const panel = document.getElementById('sessionPanel');
  if (panel.classList.contains('visible')) {
    panel.classList.remove('visible');
  } else {
    panel.classList.add('visible');
    // Reset sub-nav to default on open
    _currentSubnavTab = 'all';
    _drillBookId = null;
    _searchTerm = '';
    _listOffset = 0;
    _listHasMore = true;
    var searchInput = document.getElementById('sessionSearchInput');
    if (searchInput) searchInput.value = '';
    updateSubnavActive();
    loadSessionList(true);
  }
}

// ── Sub-nav tab switching ─────────────────────────────────────────

export function switchSubnavTab(tab) {
  _currentSubnavTab = tab;
  _drillBookId = null;
  _drillCharName = null;
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

  // ── Search path ──────────────────────────────────────────────
  if (_searchTerm) {
    try {
      var searchResp = await fetch(S.API + '/api/sessions/search?q=' + encodeURIComponent(_searchTerm) + '&limit=30');
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
        var snippetHtml = (r.snippets || []).map(function (s) {
          return '<div class="si-snippet">' + escapeHtml(s) + '</div>';
        }).join('');
        return '<div class="session-item' + (r.session_id === S.currentSessionId ? ' active' : '') + '"'
          + ' onclick="loadSession(\'' + r.session_id + '\')">'
          + (cached && cached.book_title ? '<span class="si-book">' + escapeHtml(cached.book_title) + '</span>' : '')
          + (cached && cached.character_name ? '<span class="si-char"><i data-lucide="drama"></i> ' + escapeHtml(cached.character_name) + '</span>' : '')
          + '<div class="si-title"><span class="si-title-text">' + escapeHtml(r.title || '(无标题)') + '</span></div>'
          + snippetHtml
          + '<div class="si-meta">'
          + '<span class="si-mode">' + (SESSION_MODE_LABELS[r.mode] || r.mode) + '</span>'
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
    var resp = await fetch(S.API + '/api/sessions/list?limit=' + _pageSize + '&offset=' + _listOffset + '&root_only=true');
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
  if (!_cachedSessions.length) {
    listEl.innerHTML = '<div class="session-empty">暂无历史会话</div>';
    return;
  }
  var html = _cachedSessions.map(function (s) { return renderSessionItem(s); }).join('');
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

function renderSessionItem(s, depth) {
  depth = depth || 0;
  var title = escapeHtml(s.title || '(无标题)');
  var modeLabel = SESSION_MODE_LABELS[s.mode] || s.mode;
  var isActive = s.id === S.currentSessionId;
  var time = formatSessionTime(s.time_updated);
  // Book label — show for sessions with project_id, "主会话" for home-page sessions
  var bookLabel = s.book_title
    ? '<span class="si-book">' + escapeHtml(s.book_title) + '</span>'
    : (s.project_id ? '' : '<span class="si-book si-book-home">主会话</span>');
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
    + ' onclick="scheduleClickLoad(\'' + s.id + '\')">'
    + (bookLabel ? bookLabel : '')
    + (charBadge ? charBadge : '')
    + '<div class="si-title" ondblclick="event.stopPropagation();cancelPendingClickLoad();startRename(event,\'' + s.id + '\')" title="双击或点 <i data-lucide="pencil"></i> 重命名">'
    + expandBtn
    + '<span class="si-title-text">' + title + '</span>'
  + '<span class="si-rename-btn" onclick="event.stopPropagation();startRename(event,\'' + s.id + '\')" title="重命名"><i data-lucide="pencil"></i></span>'
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

  // Drill-down: showing sessions for a specific book
  if (_drillBookId !== null) {
    const bookSessions = _cachedSessions.filter(s => {
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
  if (!_cachedSessions.length) {
    listEl.innerHTML = '<div class="session-empty">暂无历史会话</div>';
    return;
  }

  const groups = new Map();  // key → { title, sessions[] }
  for (const s of _cachedSessions) {
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

// ── Load / delete / new session ──────────────────────────────────

export async function loadSession(sid) {
  // Click-vs-dblclick arbitration happens in scheduleClickLoad (session
  // panel). Direct callers (fork jump, branch switcher) bypass the delay.
  if (S._chatAbort) { S._chatAbort.abort(); S._chatAbort = null; }
  try {
    const resp = await fetch(S.API + '/api/sessions/' + sid);
    const data = await resp.json();
    if (data.error) {
      alert('加载失败: ' + data.error);
      return;
    }
    const log = document.getElementById('chatLog');
    log.innerHTML = '';
    S.chatHistory = [];
    S.chatLastTimestamp = 0;  // 重置：避免上个会话的阈值污染本会话历史渲染
    S.currentSessionId = data.id;
    S.currentMode = data.mode || S.currentMode;
    refreshUsage();  // ring follows the loaded session's watermark

    // Restore role-play state when loading a role-play session
    if (data.mode === 'roleplay' && data.character_name) {
      S.currentCharacterName = data.character_name;
      S.rpSelectedBookId = data.project_id || '';
      document.getElementById('modeLabel').innerHTML = '<i data-lucide="drama"></i> ' + escapeHtml(data.character_name);
      var rpLabel = document.querySelector('#roleplayEntryBtn .mode-label');
      if (rpLabel) rpLabel.textContent = data.character_name;
    } else {
      S.currentCharacterName = '';
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

      // ── 3. Think row (agent only, above text bubble) ───────────
      if (role === 'agent' && m.thinking) {
        var thinkRow = createThinkRow(m.thinking, null);
        document.getElementById('chatLog').appendChild(thinkRow);
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
          var cleanContent = m.content;
          if (!m.thinking) {
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
          }
        }
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

      // ── 5. Voice output bubbles (agent TTS, after text) ────────
      voiceOutputParts.forEach(function (p) {
        var audioUrl = p.data.voice_url;
        var durMs = p.data.duration_ms || 0;
        createVoiceBubble(role, audioUrl, Math.round(durMs / 1000), null, m.time_created);
      });
    });
    if (S.chatHistory.length > 30) S.chatHistory = S.chatHistory.slice(-20);

    if (data.mode && typeof window.setModeLabel === 'function') {
      window.setModeLabel(data.mode);
    } else if (data.mode) {
      var modeLabels = { immersive: '沉浸伴读', overview: '俯瞰分析', divergent: '发散探索', roleplay: '<i data-lucide="drama"></i> 扮演' };
      if (modeLabels[data.mode]) {
        S.currentMode = data.mode;
        document.getElementById('modeLabel').innerHTML = modeLabels[data.mode];
        // Also update mode-option highlights in the picker
        document.querySelectorAll('.mode-option').forEach(function (opt) {
          opt.classList.toggle('active', opt.dataset.mode === data.mode);
        });
      }
    }

    // Close the panel after loading. A dblclick rename never reaches here
    // (cancelPendingClickLoad prevents the load); the .si-rename-input
    // check is a safety net for edge cases.
    if (!document.querySelector('.si-rename-input')) {
      document.getElementById('sessionPanel').classList.remove('visible');
    }
    log.scrollTop = log.scrollHeight;

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
    // Remove from cache
    _cachedSessions = _cachedSessions.filter(s => s.id !== sid);
    if (sid === S.currentSessionId) {
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
  input.addEventListener('blur', function () { finishRename(sid, input, titleEl); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { input.value = oldTitle; input.blur(); }
  });
}

async function finishRename(sid, input, titleEl) {
  var newTitle = input.value.trim() || '(无标题)';
  // Restore title text + rename pencil (structure must match renderSessionItem).
  titleEl.innerHTML = '<span class="si-title-text">' + escapeHtml(newTitle) + '</span>'
    + '<span class="si-rename-btn" onclick="event.stopPropagation();startRename(event,\'' + sid + '\')" title="重命名"><i data-lucide="pencil"></i></span>';
  // Update cache
  for (var i = 0; i < _cachedSessions.length; i++) {
    if (_cachedSessions[i].id === sid) { _cachedSessions[i].title = newTitle; break; }
  }
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
// Renders a ◀ 1/N ▶ control on the message at each fork_point_index so the
// user can switch between sibling branches without leaving the chat view.

function renderBranchSwitchers(sessionId) {
  fetch(S.API + '/api/sessions/' + sessionId + '/branches')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data || !data.current) return;
      // Group branches by fork_point_index. At each fork point, the user can
      // switch between the current session and any sibling that branched
      // there. current is added to every group because its messages exist
      // at every position.
      var groups = {};
      function addToGroup(idx, branch) {
        if (idx == null) return;
        var k = String(idx);
        if (!groups[k]) groups[k] = [];
        for (var i = 0; i < groups[k].length; i++) {
          if (groups[k][i].id === branch.id) return;  // dedupe current
        }
        groups[k].push(branch);
      }
      // Parent: at the current session's fork_point_index, can switch to parent.
      if (data.parent) {
        var pIdx = data.parent.fork_point_index;
        addToGroup(pIdx, {id: data.parent.id, title: data.parent.title, isCurrent: false});
        addToGroup(pIdx, {id: data.current.id, title: data.current.title, isCurrent: true});
      }
      // Children: at each child's fork_point_index, can switch to that child.
      data.children.forEach(function (c) {
        addToGroup(c.fork_point_index, {id: c.id, title: c.title, isCurrent: false});
        addToGroup(c.fork_point_index, {id: data.current.id, title: data.current.title, isCurrent: true});
      });
      // Render a switcher on each indexed message bubble.
      Object.keys(groups).forEach(function (k) {
        var idx = parseInt(k, 10);
        var branches = groups[k];
        if (branches.length < 2) return;  // no siblings -> no switcher
        var msgEls = document.querySelectorAll('#chatLog > .msg');
        if (idx >= msgEls.length) return;
        var target = msgEls[idx];
        if (!target || target.querySelector('.branch-switcher')) return;
        target.appendChild(buildSwitcher(branches));
      });
    })
    .catch(function () { /* silent - switchers are optional */ });
}

function buildSwitcher(branches) {
  var wrap = document.createElement('div');
  wrap.className = 'branch-switcher';
  var currentIdx = 0;
  for (var i = 0; i < branches.length; i++) {
    if (branches[i].isCurrent) { currentIdx = i; break; }
  }
  var left = document.createElement('button');
  left.className = 'bs-btn';
  left.innerHTML = '<i data-lucide="chevron-left"></i>';
  left.title = '上一分支';
  var label = document.createElement('span');
  label.className = 'bs-label';
  label.textContent = (currentIdx + 1) + '/' + branches.length;
  var right = document.createElement('button');
  right.className = 'bs-btn';
  right.innerHTML = '<i data-lucide="play"></i>';
  right.title = '下一分支';
  function jump(delta) {
    var ni = (currentIdx + delta + branches.length) % branches.length;
    if (window.loadSession) window.loadSession(branches[ni].id);
  }
  left.onclick = function (e) { e.stopPropagation(); jump(-1); };
  right.onclick = function (e) { e.stopPropagation(); jump(1); };
  wrap.appendChild(left);
  wrap.appendChild(label);
  wrap.appendChild(right);
  return wrap;
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
    _cachedSessions = _cachedSessions.filter(function (s) { return s.id !== sid; });
    if (sid === S.currentSessionId) { newSession(); }
    else { renderSessionView(); }
  } catch (e) { alert('归档失败: ' + e.message); }
}

// ── New session ───────────────────────────────────────────────────

export function newSession() {
  if (S._chatAbort) { S._chatAbort.abort(); S._chatAbort = null; }
  const log = document.getElementById('chatLog');
  log.innerHTML = '<div class="msg agent">' + getWelcomeMessage() + '</div>';
  S.chatHistory = [];
  S.currentSessionId = '';
  // Hide context bar — new sessions on the home page have no book context.
  window.hideContextBar && window.hideContextBar();
  document.getElementById('sessionPanel').classList.remove('visible');
  window.saveViewState && window.saveViewState();
  refreshUsage();  // ring resets to the new-session baseline
}

// ── HTML onclick access ───────────────────────────────────────────
window.toggleSessionPanel = toggleSessionPanel;
window.loadSession = loadSession;
window.deleteSession = deleteSession;
window.newSession = newSession;
window.switchSubnavTab = switchSubnavTab;
window.drillBookIn = drillBookIn;
window.drillBookOut = drillBookOut;
window.drillCharIn = drillCharIn;
window.drillCharOut = drillCharOut;
window.startRename = startRename;
window.scheduleClickLoad = scheduleClickLoad;
window.cancelPendingClickLoad = cancelPendingClickLoad;
window.toggleChildren = toggleChildren;
window.archiveSession = archiveSession;
window.loadMoreSessions = loadMoreSessions;
