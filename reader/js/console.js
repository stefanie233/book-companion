// Console page module — provider/slot management (page-console).
// Layout: slot status band (top) + two-tier filter (capability × vendor)
// + provider card grid. Server hot-applies every change to the shared
// Config instance, so this module only renders snapshots and reloads
// state after each mutation.
// v2: a provider is a kind-less vendor connection; every model entry
// carries its own kind ({id, kind}), and slots bind to (provider, model).

const SLOT_META = {
  main_llm:  { label: '主模型 LLM', short: '主模型', kind: 'llm', desc: '对话 + 全部后台管线（图谱/记忆/清洗）', modelOptional: false },
  embedding: { label: 'Embedding', short: 'Embedding', kind: 'embedding', desc: '语义检索（预留，消费端接入后生效）', modelOptional: false },
  tts:       { label: 'TTS 语音', short: 'TTS', kind: 'tts', desc: '角色扮演语音；不绑定则回退主模型密钥', modelOptional: true },
  asr:       { label: '语音转写 ASR', short: 'ASR', kind: 'asr', desc: '云端 ASR 转录；绑定则 /api/asr 走云端，不绑定用本地 SenseVoice', modelOptional: false },
};

// kind → slot it can bind to. Kinds without a slot (e.g. reserved) still
// show up in the grid but have no bind button.
const KIND_SLOT = { llm: 'main_llm', embedding: 'embedding', tts: 'tts', asr: 'asr' };
const KIND_ORDER = ['llm', 'embedding', 'tts', 'asr'];

let SNAP = null;
let editId = null;
let FILTER = { cap: 'all', vendor: 'all' };
// pid -> picked model id (card-level single pick). Clicking a model row
// selects it; the card's bind buttons then bind THIS model (falls back to
// the first model of that kind when nothing is picked).
let PICK = {};

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function api(method, url, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function toast(msg, isErr) {
  let el = document.getElementById('consoleToast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'consoleToast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = isErr ? 'console-toast err' : 'console-toast';
  el.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.style.display = 'none'; }, 2600);
}

// ── Derived data ─────────────────────────────────────────────────────

function vendorOf(p) {
  return (p.vendor || '').trim() || (p.name || '').trim().split(/\s+/)[0] || '?';
}

function boundProvider(slot) {
  return SNAP.providers.find(p => p.id === SNAP.slots[slot].provider_id) || null;
}

function capsInUse() {
  const present = new Set();
  SNAP.providers.forEach(p => (p.models || []).forEach(m => present.add(m.kind)));
  Object.values(SNAP.slots).forEach(s => present.add(s.kind));
  return KIND_ORDER.filter(k => present.has(k));
}

function vendorsInUse() {
  return [...new Set(SNAP.providers.map(vendorOf))].sort((a, b) => a.localeCompare(b));
}

// ── Console secondary nav: 驱动控制台 / 用量分析 ─────────────────────
// Mirror of switchGraphView (graph-viz.js): toggle active + display,
// lazy-refresh the usage view on entry. View choice persists in
// localStorage under 'bc_console_view'.

let _consoleView = localStorage.getItem('bc_console_view') || 'drivers';

