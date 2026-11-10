/* Liubai — bookshelf: loading, sorting, import, delete. */

import { S } from './state.js';
import { escapeHtml, escapeHtmlAttr, compareStringAsc } from './utils.js';
import { switchTab, saveViewState } from './app.js';
import { selectBook } from './reader.js';

// ── Sort persistence ──────────────────────────────────────────────

// 五种排序：导入时间 / 书名 / 作家 / 最近打开 / 自定义。
// key 是持久化契约（localStorage 的 bc_shelf_sort），改名要同步白名单。
export const SORT_KEYS = ['time', 'title', 'author', 'recent', 'custom'];

export const SORT_LABELS = {
  time: '导入时间',
  title: '书名',
  author: '作家',
  recent: '最近打开',
  custom: '自定义',
};

// 切到一个新 key 时的默认方向。「最近打开」自然要最近在前，其余正序。
// 「导入时间」保持 asc —— 这是改动前就有的默认观感，不动它。
// 自定义是手工序列，只有 1→N 一种读法；它没有倒序，下面也不翻方向。
const SORT_DEFAULT_DIR = {
  time: 'asc', title: 'asc', author: 'asc', recent: 'desc', custom: 'asc',
};

export function loadShelfSort() {
  try {
    const raw = localStorage.getItem('bc_shelf_sort');
    if (raw) {
      const parsed = JSON.parse(raw);
      if (SORT_KEYS.includes(parsed.key) && ['asc', 'desc'].includes(parsed.dir)) {
        // 自定义顺序是手工排的固定序列，没有倒序；旧 localStorage 若存了
        // custom+desc，读入时就归正，避免重开页面后书架忽然倒过来。
        S.shelfSort = {
          key: parsed.key,
          dir: parsed.key === 'custom' ? 'asc' : parsed.dir,
        };
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
    if (key === 'recent') {
      const ta = a.last_opened_at || 0;
      const tb = b.last_opened_at || 0;
      // 从未打开过的（0）永远沉底，不随方向翻转 ——
      // 否则正序时「从没读过的书」会占满榜首。
      if (!ta || !tb) return ta ? -1 : (tb ? 1 : 0);
      return sign * (ta - tb);
    }
    if (key === 'custom') {
      const ra = a.sort_order || 0;
      const rb = b.sort_order || 0;
      // 未参与过手工排版的（0）永远沉底，新导入的书不会插到用户
      // 精心排好的顺序前面。两者都未排名时按导入时间排，保证稳定。
      if (!ra || !rb) {
        if (ra !== rb) return ra ? -1 : 1;
        return (a.time_created || 0) - (b.time_created || 0);
      }
      // 手工序列固定 1→N；方向不参与，避免 custom+desc 把书架倒过来。
      return ra - rb;
    }
    return 0;
  });

  return sorted;
}

export function updateShelfSortUI() {
  // 触发器上直接写当前排序名，面板关着也知道现在按什么排。
  const label = document.getElementById('shelfSortLabel');
  if (label) label.textContent = SORT_LABELS[S.shelfSort.key] || '排序';
  // 触发器自己也是唯一的“怎么整理”提示：自定义时悬停会说明可拖封面。
  const trigger = document.getElementById('shelfSortTrigger');
  if (trigger) {
    trigger.title = S.shelfSort.key === 'custom' ? '自定义顺序：拖动封面调整' : '排序';
  }

  document.querySelectorAll('#shelfSortPanel .sort-option').forEach(opt => {
    const active = opt.dataset.key === S.shelfSort.key;
    opt.classList.toggle('active', active);
    const dirSpan = opt.querySelector('.so-dir');
    if (!dirSpan) return;
    // 方向箭头只挂在激活项上；chevron-up = 正序，down = 倒序。
    // 自定义是固定手工序列，不画箭头，也不给“再点一下能翻”的暗示。
    const showDir = active && opt.dataset.key !== 'custom';
    dirSpan.innerHTML = showDir
      ? (S.shelfSort.dir === 'asc'
          ? '<i data-lucide="chevron-up"></i>'
          : '<i data-lucide="chevron-down"></i>')
      : '';
  });
}

// ── Sort panel (collapsed dropdown) ───────────────────────────────

export function toggleSortPanel() {
  const wrap = document.getElementById('shelfSortWrap');
  if (!wrap) return;
  wrap.classList.toggle('open');
  if (wrap.classList.contains('open')) updateShelfSortUI();
}

export function closeSortPanel() {
  const wrap = document.getElementById('shelfSortWrap');
  if (wrap) wrap.classList.remove('open');
}

let _sortPanelBound = false;

export function initSortPanel() {
  if (_sortPanelBound) return;
  _sortPanelBound = true;

  // 点面板外收起。注意触发按钮自己也在 #shelfSortWrap 里，所以
  // 「点按钮开面板」那一次点击走到这里会因为 closest 命中而直接返回，
  // 不会开了又立刻关。
  document.addEventListener('click', e => {
    const wrap = document.getElementById('shelfSortWrap');
    if (!wrap || !wrap.classList.contains('open')) return;
    if (e.target && e.target.closest && e.target.closest('#shelfSortWrap')) return;
    closeSortPanel();
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeSortPanel();
  });

  // 滚动时收起：面板锚在 sticky 头部上，而头部滚动时会压缩变形，
  // 面板挂在那儿会显得错位。
  const pageHome = document.getElementById('page-home');
  if (pageHome) pageHome.addEventListener('scroll', closeSortPanel, { passive: true });
}

export function setSort(key) {
  if (!SORT_KEYS.includes(key)) return;
  if (_drag) endDrag();                     // 拖动中换排序会重建 DOM，先收手
  if (S.shelfSort.key === key) {
    // 自定义是手工序列，只有 1→N；再点一次不翻方向。选中后直接收起
    // 面板——书架本身就是可拖视图，不需要再点一次「整理」。
    if (key === 'custom') {
      if (S.shelfSort.dir !== 'asc') {
        S.shelfSort.dir = 'asc';
        saveShelfSort();
      }
      updateShelfSortUI();
      closeSortPanel();
      return;
    }
    S.shelfSort.dir = S.shelfSort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    S.shelfSort.key = key;
    S.shelfSort.dir = SORT_DEFAULT_DIR[key] || 'asc';
  }
  saveShelfSort();
  updateShelfSortUI();
  // 自定义没有方向可翻，菜单使命到选中为止：收起面板后直接拖封面。
  if (key === 'custom') closeSortPanel();
  _suppressEnterAnim = false;
  if (key === 'custom' && _lastShelfBooks) {
    // 同步按缓存重排；等网络的话，旧排序还挂在屏幕上时拖拽已经可用，
    // 用户一拖就会把旧顺序当成自定义顺序存回去。
    renderShelfRows(sortBooks(_lastShelfBooks));
  } else {
    loadShelf();
  }
}

// ── 自定义排序：直接拖动书本 ─────────────────────────────────────
//
// 没有独立的整理模式，也没有「完成」键：只要当前排序是自定义，卡片就
// 可以拖。鼠标按下封面、移动超过阈值即进入拖动；短点仍然是打开书。
// 触屏用 320ms 长按起拖，先滑动则视为滚动，取消本次拖拽。
//
// 落位策略：拖动过程中 DOM 完全不动，只移动一条「插入位」指示条；
// 松手时才按新顺序整体重渲染，再按 data-id 做一次 FLIP。
// 拖动期间才停掉倾斜、隐藏角标、放开 content-visibility——平常书架
// 的悬停抬升、角标和入场动画都不受影响。

let _drag = null;            // 当前拖拽：{ el, id, pointerId, startX/Y, lastX/Y, active, touch, armTimer, raf }
let _dropIndex = -1;         // 插入位（相对「除被拖卡片外」的扁平列表）
let _saveTimer = null;
let _suppressEnterAnim = false;  // 拖动落位后抑制一次入场动画
let _suppressClickUntil = 0;     // 拖动结束后的 click 要吞掉，避免误开书

const DRAG_THRESHOLD = 8;    // px：小于它算点击，不算拖动
const TOUCH_ARM_MS = 320;    // 触屏长按多久进入拖动
const CLICK_GUARD_MS = 350;  // 拖动松手后多久内的 click 视为拖拽残留
const EDGE_ZONE = 60;        // px：距滚动容器上下边缘多近开始自动滚动
const EDGE_SPEED = 9;        // px/帧

// 按 DOM 顺序取书架上当前的书籍 id。行只是布局产物，文档顺序就是扁平顺序。
function currentOrderIds() {
  const grid = document.getElementById('shelfGrid');
  if (!grid) return [];
  return [...grid.querySelectorAll('.scene-book')].map(el => el.dataset.id);
}

function saveCustomOrder() {
  const ids = currentOrderIds();
  if (!ids.length) return;
  fetch(S.API + '/api/books/order', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  }).catch(() => { /* 下次拖动还会再存一次，不必打扰用户 */ });
}

