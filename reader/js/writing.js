// WriteGraph workbench — works, chapters, context, streaming draft, settle.
//
// The backend owns every truth decision: this module only renders the ledger,
// posts user intent, and streams text. Never optimistically invent a fact here.

import { renderStoryMap } from './writing-map.js';
import { renderTensionCurve } from './writing-tension.js';
import { escapeHtml, escapeHtmlAttr } from './utils.js';

const S = {
  works: [],
  workId: '',
  workTitle: '',
  chapters: [],
  volumes: [],
  // 左栏是一棵「作品 → 卷 → 章 → 场景」的树：只展开当前工作台，
  // 根目录折叠状态记在本地；切作品不重置筛选。
  rootCollapsed: readFlag('bc.writeRootCollapsed'),
  collapsedVolumes: new Set(),
  expandedChapters: new Set(),
  workspaceOpen: false,
  workspaceFilter: '',
  index: 0,
  chapter: null,
  contract: null,
  // 参考风格（AuthorGraph-Voice）：默认关闭；作品级默认存 style_profile。
  styleAuthorId: '',
  styleAuthorName: '',
  styleAuthors: [],
  styleAuthorsLoaded: false,
  styleInfo: null,
  stylePending: null,
  generating: false,
  // 编辑器脏状态与空闲自动保存：输入只改本地，不每键打后端。
  autosaveTimer: null,
  autosaving: false,
  autosavePending: false,
  vcsStale: false,
  // 保存点（最近一次「保存」的版本摘要）：用来区分「已暂存」与「已保存」。
  savePoint: null,
  // 编辑器撤销/重做栈：每项 {content, start, end}；undoAt 用来合并输入串。
  undoStack: [],
  redoStack: [],
  undoAt: 0,
  preEdit: null,
  filter: 'all',
  view: 'editor',
  // 新建作品/章节的行内草稿：{kind:'work'|'chapter', value, focus}
  // 不先落库——用户敲了什么才建什么，空栏等于没发生。
  draft: null,
  // 改名保存失败时，把名字塞回重命名模式重试，别让一次网络错误吞掉输入。
  renameRetry: null,
  // 写作 git：状态、历史、当前 diff 的起止引用。
  vcs: null,
  vcsLog: [],
  vcsStashes: [],
  vcsReflog: [],
  vcsFeedback: [],
  logAllBranches: true,
  logOnlyChapter: false,
  diffFrom: 'HEAD',
  diffTo: 'WORK',
  diffCommitId: '',
  // 合并/撤销/摘取的待裁决请求：{kind, path, body, conflicts, choices}
  mergeReq: null,
};

//: 停止输入多久后自动落盘。2 秒是「手刚离键盘」与「别太频繁」的折中。
const AUTOSAVE_IDLE_MS = 2000;

const FILTERS = {
  all: () => true,
  todo: (s) => s === 'planned' || s === 'drafting',
  draft: (s) => s === 'drafted' || s === 'stale',
  reviewed: (s) => s === 'reviewed',
  final: (s) => s === 'final',
};

const FILTER_LABELS = [
  ['all', '全部'], ['todo', '待写'], ['draft', '草稿'],
  ['reviewed', '已审'], ['final', '完成'],
];

const STATUS_LABELS = {
  planned: '待写', drafting: '草稿', drafted: '草稿',
  stale: '草稿', reviewed: '已审', final: '完成',
};

const $ = (id) => document.getElementById(id);

/** 本地开关（根目录折叠）：读写失败时安静退回默认值。 */
function readFlag(key) {
  try { return localStorage.getItem(key) === '1'; } catch (_) { return false; }
}

function writeFlag(key, on) {
  try { localStorage.setItem(key, on ? '1' : '0'); } catch (_) { /* private mode */ }
}

function setStatus(text, kind = '') {
  const el = $('writeStatus');
  if (!el) return;
  el.textContent = text || '';
  el.dataset.kind = kind;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.ok === false) {
    throw new Error(body.error || `请求失败 (${res.status})`);
  }
  return body;
}

/** 冲突（409）要拿到 body 才能裁决，所以单独留一个不抛错的版本。 */
async function apiRaw(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok && body.ok !== false, status: res.status, body };
}

function renderWorks() {
  renderWorkspaceMenu();
  renderChapters();
}

// ── 写作台（作品切换下拉）─────────────────────────────────────────
// 只把「当前这一本」放进工作台；其余作品收进下拉，别让书单常驻左栏。

function renderWorkspaceMenu() {
  const list = $('writeWorkspaceList');
  if (!list) return;
  const current = S.works.find((w) => w.id === S.workId);
  $('writeWorkspaceName').textContent = current
    ? (current.title || '（未命名）') : '未选择作品';
  $('writeWorkspaceCount').textContent = S.works.length
    ? `${S.works.length} 本` : '';
  $('writeWorkspaceSearchWrap').hidden = S.works.length <= 5;
  list.textContent = '';
  const query = (S.workspaceFilter || '').trim().toLowerCase();
  const rows = S.works.filter((w) => !query
    || (w.title || '').toLowerCase().includes(query));
  if (!rows.length) {
    list.appendChild(el('p', 'write-workspace-empty',
      S.works.length ? '没有匹配的作品' : '还没有作品。'));
    return;
  }
  rows.forEach((w) => list.appendChild(workspaceOption(w)));
}

function workspaceOption(work) {
  const selected = work.id === S.workId;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'write-workspace-item';
  btn.setAttribute('role', 'option');
  btn.setAttribute('aria-selected', selected ? 'true' : 'false');
  btn.dataset.workId = work.id;
  btn.title = work.title || '';
  const iconEl = treeIcon('book-open', 'write-workspace-item-icon');
  if (work.pinned) {
    const pin = el('span', 'write-workspace-item-pin');
    pin.title = '已置顶';
    pin.innerHTML = '<i data-lucide="pin"></i>';
    iconEl.appendChild(pin);
  }
  btn.appendChild(iconEl);
  const main = el('span', 'write-workspace-item-main');
  main.append(
    el('span', 'write-workspace-item-title', work.title || '（未命名）'),
    el('span', 'write-workspace-item-meta',
       `${work.chapter_count || 0} 章 · ${work.total_words || 0} 字`));
  btn.appendChild(main);
  const check = el('span', 'write-workspace-item-check');
  check.innerHTML = '<i data-lucide="check"></i>';
  btn.appendChild(check);
  btn.onclick = () => chooseWork(work.id);
  btn.onkeydown = (event) => workspaceKeydown(event, work);
  btn.oncontextmenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    closeWorkspaceMenu();
    $('writeWorkspaceBtn').focus();
    openWorkContextMenu(event.clientX, event.clientY, work,
                        work.id === S.workId);
  };
  return btn;
}

function workspaceKeydown(event, work) {
  if (event.key === 'ContextMenu'
      || (event.shiftKey && event.key === 'F10')) {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    // 下拉项马上会被 closeWorkspaceMenu 藏起来，焦点交还触发器。
    $('writeWorkspaceBtn').focus();
    openWorkContextMenu(rect.left + 20, rect.bottom, work,
                        work.id === S.workId);
    return;
  }
  const items = [...$('writeWorkspaceList')
    .querySelectorAll('.write-workspace-item')];
  const i = items.indexOf(event.currentTarget);
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    const next = items[i + (event.key === 'ArrowDown' ? 1 : -1)];
    if (next) next.focus();
    else if (event.key === 'ArrowUp') {
      if (!$('writeWorkspaceSearchWrap').hidden) {
        $('writeWorkspaceSearch').focus();
      } else {
        $('writeWorkspaceBtn').focus();
      }
    }
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    event.currentTarget.click();
  }
}

async function chooseWork(workId) {
  closeWorkspaceMenu();
  if (workId !== S.workId) await selectWork(workId);
  const active = $('writeChapterList').querySelector('.write-tree-chapter.active')
    || $('writeChapterList').querySelector('.write-tree-root');
  if (active) active.focus();
}

function openWorkspaceMenu() {
  if (S.workspaceOpen) return;
  S.workspaceOpen = true;
  S.workspaceFilter = '';
  const search = $('writeWorkspaceSearch');
  if (search) search.value = '';
  renderWorkspaceMenu();
  $('writeWorkspaceMenu').hidden = false;
  $('writeWorkspaceBtn').setAttribute('aria-expanded', 'true');
  requestAnimationFrame(() => {
    if (!$('writeWorkspaceSearchWrap').hidden) {
      search.focus();
      return;
    }
    const current = $('writeWorkspaceList')
      .querySelector('[aria-selected="true"]')
      || $('writeWorkspaceList').querySelector('.write-workspace-item');
    if (current) current.focus();
  });
}

function closeWorkspaceMenu(refocus = false) {
  if (!S.workspaceOpen) return;
  S.workspaceOpen = false;
  $('writeWorkspaceMenu').hidden = true;
  $('writeWorkspaceBtn').setAttribute('aria-expanded', 'false');
  if (refocus) $('writeWorkspaceBtn').focus();
}

function toggleWorkspaceMenu() {
  if (S.workspaceOpen) closeWorkspaceMenu(true);
  else openWorkspaceMenu();
}

async function loadWorks() {
  const data = await api('/api/writing/works');
  S.works = data.works || [];
  if (S.workId && !S.works.some((w) => w.id === S.workId)) {
    S.workId = '';
    S.workTitle = '';
    S.chapters = [];
    S.volumes = [];
  }
  renderWorks();
}

// ── 右键菜单 + 作品置顶/删除 ──────────────────────────────────────
// 菜单挂到 body 上（position:fixed），左栏滚动容器裁不到它。

let _writeMenuEl = null;
let _writeMenuReturnFocus = null;

