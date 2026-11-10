/* Liubai — entry point: init, view state, tab switching, UI shell. */

import { S } from './state.js';
import { escapeHtml } from './utils.js';
import { getWelcomeMessage } from './greetings.js';
import { loadSettings, applySettings, populateThemeForm,
         loadAgentProfile, refreshAgentProfile,
         loadUserProfile, refreshUserProfile,
         refreshBenchPreview } from './theme.js';
import { loadShelfSort, loadShelfScene, updateShelfSortUI, loadShelf, loadBookDropdown, refreshBooks, refreshWatchStatus, initStickyCompact, initSortPanel, initShelfReorder } from './shelf.js';
import { renderBookProgress, selectBook } from './reader.js';
import { refreshUsage } from './context-usage.js';
import { checkOnboarding } from './onboarding.js';
import { resizeNative, suspendHostResizeObserver, resumeHostResizeObserver,
         stashViewport, restoreViewport, invalidatePendingRestore,
         whenHostVisible,
         jumpToCfi, jumpToAnchor } from './epub-reader.js';
import { onSearchLeftScroll } from './search.js';
import { leaveFocusModeSilently } from './focus-mode.js';
import { createMorph } from '../lib/morphicons.dom.js';
import './mode.js';     // side-effect: registers window.setMode / window.setModeLabel

// ── Chat toggle icon（morphicons：minus ⇄ plus）───────────────────
// 展开态显示 minus（点击收起）、收起态显示 plus（点击展开）。子路径数
// 不等时 plan 走"细胞分裂"——竖线从横线里长出/缩回，正是这对的招牌 morph。
const CHAT_ICON_MINUS = 'M5 12h14';
const CHAT_ICON_PLUS = 'M5 12h14 M12 5v14';
// module 脚本 defer 执行，DOM 已就绪；静态 svg 无 path 时退化为无图标按钮
const _chatToggleMorph = (() => {
  const path = document.querySelector('#chatToggleBtn svg path');
  return path ? createMorph(path, CHAT_ICON_MINUS, { reducedMotion: 'user' }) : null;
})();

// ── Tab state ─────────────────────────────────────────────────────

window._currentTab = 'home';

// ── View state persistence ────────────────────────────────────────

const VIEW_DEFAULTS = { activeTab: 'home', bookId: null, sessionId: '' };

export function saveViewState() {
  try {
    localStorage.setItem('bc_view', JSON.stringify({
      activeTab: window._currentTab,
      bookId: S.currentBook,
      sessionId: S.currentSessionId,
      timestamp: Date.now()
    }));
  } catch (e) {}
}

/** Resolve the fork family's latest branch for view restore.
 *
 * ``bc_view`` can point at the parent (or a sibling the user peeked at)
 * even though the last real interaction happened in a fork child. Reopening
 * should land on that latest branch, not on a dead-end parent.
 */
async function resolveLatestSessionId(sid) {
  if (!sid) return '';
  try {
    const resp = await fetch(S.API + '/api/sessions/' + sid + '/latest');
    if (!resp.ok) return sid;
    const data = await resp.json();
    return (data && data.id) || sid;
  } catch (_) {
    return sid;
  }
}

