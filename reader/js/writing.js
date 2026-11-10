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
  // 空项目 = 作者显式选了「不绑定作品」：助手不带入任何一本的设定与正文。
  // 记在本地，刷新页面/工具回合结束后不会被自动选中的第一本冲掉。
  emptyProject: readFlag('bc.writeEmptyProject'),
  chapters: [],
  volumes: [],
  // 左栏是一棵「作品 → 卷 → 章 → 场景」的树：只展开当前工作台，
  // 根目录折叠状态记在本地；切作品要回到「正文」分区。
  rootCollapsed: readFlag('bc.writeRootCollapsed'),
  workspaceCollapsed: readFlag('bc.writeWorkspaceCollapsed'),
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
  // 左栏两个导航：work=项目浏览器（正文 + 工作区），vcs=版本控制。
  section: 'work',
  // 正文分区里最后停的视图（编辑器/地图/曲线），切回正文时原样回去。
  textView: 'editor',
  view: 'editor',
  // 差异是主区的一屏：进来时记下当时那一屏，返回或换分区都回到它。
  diffReturn: null,
  contractRows: null,
  contractByChapter: null,
  // 作品工作区（草稿分区）：文件清单、当前打开的文件、收起目录。
  docs: [],
  docDirs: [],
  docPath: '',
  docRow: null,
  docsCollapsed: new Set(),
  // 全局素材库：不属于任何作品。空项目时左栏默认展开；有作品时是固定入口。
  library: [],
  libDirs: [],
  // 待归类里还等着整理的素材数量：空项目空白会话据此挂建议气泡。
  libPending: 0,
  // 分类默认收起：素材攒起来以后一屏放不下，点开哪个看哪个。
  libExpanded: new Set(),
  libraryCollapsed: readFlag('bc.writeLibraryCollapsed'),
  libPath: '',
  libRow: null,
  libEditing: false,
  // 回收站：删除不硬删，列表只读；恢复/清理都走显式动作。
  libRecycled: [],
  libRecycleCollapsed: true,
  // 工作区的新建行内输入（{parent, kind, value}）与底部时间线。
  docsDraft: null,
  timeline: null,
  // 新建作品/章节的行内草稿：{kind:'work'|'chapter', value, focus}
  // 不先落库——用户敲了什么才建什么，空栏等于没发生。
  draft: null,
  // 改名保存失败时，把名字塞回重命名模式重试，别让一次网络错误吞掉输入。
  renameRetry: null,
  // 写作 git：状态、历史、当前 diff 的起止引用。
  vcs: null,
  // 左栏版本控制的分组开合（bind 时从本地读），只有「更改」「图表」默认摊开。
  vcsOpen: {},
  vcsLog: [],
  // 版本图里点开的提交：id → {loading?, error?}。展开时按需取这条提交
  // 相对第一父节点的改动清单，缓存到 commitFiles（提交内容不会变）。
  vcsExpand: {},
  // 刚点开的那条提交：只有它会播放展开动画，后续重渲染不再重复播。
  vcsOpening: '',
  commitFiles: {},
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

//: 左栏两个导航：作品（项目浏览器）与版本控制（VS Code 的 Source Control）。
const SECTIONS = ['work', 'vcs'];

//: 主区的每一块视图（同一时刻只显示一块）。diff / merge 是叠在正文上的临时层。
const MAIN_VIEWS = {
  editor: 'writeEditorView',
  map: 'writeMapView',
  curve: 'writeCurveView',
  draft: 'writeDraftView',
  contract: 'writeContractView',
  diff: 'writeDiffView',
  merge: 'writeMergeView',
};

const TEXT_VIEWS = new Set(['editor', 'map', 'curve']);

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
  renderNav();
  renderChapters();
}

// ── 写作台（作品切换下拉）─────────────────────────────────────────
// 只把「当前这一本」放进工作台；其余作品收进下拉，别让书单常驻左栏。