function ensureWriteContextMenu() {
  if (_writeMenuEl) return _writeMenuEl;
  const menu = document.createElement('div');
  menu.className = 'write-context-menu';
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  document.body.appendChild(menu);
  _writeMenuEl = menu;

  document.addEventListener('click', (event) => {
    if (menu.hidden || menu.contains(event.target)) return;
    closeWriteContextMenu();
  });
  document.addEventListener('contextmenu', (event) => {
    if (menu.hidden || menu.contains(event.target)) return;
    closeWriteContextMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (menu.hidden) return;
    const items = [...menu.querySelectorAll(
      '.write-context-item:not([disabled])')];
    if (event.key === 'Escape') {
      event.preventDefault();
      closeWriteContextMenu(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const i = items.indexOf(document.activeElement);
      const next = items[i + (event.key === 'ArrowDown' ? 1 : -1)];
      if (next) next.focus();
      else if (items.length) {
        (event.key === 'ArrowUp' ? items[items.length - 1] : items[0]).focus();
      }
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      if (items.length) {
        items[event.key === 'Home' ? 0 : items.length - 1].focus();
      }
    } else if (event.key === 'Tab') {
      closeWriteContextMenu();
    }
  }, true);
  document.addEventListener('scroll', () => closeWriteContextMenu(), true);
  window.addEventListener('resize', () => closeWriteContextMenu());
  window.addEventListener('blur', () => closeWriteContextMenu());
  return menu;
}

function openWriteContextMenu(x, y, items) {
  closeWorkspaceMenu();
  const menu = ensureWriteContextMenu();
  _writeMenuReturnFocus = document.activeElement;
  menu.replaceChildren(...items);
  menu.hidden = false;
  menu.style.left = '0px';
  menu.style.top = '0px';
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(
    x, window.innerWidth - rect.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(
    y, window.innerHeight - rect.height - 8))}px`;
  requestAnimationFrame(() => {
    const first = menu.querySelector('.write-context-item:not([disabled])');
    if (first) first.focus();
  });
}

function closeWriteContextMenu(refocus = false) {
  if (!_writeMenuEl || _writeMenuEl.hidden) return;
  _writeMenuEl.hidden = true;
  _writeMenuEl.replaceChildren();
  const target = _writeMenuReturnFocus;
  _writeMenuReturnFocus = null;
  if (refocus && target && document.contains(target)) target.focus();
}

function contextItem(icon, label, onPick, options = {}) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'write-context-item' + (options.danger ? ' danger' : '');
  btn.setAttribute('role', 'menuitem');
  btn.disabled = !!options.disabled;
  const iconEl = el('span', 'write-context-icon');
  iconEl.innerHTML = `<i data-lucide="${icon}"></i>`;
  btn.appendChild(iconEl);
  btn.appendChild(el('span', 'write-context-label', label));
  btn.onclick = () => {
    if (btn.disabled) return;
    closeWriteContextMenu();
    onPick();
  };
  return btn;
}

function contextSeparator() {
  const sep = document.createElement('div');
  sep.className = 'write-context-sep';
  sep.setAttribute('role', 'separator');
  return sep;
}

function openWorkContextMenu(x, y, work, current) {
  const items = [];
  if (!current) {
    items.push(contextItem('arrow-right-left', '切换到此作品',
      () => selectWork(work.id)));
  }
  items.push(contextItem(work.pinned ? 'pin-off' : 'pin',
    work.pinned ? '取消置顶' : '置顶', () => toggleWorkPin(work)));
  if (current) {
    items.push(contextItem('pencil', '重命名作品', () => {
      const label = $('writeChapterList').querySelector(
        '.write-tree-root .write-tree-label');
      if (label) startRenameWork(work, label);
      else setStatus('找不到作品根目录', 'error');
    }));
    items.push(contextItem('plus', '新建章节', () => createChapter()));
  }
  items.push(contextSeparator());
  items.push(contextItem('trash-2', '删除作品…',
    () => deleteWork(work), { danger: true }));
  openWriteContextMenu(x, y, items);
}

function openChapterContextMenu(x, y, chapter) {
  openWriteContextMenu(x, y, [
    contextItem('book-open', '打开本章',
      () => selectChapter(chapter.chapter_index)),
    contextItem('pencil', '重命名章节', () => {
      const row = $('writeChapterList').querySelector(
        `.write-tree-chapter[data-index="${chapter.chapter_index}"]`);
      const label = row && row.querySelector('.write-tree-label');
      if (label) startRenameChapter(chapter, label);
      else setStatus('找不到这一章', 'error');
    }),
  ]);
}

function openSceneContextMenu(x, y, chapter) {
  openWriteContextMenu(x, y, [
    contextItem('book-open', '打开所属章',
      () => selectChapter(chapter.chapter_index)),
  ]);
}

async function toggleWorkPin(work) {
  const pinned = !work.pinned;
  try {
    await api(`/api/writing/works/${encodeURIComponent(work.id)}/pin`,
      { method: 'PUT', body: JSON.stringify({ pinned }) });
    await loadWorks();
    setStatus(pinned ? `已置顶《${work.title}》`
      : `已取消置顶《${work.title}》`, 'ok');
    $('writeWorkspaceBtn').focus();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function deleteWork(work) {
  if (!window.confirm(
    `删除《${work.title}》？\n\n`
    + '章节、版本、提交记录与分支都会一起删除，且不可恢复。')) {
    return;
  }
  try {
    await api(`/api/writing/works/${encodeURIComponent(work.id)}`, {
      method: 'DELETE',
      body: JSON.stringify({ confirm_title: work.title }),
    });
    const wasCurrent = work.id === S.workId;
    await loadWorks();
    if (wasCurrent) {
      clearChapter();
      if (S.works.length) await selectWork(S.works[0].id);
      else renderWorks();
    }
    setStatus(`已删除《${work.title}》`, 'ok');
    $('writeWorkspaceBtn').focus();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

// ── 作品树 ────────────────────────────────────────────────────────
// 作品是根目录，卷是子目录，章是文件，场景是章的子文件。层级由数据
// 决定：没有卷就章直接挂在根下，没有场景就不长孙子层。

function renderChapters() {
  const box = $('writeChapterList');
  if (!box) return;
  const focused = document.activeElement;
  if (focused && focused.classList
      && focused.classList.contains('write-rename-input')
      && box.contains(focused)) {
    return;   // 正在行内改名/新建，别让自动保存把输入框重绘掉
  }
  const scrollTop = box.scrollTop;
  const focusDraftPending = !!(S.draft && S.draft.focus);
  buildTree(box);
  if (!focusDraftPending) box.scrollTop = scrollTop;
}

function buildTree(box) {
  box.textContent = '';
  if (S.draft && S.draft.kind === 'work') {
    const row = workDraftRow();
    box.appendChild(row);
    focusDraft(row);
  }
  const work = S.works.find((w) => w.id === S.workId);
  if (!work) {
    if (!(S.draft && S.draft.kind === 'work')) {
      box.appendChild(worksEmptyState());
    }
    return;
  }
  box.appendChild(rootRow(work));
  applyRenameRetry(box);
  if (S.rootCollapsed) return;

  const rows = visibleChapters();
  const children = el('div', 'write-tree-children');
  if (S.chapters.length) children.appendChild(filterRow());
  if (!rows.length && !(S.draft && S.draft.kind === 'chapter')) {
    children.appendChild(treeHint(S.chapters.length
      ? '这个筛选下没有章节。'
      : '还没有章节。点根目录右侧的 + 新建一章。'));
  }
  const volumes = Array.isArray(S.volumes) ? S.volumes : [];
  if (volumes.length) {
    const byVolume = new Map(volumes.map((v) => [v.id, []]));
    const loose = [];
    rows.forEach((c) => {
      const bucket = c.volume_id ? byVolume.get(c.volume_id) : null;
      (bucket || loose).push(c);
    });
    volumes.forEach((v) => {
      const members = byVolume.get(v.id) || [];
      if (members.length) children.appendChild(volumeNode(v, members));
    });
    loose.forEach((c) => children.appendChild(chapterNode(c, 1)));
  } else {
    rows.forEach((c) => children.appendChild(chapterNode(c, 1)));
  }
  let chapterDraft = null;
  if (S.draft && S.draft.kind === 'chapter') {
    chapterDraft = chapterDraftRow();
    children.appendChild(chapterDraft);
  }
  box.appendChild(children);
  if (chapterDraft) focusDraft(chapterDraft);
  applyRenameRetry(box);
}

function visibleChapters() {
  const keep = FILTERS[S.filter] || FILTERS.all;
  return S.chapters.filter((c) => keep(c.status || 'planned'));
}

function rootRow(work) {
  const row = el('div', 'write-tree-row write-tree-root');
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.setAttribute('aria-expanded', S.rootCollapsed ? 'false' : 'true');
  const toggle = () => {
    S.rootCollapsed = !S.rootCollapsed;
    writeFlag('bc.writeRootCollapsed', S.rootCollapsed);
    renderChapters();
  };
  row.appendChild(twistyButton(S.rootCollapsed, toggle));
  row.appendChild(treeIcon(S.rootCollapsed ? 'folder' : 'folder-open'));
  const label = el('span', 'write-tree-label', work.title || '（未命名）');
  row.appendChild(label);
  const words = S.chapters.reduce((n, c) => n + (c.word_count || 0), 0);
  const actions = el('span', 'write-tree-actions');
  actions.appendChild(treeAction('pencil', '重命名作品',
    (event) => { event.stopPropagation(); startRenameWork(work, label); }));
  actions.appendChild(treeAction('plus', '新建章节',
    (event) => { event.stopPropagation(); createChapter(); }));
  row.appendChild(actions);
  row.title = `${work.title || '（未命名）'} · ${S.chapters.length} 章`
    + (words ? ` · ${words} 字` : '')
    + (work.pinned ? ' · 已置顶' : '');
  row.onclick = toggle;
  row.oncontextmenu = (event) => {
    if (event.target.closest('input, textarea')) return;
    event.preventDefault();
    event.stopPropagation();
    row.focus();
    openWorkContextMenu(event.clientX, event.clientY, work, true);
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'ContextMenu'
        || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      row.focus();
      openWorkContextMenu(rect.left + 24, rect.bottom - 2, work, true);
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggle();
    } else if (event.key === 'ArrowRight' && S.rootCollapsed) {
      event.preventDefault();
      toggle();
    } else if (event.key === 'ArrowLeft' && !S.rootCollapsed) {
      event.preventDefault();
      toggle();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveRowFocus(row, event.key === 'ArrowDown' ? 1 : -1);
    }
  };
  return row;
}

function volumeNode(volume, members) {
  const wrapper = el('div', 'write-tree-group');
  const collapsed = S.collapsedVolumes.has(volume.id);
  const row = el('div', 'write-tree-row write-tree-volume');
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  const toggle = () => {
    if (collapsed) S.collapsedVolumes.delete(volume.id);
    else S.collapsedVolumes.add(volume.id);
    renderChapters();
  };
  row.appendChild(twistyButton(collapsed, toggle));
  row.appendChild(treeIcon(collapsed ? 'folder' : 'folder-open'));
  row.appendChild(el('span', 'write-tree-label',
    volume.title || `第${volume.volume_index}卷`));
  row.appendChild(el('span', 'write-tree-meta', `${members.length} 章`));
  row.onclick = toggle;
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggle();
    } else if (event.key === 'ArrowRight' && collapsed) {
      event.preventDefault();
      toggle();
    } else if (event.key === 'ArrowLeft' && !collapsed) {
      event.preventDefault();
      toggle();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveRowFocus(row, event.key === 'ArrowDown' ? 1 : -1);
    }
  };
  wrapper.appendChild(row);
  if (!collapsed) {
    const kids = el('div', 'write-tree-children');
    members.forEach((c) => kids.appendChild(chapterNode(c, 2)));
    wrapper.appendChild(kids);
  }
  return wrapper;
}

function chapterNode(chapter, depth) {
  const wrapper = el('div', 'write-tree-group');
  const row = el('div', 'write-tree-row write-tree-chapter');
  row.style.setProperty('--tree-indent', `${depth * 14}px`);
  row.dataset.index = String(chapter.chapter_index);
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  const active = chapter.chapter_index === S.index;
  if (active) {
    row.classList.add('active');
    row.setAttribute('aria-current', 'true');
  }
  const scenes = Array.isArray(chapter.scenes) ? chapter.scenes : [];
  const expanded = S.expandedChapters.has(chapter.id);
  if (scenes.length) {
    row.appendChild(twistyButton(!expanded, () => {
      if (expanded) S.expandedChapters.delete(chapter.id);
      else S.expandedChapters.add(chapter.id);
      renderChapters();
    }));
  } else {
    row.appendChild(treeSpacer());
  }
  row.appendChild(treeIcon('file-text'));
  const label = el('span', 'write-tree-label', chapterTreeLabel(chapter));
  row.appendChild(label);
  row.appendChild(statusDot(chapter.status));
  if (chapter.word_count) {
    row.appendChild(el('span', 'write-tree-meta', `${chapter.word_count} 字`));
  }
  const actions = el('span', 'write-tree-actions');
  actions.appendChild(treeAction('pencil', '重命名章节',
    (event) => { event.stopPropagation(); startRenameChapter(chapter, label); }));
  row.appendChild(actions);
  row.title = chapterTreeLabel(chapter)
    + (chapter.word_count ? ` · ${chapter.word_count} 字` : '');
  row.onclick = () => selectChapter(chapter.chapter_index);
  row.oncontextmenu = (event) => {
    if (event.target.closest('input, textarea')) return;
    event.preventDefault();
    event.stopPropagation();
    row.focus();
    openChapterContextMenu(event.clientX, event.clientY, chapter);
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'ContextMenu'
        || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      row.focus();
      openChapterContextMenu(rect.left + 24, rect.bottom - 2, chapter);
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      selectChapter(chapter.chapter_index);
    } else if (event.key === 'ArrowRight' && scenes.length && !expanded) {
      event.preventDefault();
      S.expandedChapters.add(chapter.id);
      renderChapters();
    } else if (event.key === 'ArrowLeft' && scenes.length && expanded) {
      event.preventDefault();
      S.expandedChapters.delete(chapter.id);
      renderChapters();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveRowFocus(row, event.key === 'ArrowDown' ? 1 : -1);
    }
  };
  wrapper.appendChild(row);
  if (scenes.length && expanded) {
    const kids = el('div', 'write-tree-children');
    scenes.forEach((s) => kids.appendChild(sceneNode(s, depth + 1, chapter)));
    wrapper.appendChild(kids);
  }
  return wrapper;
}

function sceneNode(scene, depth, chapter) {
  const row = el('div', 'write-tree-row write-tree-scene');
  row.style.setProperty('--tree-indent', `${depth * 14}px`);
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.appendChild(treeSpacer());
  row.appendChild(treeIcon('text'));
  row.appendChild(el('span', 'write-tree-label',
    scene.title || `场景${scene.scene_index}`));
  row.appendChild(statusDot(scene.status));
  row.title = `${chapterTreeLabel(chapter)} · `
    + (scene.title || `场景${scene.scene_index}`);
  row.onclick = () => selectChapter(chapter.chapter_index);
  row.oncontextmenu = (event) => {
    if (event.target.closest('input, textarea')) return;
    event.preventDefault();
    event.stopPropagation();
    row.focus();
    openSceneContextMenu(event.clientX, event.clientY, chapter);
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'ContextMenu'
        || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      row.focus();
      openSceneContextMenu(rect.left + 24, rect.bottom - 2, chapter);
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      selectChapter(chapter.chapter_index);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveRowFocus(row, event.key === 'ArrowDown' ? 1 : -1);
    }
  };
  return row;
}

function filterRow() {
  const row = el('div', 'write-tree-filters');
  FILTER_LABELS.forEach(([key, label]) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'write-filter' + (S.filter === key ? ' active' : '');
    btn.textContent = label;
    btn.onclick = () => {
      S.filter = key;
      renderChapters();
    };
    row.appendChild(btn);
  });
  return row;
}

function worksEmptyState() {
  const box = el('div', 'write-tree-empty');
  const title = document.createElement('strong');
  title.textContent = '还没有作品';
  box.appendChild(title);
  box.appendChild(el('span', '', '新建一本，然后建第一章。'));
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'write-btn primary';
  btn.textContent = '新建作品';
  btn.onclick = createWork;
  box.appendChild(btn);
  return box;
}

function treeHint(text) {
  return el('p', 'write-tree-hint', text);
}

function workDraftRow() {
  const row = el('div', 'write-tree-row write-tree-draft');
  row.appendChild(treeSpacer());
  row.appendChild(treeIcon('folder-plus'));
  row.appendChild(draftInput('作品名', (value) => commitDraftWork(value)));
  return row;
}

function chapterDraftRow() {
  const row = el('div', 'write-tree-row write-tree-draft write-tree-chapter');
  row.style.setProperty('--tree-indent', '14px');
  row.appendChild(treeSpacer());
  row.appendChild(treeIcon('file-text'));
  const next = S.chapters.length
    ? S.chapters[S.chapters.length - 1].chapter_index + 1 : 1;
  row.appendChild(draftInput(`第${next}章标题`,
    (value) => commitDraftChapter(value)));
  return row;
}

function applyRenameRetry(box) {
  const retry = S.renameRetry;
  if (!retry) return;
  if (retry.kind === 'work') {
    const work = S.works.find((w) => w.id === retry.id);
    const row = box.querySelector('.write-tree-root');
    const label = row && row.querySelector('.write-tree-label');
    if (!work || !label) return;
    S.renameRetry = null;
    startRenameWork(work, label, retry.value);
  } else if (retry.kind === 'chapter') {
    const chapter = S.chapters.find((c) => c.chapter_index === retry.index);
    const row = box.querySelector(
      `.write-tree-chapter[data-index="${retry.index}"]`);
    const label = row && row.querySelector('.write-tree-label');
    if (!chapter || !label) return;
    S.renameRetry = null;
    startRenameChapter(chapter, label, retry.value);
  }
}

function chapterTreeLabel(chapter) {
  const title = (chapter.title || '').trim();
  if (!title) return `第${chapter.chapter_index}章 · 未命名`;
  return /^第\s*[0-9一二三四五六七八九十百千零两]+\s*[章回节]/.test(title)
    ? title : `第${chapter.chapter_index}章 · ${title}`;
}

function statusDot(status) {
  const dot = el('span', 'write-tree-dot');
  dot.dataset.status = status || 'planned';
  dot.title = STATUS_LABELS[status] || STATUS_LABELS.planned;
  return dot;
}

function twistyButton(collapsed, onClick) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'write-tree-twisty';
  btn.title = collapsed ? '展开' : '收起';
  btn.setAttribute('aria-label', btn.title);
  btn.innerHTML = `<i data-lucide="chevron-${collapsed ? 'right' : 'down'}"></i>`;
  btn.onclick = (event) => {
    event.stopPropagation();
    onClick();
  };
  return btn;
}

function treeSpacer() {
  const span = el('span', 'write-tree-twisty is-spacer');
  span.setAttribute('aria-hidden', 'true');
  return span;
}

function treeIcon(name, className = 'write-tree-icon') {
  const span = el('span', className);
  span.innerHTML = `<i data-lucide="${name}"></i>`;
  return span;
}

function treeAction(name, title, onClick) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'write-tree-action';
  btn.title = title;
  btn.setAttribute('aria-label', title);
  btn.innerHTML = `<i data-lucide="${name}"></i>`;
  btn.onclick = onClick;
  return btn;
}

function moveRowFocus(row, delta) {
  const rows = [...$('writeChapterList')
    .querySelectorAll('.write-tree-row[tabindex="0"]')];
  const next = rows[rows.indexOf(row) + delta];
  if (next) next.focus();
}

// ── Inline rename ─────────────────────────────────────────────────
// 对齐会话导航栏的交互：hover 露出铅笔，点开变成行内输入框，
// Enter/blur 提交，Esc 取消。列表项因此从 <button> 换成 div[role=button]
// —— 输入框不能嵌在 button 里。

function inlineRenameInput(oldValue, done) {
  const input = document.createElement('input');
  input.className = 'write-rename-input';
  input.value = oldValue;
  input.maxLength = 120;
  let cancelled = false;
  let settled = false;
  input.addEventListener('click', (event) => event.stopPropagation());
  input.addEventListener('keydown', (event) => {
    event.stopPropagation();
    if (event.key === 'Enter') { event.preventDefault(); input.blur(); }
    else if (event.key === 'Escape') { cancelled = true; input.blur(); }
  });
  input.addEventListener('blur', () => {
    if (settled) return;
    settled = true;
    done(cancelled ? null : input.value.trim());
  });
  return input;
}

// 新建草稿：和重命名同一个输入框，但值先留在 S.draft 里，
// 不落库；列表重绘时也能把用户敲到一半的名字带回来。
function draftInput(placeholder, done) {
  const draft = S.draft;
  const input = document.createElement('input');
  input.className = 'write-rename-input';
  input.placeholder = placeholder;
  input.maxLength = 120;
  input.value = draft.value || '';
  let cancelled = false;
  let settled = false;
  input.addEventListener('click', (event) => event.stopPropagation());
  input.addEventListener('input', () => {
    if (S.draft === draft) draft.value = input.value;
  });
  input.addEventListener('keydown', (event) => {
    event.stopPropagation();
    if (event.key === 'Enter') {
      event.preventDefault();
      input.blur();
    } else if (event.key === 'Escape') {
      cancelled = true;
      input.blur();
    }
  });
  input.addEventListener('blur', () => {
    if (settled) return;
    settled = true;
    done(cancelled ? null : input.value);
  });
  return input;
}

function focusDraft(row) {
  if (!S.draft || !S.draft.focus) return;
  const input = row.querySelector('.write-rename-input');
  if (!input) return;
  S.draft.focus = false;
  row.scrollIntoView({ block: 'nearest' });
  input.focus();
  input.select();
}

function startRenameWork(work, label, initial) {
  if (!label || label.querySelector('.write-rename-input')) return;
  const input = inlineRenameInput(
    initial !== undefined ? initial : (work.title || ''),
    (value) => finishRenameWork(work, value));
  label.replaceWith(input);
  input.focus();
  input.select();
}

function finishRenameWork(work, value) {
  const old = work.title || '';
  if (value === null || !value || value === old) {
    renderWorks();
    return;
  }
  work.title = value;
  if (work.id === S.workId) S.workTitle = value;
  renderWorks();
  api(`/api/writing/works/${encodeURIComponent(work.id)}`,
    { method: 'PUT', body: JSON.stringify({ title: value }) })
    .then(() => setStatus('作品名已更新', 'ok'))
    .catch((err) => {
      work.title = old;
      if (work.id === S.workId) S.workTitle = old;
      // 没保存成功就不算退出重命名模式：把输入框连同刚敲的名字放回去。
      S.renameRetry = { kind: 'work', id: work.id, value };
      renderWorks();
      setStatus(err.message, 'error');
    });
}

function startRenameChapter(chapter, label, initial) {
  if (!label || label.querySelector('.write-rename-input')) return;
  const input = inlineRenameInput(
    initial !== undefined ? initial : (chapter.title || ''),
    (value) => finishRenameChapter(chapter, value));
  label.replaceWith(input);
  input.focus();
  input.select();
}

function finishRenameChapter(chapter, value) {
  const old = chapter.title || '';
  if (value === null || value === old) {
    renderChapters();
    return;
  }
  chapter.title = value;
  if (chapter.chapter_index === S.index) {
    if (S.chapter) S.chapter.title = value;
    $('writeChapterTitle').value = value;
  }
  renderChapters();
  const path = `/api/writing/works/${encodeURIComponent(S.workId)}`
    + `/chapters/${chapter.chapter_index}/title`;
  api(path, { method: 'PUT', body: JSON.stringify({ title: value }) })
    .then(() => setStatus('章标题已更新', 'ok'))
    .catch((err) => {
      chapter.title = old;
      if (chapter.chapter_index === S.index) {
        if (S.chapter) S.chapter.title = old;
        $('writeChapterTitle').value = old;
      }
      S.renameRetry = {
        kind: 'chapter', index: chapter.chapter_index, value,
      };
      renderChapters();
      setStatus(err.message, 'error');
    });
}

function clearChapter() {
  S.index = 0;
  S.chapter = null;
  S.contract = null;
  S.savePoint = null;
  $('writeEditor').value = '';
  $('writeChapterTitle').value = '';
  resetEditorHistory();
  updateEditorState();
}

async function selectWork(workId) {
  // 换作品前先把当前这件作品的未保存正文落盘——flush 用的是旧 workId。
  if (!(await flush())) {
    setStatus('当前作品还有未保存的正文，保存失败，已取消切换', 'error');
    return;
  }
  // 清掉上一本的编辑器现场：新作品还没加载完就按 Ctrl+S 也写不到旧章上。
  clearChapter();
  S.workId = workId;
  const work = S.works.find((x) => x.id === workId);
  S.workTitle = work ? work.title : '';
  S.chapters = [];
  S.volumes = [];
  S.expandedChapters.clear();
  S.collapsedVolumes.clear();
  S.filter = 'all';   // 换一本就从「全部」看起，别把上一本的筛选带过来
  renderWorks();
  try {
    const data = await api(`/api/writing/works/${encodeURIComponent(workId)}`);
    S.chapters = data.chapters || [];
    S.volumes = data.volumes || [];
    renderChapters();
    const last = S.chapters[S.chapters.length - 1];
    if (last) await selectChapter(last.chapter_index);
    else clearChapter();
    if (S.view === 'map') loadMap();
    if (S.view === 'curve') loadCurve();
    await loadVcs();
    await loadStyleProfile();
    if (S.stylePending) {
      const pending = S.stylePending;
      await setStyleAuthor(pending, styleName(pending));
    }
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

// ── 参考风格（作者声音透镜）──────────────────────────────────────────

const STYLE_CODE_LABEL = {
  summary_sentence_density: '总结句',
  repeated_openings: '重复开头',
  sentence_rhythm: '句长',
  dialogue_density: '对话比例',
  paragraph_shape: '段落形态',
  emotional_expression: '情绪表达',
};

function styleName(authorId) {
  const hit = S.styleAuthors.find((a) => a.author_id === authorId);
  if (hit) return hit.canonical_name;
  return authorId && authorId === S.styleAuthorId ? S.styleAuthorName : '';
}

async function loadStyleAuthors() {
  // 用「取过一次」标记，不用列表长度：档案页可能先塞了一位作者进来，
  // 那时列表非空但还没从库里取过，按长度判断会永远不再刷新。
  if (S.styleAuthorsLoaded) return;
  try {
    const data = await api('/api/authors?limit=100');
    S.styleAuthors = (data.items || []).filter(
      (a) => a.voice_ready || a.voice_traits > 0 || a.voice_samples > 0);
    S.styleAuthorsLoaded = true;
  } catch (_) {
    S.styleAuthors = [];
  }
}

function renderStyleBar() {
  const bar = $('writeStyleBar');
  const select = $('writeStyleSelect');
  if (!bar || !select) return;
  if (!S.workId || (!S.styleAuthors.length && !S.styleAuthorId)) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  select.textContent = '';
  const off = document.createElement('option');
  off.value = '';
  off.textContent = '不启用';
  select.appendChild(off);
  const ids = new Set(S.styleAuthors.map((a) => a.author_id));
  S.styleAuthors.forEach((a) => {
    const option = document.createElement('option');
    option.value = a.author_id;
    // 只有明确知道「没就绪 / 没有 trait」才标样本不足；档案页临时塞进来的
    // 条目没有这两个字段，不能凭缺失就唱衰。
    const weak = a.voice_ready === false || a.voice_traits === 0;
    option.textContent = a.canonical_name + (weak ? ' · 样本不足' : '');
    select.appendChild(option);
  });
  if (S.styleAuthorId && !ids.has(S.styleAuthorId)) {
    const option = document.createElement('option');
    option.value = S.styleAuthorId;
    option.textContent = styleName(S.styleAuthorId) || S.styleAuthorId;
    select.appendChild(option);
  }
  select.value = S.styleAuthorId || '';

  const note = $('writeStyleNote');
  const info = S.styleInfo || {};
  if (info.code) {
    const msg = info.code === 'no_voice_samples'
      ? '这位作者还没有正文声音样本'
      : info.code === 'author_db_unavailable'
        ? '作者库不可用，本次不注入'
        : info.code === 'no_transferable_traits'
          ? '没有可迁移的文风特征'
          : info.code;
    note.textContent = msg;
    note.dataset.state = 'warn';
  } else if (S.styleAuthorId) {
    const bits = [`已启用`];
    if (info.rules != null) bits.push(`规则 ${info.rules}`);
    if (info.samples != null) bits.push(`例证 ${info.samples}`);
    if (info.partial) bits.push('样本不足，仅供轻参考');
    note.textContent = bits.join(' · ');
    note.dataset.state = info.partial ? 'warn' : '';
  } else {
    note.textContent = '';
    note.dataset.state = '';
  }
  const check = $('writeStyleCheckBtn');
  if (check) check.hidden = !S.styleAuthorId;
}

async function loadStyleProfile() {
  if (!S.workId) return;
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/style`);
    const ref = (data.author_refs || [])[0];
    S.styleAuthorId = ref ? ref.author_id : '';
    S.styleInfo = null;
    await loadStyleAuthors();
    S.styleAuthorName = styleName(S.styleAuthorId);
    renderStyleBar();
  } catch (_) {
    S.styleAuthorId = '';
    S.styleAuthorName = '';
    renderStyleBar();
  }
}