export async function restoreViewState() {
  try {
    const raw = localStorage.getItem('bc_view');
    if (!raw) return false;
    const state = JSON.parse(raw);
    // 阅读页即使没记下书也要恢复：旧版本在选书前就 saveViewState，
    // bc_view 会是 {activeTab:'reader', bookId:null}。以前这里不匹配任何
    // 分支，刷新后退回书架页 —— 而「继续阅读」卡片仍标着上次那本书。
    const needsSession = state.activeTab === 'home'
      || state.activeTab === 'write'
      || state.activeTab === 'reader';
    const restoreSid = needsSession
      ? await resolveLatestSessionId(state.sessionId)
      : '';

    if (state.activeTab === 'reader' && !state.bookId) {
      S.currentSessionId = restoreSid;
      _activeChatTab = 'reader';
      // switchTab 走非 skipLoad 分支 → refreshBooks() 按「继续阅读」真相源
      // 挑书并在选书后重新落盘 bc_view。
      switchTab('reader');
      if (restoreSid && window.loadSession) window.loadSession(restoreSid);
      return true;
    }

    if (state.activeTab === 'reader' && state.bookId) {
      S.currentBook = state.bookId;
      // View-state restore must NOT look like a book switch — mark the book
      // as already-loaded so selectBook() keeps the restored session alive.
      S._lastLoadedBook = state.bookId;
      S.currentSessionId = restoreSid;
      // Prime the line so switchTab() treats this as "already on reader" and
      // leaves the bc_view session alone (it renders after the book loads).
      _activeChatTab = 'reader';
      recordTabSession('reader', restoreSid, { bookId: state.bookId });
      switchTab('reader', true);
      loadBookDropdown().then(() => {
        document.getElementById('bookSelect').value = state.bookId;
        selectBook().then(() => {
          // restoreViewState only stored the id; without this call the chat
          // pane comes back as a blank welcome screen after F5.
          if (restoreSid && S.currentSessionId === restoreSid && window.loadSession) {
            window.loadSession(restoreSid);
          }
          saveViewState();
        });
      });
      return true;
    }

    if (state.activeTab === 'home') {
      // 扮演会话在首页。这里原先直接 switchTab('home')，把 sessionId 丢了 ——
      // 而 switchTab 末尾会 saveViewState()，于是刚读到的 id 立刻被空值覆盖：
      // 刷新后回复消失，看起来像"没落盘"。
      S.currentSessionId = restoreSid;
      _activeChatTab = 'home';
      recordTabSession('home', restoreSid);
      switchTab('home');
      if (S.currentSessionId && window.loadSession) {
        window.loadSession(S.currentSessionId);
      }
      return true;
    }
    if (state.activeTab === 'write') {
      // 写作页有自己的会话线：先归位指针再切页，避免 switchTab 又去
      // 恢复一遍（它恢复的是本日线，bc_view 才是最精确的那条）。
      _activeChatTab = 'write';
      recordTabSession('write', restoreSid);
      S.currentSessionId = restoreSid;
      switchTab('write');
      if (restoreSid && window.loadSession) {
        window.loadSession(restoreSid);
      }
      saveViewState();
      return true;
    }
    if (state.activeTab === 'theme') { switchTab('theme'); return true; }
    if (state.activeTab === 'graph') { switchTab('graph'); return true; }
    if (state.activeTab === 'console') {
      switchTab('console');
      // Restore the secondary view too (drivers / usage).
      const v = localStorage.getItem('bc_console_view') || 'drivers';
      if (v !== 'drivers' && window.switchConsoleView) window.switchConsoleView(v);
      return true;
    }

    return false;
  } catch (e) { return false; }
}

// ── UI shell ──────────────────────────────────────────────────────

export function updateChatModeBar() {
  const isReader = window._currentTab === 'reader';
  const isWriting = window._currentTab === 'write';
  const rpEntryBtn = document.getElementById('roleplayEntryBtn');

  // Role-play entry: home page only (no reading context needed).
  // 写作页由 .right.writing-mode 一并隐藏。
  if (rpEntryBtn) rpEntryBtn.style.display = (isReader || isWriting) ? 'none' : '';

  // 模式预选只属于「阅读页 + 空白会话」。
  if (typeof window.updateModePrelude === 'function') window.updateModePrelude();
}

// ── Context bar ────────────────────────────────────────────────────

function ensureContextBar() {
  if (document.getElementById('contextBar')) return;
  const chatLog = document.getElementById('chatLog');
  const bar = document.createElement('div');
  bar.id = 'contextBar';
  bar.className = 'context-bar';
  bar.style.display = 'none';
  chatLog.parentNode.insertBefore(bar, chatLog);
}

export function updateContextBar(bookTitle, chapterTitle) {
  ensureContextBar();
  const bar = document.getElementById('contextBar');
  if (!bookTitle) { bar.style.display = 'none'; return; }
  bar.style.display = '';
  // 图标用项目统一的 lucide（丑拒 emoji）；书名/章名要转义，这里改了 innerHTML。
  bar.innerHTML = '<i data-lucide="book-open"></i> 正在阅读《'
    + escapeHtml(bookTitle) + '》'
    + (chapterTitle ? ' · ' + escapeHtml(chapterTitle) : '');
}

export function hideContextBar() {
  const bar = document.getElementById('contextBar');
  if (bar) bar.style.display = 'none';
}

