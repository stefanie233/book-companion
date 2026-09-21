// Usage analytics page module — console 用量分析 (page-console second view).
// Three blocks: stat cards (calls / tokens / errors), stacked daily chart
// with hover tooltip card, per-model dual charts (calls curve + tokens bars)
// — plus the model profiles editor.
// Talks to /api/console/usage/* endpoints; reuses console.js's pattern with
// its own tiny api/toast helpers.

let PROFILES = null;
let OVERVIEW = { month: null, totals: null, days: [], by_model: [] };
const SLOTS = ['--chart-c1', '--chart-c2', '--chart-c3',
               '--chart-c4', '--chart-c5', '--chart-c6'];
const MAX_SLOTS = 6;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtNum(n) {
  if (n == null) return '0';
  if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
  if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
  return String(n);
}

async function api(method, url, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

let _toastT = null;
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
  clearTimeout(_toastT);
  _toastT = setTimeout(() => { el.style.display = 'none'; }, 2600);
}

// ── Load / render orchestration ──────────────────────────────────────

async function load() {
  try {
    const month = OVERVIEW.month || _currentMonth();
    const [profiles, overview] = await Promise.all([
      api('GET', '/api/console/usage/profiles'),
      api('GET', `/api/console/usage/overview?month=${month}`),
    ]);
    PROFILES = profiles.profiles;
    OVERVIEW = overview;
    renderStats();
    renderChart();
    renderModelCards();
    renderProfilesTable();
  } catch (e) {
    toast(`用量数据加载失败：${e.message}`, true);
  }
}

function _currentMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

// ── Stat cards(请求次数 / tokens 总量 / 错误次数)───────────────────

function renderStats() {
  const host = document.getElementById('usageStats');
  if (!host) return;
  const t = OVERVIEW.totals || { calls: 0, tokens_in: 0, tokens_out: 0, errors: 0 };
  const stat = (label, value, sub) => `
    <div class="usage-stat">
      <span class="usage-stat-label">${esc(label)}</span>
      <div class="usage-stat-value">${fmtNum(value)}</div>
      <div class="usage-stat-sub">${sub}</div>
    </div>`;
  host.innerHTML =
    stat('API 请求次数', t.calls, `${esc(OVERVIEW.month)} 当月`) +
    stat('Tokens 总量', (t.tokens_in || 0) + (t.tokens_out || 0),
      `输入 ${fmtNum(t.tokens_in)} · 输出 ${fmtNum(t.tokens_out)}`) +
    stat('错误次数', t.errors, t.calls ? `占 ${(t.errors / t.calls * 100).toFixed(1)}%` : '—');
}

// ── 模型→颜色:按名稳定排序占槽,颜色跟随实体,翻月不变色 ─────────────

function assignColors() {
  const names = [...new Set(OVERVIEW.by_model.map(m => m.model))]
    .filter(n => n !== '').sort((a, b) => a.localeCompare(b));
  const map = {};
  names.forEach((n, i) => {
    map[n] = i < MAX_SLOTS ? `var(${SLOTS[i]})` : 'var(--chart-other)';
  });
  if (names.length > MAX_SLOTS) map.__other__ = 'var(--chart-other)';
  if (OVERVIEW.by_model.some(m => m.model === '')) map[''] = 'var(--chart-other)';
  return map;
}

const modelLabel = n => (n === '' ? '未记录' : n);

// ── 每日用量堆叠柱状图 ───────────────────────────────────────────────

