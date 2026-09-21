/* Book Companion — bookshelf: loading, sorting, import, delete. */

import { S } from './state.js';
import { escapeHtml, escapeHtmlAttr, compareStringAsc } from './utils.js';
import { switchTab, saveViewState } from './app.js';
import { selectBook } from './reader.js';

// ── Sort persistence ──────────────────────────────────────────────

const SHELF_SORT_DEFAULTS = { key: 'time', dir: 'asc' };

export function loadShelfSort() {
  try {
    const raw = localStorage.getItem('bc_shelf_sort');
    if (raw) {
      const parsed = JSON.parse(raw);
      if (['time', 'title', 'author'].includes(parsed.key) && ['asc', 'desc'].includes(parsed.dir)) {
        S.shelfSort = { key: parsed.key, dir: parsed.dir };
      }
    }
  } catch (e) {}
}

function saveShelfSort() {
  try { localStorage.setItem('bc_shelf_sort', JSON.stringify(S.shelfSort)); } catch (e) {}
}

export function sortBooks(books) {
  const sorted = [...books];
  const { key, dir } = S.shelfSort;
  const sign = dir === 'asc' ? 1 : -1;

  sorted.sort((a, b) => {
    if (key === 'time') {
      const ta = a.time_created || 0;
      const tb = b.time_created || 0;
      return sign * (ta - tb);
    }
    if (key === 'title') {
      return sign * compareStringAsc((a.title || '').trim(), (b.title || '').trim());
    }
    if (key === 'author') {
      return sign * compareStringAsc((a.author || '').trim(), (b.author || '').trim());
    }
    return 0;
  });

  return sorted;
}

export function updateShelfSortUI() {
  document.querySelectorAll('#shelfSortBar .sort-btn').forEach(btn => {
    const key = btn.dataset.key;
    const dirSpan = document.getElementById('sort-dir-' + key);
    if (key === S.shelfSort.key) {
      btn.classList.add('active');
      dirSpan.innerHTML = S.shelfSort.dir === 'asc' ? '<i data-lucide="chevron-up"></i>' : '<i data-lucide="chevron-down"></i>';
    } else {
      btn.classList.remove('active');
      dirSpan.textContent = '';
    }
  });
}

export function setSort(key) {
  if (S.shelfSort.key === key) {
    S.shelfSort.dir = S.shelfSort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    S.shelfSort.key = key;
    S.shelfSort.dir = 'asc';
  }
  saveShelfSort();
  updateShelfSortUI();
  loadShelf();
}

// ── Scene (day / night) ───────────────────────────────────────────

export function loadShelfScene() {
  try {
    const v = localStorage.getItem('bc_shelf_scene');
    if (v === 'day' || v === 'night') S.shelfScene = v;
  } catch (e) {}
  applyShelfScene();
  // 指针倾斜只需绑定一次（用事件委托，卡片换了也不用重绑）
  initShelfTilt();
}

export function toggleShelfScene() {
  S.shelfScene = S.shelfScene === 'day' ? 'night' : 'day';
  try { localStorage.setItem('bc_shelf_scene', S.shelfScene); } catch (e) {}
  applyShelfScene();
}

// ── Day-theme sticker pool ───────────────────────────────────────
// Random pick on launch and every switch to day theme.
const STICKER_POOL = [
  'assets/journal-corner.png',
  'assets/stickers/summer-lotus.png',
  'assets/stickers/summer-watermelon.png',
  'assets/stickers/summer-window.png',
];

function randomizeSticker() {
  const img = document.getElementById('journalSticker');
  if (!img) return;
  // Avoid repeating the current sticker on re-roll.
  const pool = STICKER_POOL.filter(s => !img.src.endsWith(s));
  const pick = pool[Math.floor(Math.random() * pool.length)];
  img.src = pick;
}

