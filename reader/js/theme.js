/* Book Companion — theme settings. */

import { S } from './state.js';
import * as EpubReader from './epub-reader.js';

const THEME_DEFAULTS = {
  // 出厂默认阅读主题：象牙暖底 + 深墨绿字 + 楷体（用户实际在用的配色）。
  readerFontFamily: 'kai',
  readerFontSize: '18',
  readerFontColor: '#183422',
  readerFontBrightness: '0.7',
  readerBgColor: '#ddb56e',
  readerBgBrightness: '1',
  chatFontSize: '14',
  chatFontColor: '#9a8e7a',
  selectionColor: '#ffffff',
  readerSelectionColor: '#7da9d8',
  notesFontSize: '13',
  pageTurnThreshold: '1',  // wheel ticks needed to turn page at edge
};

// Font-family stacks for each readerFontFamily option.
// Windows-first with cross-platform fallbacks (macOS / Linux / web fonts).
const READER_FONT_FAMILIES = {
  mono: '"Cascadia Code", "JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New", "Sarasa Mono SC", "Sarasa Term SC", "Microsoft YaHei", monospace',
  song: 'SimSun, "Songti SC", "Source Han Serif SC", "Noto Serif SC", serif',
  kai: 'KaiTi, "STKaiti", "Kaiti SC", "Source Han Serif SC", "Noto Serif SC", serif',
  hei: '"Microsoft YaHei", "PingFang SC", "Source Han Sans SC", "Noto Sans SC", sans-serif',
  fangsong: 'FangSong, "STFangsong", "Source Han Serif SC", "Noto Serif SC", serif',
};

export function loadSettings() {
  try {
    const raw = localStorage.getItem('bc_theme');
    const s = raw ? { ...THEME_DEFAULTS, ...JSON.parse(raw) } : { ...THEME_DEFAULTS };
    // Migrate stale chatFontColor from the old dark-mode default (#c8c0b8) to
    // the paper-mode default so chat text stays legible on the light bg.
    // User-chosen colors (anything else) are preserved.
    if (s.chatFontColor === '#c8c0b8') s.chatFontColor = THEME_DEFAULTS.chatFontColor;
    return s;
  } catch (e) { return { ...THEME_DEFAULTS }; }
}

export function saveSettings(s) {
  try { localStorage.setItem('bc_theme', JSON.stringify(s)); } catch (e) {}
}

export function applySettings(s) {
  const root = document.documentElement;
  root.style.setProperty('--t-reader-font-family', READER_FONT_FAMILIES[s.readerFontFamily] || READER_FONT_FAMILIES.mono);
  root.style.setProperty('--t-reader-font-size', s.readerFontSize + 'px');
  root.style.setProperty('--t-reader-font-color', s.readerFontColor);
  root.style.setProperty('--t-reader-font-brightness', s.readerFontBrightness);
  root.style.setProperty('--t-reader-bg-color', s.readerBgColor);
  root.style.setProperty('--t-reader-bg-brightness', s.readerBgBrightness);
  root.style.setProperty('--t-chat-font-size', s.chatFontSize + 'px');
  root.style.setProperty('--t-chat-font-color', s.chatFontColor);
  root.style.setProperty('--t-notes-font-size', s.notesFontSize + 'px');
  root.style.setProperty('--t-selection-color', s.selectionColor);
  root.style.setProperty('--t-reader-selection-color', s.readerSelectionColor);
  // 原生 EPUB 渲染激活时同步注入 iframe（主路径直通，消除 observer 防抖延迟；
  // 主题系统是唯一写入口，显式调用比等 MutationObserver 更可靠）。
  EpubReader.applyThemeFromDom();
}