function renderChart() {
  const host = document.getElementById('usageChart');
  if (!host) return;
  const [y, m] = OVERVIEW.month.split('-').map(Number);
  const byDay = {};
  OVERVIEW.days.forEach(d => { byDay[d.day] = d.models; });
  const maxDay = Math.max(0, ...OVERVIEW.days.map(d =>
    d.models.reduce((a, x) => a + x.tokens_in + x.tokens_out, 0)));
  const colors = assignColors();
  const legendOrder = Object.keys(colors); // 插入序:已排序模型 → 其他 → 未记录
  document.getElementById('usageChartNav').innerHTML = `
    <button class="console-btn" onclick="UsageUI.shiftMonth(-1)">‹</button>
    <span class="usage-chart-title">${y} 年 ${m} 月</span>
    <button class="console-btn" onclick="UsageUI.shiftMonth(1)">›</button>
    <span class="usage-legend">${legendOrder.map(n => `
      <span><i class="usage-legend-dot" style="background:${colors[n]}"></i>
        ${esc(n === '__other__' ? '其他' : modelLabel(n))}</span>`).join('')}</span>`;

  const daysInMonth = new Date(y, m, 0).getDate();
  const today = new Date(); today.setHours(0, 0, 0, 0);
  let cols = '', ticks = '';
  if (!maxDay) {
    host.innerHTML = '<div class="console-hint">本月暂无调用记录</div>';
    document.getElementById('usageChartDays').innerHTML = '';
    return;
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const key = `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    const recs = byDay[key] || [];
    if (new Date(y, m - 1, d) > today) {   // 未来日期只占位
      cols += '<div class="usage-chart-col empty"></div>';
      ticks += `<span>${d}</span>`;
      continue;
    }
    let segs = '';
    for (const n of legendOrder) {
      const r = recs.find(x => x.model === n);
      if (!r) continue;
      const h = (r.tokens_in + r.tokens_out) / maxDay * 100;
      if (h > 0) segs += `<div class="usage-chart-seg" style="height:${h.toFixed(2)}%;--h:${h.toFixed(2)}%;background:${colors[n]};animation-delay:${(d - 1) * 8}ms"></div>`;
    }
    cols += `<div class="usage-chart-col${segs ? '' : ' empty'}">${segs}</div>`;
    ticks += `<span>${d}</span>`;
  }
  host.innerHTML = cols;
  document.getElementById('usageChartDays').innerHTML = ticks;
  bindChartTip(host, byDay, y, m, legendOrder, colors);
}

// ── 图表工具:单调三次插值(防 0 平段/尖峰过冲)+ 轴刻度格式 ─────────

function fmtAxis(n) {
  if (n >= 1e8) return (n / 1e8).toFixed(1).replace(/\.0$/, '') + '亿';
  if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, '') + '万';
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}

// y 轴顶刻度「贪心」算法:让数据峰值落在顶轴的 65%~80%,且刻度永远是圆数。
// 候选顶轴只取 1/2/5 ×10^k 步长格上的取整值,从最粗步长往细试探,
// 第一个落进 [max/0.8, max/0.65] 窗口者胜出 —— 粗步长优先保证刻度圆
// (峰值 76 → 顶轴 100,而非贴着数据的 80;DeepSeek 同款行为)。
// 微量数据(≤5)走特查表,保证中线刻度(top/2)也是整数。
function niceAxisTop(max) {
  if (!(max > 0)) return 4;                        // 无数据:给个安静的小刻度
  const SMALL = { 1: 2, 2: 4, 3: 4, 4: 6, 5: 8 };
  if (max <= 5) return SMALL[max];
  const lo = max / 0.8, hi = max / 0.65;           // 顶轴的合法区间
  for (let e = Math.ceil(Math.log10(hi)); e >= 0; e--)
    for (const m of [5, 2, 1]) {
      const s = m * Math.pow(10, e);
      const top = Math.ceil(lo / s) * s;
      if (top <= hi) return top;
    }
  return Math.ceil(hi);                            // 理论不可达,兜底
}

function smoothPath(pts) {          // Fritsch-Carlson monotone cubic → SVG path
  const n = pts.length;
  if (!n) return '';
  if (n === 1) return `M${pts[0].x.toFixed(2)},${pts[0].y.toFixed(2)}`;
  const dx = [], m = [], t = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1].x - pts[i].x;
    m[i] = (pts[i + 1].y - pts[i].y) / dx[i];
  }
  t[0] = m[0]; t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / m[i], b = t[i + 1] / m[i];
    const s = a * a + b * b;
    if (s > 9) { const tau = 3 / Math.sqrt(s); t[i] = tau * a * m[i]; t[i + 1] = tau * b * m[i]; }
  }
  let d = `M${pts[0].x.toFixed(2)},${pts[0].y.toFixed(2)}`;
  for (let i = 0; i < n - 1; i++) {
    const c1x = pts[i].x + dx[i] / 3, c1y = pts[i].y + t[i] * dx[i] / 3;
    const c2x = pts[i + 1].x - dx[i] / 3, c2y = pts[i + 1].y - t[i + 1] * dx[i] / 3;
    d += `C${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${pts[i + 1].x.toFixed(2)},${pts[i + 1].y.toFixed(2)}`;
  }
  return d;
}

// ── 总图 hover 浮动卡片:日期 + 当日总量 + 按模型色点细分行 ──────────

let _tipEl = null, _tipKey = '';

function tipHTML(m, d, recs, legendOrder, colors) {
  const total = recs.reduce((a, r) => a + r.tokens_in + r.tokens_out, 0);
  const calls = recs.reduce((a, r) => a + r.calls, 0);
  const rows = legendOrder.map(n => {
    const r = recs.find(x => x.model === n);
    if (!r) return '';
    return `<div class="usage-tip-row"><i style="background:${colors[n]}"></i>` +
      `<span>${esc(modelLabel(n))}</span><b>${fmtNum(r.tokens_in + r.tokens_out)}</b></div>`;
  }).join('');
  return `<div class="usage-tip-date">${m}/${d}</div>` +
    `<div class="usage-tip-total">${fmtNum(total)} tokens · ${calls} 次</div>${rows}`;
}

function bindChartTip(host, byDay, y, m, legendOrder, colors) {
  const card = host.closest('.usage-card');
  if (!card) return;
  if (!_tipEl) {
    _tipEl = document.createElement('div');
    _tipEl.className = 'usage-tip';
  }
  if (!_tipEl.isConnected) card.appendChild(_tipEl);
  const hide = () => { _tipEl.classList.remove('show'); _tipKey = ''; };
  host.onmouseleave = hide;
  host.onmousemove = e => {
    const col = e.target.closest('.usage-chart-col');
    if (!col || !host.contains(col) || col.classList.contains('empty')) { hide(); return; }
    const d = [...host.children].indexOf(col) + 1;   // 列序 = 日
    const recs = byDay[`${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`] || [];
    if (!recs.length) { hide(); return; }
    if (_tipKey !== String(d)) {
      _tipKey = String(d);
      _tipEl.innerHTML = tipHTML(m, d, recs, legendOrder, colors);
    }
    _tipEl.classList.add('show');
    const cr = card.getBoundingClientRect(), or = col.getBoundingClientRect();
    const w = _tipEl.offsetWidth, h = _tipEl.offsetHeight;
    let x = or.left + or.width / 2 - cr.left - w / 2;
    x = Math.max(8, Math.min(x, cr.width - w - 8));
    const above = or.top - cr.top - h - 10;
    const yPos = above >= 6 ? above : or.bottom - cr.top + 10;
    _tipEl.style.transform = `translate(${Math.round(x)}px, ${Math.round(yPos)}px)`;
  };
}

async function shiftMonth(delta) {
  const [y, m] = OVERVIEW.month.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  OVERVIEW.month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  try {
    OVERVIEW = await api('GET', `/api/console/usage/overview?month=${OVERVIEW.month}`);
    renderStats(); renderChart(); renderModelCards();
  } catch (e) {
    toast(e.message, true);
  }
}

// ── 模型细则:每模型一组「调用次数曲线 + tokens 柱状」双图 ──────────
// hover 响应与总图 tooltip 同款:竖虚线 + 曲线数据点 + 浮动卡片。
// tokens 柱为 DeepSeek 式分段量能柱:输入(浅)在下、输出(深)在上。

let _modelCharts = [];   // 每次渲染重建,供 hover 绑定取逐日数据

function renderModelCards() {
  const host = document.getElementById('usageModels');
  if (!host) return;
  if (!OVERVIEW.by_model.length) { host.innerHTML = ''; _modelCharts = []; return; }
  const colors = assignColors();
  const [y, m] = OVERVIEW.month.split('-').map(Number);
  const dim = new Date(y, m, 0).getDate();
  const prefix = `${y}-${String(m).padStart(2, '0')}-`;
  const tickDays = [1, 8, 15, 22, 29].filter(d => d <= dim);
  const ticks = Array.from({ length: dim }, (_, i) =>
    `<span>${tickDays.includes(i + 1) ? i + 1 : ''}</span>`).join('');
  _modelCharts = OVERVIEW.by_model.map(mo => {
    const color = colors[mo.model] || 'var(--chart-other)';
    const dT = [], dC = [], dIn = [], dOut = [];
    for (let d = 1; d <= dim; d++) {   // 整月逐日,无数据天补 0(旧 mini 只画有数据天)
      const recs = (OVERVIEW.days.find(x => x.day === prefix + String(d).padStart(2, '0')) || {}).models;
      const r = recs ? recs.find(x => x.model === mo.model) : null;
      dC.push(r ? r.calls : 0);
      dIn.push(r ? r.tokens_in : 0);
      dOut.push(r ? r.tokens_out : 0);
      dT.push(r ? r.tokens_in + r.tokens_out : 0);
    }
    return { mo, color, dC, dT, dIn, dOut,
             pkC: niceAxisTop(Math.max(0, ...dC)),
             pkT: niceAxisTop(Math.max(0, ...dT)) };
  });
  host.innerHTML = _modelCharts.map(mc => `
    <div class="usage-msection">
      <div class="usage-msection-head">
        <i class="usage-legend-dot" style="background:${mc.color}"></i>${esc(modelLabel(mc.mo.model))}
        ${mc.mo.errors ? `<span class="usage-merr">错误 ${mc.mo.errors} 次</span>` : ''}
      </div>
      <div class="usage-mcharts">
        ${_callsChart(mc, ticks)}
        ${_tokensChart(mc, ticks)}
      </div>
      <div class="usage-tip"></div>
    </div>`).join('');
  bindModelTips(host, dim, m);
}

// 左图:该模型每日调用次数 — SVG 平滑面积曲线(monotone 防过冲)
function _callsChart(mc, ticks) {
  const { dC, color, pkC } = mc;
  const W = 310, H = 100, st = W / dC.length;
  const pts = dC.map((v, i) => ({ x: i * st + st / 2, y: H - v / pkC * H }));
  const line = smoothPath(pts);
  const area = line + `L${pts[pts.length - 1].x.toFixed(2)},${H}L${pts[0].x.toFixed(2)},${H}Z`;
  return `
    <div class="usage-mchart" data-kind="calls">
      <div class="usage-mchart-title">API 请求次数<b>${fmtNum(dC.reduce((a, b) => a + b, 0))}</b></div>
      <div class="u-line">
        <div class="u-line-yaxis"><span>${fmtAxis(pkC)}</span><span>${fmtAxis(pkC / 2)}</span><span>0</span></div>
        <div class="u-line-plot">
          <i class="u-line-gline" style="top:0"></i>
          <i class="u-line-gline" style="top:50%"></i>
          <i class="u-line-gline" style="top:100%"></i>
          <svg class="usage-line-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
            <path class="usage-line-area" d="${area}" fill="${color}"/>
            <path d="${line}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>
          </svg>
          <i class="u-hover-line"></i><i class="u-hover-dot" style="background:${color}"></i>
        </div>
      </div>
      <div class="usage-mchart-days">${ticks}</div>
    </div>`;
}

// 右图:该模型每日 tokens — 31 等分分段柱:输入(浅)在下、输出(深)在上,零值列占位不画
function _tokensChart(mc, ticks) {
  const { dT, dIn, dOut, color, pkT } = mc;
  const bars = dT.map((v, i) => {
    if (v <= 0) return '<span class="usage-bar-col"></span>';
    const inH = (dIn[i] / pkT * 100).toFixed(2);
    const outH = (dOut[i] / pkT * 100).toFixed(2);
    const delay = `animation-delay:${i * 8}ms;`;
    const inEl = dIn[i] > 0
      ? `<i style="height:${inH}%;--h:${inH}%;background:${color};opacity:.4;` +
        `${dOut[i] > 0 ? '' : 'border-radius:2px 2px 0 0;'}${delay}"></i>` : '';
    const outEl = dOut[i] > 0
      ? `<i style="height:${outH}%;--h:${outH}%;background:${color};` +
        `border-radius:2px 2px 0 0;${delay}"></i>` : '';
    return `<span class="usage-bar-col">${inEl}${outEl}</span>`;
  }).join('');
  return `
    <div class="usage-mchart" data-kind="tokens">
      <div class="usage-mchart-title">Tokens<b>${fmtNum(mc.mo.tokens_in + mc.mo.tokens_out)}</b>
        <span class="sub">输入 ${fmtNum(mc.mo.tokens_in)} · 输出 ${fmtNum(mc.mo.tokens_out)}</span></div>
      <div class="u-line">
        <div class="u-line-yaxis"><span>${fmtAxis(pkT)}</span><span>${fmtAxis(pkT / 2)}</span><span>0</span></div>
        <div class="u-line-plot">
          <i class="u-line-gline" style="top:0"></i>
          <i class="u-line-gline" style="top:50%"></i>
          <i class="u-line-gline" style="top:100%"></i>
          <div class="usage-bars">${bars}</div>
          <i class="u-hover-line"></i>
        </div>
      </div>
      <div class="usage-mchart-days">${ticks}</div>
    </div>`;
}

// 模型细则 hover 绑定:x 坐标 → 日序号;左图给虚线+数据点,右图给虚线+柱列高亮
function bindModelTips(host, dim, m) {
  host.querySelectorAll('.usage-msection').forEach((section, si) => {
    const mc = _modelCharts[si];
    if (!mc) return;
    const tip = section.querySelector('.usage-tip');
    const clearFx = () => {
      section.querySelectorAll('.u-hover-line.show, .u-hover-dot.show')
        .forEach(el => el.classList.remove('show'));
      section.querySelectorAll('.usage-bar-col.hl')
        .forEach(el => el.classList.remove('hl'));
    };
    const hide = () => { tip.classList.remove('show'); clearFx(); };
    section.querySelectorAll('.usage-mchart').forEach(chart => {
      const kind = chart.dataset.kind;
      const plot = chart.querySelector('.u-line-plot');
      if (!plot) return;
      const hline = chart.querySelector('.u-hover-line');
      const dot = chart.querySelector('.u-hover-dot');
      const data = kind === 'calls' ? mc.dC : mc.dT;
      const pk = kind === 'calls' ? mc.pkC : mc.pkT;
      chart.onmouseleave = hide;
      chart.onmousemove = e => {
        const r = plot.getBoundingClientRect();
        const i = Math.max(0, Math.min(dim - 1,
          Math.floor((e.clientX - r.left) / r.width * dim)));
        const v = data[i];
        if (v <= 0) { hide(); return; }          // 无数据日:不给响应,同总图
        clearFx();
        const date = `<div class="usage-tip-date">${m}/${i + 1}</div>`;
        if (kind === 'calls') {
          tip.innerHTML = date +
            `<div class="usage-tip-row"><span>请求次数</span><b>${fmtNum(v)}</b></div>`;
          dot.style.left = ((i + 0.5) / dim * 100) + '%';
          dot.style.top = ((1 - v / pk) * 100) + '%';
          dot.classList.add('show');
        } else {
          const tin = mc.dIn[i], tout = mc.dOut[i];
          tip.innerHTML = date +
            `<div class="usage-tip-total">${fmtNum(tin + tout)}</div>` +
            `<div class="usage-tip-row"><i style="background:${mc.color};opacity:.4"></i>` +
              `<span>输入</span><b>${fmtNum(tin)}</b></div>` +
            `<div class="usage-tip-row"><i style="background:${mc.color}"></i>` +
              `<span>输出</span><b>${fmtNum(tout)}</b></div>`;
          chart.querySelectorAll('.usage-bar-col')[i]?.classList.add('hl');
        }
        hline.style.left = ((i + 0.5) / dim * 100) + '%';
        hline.classList.add('show');
        tip.classList.add('show');
        // 定位:同 bindChartTip 的水平夹取 + 上下翻转逻辑
        const sr = section.getBoundingClientRect();
        const w = tip.offsetWidth, h = tip.offsetHeight;
        let x = r.left + r.width * ((i + 0.5) / dim) - sr.left - w / 2;
        x = Math.max(8, Math.min(x, sr.width - w - 8));
        const above = r.top - sr.top - h - 10;
        const yPos = above >= 6 ? above : r.bottom - sr.top + 10;
        tip.style.transform = `translate(${Math.round(x)}px, ${Math.round(yPos)}px)`;
      };
    });
  });
}

// ── Model profiles editor ────────────────────────────────────────────

function renderProfilesTable() {
  const body = document.getElementById('usageProfilesBody');
  if (!body || !PROFILES) return;
  const rows = Object.entries(PROFILES.models).map(([name, p]) => `
    <tr>
      <td><input class="up-model" value="${esc(name)}" ${name === '*' ? 'readonly' : ''}></td>
      <td><input class="up-window" type="number" min="1" value="${p.context_window || 1048576}"></td>
      <td><input class="up-tok" placeholder="分词器覆盖（可选）" value="${esc(p.tokenizer_path || '')}"></td>
      <td>${name === '*' ? '<span class="est">兜底</span>' : `<button class="console-btn" onclick="UsageUI.delProfileRow(this)">删</button>`}</td>
    </tr>`).join('');
  body.innerHTML = rows;
  renderVendorTable();
  const tokPath = document.getElementById('usageTokPath');
  const tokEn = document.getElementById('usageTokEnabled');
  if (tokPath) tokPath.value = PROFILES.tokenizer?.path || '';
  if (tokEn) tokEn.checked = !!PROFILES.tokenizer?.enabled;
}

function renderVendorTable() {
  const body = document.getElementById('usageVendorBody');
  if (!body || !PROFILES) return;
  body.innerHTML = Object.entries(PROFILES.vendors || {}).map(([alias, path]) => `
    <tr>
      <td><input class="up-vendor" value="${esc(alias)}" placeholder="glm,zhipu"></td>
      <td><input class="up-vpath" value="${esc(path || '')}" placeholder="tokenizer.json 路径"></td>
      <td><button class="console-btn" onclick="UsageUI.delProfileRow(this)">删</button></td>
    </tr>`).join('');
}

function addVendorRow() {
  const body = document.getElementById('usageVendorBody');
  if (!body) return;
  body.insertAdjacentHTML('beforeend', `
    <tr>
      <td><input class="up-vendor" placeholder="glm,zhipu"></td>
      <td><input class="up-vpath" placeholder="tokenizer.json 路径"></td>
      <td><button class="console-btn" onclick="UsageUI.delProfileRow(this)">删</button></td>
    </tr>`);
}

function addProfileRow() {
  const body = document.getElementById('usageProfilesBody');
  if (!body) return;
  body.insertAdjacentHTML('beforeend', `
    <tr>
      <td><input class="up-model" placeholder="模型名"></td>
      <td><input class="up-window" type="number" min="1" value="1048576"></td>
      <td><input class="up-tok" placeholder="分词器覆盖（可选）"></td>
      <td><button class="console-btn" onclick="UsageUI.delProfileRow(this)">删</button></td>
    </tr>`);
}

function delProfileRow(btn) {
  btn.closest('tr')?.remove();
}

function collectProfiles() {
  const models = {};
  document.querySelectorAll('#usageProfilesBody tr').forEach(tr => {
    const name = tr.querySelector('.up-model')?.value.trim();
    if (!name) return;
    const entry = {
      context_window: parseInt(tr.querySelector('.up-window')?.value, 10) || 1048576,
    };
    const override = tr.querySelector('.up-tok')?.value.trim();
    if (override) entry.tokenizer_path = override;
    models[name] = entry;
  });
  const vendors = {};
  document.querySelectorAll('#usageVendorBody tr').forEach(tr => {
    const alias = tr.querySelector('.up-vendor')?.value.trim();
    if (!alias) return;
    vendors[alias] = tr.querySelector('.up-vpath')?.value.trim() || '';
  });
  return {
    models,
    vendors,
    tokenizer: {
      path: document.getElementById('usageTokPath')?.value.trim() || '',
      enabled: document.getElementById('usageTokEnabled')?.checked ?? true,
    },
  };
}

async function saveProfiles() {
  try {
    await api('PUT', '/api/console/usage/profiles', collectProfiles());
    toast('模型档案已保存');
    const r = await api('GET', '/api/console/usage/profiles');
    PROFILES = r.profiles;
    renderProfilesTable();
  } catch (e) {
    toast(e.message, true);
  }
}

// ── Export ───────────────────────────────────────────────────────────

window.UsageUI = {
  load, shiftMonth, saveProfiles, addProfileRow, addVendorRow, delProfileRow,
};

