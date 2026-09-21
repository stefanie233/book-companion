/* Book Companion — 首次运行引导：把填 Key 从手编 .env 挪进可视化界面。
 *
 * 触发：app.js 启动时调 checkOnboarding()，/api/health 的 llm_ready=false
 * （无 key、占位 key）即弹出模态。保存走控制台既有端点（provider save →
 * test → slot 绑定），绑定热应用立即生效，无需重启。可关闭（✕ / 取消 / Esc），
 * 关闭后聊天输入区保留「连接 AI 服务」重入按钮。
 *
 * 生命周期：见文件末尾「配置引导生命周期」。引导与控制台共用 llm_ready
 * 这一个状态源——在哪边配好，另一边都会自动收摊。模型下拉支持多选（第一个
 * 勾的当主模型，绑 main_llm；其余一并存进连接）。样式复用 console.css 的
 * overlay/dialog/field/btn 族。
 */

import { S } from './state.js';

let _pid = '';          // 引导创建的连接档案 id；重试时走编辑态（空 key = 保留旧值）
let _busy = false;      // 连接流程进行中，防重复提交
let _models = [];       // 最近一次拉取到的模型名列表（下拉面板数据源）
let _selected = [];     // 面板里勾选的模型 id，**按点击先后排序**：_selected[0] 即主模型
let _ready = false;     // llm_ready 的本地镜像——配置引导生命周期的唯一状态

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function api(method, url, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(S.API + url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function toast(msg, isErr) {
  let el = document.getElementById('onboardToast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'onboardToast';
    el.className = 'console-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = isErr ? 'console-toast err' : 'console-toast';
  el.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.style.display = 'none'; }, 2600);
}

function msg(text, ok) {
  const el = document.getElementById('onboardMsg');
  if (!el) return;
  el.textContent = text;
  el.className = 'console-test-out ' + (ok ? 'ok' : 'bad');
}

function ensureDom() {
  if (document.getElementById('onboardOverlay')) return;
  const overlay = document.createElement('div');
  overlay.id = 'onboardOverlay';
  overlay.className = 'console-overlay';
  overlay.innerHTML = `
    <div class="console-dialog onboarding-dialog">
      <h3>连接 AI 服务</h3>
      <p class="onboarding-sub">只需三步：填入 API 服务商给你的接口地址、密钥和模型名。
        不确定怎么填？多数服务商的控制台会直接提供这三项。</p>
      <label class="console-field">接口地址（API Base）
        <input id="onboardBase" type="text" placeholder="https://api.openai.com/v1"
               autocomplete="off" spellcheck="false">
      </label>
      <label class="console-field">密钥（API Key）
        <span class="onboarding-keyrow">
          <input id="onboardKey" type="password" placeholder="sk-..." autocomplete="off">
          <button class="console-btn" id="onboardKeyToggle" type="button">显示</button>
        </span>
      </label>
      <label class="console-field">模型名（可多选）
        <span class="onboarding-keyrow">
          <span class="onboard-modelwrap">
            <input id="onboardModel" type="text"
                   placeholder="例如 gpt-4o / glm-4.7 / deepseek-chat" autocomplete="off">
            <div class="onboard-models" id="onboardModels"></div>
          </span>
          <button class="console-btn" id="onboardFetchBtn" type="button"
                  title="从服务商拉取可用模型列表">拉取</button>
        </span>
      </label>
      <div class="onboard-sel" id="onboardSel"></div>
      <div class="console-test-out" id="onboardMsg"></div>
      <div class="onboarding-actions">
        <button class="console-btn console-btn-primary" id="onboardConnectBtn">测试并连接</button>
        <button class="console-btn" id="onboardCancelBtn" type="button">取消</button>
        <button class="console-btn onboarding-adv" id="onboardAdvancedBtn" type="button"
                title="多连接、语音、向量等进阶配置">高级配置…</button>
      </div>
      <button class="onboarding-close" id="onboardCloseBtn" title="稍后再说">✕</button>
    </div>`;
  document.body.appendChild(overlay);
  bindDom();
  if (window.lucide && lucide.createIcons) lucide.createIcons();
}

function bindDom() {
  document.getElementById('onboardCloseBtn').onclick = hide;
  document.getElementById('onboardCancelBtn').onclick = hide;
  document.getElementById('onboardKeyToggle').onclick = function () {
    const input = document.getElementById('onboardKey');
    const showPw = input.type === 'password';
    input.type = showPw ? 'text' : 'password';
    this.textContent = showPw ? '隐藏' : '显示';
  };
  document.getElementById('onboardFetchBtn').onclick = fetchModels;
  document.getElementById('onboardConnectBtn').onclick = connect;
  document.getElementById('onboardAdvancedBtn').onclick = goAdvanced;
  // 已有拉取结果时，点回输入框可重新展开列表（datalist 的替代交互）
  document.getElementById('onboardModel').addEventListener('focus', openModelList);
  // 手工改写模型名即退出多选，回到「单个手填模型」路径，
  // 免得输入框显示 A、勾选里还挂着 B。
  document.getElementById('onboardModel').addEventListener('input', function () {
    if (!_selected.length) return;
    if (this.value.trim() === _selected[0]) return;  // 程序回写不算改
    _selected = [];
    syncModelField();
    renderModelList();
  });
  ['onboardBase', 'onboardKey', 'onboardModel'].forEach((id) => {
    document.getElementById(id).addEventListener('keydown', function (e) {
      if (e.key === 'Enter') connect();
    });
  });
  // 弹窗开着时 Esc 退出：先收模型列表，再按一次才关弹窗
  document.addEventListener('keydown', function (e) {
    const ov = document.getElementById('onboardOverlay');
    if (!ov || !ov.classList.contains('open') || e.key !== 'Escape') return;
    const box = document.getElementById('onboardModels');
    if (box && box.classList.contains('open')) { closeModelList(); return; }
    hide();
  });
  // 点面板外面收起列表（不拦 mousedown，避免抢输入框焦点）
  document.addEventListener('mousedown', function (e) {
    const box = document.getElementById('onboardModels');
    if (!box || !box.classList.contains('open')) return;
    if (!box.parentElement.contains(e.target)) closeModelList();
  });
}

/* ── 模型下拉面板 ────────────────────────────────────────────────────
 * 拉取结果以面板列出，固定约 5 行高、超出滚动——模型数 > 5 时的行为
 * 对齐控制台的模型清单（.console-mrows）。
 *
 * 多选：点行切换勾选，面板不关（方便连点几个）。勾选顺序即优先级，
 * 第一个勾的当主模型（行尾「主」徽标），绑到 main_llm；其余一并存进
 * 这条连接，之后在控制台换模型不必重新拉取。选完点面板外任意处收起。
 */

function renderModelList() {
  const box = document.getElementById('onboardModels');
  if (!box) return;
  if (!_models.length) { closeModelList(); return; }
  const keepScroll = box.scrollTop;   // 重画别把滚动位置弹回顶部（长列表要命）
  const primary = _selected[0];
  box.innerHTML = _models.map((id, i) => {
    const on = _selected.includes(id);
    return `<div class="onboard-mitem${on ? ' on' : ''}" data-i="${i}"
                 role="option" aria-selected="${on}">
        <span class="mi-ck" aria-hidden="true">${on ? '✓' : ''}</span>
        <span class="mi-name">${esc(id)}</span>
        ${id === primary ? '<span class="mi-main">主</span>' : ''}
      </div>`;
  }).join('');
  box.querySelectorAll('.onboard-mitem').forEach((el) => {
    el.onclick = () => toggleModel(el.dataset.i);
  });
  box.scrollTop = keepScroll;
  box.classList.add('open');
}

function toggleModel(i) {
  const id = _models[Number(i)];
  if (id == null) return;
  const at = _selected.indexOf(id);
  if (at >= 0) _selected.splice(at, 1);   // 取消勾选
  else _selected.push(id);                // 追加到末尾 → 首个勾的稳居主位
  renderModelList();                      // 重画以更新「主」徽标
  syncModelField();
}

/* 输入框始终显示主模型名（便于手工改写），其余勾选在下方摘要里交代。 */
function syncModelField() {
  if (_selected.length) {
    document.getElementById('onboardModel').value = _selected[0];
  }
  const hint = document.getElementById('onboardSel');
  if (!hint) return;
  if (_selected.length <= 1) {
    hint.textContent = '';
    hint.classList.remove('open');
    return;
  }
  hint.textContent = `已选 ${_selected.length} 个：主模型 ${_selected[0]}，`
    + `另存 ${_selected.slice(1).join('、')}`;
  hint.classList.add('open');
}

function openModelList() {
  const box = document.getElementById('onboardModels');
  if (box && _models.length) box.classList.add('open');
}

function closeModelList() {
  const box = document.getElementById('onboardModels');
  if (box) box.classList.remove('open');
}

function show() {
  if (_ready) return;   // 已连上就别再引导（重入按钮此刻也已被摘掉）
  ensureDom();
  document.getElementById('onboardOverlay').classList.add('open');
}

function hide() {
  const el = document.getElementById('onboardOverlay');
  if (el) el.classList.remove('open');
}

function goAdvanced() {
  hide();
  if (typeof window.switchTab === 'function') window.switchTab('console');
}

async function fetchModels() {
  const base = document.getElementById('onboardBase').value.trim().replace(/\/+$/, '');
  const key = document.getElementById('onboardKey').value.trim();
  if (!base || (!key && !_pid)) {
    msg('拉取模型需先填写接口地址与密钥', false);
    return;
  }
  const btn = document.getElementById('onboardFetchBtn');
  btn.disabled = true;
  closeModelList();
  msg('正在拉取模型列表…', true);
  try {
    const data = await api('POST', '/api/console/models/fetch',
      { base_url: base, api_key: key, provider_id: _pid });
    _models = (data.models || []).map((m) => m.id).filter(Boolean);
    // 手工填过的模型名若在列表里，直接预勾上，省一次点击
    const typed = document.getElementById('onboardModel').value.trim();
    if (typed && _models.includes(typed) && !_selected.includes(typed)) {
      _selected = [typed];
    }
    renderModelList();
    syncModelField();
    msg(_models.length
      ? `已拉取 ${_models.length} 个模型，可多选；选完点面板外任意处收起`
      : '服务商未返回可用模型，请手动填写模型名', !!_models.length);
  } catch (e) {
    msg('拉取失败：' + e.message, false);
  } finally {
    btn.disabled = false;
  }
}

async function connect() {
  if (_busy) return;
  const base = document.getElementById('onboardBase').value.trim().replace(/\/+$/, '');
  const key = document.getElementById('onboardKey').value.trim();
  const typed = document.getElementById('onboardModel').value.trim();
  // 勾选优先；没勾过就走输入框里的单个手填模型
  const ids = _selected.length ? _selected.slice() : (typed ? [typed] : []);
  const model = ids[0] || '';
  if (!base) { msg('请填写接口地址（API Base）', false); return; }
  if (!model) { msg('请填写模型名', false); return; }
  _busy = true;
  const btn = document.getElementById('onboardConnectBtn');
  btn.disabled = true;
  msg('正在测试连接…', true);
  try {
    // 多选时全部模型一并存进这条连接（之后在控制台换模型不必重新拉取），
    // 但只有主模型（第一个勾的）绑到 main_llm。
    const body = { name: '我的连接', base_url: base,
      models: ids.map((id) => ({ id, kind: 'llm' })) };
    if (_pid) body.id = _pid;
    if (key) body.api_key = key;
    const saved = await api('POST', '/api/console/providers', body);
    _pid = saved.provider.id;
    msg('档案已保存，正在测试连通性…', true);
    await api('POST', `/api/console/providers/${_pid}/test`, { model, kind: 'llm' });
    await api('PUT', '/api/console/slots/main_llm', { provider_id: _pid, model });
    const h = await api('GET', '/api/health');
    if (h.llm_ready) {
      markConfigured();
      toast('AI 服务已连接', false);
    } else {
      msg('已绑定但服务未就绪，请检查填写内容', false);
    }
  } catch (e) {
    msg('连接失败：' + e.message, false);
  } finally {
    _busy = false;
    btn.disabled = false;
  }
}

/* 输入区重入按钮：关闭弹窗后仍可从这里再次打开引导。
 * 仅图标——带 6 字标签会把 mode-bar 撑爆，把最右侧的「会话」顶出屏幕。 */
function injectEntryBtn() {
  if (document.getElementById('onboardEntryBtn')) return;
  const modeBar = document.querySelector('.chat-mode-bar');
  if (!modeBar) return;
  const btn = document.createElement('button');
  btn.id = 'onboardEntryBtn';
  btn.className = 'mode-toggle onboard-entry';
  btn.title = '尚未连接 AI 服务，点击配置';
  btn.setAttribute('aria-label', '连接 AI 服务');
  btn.innerHTML = '<span class="mode-icon"><i data-lucide="plug-zap"></i></span>';
  btn.onclick = show;
  modeBar.insertBefore(btn, modeBar.firstChild);
  if (window.lucide && lucide.createIcons) lucide.createIcons();
}

/* ── 配置引导生命周期 ────────────────────────────────────────────────
 * 唯一状态是 _ready（/api/health 的 llm_ready），三个驱动点：
 *   1. 启动 checkOnboarding()        → 未就绪则弹窗 + 挂重入按钮
 *   2. 引导内 connect() 成功          → markConfigured()
 *   3. 控制台任何改动后（console.js load()）→ refreshOnboardingState()
 * 这样「在控制台里配好了」也会摘掉交互页那颗药丸，不会两处并存；
 * 反过来在控制台解绑了主 LLM，药丸会重新长出来。
 */

function markConfigured() {
  _ready = true;
  closeModelList();
  hide();
  const entry = document.getElementById('onboardEntryBtn');
  if (entry) entry.remove();
}

/* 控制台改完配置后回查。health 不可达时不动现状（纯浏览场景不打扰）。 */
export async function refreshOnboardingState() {
  let ready;
  try {
    const h = await api('GET', '/api/health');
    ready = !!(h && h.llm_ready);
  } catch (_) {
    return;
  }
  if (ready) markConfigured();
  else injectEntryBtn();
}

/* app.js 启动时调用：服务未配置（llm_ready=false）才弹引导。 */
export async function checkOnboarding() {
  try {
    const h = await api('GET', '/api/health');
    if (h && h.llm_ready) { _ready = true; return; }
  } catch (_) {
    return;  // health 不可达不弹窗，避免干扰纯浏览场景
  }
  ensureDom();
  injectEntryBtn();
  show();
}
