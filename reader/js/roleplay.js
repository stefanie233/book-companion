/* Book Companion — role-play setup (graph-driven). */

import { S } from './state.js';
import { escapeHtml } from './utils.js';
import { addChat } from './chat.js';

export function startRolePlaySetup() {
  document.getElementById('modePicker').classList.remove('visible');
  document.getElementById('roleplayOverlay').classList.add('visible');
  document.body.style.overflow = 'hidden';   // freeze background scroll
  S.rpSelectedBookId = null;
  S.rpSelectedCharName = null;
  showRPBookList();
}

export function cancelRolePlaySetup() {
  document.getElementById('roleplayOverlay').classList.remove('visible');
  document.body.style.overflow = '';        // restore background scroll
  S.rpSelectedBookId = null;
  S.rpSelectedCharName = null;
  S.rpSelectedCharChapter = null;
}

function setRPBody(html) {
  document.getElementById('rpBody').innerHTML = html;
}

function showRPFooter(show) {
  document.getElementById('rpFooter').style.display = show ? 'flex' : 'none';
}

// ── Step 1: Book list ─────────────────────────────────────────────

export async function showRPBookList() {
  setRPBody('<div class="rp-loading">加载已图谱化的书籍...</div>');
  showRPFooter(false);
  try {
    const resp = await fetch(S.API + '/api/graph/books');
    const data = await resp.json();
    const books = data.books || [];
    if (!books.length) {
      setRPBody('<div class="rp-empty">还没有图谱化的书籍。<br>请先在书架中对书籍运行图谱分析（<i data-lucide="network"></i> 按钮）。</div>');
      return;
    }
    let html = '<div class="rp-section-title">选择书籍</div><div class="rp-book-list">';
    books.forEach(b => {
      html += '<div class="rp-book-card" onclick="selectRPBook(\'' + b.book_id + '\',\'' + escapeHtml(b.title) + '\')">'
        + '<div class="rp-book-title">' + escapeHtml(b.title) + '</div>'
        + '<div class="rp-book-meta">' + (b.author || '') + ' · ' + b.nodes + ' 个实体</div>'
        + '</div>';
    });
    html += '</div>';
    setRPBody(html);
  } catch (e) {
    setRPBody('<div class="rp-error">加载失败：' + e.message + '</div>');
  }
}

// ── Step 2: Character list ────────────────────────────────────────

export async function selectRPBook(bookId, bookTitle) {
  S.rpSelectedBookId = bookId;
  setRPBody('<div class="rp-loading">加载角色...</div>');
  showRPFooter(false);
  try {
    const resp = await fetch(S.API + '/api/graph/' + bookId + '/characters');
    const data = await resp.json();
    const chars = data.characters || [];
    if (!chars.length) {
      setRPBody('<div class="rp-empty">该书图谱中没有角色。</div>');
      return;
    }
    let html = '<div class="rp-back-row"><button class="rp-back-btn" onclick="showRPBookList()"><i data-lucide="arrow-left"></i> 返回书籍列表</button></div>';
    html += '<div class="rp-section-title">' + escapeHtml(bookTitle || '') + ' — 选择角色</div>';
    html += '<div class="rp-char-list">';
    chars.forEach(c => {
      const desc = (c.description || '').slice(0, 60);
      html += '<div class="rp-char-card" onclick="selectRPCharacter(\'' + escapeHtml(c.canonical_name) + '\',' + (c.first_appearance_chapter || 0) + ')">'
        + '<div class="rp-char-name">' + escapeHtml(c.canonical_name) + '</div>'
        + (desc ? '<div class="rp-char-desc">' + escapeHtml(desc) + '</div>' : '')
        + '</div>';
    });
    html += '</div>';
    setRPBody(html);
  } catch (e) {
    setRPBody('<div class="rp-error">加载失败：' + e.message + '</div>');
  }
}

// ── Step 3: Mode selector ─────────────────────────────────────────

export function selectRPCharacter(charName, firstChapter) {
  S.rpSelectedCharName = charName;
  S.rpSelectedCharChapter = firstChapter;
  const isImmersive = S.currentChapter > 0;
  const curChLabel = isImmersive ? '（当前阅读进度：第 ' + (S.currentChapter + 1) + ' 章）' : '';
  setRPBody(
    '<div class="rp-back-row"><button class="rp-back-btn" onclick="selectRPBook(rpSelectedBookId,\'\')"><i data-lucide="arrow-left"></i> 返回角色列表</button></div>'
    + '<div class="rp-section-title">扮演「' + escapeHtml(charName) + '」</div>'
    + '<div class="rp-mode-pick">'
    + '<label class="rp-mode-option"><input type="radio" name="rpSpoiler" value="global" checked onchange="onRPModeChange()">'
    + '<div class="rp-mode-label">全局模式</div><div class="rp-mode-desc">角色知晓全部情节，适合重温或已读完</div></label>'
    + '<label class="rp-mode-option"><input type="radio" name="rpSpoiler" value="immersive" onchange="onRPModeChange()">'
    + '<div class="rp-mode-label">沉浸模式 ' + curChLabel + '</div><div class="rp-mode-desc">仅知悉当前章节及之前的信息，不剧透</div></label>'
    + '</div>'
  );
  showRPFooter(true);
}

export function onRPModeChange() {
  // no-op for now; mode is read on confirm
}

// ── Confirm ───────────────────────────────────────────────────────

export function confirmRolePlay() {
  const spoilerEl = document.querySelector('input[name="rpSpoiler"]:checked');
  const spoilerMode = spoilerEl ? spoilerEl.value : 'global';

  S.currentMode = 'roleplay';
  S.currentCharacterName = S.rpSelectedCharName;
  S.currentMaxChapter = spoilerMode === 'immersive'
    ? (S.currentChapter >= 0 ? S.currentChapter + 1 : null)
    : null;

  document.getElementById('roleplayOverlay').classList.remove('visible');
  document.body.style.overflow = '';        // restore background scroll

  // Update both reader mode label and home-page role-play entry label
  document.getElementById('modeLabel').innerHTML = '<i data-lucide="drama"></i> ' + escapeHtml(S.rpSelectedCharName);
  var rpLabel = document.querySelector('#roleplayEntryBtn .mode-label');
  if (rpLabel) rpLabel.textContent = S.rpSelectedCharName;

  document.querySelectorAll('.mode-option').forEach(function (opt) {
    opt.classList.toggle('active', opt.dataset.mode === 'roleplay');
  });

  var modeLabel = spoilerMode === 'immersive'
    ? '沉浸模式（不剧透' + (S.currentMaxChapter ? '，仅知第 ' + S.currentMaxChapter + ' 章及之前' : '') + '）'
    : '全局模式';
  addChat('agent', '已进入扮演模式。我现在是「' + S.rpSelectedCharName + '」。' + modeLabel + '。');
}

// ── HTML onclick access ───────────────────────────────────────────
window.startRolePlaySetup = startRolePlaySetup;
window.cancelRolePlaySetup = cancelRolePlaySetup;
window.showRPBookList = showRPBookList;
window.selectRPBook = selectRPBook;
window.selectRPCharacter = selectRPCharacter;
window.onRPModeChange = onRPModeChange;
window.confirmRolePlay = confirmRolePlay;