function applyShelfScene() {
  const day = S.shelfScene === 'day';
  // Global day/night theme applies app-wide via data-theme on <html>.
  document.documentElement.dataset.theme = S.shelfScene;
  // Shelf scene (book-wall lighting) only exists on the shelf page.
  const scene = document.getElementById('shelfScene');
  if (scene) {
    scene.classList.toggle('scene-day', day);
    scene.classList.toggle('scene-night', !day);
  }
  const btn = document.getElementById('sceneToggle');
  if (btn) {
    btn.innerHTML = `<i data-lucide="${day ? 'moon' : 'sun'}"></i>`;
    btn.title = day ? '切换到夜晚' : '切换到白天';
  }
  // Random sticker on day theme (launch + switch).
  if (day) randomizeSticker();
}

// ── Sticky head compact on scroll ────────────────────────────────

export function initStickyCompact() {
  const pageHome = document.getElementById('page-home');
  const stickyHead = document.querySelector('.shelf-sticky-head');
  if (!pageHome || !stickyHead) return;
  // Trigger = head pinned to the scroll-container top (headTop == homeTop).
  // Two safeguards against the flicker loop that compacting the head height
  // used to cause:
  //  (1) #page-home sets overflow-anchor: none in CSS, so the browser no
  //      longer auto-adjusts scrollTop to compensate for the height change
  //      compact introduces - that auto-adjust was what re-coupled the
  //      trigger to its own side effect.
  //  (2) Hysteresis: enter compact when the head pins, exit only after it
  //      lifts EXIT px off the top, so single-pixel jitter at the boundary
  //      can't toggle the class.
  const ENTER = 1;    // pin threshold (px from homeTop)
  const EXIT  = 10;   // must lift this far to leave compact
  function update() {
    const delta = stickyHead.getBoundingClientRect().top
                - pageHome.getBoundingClientRect().top;
    const isCompact = stickyHead.classList.contains('compact');
    if (isCompact) {
      if (delta > EXIT) stickyHead.classList.remove('compact');
    } else {
      if (delta <= ENTER) stickyHead.classList.add('compact');
    }
  }
  let ticking = false;
  pageHome.addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () { update(); ticking = false; });
  }, { passive: true });
  update();  // initial check for restored scroll position
}

// ── Shelf rendering (wooden-plank scene) ─────────────────────────