function renderWorkspaceMenu() {
  const list = $('writeWorkspaceList');
  if (!list) return;
  const current = S.works.find((w) => w.id === S.workId);
  const title = current ? (current.title || '（未命名）') : '';
  $('writeWorkspaceName').textContent = title || '空项目';
  // 书名 tab 在左栏会截断，悬停看全；切换按钮自己的标题是「切换作品」。
  const workTab = $('writeNavWork');
  if (workTab) workTab.title = title || '作品';
  const total = $('writeWorkspaceTotal');
  if (total) total.textContent = S.works.length ? `${S.works.length} 本` : '';
  $('writeWorkspaceCount').textContent = S.works.length
    ? `${S.works.length} 本` : '';
  $('writeWorkspaceSearchWrap').hidden = S.works.length <= 5;
  list.textContent = '';
  const query = (S.workspaceFilter || '').trim().toLowerCase();
  // 有作品才需要这个固定项：一本都没有时本来就在空项目里。
  if (!query && S.works.length) list.appendChild(emptyProjectOption());
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

/** 下拉里的固定项：空项目 = 不绑定作品，助手不带入任何一本的设定。 */
function emptyProjectOption() {
  const selected = !S.workId;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'write-workspace-item write-workspace-scratch';
  btn.setAttribute('role', 'option');
  btn.setAttribute('aria-selected', selected ? 'true' : 'false');
  btn.dataset.emptyProject = '1';
  btn.title = '不绑定作品：助手只按对话内容回答，不注入某一本的设定与正文';
  btn.appendChild(treeIcon('sparkles', 'write-workspace-item-icon'));
  const main = el('span', 'write-workspace-item-main');
  main.append(
    el('span', 'write-workspace-item-title', '空项目'),
    el('span', 'write-workspace-item-meta', '不绑定作品，先聊想法'));
  btn.appendChild(main);
  const check = el('span', 'write-workspace-item-check');
  check.innerHTML = '<i data-lucide="check"></i>';
  btn.appendChild(check);
  btn.onclick = () => chooseEmptyProject();
  btn.onkeydown = (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    const items = [...$('writeWorkspaceList')
      .querySelectorAll('.write-workspace-item')];
    const next = items[items.indexOf(event.currentTarget)
      + (event.key === 'ArrowDown' ? 1 : -1)];
    if (next) next.focus();
    else $('writeWorkspaceBtn').focus();
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
  if (workId !== S.workId) {
    endWritingSession();
    await selectWork(workId);
  }
  const active = $('writeChapterList').querySelector('.write-tree-chapter.active')
    || $('writeChapterList').querySelector('.write-tree-root');
  if (active) active.focus();
}

async function chooseEmptyProject() {
  closeWorkspaceMenu();
  if (S.emptyProject && !S.workId) return;
  endWritingSession();
  await selectEmptyProject();
  const active = $('writeChapterList').querySelector('.write-tree-root');
  if (active) active.focus();
}

/**
 * 换作品 = 换会话线：旧对话留在原作品名下，新作品从一句新话开始。
 * 阅读页换书也是这么做的（决策 84「换书重开」）；旧对话随时能从
 * 会话面板的作品组里点回来。
 */
function endWritingSession() {
  if (window.clearTabSession) window.clearTabSession('write');
  // S 是写作台自己的状态；聊天区的当前会话在 window.S（state.js）上。
  if (window.S && window.S.currentSessionId && window.resetChatView) {
    window.resetChatView();
  }
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
    $('writeNavWork').focus();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function deleteWork(work) {
  if (!window.confirm(
    `删除《${work.title}》？\n\n`
    + '章节、版本、存档记录与分支都会一起删除，且不可恢复。')) {
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
  syncSidePanels();
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
    // 空项目：没有作品树，素材库就是主要工作对象。
    box.appendChild(libraryNode());
    renderLibraryTree();
    if (!(S.draft && S.draft.kind === 'work')) {
      box.appendChild(worksEmptyState());
    }
    return;
  }
  if (S.section !== 'work') return;   // 版本控制整栏交给 VCS 面板
  box.appendChild(folderRow('正文', {
    collapsed: S.rootCollapsed,
    meta: chapterMeta(),
    actions: [
      treeAction('file-text', '合同总览', (event) => {
        event.stopPropagation();
        openContracts();
      }),
      treeAction('plus', '新建章节', (event) => {
        event.stopPropagation();
        createChapter();
      }),
    ],
    onToggle: (row) => {
      const open = S.rootCollapsed;
      S.rootCollapsed = !S.rootCollapsed;
      writeFlag('bc.writeRootCollapsed', S.rootCollapsed);
      foldTree(row, 'root', open, renderChapters);
    },
  }));
  applyRenameRetry(box);
  if (!S.rootCollapsed) {
    box.appendChild(chapterChildren());
    // 空态提示放在子级容器外，免得把「正文」的缩进参考线拉长。
    if (!S.chapters.length && !(S.draft && S.draft.kind === 'chapter')) {
      box.appendChild(treeHint('还没有章节。点「正文」右侧的 + 新建一章。'));
    }
  }
  box.appendChild(workspaceNode());
  box.appendChild(libraryNode());
  renderDocsTree();
  renderLibraryTree();
  applyRenameRetry(box);
}

function chapterMeta() {
  const words = S.chapters.reduce((n, c) => n + (c.word_count || 0), 0);
  return `${S.chapters.length} 章` + (words ? ` · ${words} 字` : '');
}

function chapterChildren() {
  const rows = S.chapters;
  const children = treeChildren('root', foldGuide(0, 14));
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
  if (S.draft && S.draft.kind === 'chapter') {
    const chapterDraft = chapterDraftRow();
    children.appendChild(chapterDraft);
    focusDraft(chapterDraft);
  }
  return children;
}

function workspaceNode() {
  const count = (S.docs || []).length;
  const wrapper = el('div', 'write-tree-group');
  wrapper.appendChild(folderRow('工作区', {
    collapsed: S.workspaceCollapsed,
    meta: count ? `${count} 份` : '',
    actions: [treeAction('refresh-cw', '刷新工作区', (event) => {
      event.stopPropagation();
      loadDocs();
    })],
    onToggle: (row) => {
      const open = S.workspaceCollapsed;
      S.workspaceCollapsed = !S.workspaceCollapsed;
      writeFlag('bc.writeWorkspaceCollapsed', S.workspaceCollapsed);
      foldTree(row, 'workspace', open, renderChapters);
    },
  }));
  if (!S.workspaceCollapsed) {
    const kids = treeChildren('workspace', foldGuide(0, 12));
    kids.id = 'writeDocsTree';
    wrapper.appendChild(kids);
    // 空态提示挂在子级容器外：它在容器里会把缩进参考线拖长。
    wrapper.appendChild(treeNoteHost('writeDocsHint'));
  }
  return wrapper;
}

/** 空态提示的宿主：跟子级容器平级，不参与参考线的长度。 */
function treeNoteHost(id) {
  const host = el('div', 'write-tree-note');
  host.id = id;
  return host;
}

/**
 * 全局素材库：跟作品工作区并列的一棵树，但不属于任何一本书。
 * 空项目时它就是左栏的主角；绑定作品后仍留一个固定入口（否则只进不出）。
 */
function libraryNode() {
  const count = (S.library || []).length;
  const wrapper = el('div', 'write-tree-group');
  wrapper.appendChild(folderRow('素材库', {
    collapsed: S.libraryCollapsed,
    meta: count ? `${count} 张` : '',
    actions: [treeAction('refresh-cw', '刷新素材库', (event) => {
      event.stopPropagation();
      loadLibrary();
    })],
    onToggle: (row) => {
      const open = S.libraryCollapsed;
      S.libraryCollapsed = !S.libraryCollapsed;
      writeFlag('bc.writeLibraryCollapsed', S.libraryCollapsed);
      foldTree(row, 'library', open, renderChapters);
    },
  }));
  if (!S.libraryCollapsed) {
    const kids = treeChildren('library', foldGuide(0, 12));
    kids.id = 'writeLibraryTree';
    wrapper.appendChild(kids);
    wrapper.appendChild(treeNoteHost('writeLibraryHint'));
  }
  return wrapper;
}

function folderRow(label, { collapsed, meta = '', actions = [], onToggle }) {
  const row = el('div', 'write-tree-row write-tree-root');
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  row.appendChild(twistyButton(collapsed, () => onToggle(row)));
  row.appendChild(treeIcon(collapsed ? 'folder' : 'folder-open'));
  row.appendChild(el('span', 'write-tree-label', label));
  if (meta) row.appendChild(el('span', 'write-tree-meta', meta));
  const box = el('span', 'write-tree-actions');
  actions.forEach((action) => box.appendChild(action));
  row.appendChild(box);
  row.onclick = () => onToggle(row);
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onToggle(row);
    } else if (event.key === 'ArrowRight' && collapsed) {
      event.preventDefault();
      onToggle(row);
    } else if (event.key === 'ArrowLeft' && !collapsed) {
      event.preventDefault();
      onToggle(row);
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
    const open = collapsed;
    if (open) S.collapsedVolumes.delete(volume.id);
    else S.collapsedVolumes.add(volume.id);
    foldTree(row, `volume:${volume.id}`, open, renderChapters);
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
    const kids = treeChildren(`volume:${volume.id}`, foldGuide(1, 14));
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
      foldTree(row, `chapter:${chapter.id}`, !expanded, renderChapters);
    }));
  } else {
    row.appendChild(treeSpacer());
  }
  row.appendChild(treeIcon('file-text'));
  const label = el('span', 'write-tree-label', chapterTreeLabel(chapter));
  row.appendChild(label);
  row.appendChild(statusDot(chapter.status));
  const contractDot = contractBadge(chapter);
  if (contractDot) row.appendChild(contractDot);
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
      foldTree(row, `chapter:${chapter.id}`, true, renderChapters);
    } else if (event.key === 'ArrowLeft' && scenes.length && expanded) {
      event.preventDefault();
      S.expandedChapters.delete(chapter.id);
      foldTree(row, `chapter:${chapter.id}`, false, renderChapters);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveRowFocus(row, event.key === 'ArrowDown' ? 1 : -1);
    }
  };
  wrapper.appendChild(row);
  if (scenes.length && expanded) {
    const kids = treeChildren(`chapter:${chapter.id}`, foldGuide(depth, 14));
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

/** 切导航：作品（项目浏览器）与版本控制。 */
function setSection(name) {
  if (!SECTIONS.includes(name)) name = 'work';
  const changing = name !== S.section;
  S.section = name;
  if (name === 'vcs' && !TEXT_VIEWS.has(S.view) && S.view !== 'diff') {
    // 版本控制是左栏的模式：主区照旧停在正文。
    setView(S.textView || 'editor');
  }
  // 差异是从版本控制点进来的一屏：切回作品就是切回正文，不用先按「返回」。
  if (name === 'work' && S.view === 'diff') setView(diffReturnView());
  // 时间线属于项目浏览器；切到版本控制时收起来（对应 VS Code 换视图）。
  if (name === 'vcs') closeTimeline();
  renderNav();
  renderChapters();
  if (name === 'vcs' && (changing || !S.vcs)) loadVcs();
  if (name === 'work' && changing) loadDocs();
}

function renderNav() {
  const workBtn = $('writeNavWork');
  const vcsBtn = $('writeNavVcs');
  // 空项目：素材库不进写作 git，版本控制没有对象。导航药丸和头部
  // 分支徽章一起收起；若当前停在版本控制，先回项目浏览器。
  const noWork = !S.workId;
  if (noWork && S.section === 'vcs') S.section = 'work';
  if (vcsBtn) vcsBtn.hidden = noWork;
  const badge = $('writeVcsBadge');
  if (badge) badge.hidden = noWork;
  if (workBtn) {
    workBtn.classList.toggle('active', S.section === 'work');
    workBtn.setAttribute('aria-selected',
                         S.section === 'work' ? 'true' : 'false');
  }
  if (vcsBtn) {
    vcsBtn.classList.toggle('active', !noWork && S.section === 'vcs');
    vcsBtn.setAttribute('aria-selected',
                        !noWork && S.section === 'vcs' ? 'true' : 'false');
  }
}

/**
 * 版本控制是左栏的另一种模式：切过去时树让位，整栏交给源代码管理面板。
 * 作品（项目浏览器）下，正文与工作区共用同一棵树。
 */
function syncSidePanels() {
  const vcs = $('writeVcsRail');
  const showVcs = S.section === 'vcs' && !!S.workId;
  if (vcs) vcs.hidden = !showVcs;
  const rail = $('writeRail');
  if (rail) rail.classList.toggle('is-vcs', showVcs);
}

function worksEmptyState() {
  const box = el('div', 'write-tree-empty');
  const hasWorks = S.works.length > 0;
  const title = document.createElement('strong');
  title.textContent = hasWorks ? '空项目' : '还没有作品';
  box.appendChild(title);
  box.appendChild(el('span', '', hasWorks
    ? '不绑定任何作品。直接跟助手聊；要写正文再从左上角选一本。'
    : '新建一本，然后建第一章。'));
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'write-btn primary';
  btn.textContent = '新建作品';
  btn.onclick = createWork;
  box.appendChild(btn);
  if (hasWorks) {
    const pick = document.createElement('button');
    pick.type = 'button';
    pick.className = 'write-btn';
    pick.textContent = '选择作品';
    // 这个按钮长在树里、不在 .write-workspace-bar 内。不拦一下，同一次点击
    // 冒泡到 document 时会被「点面板外面就收起」的监听判成外面，菜单
    // 刚开就关，看起来像点了没反应。
    pick.onclick = (event) => {
      event.stopPropagation();
      openWorkspaceMenu();
    };
    box.appendChild(pick);
  }
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

function contractBadge(chapter) {
  if (!S.contractByChapter) return null;
  const state = S.contractByChapter[chapter.chapter_index];
  const dot = el('span', 'write-contract-dot'
    + (state === 'closed' ? ' closed' : state ? ' active' : ' missing'));
  dot.title = state === 'closed' ? '合同已结清'
    : state ? '有合同' : '缺合同';
  return dot;
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

// ── 文件夹开合过渡 ────────────────────────────────────────────────
// 树是整棵重绘的：点一下就把容器内容全换掉，元素不跨帧存活，所以
// 不能靠 CSS class 过渡。展开时先让新的子级容器以 0 高度入场，下一
// 帧再拉到实际高度；收起时趁旧元素还在先播完，再把重绘交给回调。
// 一次点击只影响一个文件夹，待展开的 key 只记一个。

const TREE_FOLD_MS = 160;
let pendingFoldKey = null;

function reducedMotion() {
  return !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

/** 缩进参考线对齐文件夹的折叠箭头中心：行左内边距 6px + 箭头半宽 7px。 */
function foldGuide(depth, step) {
  return `${13 + depth * step}px`;
}

function treeChildren(key, guideLeft) {
  const kids = el('div', 'write-tree-children');
  if (guideLeft) kids.style.setProperty('--guide-left', guideLeft);
  if (!key) return kids;
  if (pendingFoldKey === key) {
    pendingFoldKey = null;
    kids.style.height = '0px';
    kids.style.overflow = 'hidden';
    requestAnimationFrame(() => playFoldOpen(kids));
  }
  return kids;
}

function playFoldOpen(kids) {
  const clear = () => {
    kids.style.height = '';
    kids.style.overflow = '';
  };
  if (!kids.isConnected || reducedMotion()) { clear(); return; }
  // 占位图标还没落成 svg 时高度会偏，先刷一次再量。
  if (window.refreshIcons) window.refreshIcons();
  kids.style.height = '';
  const height = kids.getBoundingClientRect().height;
  if (!height) { clear(); return; }
  kids.style.height = '0px';
  const anim = kids.animate(
    [{ height: '0px', opacity: 0 }, { height: `${height}px`, opacity: 1 }],
    { duration: TREE_FOLD_MS, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' });
  anim.onfinish = clear;
  anim.oncancel = clear;
}

function playFoldClose(kids, done) {
  if (reducedMotion()) { done(); return; }
  const height = kids.getBoundingClientRect().height;
  if (!height) { done(); return; }
  kids.style.overflow = 'hidden';
  const anim = kids.animate(
    [{ height: `${height}px`, opacity: 1 }, { height: '0px', opacity: 0 }],
    { duration: TREE_FOLD_MS, easing: 'cubic-bezier(0.4, 0, 1, 1)' });
  let fired = false;
  const finish = () => {
    if (fired) return;
    fired = true;
    done();
  };
  anim.onfinish = finish;
  anim.oncancel = finish;
  setTimeout(finish, TREE_FOLD_MS + 80);   // 动画事件失约时别把树卡住
}

/**
 * 文件夹行开合的统一入口。row 是文件夹那一行，open 是「这次点完
 * 是否展开」——展开时记下 key，重绘后由新的子级容器认领；收起时
 * 旧子级容器还在，直接对它播退场。
 */
function foldTree(row, key, open, render) {
  if (open) {
    pendingFoldKey = key;
    // 空文件夹没有子级容器认领，重绘完就作废（也别把异常留成脏状态）
    try { render(); } finally { pendingFoldKey = null; }
    return;
  }
  const kids = row && row.nextElementSibling;
  if (kids && kids.classList.contains('write-tree-children')) {
    playFoldClose(kids, render);
  } else {
    render();
  }
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

/** 换书或切空项目：上一本的章节、工作区、版本现场全部作废。 */
function resetWorkScene() {
  S.chapters = [];
  S.volumes = [];
  S.expandedChapters.clear();
  S.collapsedVolumes.clear();
  // 换一本就回到项目浏览器：版本控制是上一本的现场，别把导航带过来。
  S.section = 'work';
  S.contractRows = null;
  S.contractByChapter = null;
  S.vcs = null;                 // 上一本的状态不能拿来给这一本的文件画徽章
  S.vcsLog = [];
  S.vcsExpand = {};
  S.vcsOpening = '';
  S.commitFiles = {};
  S.docs = [];
  S.docDirs = [];
  S.docPath = '';
  S.docRow = null;
  S.docsCollapsed.clear();
  S.docsDraft = null;
  // 素材库跨作品保留，但打开的那张卡属于上一屏，切作品时放下。
  S.libPath = '';
  S.libRow = null;
  S.libEditing = false;
  closeTimeline();
  if (!TEXT_VIEWS.has(S.view)) setView(S.textView || 'editor');
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
  S.emptyProject = false;
  writeFlag('bc.writeEmptyProject', false);
  const work = S.works.find((x) => x.id === workId);
  S.workTitle = work ? work.title : '';
  resetWorkScene();
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
    await Promise.all([loadDocs(), loadContracts(), loadLibrary()]);
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

/**
 * 空项目：不绑定任何作品。
 *
 * 对话照常走写作模式，但服务端不再注入某一本的章节/设定/工作区/版本现场
 * （以前 agent_prompt 会回退到第一本，想拿到干净上下文只能新建一本书）。
 * 助手这一轮建了书，refresh() 会把新书选上并退出空项目。
 */
async function selectEmptyProject({ quiet = false } = {}) {
  if (!(await flush())) {
    setStatus('当前作品还有未保存的正文，保存失败，已取消切换', 'error');
    return;
  }
  clearChapter();
  S.workId = '';
  S.workTitle = '';
  S.emptyProject = true;
  writeFlag('bc.writeEmptyProject', true);
  resetWorkScene();
  S.libPath = '';
  S.libRow = null;
  await loadLibrary();
  renderWorks();
  // 空项目没有正文可写，主区先说明素材库是什么；点卡片再切到卡。
  if (!S.libPath) {
    setView('draft');
    renderLibraryEmpty();
  }
  if (!quiet) setStatus('空项目：助手不再代入任何作品的设定与正文', 'ok');
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

function renderStyleBar(onText = TEXT_VIEWS.has(S.view)) {
  const bar = $('writeStyleBar');
  const select = $('writeStyleSelect');
  if (!bar || !select) return;
  if (!onText) {   // 风格条只属于正文，别漏到草稿/合同/diff 上
    bar.hidden = true;
    return;
  }
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
    openReview();
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
    // 从设定/合同视图点回章节：主区要切回正文，否则编辑器被盖住。
    if (!TEXT_VIEWS.has(S.view)) setView(S.textView || 'editor');
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
    // 引用条跟着章走：换章之后旧的引用已经没有锚点了。
    if (typeof window.clearWritingSelection === 'function') {
      window.clearWritingSelection();
    }
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

/** 右侧只剩审稿一块：面板只有开/关两种状态。 */
function openReview() {
  $('writeSide').classList.add('open');
  loadReview();
}

/**
 * 只留一块主区视图（diff / merge 覆盖层也走这里，省得逐块列 hidden）。
 * 头部（章标题/撤销/保存/生成）与风格条跟着**视图**走，不跟分区走：
 * 版本控制停在左栏时主区还是正文，这两个东西得留着。
 */
function showOnlyView(name) {
  Object.entries(MAIN_VIEWS).forEach(([key, id]) => {
    const node = $(id);
    if (node) node.hidden = key !== name;
  });
  const onText = TEXT_VIEWS.has(name);
  $('writeMainHead').hidden = !onText;
  renderStyleBar(onText);
}

function setView(name) {
  S.view = name;
  if (TEXT_VIEWS.has(name)) S.textView = name;
  document.querySelectorAll('.write-view-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === name);
  });
  showOnlyView(name);
  // 正文被切走，镜像没有可对齐的对象了（引用状态本身不受影响）；
  // 切回来时若选区还在，把它重新画上。
  if (name !== 'editor') {
    hideQuoteVeil();
  } else {
    const ed = $('writeEditor');
    if (ed && ed.selectionEnd > ed.selectionStart) {
      showQuoteVeil(ed.selectionStart, ed.selectionEnd);
    }
  }
  if (name === 'map') loadMap();
  if (name === 'curve') loadCurve();
}

/** 差异屏退出后回到哪一屏：此刻若不在差异上就是它自己，否则回进来之前那一屏。 */
function diffReturnView() {
  const back = (S.view && S.view !== 'diff') ? S.view : S.diffReturn;
  return (back && back !== 'diff' && MAIN_VIEWS[back])
    ? back : (S.textView || 'editor');
}

// ── 合同收录（左栏「合同」）──────────────────────────────────────────
// 合同是硬边界：这一章要做什么、不能做什么。写合同的入口只在写作助手
// （writing_set_contract），这里只负责收拢与跳转，不开第二套手写表单。

const CONTRACT_FIELDS = [
  ['arc_ref', '弧线'],
  ['purpose', '目的'],
  ['exclusive_event', '独占事件'],
  ['ending_state', '章末状态'],
  ['next_entry_state', '下场可承接'],
  ['story_time', '故事时间'],
];

const CONTRACT_LISTS = [
  ['must_hit_now', '必须兑现'],
  ['must_preserve', '必须保留'],
  ['forbidden_cross', '禁止触碰'],
];

function openContracts() {
  loadContracts();
  setView('contract');
}

async function loadContracts() {
  if (!S.workId) return;
  const host = $('writeContractList');
  if (!host) return;
  host.textContent = '';
  host.appendChild(el('p', 'write-vcs-empty', '正在读合同…'));
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/contracts`);
    S.contractRows = data.contracts || [];
    renderContracts();
  } catch (err) {
    host.textContent = '';
    host.appendChild(el('p', 'write-vcs-empty', err.message));
  }
}

function renderContracts() {
  const host = $('writeContractList');
  if (!host) return;
  const rows = S.contractRows || [];
  S.contractByChapter = {};
  rows.forEach((row) => {
    S.contractByChapter[row.chapter_index] = row.status || 'active';
  });
  renderChapters();
  $('writeContractCount').textContent = rows.length ? `${rows.length} 章` : '';
  host.textContent = '';
  if (!rows.length) {
    const empty = el('div', 'write-placeholder');
    empty.appendChild(el('strong', '', '还没有合同'));
    empty.appendChild(el('p', '',
      '合同由写作助手在起草前写下：这一章要做什么、不能做什么、'
      + '哪些事必须在这一章兑现。缺了合同，生成正文会被挡回来。'));
    host.appendChild(empty);
    return;
  }
  rows.forEach((row) => host.appendChild(contractCard(row)));
}

function contractCard(row) {
  // 「打开这一章」= 切回正文分区再载入这一章。刷新（refresh）不走这里，
  // 所以助手跑完一轮不会把正在看版本控制的读者弹回编辑器。
  const openChapter = () => {
    setSection('work');
    setView('editor');
    selectChapter(row.chapter_index);
  };
  const card = el('article', 'write-contract');
  card.tabIndex = 0;
  card.onclick = openChapter;
  card.onkeydown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openChapter();
    }
  };

  const closed = String(row.status || '') === 'closed';
  const outcomes = Array.isArray(row.outcomes) ? row.outcomes : [];
  const byItem = new Map(outcomes.map((o) => [String(o.item || ''), o]));

  const head = el('div', 'write-contract-card-head');
  head.appendChild(el('span', 'write-contract-chapter',
    `第${row.chapter_index}章 ${row.chapter_title || '未命名'}`));
  head.appendChild(el('span',
    'write-contract-state' + (closed ? ' closed' : ''),
    closed ? '已结清' : '在写'));
  const bits = [];
  if (row.word_count) bits.push(`${row.word_count} 字`);
  if (row.target_chars) bits.push(`目标 ${row.target_chars}`);
  head.appendChild(el('span', 'write-contract-meta', bits.join(' · ')));
  const jump = el('span', 'write-contract-jump', '打开这一章 ›');
  head.appendChild(jump);
  card.appendChild(head);

  const body = el('div', 'write-contract-body');
  CONTRACT_FIELDS.forEach(([key, label]) => {
    const value = String(row[key] || '').trim();
    if (!value) return;
    const line = el('div', 'write-contract-field');
    line.appendChild(el('span', 'write-contract-key', label));
    line.appendChild(el('span', 'write-contract-value', value));
    body.appendChild(line);
  });
  CONTRACT_LISTS.forEach(([key, label]) => {
    const values = Array.isArray(row[key]) ? row[key] : [];
    if (!values.length) return;
    const line = el('div', 'write-contract-field');
    line.appendChild(el('span', 'write-contract-key', label));
    const list = el('ul', 'write-contract-items');
    values.forEach((v) => {
      const li = el('li', '', String(v));
      // 必须兑现的条目结算时逐条勾：已兑现 / 勉强 / 未兑现，未申报的不加标记。
      const outcome = key === 'must_hit_now' ? byItem.get(String(v)) : null;
      if (outcome) {
        const verdict = String(outcome.verdict || '');
        li.classList.add('write-contract-outcome', verdict);
        const text = { hit: '已兑现', partial: '勉强', miss: '未兑现' }[verdict];
        if (text) li.prepend(el('span', 'write-contract-verdict', text));
      }
      list.appendChild(li);
    });
    line.appendChild(list);
    body.appendChild(line);
  });
  const closeNote = String(row.close_note || '').trim();
  if (closeNote) {
    const line = el('div', 'write-contract-field');
    line.appendChild(el('span', 'write-contract-key', '结清备注'));
    line.appendChild(el('span', 'write-contract-value', closeNote));
    body.appendChild(line);
  }
  if (!body.childNodes.length) {
    body.appendChild(el('p', 'write-vcs-empty', '这份合同只有骨架，还没写细节。'));
  }
  card.appendChild(body);
  return card;
}

// ── 草稿：作品工作区（作品记忆 / 设定 / 计划稿 / 素材）────────────────
// 文件是写作助手维护的普通 Markdown（data/writing/works/<id>/），这里只读：
// 左栏给目录，主区给正文。要改就说给助手听，页面不开第二套编辑入口。

async function loadDocs() {
  if (!S.workId) return;
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/docs`);
    S.docs = data.files || [];
    S.docDirs = data.dirs || [];
    // 当前打开的那份可能刚被助手删掉：留着路径只会显示一片空。
    if (S.docPath && !S.docs.some((f) => f.path === S.docPath)) {
      S.docPath = '';
      S.docRow = null;
    }
    renderDocsTree();
    if (S.docPath) await openDoc(S.docPath, { quiet: true });
    else renderDocEmpty();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

// ── agent 输出里的文件链接（只在写作页）────────────────────────────
// renderMarkdown 只负责把像路径的 .md 标成链接；真实位置在这里解析：
// 工作区文档 → 素材卡 → 章节。路径可能是助手刚落的，先按现场找，找不到再刷新。

function onWriteFileLinkClick(event) {
  if (window._currentTab !== 'write') return;
  const link = event.target.closest('a.write-file-link');
  if (!link) return;
  event.preventDefault();
  event.stopPropagation();
  openWriteFileLink(link.dataset.writePath || '');
}

function findOpenablePath(path) {
  const exact = (rows) => (rows || []).find((row) => row.path === path);
  const doc = exact(S.docs);
  if (doc) return { kind: 'doc', path: doc.path };
  const card = exact(S.library);
  if (card) return { kind: 'library', path: card.path };
  const base = path.split('/').pop();
  if (!base) return null;
  // 只给文件名时唯一命中才开；重名宁愿报错，也不猜作者想开哪份。
  const docs = (S.docs || []).filter((row) => row.path === base
    || String(row.path || '').endsWith(`/${base}`));
  if (docs.length === 1) return { kind: 'doc', path: docs[0].path };
  const cards = (S.library || []).filter((row) => row.path === base
    || String(row.path || '').endsWith(`/${base}`));
  if (cards.length === 1) return { kind: 'library', path: cards[0].path };
  return null;
}

async function openWriteFileLink(raw) {
  const path = String(raw || '').trim()
    .replace(/^\.\//, '')
    .replace(/^工作区\//, '')
    .replace(/\\/g, '/');
  if (!path) return;
  let hit = findOpenablePath(path);
  if (!hit) {
    // 助手这一轮可能刚创建/改名：对齐一次现场再认。
    await Promise.all([loadDocs(), loadLibrary()]);
    hit = findOpenablePath(path);
  }
  if (!hit) {
    const chapter = path.match(/第\s*(\d+)\s*章/);
    const index = chapter ? Number(chapter[1]) : 0;
    if (index && (S.chapters || []).some(
      (row) => row.chapter_index === index)) {
      await selectChapter(index);
      return;
    }
    setStatus(`找不到文件：${path}`, 'error');
    return;
  }
  setView('draft');
  if (hit.kind === 'library') await openLibraryCard(hit.path);
  else await openDoc(hit.path);
}

async function openDoc(path, options = {}) {
  if (!S.workId || !path) return;
  try {
    const row = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/docs/file?path=`
      + encodeURIComponent(path));
    S.libPath = '';
    S.libRow = null;
    S.libEditing = false;
    S.docPath = row.path || path;
    S.docRow = row;
    renderDoc();
    renderDocsTree();
  } catch (err) {
    if (options.quiet) {
      S.docPath = '';
      S.docRow = null;
      renderDocEmpty();
    }
    setStatus(err.message, 'error');
  }
}

function docLabel(path) {
  // 页面上不出现文件后缀：作者只需要知道这是「作品记忆」还是「人物A」。
  return String(path || '').replace(/\.md$/i, '');
}

function docSize(size) {
  const n = Number(size) || 0;
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}

// ── 文件区的 git 状态徽章（VS Code 式 M / U / D）───────────────────
// 状态由后端 vcs_status 给出（与版本控制栏同一份数据）。写作 git 没有
// 暂存区：一次提交是整份工作区快照（决策 127），所以不存在 A（已加索引），
// 新文件一律是 U（未跟踪）。

const DOC_STATUS_LABELS = { M: '已修改', U: '未跟踪', D: '已删除' };
const DOC_STATUS_FALLBACK = { changed: 'M', added: 'U', removed: 'D' };
// 胶囊只做悬停说明：讲清楚这个状态对这份文件意味着什么。
const DOC_STATUS_TIPS = {
  M: '与上次存档的内容不同，还没有存档',
  U: '新文件，还没有进过任何存档',
  D: '已从工作区删除，还没有存档',
};

function docStatusMap() {
  const map = new Map();
  const changes = (S.vcs && S.vcs.file_changes) || {};
  ['changed', 'added', 'removed'].forEach((op) => {
    (changes[op] || []).forEach((row) => {
      if (!row || !row.path) return;
      const letter = row.status || DOC_STATUS_FALLBACK[op];
      map.set(row.path, {
        letter,
        label: row.status_label || DOC_STATUS_LABELS[letter] || '',
        tip: DOC_STATUS_TIPS[letter] || '',
      });
    });
  });
  return map;
}

/** 文件的 git 状态；没改动或还不知道状态时返回 null。 */
function docStatusOf(path) {
  return docStatusMap().get(path) || null;
}

function docStatusBadge(info) {
  const badge = el('span', 'write-doc-badge');
  applyDocBadgeInfo(badge, info);
  // 只做悬停说明：点在胶囊上不打开文件，点击本身不给任何反馈。
  badge.addEventListener('click', (event) => event.stopPropagation());
  return badge;
}

function applyDocBadgeInfo(badge, info) {
  badge.textContent = info.label;
  badge.dataset.status = info.letter;
  badge.title = info.tip ? `${info.label}：${info.tip}` : info.label;
  badge.setAttribute('aria-label', badge.title);
}

/** 只改徽章，不重建行：树里可能有行内改名/新建输入框。 */
function applyDocStatusBadges() {
  const box = $('writeDocsTree');
  if (!box) return;
  const map = docStatusMap();
  box.querySelectorAll('.write-doc-file').forEach((row) => {
    const path = row.dataset.path;
    if (!path || row.classList.contains('is-draft')) return;
    const info = map.get(path);
    let badge = row.querySelector('.write-doc-badge');
    if (!info) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = docStatusBadge(info);
      row.appendChild(badge);
    } else {
      applyDocBadgeInfo(badge, info);
    }
  });
  // 文件夹只挂一个点：里面有未提交的改动（VS Code 的 Explorer 也这样染色）。
  const dirtyDirs = new Set();
  map.forEach((_info, path) => {
    const parts = String(path).split('/');
    for (let i = 1; i < parts.length; i += 1) {
      dirtyDirs.add(parts.slice(0, i).join('/'));
    }
  });
  box.querySelectorAll('.write-doc-dir').forEach((row) => {
    row.classList.toggle('has-changes', dirtyDirs.has(row.dataset.path));
  });
}

function renderDoc() {
  const row = S.docRow;
  const body = $('writeDocBody');
  if (!row || !body) return;
  const edit = $('writeDocEditBtn');
  if (edit) edit.hidden = true;
  if ($('writeDocAddBtn')) $('writeDocAddBtn').hidden = true;
  $('writeDocPath').textContent = docLabel(row.path);
  const bits = [docSize(row.size)];
  if (row.modified) bits.push(whenText(row.modified));
  const status = docStatusOf(row.path);
  if (status) bits.push(status.label);
  $('writeDocMeta').textContent = bits.join(' · ');
  body.innerHTML = renderDocMarkdown(row.content || '')
    || '<p class="write-docs-empty">这份文件还是空的。</p>';
  decorateMaterialHead(body);
  body.scrollTop = 0;
}

function renderDocEmpty() {
  S.docRow = null;
  const body = $('writeDocBody');
  if (!body) return;
  const edit = $('writeDocEditBtn');
  if (edit) edit.hidden = true;
  if ($('writeDocAddBtn')) $('writeDocAddBtn').hidden = true;
  if ($('writeDocPath')) $('writeDocPath').textContent = '作品工作区';
  if ($('writeDocMeta')) $('writeDocMeta').textContent = '';
  body.textContent = '';
  const box = el('div', 'write-placeholder');
  box.appendChild(el('strong', '', '作品工作区'));
  box.appendChild(el('p', '',
    '左栏是这本书的工作区：作品记忆、设定、计划稿、素材。'
    + '写作助手会在对话里维护它们——点一份文件在这里读。'));
  body.appendChild(box);
}

function renderDocsTree() {
  const box = $('writeDocsTree');
  if (!box) return;
  box.textContent = '';
  const files = S.docs || [];
  const tree = { dirs: new Map(), files: [] };
  const ensureDir = (parts) => {
    let node = tree;
    for (let i = 0; i < parts.length; i += 1) {
      const name = parts[i];
      if (!node.dirs.has(name)) {
        node.dirs.set(name, {
          name, path: parts.slice(0, i + 1).join('/'),
          dirs: new Map(), files: [],
        });
      }
      node = node.dirs.get(name);
    }
    return node;
  };
  // 空目录也要占位：作者打开草稿就该看见「设定」「计划稿」「素材」在哪。
  (S.docDirs || []).forEach((dir) => {
    const parts = String(dir.path || '').split('/').filter(Boolean);
    if (parts.length) ensureDir(parts);
  });
  files.forEach((file) => {
    const parts = String(file.path || '').split('/').filter(Boolean);
    if (!parts.length) return;
    const node = ensureDir(parts.slice(0, -1));
    node.files.push({ ...file, name: parts[parts.length - 1] });
  });
  const walk = (node, depth, parent) => {
    node.files.forEach((file) => parent.appendChild(docFileRow(file, depth)));
    node.dirs.forEach((dir) => {
      parent.appendChild(docDirRow(dir, depth));
      if (S.docsCollapsed.has(dir.path)) return;
      const kids = treeChildren(`docs:${dir.path}`, foldGuide(depth, 12));
      parent.appendChild(kids);
      walk(dir, depth + 1, kids);
      if (S.docsDraft && S.docsDraft.parent === dir.path) {
        kids.appendChild(docDraftRow(depth + 1));
      }
      if (!kids.childElementCount) kids.remove();
    });
  };
  walk(tree, 1, box);
  const hint = $('writeDocsHint');
  if (hint) {
    hint.textContent = '';
    if (!files.length) {
      hint.appendChild(el('p', 'write-docs-empty',
        '工作区还是空的。让助手记一条设定，或收一份书外素材。'));
    }
  }
  applyDocStatusBadges();
}

function docCount(dir) {
  let n = dir.files.length;
  dir.dirs.forEach((child) => { n += docCount(child); });
  return n;
}

function docDirRow(dir, depth) {
  const collapsed = S.docsCollapsed.has(dir.path);
  const row = el('div', 'write-tree-row write-doc-dir');
  row.style.setProperty('--tree-indent', `${depth * 12}px`);
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  row.dataset.path = dir.path;
  row.dataset.kind = 'dir';
  const toggle = () => {
    const open = collapsed;
    if (open) S.docsCollapsed.delete(dir.path);
    else S.docsCollapsed.add(dir.path);
    foldTree(row, `docs:${dir.path}`, open, renderDocsTree);
  };
  row.appendChild(twistyButton(collapsed, toggle));
  row.appendChild(treeIcon(collapsed ? 'folder' : 'folder-open'));
  row.appendChild(el('span', 'write-tree-label', dir.name));
  const count = docCount(dir);
  if (count) row.appendChild(el('span', 'write-tree-meta', String(count)));
  row.onclick = toggle;
  row.oncontextmenu = (event) => {
    if (event.target.closest('input, textarea')) return;
    event.preventDefault();
    event.stopPropagation();
    row.focus();
    openDocContextMenu(event.clientX, event.clientY, {
      path: dir.path, kind: 'dir', node: row,
    });
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'ContextMenu'
        || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      openDocContextMenu(rect.left + 24, rect.bottom - 2, {
        path: dir.path, kind: 'dir', node: row,
      });
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggle();
    }
  };
  return row;
}

function docFileRow(file, depth) {
  const row = el('div', 'write-tree-row write-doc-file');
  row.style.setProperty('--tree-indent', `${depth * 12}px`);
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  if (file.path === S.docPath) {
    row.classList.add('active');
    row.setAttribute('aria-current', 'true');
  }
  row.appendChild(treeSpacer());
  row.appendChild(treeIcon('file-text'));
  row.appendChild(el('span', 'write-tree-label', docLabel(file.name)));
  row.title = docLabel(file.path);
  row.dataset.path = file.path;
  row.dataset.kind = 'file';
  row.onclick = () => { setView('draft'); openDoc(file.path); };
  row.oncontextmenu = (event) => {
    if (event.target.closest('input, textarea')) return;
    event.preventDefault();
    event.stopPropagation();
    row.focus();
    openDocContextMenu(event.clientX, event.clientY, {
      path: file.path, kind: 'file', node: row,
    });
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'ContextMenu'
        || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      openDocContextMenu(rect.left + 24, rect.bottom - 2, {
        path: file.path, kind: 'file', node: row,
      });
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      setView('draft');
      openDoc(file.path);
    }
  };
  return row;
}

// ── 工作区右键：新建 / 重命名 / 删除 / 添加到聊天 / 时间线 ─────────

function docsPath(suffix = '') {
  const base = `/api/writing/works/${encodeURIComponent(S.workId)}/docs`;
  return suffix ? `${base}/${suffix}` : base;
}

function openDocContextMenu(x, y, target) {
  const isDir = target.kind === 'dir';
  const isRoot = isDir && !String(target.path).includes('/');
  const items = [];
  if (isDir) {
    items.push(contextItem('file-plus', '新建文件',
      () => startDocDraft(target.path, 'file')));
    items.push(contextItem('folder-plus', '新建文件夹',
      () => startDocDraft(target.path, 'dir')));
    items.push(contextSeparator());
  } else {
    items.push(contextItem('history', '打开时间线',
      () => openTimeline(target)));
    items.push(contextSeparator());
  }
  items.push(contextItem('pencil', '重命名',
    () => startRenameDocEntry(target), { disabled: isRoot }));
  items.push(contextItem('message-square-plus', '添加到聊天',
    () => addDocToChat(target)));
  items.push(contextSeparator());
  items.push(contextItem('trash-2', '删除', () => deleteDocEntry(target),
    { danger: true, disabled: isRoot }));
  openWriteContextMenu(x, y, items);
}

function startDocDraft(parent, kind) {
  if (!S.workId) return;
  S.workspaceCollapsed = false;
  writeFlag('bc.writeWorkspaceCollapsed', false);
  S.docsCollapsed.delete(parent);
  S.docsDraft = { parent, kind, value: '' };
  renderChapters();
}

function docDraftRow(depth) {
  const draft = S.docsDraft;
  const row = el('div', 'write-tree-row write-doc-file is-draft');
  row.style.setProperty('--tree-indent', `${depth * 12}px`);
  row.appendChild(treeSpacer());
  row.appendChild(treeIcon(draft.kind === 'dir' ? 'folder' : 'file-text'));
  const input = document.createElement('input');
  input.className = 'write-rename-input';
  input.placeholder = draft.kind === 'dir' ? '新文件夹名' : '新文件名（不用写 .md）';
  input.value = draft.value || '';
  input.maxLength = 60;
  let settled = false;
  const finish = (commit) => {
    if (settled) return;
    settled = true;
    const value = input.value.trim();
    S.docsDraft = null;
    if (!commit || !value) {
      renderChapters();
      return;
    }
    createDocEntry(draft, value);
  };
  input.addEventListener('click', (event) => event.stopPropagation());
  input.addEventListener('input', () => { draft.value = input.value; });
  input.addEventListener('keydown', (event) => {
    event.stopPropagation();
    if (event.key === 'Enter') { event.preventDefault(); input.blur(); }
    else if (event.key === 'Escape') { event.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  row.appendChild(input);
  requestAnimationFrame(() => {
    if (input.isConnected) { input.focus(); input.select(); }
  });
  return row;
}

async function createDocEntry(draft, value) {
  const isFile = draft.kind !== 'dir';
  const name = isFile && !/\.md$/i.test(value) ? `${value}.md` : value;
  const path = `${draft.parent}/${name}`;
  try {
    const row = await api(docsPath('file'), {
      method: 'POST',
      body: JSON.stringify({ path, kind: draft.kind }),
    });
    await loadDocs();
    await refreshStatus();          // 新文件立刻挂上 U
    renderChapters();
    if (isFile && row.path) {
      setView('draft');
      await openDoc(row.path);
    }
    setStatus(isFile ? `已新建 ${docLabel(row.path || path)}`
      : `已新建文件夹 ${row.path || path}`, 'ok');
  } catch (err) {
    // 失败把刚敲的名字留在输入框里，作者改一下就能重试。
    S.docsDraft = { parent: draft.parent, kind: draft.kind, value };
    renderChapters();
    setStatus(err.message, 'error');
  }
}

function startRenameDocEntry(target) {
  const row = target.node;
  const label = row && row.querySelector('.write-tree-label');
  if (!label) { setStatus('找不到这一项', 'error'); return; }
  const oldLabel = label.textContent;
  const input = inlineRenameInput(oldLabel, async (value) => {
    if (!value || value === oldLabel) { renderDocsTree(); return; }
    const wasOpen = target.kind === 'file' && S.docPath === target.path;
    // 文件夹改名：正在看的那份可能就在里面，旧路径整段要换掉。
    const openUnder = target.kind === 'dir' && S.docPath
      && S.docPath.startsWith(`${target.path}/`);
    const openPath = openUnder ? S.docPath : '';
    const timelineUnder = S.timeline && (S.timeline.path === target.path
      || S.timeline.path.startsWith(`${target.path}/`));
    try {
      const res = await api(docsPath('rename'), {
        method: 'POST',
        body: JSON.stringify({
          path: target.path, kind: target.kind, name: value,
        }),
      });
      // 正打开的那份改了名：先放下旧路径，免得 loadDocs 拿它去读。
      if (wasOpen || openUnder) { S.docPath = ''; S.docRow = null; }
      await loadDocs();
      await refreshStatus();
      renderChapters();
      if (wasOpen && res.path) {
        setView('draft');
        await openDoc(res.path);
      } else if (openUnder && res.path) {
        setView('draft');
        await openDoc(res.path + openPath.slice(target.path.length));
      }
      if (timelineUnder) closeTimeline();
      setStatus('已重命名', 'ok');
    } catch (err) {
      renderDocsTree();
      setStatus(err.message, 'error');
    }
  });
  label.replaceWith(input);
  input.focus();
  input.select();
}

async function deleteDocEntry(target) {
  const isDir = target.kind === 'dir';
  const label = docLabel(target.path);
  const message = isDir
    ? `删除文件夹「${label}」？里面的文件会一起删除。`
    : `删除「${label}」？`;
  if (!window.confirm(message)) return;
  try {
    await api(`${docsPath('file')}?path=${encodeURIComponent(target.path)}`
      + `&kind=${encodeURIComponent(target.kind)}`, { method: 'DELETE' });
    const gone = (path) => (isDir
      ? (path === target.path || path.startsWith(`${target.path}/`))
      : path === target.path);
    if (S.docPath && gone(S.docPath)) {
      S.docPath = '';
      S.docRow = null;
      renderDocEmpty();
    }
    if (S.timeline && gone(S.timeline.path)) closeTimeline();
    await loadDocs();
    await refreshStatus();
    renderChapters();
    setStatus('已删除', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function addDocToChat(target) {
  if (typeof window.setWorkspaceSelection !== 'function') return;
  window.setWorkspaceSelection(target.path, target.kind);
  const input = document.getElementById('chatInput');
  if (input) input.focus();
  setStatus(`已把「${docLabel(target.path)}」加到聊天`, 'ok');
}

// ── 全局素材库（决策 156）──────────────────────────────────────────
// 原料仓，不属于任何作品：不进写作 git，也不自动进上下文。
// 页面只负责看与改正文；收录交给助手（作者点名 / 写作时顺手沉淀）。

const LIBRARY_API = '/api/writing/materials';

async function loadLibrary() {
  try {
    // 回收站与在架卡片一起取：左栏是一个整体，分开取会闪两次。
    const [data, recycled] = await Promise.all([
      api(LIBRARY_API),
      api(`${LIBRARY_API}/recycled`).catch(() => ({ items: [] })),
    ]);
    S.library = data.files || [];
    S.libDirs = data.dirs || [];
    S.libRecycled = recycled.items || [];
    S.libPending = data.pending_count || 0;
    if (S.libPath && !S.library.some((f) => f.path === S.libPath)) {
      S.libPath = '';
      S.libRow = null;
      if (!S.docPath) renderLibraryEmpty();
    }
    renderChapters();
    syncCookSuggest();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

/** 只刷新素材库（阅读页收录待归类卡后调用）：不动作品、章节与版本现场。
 *  切回写作页时 open() 在「作品没变」那条分支会早退，不刷新；这里当场对齐，
 *  左栏的「素材库 → 待归类」与待加工气泡都不会停留在旧状态。 */
async function refreshLibrary() {
  await loadLibrary();
}

// ── 待归类整理建议（决策 167）──────────────────────────────────────
// 待归类里出现待整理素材时，空项目的空白会话在输入框上方挂一张提示卡。
// 页面只侦测与建议：加工仍走写作对话（点一下等于替作者发出那句请求）。

function syncCookSuggest() {
  const box = $('writeCookSuggest');
  if (!box) return;
  const text = $('writeCookSuggestText');
  const chat = (window.S && window.S.chatHistory) || [];
  const fresh = chat.length === 0 && !(window.S && window.S.currentSessionId);
  const show = window._currentTab === 'write'
    && S.emptyProject && !S.workId
    && fresh && S.libPending > 0;
  if (text && S.libPending > 0) {
    text.textContent = `有 ${S.libPending} 张素材待归类，整理一下`;
  }
  box.hidden = !show;
}

function runCookSuggest() {
  if (!S.libPending || typeof window.sendMessage !== 'function') return;
  // 先收起：下一帧 updateModePrelude 也会收，但用户点完不该再看到它。
  const box = $('writeCookSuggest');
  if (box) box.hidden = true;
  window.sendMessage(
    `有 ${S.libPending} 张素材待归类，帮我整理成素材卡：`
    + '逐张补标题、分类、标签和一句话说明。先列出来让我确认，'
    + '确认后再用 writing_materials 的 cook 落盘。',
  );
}

function libraryTitle(file) {
  return String(file.title || '').trim() || docLabel(file.name || file.path);
}

function renderLibraryTree() {
  const box = $('writeLibraryTree');
  if (!box) return;
  box.textContent = '';
  const tree = { dirs: new Map(), files: [] };
  const ensureDir = (parts) => {
    let node = tree;
    for (let i = 0; i < parts.length; i += 1) {
      const name = parts[i];
      if (!node.dirs.has(name)) {
        node.dirs.set(name, {
          name, path: parts.slice(0, i + 1).join('/'),
          dirs: new Map(), files: [],
        });
      }
      node = node.dirs.get(name);
    }
    return node;
  };
  (S.libDirs || []).forEach((dir) => {
    const parts = String(dir.path || '').split('/').filter(Boolean);
    if (parts.length) ensureDir(parts);
  });
  (S.library || []).forEach((file) => {
    const parts = String(file.path || '').split('/').filter(Boolean);
    if (!parts.length) return;
    const node = ensureDir(parts.slice(0, -1));
    node.files.push({ ...file, name: parts[parts.length - 1] });
  });
  const walk = (node, depth, parent) => {
    node.files.forEach((file) => parent.appendChild(libFileRow(file, depth)));
    node.dirs.forEach((dir) => {
      parent.appendChild(libDirRow(dir, depth));
      if (!S.libExpanded.has(dir.path)) return;
      const kids = treeChildren(`lib:${dir.path}`, foldGuide(depth, 12));
      parent.appendChild(kids);
      walk(dir, depth + 1, kids);
      if (!kids.childElementCount) kids.remove();
    });
  };
  walk(tree, 1, box);
  const hint = $('writeLibraryHint');
  if (hint) {
    hint.textContent = '';
    if (!(S.library || []).length) {
      hint.appendChild(el('p', 'write-docs-empty',
        '素材库还是空的。让助手把一段原文收进来，'
        + '或从阅读页选中一段存到素材库。'));
    }
  }
  if ((S.libRecycled || []).length) {
    // 回收站挂在素材库组的末尾，不放进分类子级容器：素材库的缩进参考线
    // 只该罩住分类，越到回收站（及其条目）下面就成了没有归属的竖线。
    // 直接 renderLibraryTree 的重绘要先把旧的收掉，避免重复挂载。
    const host = box.parentElement || box;
    host.querySelectorAll(':scope > .write-tree-recycle')
      .forEach((node) => node.remove());
    host.appendChild(recycledNode());
  }
}

function recycledNode() {
  const count = (S.libRecycled || []).length;
  const wrapper = el('div', 'write-tree-group write-tree-recycle');
  wrapper.appendChild(folderRow('回收站', {
    collapsed: S.libRecycleCollapsed,
    meta: `${count} 项`,
    actions: [treeAction('trash-2', '清空回收站', (event) => {
      event.stopPropagation();
      purgeRecycled();
    })],
    onToggle: (row) => {
      const open = S.libRecycleCollapsed;
      S.libRecycleCollapsed = !S.libRecycleCollapsed;
      foldTree(row, 'recycle', open, renderLibraryTree);
    },
  }));
  if (!S.libRecycleCollapsed) {
    const kids = treeChildren('recycle', foldGuide(0, 12));
    (S.libRecycled || []).forEach((item) => {
      kids.appendChild(recycledRow(item));
    });
    wrapper.appendChild(kids);
  }
  return wrapper;
}

function recycledRow(item) {
  const row = el('div', 'write-tree-row write-doc-file');
  row.style.setProperty('--tree-indent', '12px');
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.appendChild(treeSpacer());
  row.appendChild(treeIcon('trash-2'));
  row.appendChild(el('span', 'write-tree-label', item.name || item.path));
  if (item.deleted_at) {
    row.appendChild(el('span', 'write-tree-meta', whenText(item.deleted_at)));
  }
  row.title = `${item.original || item.path} · 右键可恢复`;
  const openMenu = (x, y) => openLibraryContextMenu(x, y, {
    path: item.path, kind: item.kind || 'file', recycled: true, node: row,
  });
  row.onclick = (event) => {
    event.stopPropagation();
    const rect = row.getBoundingClientRect();
    openMenu(rect.left + 24, rect.bottom - 2);
  };
  row.oncontextmenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    row.focus();
    openMenu(event.clientX, event.clientY);
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      openMenu(rect.left + 24, rect.bottom - 2);
    }
  };
  return row;
}

function libCount(dir) {
  let n = dir.files.length;
  dir.dirs.forEach((child) => { n += libCount(child); });
  return n;
}

function libDirRow(dir, depth) {
  const expanded = S.libExpanded.has(dir.path);
  const collapsed = !expanded;
  const row = el('div', 'write-tree-row write-doc-dir');
  row.style.setProperty('--tree-indent', `${depth * 12}px`);
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  row.dataset.path = dir.path;
  const toggle = () => {
    const open = collapsed;
    if (open) S.libExpanded.add(dir.path);
    else S.libExpanded.delete(dir.path);
    foldTree(row, `lib:${dir.path}`, open, renderLibraryTree);
  };
  row.appendChild(twistyButton(collapsed, toggle));
  row.appendChild(treeIcon(collapsed ? 'folder' : 'folder-open'));
  row.appendChild(el('span', 'write-tree-label', dir.name));
  const count = libCount(dir);
  if (count) row.appendChild(el('span', 'write-tree-meta', String(count)));
  row.onclick = toggle;
  row.oncontextmenu = (event) => {
    if (event.target.closest('input, textarea')) return;
    event.preventDefault();
    event.stopPropagation();
    row.focus();
    openLibraryContextMenu(event.clientX, event.clientY, {
      path: dir.path, kind: 'dir', node: row,
    });
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'ContextMenu'
        || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      openLibraryContextMenu(rect.left + 24, rect.bottom - 2, {
        path: dir.path, kind: 'dir', node: row,
      });
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggle();
    }
  };
  return row;
}

function libFileRow(file, depth) {
  const row = el('div', 'write-tree-row write-doc-file');
  row.style.setProperty('--tree-indent', `${depth * 12}px`);
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  if (file.path === S.libPath) {
    row.classList.add('active');
    row.setAttribute('aria-current', 'true');
  }
  row.appendChild(treeSpacer());
  row.appendChild(treeIcon('file-text'));
  row.appendChild(el('span', 'write-tree-label', libraryTitle(file)));
  row.title = file.category ? `${file.path} · ${file.category}` : file.path;
  row.dataset.path = file.path;
  row.onclick = () => { setView('draft'); openLibraryCard(file.path); };
  row.oncontextmenu = (event) => {
    if (event.target.closest('input, textarea')) return;
    event.preventDefault();
    event.stopPropagation();
    row.focus();
    openLibraryContextMenu(event.clientX, event.clientY, {
      path: file.path, kind: 'file', node: row,
    });
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'ContextMenu'
        || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      openLibraryContextMenu(rect.left + 24, rect.bottom - 2, {
        path: file.path, kind: 'file', node: row,
      });
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      setView('draft');
      openLibraryCard(file.path);
    }
  };
  return row;
}

function openLibraryContextMenu(x, y, target) {
  const items = [];
  if (target.recycled) {
    items.push(contextItem('rotate-ccw', '恢复', () => restoreLibraryEntry(target)));
  } else {
    items.push(contextItem('pencil', '重命名',
      () => startRenameLibraryEntry(target)));
    items.push(contextItem('folder', '移动到…',
      () => openLibraryMoveMenu(x, y, target)));
    if (target.kind === 'dir') {
      // 只清里面的卡，不删文件夹本身；空文件夹不需要这个动作。
      items.push(contextItem('eraser', '清空文件夹',
        () => emptyLibraryFolder(target),
        { danger: true,
          disabled: !libraryFilesUnder(target.path).length }));
    }
    items.push(contextSeparator());
    items.push(contextItem('trash-2', '删除（回收站）',
      () => deleteLibraryEntry(target), { danger: true }));
  }
  openWriteContextMenu(x, y, items);
}

function libraryFilesUnder(path) {
  return (S.library || []).filter(
    (file) => String(file.path || '').startsWith(`${path}/`));
}

/** 清空文件夹：逐张进回收站，文件夹与子分类原地保留。 */
async function emptyLibraryFolder(target) {
  const files = libraryFilesUnder(target.path);
  const name = String(target.path).split('/').pop();
  if (!files.length) {
    setStatus('这个文件夹已经是空的', 'ok');
    return;
  }
  const message = `清空文件夹「${name}」里的 ${files.length} 个素材卡？`
    + '它们会进回收站（之后可以恢复），文件夹与子分类保留。';
  if (!window.confirm(message)) return;
  try {
    const res = await api(`${LIBRARY_API}/empty`, {
      method: 'POST',
      body: JSON.stringify({ path: target.path }),
    });
    const gone = (path) => String(path).startsWith(`${target.path}/`);
    if (S.libPath && gone(S.libPath)) {
      S.libPath = '';
      S.libRow = null;
      renderLibraryEmpty();
    }
    await loadLibrary();
    setStatus(res.count ? `已清空 ${res.count} 个素材卡` : '这个文件夹已经是空的', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function openLibraryMoveMenu(x, y, target) {
  const dirs = [...new Set((S.libDirs || [])
    .map((d) => d.path).filter(Boolean))];
  const items = dirs.map((dir) => contextItem(
    'folder', dir, () => moveLibraryEntry(target, dir),
    { disabled: dir === String(target.path).split('/').slice(0, -1).join('/') }));
  if (!items.length) {
    items.push(contextItem('folder', '还没有分类目录', () => {}, { disabled: true }));
  }
  openWriteContextMenu(x, y, items);
}

function startRenameLibraryEntry(target) {
  const label = target.node
    && target.node.querySelector('.write-tree-label');
  if (!label) { setStatus('找不到这一项', 'error'); return; }
  const oldLabel = label.textContent;
  const input = inlineRenameInput(oldLabel, async (value) => {
    if (!value || value === oldLabel) { renderLibraryTree(); return; }
    const wasOpen = target.kind === 'file' && S.libPath === target.path;
    const openUnder = target.kind === 'dir' && S.libPath
      && S.libPath.startsWith(`${target.path}/`);
    const openPath = openUnder ? S.libPath : '';
    try {
      const res = await api(`${LIBRARY_API}/rename`, {
        method: 'POST',
        body: JSON.stringify({
          path: target.path, kind: target.kind, name: value,
        }),
      });
      if (wasOpen || openUnder) { S.libPath = ''; S.libRow = null; }
      await loadLibrary();
      if (wasOpen && res.path) await openLibraryCard(res.path);
      else if (openUnder && res.path) {
        await openLibraryCard(res.path + openPath.slice(target.path.length));
      }
      setStatus('已重命名', 'ok');
    } catch (err) {
      renderLibraryTree();       // 把行内输入还回标签，别留一个孤零零的框
      setStatus(err.message, 'error');
    }
  });
  label.replaceWith(input);
  input.focus();
  input.select();
}

async function moveLibraryEntry(target, targetDir) {
  const wasOpen = target.kind === 'file' && S.libPath === target.path;
  try {
    const res = await api(`${LIBRARY_API}/move`, {
      method: 'POST',
      body: JSON.stringify({
        path: target.path, kind: target.kind, target_dir: targetDir,
      }),
    });
    if (wasOpen) { S.libPath = ''; S.libRow = null; }
    await loadLibrary();
    if (wasOpen && res.path) await openLibraryCard(res.path);
    setStatus(`已移动到 ${targetDir}`, 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function deleteLibraryEntry(target) {
  const isDir = target.kind === 'dir';
  const name = String(target.path).split('/').pop();
  const message = isDir
    ? `删除分类「${name}」？里面的素材卡会一起进回收站。`
    : `删除「${name}」？会进回收站，之后可以恢复。`;
  if (!window.confirm(message)) return;
  try {
    await api(`${LIBRARY_API}/file?path=${encodeURIComponent(target.path)}`
      + `&kind=${encodeURIComponent(target.kind)}`, { method: 'DELETE' });
    const gone = (path) => (isDir
      ? (path === target.path || path.startsWith(`${target.path}/`))
      : path === target.path);
    if (S.libPath && gone(S.libPath)) {
      S.libPath = '';
      S.libRow = null;
      renderLibraryEmpty();
    }
    await loadLibrary();
    setStatus('已移到回收站', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function restoreLibraryEntry(target) {
  try {
    const res = await api(`${LIBRARY_API}/restore`, {
      method: 'POST',
      body: JSON.stringify({ path: target.path }),
    });
    await loadLibrary();
    setStatus(`已恢复到 ${res.restored}`, 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function purgeRecycled() {
  const count = (S.libRecycled || []).length;
  if (!count) { setStatus('回收站已经是空的', 'ok'); return; }
  const message = `清空回收站里的 ${count} 项？删除后无法恢复。`
    + '（在架素材卡不受影响）';
  if (!window.confirm(message)) return;
  try {
    const res = await api(`${LIBRARY_API}/purge`, {
      method: 'POST',
      body: JSON.stringify({ all: true }),
    });
    await loadLibrary();
    setStatus(res.count ? `已清空 ${res.count} 项` : '回收站已经是空的', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

async function openLibraryCard(path, options = {}) {
  if (!path) return;
  try {
    const row = await api(`${LIBRARY_API}/file?path=`
      + encodeURIComponent(path));
    S.docPath = '';
    S.docRow = null;
    S.libPath = row.path || path;
    S.libRow = row;
    renderLibraryCard();
    renderLibraryTree();
  } catch (err) {
    if (options.quiet) {
      S.libPath = '';
      S.libRow = null;
      renderLibraryEmpty();
    }
    setStatus(err.message, 'error');
  }
}

function renderLibraryCard() {
  const row = S.libRow;
  const body = $('writeDocBody');
  if (!row || !body) return;
  S.libEditing = false;
  $('writeDocPath').textContent = row.title || docLabel(row.path);
  // 抬头只留标题：分类 / 标签 / 采集时间在卡片正文里已经有了，再排一遍
  // 只是把「添加到会话 / 编辑」挤到中间。空白留给标题。
  $('writeDocMeta').textContent = '';
  const edit = $('writeDocEditBtn');
  if (edit) edit.hidden = false;
  if ($('writeDocAddBtn')) $('writeDocAddBtn').hidden = false;
  body.innerHTML = renderDocMarkdown(row.content || '')
    || '<p class="write-docs-empty">这张卡还是空的。</p>';
  decorateMaterialHead(body);
  body.scrollTop = 0;
}

function renderLibraryEmpty() {
  S.libRow = null;
  const body = $('writeDocBody');
  if (!body) return;
  if ($('writeDocPath')) $('writeDocPath').textContent = '素材库';
  if ($('writeDocMeta')) $('writeDocMeta').textContent = '';
  const edit = $('writeDocEditBtn');
  if (edit) edit.hidden = true;
  if ($('writeDocAddBtn')) $('writeDocAddBtn').hidden = true;
  body.textContent = '';
  const box = el('div', 'write-placeholder');
  box.appendChild(el('strong', '', '素材库'));
  box.appendChild(el('p', '',
    '这里放不属于任何一本书的原料：书外的观察、例子、语料、'
    + '别家的写法与物象。它不进写作 git，也不会自动塞给正文——'
    + '想用时跟助手说，或让助手从书里收一段进来。'));
  body.appendChild(box);
}

function startLibraryEdit() {
  if (!S.libRow) return;
  S.libEditing = true;
  // 编辑期间不提供「添加到会话」：此刻文件里还是旧版本，发出去的引用会错位。
  if ($('writeDocAddBtn')) $('writeDocAddBtn').hidden = true;
  const body = $('writeDocBody');
  body.textContent = '';
  const wrap = el('div', 'write-doc-edit');
  const area = document.createElement('textarea');
  area.className = 'write-editor write-doc-editor';
  area.value = S.libRow.content || '';
  area.spellcheck = false;
  const bar = el('div', 'write-doc-edit-bar');
  const save = el('button', 'write-btn primary', '保存');
  const cancel = el('button', 'write-btn', '取消');
  save.onclick = () => saveLibraryEdit(area.value);
  cancel.onclick = () => renderLibraryCard();
  bar.append(save, cancel);
  wrap.append(area, bar);
  body.appendChild(wrap);
  area.focus();
}

async function saveLibraryEdit(text) {
  if (!S.libPath) return;
  try {
    await api(`${LIBRARY_API}/save`, {
      method: 'POST',
      body: JSON.stringify({ path: S.libPath, content: text }),
    });
    S.libEditing = false;
    await openLibraryCard(S.libPath);
    await loadLibrary();
    setStatus('已保存素材卡', 'ok');
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

// ── 时间线：一份工作区文件的 git 历史（VS Code 的 Timeline）──────────

async function openTimeline(target) {
  if (!S.workId) return;
  S.timeline = { path: target.path, entries: [], loading: true };
  renderTimeline();
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/vcs/timeline?path=`
      + encodeURIComponent(target.path));
    if (!S.timeline || S.timeline.path !== target.path) return;
    S.timeline = { path: target.path, entries: data.entries || [],
                   loading: false };
    renderTimeline();
  } catch (err) {
    closeTimeline();
    setStatus(err.message, 'error');
  }
}

function closeTimeline() {
  S.timeline = null;
  const panel = $('writeTimeline');
  if (panel) panel.hidden = true;
}

function renderTimeline() {
  const panel = $('writeTimeline');
  if (!panel) return;
  const state = S.timeline;
  if (!state) { panel.hidden = true; return; }
  panel.hidden = false;
  $('writeTimelineFile').textContent = docLabel(state.path);
  const list = $('writeTimelineList');
  list.textContent = '';
  if (state.loading) {
    list.appendChild(el('p', 'write-vcs-empty', '正在读历史…'));
    return;
  }
  if (!state.entries.length) {
    list.appendChild(el('p', 'write-vcs-empty', '这份文件还没有历史。'));
    return;
  }
  state.entries.forEach((entry) => list.appendChild(timelineRow(entry)));
}

function timelineRow(entry) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'write-timeline-item';
  row.title = entry.message || '';
  const op = entry.op || 'changed';
  const dot = el('span', 'write-timeline-op');
  dot.dataset.op = op;
  const main = el('span', 'write-timeline-main');
  main.appendChild(el('span', 'write-timeline-msg',
    entry.message || '（无说明）'));
  const opLabels = { added: '新增', removed: '删除', changed: '修改' };
  const bits = [entry.is_work ? '工作区' : (entry.short || ''),
                whenText(entry.time_created), opLabels[op] || op];
  main.appendChild(el('span', 'write-timeline-meta',
    bits.filter(Boolean).join(' · ')));
  row.appendChild(dot);
  row.appendChild(main);
  row.onclick = () => openTimelineDiff(entry);
  return row;
}

function openTimelineDiff(entry) {
  if (!S.timeline) return;
  const from = entry.is_work ? 'HEAD' : (entry.parent_id || 'EMPTY');
  const to = entry.is_work ? 'WORK' : entry.id;
  openVcsDiff(from, to, '', S.timeline.path);
}

/** 极简 Markdown：标题 / 列表 / 引用 / 粗体 / 行内代码。先转义再加标签。 */
function renderDocMarkdown(text) {
  const inline = (raw) => escapeHtml(raw)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
  const out = [];
  let para = [];
  let list = '';
  const flushPara = () => {
    if (!para.length) return;
    out.push(`<p>${para.map(inline).join('<br>')}</p>`);
    para = [];
  };
  const closeList = () => {
    if (list) { out.push(`</${list}>`); list = ''; }
  };
  String(text || '').split(/\r?\n/).forEach((line) => {
    const t = line.trim();
    if (!t) { flushPara(); closeList(); return; }
    let match = t.match(/^(#{1,4})\s+(.*)$/);
    if (match) {
      flushPara();
      closeList();
      const level = Math.min(match[1].length + 1, 5);
      out.push(`<h${level}>${inline(match[2])}</h${level}>`);
      return;
    }
    match = t.match(/^[-*]\s+(.*)$/);
    if (match) {
      flushPara();
      if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${inline(match[1])}</li>`);
      return;
    }
    match = t.match(/^>\s?(.*)$/);
    if (match) {
      flushPara();
      closeList();
      out.push(`<blockquote>${inline(match[1])}</blockquote>`);
      return;
    }
    flushPara();
    closeList();
    para.push(line);
  });
  flushPara();
  closeList();
  return out.join('');
}

// ── 素材卡抬头：身份 / 会话 id 可复制，卡片可加入会话 ──────────────────
//
// 抬头里的这两串东西是「要跟 agent 说话时唯一得抄下来的值」，手选最容易
// 抄错一位。渲染完再把它们换成按钮，不改变文件里的写法——文件仍是纯
// Markdown，别的编辑器、脚本照旧能读。

const MATERIAL_ID_RE = /(素材\s+)([0-9a-fA-F]{6,})/;
const MATERIAL_SESSION_RE = /(会话\s+)([A-Za-z0-9._:-]+)/;

function matCopyChip(value, tip) {
  return `<button type="button" class="mat-copy"`
    + ` data-copy="${escapeHtmlAttr(value)}" title="${tip}"`
    + ` aria-label="${tip}">${escapeHtml(value)}`
    + '<i data-lucide="copy" aria-hidden="true"></i></button>';
}

/** 只装饰卡片抬头（第一个 `## 原文` 之前），正文里的引用块不碰。 */
function decorateMaterialHead(body) {
  if (!body) return;
  let seen = false;
  for (const node of Array.from(body.children)) {
    if (/^H[1-5]$/.test(node.tagName)) {
      if (seen) break;        // 进了「## 原文 / ## 说明」，后面是正文
      continue;               // 第一行是标题
    }
    if (node.tagName !== 'BLOCKQUOTE') continue;
    seen = true;
    const text = node.textContent || '';
    if (!MATERIAL_ID_RE.test(text) && !MATERIAL_SESSION_RE.test(text)) continue;
    let html = escapeHtml(text)
      .replace(MATERIAL_ID_RE,
        (_, lead, id) => lead + matCopyChip(id, '复制素材id'))
      .replace(MATERIAL_SESSION_RE,
        (_, lead, sid) => lead + matCopyChip(sid, '复制会话id'));
    node.innerHTML = html;
    node.querySelectorAll('.mat-copy').forEach((btn) => {
      btn.addEventListener('click', () => copyChipValue(btn));
    });
  }
  if (window.lucide && lucide.createIcons) lucide.createIcons();
}

/** 剪贴板：优先 async API，非安全上下文回落到 execCommand。 */
async function copyPlain(text) {
  const value = String(text || '');
  if (!value) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch (_) { /* 落到下面的兜底 */ }
  try {
    const area = document.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch (_) {
    return false;
  }
}

async function copyChipValue(btn) {
  const tip = btn.title;
  if (!(await copyPlain(btn.dataset.copy || ''))) {
    setStatus('复制失败，请手动选中', 'error');
    return;
  }
  btn.classList.add('is-copied');
  btn.title = '已复制';
  setTimeout(() => {
    btn.classList.remove('is-copied');
    btn.title = tip;
  }, 1200);
}

/**
 * 把这张卡挂进对话输入框上方的附件托盘（不发送）。
 *
 * 托盘可以叠多张，× 取消；按下发送时随消息一起送出并清空。这里只传事实，
 * 读不读、提不提升、用不用，交给写作提示词里的材料规则。
 */
function addCardToSession() {
  const row = S.libRow;
  if (!row || !row.path) return;
  if (typeof window.attachMaterial !== 'function') {
    setStatus('对话还没准备好，稍后再试', 'error');
    return;
  }
  const panel = document.getElementById('chatPanel');
  if (panel && panel.classList.contains('collapsed')
      && typeof window.toggleChatPanel === 'function') {
    // 托盘在对话面板里，收起着就看不见了。
    window.toggleChatPanel();
  }
  const res = window.attachMaterial({
    id: row.material_id || '',
    title: row.title || docLabel(row.path),
    category: row.category || '',
    path: row.path,
  });
  if (res && res.added) {
    setStatus(`已加入附件（${res.count} 张）：发送时随消息送出`, 'ok');
  } else if (res && res.reason === 'duplicate') {
    setStatus('这张素材已经在附件里了', 'ok');
  } else {
    setStatus('这张卡缺少身份，没法加入附件', 'error');
  }
}

// ── 写作 git：版本面板 ─────────────────────────────────────────────

//: 左栏的分组。【更改】与【图表】默认摊开，其余收着——窄栏一屏放不下七块。
const VCS_GROUPS = ['changes', 'graph', 'branches', 'tags', 'stashes',
                    'reflog', 'feedback'];
const VCS_GROUPS_OPEN = { changes: true, graph: true };

function readVcsGroups() {
  const state = {};
  VCS_GROUPS.forEach((key) => { state[key] = !!VCS_GROUPS_OPEN[key]; });
  try {
    const saved = JSON.parse(localStorage.getItem('bc.writeVcsGroups') || '{}');
    VCS_GROUPS.forEach((key) => {
      if (typeof saved[key] === 'boolean') state[key] = saved[key];
    });
  } catch (_) { /* 坏数据就用默认 */ }
  return state;
}

function applyVcsGroups() {
  document.querySelectorAll('#writeVcsRail .write-vcs-group').forEach((group) => {
    const open = !!S.vcsOpen[group.dataset.group];
    const head = group.querySelector('.write-vcs-group-head');
    const body = group.querySelector('.write-vcs-group-body');
    if (!head || !body) return;
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    body.hidden = !open;
    const caret = head.querySelector('.write-vcs-group-caret');
    if (caret) caret.innerHTML = `<i data-lucide="chevron-${open ? 'down' : 'right'}"></i>`;
  });
  if (window.refreshIcons) window.refreshIcons();
}

function setVcsGroup(key, open) {
  if (!VCS_GROUPS.includes(key)) return;
  S.vcsOpen[key] = open;
  try {
    localStorage.setItem('bc.writeVcsGroups', JSON.stringify(S.vcsOpen));
  } catch (_) { /* private mode */ }
  applyVcsGroups();
}

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

/** 只重取状态（不拉历史/暂存/反馈）：文件徽章与「更改」面板的实时来源。
 *
 * 助手跑工具时 chat.js 会在 tool_end 调它，作者不必等整轮结束才看到 M/U。
 * 失败静默：状态是装饰，取不到不该打断手头的活。
 */
async function refreshStatus() {
  if (!S.workId) return;
  try {
    const status = await api(
      `/api/writing/works/${encodeURIComponent(S.workId)}/vcs/status`);
    if (!S.workId) return;
    S.vcs = status;
    S.vcsStale = false;
    renderVcs();
  } catch (_) { /* 状态是装饰 */ }
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
      + ` title="只丢弃这一章的未存档改动">丢弃</button></div>`
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
  const fileChanges = vcs.file_changes || { added: [], removed: [], changed: [] };
  const fileMeta = { changed: '已修改', added: '未跟踪', removed: '已删除' };
  const fileLetter = { changed: 'M', added: 'U', removed: 'D' };
  ['changed', 'added', 'removed'].forEach((op) => {
    (fileChanges[op] || []).forEach((f) => {
      const letter = f.status || fileLetter[op];
      const label = f.status_label || fileMeta[op];
      const tip = DOC_STATUS_TIPS[letter]
        ? `${label}：${DOC_STATUS_TIPS[letter]}` : label;
      // 点开看这一份的逐行差异（中间栏）；文件区胶囊和这里同一份状态。
      rows.push(`<div class="write-vcs-item write-vcs-file"`
        + ` data-doc-path="${escapeHtmlAttr(f.path)}">`
        + `<div>${escapeHtml(docLabel(f.label || f.path))}`
        + `<span class="write-vcs-badge" data-status="${letter}"`
        + ` title="${escapeHtmlAttr(tip)}">${escapeHtml(label)}</span>`
        + `</div></div>`);
    });
  });
  if (vcs.ledger_dirty) {
    rows.push('<div class="write-vcs-item" data-diff-ledger="1">'
      + '<div>账本</div><div class="vcs-meta">合同 / 事实 / 承诺有未存档改动</div></div>');
  }
  $('writeVcsChanges').innerHTML = rows.length
    ? rows.join('') : '<div class="write-vcs-empty">工作区干净</div>';
  const bits = [];
  const n = (changes.added || []).length + (changes.removed || []).length
    + (changes.changed || []).length;
  if (n) bits.push(`${n} 章`);
  const fn = (fileChanges.added || []).length
    + (fileChanges.removed || []).length + (fileChanges.changed || []).length;
  if (fn) bits.push(`${fn} 份文件`);
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
  // 文件树的徽章和「更改」是同一份状态：一次渲染两处一起对齐。
  applyDocStatusBadges();
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
//: 展开的改动清单：每行高度与上下留白，决定续线的 SVG 高度。
//: 必须与 CSS 的 .write-graph-file / .write-graph-files-body 相等，
//: 否则续线与下一行的竖线接不上。
const GRAPH_FILE_ROW_H = 24;
const GRAPH_FILE_PAD_Y = 8;
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
    // after：本行画完后还活着的泳道。展开的改动清单要接着把这些线
    // 画下去，否则下一行的竖线会从半空中开始。
    rows.push({ commit, col, joins, through, mergeCol, mergeActive,
                stub: !fresh, down: !!parent, after: lanes.slice() });
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
  const open = S.vcsExpand && S.vcsExpand[c.id] ? ' open' : '';
  return `<div class="write-graph-row${dim}${open}" data-commit="${escapeHtmlAttr(c.id)}"`
    + ` aria-expanded="${open ? 'true' : 'false'}"`
    + ` title="${escapeHtmlAttr(`${text}\n${c.short} · ${whenText(c.time_created)}\n点击展开这条存档的改动文件`)}">`
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
    box.innerHTML = '<div class="write-vcs-empty">还没有存档——保存一次改动，'
      + '这里就会长出第一条线</div>';
    return;
  }
  const { rows, laneCount } = layoutCommitGraph(S.vcsLog);
  const gutter = GRAPH_PAD * 2 + Math.max(laneCount - 1, 0) * GRAPH_LANE_W;
  box.innerHTML = rows.map((row) => {
    const head = graphRowHtml(row, gutter);
    const files = S.vcsExpand[row.commit.id]
      ? commitFilesHtml(row, gutter) : '';
    if (!files) return head;
    // 展开块是提交行的兄弟节点：点里面的文件不会被当成再点一次提交。
    return `<div class="write-graph-item">${head}${files}</div>`;
  }).join('');
}

// ── 版本图：点开一条提交看它改了哪些文件（VS Code 的提交明细）──────
// 点击提交行 = 展开/收起；点清单里的文件 = 中间栏看这一份的逐行差异；
// 清单末尾留一个「查看全部改动」，章级回滚/账本差异还从那里进。

const COMMIT_OP_LABELS = { added: '新增', removed: '删除', changed: '修改' };

function toggleCommitFiles(commitId) {
  if (!commitId) return;
  if (S.vcsExpand[commitId]) {
    delete S.vcsExpand[commitId];
    renderVcsLog();
    return;
  }
  S.vcsExpand[commitId] = { loading: true };
  renderVcsLog();
  loadCommitFiles(commitId);
}

async function loadCommitFiles(commitId) {
  const commit = S.vcsLog.find((c) => c.id === commitId);
  if (!commit || !S.workId) return;
  const workId = S.workId;
  const params = new URLSearchParams({
    from: commit.parent_id || 'EMPTY', to: commitId, text: '0',
  });
  try {
    const data = await api(
      `/api/writing/works/${encodeURIComponent(workId)}/vcs/diff?${params}`);
    if (S.workId !== workId) return;          // 换书了：旧请求作废
    S.commitFiles[commitId] = commitFileRows(data);
    // 请求回来的路上用户又点了一次收起：只留缓存，别再自动展开。
    if (!S.vcsExpand[commitId]) return;
    S.vcsExpand[commitId] = {};
    // 清单真正出现的这一帧才播展开动画（loading 那几帧什么都不画）。
    S.vcsOpening = commitId;
  } catch (err) {
    if (S.workId !== workId || !S.vcsExpand[commitId]) return;
    S.vcsExpand[commitId] = { error: err.message };
  }
  renderVcsLog();
}

/** 一条提交相对第一父节点的改动清单：章在前（按章序），工作区文件在后。 */
function commitFileRows(data) {
  const rows = [];
  const chapters = [];
  ['changed', 'added', 'removed'].forEach((op) => {
    ((data.changes || {})[op] || []).forEach((c) => {
      chapters.push({ kind: 'chapter', op, chapterId: String(c.chapter_id || ''),
                      index: Number(c.chapter_index) || 0,
                      label: chapterLabel(c) });
    });
  });
  chapters.sort((a, b) => a.index - b.index);
  rows.push(...chapters);
  const files = [];
  ['changed', 'added', 'removed'].forEach((op) => {
    ((data.files || {})[op] || []).forEach((f) => {
      files.push({ kind: 'file', op, path: String(f.path || ''),
                   label: docLabel(f.label || f.path) });
    });
  });
  files.sort((a, b) => a.path.localeCompare(b.path));
  rows.push(...files);
  // 账本不在章/文件里，单独一行；它没有单份 diff，点开走整条提交。
  const ledger = data.ledger || {};
  const ledgerOps = new Set();
  Object.keys(ledger).forEach((key) => {
    const item = ledger[key] || {};
    ['added', 'removed', 'changed'].forEach((op) => {
      if ((item[op] || []).length) ledgerOps.add(op);
    });
  });
  if (ledgerOps.size) {
    // 只有新增/只有删除时才敢说「新增 / 删除」，混着就是「修改」。
    const op = ledgerOps.size === 1 ? [...ledgerOps][0] : 'changed';
    rows.push({ kind: 'ledger', op, label: '账本' });
  }
  return rows;
}

function commitFilesHtml(row, gutter) {
  const id = row.commit.id;
  const state = S.vcsExpand[id] || {};
  const list = S.commitFiles[id];
  // 读取中不画占位块：清单回来时直接从 0 展开，比「先闪一行再撑开」稳。
  if (state.loading && !list) return '';
  const items = [];
  const note = (text) => '<div class="write-graph-file is-note">'
    + `${escapeHtml(text)}</div>`;
  if (state.error) {
    items.push(note(state.error));
  } else if (!list || !list.length) {
    items.push(note('这次存档没有改动'));
  } else {
    list.forEach((f) => {
      const status = `<span class="wgf-status" data-op="${f.op}">`
        + `${COMMIT_OP_LABELS[f.op] || f.op}</span>`;
      const target = f.kind === 'chapter'
        ? ` data-diff-chapter="${escapeHtmlAttr(f.chapterId)}"`
        : (f.kind === 'ledger'
          ? ' data-commit-all="1"'
          : ` data-diff-file="${escapeHtmlAttr(f.path)}"`);
      items.push(`<button class="write-graph-file" type="button"`
        + ` data-expand-commit="${escapeHtmlAttr(id)}"${target}`
        + ` title="${escapeHtmlAttr(`${f.label} · ${COMMIT_OP_LABELS[f.op] || ''}`)}">`
        + `<span class="wgf-name">${escapeHtml(f.label)}</span>${status}</button>`);
    });
  }
  const lanes = row.after
    .map((v, i) => (v ? i : -1)).filter((i) => i >= 0);
  const height = GRAPH_FILE_PAD_Y + items.length * GRAPH_FILE_ROW_H;
  // 动画只播一次：读到 vcsOpening 就消费掉，后续重渲染不再重播。
  const opening = S.vcsOpening === id ? ' is-opening' : '';
  if (S.vcsOpening === id) S.vcsOpening = '';
  return `<div class="write-graph-files${opening}"`
    + ` style="--files-h:${height}px">`
    + graphContinuationSvg(lanes, gutter, height)
    + `<div class="write-graph-files-body">${items.join('')}</div></div>`;
}

/** 展开块里的泳道续线：把这一行之后还活着的线画满整个清单高度。 */
function graphContinuationSvg(lanes, gutter, height) {
  const parts = lanes.map((j) => {
    const x = GRAPH_PAD + j * GRAPH_LANE_W;
    return `<path d="M${x} 0V${height}" style="stroke:${graphColor(j)}"/>`;
  }).join('');
  return `<svg class="write-graph-gutter" width="${gutter}" height="${height}"`
    + ` viewBox="0 0 ${gutter} ${height}" aria-hidden="true">${parts}</svg>`;
}

async function openVcsDiff(fromRef, toRef, chapterId, filePath) {
  if (!S.workId) return;
  const params = new URLSearchParams({
    from: fromRef || 'EMPTY', to: toRef || 'WORK',
    text: (chapterId || filePath) ? '1' : '0',
  });
  if (chapterId) params.set('chapter_id', chapterId);
  if (filePath) params.set('file_path', filePath);
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
  // 记成真的视图，别的入口（点章节、开设定、换分区）才会自己让位；
  // 以前只 showOnlyView，S.view 还停在旧屏上，等于盖了一层揭不掉的浮层。
  if (S.view !== 'diff') S.diffReturn = S.view;
  setView('diff');
}

function renderDiff(data) {
  const from = data.from || {};
  const to = data.to || {};
  // 时间线打开的是单份文件的差异：恢复/撤销/摘取都是章级动作，不能用。
  const isFile = !!data.file_path;
  $('writeDiffTitle').textContent = isFile
    ? `${docLabel(data.file_path)} · ${from.short || 'EMPTY'} → ${to.short || 'WORK'}`
    : `${from.short || 'EMPTY'} → ${to.short || 'WORK'}`;
  $('writeDiffRestore').hidden = !S.diffCommitId || isFile;
  $('writeDiffRevert').hidden = !S.diffCommitId || isFile;
  $('writeDiffPick').hidden = !S.diffCommitId || isFile;
  const parts = [];
  [[data.changes && data.changes.changed, '修改'],
   [data.changes && data.changes.added, '新增'],
   [data.changes && data.changes.removed, '删除']].forEach(([rows, label]) => {
    if (isFile || !rows || !rows.length) return;
    parts.push(`<div class="write-diff-section"><h4>${label}（${rows.length}）</h4>`
      + rows.map((c) => `<div class="write-vcs-item" data-diff-chapter="${c.chapter_id}">`
        + `<div>${escapeHtml(chapterLabel(c))}</div>`
        + `<div class="vcs-meta">${escapeHtml(fieldText(c.fields))}</div></div>`).join('')
      + '</div>');
  });
  const opLabels = { added: '新增', removed: '删除', changed: '修改' };
  const fileRows = [];
  if (!isFile) Object.keys(data.files || {}).forEach((key) => {
    const list = (data.files || {})[key] || [];
    if (list.length) {
      fileRows.push(`<div class="write-diff-section">`
        + `<h4>设定文件 · ${opLabels[key] || key}（${list.length}）</h4>`
        + list.map((f) => `<div class="write-vcs-item">`
          + `<div>${escapeHtml(docLabel(f.label || f.path))}</div>`
          + `<div class="vcs-meta">${opLabels[key] || key}</div></div>`
        ).join('') + '</div>');
    }
  });
  fileRows.forEach((part) => parts.push(part));
  const ledgerRows = [];
  if (!isFile) Object.keys(data.ledger || {}).forEach((key) => {
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
    parts.push(`<div class="write-diff-section"><h4>${
      isFile ? escapeHtml(docLabel(data.file_path)) : '正文'}</h4>`
      + renderDiffText(data.text_diff) + '</div>');
  }
  if (!parts.length) parts.push('<div class="write-vcs-empty">没有差异</div>');
  $('writeDiffBody').innerHTML = parts.join('');
}

/** 统一 diff → 双行号 + 整行底色（VS Code 的逐行差异）。
 *
 * 行号从 @@ 头读出：删除行只走旧号，新增行只走新号，上下文两边都走。
 * 文件头（--- / +++）只在第一个 hunk 之前认；hunk 里以 -- 开头的正文
 * 不能被误当文件头（那正是「删掉一行以 -- 开头」的常见字形）。
 */
function renderDiffText(text) {
  const rows = [];
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;
  const lines = String(text || '').split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  lines.forEach((line) => {
    if (line.startsWith('@@')) {
      const match = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldNo = Number(match[1]);
        newNo = Number(match[2]);
      }
      inHunk = true;
      rows.push(diffTextRow('hunk', '', '', line));
      return;
    }
    if (line.startsWith('\\')) {
      // "\ No newline at end of file"：没有行号，不推进任何一侧。
      rows.push(diffTextRow('meta', '', '', line));
      return;
    }
    if (!inHunk && (line.startsWith('---') || line.startsWith('+++'))) {
      // 文件头不进正文：两端的 ref 已经写在 diff 标题里，VS Code 的内联
      // 差异也不重复这两行。
      return;
    }
    if (line.startsWith('+')) {
      rows.push(diffTextRow('add', '', String(newNo), line));
      newNo += 1;
      return;
    }
    if (line.startsWith('-')) {
      rows.push(diffTextRow('del', String(oldNo), '', line));
      oldNo += 1;
      return;
    }
    rows.push(diffTextRow('ctx', String(oldNo), String(newNo), line));
    oldNo += 1;
    newNo += 1;
  });
  return `<div class="write-diff-text">${rows.join('')}</div>`;
}

function diffTextRow(kind, oldNo, newNo, line) {
  return `<div class="write-diff-row" data-kind="${kind}">`
    + `<span class="write-diff-num">${oldNo}</span>`
    + `<span class="write-diff-num">${newNo}</span>`
    + `<span class="write-diff-line">${escapeHtml(line) || ' '}</span>`
    + `</div>`;
}

async function commitVcs() {
  if (!S.workId) return;
  try {
    await flush();
    const message = $('writeVcsMessage').value.trim();
    await api(`/api/writing/works/${encodeURIComponent(S.workId)}/vcs/commit`,
      { method: 'POST', body: JSON.stringify({ message }) });
    $('writeVcsMessage').value = '';
    setStatus('已存档', 'ok');
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
  if (!window.confirm(`删除分支「${name}」？存档不会消失，只删掉这个入口。`)) return;
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
    '修正上一次存档：说明留空就沿用原文。\n工作区里没存档的改动也会一并带进去。',
    current);
  if (message === null) return;
  try {
    await flush();
    await api(vcsPath('amend'),
      { method: 'POST', body: JSON.stringify({ message }) });
    setStatus('已修正上一次存档', 'ok');
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
    setStatus('已暂存，工作区回到上次存档', 'ok');
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
  if (!window.confirm('只丢弃这一章的未存档改动？其他章与账本不动。')) return;
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
  if (!window.confirm('回滚到这次存档？正文与账本会一起恢复，历史不会删除。')) return;
  try {
    await flush();
    await api(`/api/writing/works/${encodeURIComponent(S.workId)}/vcs/restore`,
      { method: 'POST', body: JSON.stringify({ commit_id: commitId }) });
    setStatus('已回滚', 'ok');
    setView(diffReturnView());
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
  'cherry-pick': ['保留当前分支', '采用该存档的版本'],
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
  if (!window.confirm('撤销这次存档的改动？会留下一条反向存档，历史不删。')) return;
  await runVcsCommand('revert', 'revert', { commit_id: commitId },
    '已撤销这次存档');
}

async function pickVcsCommit(commitId) {
  if (!commitId) return;
  if (!window.confirm('把这次存档的改动摘到当前分支？')) return;
  await runVcsCommand('cherry-pick', 'cherry-pick', { commit_id: commitId },
    '已摘取这次存档');
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
    setView(diffReturnView());
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
  showOnlyView('merge');
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
    } else if (c.kind === 'file') {
      detail = `<div class="write-merge-previews">`
        + `<div><b>当前</b>${escapeHtml(c.ours_preview || '（这边删掉了）')}</div>`
        + `<div><b>对方</b>${escapeHtml(c.theirs_preview || '（那边删掉了）')}</div>`
        + '</div>';
    } else {
      detail = `<div class="vcs-meta">账本 · ${escapeHtml(c.collection || '')}`
        + ` · ${escapeHtml(c.label || c.id || '')}</div>`;
    }
    const title = c.kind === 'chapter'
      ? chapterLabel({ chapter_index: c.chapter_index, title: c.title })
      : (c.kind === 'file'
        ? `设定「${docLabel(c.label || c.path)}」`
        : `账本条目 ${c.id || ''}`);
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
    setView(diffReturnView());
    await selectWork(S.workId);
    await loadVcs();
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

function cancelVcsMerge() {
  S.mergeReq = null;
  setView(diffReturnView());
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
  const group = $('writeVcsReflogSection');
  // 「找回」就是开关这一组：已经摊着就收起，否则现取现开。
  if (group && !group.hidden && S.vcsOpen.reflog) {
    setVcsGroup('reflog', false);
    return;
  }
  try {
    const data = await api(vcsPath('reflog?limit=50'));
    S.vcsReflog = data.entries || [];
    renderVcsReflog();
    if (group) group.hidden = false;
    setVcsGroup('reflog', true);
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
  const reasons = { manual: '存档', auto: '自动存档', checkpoint: '检查点',
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
  refreshCharCount();
}

// 字数口径跟服务端一致（storage/writing_db 用 len(content)，换行也算一个），
// 这样底栏、章节列表和「已生成 · N 字」不会互相打架。程序改正文（切章、
// 应用修订、生成流）都要过这里，所以挂在 updateEditorState 上统一刷新。
function refreshCharCount() {
  const el = $('writeCharCount');
  if (!el) return;
  const ed = $('writeEditor');
  const n = ed ? ed.value.length : 0;
  el.textContent = `${n.toLocaleString('zh-CN')} 字`;
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
    refreshCharCount();   // 生成流不经过 input/updateEditorState，得单独刷
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
  // 新建章节的行内输入框长在正文目录里，先把导航与视图切回去。
  if (S.section !== 'work') setSection('work');
  if (!TEXT_VIEWS.has(S.view)) setView(S.textView || 'editor');
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

// ── 引用高亮：失焦之后仍然看得见选中段 ────────────────────────────
// <textarea> 一旦失焦，Chrome 就不再画选区；而引用流程恰恰要把光标送进
// 聊天框，于是「选中了什么」在正文里就消失了。这层镜像只负责把那段重新
// 涂上底色：文字透明，字体/行高/内边距/可用宽度照抄 textarea，换行才能
// 逐字对齐。它是纯装饰（pointer-events: none），错了也只是底色偏移。
let _veil = null;

function veilEls() {
  if (_veil) return _veil;
  const ta = $('writeEditor');
  if (!ta) return null;
  const host = document.createElement('div');
  const inner = document.createElement('div');
  host.className = 'write-veil';
  inner.className = 'write-veil-inner';
  host.appendChild(inner);
  document.body.appendChild(host);
  // 拖分隔条 / 换窗口大小时正文宽度会变，镜像要跟着重排。
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(layoutVeil).observe(ta);
  }
  _veil = { host, inner, ta };
  return _veil;
}

function layoutVeil() {
  const v = _veil;
  if (!v || !v.host.classList.contains('visible')) return;
  const { host, inner, ta } = v;
  const rect = ta.getBoundingClientRect();
  if (!rect.width || !rect.height || ta.offsetParent === null) {
    host.classList.remove('visible');
    return;
  }
  const cs = getComputedStyle(ta);
  host.style.left = `${rect.left + ta.clientLeft}px`;
  host.style.top = `${rect.top + ta.clientTop}px`;
  host.style.width = `${ta.clientWidth}px`;
  host.style.height = `${ta.clientHeight}px`;
  inner.style.fontFamily = cs.fontFamily;
  inner.style.fontSize = cs.fontSize;
  inner.style.fontWeight = cs.fontWeight;
  inner.style.fontStyle = cs.fontStyle;
  inner.style.lineHeight = cs.lineHeight;
  inner.style.letterSpacing = cs.letterSpacing;
  inner.style.padding = cs.padding;
  inner.style.textAlign = cs.textAlign;
  inner.style.transform = `translateY(${-ta.scrollTop}px)`;
}

function escHtml(s) {
  return s.replace(/[&<>]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

function showQuoteVeil(start, end) {
  const v = veilEls();
  if (!v || end <= start) return;
  const text = v.ta.value;
  v.inner.innerHTML = escHtml(text.slice(0, start))
    + `<mark>${escHtml(text.slice(start, end))}</mark>`
    + escHtml(text.slice(end));
  v.host.classList.add('visible');
  layoutVeil();
}

function hideQuoteVeil() {
  if (_veil) _veil.host.classList.remove('visible');
}
window.hideWriteQuoteVeil = hideQuoteVeil;

function bind() {
  // 顶部的「切换作品」只管换书；下面两个 tab 只管切视图。
  $('writeWorkspaceBtn').onclick = toggleWorkspaceMenu;
  $('writeNavWork').onclick = () => setSection('work');
  $('writeNavVcs').onclick = () => setSection('vcs');
  $('writeNavWork').addEventListener('contextmenu', (event) => {
    const work = S.works.find((w) => w.id === S.workId);
    if (!work) return;
    event.preventDefault();
    event.stopPropagation();
    closeWorkspaceMenu();
    $('writeNavWork').focus();
    openWorkContextMenu(event.clientX, event.clientY, work, true);
  });
  $('writeWorkspaceNew').onclick = () => {
    closeWorkspaceMenu();
    createWork();
  };
  $('writeNewWorkBtn').onclick = () => {
    closeWorkspaceMenu();
    createWork();
  };
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
    // 下拉现在挂在「切换作品」那一行里：点导航 tab / 树 / 主区都算离开。
    const bar = document.querySelector('.write-workspace-bar');
    if (bar && !bar.contains(event.target)) closeWorkspaceMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && S.workspaceOpen) {
      event.preventDefault();
      closeWorkspaceMenu(true);
    }
  });
  $('writeGenBtn').onclick = generate;
  // 素材卡是原料，不是正典：页面可以直接改正文；作品设定/正文仍只由助手写。
  $('writeDocEditBtn').onclick = () => {
    if (S.libRow && !S.libEditing) startLibraryEdit();
  };
  $('writeDocAddBtn').onclick = addCardToSession;
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
  // 圈正文 → 引用条（阅读页那套「选中再引用」在编辑器上的版本）。
  // 编辑器是 <textarea>，window.getSelection() 拿不到它的选区，只能读
  // selectionStart/End。只在**真的有选区**时更新引用条，不自动落盘：
  // 发送前 chat.js 会复核这段文字还在不在正文里。
  const syncQuote = () => {
    const ed = $('writeEditor');
    if (!ed || ed.selectionEnd <= ed.selectionStart) return false;
    const text = ed.value.slice(ed.selectionStart, ed.selectionEnd).trim();
    if (text.length < 2) return false;
    if (typeof window.setWritingSelection !== 'function') return false;
    window.setWritingSelection(text);
    return true;
  };
  // 拖选过程中会连续触发 select：只更新引用条，不动焦点，
  // 否则拖到一半光标跑掉，选不完整。
  $('writeEditor').addEventListener('select', syncQuote);
  // 松手之后：有选区 = 引用它，并把光标送进聊天框（可以直接打字）；
  // 没有选区 = 这一下点在选中段以外，选中既已取消，引用一起取消。
  $('writeEditor').addEventListener('mouseup', () => {
    setTimeout(() => {
      const ed = $('writeEditor');
      if (syncQuote()) {
        // 焦点马上要交给聊天框，选区高亮会跟着消失——先把它画到镜像上。
        showQuoteVeil(ed.selectionStart, ed.selectionEnd);
        const input = document.getElementById('chatInput');
        if (input) input.focus();
      } else if (typeof window.clearWritingSelection === 'function') {
        window.clearWritingSelection();
      }
    }, 0);
  });
  // 回到正文里，浏览器自己的选区高亮又回来了，镜像让位。
  $('writeEditor').addEventListener('focus', hideQuoteVeil);
  $('writeEditor').addEventListener('scroll', layoutVeil);
  window.addEventListener('resize', layoutVeil);
  // 写作台里点到正文以外的区域（章节树、工具条、标题框）= 取消选中。
  // 聊天面板不在此列（它在 #page-write 之外）：光标得待在那边才谈得上
  // 引用；同理，点写作台以外的地方也不取消。
  const writePage = document.getElementById('page-write');
  if (writePage) {
    writePage.addEventListener('mousedown', (event) => {
      if (event.target.closest && event.target.closest('#writeEditor')) return;
      if (typeof window.clearWritingSelection === 'function') {
        window.clearWritingSelection();
      }
    });
  }
  $('writeEditor').addEventListener('blur', () => {
    autosaveNow();
    // 键盘选出来的段落在失焦时同样要补画（鼠标路径已在 mouseup 里画过）。
    const ed = $('writeEditor');
    if (ed && ed.selectionEnd > ed.selectionStart) {
      showQuoteVeil(ed.selectionStart, ed.selectionEnd);
    }
  });
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
  $('writeReviewBtn').onclick = openReview;
  $('writeTimelineClose').onclick = closeTimeline;
  $('writeSideClose').onclick = () => $('writeSide').classList.remove('open');
  // 写作 git：左栏分组自己管开合，开合状态记在本地。
  S.vcsOpen = readVcsGroups();
  document.querySelectorAll('#writeVcsRail .write-vcs-group-head')
    .forEach((head) => {
      head.onclick = () => {
        const group = head.closest('.write-vcs-group');
        const key = group ? group.dataset.group : '';
        setVcsGroup(key, !S.vcsOpen[key]);
      };
    });
  applyVcsGroups();
  $('writeVcsCommitBtn').onclick = commitVcs;
  $('writeVcsAmendBtn').onclick = amendVcs;
  $('writeVcsNewBranch').onclick = createVcsBranch;
  $('writeVcsStashBtn').onclick = stashVcs;
  $('writeVcsTagBtn').onclick = createVcsTag;
  $('writeVcsReflogBtn').onclick = toggleVcsReflog;
  $('writeVcsBranchSelect').onchange = (event) => {
    checkoutVcsBranch(event.target.value);
  };
  // 头部的分支徽章是版本控制的第二个入口（第一个是左栏药丸）。
  $('writeVcsBadge').onclick = () => setSection('vcs');
  $('writeVcsAllBranches').onchange = (event) => {
    S.logAllBranches = event.target.checked;
    loadVcs();
  };
  $('writeVcsOnlyChapter').onchange = (event) => {
    S.logOnlyChapter = event.target.checked;
    loadVcs();
  };
  $('writeDiffClose').onclick = () => setView(diffReturnView());
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
    const doc = event.target.closest('[data-doc-path]');
    if (doc && doc.dataset.docPath) {
      // 版本控制里点一份文件 = 看它改了什么：中间栏只渲染这一份的逐行差异。
      // 看正文走文件区（点文件名），两个动作不要抢同一个点击。
      openVcsDiff('HEAD', 'WORK', '', doc.dataset.docPath);
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
    // 清单里的「查看全部改动」：章级差异 + 账本，还走原来的整条提交 diff。
    const all = event.target.closest('[data-commit-all]');
    if (all) {
      const commit = S.vcsLog.find((c) => c.id === all.dataset.expandCommit);
      if (commit) openVcsDiff(commit.parent_id || 'EMPTY', commit.id, '');
      return;
    }
    // 清单里的具体文件/章：中间栏只渲染这一份，带逐行高亮。
    const item = event.target.closest('[data-expand-commit]');
    if (item && (item.dataset.diffChapter || item.dataset.diffFile)) {
      const commit = S.vcsLog.find((c) => c.id === item.dataset.expandCommit);
      if (!commit) return;
      openVcsDiff(commit.parent_id || 'EMPTY', commit.id,
                  item.dataset.diffChapter || '',
                  item.dataset.diffFile || '');
      return;
    }
    const row = event.target.closest('[data-commit]');
    if (!row) return;
    toggleCommitFiles(row.dataset.commit);
  });
  $('writeDiffBody').addEventListener('click', (event) => {
    const row = event.target.closest('[data-diff-chapter]');
    if (row) openVcsDiff(S.diffFrom, S.diffTo, row.dataset.diffChapter);
  });
  document.querySelectorAll('.write-view-tab').forEach((tab) => {
    tab.onclick = () => setView(tab.dataset.view);
  });
  const chatLog = $('chatLog');
  if (chatLog) chatLog.addEventListener('click', onWriteFileLinkClick);
}

async function open() {
  try {
    await loadWorks();
    // 写作会话绑在作品上：这条线本日的会话属于哪一本，就回到哪一本
    //（空项目会话的 workId 是空串，也要照原样回到空项目）。
    const line = window.tabSessionFor ? window.tabSessionFor('write') : null;
    const lineWork = line ? line.workId : undefined;
    if (lineWork !== undefined && lineWork !== null) {
      if (!lineWork) {
        if (S.workId || !S.emptyProject) {
          await selectEmptyProject({ quiet: true });
        } else {
          await loadLibrary();
          renderWorks();
        }
        return;
      }
      if (S.works.some((w) => w.id === lineWork)) {
        if (S.workId !== lineWork) await selectWork(lineWork);
        return;
      }
    }
    // 作者上次选的是「空项目」就留着——别用第一本把它顶掉。
    if (!S.works.length) {
      S.emptyProject = true;
      await loadLibrary();
      renderWorks();
    } else if (!S.workId && !S.emptyProject) {
      await selectWork(S.works[0].id);
    } else if (!S.workId) {
      await loadLibrary();
      renderWorks();
    }
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

/** 从会话面板点开某本作品的对话时，把写作台切到那一本（不改会话线）。 */
async function openWork(workId = '') {
  if (!S.works.length) {
    try { await loadWorks(); } catch (_) { return; }
  }
  if (!workId) {
    if (S.workId || !S.emptyProject) {
      await selectEmptyProject({ quiet: true });
    }
    return;
  }
  if (S.workId === workId) return;
  if (!S.works.some((w) => w.id === workId)) return;
  await selectWork(workId);
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
    if (!S.workId && !S.emptyProject && S.works.length) {
      await selectWork(S.works[0].id);
      return;
    }
    if (!S.workId) {
      // 空项目：助手这一轮可能刚收录了素材，先把库对齐。
      await loadLibrary();
      if (S.libPath) await openLibraryCard(S.libPath, { quiet: true });
      renderWorks();
      return;
    }
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
    // 助手这一轮可能刚写了合同/设定、收录了素材：当场刷新。
    await Promise.all([loadDocs(), loadContracts(), loadLibrary()]);
    if (S.libPath) await openLibraryCard(S.libPath, { quiet: true });
  } catch (err) {
    setStatus(err.message, 'error');
  }
}

window.WritingUI = { open, context, refresh, refreshStatus, flush, openWork,
                     setStyleRef: setStyleAuthor,
                     refreshLibrary, syncCookSuggest, runCookSuggest };
bind();

