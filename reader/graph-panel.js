/* BookGraph panel — knowledge graph extraction UI.
 *
 * Adds a "🕸" button on each book card. Click opens a modal showing
 * extraction progress (live SSE) + final stats.
 */

import { S } from './js/state.js';
import { escapeHtml } from './js/utils.js';

// ── Modal HTML (injected once) ────────────────────────────────────

function ensureGraphModal() {
  if (document.getElementById('graphModal')) return;
  const modal = document.createElement('div');
  modal.id = 'graphModal';
  modal.className = 'graph-modal';
  modal.innerHTML = `
    <div class="graph-modal-backdrop" onclick="closeGraphPanel()"></div>
    <div class="graph-modal-content">
      <div class="graph-modal-header">
        <h2 id="graphModalTitle">图谱分析</h2>
        <button class="graph-modal-close" onclick="closeGraphPanel()">×</button>
      </div>
      <div class="graph-modal-body" id="graphModalBody">
        <!-- dynamic content -->
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

// ── Open / close ──────────────────────────────────────────────────

let currentBookTitle = '';

function openGraphPanel(bookId, bookTitle) {
  currentBookTitle = bookTitle || bookId;
  ensureGraphModal();
  document.getElementById('graphModalTitle').textContent = '图谱分析 — ' + currentBookTitle;
  document.getElementById('graphModal').style.display = 'flex';
  loadGraphStatus(bookId);
}

function closeGraphPanel() {
  const modal = document.getElementById('graphModal');
  if (modal) modal.style.display = 'none';
  // Note: SSE lifecycle is now managed by the floating pill
  // (graph-progress-ui.js).  Do NOT close it here.
}

// ── Load existing status / start extraction ───────────────────────

async function loadGraphStatus(bookId) {
  const body = document.getElementById('graphModalBody');
  body.innerHTML = '<div class="graph-loading">检查现有图谱...</div>';

  try {
    const statsResp = await fetch(S.API + '/api/graph/stats/' + bookId);
    const statsData = await statsResp.json();
    const stats = statsData.stats;

    if (stats && stats.chunks_total > 0) {
      renderGraphStats(bookId, stats);
      if (stats.chunks_done < stats.chunks_total) {
        // Delegate live progress to the floating pill
        if (typeof window.startExtractionProgress === 'function') {
          window.startExtractionProgress(bookId, currentBookTitle);
        }
      }
      return;
    }
  } catch (e) {
    // No graph yet — fall through to start
  }

  body.innerHTML = `
    <div class="graph-start">
      <p>这本书还没有知识图谱。</p>
      <p class="graph-start-hint">点击下方按钮开始提取。提取过程会分析全书内容，构建人物、地点、事件、关系的知识图谱。</p>
      <button class="graph-start-btn" onclick="startGraphExtraction('${bookId}')">
        开始图谱分析
      </button>
    </div>
  `;
}

async function startGraphExtraction(bookId) {
  const body = document.getElementById('graphModalBody');
  body.innerHTML = '<div class="graph-loading">启动提取...</div>';

  try {
    const resp = await fetch(S.API + '/api/graph/init/' + bookId + '?batch_size=5', {
      method: 'POST',
    });
    const data = await resp.json();

    if (data.error) {
      body.innerHTML = `<div class="graph-error">${data.error}</div>`;
      return;
    }

    // Delegate live progress to the floating pill
    if (typeof window.startExtractionProgress === 'function') {
      window.startExtractionProgress(bookId, currentBookTitle);
    }
  } catch (e) {
    body.innerHTML = `<div class="graph-error">启动失败：${e.message}</div>`;
  }
}

// ── SSE progress listener ─────────────────────────────────────────
// Live progress is now rendered by the floating pill (graph-progress-ui.js).
// This function is kept as a backward-compatible entry point.

function startProgressSSE(bookId) {
  const body = document.getElementById('graphModalBody');
  body.innerHTML = `
    <div class="graph-progress-container">
      <p style="text-align:center;color:var(--text-secondary);font-size:13px">
        <i data-lucide="network"></i> 实时进度已移至<span style="color:var(--accent)">顶部悬浮窗</span>
      </p>
      <p style="text-align:center;font-size:12px;color:var(--text-secondary)">
        点击顶部灵动岛展开查看事件流
      </p>
    </div>
  `;
  if (typeof window.startExtractionProgress === 'function') {
    window.startExtractionProgress(bookId, currentBookTitle);
  }
}

// ── Stats rendering ───────────────────────────────────────────────

function renderStatsInline(stats) {
  const parts = [];
  if (stats.nodes) parts.push(`节点 ${stats.nodes}`);
  if (stats.edges) parts.push(`边 ${stats.edges}`);
  if (stats.aliases) parts.push(`别名 ${stats.aliases}`);
  if (stats.merge_candidates) parts.push(`合并候选 ${stats.merge_candidates}`);
  return parts.join(' · ');
}

function renderGraphStats(bookId, stats) {
  const body = document.getElementById('graphModalBody');
  const done = stats.chunks_done || 0;
  const total = stats.chunks_total || 0;
  const failed = stats.chunks_failed || 0;
  const pct = total > 0 ? Math.round(done / total * 100) : 0;

  let kindsHtml = '';
  if (stats.kinds) {
    kindsHtml = '<div class="graph-kinds">';
    for (const [kind, count] of Object.entries(stats.kinds)) {
      kindsHtml += `<div class="graph-kind-tag"><span class="graph-kind-name">${kind}</span><span class="graph-kind-count">${count}</span></div>`;
    }
    kindsHtml += '</div>';
  }

  const isIncomplete = done < total;

  body.innerHTML = `
    <div class="graph-stats-container">

      <div class="graph-stats-grid">
        <div class="graph-stat-card">
          <div class="graph-stat-num">${stats.nodes || 0}</div>
          <div class="graph-stat-label">实体节点</div>
        </div>
        <div class="graph-stat-card">
          <div class="graph-stat-num">${stats.edges || 0}</div>
          <div class="graph-stat-label">关系边</div>
        </div>
        <div class="graph-stat-card">
          <div class="graph-stat-num">${stats.aliases || 0}</div>
          <div class="graph-stat-label">别名</div>
        </div>
        <div class="graph-stat-card">
          <div class="graph-stat-num">${stats.merge_candidates || 0}</div>
          <div class="graph-stat-label">合并候选</div>
        </div>
      </div>

      ${kindsHtml}

      <p style="text-align:center;font-size:13px;color:var(--text-secondary)">
        ${done} / ${total} chunks${failed > 0 ? ` (${failed} failed)` : ''}
        ${isIncomplete ? ` · ${pct}% 完成` : ' · <i data-lucide="check"></i> 全部完成'}
      </p>

      <div class="graph-stats-actions">
        ${isIncomplete ? `
          <button class="graph-action-btn primary" onclick="window.startExtractionProgress('${bookId}','${(currentBookTitle || '').replace(/'/g, "\\'")}')">查看实时进度</button>
          <button class="graph-action-btn" onclick="startGraphExtraction('${bookId}')">继续提取</button>
        ` : `
          <button class="graph-action-btn" onclick="viewGraphNodes('${bookId}')">查看节点列表</button>
          <button class="graph-action-btn danger" onclick="resetGraph('${bookId}')">删除重建</button>
        `}
      </div>

      <div id="graphNodesList" class="graph-nodes-list" style="display:none"></div>
    </div>
  `;
}

async function viewGraphNodes(bookId) {
  const list = document.getElementById('graphNodesList');
  list.style.display = 'block';
  list.innerHTML = '<div class="graph-loading">加载节点...</div>';

  try {
    const resp = await fetch(S.API + '/api/graph/nodes/' + bookId + '?limit=200');
    const data = await resp.json();
    const nodes = data.nodes || [];

    const byKind = {};
    for (const n of nodes) {
      if (!byKind[n.kind]) byKind[n.kind] = [];
      byKind[n.kind].push(n);
    }

    let html = '';
    for (const [kind, ns] of Object.entries(byKind)) {
      html += `<div class="graph-kind-group"><h3>${kind} (${ns.length})</h3>`;
      for (const n of ns) {
        const desc = n.description ? escapeHtml(n.description.slice(0, 80)) : '';
        html += `<div class="graph-node-item"><span class="graph-node-name">${escapeHtml(n.canonical_name)}</span>${desc ? `<span class="graph-node-desc">${desc}</span>` : ''}</div>`;
      }
      html += '</div>';
    }
    list.innerHTML = html || '<div class="graph-empty">无节点</div>';
  } catch (e) {
    list.innerHTML = `<div class="graph-error">加载失败：${e.message}</div>`;
  }
}

async function resetGraph(bookId) {
  if (!confirm('确定删除这本书的图谱？此操作不可恢复。')) return;
  try {
    await fetch(S.API + '/api/graph/' + bookId, { method: 'DELETE' });
    loadGraphStatus(bookId);
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

// ── HTML onclick access ───────────────────────────────────────────
window.openGraphPanel = openGraphPanel;
window.closeGraphPanel = closeGraphPanel;
window.startGraphExtraction = startGraphExtraction;
window.viewGraphNodes = viewGraphNodes;
window.resetGraph = resetGraph;
window.startProgressSSE = startProgressSSE;
