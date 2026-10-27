/* 标注面板 — 书籍信息编辑 + 覆盖层便利贴的前端入口。
 *
 * 书架卡 hover "标注"按钮 → 弹窗顶部可改书名/作者（电子书源的书名常不干净，
 * 导入时 LLM 清洗的兜底），下方列出全部章节 + 当前类型（◉便利贴/○原表），
 * 行内下拉 + 备注 + 保存/清除即时改 human override（book_chapter_overrides）。
 *
 * 与 graph-panel.js 同一家族：ensureModal + openXxxPanel 挂 window。
 * 性能：题库书几千章时 body 组默认折叠，展开才渲染；保存/清除跨组才重载列表。
 */

import { S } from './js/state.js';
import { escapeHtml } from './js/utils.js';
import { loadShelf, refreshBooks } from './js/shelf.js';

// ── 类型中文映射（与 api_server._type_label / CLI chapter_type_editor 同款）────
const LABELS = {
  cover: '封面', copyright: '版权', toc: '目录', preface: '前言',
  body: '正文', postscript: '后记', appendix: '附录',
  bibliography: '参考', index: '索引', notes: '注释',
};
function typeLabel(t) { return LABELS[t] || t; }

function groupOf(type) {
  if (['cover', 'copyright', 'toc', 'preface'].includes(type)) return 'front';
  if (['postscript', 'appendix', 'bibliography', 'index', 'notes'].includes(type)) return 'back';
  return 'body';
}
const GROUP_TITLES = { front: '前置内容', body: '正文', back: '后置内容' };

// ── Modal HTML（injected once）────────────────────────────────────────

let currentBookId = '';
let modalBodyEl = null;