function switchConsoleView(view) {
  _consoleView = view;
  localStorage.setItem('bc_console_view', view);
  document.querySelectorAll('#consoleSecNav .console-secnav-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.view === view));
  document.getElementById('consoleViewDrivers').style.display = view === 'drivers' ? '' : 'none';
  document.getElementById('consoleViewUsage').style.display = view === 'usage' ? '' : 'none';
  if (view === 'usage' && window.UsageUI) window.UsageUI.load();
}

// ── Render: slot status band ─────────────────────────────────────────

async function load() {
  SNAP = await api('GET', '/api/console/config');
  renderBand();
  renderFilter();
  renderGrid();
}

function renderBand() {
  const host = document.getElementById('consoleBand');
  if (!host) return;
  host.innerHTML = Object.entries(SLOT_META).map(([slot, meta]) => {
    const s = SNAP.slots[slot];
    const r = s.resolved || {};
    const bound = boundProvider(slot);
    const env = s.source !== 'console';
    const val = bound
      ? `${vendorOf(bound)} · ${s.model || r.model || '?'}`
      : `.env · ${r.model || '默认'}`;
    const src = env
      ? '<span class="console-src">继承 .env</span>'
      : '<span class="console-src console-src-on">控制台</span>';
    const modelField = !env ? `
      <div class="console-band-acwrap" onclick="event.stopPropagation()">
        <input class="console-band-model" value="${esc(s.model || '')}"
               placeholder="模型名" title="输入模型名回车绑定到当前连接；点 ▾ 浏览全部"
               onchange="ConsoleUI.onModelChange('${slot}', this.value)">
        <button class="console-band-caret" title="展开模型列表"
                onmousedown="event.preventDefault()"
                onclick="ConsoleUI.toggleAc('${slot}')">▾</button>
        <div class="console-band-ac" id="ac-${slot}" data-slot="${slot}"
             onmousedown="ConsoleUI.acPick(event, this)">
          <input class="console-ac-search" placeholder="搜索模型或供应商…"
                 oninput="ConsoleUI.searchModels('${slot}', this.value)"
                 onkeydown="if(event.key==='Escape')ConsoleUI.acClose('${slot}')">
          <div class="console-ac-list"></div>
        </div>
      </div>` : '';
    const testBtn = s.kind === 'tts'
      ? `<button class="console-btn" onclick="event.stopPropagation(); ConsoleUI.previewTts()">▶ 试听</button>
         <button class="console-btn" onclick="event.stopPropagation(); ConsoleUI.testSlot('${slot}')">测试</button>`
      : `<button class="console-btn" onclick="event.stopPropagation(); ConsoleUI.testSlot('${slot}')">测试</button>`;
    return `
      <div class="console-band-card" title="${esc(meta.desc)} — 点击筛选 ${esc(s.kind)} 连接"
           onclick="ConsoleUI.focusCap('${s.kind}')">
        <div class="console-band-head">
          <span class="console-sum-dot${env ? ' env' : ''}"></span>
          <span class="console-band-label">${esc(meta.label)}</span>
          ${src}
        </div>
        <div class="console-band-value">${esc(val)}</div>
        ${modelField}
        <div class="console-band-foot">
          <span class="console-test-out" id="band-out-${slot}"></span>
          ${testBtn}
        </div>
      </div>`;
  }).join('');
}

// ── Render: two-tier filter ──────────────────────────────────────────

function renderFilter() {
  const host = document.getElementById('consoleFilterBar');
  if (!host) return;
  const row = (label, key, values) => `
    <div class="console-frow">
      <span class="console-flabel">${label}</span>
      ${values.map(v => {
        const val = v === 'all' ? 'all' : v;
        const text = v === 'all' ? '不限' : v;
        const on = FILTER[key] === val ? ' on' : '';
        return `<span class="console-fopt${on}" onclick="ConsoleUI.setFilter('${key}', '${esc(val)}')">${esc(text)}</span>`;
      }).join('')}
    </div>`;
  host.innerHTML =
    row('能力', 'cap', ['all', ...capsInUse()]) +
    row('供应商', 'vendor', ['all', ...vendorsInUse()]);
}

function setFilter(key, value) {
  FILTER[key] = value;
  renderFilter();
  renderGrid();
}

function focusCap(cap) {
  FILTER.cap = cap;
  renderFilter();
  renderGrid();
  document.getElementById('consoleFilterBar')
    ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── Render: provider card grid ───────────────────────────────────────

function renderGrid() {
  const host = document.getElementById('consoleProviders');
  const count = document.getElementById('consoleCount');
  const list = SNAP.providers.filter(p =>
    (FILTER.cap === 'all' || (p.models || []).some(m => m.kind === FILTER.cap)) &&
    (FILTER.vendor === 'all' || vendorOf(p) === FILTER.vendor));
  if (count) count.textContent = `共 ${list.length} 个连接档案`;

  if (!list.length) {
    host.innerHTML = `<div class="console-effective">${
      SNAP.providers.length ? '该筛选条件下暂无连接档案。' : '暂无连接档案，点击下方"新增连接"添加。'
    }</div>`;
    return;
  }

  host.innerHTML = list.map(p => {
    const vendor = vendorOf(p);
    const subName = p.name !== vendor ? `<div class="console-pcard-name">${esc(p.name)}</div>` : '';
    const models = p.models || [];
    // Capability filter narrows the ROWS too: picking "向量" shows only
    // embedding models on matched cards, not the full catalog.
    const shown = FILTER.cap === 'all' ? models : models.filter(m => m.kind === FILTER.cap);
    // kind -> bound model, when this provider serves the slot
    const boundKinds = {};
    Object.entries(SNAP.slots).forEach(([slot, s]) => {
      if (s.provider_id === p.id) boundKinds[SLOT_META[slot].kind] = s.model || '';
    });
    const modelRows = shown.map(m => {
      const live = boundKinds[m.kind] === m.id;
      const sel = PICK[p.id] === m.id;
      return `<div class="console-mrow${live ? ' on' : ''}${sel ? ' sel' : ''}" data-model="${esc(m.id)}">
        <span class="console-mrow-id" title="${esc(m.id)}">${esc(m.id)}</span>
        <span class="console-kind-chip">${esc(m.kind)}</span>
      </div>`;
    }).join('');
    const bindRows = Object.entries(KIND_SLOT)
      .filter(([kind]) => shown.some(m => m.kind === kind))
      .map(([kind, slot]) => {
        const bound = SNAP.slots[slot].provider_id === p.id;
        return `<button class="console-btn${bound ? '' : ' console-btn-bind'}"
          onclick="ConsoleUI.bindProvider('${p.id}', '${kind}')">${
          bound ? `解绑${esc(SLOT_META[slot].short)}` : `绑定到${esc(SLOT_META[slot].short)}`}</button>`;
      }).join('');
    const boundTags = Object.entries(boundKinds).map(([kind, model]) =>
      `<span class="console-bind-tag">→ ${esc(SLOT_META[KIND_SLOT[kind]].short)}${model ? ' · ' + esc(model) : ''}</span>`).join('');
    return `
      <div class="console-pcard">
        <div class="console-pcard-head">
          <span class="console-pcard-vendor">${esc(vendor)}</span>
          ${boundTags}
        </div>
        ${subName}
        <div class="console-mrows" title="点击模型行选中，再点下方绑定按钮完成绑定"
             onclick="ConsoleUI.pickCard(event, '${p.id}')">${modelRows || '<div class="console-effective">未缓存模型</div>'}</div>
        <div class="console-pcard-meta">${esc(p.base_url)} · key ${esc(p.api_key || '(空)')}</div>
        <div class="console-pcard-foot">
          <span class="console-test-out" id="test-out-${p.id}"></span>
          <div class="console-pcard-actions">
            <button class="console-btn" onclick="ConsoleUI.testProvider('${p.id}')">测试</button>
            <button class="console-btn" onclick="ConsoleUI.openEditor('${p.id}')">编辑</button>
            ${bindRows}
            <button class="console-btn console-btn-danger" ${Object.keys(boundKinds).length ? 'disabled title="先解绑槽位再删除"' : ''} onclick="ConsoleUI.remove('${p.id}')">删除</button>
          </div>
        </div>
      </div>`;
  }).join('');
}

// ── Mutations ────────────────────────────────────────────────────────

async function bindProvider(pid, kind) {
  const p = SNAP.providers.find(x => x.id === pid);
  if (!p || !kind) return;
  const slot = KIND_SLOT[kind];
  if (!slot) {
    toast(`该类型 (${kind}) 暂无可绑定槽位`, true);
    return;
  }
  try {
    if (SNAP.slots[slot].provider_id === pid) {
      await api('PUT', `/api/console/slots/${slot}`, { provider_id: '' });
      toast(`已从${SLOT_META[slot].short}解绑，恢复 .env 配置`);
    } else {
      // Picked model wins when its kind matches the button; otherwise the
      // first model of that kind, with a hint when a pick was skipped.
      const picked = (PICK[pid] && (p.models || []).find(m => m.id === PICK[pid] && m.kind === kind)) || null;
      const model = (picked || (p.models || []).find(m => m.kind === kind) || {}).id || '';
      if (PICK[pid] && !picked && model) {
        const pk = ((p.models || []).find(m => m.id === PICK[pid]) || {}).kind;
        toast(`选中的 ${PICK[pid]} 是 ${pk || '?'} 模型，已绑定该类型默认模型`, true);
      }
      if (!model && !SLOT_META[slot].modelOptional) {
        toast('该档案未缓存该类型模型，请先「编辑」拉取模型', true);
        return;
      }
      await api('PUT', `/api/console/slots/${slot}`, { provider_id: pid, model });
      delete PICK[pid]; // pick consumed
      toast(`已绑定到${SLOT_META[slot].short}，立即生效`);
    }
    await load();
  } catch (e) {
    toast(e.message, true);
    await load();
  }
}

// Card model-row pick: select (or deselect) a model on this card; the
// card's bind buttons then target it. Event delegation keeps model ids
// out of inline JS (same pattern as acPick).
function pickCard(event, pid) {
  const row = event.target.closest('.console-mrow');
  if (!row || !row.dataset.model) return;
  if (PICK[pid] === row.dataset.model) delete PICK[pid]; // click again to unpick
  else PICK[pid] = row.dataset.model;
  renderGrid();
}

async function onModelChange(slot, value) {
  const pid = SNAP.slots[slot].provider_id;
  if (!pid) {
    toast('请先绑定连接', true);
    await load();
    return;
  }
  try {
    if (!value || !value.trim()) {
      toast('模型名不能为空', true);
      return;
    }
    await api('PUT', `/api/console/slots/${slot}`, { provider_id: pid, model: value.trim() });
    toast('模型已切换，立即生效');
  } catch (e) {
    toast(e.message, true);
  }
  await load();
}

// ── Band model picker: WPS-font-style dropdown panel ─────────────────
// 显示框(可手输回车绑定) + ▾ 展开 → 面板内搜索框 + 全量列表(左名右商)。

function toggleAc(slot) {
  const host = document.getElementById(`ac-${slot}`);
  if (!host) return;
  if (host.classList.contains('open')) { acClose(slot); return; }
  closeAllAc();
  renderAcList(slot, '');
  host.classList.add('open');
  const search = host.querySelector('.console-ac-search');
  if (search) search.focus();
}

function renderAcList(slot, q) {
  const host = document.getElementById(`ac-${slot}`);
  if (!host) return;
  const kind = SLOT_META[slot].kind;
  const s = String(q || '').trim().toLowerCase();
  const hits = [];
  for (const p of SNAP.providers) {
    const vendor = vendorOf(p) || p.name;
    for (const m of p.models || []) {
      if (m.kind !== kind) continue;
      // 命中模型名或供应商名——支持「输小米列出小米全部模型」
      if (s && !m.id.toLowerCase().includes(s) && !vendor.toLowerCase().includes(s)) continue;
      hits.push({ pid: p.id, model: m.id, vendor });
    }
  }
  const list = host.querySelector('.console-ac-list');
  if (!list) return;
  list.innerHTML = hits.length ? hits.map(h => `
    <div class="console-ac-item" data-pid="${esc(h.pid)}" data-model="${esc(h.model)}">
      <span class="console-ac-model" title="${esc(h.model)}">${esc(h.model)}</span>
      <span class="console-ac-vendor">${esc(h.vendor)}</span>
    </div>`).join('')
    : '<div class="console-ac-empty">无匹配模型</div>';
}

function searchModels(slot, q) { renderAcList(slot, q); }

// 容器级委托：从 data-* 取值，模型名不走 JS 字符串内插，无注入面
function acPick(e, host) {
  const item = e.target.closest('.console-ac-item');
  if (!item) return;
  e.preventDefault();  // 不抢焦点，避开显示框 onchange 的竞争
  const slot = host.dataset.slot;
  acClose(slot);
  pickModel(slot, item.dataset.pid, item.dataset.model);
}

async function pickModel(slot, pid, model) {
  try {
    await api('PUT', `/api/console/slots/${slot}`, { provider_id: pid, model });
    toast('模型已切换，立即生效');
  } catch (e) {
    toast(e.message, true);
  }
  await load();
}

function acClose(slot) {
  const host = document.getElementById(`ac-${slot}`);
  if (!host) return;
  host.classList.remove('open');
  const search = host.querySelector('.console-ac-search');
  if (search) search.value = '';
  const list = host.querySelector('.console-ac-list');
  if (list) list.innerHTML = '';
}

function closeAllAc() {
  Object.keys(SLOT_META).forEach(acClose);
}

// 点击面板外收起（band 重渲染不影响这条常驻监听）
document.addEventListener('click', (e) => {
  if (e.target.closest('.console-band-acwrap')) return;
  closeAllAc();
});

async function testProvider(pid, outId, model, kind) {
  const p = SNAP.providers.find(x => x.id === pid);
  const out = document.getElementById(outId || `test-out-${pid}`);
  if (out) {
    out.textContent = '测试中…';
    out.className = 'console-test-out';
  }
  // probe target: explicit slot model > first slot-able kind's model > first cached
  let body = {};
  if (model || kind) {
    body = { model: model || '', kind: kind || '' };
  } else if (p) {
    const models = p.models || [];
    const slotKind = Object.keys(KIND_SLOT).find(k => models.some(m => m.kind === k));
    const t = models.find(m => m.kind === slotKind) || models[0];
    if (t) body = { model: t.id, kind: t.kind };
  }
  try {
    const r = await api('POST', `/api/console/providers/${pid}/test`, body);
    if (!r.ok) {
      throw new Error(`${r.error}${r.detail ? ' — ' + r.detail : ''}`);
    }
    const msg = `✓ ${r.latency_ms}ms ${r.detail || ''}`;
    if (out) {
      out.textContent = msg;
      out.className = 'console-test-out ok';
    }
  } catch (e) {
    if (out) {
      out.textContent = `✗ ${e.message}`;
      out.className = 'console-test-out bad';
    }
  }
}

async function testSlot(slot) {
  const pid = SNAP.slots[slot].provider_id;
  if (!pid) {
    toast('未绑定连接，无法测试', true);
    return;
  }
  await testProvider(pid, `band-out-${slot}`, SNAP.slots[slot].model || '', SLOT_META[slot].kind);
}

// ── Editor overlay ───────────────────────────────────────────────────

function ensureOverlay() {
  if (document.getElementById('consoleEditorOverlay')) return;
  const div = document.createElement('div');
  div.id = 'consoleEditorOverlay';
  div.className = 'console-overlay';
  div.innerHTML = `
    <div class="console-dialog">
      <h3><i data-lucide="plug"></i> <span id="ceTitle">新增连接</span></h3>
      <label class="console-field">供应商 (vendor)
        <input id="ceVendor" list="ceVendorList" placeholder="如 MiMo / 千问 / DeepSeek / GLM">
        <datalist id="ceVendorList"></datalist>
      </label>
      <label class="console-field">显示名（备注，可留空）
        <input id="ceName" placeholder="如 MiMo 官方 / 中转站-上海">
      </label>
      <label class="console-field">Base URL
        <input id="ceBase" placeholder="https://api.example.com/v1">
      </label>
      <label class="console-field">API Key <span id="ceKeyHint"></span>
        <input id="ceKey" type="password" placeholder="sk-...">
      </label>
      <div class="console-madd">
        <button class="console-btn" onclick="ConsoleUI.fetchModels()"><i data-lucide="refresh-cw"></i> 拉取模型</button>
        <button class="console-btn" onclick="ConsoleUI.addManualRow()"><i data-lucide="plus"></i> 手动添加</button>
        <span class="console-test-out" id="ceFetchOut"></span>
      </div>
      <div class="console-mrows" id="ceMrows"></div>
      <div class="console-err" id="ceErr"></div>
      <div class="console-pcard-actions">
        <button class="console-btn" onclick="ConsoleUI.closeEditor()">取消</button>
        <button class="console-btn console-btn-primary" onclick="ConsoleUI.saveEditor()">保存</button>
      </div>
    </div>`;
  document.body.appendChild(div);
}

// ── Model rows (editor catalog) ──────────────────────────────────────

const KIND_OPTIONS = ['llm', 'embedding', 'tts', 'asr'];

function modelRowHTML(m) {
  return `<div class="console-mrow">
    <input type="checkbox"${m.checked === false ? '' : ' checked'} class="console-mrow-ck">
    <input class="console-mrow-id" placeholder="模型名称" value="${esc(m.id || '')}">
    <select class="console-mkind">${KIND_OPTIONS.map(k =>
      `<option value="${k}"${k === m.kind ? ' selected' : ''}>${k}</option>`).join('')}</select>
  </div>`;
}

function collectRows() {
  return [...document.querySelectorAll('#ceMrows .console-mrow')].map(row => ({
    id: row.querySelector('.console-mrow-id').value.trim(),
    kind: row.querySelector('.console-mkind').value,
    checked: row.querySelector('.console-mrow-ck').checked,
  }));
}

// 与后端 provider_store.guess_model_kind 同规则——手动行/粘贴行预填种类
function guessKind(id) {
  const s = String(id).toLowerCase();
  if (/(tts|speech|voice)/.test(s)) return 'tts';
  if (/(asr|whisper|transcri)/.test(s)) return 'asr';
  if (s.includes('embed')) return 'embedding';
  return 'llm';
}

// 手动行支持批量粘贴：多行/顿号分隔的清单自动拆成多行
function wireRowId(inp) {
  inp.addEventListener('paste', (e) => {
    const text = (e.clipboardData || window.clipboardData).getData('text') || '';
    if (!/[\n,、;]+/.test(text.trim())) return;  // 单个名称直接粘
    e.preventDefault();
    const parts = text.split(/[\n,、;]+/).map(s => s.trim()).filter(Boolean);
    if (!parts.length) return;
    inp.value = parts[0];
    const host = document.getElementById('ceMrows');
    for (const p of parts.slice(1)) {
      host.insertAdjacentHTML('beforeend',
        modelRowHTML({ id: p, kind: guessKind(p), checked: true }));
      wireRowId(host.lastElementChild.querySelector('.console-mrow-id'));
    }
  });
}

function addManualRow() {
  const host = document.getElementById('ceMrows');
  host.insertAdjacentHTML('beforeend', modelRowHTML({ id: '', kind: 'llm', checked: true }));
  const inp = host.lastElementChild.querySelector('.console-mrow-id');
  wireRowId(inp);
  inp.focus();
}

async function fetchModels() {
  const base = document.getElementById('ceBase').value.trim();
  const key = document.getElementById('ceKey').value;
  const out = document.getElementById('ceFetchOut');
  const host = document.getElementById('ceMrows');
  if (!base) {
    if (out) { out.textContent = '✗ 请先填写 Base URL'; out.className = 'console-test-out bad'; }
    return;
  }
  if (out) { out.textContent = '拉取中…'; out.className = 'console-test-out'; }
  const body = { base_url: base, api_key: key };
  if (editId && !key) body.provider_id = editId;
  try {
    const r = await api('POST', '/api/console/models/fetch', body);
    if (!r.ok || !Array.isArray(r.models)) {
      throw new Error(r.error || '拉取响应缺少模型列表');
    }
    // union merge: stored rows keep their checked/kind state, new ones arrive checked
    const byId = new Map(collectRows().map(m => [m.id, m]));
    r.models.forEach(m => { if (!byId.has(m.id)) byId.set(m.id, { ...m, checked: true }); });
    host.innerHTML = [...byId.values()].map(modelRowHTML).join('');
    host.querySelectorAll('.console-mrow-id').forEach(wireRowId);
    if (out) { out.textContent = `✓ ${r.models.length} 个模型`; out.className = 'console-test-out ok'; }
  } catch (e) {
    if (out) { out.textContent = `✗ ${e.message}`; out.className = 'console-test-out bad'; }
  }
}

function openEditor(pid) {
  editId = pid || null;
  ensureOverlay();
  const ov = document.getElementById('consoleEditorOverlay');
  const p = pid ? SNAP.providers.find(x => x.id === pid) : null;
  document.getElementById('ceVendorList').innerHTML =
    vendorsInUse().map(v => `<option value="${esc(v)}">`).join('');
  document.getElementById('ceTitle').textContent = p ? '编辑连接' : '新增连接';
  document.getElementById('ceVendor').value = p ? vendorOf(p) : '';
  document.getElementById('ceName').value = p ? p.name : '';
  document.getElementById('ceBase').value = p ? p.base_url : '';
  document.getElementById('ceKey').value = '';
  document.getElementById('ceKeyHint').textContent = p ? '（留空则不修改）' : '';
  document.getElementById('ceMrows').innerHTML =
    (p ? (p.models || []) : []).map(m => modelRowHTML({ ...m, checked: true })).join('');
  document.querySelectorAll('#ceMrows .console-mrow-id').forEach(wireRowId);
  document.getElementById('ceFetchOut').textContent = '';
  document.getElementById('ceErr').textContent = '';
  ov.classList.add('open');
  bindOverlayBackdrop();
}

function bindOverlayBackdrop() {
  const ov = document.getElementById('consoleEditorOverlay');
  ov.onclick = (e) => {
    if (e.target === ov) closeEditor();
  };
}

function closeEditor() {
  const ov = document.getElementById('consoleEditorOverlay');
  if (ov) ov.classList.remove('open');
  editId = null;
}

async function saveEditor() {
  const vendor = document.getElementById('ceVendor').value.trim();
  const name = document.getElementById('ceName').value.trim();
  const base = document.getElementById('ceBase').value.trim();
  const errEl = document.getElementById('ceErr');
  errEl.textContent = '';
  if (!base) {
    errEl.textContent = 'Base URL 为必填项';
    return;
  }
  const models = collectRows()
    .filter(m => m.checked && m.id)
    .map(m => ({ id: m.id, kind: m.kind }));
  const body = { vendor, name, base_url: base, api_key: document.getElementById('ceKey').value, models };
  if (editId) body.id = editId;
  try {
    await api('POST', '/api/console/providers', body);
    closeEditor();
    toast('已保存');
    await load();
  } catch (e) {
    errEl.textContent = e.message;
  }
}

// ── TTS preview ──────────────────────────────────────────────────────

let ttsAudio = null;

async function previewTts() {
  const out = document.getElementById('band-out-tts');
  if (out) {
    out.textContent = '合成中…';
    out.className = 'console-test-out';
  }
  try {
    const r = await api('POST', '/api/tts', {
      text: '你好，我是书伴。这是当前语音驱动的试听。',
      voice: '茉莉', speed: 1.0,
    });
    if (ttsAudio) ttsAudio.pause();
    ttsAudio = new Audio(r.url);
    await ttsAudio.play();
    if (out) {
      out.textContent = '✓ 播放中';
      out.className = 'console-test-out ok';
    }
  } catch (e) {
    if (out) {
      out.textContent = `✗ ${e.message}`;
      out.className = 'console-test-out bad';
    }
  }
}

window.ConsoleUI = {
  load, setFilter, focusCap, bindProvider, onModelChange, pickCard, switchConsoleView,
  toggleAc, searchModels, acPick, acClose, pickModel,
  testProvider, testSlot, previewTts,
  openEditor, closeEditor, saveEditor, fetchModels, addManualRow, remove,
};

window.switchConsoleView = switchConsoleView;

async function remove(pid) {
  if (!confirm('确定删除该连接档案？')) return;
  try {
    await api('DELETE', `/api/console/providers/${pid}`);
    toast('已删除');
    await load();
  } catch (e) {
    toast(e.message, true);
  }
}