async function setStyleAuthor(authorId, authorName = '') {
  const prevId = S.styleAuthorId;
  const prevInfo = S.styleInfo;
  const prevName = S.styleAuthorName;
  S.styleAuthorId = authorId || '';
  S.styleAuthorName = authorName || styleName(authorId);
  S.styleInfo = null;
  if (!S.workId) {
    S.stylePending = authorId || '';
    renderStyleBar();
    setStatus('已记住参考作者；选一个作品后生效', 'ok');
    return;
  }
  try {
    await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/style`,
      { method: 'PUT', body: JSON.stringify({ author_id: authorId || '' }) });
    S.stylePending = null;
    renderStyleBar();
    setStatus(authorId ? '参考风格已启用' : '参考风格已关闭', 'ok');
  } catch (err) {
    // 落库失败就回到原选择，别让界面显示「已启用」而后端仍是旧值。
    S.styleAuthorId = prevId;
    S.styleAuthorName = prevName;
    S.styleInfo = prevInfo;
    renderStyleBar();
    setStatus(err.message, 'error');
  }
}

async function runStyleCheck() {
  if (!S.chapter) { setStatus('先选一章再对照', 'error'); return; }
  setStatus('正在对照参考风格…');
  try {
    const q = S.styleAuthorId
      ? `?style_author_id=${encodeURIComponent(S.styleAuthorId)}` : '';
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}`
      + `/chapters/${S.index}/style-check${q}`);
    renderStyleCheck(data);
    showPane('review');
    const n = (data.findings || []).length;
    setStatus(n ? `风格对照：${n} 条提示（不阻断）` : '风格对照：没有明显偏离',
      'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function renderStyleCheck(data) {
  const box = $('writeStyleFindings');
  if (!box) return;
  box.textContent = '';
  box.hidden = false;
  const m = data.metrics || {};
  const head = el('div', 'write-style-check-head');
  if (data.note) {
    head.textContent = data.note;
  } else {
    head.textContent = `参考风格：${data.author_name || '已选作者'}`
      + ` · 平均句长 ${m.avg_sentence_len ?? '—'} 字`
      + ` · 对话 ${Math.round((m.dialogue_ratio || 0) * 100)}%`
      + ` · 总结句 ${Math.round((m.summary_sentence_ratio || 0) * 100)}%`;
  }
  box.appendChild(head);
  (data.findings || []).forEach((finding) => {
    const card = el('div', 'write-finding');
    const line = el('div', 'write-finding-head');
    const sev = el('span', 'write-sev', finding.severity);
    sev.dataset.sev = finding.severity;
    line.append(sev, el('span', 'write-finding-cat',
      STYLE_CODE_LABEL[finding.code] || finding.code));
    card.appendChild(line);
    card.appendChild(el('div', '', finding.message || ''));
    if ((finding.evidence || []).length) {
      card.appendChild(el('div', 'write-finding-evidence',
        finding.evidence.join('、')));
    }
    box.appendChild(card);
  });
}

async function selectChapter(index) {
  try {
    // 切章前先落盘，避免这份草稿被下一章覆盖。已经换了作品时不再
    // 用旧编辑器内容往新作品写（selectWork 已经 flush 过一次）。
    if (S.chapter && S.chapter.work_id === S.workId
        && S.index !== index) {
      if (!(await flush())) {
        setStatus('保存失败，已取消切章', 'error');
        return;
      }
    }
    const box = $('writeChapterList');
    const fromTree = !!(box && box.contains(document.activeElement));
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/chapters/${index}`);
    S.index = index;
    S.chapter = data.chapter;
    S.contract = data.contract;
    S.savePoint = data.save_point || null;
    // 风格对照是针对上一章正文算的，换章必须收起，避免拿旧结论看新章。
    const styleFindings = $('writeStyleFindings');
    if (styleFindings) {
      styleFindings.hidden = true;
      styleFindings.textContent = '';
    }
    $('writeEditor').value = data.chapter.content || '';
    $('writeChapterTitle').value = data.chapter.title || '';
    resetEditorHistory();
    renderChapters();
    const active = box && box.querySelector(
      `.write-tree-chapter[data-index="${index}"]`);
    if (active) {
      active.scrollIntoView({ block: 'nearest' });
      if (fromTree) active.focus();
    }
    updateEditorState();
    setStatus('');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function showPane(name) {
  document.querySelectorAll('.write-side-tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.pane === name);
  });
  $('writeReviewPane').hidden = name !== 'review';
  $('writeVcsPane').hidden = name !== 'vcs';
  $('writeSide').classList.add('open');
  if (name === 'review') loadReview();
  if (name === 'vcs') loadVcs();
}

function setView(name) {
  S.view = name;
  document.querySelectorAll('.write-view-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === name);
  });
  $('writeEditorView').hidden = name !== 'editor';
  $('writeMapView').hidden = name !== 'map';
  $('writeCurveView').hidden = name !== 'curve';
  $('writeDiffView').hidden = true;
  $('writeMergeView').hidden = true;
  if (name === 'map') loadMap();
  if (name === 'curve') loadCurve();
}

// ── 写作 git：版本面板 ─────────────────────────────────────────────

async function loadVcs() {
  if (!S.workId) return;
  try {
    const base = `/api/writing/works/${encodeURIComponent(S.workId)}/vcs`;
    // 图谱要看得出岔路和合并，默认就把所有分支的提交取回来。
    const params = new URLSearchParams({ limit: '120' });
    if (S.logAllBranches) params.set('all', '1');
    if (S.logOnlyChapter && S.chapter) {
      // mark 而不是过滤：泳道要完整的父子链，删行会把线画断。
      params.set('chapter_id', S.chapter.id);
      params.set('mark', '1');
    }
    const [status, log, stash, feedback] = await Promise.all([
      api(`${base}/status`),
      api(`${base}/log?${params.toString()}`),
      api(`${base}/stash`),
      api('/api/writing/feedback?status=open&limit=20'),
    ]);
    S.vcs = status;
    S.vcsLog = log.commits || [];
    S.vcsStashes = stash.stashes || [];
    S.vcsFeedback = feedback.items || [];
    S.vcsStale = false;
    renderVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function chapterLabel(entry) {
  const n = entry.chapter_index != null ? entry.chapter_index : '?';
  return `第${n}章 ${entry.title || '未命名'}`;
}

function fieldText(fields) {
  const labels = { content: '正文', content_hash: '正文', title: '标题',
                   status: '状态', volume_id: '卷', chapter_index: '章序',
                   word_count: '字数' };
  return Array.from(new Set((fields || []).map((f) => labels[f] || f)))
    .join(' / ');
}

function renderVcs() {
  const vcs = S.vcs;
  if (!vcs) return;
  $('writeVcsBranchLabel').textContent = vcs.branch || 'main';
  updateEditorState();

  const select = $('writeVcsBranchSelect');
  select.textContent = '';
  (vcs.branches || []).forEach((b) => {
    const opt = document.createElement('option');
    opt.value = b.name;
    opt.textContent = b.name;
    select.appendChild(opt);
  });
  const currentBranch = vcs.branch || 'main';
  if (!Array.from(select.options).some((o) => o.value === currentBranch)) {
    const opt = document.createElement('option');
    opt.value = currentBranch;
    opt.textContent = currentBranch;
    select.appendChild(opt);
  }
  select.value = currentBranch;
  $('writeVcsCommitBtn').disabled = !!vcs.clean;

  const changes = vcs.changes || { added: [], removed: [], changed: [] };
  const rows = [];
  (changes.changed || []).forEach((c) => {
    rows.push(`<div class="write-vcs-item" data-diff-chapter="${c.chapter_id}">`
      + `<div>${escapeHtml(chapterLabel(c))}`
      + `<button class="write-vcs-inline" data-discard-chapter="${escapeHtmlAttr(c.chapter_id)}"`
      + ` title="只丢弃这一章的未提交改动">丢弃</button></div>`
      + `<div class="vcs-meta">已修改 · ${escapeHtml(fieldText(c.fields))}</div></div>`);
  });
  (changes.added || []).forEach((c) => {
    rows.push(`<div class="write-vcs-item" data-diff-chapter="${c.chapter_id}">`
      + `<div>${escapeHtml(chapterLabel(c))}</div>`
      + `<div class="vcs-meta">新增</div></div>`);
  });
  (changes.removed || []).forEach((c) => {
    rows.push(`<div class="write-vcs-item" data-diff-chapter="${c.chapter_id}">`
      + `<div>${escapeHtml(chapterLabel(c))}</div>`
      + `<div class="vcs-meta">删除</div></div>`);
  });
  if (vcs.ledger_dirty) {
    rows.push('<div class="write-vcs-item" data-diff-ledger="1">'
      + '<div>账本</div><div class="vcs-meta">合同 / 事实 / 承诺有未提交改动</div></div>');
  }
  $('writeVcsChanges').innerHTML = rows.length
    ? rows.join('') : '<div class="write-vcs-empty">工作区干净</div>';
  const bits = [];
  const n = (changes.added || []).length + (changes.removed || []).length
    + (changes.changed || []).length;
  if (n) bits.push(`${n} 章`);
  if (vcs.ledger_dirty) bits.push('账本');
  $('writeVcsCount').textContent = bits.join(' · ');
  $('writeVcsAmendBtn').disabled = !vcs.head;
  $('writeVcsStashBtn').disabled = !!vcs.clean;
  $('writeVcsTagBtn').disabled = !vcs.head;
  renderVcsBranches(vcs);
  renderVcsTags(vcs);
  renderVcsStashes();
  renderVcsFeedback();
  renderVcsLog();
}

function renderVcsFeedback() {
  const box = $('writeVcsFeedback');
  const rows = S.vcsFeedback || [];
  $('writeVcsFeedbackCount').textContent = rows.length ? `(${rows.length})` : '';
  if (!rows.length) {
    box.innerHTML = '<div class="write-vcs-empty">没有待处理反馈</div>';
    return;
  }
  const kinds = { issue: '问题', suggestion: '建议', question: '疑问' };
  const sev = { high: '高', normal: '中', low: '低' };
  box.innerHTML = rows.map((f) => (
    `<details class="write-vcs-item write-vcs-feedback">`
    + `<summary>${escapeHtml(kinds[f.kind] || f.kind)} · ${escapeHtml(f.title)}`
    + `<span class="vcs-meta"> ${escapeHtml(sev[f.severity] || '')} · `
    + `${escapeHtml(f.area || '')} · ${escapeHtml(whenText(f.time_created))}</span></summary>`
    + `<div class="vcs-meta">${escapeHtml(f.body || '')}</div>`
    + `<button class="write-vcs-inline" data-resolve-feedback="${escapeHtmlAttr(f.id)}">`
    + `标记已处理</button></details>`
  )).join('');
}

function renderVcsBranches(vcs) {
  const box = $('writeVcsBranches');
  const branches = vcs.branches || [];
  $('writeVcsBranchCount').textContent = branches.length
    ? `(${branches.length})` : '';
  if (!branches.length) {
    box.innerHTML = '<div class="write-vcs-empty">还没有分支</div>';
    return;
  }
  box.innerHTML = branches.map((b) => {
    const bits = [];
    if (b.ahead) bits.push(`领先 ${b.ahead}`);
    if (b.behind) bits.push(`落后 ${b.behind}`);
    if (b.base_short) bits.push(`分叉自 ${b.base_short}`);
    if (!bits.length) bits.push(b.is_head ? '当前所在' : '与本分支一致');
    const name = escapeHtmlAttr(b.name);
    const actions = b.is_head ? (
      `<span class="write-vcs-actions">`
      + `<button data-rename-branch="${name}">改名</button></span>`)
      : (
        `<span class="write-vcs-actions">`
        + `<button data-diff-branch="${name}">对比</button>`
        + `<button data-rename-branch="${name}">改名</button>`
        + `<button data-merge-branch="${name}">合并</button>`
        + `<button data-delete-branch="${name}">删除</button></span>`);
    return `<div class="write-vcs-item${b.is_head ? ' current' : ''}">`
      + `<div>⎇ ${escapeHtml(b.name)}${actions}</div>`
      + `<div class="vcs-meta">${escapeHtml(bits.join(' · '))}`
      + ` · ${escapeHtml(b.message || '')}</div></div>`;
  }).join('');
}

function renderVcsTags(vcs) {
  const box = $('writeVcsTags');
  const tags = vcs.tags || [];
  if (!tags.length) {
    box.innerHTML = '<div class="write-vcs-empty">还没有标签</div>';
    return;
  }
  box.innerHTML = tags.map((t) => (
    `<div class="write-vcs-item"><div>⌂ ${escapeHtml(t.name)}`
    + `<span class="write-vcs-actions">`
    + `<button data-delete-tag="${escapeHtmlAttr(t.name)}">删除</button></span></div>`
    + `<div class="vcs-meta">${escapeHtml(String(t.commit_id || '').slice(-8))}`
    + ` · ${escapeHtml(t.message || '')}</div></div>`
  )).join('');
}

function renderVcsStashes() {
  const box = $('writeVcsStashes');
  const rows = S.vcsStashes || [];
  $('writeVcsStashCount').textContent = rows.length ? `(${rows.length})` : '';
  if (!rows.length) {
    box.innerHTML = '<div class="write-vcs-empty">没有暂存</div>';
    return;
  }
  box.innerHTML = rows.map((s) => (
    `<div class="write-vcs-item"><div>${escapeHtml(s.label)} ${escapeHtml(s.message || '')}`
    + `<span class="write-vcs-actions">`
    + `<button data-pop-stash="${escapeHtmlAttr(s.name)}">恢复</button>`
    + `<button data-drop-stash="${escapeHtmlAttr(s.name)}">丢弃</button></span></div>`
    + `<div class="vcs-meta">${escapeHtml(whenText(s.time_updated))}</div></div>`
  )).join('');
}

function whenText(ms) {
  if (!ms) return '';
  return new Date(ms).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit' });
}

// ── 版本图：把提交摊到泳道上 ─────────────────────────────────────
//
// 照 git log --graph 的画法：按时间倒序一行一条提交，每列记着「这一列接
// 下来在等哪条提交」。一条提交到来时，凡是在等它的列都要接到这个节点——
// 最左那列留下来继续往下走，其余列画一段圆弧汇进来；随后第一父节点接管
// 本列，第二父节点（合并）另起一列。只有第一父节点不在了才断线。
//
// GRAPH_ROW_H 必须与 CSS 的 --graph-row-h 相等，否则上下两行的竖线接不上。
const GRAPH_LANE_W = 14;
const GRAPH_ROW_H = 44;
const GRAPH_PAD = 7;
const GRAPH_BEND = 8;          // 汇入/分出那一段圆弧的高度
const GRAPH_COLORS = ['--graph-1', '--graph-2', '--graph-3',
                      '--graph-4', '--graph-5'];

const graphColor = (col) => `var(${GRAPH_COLORS[col % GRAPH_COLORS.length]})`;

function layoutCommitGraph(commits) {
  const lanes = [];            // 列 → 这一列正在等它出现的提交 id
  const rows = [];
  let laneCount = 1;

  // avoid：本行的列已经许给第一父节点了，别让第二父节点抢回去。
  const claim = (avoid) => {
    for (let i = 0; i < lanes.length; i += 1) {
      if (!lanes[i] && i !== avoid) return i;
    }
    lanes.push(null);
    return lanes.length - 1;
  };

  commits.forEach((commit) => {
    const before = lanes.slice();
    const waiting = [];
    before.forEach((id, i) => { if (id === commit.id) waiting.push(i); });
    const fresh = waiting.length === 0;
    const col = fresh ? claim() : waiting[0];
    // 新开的列从本行才开始，上面没有线可接，所以不算汇入。
    const joins = fresh ? [] : waiting.slice(1);

    waiting.forEach((j) => { lanes[j] = null; });
    lanes[col] = null;

    // 第二父节点先定位：已经有列在等它就并过去，否则新开一列。
    const second = String(commit.parent2_id || '');
    let mergeCol = -1;
    let mergeActive = false;
    if (second) {
      const stacked = lanes.indexOf(second);
      if (stacked >= 0) { mergeCol = stacked; mergeActive = true; }
      else mergeCol = claim(col);
    }

    const parent = String(commit.parent_id || '');
    if (parent) lanes[col] = parent;
    if (mergeCol >= 0 && !mergeActive) lanes[mergeCol] = second;

    // 本行穿过而不落点的列：整行一条竖线。必须比 before 和 after 的值，
    // 不能只判非空——合并会把这一列腾出来又立刻让给新泳道，那是另一根线。
    const through = [];
    lanes.forEach((id, j) => {
      if (id && before[j] === id && j !== col) through.push(j);
    });

    laneCount = Math.max(laneCount,
                         Math.max(col, mergeCol, ...joins, ...through) + 1);
    rows.push({ commit, col, joins, through, mergeCol, mergeActive,
                stub: !fresh, down: !!parent });
  });
  return { rows, laneCount };
}

function graphRefs(commit, col) {
  const refs = (commit.refs || []).slice().sort(
    (a, b) => (a.kind === 'branch' ? 0 : 1) - (b.kind === 'branch' ? 0 : 1));
  return refs.map((r) => {
    const head = r.kind === 'branch' && S.vcs && r.name === S.vcs.branch;
    const cls = `write-graph-ref ${r.kind === 'tag' ? 'tag' : 'branch'}`
      + (head ? ' head' : '');
    const dot = r.kind === 'tag' ? ''
      : `<i class="write-graph-dot" style="background:${graphColor(col)}"></i>`;
    return `<span class="${cls}">${dot}${escapeHtml(r.name)}</span>`;
  }).join('');
}

function graphRowHtml(row, gutter) {
  const c = row.commit;
  const cy = GRAPH_ROW_H / 2;
  const x = (col) => GRAPH_PAD + col * GRAPH_LANE_W;
  const parts = [];
  row.through.forEach((j) => {
    parts.push(`<path d="M${x(j)} 0V${GRAPH_ROW_H}"`
      + ` style="stroke:${graphColor(j)}"/>`);
  });
  // 本列上方留下的线头（新开的列没有，别凭空画一截）。
  if (row.stub) {
    parts.push(`<path d="M${x(row.col)} 0V${cy}"`
      + ` style="stroke:${graphColor(row.col)}"/>`);
  }
  row.joins.forEach((j) => {
    parts.push(`<path d="M${x(j)} 0V${cy - GRAPH_BEND}`
      + `Q${x(j)} ${cy} ${x(row.col)} ${cy}"`
      + ` style="stroke:${graphColor(j)}"/>`);
  });
  if (row.down) {
    parts.push(`<path d="M${x(row.col)} ${cy}V${GRAPH_ROW_H}"`
      + ` style="stroke:${graphColor(row.col)}"/>`);
  }
  if (row.mergeCol >= 0) {
    const q = row.mergeCol;
    // 那条泳道如果本行还活着，圆弧接到线上就停，别再描一遍竖线。
    const tail = row.mergeActive ? '' : `V${GRAPH_ROW_H}`;
    parts.push(`<path d="M${x(row.col)} ${cy}Q${x(q)} ${cy}`
      + ` ${x(q)} ${cy + GRAPH_BEND}${tail}"`
      + ` style="stroke:${graphColor(q)}"/>`);
  }
  const color = graphColor(row.col);
  const radius = row.mergeCol >= 0 ? 4.6 : 3.6;
  if (c.is_head) {
    parts.push(`<circle cx="${x(row.col)}" cy="${cy}" r="${radius + 3}"`
      + ` fill="none" stroke-opacity=".45" stroke-width="1.5"`
      + ` style="stroke:${color}"/>`);
  }
  parts.push(`<circle cx="${x(row.col)}" cy="${cy}" r="${radius}"`
    + ` style="fill:${color}"/>`);

  const text = c.message || '（无说明）';
  const dim = c.touches === false ? ' dim' : '';
  return `<div class="write-graph-row${dim}" data-commit="${escapeHtmlAttr(c.id)}"`
    + ` title="${escapeHtmlAttr(`${text}\n${c.short} · ${whenText(c.time_created)}`)}">`
    + `<svg class="write-graph-gutter" width="${gutter}" height="${GRAPH_ROW_H}"`
    + ` viewBox="0 0 ${gutter} ${GRAPH_ROW_H}" aria-hidden="true">`
    + `${parts.join('')}</svg>`
    + `<div class="write-graph-body">`
    + `<div class="write-graph-msg">${escapeHtml(text)}</div>`
    + `<div class="write-graph-meta">${graphRefs(c, row.col)}`
    + `<span class="write-graph-hash">${escapeHtml(c.short || '')}</span>`
    + `<span class="write-graph-when">${escapeHtml(whenText(c.time_created))}</span>`
    + `</div></div></div>`;
}