// Deterministic hash → stable pseudo-random tilt/size per book across renders.
function bookHash(id) {
  let h = 0;
  const s = String(id);
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function bookStyleVars(b) {
  const h = bookHash(b.id);
  const size = 0.92 + (h % 17) / 100;                 // 0.92 – 1.08
  const tilt = (((h >> 4) % 9) - 4) / 2;              // -2.0 – 2.0 deg, .5 steps
  const ratio = 0.68 + ((h >> 8) % 13) / 100;         // 0.68 – 0.80 width/height
  return `--size:${size.toFixed(2)};--tilt:${tilt}deg;--ratio:${ratio.toFixed(2)};`;
}

function currentPerRow() {
  const grid = document.getElementById('shelfGrid');
  const w = grid && grid.clientWidth ? grid.clientWidth : 1000;
  return Math.max(3, Math.min(6, Math.floor(w / 180)));
}

function sceneBookHtml(b, idx) {
  const coverUrl = S.API + '/api/books/' + b.id + '/cover';
  const title = escapeHtml(b.title);
  const author = escapeHtml(b.author || '');
  const initials = escapeHtml(b.title.slice(0, 4));
  const ribbon = b.last_read_at ? '<span class="book-ribbon"></span>' : '';
  const delay = Math.min(idx * 45, 900);
  return `
      <div class="scene-book" style="${bookStyleVars(b)}--d:${delay}ms" onclick="openBookFromShelf('${b.id}')" title="${title}">
        <div class="scene-book-lift">
        ${ribbon}
        <div class="scene-book-inner">
          <img src="${coverUrl}" alt="${title}" loading="lazy"
               onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
          <div class="book-cover-placeholder" style="display:none"><span>${initials}</span></div>
          <i class="book-glare"></i>
          <div class="scene-book-scrim">
            <div class="scrim-title">${title}</div>
            <div class="scrim-author">${author}</div>
          </div>
          <button class="book-graph-btn" onclick="event.stopPropagation();openGraphPanel('${b.id}','${title.replace(/'/g, "\\'")}')" title="图谱分析"><i data-lucide="network"></i></button>
          <button class="book-tag-btn" onclick="event.stopPropagation();openTagPanel('${b.id}','${title.replace(/'/g, "\\'")}')" title="章节标注"><i data-lucide="sticky-note"></i></button>
          <button class="shelf-delete-btn" onclick="event.stopPropagation();deleteBook('${b.id}')" title="删除此书"><i data-lucide="x"></i></button>
        </div>
        </div>
      </div>`;
}

/* ══ 跟着指针转的封面 ═══════════════════════════════════════════

   设计约束：指针静止时，不得有任何东西在跑。
   · 静止即零开销 —— 没有 rAF 循环、没有 CSS 动画、没有样式写入。
     21 本书停在那里就是 21 个静止的节点。
   · JS 只给「指针所在的那一张卡」逐帧写两个无单位数值（--tx/--ty），
     角度、透视、阴影的换算全在 CSS 里。
   · 命中层 .scene-book 的几何完全不动（它只负责接指针）。书架卡片小，
     一旦让命中层自己位移，指针就会掉出去、反复触发进入/离开而抖动。

   姿态怎么读：指针越靠右，书越向右转；指针越靠上，书上缘越向后仰。
   也就是书在「躲」指针，像被手指按住一角推了一下。 */
const TILT_EASE = 10;      // 缓动速率（1/秒）：越大越跟手，越小越有重量

// 静止姿态对应的 --ty 值。含义是「略微后仰」，让书有立着的体积感。
// 必须与 style.css 里 var(--ty, -0.21) 的兜底值完全相等 ——
// 两边不一致时，指针离开的瞬间书会跳一下（差值 0.21 相当于 2.5°）。
// 因为两边相等，收敛后可以直接删掉内联值，渲染结果不变。
const TY_REST = -0.21;
const TILT_EPS = 0.004;    // 收敛阈值：小于它就直接吸附到静止姿态

let _tiltCards = [];       // 当前这批卡片的控制器
let _tiltActive = null;    // 正在被指针驱动的那一张
let _tiltRaf = 0;          // 待执行帧的 id
let _tiltRunning = false;  // 循环是否在跑 —— 必须与 id 分开存

// 减弱动效偏好：只关掉「运动」，不影响静态外观。
// 全局 @media (prefers-reduced-motion) 规则管不到 JS 写的内联样式，
// 所以必须在这里自己判断。
let _reducedMotion = false;
try {
  const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
  _reducedMotion = mq.matches;
  const onMq = e => { _reducedMotion = e.matches; if (e.matches) releaseAllTilt(); };
  if (mq.addEventListener) mq.addEventListener('change', onMq);
  else if (mq.addListener) mq.addListener(onMq);          // 老 Safari 兜底
} catch (e) {}

// 用独立的布尔量表示「循环在跑」，不要拿 rAF 返回的 id 兼作标志位：
// 那个 id 在循环自然结束、被取消、或标签页切换交错时都可能残留非零值，
// 于是 `if (!rafId)` 这类守卫会永久拒绝重启循环。
function startTiltLoop() {
  if (_tiltRunning) return;
  _tiltRunning = true;
  tiltTick._last = performance.now();
  _tiltRaf = requestAnimationFrame(tiltTick);
}

function stopTiltLoop() {
  _tiltRunning = false;
  if (_tiltRaf) { cancelAnimationFrame(_tiltRaf); _tiltRaf = 0; }
  tiltTick._last = 0;
}

function tiltTick(now) {
  const dt = tiltTick._last ? Math.min((now - tiltTick._last) / 1000, 0.05) : 0;
  tiltTick._last = now;
  const k = 1 - Math.exp(-dt * TILT_EASE);   // 指数缓动：与帧率无关

  let busy = false;
  for (const c of _tiltCards) {
    // 已收敛到静止姿态（且内联值已清掉），跳过
    if (!c.on && c.tx === 0 && c.ty === TY_REST) continue;
    busy = true;
    c.tx += (c.ttx - c.tx) * k;
    c.ty += (c.tty - c.ty) * k;

    if (!c.on && Math.abs(c.tx) < TILT_EPS && Math.abs(c.ty - TY_REST) < TILT_EPS) {
      // 收敛：清掉内联值。因为 CSS 里 var(--ty, -0.21) 的兜底值
      // 就是这个静止姿态，删掉内联值渲染结果完全一致 ——
      // 于是静止时既零内联样式、又没有任何跳变。
      c.tx = 0; c.ty = TY_REST;
      c.inner.style.removeProperty('--tx');
      c.inner.style.removeProperty('--ty');
      continue;
    }
    c.inner.style.setProperty('--tx', c.tx.toFixed(3));
    c.inner.style.setProperty('--ty', c.ty.toFixed(3));
  }

  if (busy) {
    _tiltRaf = requestAnimationFrame(tiltTick);
  } else {
    _tiltRunning = false;
    _tiltRaf = 0;
    tiltTick._last = 0;
  }
}
tiltTick._last = 0;

// 指针离开这张卡：目标回到静止姿态，剩下的交给缓动慢慢收回。
function releaseTilt(c) {
  if (!c || !c.on) return;
  c.on = false;
  c.ttx = 0;
  c.tty = TY_REST;          // 不是 0 —— 0 会让书在离开时跳一下（见 TY_REST 注释）
  startTiltLoop();          // 确保循环在跑，好把姿态收回去
}

function releaseAllTilt() {
  const c = _tiltActive;
  _tiltActive = null;
  if (c) releaseTilt(c);
}

// 每次重新渲染书架都要重新绑定：DOM 被整体替换，旧控制器里的引用已失效。
function bindShelfTilt(grid) {
  stopTiltLoop();           // 旧循环持有的是废弃引用，先干净停掉
  _tiltActive = null;
  // 新渲染出来的卡片没有内联值，实际姿态就是 CSS 的静止姿态，
  // 所以控制器初值直接用 (0, TY_REST) —— 与画面一致，不会有一帧错位。
  _tiltCards = [...grid.querySelectorAll('.scene-book')].map(el => ({
    el,
    inner: el.querySelector('.scene-book-inner'),
    tx: 0, ty: TY_REST,     // 当前姿态
    ttx: 0, tty: TY_REST,   // 目标姿态
    on: false,              // 指针是否在这张卡上
  })).filter(c => c.inner);
}

function initShelfTilt() {
  const scene = document.getElementById('shelfScene');
  if (!scene || scene._tiltBound) return;
  scene._tiltBound = true;

  scene.addEventListener('pointermove', e => {
    if (_reducedMotion) return;
    // 触屏没有「悬停」语义：手指按住不放时指针不会离开，
    // 姿态会永久卡在最后一次的位置。直接不接管。
    if (e.pointerType === 'touch') return;

    const el = e.target && e.target.closest ? e.target.closest('.scene-book') : null;
    if (!el) { releaseAllTilt(); return; }

    const c = _tiltCards.find(x => x.el === el);
    if (!c) return;

    // 指针快速划过时可能跳过上一张卡的离开事件 —— 主动释放
    if (_tiltActive && _tiltActive !== c) releaseAllTilt();
    _tiltActive = c;

    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    c.ttx = ((e.clientX - r.left) / r.width - 0.5) * 2;   // -1 … 1
    c.tty = ((e.clientY - r.top) / r.height - 0.5) * 2;
    c.on = true;
    startTiltLoop();
  }, { passive: true });

  scene.addEventListener('pointerleave', releaseAllTilt, { passive: true });
  scene.addEventListener('pointercancel', releaseAllTilt, { passive: true });

  // 切到阅读页、或切到别的浏览器标签时，pointerleave 不一定触发，
  // 会留下一个永远「on」的卡片让循环空转。书架整体不可见时主动收手。
  if ('IntersectionObserver' in window) {
    const grid = document.getElementById('shelfGrid');
    if (grid) {
      new IntersectionObserver(es => {
        if (es.some(en => !en.isIntersecting)) releaseAllTilt();
      }).observe(grid);
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) releaseAllTilt();
  });
}

