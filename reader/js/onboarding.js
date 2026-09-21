/* Book Companion — 首次运行引导：把填 Key 从手编 .env 挪进可视化界面。
 *
 * 触发：app.js 启动时调 checkOnboarding()，/api/health 的 llm_ready=false
 * （无 key、占位 key）即弹出模态。保存走控制台既有端点（provider save →
 * test → slot 绑定），绑定热应用立即生效，无需重启。可关闭，关闭后聊天
 * 输入区保留「连接 AI 服务」重入按钮。样式复用 console.css 的 overlay/
 * dialog/field/btn 族。
 */

import { S } from './state.js';

let _pid = '';          // 引导创建的连接档案 id；重试时走编辑态（空 key = 保留旧值）
let _busy = false;      // 连接流程进行中，防重复提交

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
      <label class="console-field">模型名
        <span class="onboarding-keyrow">
          <input id="onboardModel" type="text" list="onboardModels"
                 placeholder="例如 gpt-4o / glm-4.7 / deepseek-chat" autocomplete="off">
          <datalist id="onboardModels"></datalist>
          <button class="console-btn" id="onboardFetchBtn" type="button"
                  title="从服务商拉取可用模型列表">拉取</button>
        </span>
      </label>
      <div class="console-test-out" id="onboardMsg"></div>
      <div class="onboarding-actions">
        <button class="console-btn console-btn-primary" id="onboardConnectBtn">测试并连接</button>
        <button class="console-btn" id="onboardAdvancedBtn" type="button"
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
  document.getElementById('onboardKeyToggle').onclick = function () {
    const input = document.getElementById('onboardKey');
    const showPw = input.type === 'password';
    input.type = showPw ? 'text' : 'password';
    this.textContent = showPw ? '隐藏' : '显示';
  };
  document.getElementById('onboardFetchBtn').onclick = fetchModels;
  document.getElementById('onboardConnectBtn').onclick = connect;
  document.getElementById('onboardAdvancedBtn').onclick = goAdvanced;
  ['onboardBase', 'onboardKey', 'onboardModel'].forEach((id) => {
    document.getElementById(id).addEventListener('keydown', function (e) {
      if (e.key === 'Enter') connect();
    });
  });
}

function show() {
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
  msg('正在拉取模型列表…', true);
  try {
    const data = await api('POST', '/api/console/models/fetch',
      { base_url: base, api_key: key, provider_id: _pid });
    const dl = document.getElementById('onboardModels');
    dl.innerHTML = (data.models || [])
      .map((m) => `<option value="${m.id}">`).join('');
    msg(`已拉取 ${data.models.length} 个模型，点击输入框选择`, true);
  } catch (e) {
    msg('拉取失败：' + e.message, false);
  } finally {
    btn.disabled = false;
  }
}

async function connect() {
  if (_busy) return;
  const base = document.getElementById('onboardBase').value.trim().replace(/\/+$/, '');
  key = document.getElementById('onboardKey').value.trim();
  const model = document.getElementById('onboardModel').value.trim();
  if (!base) { msg('请填写接口地址（API Base）', false); return; }
  if (!model) { msg('请填写模型名', false); return; }
  _busy = true;
  const btn = document.getElementById('onboardConnectBtn');
  btn.disabled = true;
  msg('正在测试连接…', true);
  try {
    const body = { name: '我的连接', base_url: base,
      models: [{ id: model, kind: 'llm' }] };
    if (_pid) body.id = _pid;
    if (key) body.api_key = key;
    const saved = await api('POST', '/api/console/providers', body);
    _pid = saved.provider.id;
    msg('档案已保存，正在测试连通性…', true);
    await api('POST', `/api/console/providers/${_pid}/test`, { model, kind: 'llm' });
    await api('PUT', '/api/console/slots/main_llm', { provider_id: _pid, model });
    const h = await api('GET', '/api/health');
    if (h.llm_ready) {
      hide();
      const entry = document.getElementById('onboardEntryBtn');
      if (entry) entry.remove();
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

/* 输入区重入按钮：关闭弹窗后仍可从这里再次打开引导。 */
function injectEntryBtn() {
  if (document.getElementById('onboardEntryBtn')) return;
  const modeBar = document.querySelector('.chat-mode-bar');
  if (!modeBar) return;
  const btn = document.createElement('button');
  btn.id = 'onboardEntryBtn';
  btn.className = 'mode-toggle onboard-entry';
  btn.title = '尚未连接 AI 服务，点击配置';
  btn.innerHTML = '<span class="mode-icon"><i data-lucide="plug-zap"></i></span>' +
    '<span class="mode-label">连接 AI 服务</span>';
  btn.onclick = show;
  modeBar.insertBefore(btn, modeBar.firstChild);
  if (window.lucide && lucide.createIcons) lucide.createIcons();
}

/* app.js 启动时调用：服务未配置（llm_ready=false）才弹引导。 */
export async function checkOnboarding() {
  try {
    const h = await api('GET', '/api/health');
    if (h && h.llm_ready) return;
  } catch (_) {
    return;  // health 不可达不弹窗，避免干扰纯浏览场景
  }
  ensureDom();
  injectEntryBtn();
  show();
}
