/* Book Companion — reading mode prelude.
 *
 * 三档分段药丸只出现在阅读页的空白会话里（开始对话后消失，模式随会话
 * 冻结）；选中块是一枚会滑动的药丸，位移带一点挤压/回弹的果冻感。
 */

import { S } from './state.js';
import { startRolePlaySetup } from './roleplay.js';

const MODE_LABELS = {
  immersive: '沉浸伴读',
  overview: '俯瞰分析',
  divergent: '发散探索',
  roleplay: '扮演'
};

// ── Sliding thumb ─────────────────────────────────────────────────
// 药丸位置由 JS 测量（三档宽度可能随标签/图标/容器查询变化），
// 位移动画走 WAAPI：可中断，快速连点时从当前形变处接着走，不跳帧。
let _thumbAnim = null;

function _reduceMotion() {
  return !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

/** 量出当前选中项相对凹槽的位置；预选块不可见时返回 null。 */
function _thumbGeometry() {
  const seg = document.getElementById('modeSeg');
  if (!seg) return null;
  const opt = seg.querySelector('.mode-seg-opt.active');
  const thumb = seg.querySelector('.mode-seg-thumb');
  if (!opt || !thumb) return null;
  const segRect = seg.getBoundingClientRect();
  const optRect = opt.getBoundingClientRect();
  if (optRect.width < 1) return null;   // 预选块隐藏中
  const border = parseFloat(getComputedStyle(seg).borderLeftWidth) || 0;
  return {
    seg,
    thumb,
    x: Math.round(optRect.left - segRect.left - border),
    w: Math.round(optRect.width),
  };
}

/** 读取药丸当前的 translateX（动画中途也能拿到真实值）。 */
function _thumbX(thumb) {
  const t = getComputedStyle(thumb).transform;
  if (!t || t === 'none') return 0;
  const m = t.match(/matrix\(([^)]+)\)/);
  if (m) return parseFloat(m[1].split(',')[4]) || 0;
  const m3 = t.match(/matrix3d\(([^)]+)\)/);
  return m3 ? parseFloat(m3[1].split(',')[12]) || 0 : 0;
}

/** 把药丸挪到当前选中项。animate=false 用于首次定位 / 尺寸变化。 */
export function syncThumb(animate) {
  const g = _thumbGeometry();
  if (!g) return;
  const { seg, thumb, x, w } = g;
  seg.style.setProperty('--seg-thumb-w', w + 'px');
  seg.style.setProperty('--seg-thumb-x', x + 'px');
  seg.classList.add('thumb-ready');

  const fromX = _thumbX(thumb);
  thumb.style.transform = 'translateX(' + x + 'px)';

  if (_thumbAnim) {
    try { _thumbAnim.cancel(); } catch (_) { /* already finished */ }
    _thumbAnim = null;
  }
  const dx = x - fromX;
  if (!animate || _reduceMotion() || typeof thumb.animate !== 'function' || Math.abs(dx) < 1) {
    return;
  }

  // 果冻：中段横向拉长、纵向压扁，收尾反向轻微过冲再回正。
  const stretch = Math.min(0.12, 0.05 + Math.abs(dx) / 1200);
  const overshoot = dx > 0 ? 2.5 : -2.5;
  const scale = function (sx, sy) { return ' scale(' + sx.toFixed(3) + ', ' + sy.toFixed(3) + ')'; };
  _thumbAnim = thumb.animate([
    { transform: 'translateX(' + fromX + 'px) scale(1, 1)' },
    {
      transform: 'translateX(' + (fromX + dx * 0.55).toFixed(1) + 'px)'
        + scale(1 + stretch, 1 - stretch * 0.8),
      offset: 0.45,
    },
    {
      transform: 'translateX(' + (x + overshoot).toFixed(1) + 'px)'
        + scale(1 - stretch * 0.45, 1 + stretch * 0.45),
      offset: 0.8,
    },
    { transform: 'translateX(' + x + 'px) scale(1, 1)' },
  ], {
    duration: 460,
    easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
  });
  _thumbAnim.onfinish = function () {
    thumb.style.transform = 'translateX(' + x + 'px)';
    _thumbAnim = null;
  };
}

/** 只同步分段控件的选中态；不动 S.currentMode。 */
function syncModeSeg(mode, animate) {
  const seg = document.getElementById('modeSeg');
  if (!seg) return;
  seg.querySelectorAll('.mode-seg-opt').forEach(function (opt) {
    const on = opt.dataset.mode === mode;
    opt.classList.toggle('active', on);
    opt.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  // 等一帧，让 active 类先进入布局再量药丸的目标位置。
  requestAnimationFrame(function () { syncThumb(!!animate); });
}

export function setMode(mode) {
  if (mode === 'roleplay') {
    startRolePlaySetup();
    return;
  }
  if (!MODE_LABELS[mode]) return;
  S.currentMode = mode;
  S.currentCharacterName = '';
  S.currentRoleKind = 'character';
  S.currentRoleRef = '';
  S.currentMaxChapter = null;
  syncModeSeg(mode, true);

  // Reset role-play entry button label when leaving role-play
  var rpLabel = document.querySelector('#roleplayEntryBtn .mode-label');
  if (rpLabel) rpLabel.textContent = '角色对话';
}

/** 预选块只在「阅读页 + 空白会话」出现。发出第一条消息后它消失，
 *  该会话的阅读模式也就此固定（界面不再提供改模式的入口）。 */
export function updateModePrelude() {
  const el = document.getElementById('modePrelude');
  if (!el) return;
  const onReader = window._currentTab === 'reader';
  const fresh = (S.chatHistory || []).length === 0 && !S.currentSessionId;
  const show = onReader && fresh;
  el.classList.toggle('visible', show);
  // display:none → flex 之后才量得到宽度：下一帧把药丸摆到选中项。
  if (show) requestAnimationFrame(function () { syncThumb(false); });
}

// 方向键在三档之间移动（radiogroup 的标准键盘行为）；Tab 仍逐项可达。
function onSegKeydown(e) {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  const opts = Array.prototype.slice.call(
    document.querySelectorAll('#modeSeg .mode-seg-opt'));
  const idx = opts.indexOf(document.activeElement);
  if (idx < 0) return;
  e.preventDefault();
  const next = (idx + (e.key === 'ArrowRight' ? 1 : -1) + opts.length) % opts.length;
  opts[next].focus();
  setMode(opts[next].dataset.mode);
}

const segEl = document.getElementById('modeSeg');
if (segEl) {
  segEl.addEventListener('keydown', onSegKeydown);
  // 面板拖窄会收起标签（容器查询），药丸宽度跟着变——重新量一次。
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(function () { syncThumb(false); }).observe(segEl);
  }
}

// ── HTML onclick access ───────────────────────────────────────────
window.setMode = setMode;
window.updateModePrelude = updateModePrelude;
window.syncModeThumb = syncThumb;

// setModeLabel — lightweight: updates the segmented control only, doesn't
// touch S.currentMode. Used by the session loader to restore the badge.
window.setModeLabel = function (mode) { syncModeSeg(mode, false); };
