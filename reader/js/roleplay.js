/* Book Companion — role-play setup (graph-driven). */

import { S } from './state.js';
import { escapeHtml, escapeHtmlAttr } from './utils.js';
import { addChat } from './chat.js';

// 位置的中文短标签 —— 只在选择器上给一个「他在这本书里是什么」的提示，
// 真正的判断依据是后端按 position 拼进卡片的约束。这里不判、只显示。
const POSITION_ZH = {
  narrator: '叙述者', avatar: '借角色说话', editor: '整理者',
  recorder: '记录者', absent: '不在这本书里', unknown: '',
};

export function startRolePlaySetup() {
  document.getElementById('roleplayOverlay').classList.add('visible');
  document.body.style.overflow = 'hidden';   // freeze background scroll
  S.rpSelectedBookId = null;
  S.rpSelectedBookTitle = '';
  S.rpSelectedCharName = null;
  S.rpSelectedRoleKind = 'character';
  S.rpSelectedRoleRef = '';
  showRPBookList();
}

export function cancelRolePlaySetup() {
  document.getElementById('roleplayOverlay').classList.remove('visible');
  document.body.style.overflow = '';        // restore background scroll
  S.rpSelectedBookId = null;
  S.rpSelectedBookTitle = '';
  S.rpSelectedCharName = null;
  S.rpSelectedCharChapter = null;
  S.rpSelectedRoleKind = 'character';
  S.rpSelectedRoleRef = '';
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
      html += '<div class="rp-book-card" onclick="selectRPBook(\'' + b.book_id + '\',\'' + escapeHtmlAttr(b.title) + '\')">'
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
  // Remember the title so "返回角色列表" can re-render the section header
  // even when the caller only passes the id.
  S.rpSelectedBookTitle = bookTitle || S.rpSelectedBookTitle || '';
  setRPBody('<div class="rp-loading">加载角色...</div>');
  showRPFooter(false);
  // 两栏并列：书里的人（图谱）／写这本书的人（作者层）。
  // **两个请求，不是合并成一个** —— 两套 id 空间，后端也不合并
  // （设计稿 §八 选项 A）。任一栏失败不影响另一栏。
  const [charsRes, authorsRes] = await Promise.allSettled([
    fetch(S.API + '/api/graph/' + bookId + '/characters').then(r => r.json()),
    fetch(S.API + '/api/authors/' + bookId).then(r => r.json()),
  ]);
  const chars = (charsRes.status === 'fulfilled' && charsRes.value.characters) || [];
  const authors = (authorsRes.status === 'fulfilled' && authorsRes.value.authors) || [];
  // 没有语料也没有编年的作者不列 —— 扮演起来是空的，不如不出现。
  const speakable = authors.filter(a => (a.corpus || 0) + (a.records || 0) > 0);
  if (!chars.length && !speakable.length) {
    setRPBody('<div class="rp-empty">这本书还没有可扮演的角色或作者。</div>');
    return;
  }
  let html = '<div class="rp-back-row"><button class="rp-back-btn" onclick="showRPBookList()"><i data-lucide="arrow-left"></i> 返回书籍列表</button></div>';
  html += '<div class="rp-section-title">' + escapeHtml(bookTitle || '') + '</div>';
  if (speakable.length) {
    html += '<div class="rp-sub-title">作者</div><div class="rp-char-list">';
    speakable.forEach(a => {
      const meta = [a.nationality, (a.birth_year || a.death_year)
        ? ((a.birth_year || '?') + '–' + (a.death_year || '?')) : '',
        POSITION_ZH[a.position] || ''].filter(Boolean).join(' · ');
      html += '<div class="rp-char-card" onclick="selectRPCharacter(\''
        + escapeHtmlAttr(a.canonical_name) + '\',' + 0 + ',\'author\',\''
        + escapeHtmlAttr(a.author_id) + '\')">'
        + '<div class="rp-char-name">' + escapeHtml(a.canonical_name)
        + (a.role === 'co-author' ? ' <span class="rp-role-tag">合著</span>' : '')
        + '</div>'
        + (meta ? '<div class="rp-char-desc">' + escapeHtml(meta) + '</div>' : '')
        + '</div>';
    });
    html += '</div>';
  } else if (authors.length === 0) {
    html += '<div class="rp-hint">这本书还没有采到作者语料'
      + '（序跋采集没跑过，或这本书确实没有作者第一人称文本）。</div>';
  }
  if (chars.length) {
    html += '<div class="rp-sub-title">书中角色</div><div class="rp-char-list">';
    chars.forEach(c => {
      const desc = (c.description || '').slice(0, 60);
      html += '<div class="rp-char-card" onclick="selectRPCharacter(\''
        + escapeHtmlAttr(c.canonical_name) + '\',' + (c.first_appearance_chapter || 0)
        + ',\'character\',\'\')">'
        + '<div class="rp-char-name">' + escapeHtml(c.canonical_name) + '</div>'
        + (desc ? '<div class="rp-char-desc">' + escapeHtml(desc) + '</div>' : '')
        + '</div>';
    });
    html += '</div>';
  }
  setRPBody(html);
}

// ── Step 3: Mode selector ─────────────────────────────────────────

export function selectRPCharacter(charName, firstChapter, roleKind, roleRef) {
  S.rpSelectedCharName = charName;
  S.rpSelectedCharChapter = firstChapter;
  S.rpSelectedRoleKind = roleKind || 'character';
  S.rpSelectedRoleRef = roleRef || '';
  const isImmersive = S.currentChapter > 0;
  const curChLabel = isImmersive ? '（当前阅读进度：第 ' + (S.currentChapter + 1) + ' 章）' : '';
  setRPBody(
    // `rpSelectedBookId` is NOT a global — the old inline handler threw
    // ReferenceError on click and the button did nothing. Read the shared
    // state object instead (S is exposed on window), and pass the remembered
    // title so the character list keeps its book header.
    '<div class="rp-back-row"><button class="rp-back-btn" onclick="selectRPBook(S.rpSelectedBookId, S.rpSelectedBookTitle)"><i data-lucide="arrow-left"></i> 返回角色列表</button></div>'
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
  S.currentRoleKind = S.rpSelectedRoleKind || 'character';
  S.currentRoleRef = S.rpSelectedRoleRef || '';
  // 0-based chapter_index —— 与 graph_chunks.chapter_index、与后端门的判据
  // （``ch <= max_chapter``）同一基数。曾经写的是 ``S.currentChapter + 1``，
  // **差一章**：读者在《秋天的怀念》(idx 7) 会放行到 idx 8《墙下短记》整篇。
  S.currentMaxChapter = spoilerMode === 'immersive'
    ? (S.currentChapter >= 0 ? S.currentChapter : null)
    : null;

  document.getElementById('roleplayOverlay').classList.remove('visible');
  document.body.style.overflow = '';        // restore background scroll

  // 扮演不是阅读模式：分段控件清空选中，状态由「角色对话」按钮上的名字承担。
  if (typeof window.setModeLabel === 'function') window.setModeLabel('roleplay');
  var rpLabel = document.querySelector('#roleplayEntryBtn .mode-label');
  if (rpLabel) rpLabel.textContent = S.rpSelectedCharName;

  // 展示要 +1（chapter_index 是 0-based，人读的是「第 N 章」）
  var modeLabel = spoilerMode === 'immersive'
    ? '沉浸模式（不剧透' + (S.currentMaxChapter != null
        ? '，仅知第 ' + (S.currentMaxChapter + 1) + ' 章及之前' : '') + '）'
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