// 松手后 600ms 落库；连续快速拖动只发最后一次。没有「完成」键，
// 落位就保存；关页面/切标签页也不会丢刚才那次调整。
function scheduleSave() {
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => { _saveTimer = null; saveCustomOrder(); }, 600);
}

let _reorderBound = false;

export function initShelfReorder() {
  if (_reorderBound) return;
  _reorderBound = true;
  const grid = document.getElementById('shelfGrid');
  if (!grid) return;

  // 委托在 #shelfGrid 上绑一次即可 —— 它自己从不被替换，
  // 只有 innerHTML 会被重建，所以不必像倾斜控制器那样每次重渲染重绑。
  grid.addEventListener('pointerdown', e => {
    if (S.shelfSort.key !== 'custom' || _drag) return;
    if (e.button) return;                                  // 只接左键 / 单指
    const el = e.target && e.target.closest ? e.target.closest('.scene-book') : null;
    if (!el) return;
    // 角标是独立按钮，别让拖拽抢走它的点击。
    if (e.target.closest && e.target.closest('.book-actions')) return;
    _drag = {
      el, id: el.dataset.id, pointerId: e.pointerId,
      startX: e.clientX, startY: e.clientY,
      lastX: e.clientX, lastY: e.clientY,
      active: false, touch: e.pointerType === 'touch',
      armTimer: 0, raf: 0,
    };
    // 捕获指针：拖到窗口外也不丢事件。捕获后事件仍会冒泡到 window。
    try { el.setPointerCapture(e.pointerId); } catch (err) {}
    if (_drag.touch) {
      _drag.armTimer = setTimeout(() => {
        if (_drag && !_drag.active) startDrag();
      }, TOUCH_ARM_MS);
    }
  });

  // 触屏长按已经起拖后，用非 passive 的 touchmove 阻断页面滚动；
  // 长按之前的移动不拦，用户可以用手指滚动书架。
  grid.addEventListener('touchmove', e => {
    if (_drag && _drag.active) e.preventDefault();
  }, { passive: false });
  grid.addEventListener('dragstart', e => e.preventDefault());

  window.addEventListener('pointermove', onReorderMove, { passive: false });
  window.addEventListener('pointerup', onReorderUp);
  window.addEventListener('pointercancel', cancelDrag);
  window.addEventListener('resize', endDrag);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) endDrag();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && _drag) endDrag();
  });
  // 拖动松手后的 click 是拖拽残留，不能打开书。
  grid.addEventListener('click', e => {
    if (Date.now() > _suppressClickUntil) return;
    e.preventDefault();
    e.stopPropagation();
  }, true);
}