let _lastShelfBooks = null;
let _lastPerRow = 0;

function renderShelfRows(books) {
  const grid = document.getElementById('shelfGrid');
  const perRow = currentPerRow();
  _lastShelfBooks = books;
  _lastPerRow = perRow;
  const rows = [];
  for (let i = 0; i < books.length; i += perRow) rows.push(books.slice(i, i + perRow));
  let idx = 0;
  grid.innerHTML = rows.map(rowBooks => `
    <div class="shelf-row">
      <div class="shelf-books">
${rowBooks.map(b => sceneBookHtml(b, idx++)).join('')}
      </div>
      <div class="shelf-plank"><i class="plank-bracket pb-l"></i><i class="plank-bracket pb-r"></i></div>
      <div class="shelf-plank-shadow"></div>
    </div>`).join('');

  // DOM 整体换掉了，指针倾斜的控制器必须跟着重建
  bindShelfTilt(grid);
}

// Re-group rows when the window resizes enough to change books-per-row.
let _shelfResizeTimer = null;
window.addEventListener('resize', () => {
  if (!_lastShelfBooks) return;
  if (_shelfResizeTimer) clearTimeout(_shelfResizeTimer);
  _shelfResizeTimer = setTimeout(() => {
    if (currentPerRow() !== _lastPerRow) renderShelfRows(_lastShelfBooks);
  }, 200);
});

