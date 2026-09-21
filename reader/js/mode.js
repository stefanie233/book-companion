/* Book Companion — mode picker. */

import { S } from './state.js';
import { startRolePlaySetup } from './roleplay.js';

const MODE_LABELS = {
  immersive: '沉浸伴读',
  overview: '俯瞰分析',
  divergent: '发散探索',
  roleplay: '<i data-lucide="drama"></i> 扮演'
};

export function toggleModePicker() {
  const picker = document.getElementById('modePicker');
  const toggle = document.getElementById('modeToggle');
  if (!picker || !toggle) return;

  const isVisible = picker.classList.contains('visible');
  if (!isVisible) {
    // Position the picker above the toggle button (fixed, escapes all overflow parents)
    const rect = toggle.getBoundingClientRect();
    picker.style.position = 'fixed';
    picker.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
    picker.style.left = rect.left + 'px';
  }
  picker.classList.toggle('visible');
}

export function setMode(mode) {
  if (mode === 'roleplay') {
    startRolePlaySetup();
    return;
  }
  if (!MODE_LABELS[mode]) return;
  S.currentMode = mode;
  S.currentCharacterName = '';
  S.currentMaxChapter = null;
  document.getElementById('modeLabel').innerHTML = MODE_LABELS[mode];
  document.querySelectorAll('.mode-option').forEach(function (opt) {
    opt.classList.toggle('active', opt.dataset.mode === mode);
  });
  document.getElementById('modePicker').classList.remove('visible');

  // Reset role-play entry button label on home page
  var rpLabel = document.querySelector('#roleplayEntryBtn .mode-label');
  if (rpLabel) rpLabel.textContent = '角色对话';
}

// ── HTML onclick access ───────────────────────────────────────────
window.toggleModePicker = toggleModePicker;
window.setMode = setMode;

// setModeLabel — lightweight: updates UI labels only, doesn't change S.currentMode.
// Used by session loader to restore the mode badge without triggering side effects.
window.setModeLabel = function (mode) {
  var label = MODE_LABELS[mode] || mode;
  document.getElementById('modeLabel').innerHTML = label;
  document.querySelectorAll('.mode-option').forEach(function (opt) {
    opt.classList.toggle('active', opt.dataset.mode === mode);
  });
};