function startDrag() {
  if (!_drag || _drag.active) return;
  _drag.active = true;
  if (_drag.armTimer) { clearTimeout(_drag.armTimer); _drag.armTimer = 0; }
  _suppressEnterAnim = true;
  releaseAllTilt();                 // 倾斜和拖拽都吃 pointermove，先收手
  _drag.el.classList.add('dragging');
  const grid = document.getElementById('shelfGrid');
  if (grid) grid.classList.add('dragging-active');
  document.body.classList.add('shelf-dragging');
  if (!_drag.raf) _drag.raf = requestAnimationFrame(dragTick);
}

function onReorderMove(e) {
  if (!_drag || e.pointerId !== _drag.pointerId) return;
  _drag.lastX = e.clientX;
  _drag.lastY = e.clientY;
  if (!_drag.active) {
    const dx = e.clientX - _drag.startX;
    const dy = e.clientY - _drag.startY;
    if (dx * dx + dy * dy < DRAG_THRESHOLD * DRAG_THRESHOLD) return;  // 仍算点击
    // 触屏上先动了 = 用户在滚书架，取消长按；桌面鼠标则直接起拖。
    if (_drag.touch) { endDrag(); return; }
    startDrag();
  }
  e.preventDefault();                       // 拖动时别让页面跟着滚
  if (!_drag || _drag.raf) return;
  _drag.raf = requestAnimationFrame(dragTick);
}