export async function loadShelf() {
  try {
    const resp = await fetch(S.API + '/api/books');
    const data = await resp.json();
    if (!data.books || !data.books.length) {
      document.getElementById('shelfEmpty').style.display = 'block';
      const grid = document.getElementById('shelfGrid');
      grid.innerHTML = '';
      // 卡片被清空了，倾斜控制器必须跟着清 —— 否则 _tiltCards 里留着
      // 已摘除的节点引用，正在跑的 rAF 循环会继续往幽灵节点上写样式。
      bindShelfTilt(grid);
      document.getElementById('continueSection').style.display = 'none';
      _lastShelfBooks = null;
      return;
    }
    document.getElementById('shelfEmpty').style.display = 'none';
    document.getElementById('shelfCount').textContent = `(${data.books.length}本)`;

    // Continue reading section
    const lastRead = [...data.books]
      .filter(b => b.last_read_at)
      .sort((a, b) => b.last_read_at - a.last_read_at)[0] || null;
    const continueSection = document.getElementById('continueSection');
    if (lastRead) {
      continueSection.style.display = 'block';
      document.getElementById('continueCover').src = S.API + '/api/books/' + lastRead.id + '/cover';
      document.getElementById('continueTitle').textContent = lastRead.title;
      document.getElementById('continueAuthor').textContent = lastRead.author || '';
      const progress = lastRead.last_chapter != null
        ? `第${lastRead.last_chapter}章 · 第${lastRead.last_chunk + 1}段`
        : '尚未开始阅读';
      document.getElementById('continueProgress').textContent = progress;
      document.getElementById('continueCard').onclick = function () { openBookFromShelf(lastRead.id); };
    } else {
      continueSection.style.display = 'none';
    }

    // Shelf scene rows
    renderShelfRows(sortBooks(data.books));
  } catch (e) {
    console.error(e);
    document.getElementById('shelfEmpty').style.display = 'block';
  }
}

export function openBookFromShelf(bookId) {
  switchTab('reader', true);              // skipLoad: 避免与下面的 selectBook 竞态
  S.currentBook = bookId;
  loadBookDropdown().then(() => {
    const sel = document.getElementById('bookSelect');
    if (sel.querySelector(`option[value="${bookId}"]`)) {
      sel.value = bookId;
    }
    selectBook();
    saveViewState();
  });
}