export function clearChatLog() {
  const log = document.getElementById('chatLog');
  // Remove all children EXCEPT the context bar (it lives in chatBody, not chatLog)
  log.innerHTML = '';
}

export function setChatPanelVisible(visible) {
  const splitter = document.getElementById('splitter');
  const chatPanel = document.getElementById('chatPanel');
  const contentLeft = document.getElementById('contentLeft');

  if (visible) {
    splitter.style.display = '';
    chatPanel.classList.remove('hidden');
    contentLeft.style.flex = '';
    contentLeft.style.width = '';
    chatPanel.style.flex = '';
    chatPanel.style.width = '';
  } else {
    splitter.style.display = 'none';
    chatPanel.classList.add('hidden');
    contentLeft.style.flex = '1';
    contentLeft.style.width = '';
    chatPanel.style.flex = '';
    chatPanel.style.width = '';
  }
  updateChatModeBar();
  resizeNative();   // 容器宽度变了，通知 epub.js 重排（iframe 排版宽度不跟随宿主）
}

// The panel/splitter width transition is 280ms. The reader iframe must be
// told its new width only after the host has settled: resizing mid-animation
// bakes in an intermediate width and the paginated page never matches the
// final panel. Coalesce rapid toggles to a single resize.
let _chatPanelResizeTimer = null;
function scheduleChatPanelResize() {
  if (_chatPanelResizeTimer) clearTimeout(_chatPanelResizeTimer);
  const reduced = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced) {
    _chatPanelResizeTimer = null;
    resizeNative();
    return;
  }
  _chatPanelResizeTimer = setTimeout(function () {
    _chatPanelResizeTimer = null;
    resizeNative();
  }, 320);
}

export function toggleChatPanel() {
  const panel = document.getElementById('chatPanel');
  const btn = document.getElementById('chatToggleBtn');
  const splitter = document.getElementById('splitter');
  const contentLeft = document.getElementById('contentLeft');

  if (panel.classList.contains('collapsed')) {
    panel.classList.remove('collapsed');
    if (_chatToggleMorph) _chatToggleMorph.morphTo(CHAT_ICON_MINUS);
    btn.title = '收起对话';
    splitter.classList.remove('collapsed');
    contentLeft.style.flex = '';
    contentLeft.style.width = '';
    panel.style.flex = '';
    panel.style.width = '';
  } else {
    panel.classList.add('collapsed');
    if (_chatToggleMorph) _chatToggleMorph.morphTo(CHAT_ICON_PLUS);
    btn.title = '展开对话';
    splitter.classList.add('collapsed');
    contentLeft.style.flex = '1';
    contentLeft.style.width = '';
    panel.style.flex = '';
    panel.style.width = '';
  }
  // 同 setChatPanelVisible：容器宽度变化后重排阅读区，但要等过渡结束
  scheduleChatPanelResize();
}

// ── Per-tab chat session ──────────────────────────────────────────
// 主页 / 阅读页 / 写作页是三条独立的会话线。三个界面的提示词本来就不同
// （主页 <home-context>、阅读页 <reading-context>、写作页 agent prompt），
// 会话串过去会把历史、标题、mode 一起带错页。
//
// 指针持久化在 localStorage 的 bc_tab_sessions：
//   home:   {id, day}
//   reader: {id, day, bookId}
//   write:  {id, day}
// day = 这条线最后一次被使用的本地日期。切页只恢复「本日」的会话，
// 隔天再切回来按新会话处理 —— 否则昨天的对话会被原样搬到今天。
const TAB_SESSIONS_KEY = 'bc_tab_sessions';
const CHAT_TABS = ['home', 'reader', 'write'];

let _activeChatTab = null;       // S.currentSessionId 当前属于哪条线
let _tabSessionOverride = null;  // 会话面板跨页打开：{sid, tab, bookId}

function _todayKey() {
  const d = new Date();
  return d.getFullYear() + '-'
    + String(d.getMonth() + 1).padStart(2, '0') + '-'
    + String(d.getDate()).padStart(2, '0');
}