function onReorderUp(e) {
  if (!_drag || e.pointerId !== _drag.pointerId) return;
  if (_drag.active) {
    _suppressClickUntil = Date.now() + CLICK_GUARD_MS;
    commitDrag();
  } else {
    endDrag();                           // 没超过阈值 = 点击
  }
}

function cancelDrag() {
  endDrag();
}

function dragTick() {
  if (!_drag || !_drag.active) return;
  _drag.raf = 0;
  const dx = _drag.lastX - _drag.startX;
  const dy = _drag.lastY - _drag.startY;
  _drag.el.style.transform =
    `translate3d(${dx}px, ${dy}px, 0) scale(1.06) rotate(-1.5deg)`;
  updateDropIndex();
  autoscroll();
  if (_drag && _drag.active) _drag.raf = requestAnimationFrame(dragTick);
}

// 拖到滚动容器上下边缘附近时自己滚 —— 书多了必然要跨屏拖
function autoscroll() {
  const page = document.getElementById('page-home');
  if (!page || !_drag) return;
  const r = page.getBoundingClientRect();
  if (_drag.lastY < r.top + EDGE_ZONE) page.scrollTop -= EDGE_SPEED;
  else if (_drag.lastY > r.bottom - EDGE_ZONE) page.scrollTop += EDGE_SPEED;
}

// 插入位指示条：绝对定位在 #shelfGrid 内（它是 position: relative），
// 靠 transform 移动 + CSS transition 平滑滑到新位置。
function updateDropIndex() {
  const grid = document.getElementById('shelfGrid');
  if (!grid || !_drag) return;
  let slot = document.getElementById('shelfDropSlot');
  if (!slot) {
    slot = document.createElement('div');
    slot.id = 'shelfDropSlot';
    slot.className = 'shelf-drop-slot';
    grid.appendChild(slot);
  }
  const others = [...grid.querySelectorAll('.scene-book')].filter(el => el !== _drag.el);
  _dropIndex = computeInsertIndex(others, _drag.lastX, _drag.lastY);

  // 指示条摆在「插入位置之前那张卡」的左边；插到末尾就摆最后一张的右边
  const atEnd = !others[_dropIndex];
  const ref = others[_dropIndex] || others[others.length - 1];
  if (!ref) { slot.style.display = 'none'; return; }
  const r = ref.getBoundingClientRect();
  const gridRect = grid.getBoundingClientRect();
  const x = atEnd ? r.right - gridRect.left + 5 : r.left - gridRect.left - 7;
  slot.style.display = 'block';
  slot.style.height = r.height + 'px';
  slot.style.transform = `translate3d(${x}px, ${r.top - gridRect.top}px, 0)`;
}

// 取「离指针最近的那张卡」，再看指针落在它中心左侧还是右侧。
// 距离按垂直加权 —— 不加权的话跨行拖动会被相邻行的卡片横向距离带偏。
function computeInsertIndex(cards, x, y) {
  let best = cards.length, bestDist = Infinity, after = false;
  for (let i = 0; i < cards.length; i++) {
    const r = cards[i].getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const d = Math.abs(y - cy) * 3 + Math.abs(x - cx);
    if (d < bestDist) { bestDist = d; best = i; after = x > cx; }
  }
  return after ? best + 1 : best;
}