export async function loadBookDropdown() {
  try {
    const resp = await fetch(S.API + '/api/books');
    const data = await resp.json();
    if (!data.books || !data.books.length) return;
    const sel = document.getElementById('bookSelect');
    sel.innerHTML = data.books.map(b =>
      `<option value="${b.id}">${b.title.slice(0, 35)} (${b.chapter_count}章)</option>`
    ).join('');
  } catch (e) { /* ignore */ }
}

export async function refreshBooks() {
  const keepBook = S.currentBook;          // 记住当前书籍
  await loadBookDropdown();               // 重建 <select>（会丢失选中值）
  if (keepBook) {
    const sel = document.getElementById('bookSelect');
    // 仅当下拉框中存在该书时才恢复（书可能已被删除）
    if (sel.querySelector(`option[value="${keepBook}"]`)) {
      sel.value = keepBook;
    }
  }
  if (document.getElementById('page-reader').classList.contains('active')) {
    selectBook();
  }
}

// ── Book delete ───────────────────────────────────────────────────

export async function deleteBook(bookId) {
  if (!confirm('确定要删除这本书吗？\n\n注释不会丢失——重新导入后会自动恢复。')) return;
  try {
    const resp = await fetch(S.API + '/api/books/' + bookId, { method: 'DELETE' });
    const data = await resp.json();
    if (data.deleted) {
      const orphaned = data.annotations_orphaned || 0;
      alert(`已删除《${data.title}》\n${orphaned ? orphaned + ' 条注释已保留，重导后恢复' : ''}`);
      loadShelf();
    }
  } catch (e) { alert('删除失败: ' + e.message); }
}

// ── Book import ───────────────────────────────────────────────────

export function toggleImportPanel() {
  const panel = document.getElementById('importPanel');
  if (!panel) return;
  panel.classList.toggle('visible');
}

function getScanFolder() {
  try { return localStorage.getItem('bc_scan_folder') || ''; } catch (e) { return ''; }
}

function setScanFolder(folder) {
  try { localStorage.setItem('bc_scan_folder', folder); } catch (e) {}
}

function renderScanResult(data, folder) {
  const body = document.getElementById('importPopupBody');
  if (data.error) {
    body.innerHTML = '<div style="color:#ff6b6b;text-align:center;padding:var(--space-lg)">' + escapeHtml(data.error) + '</div>';
    return;
  }
  if (!data.unimported || !data.unimported.length) {
    body.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:var(--space-lg)">'
      + '<i data-lucide="circle-check"></i> ' + escapeHtml(folder) + '<br>所有文件已入库 (' + data.total_files + ' 本)</div>';
    return;
  }
  let html = '<div style="font-size:var(--fs-caption);color:var(--text-muted);margin-bottom:8px">'
    + data.unimported.length + ' 本未入库（共 ' + data.total_files + ' 本）</div>';
  data.unimported.forEach(f => {
    html += '<div class="import-file-item" onclick="importSingleFile(\'' + escapeHtmlAttr(f.path) + '\')" title="点击导入">'
      + '<span class="ifi-name">' + escapeHtml(f.name) + '</span>'
      + '<span class="ifi-meta">.' + f.format + ' · ' + f.size_mb + 'MB</span>'
      + '</div>';
  });
  body.innerHTML = html;
}

async function scanFolder(folder) {
  const body = document.getElementById('importPopupBody');
  body.innerHTML = '<div style="color:var(--text-muted);font-size:var(--fs-caption);text-align:center;padding:var(--space-lg)">扫描中: ' + escapeHtml(folder) + '</div>';
  try {
    const resp = await fetch(S.API + '/api/books/scan-folder?path=' + encodeURIComponent(folder));
    const data = await resp.json();
    if (data.error) {
      if (resp.status === 404) {
        // 文件夹不可用 → 清掉记忆，下次点击按钮可重新输入
        try { localStorage.removeItem('bc_scan_folder'); } catch (e) {}
        body.innerHTML = '<div style="color:#ff6b6b;text-align:center;padding:var(--space-lg)">'
          + escapeHtml(folder) + '<br>不存在或不可访问。<br>'
          + '<span style="color:var(--text-muted);font-size:var(--fs-caption)">点击下方按钮重新输入文件夹</span></div>';
      } else {
        body.innerHTML = '<div style="color:#ff6b6b;text-align:center;padding:var(--space-lg)">' + escapeHtml(data.error) + '</div>';
      }
      return;
    }
    renderScanResult(data, data.folder || folder);
  } catch (e) {
    body.innerHTML = '<div style="color:#ff6b6b;text-align:center;padding:var(--space-lg)">扫描失败: ' + e.message + '</div>';
  }
}