function renderVcsLog() {
  const box = $('writeVcsLog');
  if (!S.vcsLog.length) {
    box.innerHTML = '<div class="write-vcs-empty">还没有提交——保存一次改动，'
      + '这里就会长出第一条线</div>';
    return;
  }
  const { rows, laneCount } = layoutCommitGraph(S.vcsLog);
  const gutter = GRAPH_PAD * 2 + Math.max(laneCount - 1, 0) * GRAPH_LANE_W;
  box.innerHTML = rows.map((row) => graphRowHtml(row, gutter)).join('');
}

async function openVcsDiff(fromRef, toRef, chapterId) {
  if (!S.workId) return;
  const params = new URLSearchParams({
    from: fromRef || 'EMPTY', to: toRef || 'WORK',
    text: chapterId ? '1' : '0',
  });
  if (chapterId) params.set('chapter_id', chapterId);
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/vcs/diff?${params}`);
    S.diffFrom = fromRef || 'EMPTY';
    S.diffTo = toRef || 'WORK';
    S.diffCommitId = (toRef && toRef !== 'WORK') ? toRef : '';
    renderDiff(data);
    showDiffView();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function showDiffView() {
  $('writeEditorView').hidden = true;
  $('writeMapView').hidden = true;
  $('writeCurveView').hidden = true;
  $('writeMergeView').hidden = true;
  $('writeDiffView').hidden = false;
  document.querySelectorAll('.write-view-tab').forEach((t) => {
    t.classList.remove('active');
  });
}

function renderDiff(data) {
  const from = data.from || {};
  const to = data.to || {};
  $('writeDiffTitle').textContent = `${from.short || 'EMPTY'} → ${to.short || 'WORK'}`;
  $('writeDiffRestore').hidden = !S.diffCommitId;
  $('writeDiffRevert').hidden = !S.diffCommitId;
  $('writeDiffPick').hidden = !S.diffCommitId;
  const parts = [];
  [[data.changes && data.changes.changed, '修改'],
   [data.changes && data.changes.added, '新增'],
   [data.changes && data.changes.removed, '删除']].forEach(([rows, label]) => {
    if (!rows || !rows.length) return;
    parts.push(`<div class="write-diff-section"><h4>${label}（${rows.length}）</h4>`
      + rows.map((c) => `<div class="write-vcs-item" data-diff-chapter="${c.chapter_id}">`
        + `<div>${escapeHtml(chapterLabel(c))}</div>`
        + `<div class="vcs-meta">${escapeHtml(fieldText(c.fields))}</div></div>`).join('')
      + '</div>');
  });
  const opLabels = { added: '新增', removed: '删除', changed: '修改' };
  const ledgerRows = [];
  Object.keys(data.ledger || {}).forEach((key) => {
    const item = (data.ledger || {})[key] || {};
    const bits = [];
    ['added', 'removed', 'changed'].forEach((op) => {
      const list = item[op] || [];
      if (list.length) bits.push(`${opLabels[op]} ${list.length}`);
    });
    if (bits.length) {
      ledgerRows.push(`<div class="write-vcs-item"><div>${escapeHtml(key)}</div>`
        + `<div class="vcs-meta">${bits.join(' · ')}</div></div>`);
    }
  });
  if (ledgerRows.length) {
    parts.push(`<div class="write-diff-section"><h4>账本</h4>`
      + `${ledgerRows.join('')}</div>`);
  }
  if (data.text_diff) {
    const lines = String(data.text_diff).split('\n').map((line) => {
      let cls = '';
      if (line.startsWith('+++') || line.startsWith('---')
          || line.startsWith('@@')) cls = 'write-diff-line-hunk';
      else if (line.startsWith('+')) cls = 'write-diff-line-add';
      else if (line.startsWith('-')) cls = 'write-diff-line-del';
      return `<span class="${cls}">${escapeHtml(line)}</span>`;
    }).join('\n');
    parts.push(`<div class="write-diff-section"><h4>正文</h4>`
      + `<pre>${lines}</pre></div>`);
  }
  if (!parts.length) parts.push('<div class="write-vcs-empty">没有差异</div>');
  $('writeDiffBody').innerHTML = parts.join('');
}

async function commitVcs() {
  if (!S.workId) return;
  try {
    await flush();
    const message = $('writeVcsMessage').value.trim();
    await api(`/api/writing/works/${encodeURIComponent(S.workId)}/vcs/commit`,
      { method: 'POST', body: JSON.stringify({ message }) });
    $('writeVcsMessage').value = '';
    setStatus('已提交', 'ok');
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function vcsPath(suffix) {
  return `/api/writing/works/${encodeURIComponent(S.workId)}/vcs/${suffix}`;
}

async function createVcsBranch() {
  if (!S.workId) return;
  const name = window.prompt('新分支名（岔路）——建好会立刻切过去');
  if (!name || !name.trim()) return;
  try {
    await flush();
    await api(vcsPath('switch'),
      { method: 'POST',
        body: JSON.stringify({ name: name.trim(), create: true,
                               checkpoint: true }) });
    setStatus(`已创建并切换到 ${name.trim()}`, 'ok');
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function renameVcsBranch(name) {
  const to = window.prompt(`把分支「${name}」改名为`, name);
  if (!to || !to.trim() || to.trim() === name) return;
  try {
    await api(vcsPath('branches/rename'),
      { method: 'POST', body: JSON.stringify({ from: name, to: to.trim() }) });
    setStatus(`已改名为 ${to.trim()}`, 'ok');
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function deleteVcsBranch(name) {
  if (!window.confirm(`删除分支「${name}」？提交不会消失，只删掉这个入口。`)) return;
  const send = (force) => api(vcsPath('branches/delete'),
    { method: 'POST', body: JSON.stringify({ name, force }) });
  try {
    await send(false);
    setStatus(`已删除分支 ${name}`, 'ok');
  } catch (err) {
    if (!/没合并|未合并|强制/.test(err.message)) {
      setStatus(err.message, 'error');
      return;
    }
    if (!window.confirm(`${err.message}\n\n仍要强制删除吗？`)) return;
    try {
      await send(true);
      setStatus(`已强制删除分支 ${name}`, 'ok');
    } catch (err2) {
      setStatus(err2.message, 'error');
      return;
    }
  }
  await loadVcs();
}

async function amendVcs() {
  if (!S.workId) return;
  const current = (S.vcs && S.vcs.head && S.vcs.head.message) || '';
  const message = window.prompt(
    '修正上一次提交：说明留空就沿用原文。\n工作区里没提交的改动也会一并带进去。',
    current);
  if (message === null) return;
  try {
    await flush();
    await api(vcsPath('amend'),
      { method: 'POST', body: JSON.stringify({ message }) });
    setStatus('已修正上一次提交', 'ok');
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function stashVcs() {
  if (!S.workId) return;
  const message = window.prompt('暂存说明（可留空）', '');
  if (message === null) return;
  try {
    await flush();
    await api(vcsPath('stash'),
      { method: 'POST', body: JSON.stringify({ message }) });
    setStatus('已暂存，工作区回到上次提交', 'ok');
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function popVcsStash(name) {
  try {
    await flush();
    const res = await apiRaw(vcsPath('stash/pop'),
      { method: 'POST', body: JSON.stringify({ name }) });
    if (res.status === 409 && res.body.conflicts) {
      openVcsConflicts('stash-pop', 'stash/pop', { name }, res.body);
      return;
    }
    if (!res.ok) throw new Error(res.body.error || '恢复暂存失败');
    setStatus('已恢复暂存', 'ok');
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function dropVcsStash(name) {
  if (!window.confirm('丢弃这条暂存？里面的改动将不再能恢复。')) return;
  try {
    await api(vcsPath('stash/drop'),
      { method: 'POST', body: JSON.stringify({ name }) });
    setStatus('已丢弃暂存', 'ok');
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function createVcsTag() {
  if (!S.workId) return;
  const name = window.prompt('标签名（例如 v1定稿）');
  if (!name || !name.trim()) return;
  try {
    await api(vcsPath('tags'),
      { method: 'POST', body: JSON.stringify({ name: name.trim() }) });
    setStatus(`已打标签 ${name.trim()}`, 'ok');
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function deleteVcsTag(name) {
  if (!window.confirm(`删除标签「${name}」？`)) return;
  try {
    await api(vcsPath('tags/delete'),
      { method: 'POST', body: JSON.stringify({ name }) });
    setStatus('已删除标签', 'ok');
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function discardVcsChapter(chapterId) {
  if (!chapterId) return;
  if (!window.confirm('只丢弃这一章的未提交改动？其他章与账本不动。')) return;
  try {
    await api(vcsPath('discard'),
      { method: 'POST', body: JSON.stringify({ chapter_id: chapterId }) });
    setStatus('已丢弃这一章的改动', 'ok');
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function checkoutVcsBranch(name) {
  if (!S.workId || !name) return;
  if (name === (S.vcs && S.vcs.branch)) return;
  try {
    await flush();
    await api(`/api/writing/works/${encodeURIComponent(S.workId)}/vcs/checkout`,
      { method: 'POST', body: JSON.stringify({ branch: name, checkpoint: true }) });
    setStatus(`已切换到 ${name}`, 'ok');
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
    await loadVcs();
  }
}

async function restoreVcsCommit(commitId) {
  if (!S.workId || !commitId) return;
  if (!window.confirm('回滚到这条提交？正文与账本会一起恢复，历史不会删除。')) return;
  try {
    await flush();
    await api(`/api/writing/works/${encodeURIComponent(S.workId)}/vcs/restore`,
      { method: 'POST', body: JSON.stringify({ commit_id: commitId }) });
    setStatus('已回滚', 'ok');
    setView(S.view);
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

// ── 合并 / 撤销 / 摘取：同一套冲突裁决 ─────────────────────────────

const CONFLICT_LABELS = {
  merge: ['保留当前分支', '采用岔路'],
  revert: ['保留当前分支', '采用撤销后的版本'],
  'cherry-pick': ['保留当前分支', '采用该提交的版本'],
  'stash-pop': ['保留当前分支', '采用暂存里的版本'],
};

async function mergeVcsBranch(name) {
  if (!S.workId) return;
  if (!window.confirm(`把分支「${name}」合并到当前分支？`)) return;
  await runVcsCommand('merge', 'merge', { branch: name },
    `已合并 ${name}`);
}

async function revertVcsCommit(commitId) {
  if (!commitId) return;
  if (!window.confirm('撤销这次提交的改动？会留下一条反向提交，历史不删。')) return;
  await runVcsCommand('revert', 'revert', { commit_id: commitId },
    '已撤销这次提交');
}

async function pickVcsCommit(commitId) {
  if (!commitId) return;
  if (!window.confirm('把这次提交的改动摘到当前分支？')) return;
  await runVcsCommand('cherry-pick', 'cherry-pick', { commit_id: commitId },
    '已摘取这次提交');
}

async function runVcsCommand(kind, path, body, okText) {
  if (!S.workId) return;
  try {
    await flush();
    const res = await apiRaw(vcsPath(path),
      { method: 'POST', body: JSON.stringify(body) });
    if (res.status === 409 && res.body.conflicts) {
      openVcsConflicts(kind, path, body, res.body);
      return;
    }
    if (!res.ok) throw new Error(res.body.error || '操作失败');
    setStatus(okText, 'ok');
    S.diffCommitId = '';
    setView(S.view);
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function openVcsConflicts(kind, path, body, data) {
  S.mergeReq = { kind, path, body,
                 conflicts: data.conflicts || [], choices: {} };
  renderVcsConflicts();
  $('writeEditorView').hidden = true;
  $('writeMapView').hidden = true;
  $('writeCurveView').hidden = true;
  $('writeDiffView').hidden = true;
  $('writeMergeView').hidden = false;
}

function previewText(entry, preview) {
  if (preview) return preview;
  if (!entry) return '（这边没有这一章）';
  return `（${entry.word_count || 0} 字）`;
}

function renderVcsConflicts() {
  const req = S.mergeReq;
  if (!req) return;
  const [oursLabel, theirsLabel] = CONFLICT_LABELS[req.kind]
    || ['保留当前', '采用对方'];
  const done = req.conflicts.filter((c) => req.choices[c.key]).length;
  $('writeMergeTitle').textContent =
    `解决冲突（${done}/${req.conflicts.length}）`;
  const apply = $('writeMergeApply');
  apply.disabled = done !== req.conflicts.length;
  apply.textContent = done === req.conflicts.length ? '完成' : '还有未裁决项';

  $('writeMergeBody').innerHTML = req.conflicts.map((c, i) => {
    const choice = req.choices[c.key];
    const btn = (value, label) => (
      `<button class="write-btn${choice === value ? ' primary' : ''}"`
      + ` data-merge-key="${escapeHtmlAttr(c.key)}"`
      + ` data-merge-choice="${value}">${label}</button>`);
    let detail = '';
    if (c.kind === 'chapter') {
      detail = `<div class="write-merge-previews">`
        + `<div><b>当前</b>${escapeHtml(previewText(c.ours, c.ours_preview))}</div>`
        + `<div><b>对方</b>${escapeHtml(previewText(c.theirs, c.theirs_preview))}</div>`
        + '</div>';
    } else {
      detail = `<div class="vcs-meta">账本 · ${escapeHtml(c.collection || '')}`
        + ` · ${escapeHtml(c.label || c.id || '')}</div>`;
    }
    const title = c.kind === 'chapter'
      ? chapterLabel({ chapter_index: c.chapter_index, title: c.title })
      : `账本条目 ${c.id || ''}`;
    return `<div class="write-merge-item"><div>${i + 1}. ${escapeHtml(title)}</div>`
      + detail
      + `<div class="write-merge-choices">${btn('ours', oursLabel)}`
      + `${btn('theirs', theirsLabel)}${btn('base', '回到分叉点')}</div></div>`;
  }).join('');
}

async function applyVcsResolutions() {
  const req = S.mergeReq;
  if (!req) return;
  const missing = req.conflicts.filter((c) => !req.choices[c.key]);
  if (missing.length) {
    setStatus(`还有 ${missing.length} 处没有裁决`, 'error');
    return;
  }
  try {
    const body = { ...req.body, resolutions: req.choices };
    const res = await apiRaw(vcsPath(req.path),
      { method: 'POST', body: JSON.stringify(body) });
    if (res.status === 409 && res.body.conflicts) {
      // 后端可能报出新的一批冲突（理论上不会），继续裁决。
      S.mergeReq = { ...req, conflicts: res.body.conflicts, choices: {} };
      renderVcsConflicts();
      return;
    }
    if (!res.ok) throw new Error(res.body.error || '操作失败');
    S.mergeReq = null;
    setStatus('已应用裁决', 'ok');
    setView(S.view);
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function cancelVcsMerge() {
  S.mergeReq = null;
  setView(S.view);
}

async function resolveVcsFeedback(feedbackId) {
  if (!feedbackId) return;
  try {
    await api(`/api/writing/feedback/${encodeURIComponent(feedbackId)}/status`,
      { method: 'POST', body: JSON.stringify({ status: 'fixed' }) });
    setStatus('已标记为处理', 'ok');
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function toggleVcsReflog() {
  const section = $('writeVcsReflogSection');
  if (!section.hidden) {
    section.hidden = true;
    return;
  }
  try {
    const data = await api(vcsPath('reflog?limit=50'));
    S.vcsReflog = data.entries || [];
    renderVcsReflog();
    section.hidden = false;
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function renderVcsReflog() {
  const box = $('writeVcsReflog');
  if (!S.vcsReflog.length) {
    box.innerHTML = '<div class="write-vcs-empty">还没有位置变化</div>';
    return;
  }
  const reasons = { manual: '提交', auto: '自动提交', checkpoint: '检查点',
                    checkout: '切换分支', merge: '合并', 'merge-ff': '合并（快进）',
                    revert: '撤销', 'cherry-pick': '摘取', amend: '修正',
                    restore: '回滚', 'stash-pop': '恢复暂存' };
  box.innerHTML = S.vcsReflog.map((e) => (
    `<div class="write-vcs-item" data-reflog-commit="${escapeHtmlAttr(e.commit_id)}">`
    + `<div>${escapeHtml(e.commit_short)} ${escapeHtml(e.commit_message || '')}</div>`
    + `<div class="vcs-meta">${escapeHtml(reasons[e.reason] || e.reason)}`
    + ` · ${escapeHtml(e.branch || '')} · ${escapeHtml(whenText(e.time_created))}</div></div>`
  )).join('');
}

async function loadMap() {
  const host = $('writeMapHost');
  if (!S.workId) { host.textContent = '先选一个作品。'; return; }
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/map`);
    renderStoryMap(host, data, (index) => {
      setView('editor');
      selectChapter(index);
    });
  } catch (err) {
    host.textContent = err.message;
  }
}

