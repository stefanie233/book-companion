/* Book Companion — entry point: init, view state, tab switching, UI shell. */

import { S } from './state.js';
import { getWelcomeMessage } from './greetings.js';
import { loadSettings, applySettings, populateThemeForm } from './theme.js';
import { loadShelfSort, loadShelfScene, updateShelfSortUI, loadShelf, loadBookDropdown, refreshBooks, refreshWatchStatus, initStickyCompact } from './shelf.js';
import { selectBook } from './reader.js';
import { refreshUsage } from './context-usage.js';
import { checkOnboarding } from './onboarding.js';
import { resizeNative, suspendHostResizeObserver, resumeHostResizeObserver,
         stashViewport, restoreViewport } from './epub-reader.js';
import { onSearchLeftScroll } from './search.js';
import { createMorph } from '../lib/morphicons.dom.js';
import './mode.js';     // side-effect: registers window.toggleModePicker / window.setMode

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

export function restoreViewState() {
  try {
    const raw = localStorage.getItem('bc_view');
    if (!raw) return false;
    const state = JSON.parse(raw);

    if (state.activeTab === 'reader' && state.bookId) {
      S.currentBook = state.bookId;
      // View-state restore must NOT look like a book switch — mark the book
      // as already-loaded so selectBook() keeps the restored session alive.
      S._lastLoadedBook = state.bookId;
      S.currentSessionId = state.sessionId || '';
      switchTab('reader', true);
      loadBookDropdown().then(() => {
        document.getElementById('bookSelect').value = state.bookId;
        selectBook().then(() => saveViewState());
      });
      return true;
    }

    if (state.activeTab === 'home') { switchTab('home'); return true; }
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
  const modeToggle = document.getElementById('modeToggle');
  const modePicker = document.getElementById('modePicker');
  const sessionToggle = document.getElementById('sessionToggle');
  const importBtn = document.getElementById('importBooksBtn');
  const rpEntryBtn = document.getElementById('roleplayEntryBtn');

  if (modeToggle) modeToggle.style.display = isReader ? '' : 'none';
  if (modePicker) { modePicker.classList.remove('visible'); modePicker.style.display = isReader ? '' : 'none'; }
  if (importBtn) importBtn.style.display = isReader ? 'none' : '';
  // Role-play entry: visible on home page only (no reading context needed)
  if (rpEntryBtn) rpEntryBtn.style.display = isReader ? 'none' : '';
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
  let text = '\u{1F4D6} 正在阅读《' + bookTitle + '》';
  if (chapterTitle) text += ' · ' + chapterTitle;
  bar.textContent = text;
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

export function toggleChatPanel() {
  const panel = document.getElementById('chatPanel');
  const btn = document.getElementById('chatToggleBtn');
  const splitter = document.getElementById('splitter');
  const contentLeft = document.getElementById('contentLeft');

  if (panel.classList.contains('collapsed')) {
    panel.classList.remove('collapsed');
    if (_chatToggleMorph) _chatToggleMorph.morphTo(CHAT_ICON_MINUS);
    btn.title = '收起对话';
    splitter.style.display = '';
    contentLeft.style.flex = '';
    contentLeft.style.width = '';
    panel.style.flex = '';
    panel.style.width = '';
  } else {
    panel.classList.add('collapsed');
    if (_chatToggleMorph) _chatToggleMorph.morphTo(CHAT_ICON_PLUS);
    btn.title = '展开对话';
    splitter.style.display = 'none';
    contentLeft.style.flex = '1';
    contentLeft.style.width = '';
    panel.style.flex = '';
    panel.style.width = '';
  }
  resizeNative();   // 同 setChatPanelVisible：容器宽度变化后重排阅读区
}

export function switchTab(tab, skipLoad) {
  // 切离阅读 tab 前暂存视口 CFI。.page 的 display 切换会让滚动容器塌成 0
  // 尺寸、scrollTop 丢失——不暂存的话切回来视口停在书首，随后的 scrolled
  // 事件把 (0,0) 落库，表现为"切页后进度收敛到 1/N"。
  if (window._currentTab === 'reader' && tab !== 'reader') stashViewport();
  window._currentTab = tab;

  document.querySelectorAll('.sidebar .tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const tabEl = document.querySelector(`.sidebar .tab[onclick*="${tab}"]`);
  if (tabEl) tabEl.classList.add('active');
  const pageEl = document.getElementById('page-' + tab);
  if (pageEl) pageEl.classList.add('active');

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
      S.currentMaxChapter = null;
      document.getElementById('modeLabel').textContent = '沉浸伴读';
      var rpLabel = document.querySelector('#roleplayEntryBtn .mode-label');
      if (rpLabel) rpLabel.textContent = '角色对话';
    }
  }
  if (tab === 'notes') {
    setChatPanelVisible(false);
    if (typeof window.renderNotesPage === 'function') window.renderNotesPage();
  }
  if (tab === 'theme') {
    setChatPanelVisible(false);
    hideContextBar();
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

// ── HTML onclick access ───────────────────────────────────────────
window.switchTab = switchTab;
window.toggleChatPanel = toggleChatPanel;
window.saveViewState = saveViewState;
window.updateContextBar = updateContextBar;
window.hideContextBar = hideContextBar;
window.clearChatLog = clearChatLog;
window.refreshBooks = refreshBooks;

// ── Init ──────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function () {

  // ── Create book import button (home page chat) ──────────────
  const modeBar = document.querySelector('.chat-mode-bar');
  if (modeBar && !document.getElementById('importBooksBtn')) {
    const btn = document.createElement('button');
    btn.id = 'importBooksBtn';
    btn.className = 'import-books-btn';
    btn.title = '扫描本地书库导入新书';
    btn.innerHTML = '<i data-lucide="download"></i> 导入书籍';
    btn.onclick = function () { window.toggleImportPanel(); };
    modeBar.insertBefore(btn, modeBar.firstChild);
  }

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
  updateShelfSortUI();

  if (!restoreViewState()) {
    switchTab('home');
    loadShelf();
  }
  refreshUsage();  // 圆环开屏水位:恢复的会话回查真实值,新访客给 system+tools 基线
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

  // ── Click-outside listeners ─────────────────────────────────
  // Mode picker
  document.addEventListener('click', function (e) {
    const bar = document.getElementById('modeToggle');
    const picker = document.getElementById('modePicker');
    if (bar && picker && !bar.contains(e.target) && !picker.contains(e.target)) {
      picker.classList.remove('visible');
    }
  });

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