export function populateThemeForm(s) {
  document.getElementById('tReaderFontFamily').value = s.readerFontFamily;
  document.getElementById('tReaderFontSize').value = s.readerFontSize;
  document.getElementById('tReaderFontColor').value = s.readerFontColor;
  document.getElementById('tReaderFontColorVal').textContent = s.readerFontColor;
  document.getElementById('tReaderFontBrightness').value = s.readerFontBrightness;
  document.getElementById('tReaderFontBrightnessVal').textContent = parseFloat(s.readerFontBrightness).toFixed(1);
  document.getElementById('tReaderBgColor').value = s.readerBgColor;
  document.getElementById('tReaderBgColorVal').textContent = s.readerBgColor;
  document.getElementById('tReaderBgBrightness').value = s.readerBgBrightness;
  document.getElementById('tReaderBgBrightnessVal').textContent = parseFloat(s.readerBgBrightness).toFixed(1);
  document.getElementById('tChatFontSize').value = s.chatFontSize;
  document.getElementById('tChatFontColor').value = s.chatFontColor;
  document.getElementById('tChatFontColorVal').textContent = s.chatFontColor;
  document.getElementById('tNotesFontSize').value = s.notesFontSize;
  document.getElementById('tSelectionColor').value = s.selectionColor;
  document.getElementById('tSelectionColorVal').textContent = s.selectionColor;
  document.getElementById('tReaderSelectionColor').value = s.readerSelectionColor;
  document.getElementById('tReaderSelectionColorVal').textContent = s.readerSelectionColor;
  var ptEl = document.getElementById('tPageTurnThreshold');
  if (ptEl) {
    ptEl.value = s.pageTurnThreshold;
    var ptValEl = document.getElementById('tPageTurnThresholdVal');
    if (ptValEl) ptValEl.textContent = s.pageTurnThreshold + ' 格';
  }
}

export function onThemeChange() {
  const s = {
    readerFontFamily: document.getElementById('tReaderFontFamily').value,
    readerFontSize: document.getElementById('tReaderFontSize').value,
    readerFontColor: document.getElementById('tReaderFontColor').value,
    readerFontBrightness: document.getElementById('tReaderFontBrightness').value,
    readerBgColor: document.getElementById('tReaderBgColor').value,
    readerBgBrightness: document.getElementById('tReaderBgBrightness').value,
    chatFontSize: document.getElementById('tChatFontSize').value,
    chatFontColor: document.getElementById('tChatFontColor').value,
    notesFontSize: document.getElementById('tNotesFontSize').value,
    selectionColor: document.getElementById('tSelectionColor').value,
    readerSelectionColor: document.getElementById('tReaderSelectionColor').value,
    pageTurnThreshold: document.getElementById('tPageTurnThreshold').value,
  };
  saveSettings(s);
  applySettings(s);
  populateThemeForm(s);
}

export function switchPreview(name) {
  // Highlight active section
  document.querySelectorAll('.theme-section').forEach(s => s.classList.remove('active-section'));
  const section = document.querySelector(`.theme-section[onclick*="${name}"]`);
  if (section) section.classList.add('active-section');

  // Sync real reader content into preview
  if (name === 'reader') {
    const previewInner = document.querySelector('#previewReader .book-page-inner');
    // Native mode: the real content lives in an iframe (separate document)
    // that can't be copied in — show a sample rendered with the live theme
    // vars instead, so the preview honestly reflects current font/colors.
    if (previewInner && S.renderMode === 'native') {
      previewInner.innerHTML = ''
        + '<div style="opacity:.7;font-size:.85em;margin-bottom:10px;">'
        + '原生 EPUB 渲染中——主题实时生效于阅读页</div>'
        + '<p>这是一段示例文字，用于预览当前阅读主题的字体与配色。</p>';
      previewInner.classList.add('real-content');
      previewInner.style.display = 'block';
      previewInner.style.alignItems = '';
      previewInner.style.justifyContent = '';
      previewInner.style.textAlign = 'left';
      return;
    }
    const realInner = document.getElementById('bookPageInner');
    if (previewInner && realInner) {
      const hasContent = realInner.textContent.trim() && realInner.textContent.trim() !== '载入中...';
      previewInner.innerHTML = realInner.innerHTML;
      previewInner.classList.toggle('real-content', hasContent);
      if (hasContent) {
        previewInner.style.display = 'block';
        previewInner.style.alignItems = '';
        previewInner.style.justifyContent = '';
        previewInner.style.textAlign = 'left';
      } else {
        previewInner.style.display = '';
        previewInner.style.alignItems = '';
        previewInner.style.justifyContent = '';
        previewInner.style.textAlign = '';
      }
    }
  }

  // Switch preview stage
  document.querySelectorAll('.preview-stage').forEach(s => s.classList.remove('active'));
  const stage = document.getElementById('preview' + name.charAt(0).toUpperCase() + name.slice(1));
  if (stage) stage.classList.add('active');
}

export function resetTheme() {
  localStorage.removeItem('bc_theme');
  applySettings(THEME_DEFAULTS);
  populateThemeForm(THEME_DEFAULTS);
}

// ── HTML onclick access ───────────────────────────────────────────
window.onThemeChange = onThemeChange;
window.switchPreview = switchPreview;
window.resetTheme = resetTheme;