async function loadCurve() {
  const host = $('writeCurveHost');
  if (!S.workId) { host.textContent = '先选一个作品。'; return; }
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/tension`);
    renderTensionCurve(host, data, {
      onSet: async (index, value) => {
        try {
          await api(
            `/api/writing/works/${encodeURIComponent(S.workId)}/tension`,
            { method: 'PUT', body: JSON.stringify({
              chapter_index: index, value }) });
          setStatus(`第${index}章紧张度设为 ${value}（作者锚点）`, 'ok');
          await loadCurve();
        } catch (err) {
          setStatus(err.message, 'error');
        }
      },
    });
  } catch (err) {
    host.textContent = err.message;
  }
}

const DELIVERY_LABEL = {
  blocked: '不可交付',
  inconclusive: '未看清',
  pass: '通过',
  revise: '建议修订',
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderReview(review) {
  const scoreBox = $('writeReviewScore');
  const list = $('writeFindings');
  list.textContent = '';
  if (!review) {
    scoreBox.textContent = '';
    list.appendChild(el('p', 'write-review-empty',
      '还没有审稿记录。点「开始审稿」。'));
    return;
  }
  const quality = review.quality_score == null ? '—' : review.quality_score;
  scoreBox.textContent = `${DELIVERY_LABEL[review.delivery_status]
    || review.delivery_status} · ${quality} 分 · 覆盖 ${review.coverage}`
    + (review.status === 'stale' ? ' · 已过期（正文变过）' : '');
  const findings = review.findings || [];
  if (!findings.length) {
    list.appendChild(el('p', 'write-review-empty', '没有发现问题。'));
    return;
  }
  findings.forEach((f) => list.appendChild(findingCard(f)));
}

function findingCard(finding) {
  const card = el('div', 'write-finding');
  const head = el('div', 'write-finding-head');
  const sev = el('span', 'write-sev', finding.severity);
  sev.dataset.sev = finding.severity;
  head.append(sev, el('span', 'write-finding-cat', finding.category));
  card.appendChild(head);
  card.appendChild(el('div', '', finding.issue || '（未描述）'));
  if (finding.evidence) {
    card.appendChild(el('div', 'write-finding-evidence', finding.evidence));
  }
  if (finding.fix_hint) {
    card.appendChild(el('div', 'write-rev-status', `方向：${finding.fix_hint}`));
  }
  const actions = el('div', 'write-finding-actions');
  const propose = el('button', 'write-mini-btn', '生成修订');
  propose.onclick = () => proposeRevision(finding.id);
  actions.appendChild(propose);
  card.appendChild(actions);
  return card;
}

async function loadReview() {
  if (!S.chapter) return;
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/chapters/${S.index}/review`);
    renderReview(data.review);
  } catch (err) {
    setStatus(err.message, 'error');
  }
  await loadRevisions();
}

