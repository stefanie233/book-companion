/* Liubai — context usage ring: 上下文水位圆环 + 明细 popover.
 *
 * 数据源: SSE done 事件的 usage payload(实时) + GET /api/context/usage(切会话回查).
 * 口径: 总量为最后一轮 API usage(权威), 五块按本地占比对齐, 明细和恒等于总量.
 */

import { S } from './state.js';

// 块 key → 展示名/色点。key 与后端 context_meter.BLOCK_KEYS 对齐, 勿改
const BLOCK_META = {
  system:       { label: '系统提示',   cls: 'cu-c1' },
  tools:        { label: '工具定义',   cls: 'cu-c2' },
  conversation: { label: '对话历史',   cls: 'cu-c3' },
  context:      { label: '阅读上下文', cls: 'cu-c4' },
  input:        { label: '本轮输入',   cls: 'cu-c5' },
};

const RING_R = 9;
const RING_C = 2 * Math.PI * RING_R; // 56.55

let _usage = null;   // latest payload from backend
let _seq = 0;        // stale-response guard for fetch

function fmtK(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + 'K' : String(n);
}

function levelOf(pct, ratio) {
  if (pct >= ratio * 100 * 1.7) return 'alert';  // ≈60% @35% line
  if (pct >= ratio * 100) return 'warn';
  return '';
}

// ── Render: collapsed ring ─────────────────────────────────────────

function renderRing(u) {
  const btn = document.getElementById('ctxUsageBtn');
  if (!btn) return;
  const val = btn.querySelector('.ctx-ring-value');
  if (!val) return;
  const pct = Math.min(u.percent / 100, 1);
  val.setAttribute('stroke-dasharray', RING_C.toFixed(2));
  val.setAttribute('stroke-dashoffset', (RING_C * (1 - pct)).toFixed(2));
  val.setAttribute('class', 'ctx-ring-value ' + levelOf(u.percent, u.alert_ratio || 0.35));
  btn.title = '上下文用量 ' + u.percent.toFixed(1) + '% · '
    + fmtK(u.total) + '/' + fmtK(u.window);
}

// ── Render: expanded popover ───────────────────────────────────────

function renderPop(u) {
  const pop = document.getElementById('ctxUsagePop');
  if (!pop) return;
  const est = u.estimated || u.tokenizer_estimated;
  let rows = '';
  for (const b of (u.blocks || [])) {
    const meta = BLOCK_META[b.key];
    if (!meta) continue;
    rows += '<div class="ctx-pop-row">'
      + '<span class="ctx-dot ' + meta.cls + '"></span>'
      + '<span class="ctx-row-label">' + meta.label + '</span>'
      + '<span class="ctx-row-tokens">' + fmtK(b.tokens) + '</span>'
      + '</div>';
  }
  if (!rows) rows = '<div class="ctx-pop-empty">发送一条消息后查看明细</div>';
  pop.innerHTML =
    '<div class="ctx-pop-head">'
    +   '<span class="ctx-pop-pct ' + levelOf(u.percent, u.alert_ratio || 0.35) + '">'
    +     u.percent.toFixed(1) + '%</span>'
    +   '<span class="ctx-pop-total">' + fmtK(u.total) + ' / ' + fmtK(u.window) + '</span>'
    + '</div>'
    + '<div class="ctx-pop-sub">上下文用量 · ' + (u.model || '')
    +   (est ? ' · 估算' : '') + '</div>'
    + '<div class="ctx-pop-rows">' + rows + '</div>'
    + '<div class="ctx-pop-foot">预警线 '
    +   Math.round((u.alert_ratio || 0.35) * 100) + '% · '
    +   fmtK(Math.round(u.window * (u.alert_ratio || 0.35)))
    +   (u.alert ? ' · 已越线' : '') + '</div>';
}

// ── Public API ─────────────────────────────────────────────────────

/** SSE done 事件入口: 用最新快照刷新环与 popover。 */
export function updateUsage(payload) {
  if (!payload || typeof payload !== 'object') return;
  _usage = payload;
  renderRing(_usage);
  renderPop(_usage);
}

/** 会话切换/首载回查: GET /api/context/usage。 */
export async function refreshUsage() {
  const seq = ++_seq;
  try {
    const params = new URLSearchParams();
    if (S.currentSessionId) params.set('session_id', S.currentSessionId);
    if (S.currentMode) params.set('mode', S.currentMode);
    const resp = await fetch(S.API + '/api/context/usage?' + params.toString());
    const data = await resp.json();
    if (seq !== _seq || !data || !data.ok) return;
    updateUsage(data);
  } catch (_) { /* ring is best-effort */ }
}

export function toggleCtxUsage() {
  const pop = document.getElementById('ctxUsagePop');
  const btn = document.getElementById('ctxUsageBtn');
  if (!pop || !btn) return;
  if (!pop.classList.contains('visible')) {
    const rect = btn.getBoundingClientRect();
    // 药丸行在窄面板下会折成两行（.chat-mode-bar 的 flex-wrap）。只锚按钮的话，
    // 按钮落到第二行时面板会压住第一行药丸——锚到整条 bar 的上沿，永远让开。
    const bar = btn.closest('.chat-mode-bar');
    const top = bar ? Math.min(rect.top, bar.getBoundingClientRect().top) : rect.top;
    pop.style.position = 'fixed';
    pop.style.bottom = (window.innerHeight - top + 6) + 'px';
    pop.style.right = (window.innerWidth - rect.right) + 'px';
    pop.style.left = 'auto';
    if (_usage) renderPop(_usage);
  }
  pop.classList.toggle('visible');
}

// 点击别处收起 popover(与 mode-picker 同款行为)
document.addEventListener('click', function (e) {
  const pop = document.getElementById('ctxUsagePop');
  if (!pop || !pop.classList.contains('visible')) return;
  if (pop.contains(e.target) || (e.target.closest && e.target.closest('#ctxUsageBtn'))) return;
  pop.classList.remove('visible');
});

// window 挂载: index.html onclick 使用
window.toggleCtxUsage = toggleCtxUsage;
