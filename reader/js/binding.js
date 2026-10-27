// 模型绑定：每个 LLM 调用点 → (连接, 模型)。
// 未绑定的调用点逐级继承（bookgraph.scout → bookgraph → main_llm），
// 所以默认行为就是「跟随主模型」。服务端按次解析，保存即生效。

let SNAP = null;
let onlyBound = false;

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

function vendorOf(p) {
  return (p.vendor || '').trim() || (p.name || '').trim().split(/\s+/)[0] || '?';
}

// 可以承载 LLM 的连接：目录里有 llm 模型，或正被某个 route 绑定。
function llmProviders() {
  const used = new Set(Object.values(SNAP.routes || {}).map(r => r.provider_id));
  return SNAP.providers.filter(p =>
    (p.models || []).some(m => m.kind === 'llm') || used.has(p.id));
}

function llmModels(pid) {
  const p = SNAP.providers.find(x => x.id === pid);
  return p ? (p.models || []).filter(m => m.kind === 'llm') : [];
}

function routeEntry(key) {
  return (SNAP.routes || {})[key] || null;
}

function groupLabel(id) {
  const g = (SNAP.groups || []).find(x => x.id === id);
  return g ? g.label : id;
}

// 一行的生效状态：自己绑定 / 继承某个上级 / 跟随主模型。
function stateText(cp) {
  const r = cp.resolved || {};
  const target = `${esc(r.provider_name || '')}/${esc(r.model || '?')}`;
  if (cp.bound) return `<span class="binding-tag on">已绑定</span> ${target}`;
  if (cp.origin) {
    return `<span class="binding-tag inherit">继承「${esc(groupLabel(cp.origin))}」</span> ${target}`;
  }
  return `<span class="binding-tag main">跟随主模型</span> ${target}`;
}

// 控件：连接下拉 + 模型输入（带该连接的 llm 模型联想）+ 绑定/解绑。
function controls(key, entry) {
  const pid = entry ? entry.provider_id : '';
  const model = entry ? entry.model : '';
  // 顶层分组没有上级，只能回落到主模型。
  const inherit = key.includes('.') ? '跟随上级' : '跟随主模型';
  const opts = llmProviders().map(p => {
    const sel = p.id === pid ? ' selected' : '';
    return `<option value="${esc(p.id)}"${sel}>${esc(vendorOf(p))} · ${esc(p.name)}</option>`;
  }).join('');
  return `
    <div class="binding-ctrl">
      <select class="binding-provider" data-role="provider">
        <option value="">${inherit}</option>${opts}
      </select>
      <input class="binding-model" data-role="model"
             list="bindingModels-${esc(key)}" value="${esc(model)}"
             placeholder="模型名" ${pid ? '' : 'disabled'}>
      <datalist id="bindingModels-${esc(key)}">${pid
        ? llmModels(pid).map(m => `<option value="${esc(m.id)}"></option>`).join('')
        : ''}</datalist>
      <button class="console-btn" data-role="clear"
              ${pid ? '' : 'disabled'}>解绑</button>
    </div>`;
}

function groupState(g, entry) {
  if (entry) {
    const p = SNAP.providers.find(x => x.id === entry.provider_id) || {};
    return `<span class="binding-tag on">组绑定</span> ${esc(vendorOf(p))}/${esc(entry.model)}`;
  }
  const main = ((SNAP.slots || {}).main_llm || {});
  const where = main.source === 'console' ? '主模型' : '主模型（.env）';
  return `<span class="binding-tag main">跟随${where}</span>`;
}

function leafRow(cp) {
  const entry = cp.bound ? { provider_id: cp.provider_id, model: cp.model } : null;
  const ctrl = cp.main
    ? '<span class="binding-readonly">主模型槽位，在「驱动控制台」切换</span>'
    : controls(cp.id, entry);
  return `
    <div class="binding-row" data-key="${esc(cp.id)}">
      <div class="binding-row-main">
        <span class="binding-row-label" title="${esc(cp.desc || '')}">${esc(cp.label)}</span>
        <span class="binding-row-state">${stateText(cp)}</span>
      </div>
      ${ctrl}
    </div>`;
}