async function runReview() {
  if (!S.chapter) { setStatus('先选一章再审稿', 'error'); return; }
  setStatus('正在审稿…');
  $('writeReviewRun').disabled = true;
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/chapters/${S.index}/review`,
      { method: 'POST' });
    renderReview(data);
    setStatus(`审稿完成：${(data.findings || []).length} 条问题`, 'ok');
    await loadRevisions();
  } catch (err) {
    setStatus(err.message, 'error');
  } finally {
    $('writeReviewRun').disabled = false;
  }
}

async function proposeRevision(findingId) {
  setStatus('正在生成修订提案…');
  try {
    await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/chapters/${S.index}/findings/${findingId}/revision`,
      { method: 'POST' });
    await loadRevisions();
    setStatus('修订提案已生成，确认后应用', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function loadRevisions() {
  if (!S.chapter) return;
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/chapters/${S.index}/revisions`);
    renderRevisions(data.revisions || []);
  } catch (_) { /* best-effort */ }
}

function renderRevisions(list) {
  const box = $('writeRevisions');
  box.textContent = '';
  const proposed = list.filter((r) => r.status === 'proposed');
  if (!proposed.length) return;
  box.appendChild(el('div', 'write-review-empty', '待确认的修订：'));
  proposed.forEach((rev) => {
    const card = el('div', 'write-revision');
    card.appendChild(el('div', 'write-finding-cat',
      `${rev.action} · ${rev.anchor_start}-${rev.anchor_end}`));
    card.appendChild(el('div', 'write-finding-evidence', rev.anchor_quote));
    card.appendChild(el('div', '', `→ ${rev.replacement}`));
    const actions = el('div', 'write-finding-actions');
    const apply = el('button', 'write-mini-btn', '应用');
    apply.onclick = () => applyRevision(rev.id);
    const reject = el('button', 'write-mini-btn', '驳回');
    reject.onclick = () => rejectRevision(rev.id);
    actions.append(apply, reject);
    card.appendChild(actions);
    box.appendChild(card);
  });
}

async function applyRevision(revisionId) {
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/chapters/${S.index}/revisions/${revisionId}/apply`,
      { method: 'POST', body: JSON.stringify({
        expected_hash: S.chapter ? S.chapter.content_hash : '',
      }) });
    $('writeEditor').value = data.content;
    if (S.chapter) {
      S.chapter.content = data.content;
      S.chapter.content_hash = data.content_hash;
      if (data.word_count) S.chapter.word_count = data.word_count;
    }
    // 应用修订也是一次落库保存，算新的保存点。
    S.savePoint = { version: data.version,
                    content_hash: data.content_hash };
    resetEditorHistory();
    updateEditorState();
    setStatus(`修订已应用 · 版本 ${data.version}`, 'ok');
    await refreshChapters();
    await loadReview();
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function rejectRevision(revisionId) {
  try {
    await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/chapters/${S.index}/revisions/${revisionId}/reject`,
      { method: 'POST' });
    await loadRevisions();
    setStatus('已驳回该提案');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

// ── 编辑器状态：dirty 只在内存里 / draft 只暂存过 / clean 已保存 ──────
// 「保存点」是手动保存或 agent 改稿留下的版本；自动保存和动作前落盘只算
// 暂存稿。这样既能随时丢不了字，又能一眼看出「这些改动还没正式保存」。
const UNDO_LIMIT = 100;     // 一步一份整章正文，100 步对一章足够，也不吃内存。
const UNDO_BURST_MS = 700;  // 同一串输入并成一步，撤销才不会逐字回放。

function editorState() {
  if (!S.chapter) return 'clean';
  const titleDirty = $('writeChapterTitle').value.trim()
    !== (S.chapter.title || '');
  if ($('writeEditor').value !== (S.chapter.content || '') || titleDirty) {
    return 'dirty';
  }
  const saved = (S.savePoint && S.savePoint.content_hash) || '';
  return (S.chapter.content_hash || '') === saved ? 'clean' : 'draft';
}

function isEditorDirty() {
  return editorState() === 'dirty';
}

function updateEditorState() {
  const state = editorState();
  const marker = $('writeEditorState');
  if (marker) {
    marker.hidden = state === 'clean';
    marker.textContent = state === 'draft' ? '已暂存' : '未保存';
    marker.classList.toggle('is-draft', state === 'draft');
  }
  const saveBtn = $('writeSaveBtn');
  if (saveBtn) saveBtn.classList.toggle('dirty', state !== 'clean');
  const revertBtn = $('writeRevertSaveBtn');
  if (revertBtn) revertBtn.hidden = state !== 'draft';
  // 分支徽标的脏点：本地未保存，或保存后还没刷新 VCS，都算不干净。
  const vcsClean = S.vcs ? (S.vcs.clean && !S.vcsStale) : true;
  const dot = $('writeVcsDirty');
  if (dot) dot.hidden = vcsClean && state === 'clean';
  updateUndoButtons();
}

// ── 编辑器撤销/重做：像办公文档的上一步/下一步 ────────────────────────
// 记的是整章快照（chapter 正文就几 KB），不是逐键差分；同一串输入按
// 700ms 并成一步。程序改动（切章、生成流）不入栈，只有人敲的才算。

function editorSnapshot() {
  const ed = $('writeEditor');
  return { content: ed.value, start: ed.selectionStart, end: ed.selectionEnd };
}

/** 改动发生前先留一份，input 时才知道「从什么退回去」。 */
function rememberPreEdit() {
  S.preEdit = editorSnapshot();
}

function pushUndo(snapshot) {
  const now = Date.now();
  const merge = S.undoAt > 0 && now - S.undoAt < UNDO_BURST_MS
    && S.undoStack.length > 0;
  S.undoAt = now;
  S.redoStack = [];
  if (!merge) {
    S.undoStack.push(snapshot);
    if (S.undoStack.length > UNDO_LIMIT) S.undoStack.shift();
  }
  updateUndoButtons();
}

function applyEditorSnapshot(snapshot) {
  const ed = $('writeEditor');
  ed.value = snapshot.content;
  try {
    ed.setSelectionRange(snapshot.start, snapshot.end);
  } catch (_) { /* 选区越界就算了，正文已经对了 */ }
  S.undoAt = 0;                     // 撤销后的下一次输入另起一步
  S.preEdit = null;
  updateEditorState();
  scheduleAutosave();
}

function undoEditor() {
  if (!S.undoStack.length) return;
  const entry = S.undoStack.pop();
  S.redoStack.push(editorSnapshot());
  applyEditorSnapshot(entry);
}

function redoEditor() {
  if (!S.redoStack.length) return;
  const entry = S.redoStack.pop();
  S.undoStack.push(editorSnapshot());
  applyEditorSnapshot(entry);
}

function resetEditorHistory() {
  S.undoStack = [];
  S.redoStack = [];
  S.undoAt = 0;
  S.preEdit = null;
  updateUndoButtons();
}

function updateUndoButtons() {
  const undo = $('writeUndoBtn');
  if (undo) undo.disabled = !S.undoStack.length;
  const redo = $('writeRedoBtn');
  if (redo) redo.disabled = !S.redoStack.length;
}

function scheduleAutosave() {
  clearTimeout(S.autosaveTimer);
  S.autosaveTimer = null;
  if (!S.chapter || S.generating) return;
  S.autosaveTimer = setTimeout(() => { autosaveNow(); }, AUTOSAVE_IDLE_MS);
}

async function autosaveNow() {
  clearTimeout(S.autosaveTimer);
  S.autosaveTimer = null;
  if (!S.chapter || S.generating || !isEditorDirty()) return;
  if (S.autosaving) {
    // 上一次保存还在路上：记一笔，等它回来再排一次，避免并发写。
    S.autosavePending = true;
    return;
  }
  S.autosaving = true;
  try {
    await saveContent({ reason: 'autosave', quiet: true });
  } finally {
    S.autosaving = false;
    if (S.autosavePending) {
      S.autosavePending = false;
      scheduleAutosave();
    }
  }
}

async function saveContent(options = {}) {
  const reason = options.reason || 'manual';
  const quiet = Boolean(options.quiet);
  // 手动保存是「保存点」，其余来源都只是「暂存稿」。
  const kind = reason === 'manual' ? 'save' : 'draft';
  if (!S.chapter) {
    if (!quiet) setStatus('先选一章再保存', 'error');
    return false;
  }
  if (quiet && !isEditorDirty()) {
    updateEditorState();
    return true;
  }
  const content = $('writeEditor').value;
  const title = $('writeChapterTitle').value.trim();
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/chapters/${S.index}/content`,
      { method: 'PUT', body: JSON.stringify({ content, title, reason, kind }) });
    if (S.chapter) {
      S.chapter.content = content;
      S.chapter.title = title;
      S.chapter.content_hash = data.content_hash;
      S.chapter.word_count = data.word_count;
      S.chapter.version = data.version;
      S.chapter.status = 'drafted';
    }
    if (kind === 'save') {
      S.savePoint = { version: data.version,
                      content_hash: data.content_hash };
    }
    if (quiet) {
      // 自动保存不拉整份 VCS 状态（status/log/stash/feedback 四个请求），
      // 只更新本地摘要；版本面板下次打开或手动保存时再刷新。
      S.vcsStale = true;
      const row = S.chapters.find((c) => c.chapter_index === S.index);
      if (row) {
        row.word_count = data.word_count;
        row.status = 'drafted';
        renderChapters();
      }
      setStatus(`${options.message || '已自动保存'} · ${data.word_count} 字`,
                'ok');
    } else {
      setStatus(`已保存 · 版本 ${data.version} · ${data.word_count} 字`, 'ok');
      await refreshChapters();
      await loadVcs();
    }
    updateEditorState();
    return true;
  } catch (err) {
    setStatus(err.message, 'error');
    // 保持脏标记：下一次空闲/失焦还会重试。
    updateEditorState();
    return false;
  }
}