function _readTabSessions() {
  try {
    const raw = localStorage.getItem(TAB_SESSIONS_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (_) { return {}; }
}

function _writeTabSessions(map) {
  try { localStorage.setItem(TAB_SESSIONS_KEY, JSON.stringify(map)); } catch (_) {}
}

/** 记下某条线的当前会话（切页恢复与刷新恢复共用这一份真相）。 */
export function recordTabSession(tab, sid, opts) {
  if (CHAT_TABS.indexOf(tab) < 0 || !sid) return;
  const map = _readTabSessions();
  const entry = { id: sid, day: _todayKey() };
  if (tab === 'reader') {
    entry.bookId = (opts && opts.bookId) || S.currentBook || '';
  }
  if (tab === 'write') {
    // 写作会话按作品归档：显式传来的一轮现场优先（可能是别的作品的
    // 会话被点开），否则取写作页当前打开的那本。
    if (opts && opts.workId !== undefined && opts.workId !== null) {
      entry.workId = opts.workId || '';
    } else {
      const ctx = window.WritingUI && window.WritingUI.context
        ? window.WritingUI.context() : null;
      entry.workId = (ctx && ctx.workId) || '';
    }
  }
  map[tab] = entry;
  _writeTabSessions(map);
}

export function clearTabSession(tab) {
  const map = _readTabSessions();
  if (!map[tab]) return;
  delete map[tab];
  _writeTabSessions(map);
}

/** 会话被删除/归档/加载失败时，清掉所有指向它的线。 */
export function clearTabSessionById(sid) {
  if (!sid) return;
  const map = _readTabSessions();
  let changed = false;
  Object.keys(map).forEach(function (t) {
    if (map[t] && map[t].id === sid) { delete map[t]; changed = true; }
  });
  if (changed) _writeTabSessions(map);
  if (S.currentSessionId === sid) S.currentSessionId = '';
}

export function tabSessionFor(tab) {
  const entry = _readTabSessions()[tab];
  return entry && entry.id ? entry : null;
}

/** 当前可见对话属于哪条线（流式回复落盘时也要认这条线）。 */
export function activeChatTab() {
  return _activeChatTab || 'home';
}

/** 采用一个会话 id：写进当前线，并刷新它的「本日」标记。 */
export function adoptSession(sid, opts) {
  if (!sid) return;
  S.currentSessionId = sid;
  recordTabSession((opts && opts.tab) || activeChatTab(), sid, opts);
}

function showFreshChat() {
  if (typeof window.resetChatView === 'function') { window.resetChatView(); return; }
  if (S._chatAbort) { S._chatAbort.abort(); S._chatAbort = null; }
  S.chatEpoch++;
  S.chatHistory = [];
  S.chatLastTimestamp = 0;
  S.currentSessionId = '';
  const log = document.getElementById('chatLog');
  if (log) log.innerHTML = '<div class="msg agent">' + getWelcomeMessage() + '</div>';
  refreshUsage();
}

/** 取出某条线本日的会话；不是本日的线一律作废。 */
function _takeTabSession(tab) {
  const map = _readTabSessions();
  const entry = map[tab];
  if (!entry || !entry.id) return null;
  if (entry.day !== _todayKey()) {
    delete map[tab];
    _writeTabSessions(map);
    return null;
  }
  if (tab === 'reader' && entry.bookId) {
    // 阅读页的会话绑在书上：回到阅读页时把书一起带回来，refreshBooks
    // 才会保留这本书（否则默认落到下拉框第一本，会话立刻被切书清掉）。
    S.currentBook = entry.bookId;
    S._lastLoadedBook = entry.bookId;
  }
  return entry;
}

/** 进入一条会话线：恢复本日会话，或开一个干净的新会话界面。 */
function _enterChatTab(tab) {
  if (_tabSessionOverride) {
    const ov = _tabSessionOverride;
    _tabSessionOverride = null;
    const lineTab = ov.tab || tab;
    if (lineTab === 'reader' && ov.bookId) {
      // 会话面板点开的是别的书的阅读会话：阅读页跟着切过去，
      // 否则聊天上下文和书页内容会来自两本书。
      S.currentBook = ov.bookId;
      S._lastLoadedBook = ov.bookId;
    }
    _activeChatTab = lineTab;
    adoptSession(ov.sid, { tab: lineTab, bookId: ov.bookId });
    return;
  }

  const entry = _takeTabSession(tab);
  _activeChatTab = tab;
  if (entry && typeof window.loadSession === 'function') {
    S.currentSessionId = entry.id;
    window.loadSession(entry.id, { silent: true });
    return;
  }

  clearTabSession(tab);
  // 新会话界面的模式按页签归位：主页/阅读页默认沉浸；扮演只在带了
  // 角色时保留（没有角色的 roleplay 请求没有意义）。
  if ((tab === 'home' || tab === 'reader')
      && !(S.currentMode === 'roleplay' && S.currentCharacterName)) {
    S.currentMode = 'immersive';
    if (typeof window.setModeLabel === 'function') window.setModeLabel('immersive');
  }
  showFreshChat();
}

/** 会话导航栏点开一条别的线的会话：切到对应页签并采用它。 */
export function openSessionTab(sid, tab, bookId) {
  _tabSessionOverride = { sid: sid, tab: tab || 'home', bookId: bookId || '' };
  switchTab(_tabSessionOverride.tab);
}

export function switchTab(tab, skipLoad) {
  // 切离阅读 tab 前暂存视口 CFI。.page 的 display 切换会让滚动容器塌成 0
  // 尺寸、scrollTop 丢失——不暂存的话切回来视口停在书首，随后的 scrolled
  // 事件把 (0,0) 落库，表现为"切页后进度收敛到 1/N"。
  if (window._currentTab === 'reader' && tab !== 'reader') {
    // 沉浸态是阅读页的姿势，别把 body 类带到别的页上（侧栏/聊天都藏了）。
    leaveFocusModeSilently();
    stashViewport();
  }
  // 点导航栏 = 离开现场：写作页/阅读页的引用都是「选一下就成立」的，
  // 切页后既看不到原文也没法撤销，留着只会在下一次发送时悄悄带上。
  if (typeof window.clearWritingSelection === 'function') {
    window.clearWritingSelection();
  }
  if (typeof window.clearWorkspaceSelection === 'function') {
    window.clearWorkspaceSelection();
  }
  if (typeof window.clearBookSelection === 'function') {
    window.clearBookSelection();
  }
  window._currentTab = tab;
  // 会话面板的页签跟着界面走：写作页只有「按作品」，其它页没有写作页签。
  if (window.refreshSessionChrome) window.refreshSessionChrome();

  document.querySelectorAll('.sidebar .tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const tabEl = document.querySelector(`.sidebar .tab[onclick*="${tab}"]`);
  if (tabEl) tabEl.classList.add('active');
  const pageEl = document.getElementById('page-' + tab);
  if (pageEl) pageEl.classList.add('active');

  // 进入另一条会话线时恢复它本日的会话（没有就开干净的新界面）。
  // openSessionTab 走 _tabSessionOverride，由调用方随后自己渲染。
  // 主题/图谱/笔记这类非聊天页不碰会话线，回来时原样还在。
  const _isChatTab = CHAT_TABS.indexOf(tab) >= 0;
  if (_isChatTab && (_activeChatTab !== tab || _tabSessionOverride)) {
    _enterChatTab(tab);
  } else if (!_isChatTab && _tabSessionOverride) {
    // 目标不是聊天页：别把 override 留到下一次切页。
    _tabSessionOverride = null;
  }

  // Mini notes panel is a reading-context widget — keep it off other tabs
  // (its fixed position would float over e.g. the theme preview column).
  if (window.setMiniPanelVisible) window.setMiniPanelVisible(tab === 'reader');

  if (tab === 'home') {
    setChatPanelVisible(true);
    hideContextBar();
    if (!skipLoad) loadShelf();
  }
  if (tab === 'reader') {
    setChatPanelVisible(true);
    if (!skipLoad) refreshBooks();
    // 按切走前暂存的 CFI 恢复视口（stashViewport 的配对操作）。放在
    // setChatPanelVisible 之后：容器此时已有真实尺寸，定位才有意义。
    restoreViewport();
    // Role-play is a home-page-only feature — reset to immersive on reader tab
    if (S.currentMode === 'roleplay') {
      S.currentMode = 'immersive';
      S.currentCharacterName = '';
      S.currentRoleKind = 'character';
      S.currentRoleRef = '';
      S.currentMaxChapter = null;
      if (typeof window.setModeLabel === 'function') window.setModeLabel('immersive');
      var rpLabel = document.querySelector('#roleplayEntryBtn .mode-label');
      if (rpLabel) rpLabel.textContent = '角色对话';
    }
  }
  if (tab === 'notes') {
    setChatPanelVisible(false);
    if (typeof window.renderNotesPage === 'function') window.renderNotesPage();
  }
  if (tab === 'write') {
    // 写作页要对话：复用同一个聊天面板，不另造一个聊天框。
    setChatPanelVisible(true);
    hideContextBar();
    const panel = document.getElementById('chatPanel');
    if (panel) panel.classList.add('writing-mode');
    const input = document.getElementById('chatInput');
    if (input) input.placeholder = '告诉我要写什么：建书、建章、写这一章、改一段…';
    if (!skipLoad && window.WritingUI) window.WritingUI.open();
  } else {
    const panel = document.getElementById('chatPanel');
    if (panel) panel.classList.remove('writing-mode');
    const input = document.getElementById('chatInput');
    if (input) input.placeholder = '输入问题...（可先选中书页文字）';
  }
  if (tab === 'theme') {
    setChatPanelVisible(false);
    hideContextBar();
    refreshAgentProfile();
    refreshUserProfile();
    refreshBenchPreview();
  }
  if (tab === 'console') {
    setChatPanelVisible(false);
    hideContextBar();
    if (!skipLoad && window.ConsoleUI) window.ConsoleUI.load();
  }
  if (tab === 'graph') {
    setChatPanelVisible(false);
    hideContextBar();
    if (!skipLoad) {
      // Reset to graph view (in case user was on manage view last time)
      if (typeof window.switchGraphView === 'function') {
        window.switchGraphView('graph');
      }
      // Dynamic import — ensures graph-viz.js is loaded before we call its API
      import('./graph-viz.js').then(function (mod) {
        return mod.initGraphPage().then(function () {
          mod.loadGraphForBook(S.currentBook);
        });
      }).catch(function (err) {
        // Surface init errors instead of swallowing them — otherwise the
        // loading overlay stays up forever with no clue why.
        console.error('[graph] init failed:', err);
        const overlay = document.getElementById('graphLoadingOverlay');
        const errState = document.getElementById('graphErrorState');
        if (overlay) overlay.style.display = 'none';
        if (errState) errState.style.display = 'flex';
      });
    }
  }

  if (!skipLoad) saveViewState();
}

// ── Cross-tab jump: notes / graph node → 原文位置 ────────────────────
/** 跳到某本书的 (chapter, chunk)，可选 CFI 精确落点。返回是否成功。
 *
 *  笔记页 / 阅读器浮窗的批注跳转、图谱页节点的「跳转原文」共用此入口。
 *
 *  ⚠️ 不能写成 switchTab('reader') + 定位两步 —— switchTab 会做两件会把
 *  跳转吃掉的副作用：
 *   1) refreshBooks() → 异步再跑一次 selectBook()，把视口恢复到 DB 阅读
 *      进度（覆盖刚落下的跳转），并把 bookSelect 的值改回旧书；
 *   2) restoreViewport()，它等宿主布局稳定（≥2 帧）才 display 暂存锚 ——
 *      晚于本次跳转执行，同样把视口拉回原处。
 *  所以这里 skipLoad 掉 refreshBooks、自己显式驱动切书，并在落跳转前
 *  invalidatePendingRestore() 作废所有在途/待触发的恢复定位（含 initNative
 *  的迟到复核，见 epub-reader.js 的 _restoreGen）。 */
export async function jumpToPosition(bookId, chapterIdx, chunkIdx, cfi) {
  if (!bookId) return false;
  switchTab('reader', true);
  // 等阅读区真正可见：切 tab 的同一 tick 里宿主还是 0×0，此刻落点会打在
  // 零尺寸舞台上（见 whenHostVisible 注释）。
  // ⚠️ 不要在这里"把重排推迟到跳转之后"：宿主由隐藏变可见触发的那次重排
  // 是 epub.js 重新按真实尺寸布局所必需的，掐掉它反而让 display() 卡住
  // （实测：推迟重排后视口 25s 不动、evaluate 直接超时；不推迟则精确落点）。
  await whenHostVisible();
  const sel = document.getElementById('bookSelect');
  // 下拉框可能还没建（本次会话从未进过阅读页）→ 先补一次选项。
  if (sel && !sel.querySelector(`option[value="${bookId}"]`)) {
    await loadBookDropdown();
  }
  if (S.currentBook !== bookId) {
    if (!sel || !sel.querySelector(`option[value="${bookId}"]`)) return false;
    sel.value = bookId;
    await selectBook();
    if (S._lastLoadedBook !== bookId) return false;   // 加载失败：别在旧书上乱跳
  }

  const chapter = chapterIdx | 0, chunk = chunkIdx | 0;
  invalidatePendingRestore();
  if (S.renderMode === 'native') {
    if (cfi && await jumpToCfi(cfi)) { saveViewState(); return true; }
    // 无 CFI（旧数据）或 CFI 失效：走语义锚定位，同样是 native 可见的路径
    await jumpToAnchor(chapter, chunk);
  } else {
    S.currentChapter = chapter;
    S.currentChunk = chunk;
    const chSel = document.getElementById('chapterSelect');
    if (chSel) chSel.value = chapter;
    renderBookProgress(chapter, chunk);
    if (window.loadChunk) await window.loadChunk();
  }
  saveViewState();
  return true;
}

// ── HTML onclick access ───────────────────────────────────────────
window.switchTab = switchTab;
window.openSessionTab = openSessionTab;
window.recordTabSession = recordTabSession;
window.clearTabSession = clearTabSession;
window.clearTabSessionById = clearTabSessionById;
window.tabSessionFor = tabSessionFor;
window.adoptSession = adoptSession;
window.getActiveChatTab = activeChatTab;
window.jumpToPosition = jumpToPosition;
window.toggleChatPanel = toggleChatPanel;
window.updateChatModeBar = updateChatModeBar;
window.saveViewState = saveViewState;
window.updateContextBar = updateContextBar;
window.hideContextBar = hideContextBar;
window.clearChatLog = clearChatLog;
window.refreshBooks = refreshBooks;

// ── Init ──────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function () {

  // 导入书籍按钮在书架页标题旁（index.html），弹窗仍在这里建。

  // ── Create import popup panel ────────────────────────────────
  if (!document.getElementById('importPanel')) {
    const panel = document.createElement('div');
    panel.id = 'importPanel';
    panel.className = 'import-popup';
    panel.innerHTML = `
      <div class="import-popup-header">
        <span><i data-lucide="download"></i> 导入书籍 — 扫描本地书库</span>
        <button onclick="toggleImportPanel()" title="关闭"><i data-lucide="x"></i></button>
      </div>
      <div class="import-popup-body" id="importPopupBody">
        <div style="color:var(--text-muted);font-size:var(--fs-caption);text-align:center;padding:var(--space-lg)">
          点击下方按钮扫描未入库的 EPUB/MOBI 文件
        </div>
      </div>
      <div class="import-popup-footer">
        <button class="primary" onclick="scanAndShowFiles()"><i data-lucide="refresh-cw"></i> 扫描导入</button>
      </div>
    `;
    document.body.appendChild(panel);
  }

  // ── Update chat mode bar for current tab ────────────────────
  updateChatModeBar();

  // ── Replace static welcome with time-based greeting ──────────
  const chatLog = document.getElementById('chatLog');
  const firstMsg = chatLog && chatLog.querySelector('.msg.agent');
  if (firstMsg) firstMsg.textContent = getWelcomeMessage();

  // ── Load shelf sort preference ──────────────────────────────
  loadShelfSort();
  loadShelfScene();
  initStickyCompact();
  initSortPanel();
  initShelfReorder();
  updateShelfSortUI();

  // Restore is async: it first resolves the fork family's latest branch.
  restoreViewState().then(function (restored) {
    if (!restored) {
      switchTab('home');
      loadShelf();
    }
    // 圆环开屏水位:恢复的会话回查真实值,新访客给 system+tools 基线
    refreshUsage();
  });
  checkOnboarding();  // 首次运行引导:未配置主 LLM 时弹出可视化填 Key 弹窗
  refreshWatchStatus();
  setInterval(refreshWatchStatus, 30000);

  // ── Scroll-based lazy loading for search left panel ─────────
  const searchLeftEl = document.getElementById('searchLeft');
  if (searchLeftEl) {
    searchLeftEl.addEventListener('scroll', onSearchLeftScroll);
  }

  // ── Splitter drag ───────────────────────────────────────────
  const splitter = document.getElementById('splitter');
  let dragging = false, dragStartX = 0, dragStartWidth = 0, dragContainerWidth = 0;

  splitter.addEventListener('mousedown', function (e) {
    dragging = true;
    dragStartX = e.clientX;
    const contentLeft = document.getElementById('contentLeft');
    const chatPanel = document.getElementById('chatPanel');
    dragStartWidth = contentLeft.offsetWidth;
    dragContainerWidth = contentLeft.parentElement.offsetWidth;
    contentLeft.style.flex = '0 0 ' + dragStartWidth + 'px';
    contentLeft.style.width = '';
    chatPanel.style.flex = '1';
    chatPanel.style.width = '';
    chatPanel.style.minWidth = '0';
    chatPanel.style.maxWidth = 'none';
    splitter.classList.add('active');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    // 拖拽期间暂停尺寸观察：contentLeft 逐帧变宽会让 observer 每帧触发重排
    // （fork 的 manager.resize 内部 clear() 全量重渲染），拖拽会卡。mouseup
    // 时 resume 并补一次终态重排。
    suspendHostResizeObserver();
    e.preventDefault();
  });

  document.addEventListener('mousemove', function (e) {
    if (!dragging) return;
    e.preventDefault();
    const contentLeft = document.getElementById('contentLeft');
    const delta = e.clientX - dragStartX;
    const newWidth = dragStartWidth + delta;
    const minW = dragContainerWidth * 0.15;
    const maxW = dragContainerWidth * 0.75;
    const clamped = Math.min(maxW, Math.max(minW, newWidth));
    contentLeft.style.flex = '0 0 ' + clamped + 'px';
  });

  document.addEventListener('mouseup', function () {
    if (!dragging) return;
    dragging = false;
    const contentLeft = document.getElementById('contentLeft');
    const chatPanel = document.getElementById('chatPanel');
    const splitter = document.getElementById('splitter');
    // Use flex-available width as denominator so flex-basis % matches
    // the actual pixel width.  Without this correction the splitter
    // width leaks into the divisor, shaving ~0.5 % off contentLeft
    // on every drag cycle — accumulating into a permanent gap.
    const availWidth = dragContainerWidth - splitter.offsetWidth;
    const pct = (contentLeft.offsetWidth / availWidth) * 100;
    contentLeft.style.flex = '0 0 ' + pct + '%';
    chatPanel.style.flex = '1';
    chatPanel.style.width = '';
    splitter.classList.remove('active');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    // 恢复尺寸观察并补一次终态重排（拖拽中间尺寸全部作废）。
    // 先 resume（内部会 resizeNative），替代原来的裸 resizeNative 调用。
    resumeHostResizeObserver();
  });

  // ── Keyboard shortcut: Ctrl+Enter for send ─────────────────
  document.getElementById('chatInput').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); window.sendMessage(); }
  });

  // ── Theme init ──────────────────────────────────────────────
  const settings = loadSettings();
  applySettings(settings);
  populateThemeForm(settings);
  loadAgentProfile();
  loadUserProfile();

  // ── Click-outside listeners ─────────────────────────────────
  // Session panel — two-tier hit-test:
  // 1. contains() catches subnav tabs / buttons that stay in the DOM.
  // 2. Coordinate fallback catches elements detached by innerHTML
  //    (e.g. session-book-group in drill-down, session items after load).
  //    innerHTML is synchronous — it shrinks the panel BEFORE the click
  //    event bubbles here, so the coordinate check must use the *current*
  //    rect, not a cached one.
  document.addEventListener('click', function (e) {
    const panel = document.getElementById('sessionPanel');
    const toggle = document.getElementById('sessionToggle');
    if (!panel || !panel.classList.contains('visible')) return;
    if (toggle && toggle.contains(e.target)) return;
    // Tier 1: element still in DOM tree (subnav, search bar, buttons, …)
    if (panel.contains(e.target)) return;
    // Tier 2: coordinate-based for elements detached by innerHTML
    const rect = panel.getBoundingClientRect();
    const inside = e.clientX >= rect.left && e.clientX <= rect.right
                && e.clientY >= rect.top  && e.clientY <= rect.bottom;
    if (!inside) panel.classList.remove('visible');
  });
});