function commitDrag() {
  const grid = document.getElementById('shelfGrid');
  const drag = _drag;
  if (!grid || !drag) { endDrag(); return; }

  // 一次极短的拖动可能在第一帧 rAF 跑起来之前就松手了，此时插入位还是 -1。
  // 不补算的话会被当成「插到末尾」，书会莫名跳到最后一本。
  if (_dropIndex < 0) updateDropIndex();

  // 落位前先记下每张卡的位置，重渲染后按 data-id 对回去做 FLIP
  const before = new Map();
  grid.querySelectorAll('.scene-book').forEach(el => {
    before.set(el.dataset.id, el.getBoundingClientRect());
  });

  const others = [...grid.querySelectorAll('.scene-book')].filter(el => el !== drag.el);
  const ids = others.map(el => el.dataset.id);
  const at = Math.max(0, Math.min(_dropIndex < 0 ? ids.length : _dropIndex, ids.length));
  ids.splice(at, 0, drag.id);

  endDrag();

  const byId = new Map((_lastShelfBooks || []).map(b => [b.id, b]));
  const next = ids.map(id => byId.get(id));
  if (next.some(b => !b)) return;          // 有对不上的就整体放弃，别把书架弄乱
  _lastShelfBooks = next;
  renderShelfRows(next);                   // 拖动落位时 shelve-in 已关，重建是瞬时的
  flipFrom(before);
  scheduleSave();
}

// FLIP：先按旧位置把每张卡「瞬移回去」，下一帧再放开过渡让它滑到新位置。
// 被拖那张记的是它被拖走后的位置，所以它会从手上飞回落点。
function flipFrom(before) {
  if (_reducedMotion) return;              // 减弱动效：直接落位，不做位移动画
  const grid = document.getElementById('shelfGrid');
  if (!grid) return;
  const moved = [];
  grid.querySelectorAll('.scene-book').forEach(el => {
    const prev = before.get(el.dataset.id);
    if (!prev) return;
    const now = el.getBoundingClientRect();
    const dx = prev.left - now.left;
    const dy = prev.top - now.top;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
    el.style.transition = 'none';
    el.style.transform = `translate3d(${dx}px, ${dy}px, 0)`;
    moved.push(el);
  });
  if (!moved.length) return;
  requestAnimationFrame(() => {
    moved.forEach(el => {
      el.style.transition = 'transform .26s var(--ease-out)';
      el.style.transform = '';
    });
  });
  setTimeout(() => {
    moved.forEach(el => {
      el.style.removeProperty('transition');
      el.style.removeProperty('transform');
    });
  }, 360);
}