function render() {
  const host = document.getElementById('bindingList');
  if (!host || !SNAP) return;
  const groups = SNAP.groups || [];
  const cps = SNAP.callpoints || [];
  const boundCount = cps.filter(c => c.bound).length;
  host.innerHTML = groups.map(g => {
    const all = cps.filter(c => c.group === g.id);
    const entry = routeEntry(g.id);
    // 只看已绑定：组本身绑了就把整组摊开（它们在继承），否则只留单独绑定的行。
    const leaves = onlyBound && !entry ? all.filter(c => c.bound) : all;
    if (onlyBound && !entry && leaves.length === 0) return '';
    return `
      <section class="binding-group">
        <div class="binding-group-head" data-key="${esc(g.id)}">
          <div class="binding-row-main">
            <span class="binding-group-label">${esc(g.label)}</span>
            <span class="binding-row-state">${groupState(g, entry)}</span>
          </div>
          ${g.routable === false
            ? '<span class="binding-readonly">主模型槽位，在「驱动控制台」切换</span>'
            : controls(g.id, entry)}
        </div>
        <div class="binding-rows">${leaves.map(leafRow).join('')}</div>
      </section>`;
  }).join('');
  const sum = document.getElementById('bindingSummary');
  if (sum) {
    sum.textContent = `${cps.length} 个调用点 · ${boundCount} 个已单独绑定`;
  }
}

async function load() {
  try {
    SNAP = await api('GET', '/api/console/config');
  } catch (e) {
    toast(e.message, true);
    return;
  }
  const r = ((SNAP.slots || {}).main_llm || {}).resolved || {};
  const el = document.getElementById('bindingMainModel');
  if (el) el.textContent = `${r.provider_name || ''}/${r.model || '?'}`;
  render();
}

function toggleOnlyBound(on) {
  onlyBound = !!on;
  render();
}

function rowOf(el) {
  return el.closest('[data-key]');
}

function putRoute(key, body) {
  return api('PUT', `/api/console/routes/${encodeURIComponent(key)}`, body);
}

// 选择即保存：与驱动控制台的槽位带同一交互，选完连接/模型就落盘。
async function saveRow(row) {
  const key = row.dataset.key;
  const pid = row.querySelector('[data-role=provider]').value;
  const inp = row.querySelector('[data-role=model]');
  const model = inp.value.trim();
  if (!pid) return;
  if (!model) {
    toast('模型名不能为空', true);
    await load();
    return;
  }
  try {
    await putRoute(key, { provider_id: pid, model });
    toast('已绑定，下一次调用生效');
    await load();
  } catch (err) {
    toast(err.message, true);
  }
}

async function onProviderChange(el) {
  const row = rowOf(el);
  if (!row) return;
  const pid = el.value;
  const inp = row.querySelector('[data-role=model]');
  const clear = row.querySelector('[data-role=clear]');
  // 选「跟随上级」= 解绑，回退到上一级
  if (!pid) {
    if (clear && !clear.disabled) {
      await onClear(el);
    } else {
      inp.value = '';
      inp.disabled = true;
    }
    return;
  }
  const models = llmModels(pid);
  const dl = document.getElementById(inp.getAttribute('list'));
  if (dl) {
    dl.innerHTML = models.map(m => `<option value="${esc(m.id)}"></option>`).join('');
  }
  // 同一条模型在新连接里存在就保留，否则用该连接的第一个 llm 模型
  let model = inp.value.trim();
  if (!models.some(m => m.id === model)) {
    model = models[0] ? models[0].id : '';
  }
  inp.value = model;
  inp.disabled = false;
  if (clear) clear.disabled = false;
  if (!model) {
    toast('该连接还没有 llm 模型，可直接输入模型名', true);
    return;
  }
  await saveRow(row);
}

async function onClear(el) {
  const row = rowOf(el);
  const key = row.dataset.key;
  try {
    await putRoute(key, { provider_id: '' });
    toast('已解绑，回退到上级 / 主模型');
    await load();
  } catch (err) {
    toast(err.message, true);
  }
}

document.addEventListener('change', e => {
  if (e.target.matches('.binding-provider')) {
    onProviderChange(e.target);
  } else if (e.target.matches('.binding-model')) {
    const row = rowOf(e.target);
    if (row) saveRow(row);
  }
});

document.addEventListener('click', e => {
  const btn = e.target.closest('[data-role="clear"]');
  if (btn) onClear(btn);
});

window.BindingUI = { load, toggleOnlyBound };