export async function scanAndShowFiles() {
  const body = document.getElementById('importPopupBody');
  const remembered = getScanFolder();
  if (remembered) {
    await scanFolder(remembered);
    return;
  }
  // 无本地记忆 → 先试后端配置的默认卸货区（不传 path），没有再引导输入
  try {
    const probe = await fetch(S.API + '/api/books/scan-folder');
    if (probe.status === 200) {
      const data = await probe.json();
      renderScanResult(data, data.folder || '(默认卸货区)');
      return;
    }
  } catch (e) { /* 网络失败 → 落到 prompt 兜底 */ }

  const folder = window.prompt('输入待扫描文件夹路径（存放未入库 EPUB/MOBI 的目录）：');
  if (!folder) {
    body.innerHTML = '<div style="color:var(--text-muted);font-size:var(--fs-caption);text-align:center;padding:var(--space-lg)">已取消扫描，点击下方按钮重新扫描。</div>';
    return;
  }
  setScanFolder(folder);
  await scanFolder(folder);
}

export async function importSingleFile(filePath) {
  const body = document.getElementById('importPopupBody');
  body.innerHTML = '<div style="color:var(--accent);text-align:center;padding:var(--space-lg)">导入中...</div>';
  try {
    const resp = await fetch(S.API + '/api/books/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath })
    });
    const data = await resp.json();
    if (data.error) {
      body.innerHTML = '<div style="color:#ff6b6b;text-align:center;padding:var(--space-lg)">导入失败: ' + data.error + '</div>';
      return;
    }
    loadShelf();
    scanAndShowFiles();
  } catch (e) {
    body.innerHTML = '<div style="color:#ff6b6b;text-align:center;padding:var(--space-lg)">导入失败: ' + e.message + '</div>';
  }
}

// ── Watch ─────────────────────────────────────────────────────────

export async function scanWatch() {
  try {
    const resp = await fetch(S.API + '/api/watch/scan', { method: 'POST' });
    const data = await resp.json();
    const log = document.getElementById('watchLog');
    log.innerHTML = data.events ? data.events.slice(-10).map(e =>
      `<div class="watch-event">${e}</div>`
    ).join('') : '<div>扫描完成</div>';
    loadShelf();
  } catch (e) {}
}

export async function refreshWatchStatus() {
  try {
    const resp = await fetch(S.API + '/api/watch/status');
    const data = await resp.json();
    document.getElementById('watchIndicator').innerHTML = data.watching ? '<i data-lucide="circle-dot" style="color:#4caf50"></i>' : '<i data-lucide="circle"></i>';
    document.getElementById('watchDir').textContent = data.folder || '—';
  } catch (e) {}
}

// ── HTML onclick access ───────────────────────────────────────────
window.openBookFromShelf = openBookFromShelf;
window.deleteBook = deleteBook;
window.toggleImportPanel = toggleImportPanel;
window.scanAndShowFiles = scanAndShowFiles;
window.importSingleFile = importSingleFile;
window.setSort = setSort;
window.loadShelf = loadShelf;
window.toggleShelfScene = toggleShelfScene;
window.loadShelfScene = loadShelfScene;
window.initStickyCompact = initStickyCompact;

// ── Debug hook ────────────────────────────────────────────────────
// Read-only view of the tilt state machine. Exists so verification can
// assert against real state instead of inferring it from screenshots —
// the "loop stopped restarting" class of bug is invisible in a screenshot.
window.__shelfTilt = {
  get cards() { return _tiltCards; },
  get running() { return _tiltRunning; },
  get active() { return _tiltActive; },
};