// 收掉一次拖拽的全部痕迹（中途取消与松手落位都走这里）
function endDrag() {
  if (!_drag) return;
  const drag = _drag;
  _drag = null;
  if (drag.armTimer) clearTimeout(drag.armTimer);
  if (drag.raf) cancelAnimationFrame(drag.raf);
  try { drag.el.releasePointerCapture(drag.pointerId); } catch (err) {}
  drag.el.style.removeProperty('transform');
  drag.el.classList.remove('dragging');
  const slot = document.getElementById('shelfDropSlot');
  if (slot) slot.remove();
  const grid = document.getElementById('shelfGrid');
  if (grid) grid.classList.remove('dragging-active');
  document.body.classList.remove('shelf-dragging');
  _dropIndex = -1;
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
  // Freeze decorative infinite animations (ribbon sway / dust motes) while
  // the shelf is actively scrolling. They resume ~140ms after the last
  // scroll tick; the pause is visually imperceptible but removes dozens of
  // independently animating layers from the scroll compositor's budget.
  let scrollIdleTimer = null;
  pageHome.addEventListener('scroll', function () {
    if (!document.body.classList.contains('is-scrolling-home')) {
      document.body.classList.add('is-scrolling-home');
    }
    clearTimeout(scrollIdleTimer);
    scrollIdleTimer = setTimeout(function () {
      document.body.classList.remove('is-scrolling-home');
    }, 140);
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

// 书架卡片最宽 130px，原封面 500–1700px 宽（最大 800KB）。缩略图端点
// 返回 400px 宽的版本，下载量约 1/8、解码量约 1/16。
function shelfCoverUrl(id) {
  return S.API + '/api/books/' + id + '/cover?thumb=1';
}

// 刷新后把缩略图主动排进下载队列。原生 loading="lazy" 要等卡片滚进
// 视口附近才发请求，快速下滑时封面永远慢半拍；缩略图总量只有几百 KB，
// 一次预热完，之后怎么滚都是缓存命中。
let _coverPreloadToken = 0;
let _coverPreloadKeep = [];   // 持有引用：被 GC 掉的 Image 可能连带取消请求
function preloadShelfCovers(books) {
  const token = ++_coverPreloadToken;
  const kept = (_coverPreloadKeep = []);
  const total = Math.min(books.length, 150);   // 超大书库不无限预热
  const CONCURRENCY = 6;
  let next = 0;
  let active = 0;
  function pull() {
    while (token === _coverPreloadToken && active < CONCURRENCY && next < total) {
      const b = books[next++];
      active++;
      const img = new Image();
      img.decoding = 'async';
      img.onload = img.onerror = function () { active--; pull(); };
      img.src = shelfCoverUrl(b.id);
      kept.push(img);
    }
  }
  pull();
}

function sceneBookHtml(b, idx) {
  const coverUrl = shelfCoverUrl(b.id);
  const title = escapeHtml(b.title);
  const author = escapeHtml(b.author || '');
  const initials = escapeHtml(b.title.slice(0, 4));
  const ribbon = b.last_read_at ? '<span class="book-ribbon"></span>' : '';
  const delay = Math.min(idx * 45, 900);
  return `
      <div class="scene-book" data-id="${escapeHtmlAttr(b.id)}" style="${bookStyleVars(b)}--d:${delay}ms" onclick="openBookFromShelf('${b.id}')" title="${title}">
        <div class="scene-book-lift">
        ${ribbon}
        <div class="scene-book-inner">
          <img src="${coverUrl}" alt="${title}" loading="lazy" decoding="async" draggable="false"
               onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
          <div class="book-cover-placeholder" style="display:none"><span>${initials}</span></div>
          <i class="book-glare"></i>
          <div class="scene-book-scrim">
            <div class="scrim-title">${title}</div>
            <div class="scrim-author">${author}</div>
          </div>
          <div class="book-actions">
            <button class="book-act" onclick="event.stopPropagation();openGraphPanel('${b.id}','${title.replace(/'/g, "\\'")}')" title="图谱分析" aria-label="图谱分析"><i data-lucide="network"></i></button>
            <button class="book-act" onclick="event.stopPropagation();openTagPanel('${b.id}','${title.replace(/'/g, "\\'")}','${author.replace(/'/g, "\\'")}')" title="标注与书籍信息" aria-label="标注与书籍信息"><i data-lucide="sticky-note"></i></button>
            <button class="book-act book-act-danger" onclick="event.stopPropagation();deleteBook('${b.id}')" title="删除此书" aria-label="删除此书"><i data-lucide="trash-2"></i></button>
          </div>
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
    // 拖动期间指针归拖拽控制器所有，倾斜彻底让位。
    // 两个控制器都是 pointermove 驱动的，不靠事件顺序、靠这个闸门隔离。
    if (_drag) return;
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
  // 拖动落位的那次重渲染不能播入场动画：CSS 动画会盖掉拖拽写的内联
  // transform，而且每次落位都会重播一遍。平常渲染不挂这个类，切排序 /
  // 重载时入场动画照常恢复 —— 详见 style.css 的说明。
  grid.classList.toggle('no-enter-anim', _suppressEnterAnim);
  grid.classList.toggle('custom-order', S.shelfSort.key === 'custom');
  const rows = [];
  for (let i = 0; i < books.length; i += perRow) rows.push(books.slice(i, i + perRow));
  // Only long shelves benefit from content-visibility (see style.css):
  // below the threshold, realizing rows as they scroll in costs more than
  // rendering them once up front.
  grid.classList.toggle('is-long', rows.length > 6);
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
  // 拖动中不重排：DOM 重建会把正在拖的那张卡换成新节点，拖拽状态直接失效
  if (_drag) return;
  if (_shelfResizeTimer) clearTimeout(_shelfResizeTimer);
  _shelfResizeTimer = setTimeout(() => {
    if (currentPerRow() !== _lastPerRow) renderShelfRows(_lastShelfBooks);
  }, 200);
});

/** 「继续阅读」的书 —— 书架卡片与阅读页默认书共用的唯一判据。
 *
 *  "最近打开" and "最近读过" are two different stamps:
 *    last_read_at   — a reading position was synced (page turn / scroll)
 *    last_opened_at — the book was opened (POST /context), even without
 *                     turning a page yet.
 *  Opening B and going straight back to home must make B the continue
 *  card; keying only on last_read_at leaves A there until B is scrolled.
 *
 *  ⚠️ 阅读页进入时必须用这里挑书，不能让它落到下拉框第一项：
 *  /api/books 的顺序是 time_updated DESC，与"读到哪"毫无关系（实测
 *  第一项是卡拉马佐夫兄弟）。两处各算一套就会出现"卡片说甲、翻开是乙"。
 */
export function pickContinueBook(books) {
  return (books || [])
    .map(function (b) {
      return { book: b, at: Math.max(b.last_read_at || 0, b.last_opened_at || 0) };
    })
    .filter(function (x) { return x.at > 0; })
    .sort(function (a, b) { return b.at - a.at; })
    .map(function (x) { return x.book; })[0] || null;
}

export async function loadShelf() {
  if (_drag) return;                        // 拖动中别重建 DOM，拖拽状态会失效
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

    // Continue reading section.
    const lastRead = pickContinueBook(data.books);
    const continueSection = document.getElementById('continueSection');
    if (lastRead) {
      continueSection.style.display = 'block';
      document.getElementById('continueCover').src = shelfCoverUrl(lastRead.id);
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
    if (!_drag) _suppressEnterAnim = false;
    const sorted = sortBooks(data.books);
    renderShelfRows(sorted);
    preloadShelfCovers(sorted);
  } catch (e) {
    console.error(e);
    document.getElementById('shelfEmpty').style.display = 'block';
  }
}

export function openBookFromShelf(bookId) {
  // 拖动中/松手后的残留 click 不打开书；普通点击照常进入阅读。
  if (_drag && _drag.active) return;
  if (Date.now() < _suppressClickUntil) return;
  // 条件二：打开的不是上次读的那本书 = 读一本新书 → 阅读页开新会话。
  // 上次读的书优先取内存里的 _lastLoadedBook；刷新后它还没值，就用
  // 阅读线指针记着的 bookId 判断（同一本书仍可恢复今天的会话）。
  const line = window.tabSessionFor ? window.tabSessionFor('reader') : null;
  const lastBook = S._lastLoadedBook || (line && line.bookId) || null;
  if (lastBook && lastBook !== bookId && window.clearTabSession) {
    window.clearTabSession('reader');
  }
  // 先定书再切页：_enterChatTab 恢复阅读线时按 S.currentBook 做判断。
  S.currentBook = bookId;
  switchTab('reader', true);              // skipLoad: 避免与下面的 selectBook 竞态
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
    if (!data.books || !data.books.length) return [];
    const sel = document.getElementById('bookSelect');
    sel.innerHTML = data.books.map(b =>
      `<option value="${b.id}">${b.title.slice(0, 35)} (${b.chapter_count}章)</option>`
    ).join('');
    return data.books;
  } catch (e) { return []; }
}

export async function refreshBooks() {
  const books = await loadBookDropdown();   // 重建 <select>（会丢失选中值）
  const sel = document.getElementById('bookSelect');
  // 打开哪本书的优先级：内存里的当前书（可能是阅读线会话 / 书架点开）→
  // 「继续阅读」真相源。绝不能留空：<select> 会停在第 1 项，而 /api/books
  // 的顺序是 time_updated DESC（实测第一项是卡拉马佐夫兄弟），于是"继续
  // 阅读"标着甲、点开阅读页却是乙。
  const fallback = pickContinueBook(books);
  const keepBook = [S.currentBook, fallback && fallback.id]
    .find(id => id && sel.querySelector(`option[value="${id}"]`)) || null;
  if (keepBook) {
    sel.value = keepBook;             // 仅当选项存在时才恢复（书可能已被删除）
    S.currentBook = keepBook;
  }
  // 没有任何"读到哪"的记录（全新书库）时不要顺手打开下拉框第一项 ——
  // 那不是用户选的书，只是排序的副作用。
  if (keepBook && document.getElementById('page-reader').classList.contains('active')) {
    await selectBook();
    // selectBook 之后才落盘：switchTab 末尾那次 saveViewState 早于选书，
    // 会把阅读页记成 bookId:null，刷新时就恢复不到书（退回书架页）。
    saveViewState();
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

/** 路径输入表单。window.prompt 做不到两件事：浅色示例（它的第二个参数是
 *  预填值，不是 placeholder），以及失败后回到可编辑状态（关掉就没了）。 */
function pathFormHtml(message) {
  return '<div class="scan-path-form">'
    + (message ? '<div class="scan-path-hint scan-path-error">' + escapeHtml(message) + '</div>' : '')
    + '<div class="scan-path-hint">输入存放未入库 EPUB/MOBI 的文件夹路径：</div>'
    + '<div class="scan-path-row">'
    + '<input id="scanPathInput" class="scan-path-input" type="text" spellcheck="false" autocomplete="off"'
    + ' placeholder="例如：C:\\Users\\你的用户名\\Desktop\\书库"'
    + ' onkeydown="if(event.key===\'Enter\'){event.preventDefault();submitScanPath();}">'
    + '<button class="primary" onclick="submitScanPath()">扫描</button>'
    + '</div></div>';
}

function showPathForm(message, value) {
  const body = document.getElementById('importPopupBody');
  if (!body) return;
  body.innerHTML = pathFormHtml(message);
  const input = document.getElementById('scanPathInput');
  if (!input) return;
  // 用属性赋值而不是拼进 HTML：路径里的引号/反斜杠不必做属性转义
  input.value = value || '';
  input.focus();
  input.select();
}

export function submitScanPath() {
  const input = document.getElementById('scanPathInput');
  if (!input) return;
  const folder = input.value.trim();
  if (!folder) { input.focus(); return; }
  setScanFolder(folder);
  scanFolder(folder);
}

async function scanFolder(folder) {
  const body = document.getElementById('importPopupBody');
  body.innerHTML = '<div style="color:var(--text-muted);font-size:var(--fs-caption);text-align:center;padding:var(--space-lg)">扫描中: ' + escapeHtml(folder) + '</div>';
  try {
    const resp = await fetch(S.API + '/api/books/scan-folder?path=' + encodeURIComponent(folder));
    const data = await resp.json();
    if (data.error) {
      if (resp.status === 404) {
        // 文件夹不可用 → 清掉记忆 + 直接回到可编辑表单
        try { localStorage.removeItem('bc_scan_folder'); } catch (e) {}
        showPathForm(folder + ' 不存在或不可访问，请重新输入。', folder);
      } else {
        body.innerHTML = '<div style="color:#ff6b6b;text-align:center;padding:var(--space-lg)">' + escapeHtml(data.error) + '</div>';
      }
      return;
    }
    renderScanResult(data, data.folder || folder);
  } catch (e) {
    // 网络层失败（Failed to fetch）：请求根本没到服务端。
    // 清掉记忆，下次打开直接回到路径填写界面，而不是反复撞同一个死路径。
    try { localStorage.removeItem('bc_scan_folder'); } catch (_) {}
    showPathForm('连不上服务端（' + S.API + '）：' + e.message
      + '。确认服务在该地址运行后重试。', folder);
  }
}

export async function scanAndShowFiles() {
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
  } catch (e) { /* 网络失败 → 落到路径表单 */ }

  showPathForm('', '');
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
window.submitScanPath = submitScanPath;
window.importSingleFile = importSingleFile;
window.setSort = setSort;
window.toggleSortPanel = toggleSortPanel;
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