/** 暂存：立刻落盘，但不算保存点——状态仍显示「已暂存」。 */
async function parkDraft() {
  clearTimeout(S.autosaveTimer);
  S.autosaveTimer = null;
  if (!S.chapter) { setStatus('先选一章再暂存', 'error'); return false; }
  if (!isEditorDirty()) {
    updateEditorState();
    setStatus('没有需要暂存的新内容');
    return true;
  }
  return saveContent({ reason: 'park', quiet: true, message: '已暂存' });
}

/** 还原：丢掉暂存稿，把正文带回最近一个保存点（撤销栈还能反悔）。 */
async function revertToSavePoint() {
  if (!S.chapter) return;
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}`
      + `/chapters/${S.index}/saved`);
    S.undoAt = 0;
    pushUndo(editorSnapshot());
    $('writeEditor').value = data.content || '';
    updateEditorState();
    await saveContent({ reason: 'park', quiet: true, message: '已还原' });
    setStatus(`已回到保存点 · 版本 ${data.version}`, 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function refreshChapters() {
  try {
    const data = await api(`/api/writing/works/${encodeURIComponent(S.workId)}`);
    S.chapters = data.chapters || [];
    S.volumes = data.volumes || [];
    renderChapters();
  } catch (_) { /* list refresh is best-effort */ }
}

function handleFrame(frame) {
  const line = frame.split('\n').find((l) => l.startsWith('data: '));
  if (!line) return;
  const raw = line.slice(6);
  if (raw === '[DONE]') return;
  let event;
  try { event = JSON.parse(raw); } catch (_) { return; }
  const data = event.data || {};
  if (event.event === 'stage') {
    let label = '模型写作中…';
    if (data.stage === 'assembled') {
      label = data.tokens
        ? `上下文已就绪 · ${data.tokens} tokens，开始生成…`
        : '上下文已就绪，开始生成…';
    } else if (data.stage === 'revising') {
      label = '正在修订…';
    }
    setStatus(label);
    // 装配结果由服务端随 stage 事件带回：风格诊断不单独再装配一遍。
    if (data.style) {
      S.styleInfo = data.style;
      renderStyleBar();
    }
  } else if (event.event === 'delta') {
    $('writeEditor').value += data.text || '';
    $('writeEditor').scrollTop = $('writeEditor').scrollHeight;
  } else if (event.event === 'done') {
    const finalContent = data.content || $('writeEditor').value;
    $('writeEditor').value = finalContent;
    S.chapter = { ...(S.chapter || {}), content: finalContent,
                  content_hash: data.content_hash,
                  word_count: data.word_count, version: data.version,
                  status: 'drafted' };
    // 生成落库就是保存点（不是暂存稿），标记与「还原」按钮跟着对齐。
    S.savePoint = { version: data.version,
                    content_hash: data.content_hash };
    setStatus(`已生成 · 版本 ${data.version} · ${data.word_count} 字`, 'ok');
    updateEditorState();
    refreshChapters();
    loadVcs();
  } else if (event.event === 'error') {
    setStatus(data.message || '生成失败', 'error');
  }
}

async function generate() {
  if (S.generating) return;
  if (!S.chapter) { setStatus('先选一章再生成', 'error'); return; }
  if (!S.contract) {
    // 合同仍是硬边界，但不再在页面上手写：让写作助手先定合同再起草。
    setStatus('本章还没有合同——跟写作助手说这一章要写什么，它会先定合同再起草。',
      'error');
    return;
  }
  // 生成会清空编辑器：先把作者手改落盘，别让生成吞掉未保存的草稿。
  await flush();
  clearTimeout(S.autosaveTimer);
  S.autosaveTimer = null;
  // 清空前压一步撤销：模型写崩了，Ctrl+Z 还能把原来的正文退回来。
  S.undoAt = 0;
  pushUndo(editorSnapshot());
  S.generating = true;
  $('writeGenBtn').disabled = true;
  $('writeEditor').value = '';
  updateEditorState();
  setStatus('正在装配上下文…');
  try {
    const q = S.styleAuthorId
      ? `?style_author_id=${encodeURIComponent(S.styleAuthorId)}` : '';
    const res = await fetch(
      `/api/writing/works/${encodeURIComponent(S.workId)}`
      + `/chapters/${S.index}/write${q}`,
      { method: 'POST' });
    if (!res.ok || !res.body) throw new Error(`生成失败 (${res.status})`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut = buffer.indexOf('\n\n');
      while (cut >= 0) {
        handleFrame(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        cut = buffer.indexOf('\n\n');
      }
    }
  } catch (err) {
    setStatus(err.message, 'error');
  } finally {
    S.generating = false;
    $('writeGenBtn').disabled = false;
  }
}

// 新建不再弹 prompt：直接在左列长出一个空栏，敲了字才落库。
function createWork() {
  closeWorkspaceMenu();
  if (!(S.draft && S.draft.kind === 'work')) {
    S.draft = { kind: 'work', value: '', focus: true };
  }
  S.draft.focus = true;
  renderWorks();
}

function createChapter() {
  if (!S.workId) { setStatus('先选一个作品', 'error'); return; }
  if (S.rootCollapsed) {
    S.rootCollapsed = false;
    writeFlag('bc.writeRootCollapsed', false);
  }
  if (!(S.draft && S.draft.kind === 'chapter')) {
    S.draft = { kind: 'chapter', value: '', focus: true };
  }
  S.draft.focus = true;
  renderChapters();
}

async function commitDraftWork(rawValue) {
  if (!S.draft || S.draft.kind !== 'work') return;
  const title = (rawValue || '').trim();
  S.draft = null;
  renderWorks();
  if (!title) return;                       // 空栏失焦 = 放弃
  setStatus('正在创建作品…');
  try {
    const data = await api('/api/writing/works',
      { method: 'POST', body: JSON.stringify({ title }) });
    await loadWorks();
    await selectWork(data.work_id);
    setStatus('作品已创建。建一章，再让写作助手起草。', 'ok');
  } catch (err) {
    S.draft = { kind: 'work', value: title, focus: true };
    renderWorks();
    setStatus(err.message, 'error');
  }
}

async function commitDraftChapter(rawValue) {
  if (!S.draft || S.draft.kind !== 'chapter') return;
  const title = (rawValue || '').trim();
  S.draft = null;
  renderChapters();
  // 和新建作品同一条规则：没敲字就放弃，回车也不建无标题章。
  if (!title) return;
  if (!S.workId) { setStatus('先选一个作品', 'error'); return; }
  setStatus('正在创建章节…');
  try {
    await api(`/api/writing/works/${encodeURIComponent(S.workId)}/chapters`,
      { method: 'POST', body: JSON.stringify({ title }) });
    await selectWork(S.workId);
    setStatus('章节已创建。跟写作助手说本章要写什么，它会起草。', 'ok');
  } catch (err) {
    S.draft = { kind: 'chapter', value: title, focus: true };
    renderChapters();
    setStatus(err.message, 'error');
  }
}

function bind() {
  $('writeWorkspaceBtn').onclick = toggleWorkspaceMenu;
  $('writeWorkspaceBtn').addEventListener('contextmenu', (event) => {
    const work = S.works.find((w) => w.id === S.workId);
    if (!work) return;
    event.preventDefault();
    event.stopPropagation();
    closeWorkspaceMenu();
    $('writeWorkspaceBtn').focus();
    openWorkContextMenu(event.clientX, event.clientY, work, true);
  });
  $('writeWorkspaceNew').onclick = () => {
    closeWorkspaceMenu();
    createWork();
  };
  $('writeNewWorkBtn').onclick = createWork;
  const workspaceSearch = $('writeWorkspaceSearch');
  workspaceSearch.addEventListener('input', (event) => {
    S.workspaceFilter = event.target.value;
    renderWorkspaceMenu();
  });
  workspaceSearch.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'Enter') return;
    const first = $('writeWorkspaceList')
      .querySelector('.write-workspace-item');
    if (!first) return;
    event.preventDefault();
    if (event.key === 'Enter') first.click();
    else first.focus();
  });
  document.addEventListener('click', (event) => {
    if (!S.workspaceOpen) return;
    const head = document.querySelector('.write-rail-head');
    if (head && !head.contains(event.target)) closeWorkspaceMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && S.workspaceOpen) {
      event.preventDefault();
      closeWorkspaceMenu(true);
    }
  });
  $('writeGenBtn').onclick = generate;
  $('writeSaveBtn').onclick = () => saveContent();
  $('writeParkBtn').onclick = parkDraft;
  $('writeRevertSaveBtn').onclick = revertToSavePoint;
  $('writeUndoBtn').onclick = undoEditor;
  $('writeRedoBtn').onclick = redoEditor;

  // 编辑器：beforeinput 记改动前的快照（撤销用），input 只改本地脏标记；
  // 空闲 2 秒、失焦、切到后台才落盘。
  $('writeEditor').addEventListener('beforeinput', (event) => {
    if (event.isComposing) return;      // 组字中的中间态不单独记步
    rememberPreEdit();
  });
  // 中文输入法：组字开始时记一次，整段组字结束时只落一步撤销。
  $('writeEditor').addEventListener('compositionstart', rememberPreEdit);
  $('writeEditor').addEventListener('input', (event) => {
    if (!event.isComposing) {
      const pre = S.preEdit;
      S.preEdit = null;
      if (pre) pushUndo(pre);
    }
    updateEditorState();
    scheduleAutosave();
  });
  $('writeEditor').addEventListener('keydown', (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    const key = event.key.toLowerCase();
    if (key === 'z' && !event.shiftKey) {
      event.preventDefault();
      undoEditor();
    } else if (key === 'y' || (key === 'z' && event.shiftKey)) {
      event.preventDefault();
      redoEditor();
    } else if (key === 's') {
      // 别让浏览器弹「保存网页」：Ctrl+S 保存，Ctrl+Shift+S 暂存。
      event.preventDefault();
      if (event.shiftKey) parkDraft(); else saveContent();
    }
  });
  $('writeEditor').addEventListener('blur', () => { autosaveNow(); });
  $('writeChapterTitle').addEventListener('input', () => {
    updateEditorState();
    scheduleAutosave();
  });
  $('writeChapterTitle').addEventListener('blur', () => { autosaveNow(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) autosaveNow();
  });
  window.addEventListener('beforeunload', (event) => {
    if (!isEditorDirty()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  $('writeStyleSelect').onchange = (event) => {
    setStyleAuthor(event.target.value);
  };
  $('writeStyleCheckBtn').onclick = runStyleCheck;
  $('writeReviewRun').onclick = runReview;
  $('writeSideClose').onclick = () => $('writeSide').classList.remove('open');
  // 写作 git
  $('writeVcsCommitBtn').onclick = commitVcs;
  $('writeVcsAmendBtn').onclick = amendVcs;
  $('writeVcsNewBranch').onclick = createVcsBranch;
  $('writeVcsStashBtn').onclick = stashVcs;
  $('writeVcsTagBtn').onclick = createVcsTag;
  $('writeVcsReflogBtn').onclick = toggleVcsReflog;
  $('writeVcsBranchSelect').onchange = (event) => {
    checkoutVcsBranch(event.target.value);
  };
  $('writeVcsBadge').onclick = () => showPane('vcs');
  $('writeVcsAllBranches').onchange = (event) => {
    S.logAllBranches = event.target.checked;
    loadVcs();
  };
  $('writeVcsOnlyChapter').onchange = (event) => {
    S.logOnlyChapter = event.target.checked;
    loadVcs();
  };
  $('writeDiffClose').onclick = () => setView(S.view);
  $('writeDiffRestore').onclick = () => restoreVcsCommit(S.diffCommitId);
  $('writeDiffRevert').onclick = () => revertVcsCommit(S.diffCommitId);
  $('writeDiffPick').onclick = () => pickVcsCommit(S.diffCommitId);
  $('writeMergeApply').onclick = applyVcsResolutions;
  $('writeMergeCancel').onclick = cancelVcsMerge;
  $('writeMergeBody').addEventListener('click', (event) => {
    const button = event.target.closest('[data-merge-key]');
    if (!button || !S.mergeReq) return;
    S.mergeReq.choices[button.dataset.mergeKey] = button.dataset.mergeChoice;
    renderVcsConflicts();
  });
  $('writeVcsChanges').addEventListener('click', (event) => {
    const discard = event.target.closest('[data-discard-chapter]');
    if (discard) {
      discardVcsChapter(discard.dataset.discardChapter);
      return;
    }
    const row = event.target.closest('[data-diff-chapter]');
    if (row) {
      openVcsDiff('HEAD', 'WORK', row.dataset.diffChapter);
      return;
    }
    if (event.target.closest('[data-diff-ledger]')) {
      openVcsDiff('HEAD', 'WORK', '');
    }
  });
  $('writeVcsBranches').addEventListener('click', (event) => {
    const diff = event.target.closest('[data-diff-branch]');
    if (diff) {
      const name = diff.dataset.diffBranch;
      const current = (S.vcs && S.vcs.branch) || 'main';
      openVcsDiff(`${current}...${name}`, name, '');
      return;
    }
    const rename = event.target.closest('[data-rename-branch]');
    if (rename) { renameVcsBranch(rename.dataset.renameBranch); return; }
    const merge = event.target.closest('[data-merge-branch]');
    if (merge) { mergeVcsBranch(merge.dataset.mergeBranch); return; }
    const del = event.target.closest('[data-delete-branch]');
    if (del) deleteVcsBranch(del.dataset.deleteBranch);
  });
  $('writeVcsTags').addEventListener('click', (event) => {
    const del = event.target.closest('[data-delete-tag]');
    if (del) deleteVcsTag(del.dataset.deleteTag);
  });
  $('writeVcsStashes').addEventListener('click', (event) => {
    const pop = event.target.closest('[data-pop-stash]');
    if (pop) { popVcsStash(pop.dataset.popStash); return; }
    const drop = event.target.closest('[data-drop-stash]');
    if (drop) dropVcsStash(drop.dataset.dropStash);
  });
  $('writeVcsReflog').addEventListener('click', (event) => {
    const row = event.target.closest('[data-reflog-commit]');
    if (!row || !row.dataset.reflogCommit) return;
    restoreVcsCommit(row.dataset.reflogCommit);
  });
  $('writeVcsFeedback').addEventListener('click', (event) => {
    const button = event.target.closest('[data-resolve-feedback]');
    if (button) resolveVcsFeedback(button.dataset.resolveFeedback);
  });
  $('writeVcsLog').addEventListener('click', (event) => {
    const row = event.target.closest('[data-commit]');
    if (!row) return;
    const commit = S.vcsLog.find((c) => c.id === row.dataset.commit);
    if (commit) openVcsDiff(commit.parent_id || 'EMPTY', commit.id, '');
  });
  $('writeDiffBody').addEventListener('click', (event) => {
    const row = event.target.closest('[data-diff-chapter]');
    if (row) openVcsDiff(S.diffFrom, S.diffTo, row.dataset.diffChapter);
  });
  document.querySelectorAll('.write-side-tab').forEach((tab) => {
    tab.onclick = () => showPane(tab.dataset.pane);
  });
  document.querySelectorAll('.write-view-tab').forEach((tab) => {
    tab.onclick = () => setView(tab.dataset.view);
  });
}

async function open() {
  try {
    await loadWorks();
    if (!S.workId && S.works.length) await selectWork(S.works[0].id);
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

/** 聊天面板要的现场：当前作品与章。 */
function context() {
  return { workId: S.workId, index: S.index };
}

/** 发消息前先把未保存的手改落盘——否则 agent 改稿后刷新会吞掉它。 */
async function flush() {
  clearTimeout(S.autosaveTimer);
  S.autosaveTimer = null;
  if (!S.chapter) return true;
  if (!isEditorDirty()) return true;
  return saveContent({ reason: 'flush', quiet: true });
}

/** 一轮对话结束后对齐磁盘：工具可能建了书/章、写了正文。 */
async function refresh() {
  const knownWorks = new Set(S.works.map((w) => w.id));
  const knownMax = S.chapters.reduce(
    (max, c) => Math.max(max, c.chapter_index), 0);
  try {
    await loadWorks();
    const created = S.works.find((w) => !knownWorks.has(w.id));
    if (created) {
      await selectWork(created.id);
      return;
    }
    if (!S.workId && S.works.length) {
      await selectWork(S.works[0].id);
      return;
    }
    if (!S.workId) return;
    const data = await api(`/api/writing/works/${encodeURIComponent(S.workId)}`);
    S.chapters = data.chapters || [];
    S.volumes = data.volumes || [];
    renderChapters();
    const newer = S.chapters.filter((c) => c.chapter_index > knownMax);
    const target = newer.length
      ? newer[newer.length - 1]
      : (S.chapters.find((c) => c.chapter_index === S.index)
        || S.chapters[S.chapters.length - 1]);
    if (target) await selectChapter(target.chapter_index);
    if (S.view === 'map') loadMap();
    if (S.view === 'curve') loadCurve();
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

window.WritingUI = { open, context, refresh, flush,
                     setStyleRef: setStyleAuthor };
bind();