function ensureModal() {
  if (document.getElementById('tagModal')) return;
  const modal = document.createElement('div');
  modal.id = 'tagModal';
  modal.className = 'tag-modal';
  modal.innerHTML = `
    <div class="tag-modal-backdrop" onclick="closeTagPanel()"></div>
    <div class="tag-modal-content">
      <div class="tag-modal-header">
        <h2 id="tagModalTitle">标注</h2>
        <button class="tag-modal-close" onclick="closeTagPanel()">×</button>
      </div>
      <div class="tag-book-info">
        <span class="tbi-label">书籍信息</span>
        <input class="tbi-input tbi-title" id="tbiTitle" placeholder="书名" maxlength="200">
        <input class="tbi-input tbi-author" id="tbiAuthor" placeholder="作者" maxlength="100">
        <button class="tag-save" id="tbiSave" onclick="saveBookInfo()">保存</button>
      </div>
      <div class="tag-modal-body" id="tagModalBody">
        <!-- dynamic content -->
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  modalBodyEl = document.getElementById('tagModalBody');

  // 事件委托：save / clear 按钮 + 下拉 change
  const content = modal.querySelector('.tag-modal-content');
  content.addEventListener('click', e => {
    const saveBtn = e.target.closest('.tag-save');
    if (saveBtn) { tagSave(Number(saveBtn.dataset.index)); return; }
    const clearBtn = e.target.closest('.tag-clear');
    if (clearBtn) { tagClear(Number(clearBtn.dataset.index)); return; }
  });
}

// ── Open / close ─────────────────────────────────────────────────────

function openTagPanel(bookId, bookTitle, bookAuthor) {
  currentBookId = bookId;
  ensureModal();
  document.getElementById('tagModalTitle').textContent = '标注 — ' + (bookTitle || bookId);
  // 书籍信息预填：值来自书架刚渲染的数据，和卡片上所见一致
  document.getElementById('tbiTitle').value = bookTitle || '';
  document.getElementById('tbiAuthor').value = bookAuthor || '';
  document.getElementById('tbiSave').textContent = '保存';
  document.getElementById('tagModal').style.display = 'flex';
  loadChapters();
}

function closeTagPanel() {
  const modal = document.getElementById('tagModal');
  if (modal) modal.style.display = 'none';
  currentBookId = '';
}

// ── 书籍信息（书名/作者）保存 ─────────────────────────────────────────
// 电子书源的书名常不干净；导入时 LLM 清洗是一次性的，这里是人工兜底。

async function saveBookInfo() {
  const title = document.getElementById('tbiTitle').value.trim();
  const author = document.getElementById('tbiAuthor').value.trim();
  if (!title) { alert('书名不能为空'); return; }
  const btn = document.getElementById('tbiSave');
  btn.textContent = '保存中...';
  try {
    const resp = await fetch(
      S.API + '/api/books/' + encodeURIComponent(currentBookId),
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, author }),
      },
    );
    const data = await resp.json();
    if (data.error) { alert('保存失败：' + data.error); btn.textContent = '保存'; return; }
    // 面板标题跟着新书名走；书架卡片 + 阅读器书单就地刷新
    document.getElementById('tagModalTitle').textContent = '标注 — ' + data.book.title;
    btn.textContent = '已保存';
    setTimeout(() => { btn.textContent = '保存'; }, 1200);
    loadShelf();
    refreshBooks();
  } catch (e) {
    alert('保存失败：' + e.message);
    btn.textContent = '保存';
  }
}

// ── Load / render ────────────────────────────────────────────────────

async function loadChapters() {
  modalBodyEl.innerHTML = '<div class="tag-loading">加载章节...</div>';
  try {
    const resp = await fetch(S.API + '/api/books/' + encodeURIComponent(currentBookId) + '/chapters');
    const data = await resp.json();
    if (data.error) {
      modalBodyEl.innerHTML = `<div class="tag-error">${escapeHtml(data.error)}</div>`;
      return;
    }
    renderChapters(data.chapters || []);
  } catch (e) {
    modalBodyEl.innerHTML = `<div class="tag-error">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderChapters(chapters) {
  const groups = { front: [], body: [], back: [] };
  for (const ch of chapters) groups[groupOf(ch.type)].push(ch);

  const parts = [];
  for (const g of ['front', 'body', 'back']) {
    const list = groups[g];
    if (!list.length) continue;
    // body 默认收起（题库书几十上百章），front/back 默认展开（数量少、最常校验）
    const open = g !== 'body';
    parts.push(`
      <details class="tag-group" ${open ? 'open' : ''} data-group="${g}">
        <summary class="tag-group-summary">${GROUP_TITLES[g]} <span class="tag-group-count">${list.length}章</span></summary>
        <div class="tag-group-body">
          ${list.map(ch => tagRowHtml(ch)).join('')}
        </div>
      </details>`);
  }
  modalBodyEl.innerHTML = parts.join('') || '<div class="tag-empty">本书没有章节</div>';
}

function tagRowHtml(ch) {
  const mark = ch.overridden ? '◉' : '○';
  const markTitle = ch.overridden ? '便利贴（人手标注）' : '原表（导入/AI 猜测）';
  const noteVal = escapeHtml(ch.note || '');
  const opts = Object.keys(LABELS).map(t =>
    `<option value="${t}" ${t === ch.type ? 'selected' : ''}>${LABELS[t]}</option>`).join('');
  return `
    <div class="tag-row" data-index="${ch.chapter_index}" data-group="${groupOf(ch.type)}">
      <span class="tag-mark" title="${markTitle}">${mark}</span>
      <span class="tag-idx">[${ch.chapter_index}]</span>
      <span class="tag-title" title="${escapeHtml(ch.title)}">${escapeHtml(ch.title)}<em>${ch.char_count || 0}字</em></span>
      <select class="tag-select" data-index="${ch.chapter_index}">
        ${opts}
      </select>
      <input class="tag-note" data-index="${ch.chapter_index}" placeholder="备注（可选）" value="${noteVal}">
      <button class="tag-save" data-index="${ch.chapter_index}">保存</button>
      <button class="tag-clear" data-index="${ch.chapter_index}" title="撕便利贴，回落原表">清除</button>
    </div>`;
}

// ── Save / clear（局部更新，跨组才重载）────────────────────────────────

function rowIndexEl(index) {
  return modalBodyEl.querySelector(`.tag-row[data-index="${index}"]`);
}

async function tagSave(index) {
  const row = rowIndexEl(index);
  if (!row) return;
  const type = row.querySelector('.tag-select').value;
  const note = row.querySelector('.tag-note').value.trim();
  try {
    const resp = await fetch(
      S.API + '/api/books/' + encodeURIComponent(currentBookId) + '/chapters/' + index + '/override',
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter_type: type, note }),
      },
    );
    const data = await resp.json();
    if (data.error) { alert('保存失败：' + data.error); return; }
    if (groupOf(type) === row.dataset.group) {
      // 同组：行内就地更新（不改标记来源，但置为便利贴态）
      row.dataset.index = index;
      row.querySelector('.tag-mark').textContent = '◉';
      row.querySelector('.tag-mark').title = '便利贴（人手标注）';
      row.querySelector('.tag-note').value = note;
    } else {
      await loadChapters();   // 分组归属变了 → 整表重排
    }
  } catch (e) {
    alert('保存失败：' + e.message);
  }
}

async function tagClear(index) {
  const row = rowIndexEl(index);
  if (!row) return;
  try {
    const resp = await fetch(
      S.API + '/api/books/' + encodeURIComponent(currentBookId) + '/chapters/' + index + '/override',
      { method: 'DELETE' },
    );
    const data = await resp.json();
    if (data.error) { alert('清除失败：' + data.error); return; }
    const fallback = data.original_type;
    if (groupOf(fallback) === row.dataset.group) {
      row.querySelector('.tag-mark').textContent = '○';
      row.querySelector('.tag-mark').title = '原表（导入/AI 猜测）';
      row.querySelector('.tag-note').value = '';
      row.querySelector('.tag-select').value = fallback;
    } else {
      await loadChapters();
    }
  } catch (e) {
    alert('清除失败：' + e.message);
  }
}

// ── HTML onclick access ──────────────────────────────────────────────
window.openTagPanel = openTagPanel;
window.closeTagPanel = closeTagPanel;
window.saveBookInfo = saveBookInfo;