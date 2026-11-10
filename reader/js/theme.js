/* Liubai — theme settings. */

import { S } from './state.js';
import * as EpubReader from './epub-reader.js';

const THEME_DEFAULTS = {
  // 出厂默认阅读主题：象牙暖底 + 深墨绿字 + 楷体（用户实际在用的配色）。
  readerFontFamily: 'kai',
  readerFontSize: '18',
  // 沉浸阅读的版心宽度（em ≈ 每行字数）。54 是「宽屏上不像窄带、行宽
  // 又不至于太长」的折中；用户可在主题页 36–72 之间调。
  readerMeasure: '54',
  readerFontColor: '#183422',
  readerFontBrightness: '0.7',
  readerBgColor: '#ddb56e',
  readerBgBrightness: '1',
  chatFontSize: '14',
  chatFontColor: '#9a8e7a',
  selectionColor: '#ffffff',
  readerSelectionColor: '#7da9d8',
  // 选中高亮（背景层）：颜色 / 透明度 / 亮度。文字颜色仍由上面的
  // selectionColor / readerSelectionColor 控制；两层分开是为了让
  // 「浅底 + 深字」这种常见搭配不用在一个取色器里折中。
  selectionHlColor: '#b45a2d',
  selectionHlOpacity: '1',
  selectionHlBrightness: '1',
  readerSelectionHlColor: '#7da9d8',
  readerSelectionHlOpacity: '0.35',
  readerSelectionHlBrightness: '1',
  notesFontSize: '13',
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

const AGENT_DEFAULT_NAME = '留白';
const AGENT_NAME_MAX = 32;
const USER_NAME_MAX = 32;
const SOUL_WARN_CHARS = 6000;
const AVATAR_MAX_BYTES = 20 * 1024 * 1024;
const FONT_LABELS = { mono: '等距', song: '宋体', kai: '楷体', hei: '黑体', fangsong: '仿宋' };

let _agent = null;
let _agentLoaded = false;
let _identityBound = false;
let _nameTimer = 0;
let _nameSeq = 0;
let _saveStateTimer = 0;
let _soulOriginal = '';   // 打开编辑器时的渲染文本（含已替换的称呼/代词）
let _soulTemplate = '';   // 手稿模板原文（{user}/{ta} 原样）
let _soulDirty = false;
let _avatarUploading = false;
let _themeSaveTimer = 0;

let _user = null;
let _userLoaded = false;
let _userIdentityBound = false;
let _userNameTimer = 0;
let _userNameSeq = 0;
let _userSaveStateTimer = 0;
let _userAvatarUploading = false;

function fallbackAgent() {
  return { name: AGENT_DEFAULT_NAME, soul: '', soul_display: '', soul_exists: false,
           soul_is_default: true, soul_chars: 0, soul_updated_at: 0,
           avatar_url: '', has_avatar: false };
}

function fallbackUser() {
  return { name: '', gender: '', avatar_url: '', has_avatar: false };
}

// 模型在对话里调 update_identity 改了资料：设置页与身份卡不能停在旧值。
// 提示词侧由 load_persona 每轮现算，这里只负责界面。
document.addEventListener('bc:identity-changed', () => {
  refreshAgentProfile();
  refreshUserProfile();
});

function avatarSrc(url) {
  if (!url) return '';
  return /^(https?:|data:|blob:)/.test(url) ? url : S.API + url;
}

function formatSoulTime(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const hhmm = pad(d.getHours()) + ':' + pad(d.getMinutes());
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? '今天 ' + hhmm : (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hhmm;
}

function normalizeSoul(text) {
  return (text || '').replace(/\r\n?/g, '\n');
}

// 改名后不等下一次 /api/agent 往返：库存身份句/标题行当场跟随称呼，
// 否则用户改完名字立刻打开「个性化定义」还会看到旧名字。
function applyNameToSoul(soul, name) {
  const text = normalizeSoul(soul);
  if (!text || !name) return text;
  return text
    .replace(/^#\s*[^\n]*?\s*[—–-]\s*灵魂[ \t]*$/m, `# ${name} — 灵魂`)
    .replace(
      /^你是一个名为"[^"]*"（(?:Book Companion|Liubai)）的 AI 助手。/m,
      `你是一个名为"${name}"（Liubai）的 AI 助手。`
    );
}

function firstSoulLine(text) {
  for (const raw of (text || '').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || line.startsWith('---')
        || line.startsWith('>') || line.startsWith('|')) continue;
    const clean = line.replace(/^[-*]\s+/, '').replace(/`/g, '');
    if (clean.length < 6) continue;
    return clean.length > 78 ? clean.slice(0, 78) + '…' : clean;
  }
  return '';
}

function setAgentSaveState(text, isError = false) {
  const el = document.getElementById('agentSaveState');
  if (!el) return;
  clearTimeout(_saveStateTimer);
  el.textContent = text || '';
  el.classList.toggle('show', !!text);
  el.classList.toggle('err', !!isError);
  if (text) {
    _saveStateTimer = setTimeout(() => el.classList.remove('show'), 2200);
  }
}

function sizeNameInput(input, fallback, maxWidth) {
  if (!input) return;
  const computed = getComputedStyle(input);
  const canvas = sizeNameInput._canvas
    || (sizeNameInput._canvas = document.createElement('canvas'));
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const family = (computed.fontFamily || 'KaiTi').split(',')[0].trim();
  ctx.font = `${computed.fontWeight} ${computed.fontSize} ${family}`;
  // 空值时按占位文字量宽度，输入框不会先撑成一条空长条。
  const text = input.value || fallback;
  const width = Math.ceil(ctx.measureText(text).width) + 18;
  input.style.width = Math.max(76, Math.min(width, maxWidth)) + 'px';
}

function sizeAgentNameInput() {
  sizeNameInput(document.getElementById('agentNameInput'), '书', 360);
}

function sizeUserNameInput() {
  sizeNameInput(document.getElementById('userNameInput'), '待填写', 280);
}

function applyAgentName(name) {
  const clean = (name || '').trim() || AGENT_DEFAULT_NAME;
  S.agentName = clean;
  document.querySelectorAll('[data-agent-name]').forEach(el => {
    el.textContent = clean;
  });
  const input = document.getElementById('agentNameInput');
  if (input && document.activeElement !== input) input.value = clean;
  sizeAgentNameInput();
}

function applyAgentAvatar(url) {
  const clean = url || '';
  S.agentAvatarUrl = clean;
  const img = document.getElementById('agentAvatarImg');
  const fallback = document.getElementById('agentAvatarFallback');
  const remove = document.getElementById('agentAvatarRemove');
  const button = document.getElementById('agentAvatarBtn');
  if (img) {
    if (clean) {
      img.src = avatarSrc(clean);
      img.hidden = false;
    } else {
      img.hidden = true;
      img.removeAttribute('src');
    }
  }
  if (fallback) fallback.hidden = !!clean;
  if (remove) remove.hidden = !clean;
  if (button) {
    button.classList.toggle('has-avatar', !!clean);
    button.title = clean ? '更换头像' : '上传头像';
    button.setAttribute('aria-label', clean ? '更换 Agent 头像' : '上传 Agent 头像');
  }
  // chat.js owns the turn DOM. Broadcast instead of reaching into it: reloading
  // a session may finish before this fetch, and turning the avatar on/off has
  // to reach the turns already on screen too.
  document.dispatchEvent(new CustomEvent('bc:agent-avatar', {
    detail: { url: clean },
  }));
}

// SOUL.md 手稿里的 {user} / {ta} 是用户称呼与代词占位符（与
// persona.apply_user_name / apply_user_gender 同口径）。编辑器显示替换后的
// 文本，保存时再把未改动的行映射回模板，占位符不会被人名写死。
function resolveSoulText(text) {
  let soul = text || '';
  if (soul.indexOf('{user}') >= 0) {
    soul = soul.split('{user}').join((S.userName || '').trim() || '用户');
  }
  if (soul.indexOf('{ta}') >= 0) {
    soul = soul.split('{ta}').join(GENDER_TA[S.userGender] || 'ta');
  }
  return soul;
}

function soulForDisplay() {
  const soul = (_agent && _agent.soul) || '';
  if (!soul) return (_agent && _agent.soul_display) || '';
  return resolveSoulText(soul);
}

// 把编辑器里的「渲染文本」改回「占位符模板」：
//   · 逐行对齐模板与打开时的渲染文本（替换不改变行数）；
//   · 没改过的行沿用模板原文（占位符保留）；
//   · 改过 / 新增的行用编辑器里的文字（那一行里的占位符会变成具体值）。
// 用 LCS 对齐而不是按行号硬配，用户增删行时后面的占位符不会整体错位。
function mergeSoulEdits(rawText, baselineText, editedText) {
  const raw = normalizeSoul(rawText).split('\n');
  const base = normalizeSoul(baselineText).split('\n');
  const edit = normalizeSoul(editedText).split('\n');
  if (normalizeSoul(editedText) === normalizeSoul(baselineText)) {
    return raw.join('\n');           // 没动过：原样写回，一个字节不变
  }
  const n = base.length;
  const m = edit.length;
  // lcs[i][j] = base[i..] 与 edit[j..] 的最长公共子序列长度
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = base[i] === edit[j]
        ? lcs[i + 1][j + 1] + 1
        : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (base[i] === edit[j]) {
      out.push(raw[i] != null ? raw[i] : edit[j]);   // 没改的行：模板原文
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      i++;                                           // 模板里被删掉的行
    } else {
      out.push(edit[j]);                             // 新增/改写的行
      j++;
    }
  }
  while (j < m) { out.push(edit[j]); j++; }          // 末尾追加的行
  return out.join('\n');
}

function renderAgentMeta() {
  if (!_agent) return;
  const meta = document.getElementById('agentSoulMeta');
  const excerpt = document.getElementById('agentSoulExcerpt');
  const chars = Number(_agent.soul_chars) || 0;
  const custom = !!_agent.soul_exists && !_agent.soul_is_default;
  if (meta) {
    const parts = ['SOUL.md', custom ? '已定制' : '出厂人格',
                   chars.toLocaleString('zh-CN') + ' 字'];
    if (_agent.soul_updated_at) parts.push(formatSoulTime(_agent.soul_updated_at));
    meta.textContent = parts.join(' · ');
  }
  if (excerpt) {
    const line = firstSoulLine(soulForDisplay());
    if (line) {
      excerpt.textContent = '“' + line + '”';
    } else {
      excerpt.textContent = custom ? '（灵魂文稿为空）' : '还没有写下的灵魂——点「个性化定义」开始。';
    }
  }
}

export async function loadAgentProfile(force = false) {
  initAgentIdentity();
  if (_agentLoaded && !force) {
    applyAgentName(_agent.name);
    applyAgentAvatar(_agent.avatar_url);
    renderAgentMeta();
    return _agent;
  }
  try {
    const resp = await fetch(S.API + '/api/agent');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    _agent = await resp.json();
  } catch (err) {
    _agent = fallbackAgent();
  }
  _agentLoaded = true;
  applyAgentName(_agent.name);
  applyAgentAvatar(_agent.avatar_url);
  renderAgentMeta();
  return _agent;
}

export function refreshAgentProfile() {
  loadAgentProfile(true);
}

// ── User identity (自己的称呼 + 头像) ─────────────────────────────
function applyUserName(name) {
  const clean = (name || '').trim();
  S.userName = clean;
  const input = document.getElementById('userNameInput');
  if (input && document.activeElement !== input) input.value = clean;
  sizeUserNameInput();
  renderAgentMeta();   // SOUL 摘要里的 {user} 跟着称呼变
}

// 性别只影响第三人称代词：确认后 SOUL 里的 {ta} 换成「他 / 她」，
// 未设置保持中性的「ta」。
const GENDER_TA = { male: '他', female: '她' };

function applyUserGender(gender) {
  const clean = GENDER_TA[gender] ? gender : '';
  S.userGender = clean;
  const value = document.getElementById('userGenderValue');
  if (value) {
    value.textContent = clean === 'male' ? '男' : (clean === 'female' ? '女' : '未设置');
  }
  const row = document.getElementById('userGenderRow');
  if (row) {
    row.querySelectorAll('.user-gender-option').forEach(btn => {
      btn.setAttribute('aria-pressed',
        String((btn.dataset.gender || '') === clean));
    });
  }
  renderAgentMeta();   // SOUL 摘要里的 {ta} 跟着性别变
}

function setGenderPanelOpen(open) {
  const row = document.getElementById('userGenderRow');
  const trigger = document.getElementById('userGenderTrigger');
  if (!row || !trigger) return;
  row.classList.toggle('open', !!open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
}

async function saveUserGender(gender) {
  const clean = GENDER_TA[gender] ? gender : '';
  if (clean === S.userGender) return;
  const old = S.userGender;
  const seq = ++_userNameSeq;
  applyUserGender(clean);
  setUserSaveState('保存中…');
  try {
    const resp = await fetch(S.API + '/api/user/gender', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gender: clean }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    if (seq !== _userNameSeq) return;
    _user.gender = data.gender || clean;
    applyUserGender(_user.gender);
    setUserSaveState('已保存');
  } catch (err) {
    if (seq !== _userNameSeq) return;
    _user.gender = old;
    applyUserGender(old);
    setUserSaveState(err.message || '保存失败', true);
  }
}

function applyUserAvatar(url) {
  const clean = url || '';
  S.userAvatarUrl = clean;
  const img = document.getElementById('userAvatarImg');
  const fallback = document.getElementById('userAvatarFallback');
  const remove = document.getElementById('userAvatarRemove');
  const button = document.getElementById('userAvatarBtn');
  if (img) {
    if (clean) {
      img.src = avatarSrc(clean);
      img.hidden = false;
    } else {
      img.hidden = true;
      img.removeAttribute('src');
    }
  }
  if (fallback) fallback.hidden = !!clean;
  if (remove) remove.hidden = !clean;
  if (button) {
    button.classList.toggle('has-avatar', !!clean);
    button.title = clean ? '更换我的头像' : '上传我的头像';
    button.setAttribute('aria-label', clean ? '更换我的头像' : '上传我的头像');
  }
  // chat.js owns the message rows; broadcast so history painted before this
  // fetch gets the avatar without blocking the conversation.
  document.dispatchEvent(new CustomEvent('bc:user-avatar', {
    detail: { url: clean },
  }));
  // 交互预览的右侧气泡也带用户头像；正在看交互页时顺手重画。
  if (typeof _activePreviewName === 'function' && _activePreviewName() === 'chat') {
    refreshChatPreview();
  }
}

function setUserSaveState(text, isError = false) {
  const el = document.getElementById('userSaveState');
  if (!el) return;
  clearTimeout(_userSaveStateTimer);
  el.textContent = text || '';
  el.classList.toggle('show', !!text);
  el.classList.toggle('err', !!isError);
  if (text) {
    _userSaveStateTimer = setTimeout(() => el.classList.remove('show'), 2200);
  }
}

export async function loadUserProfile(force = false) {
  initUserIdentity();
  if (_userLoaded && !force) {
    applyUserName(_user.name);
    applyUserGender(_user.gender);
    applyUserAvatar(_user.avatar_url);
    return _user;
  }
  try {
    const resp = await fetch(S.API + '/api/user');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    _user = await resp.json();
  } catch (err) {
    if (!_user) _user = fallbackUser();
  }
  _userLoaded = true;
  applyUserName(_user.name);
  applyUserGender(_user.gender);
  applyUserAvatar(_user.avatar_url);
  return _user;
}

export function refreshUserProfile() {
  loadUserProfile(true);
}

export async function reloadAgentSoul() {
  await loadAgentProfile(true);
  setAgentSaveState('已重新读取');
}

function initAgentIdentity() {
  if (_identityBound) return;
  _identityBound = true;
  const input = document.getElementById('agentNameInput');
  if (input) {
    let composing = false;
    input.addEventListener('input', () => {
      sizeAgentNameInput();
      clearTimeout(_nameTimer);
      // 输入法组字期间的 input 是拼音/候选字，不该落库；等 compositionend
      // 再保存，否则中文用户按空格/回车选词会被当成「改名成功」。
      if (!composing) _nameTimer = setTimeout(saveAgentName, 700);
    });
    input.addEventListener('compositionstart', () => {
      composing = true;
      clearTimeout(_nameTimer);
    });
    input.addEventListener('compositionend', () => {
      composing = false;
      sizeAgentNameInput();
      clearTimeout(_nameTimer);
      _nameTimer = setTimeout(saveAgentName, 400);
    });
    input.addEventListener('change', () => {
      if (composing) return;
      // 退出编辑（失焦 / 回车 / Tab）才算一次提交；空值此时才回退。
      saveAgentName(true);
    });
    input.addEventListener('keydown', e => {
      // 输入法用回车选候选字时，这个 Enter 不属于「提交改名」。
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        clearTimeout(_nameTimer);
        saveAgentName(true);
        input.blur();
      }
    });
  }
  const editor = document.getElementById('soulEditor');
  if (editor) {
    editor.addEventListener('input', onSoulInput);
    editor.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        saveSoul();
      }
    });
  }
  const file = document.getElementById('agentAvatarFile');
  if (file) {
    file.addEventListener('change', () => {
      const picked = file.files && file.files[0];
      if (picked) uploadAgentAvatar(picked);
      file.value = '';
    });
  }
  window.addEventListener('beforeunload', e => {
    if (!_soulDirty) return;
    e.preventDefault();
    e.returnValue = '';
  });
}

export function focusAgentName() {
  const input = document.getElementById('agentNameInput');
  if (!input) return;
  input.focus();
  input.select();
}

export function pickAgentAvatar() {
  if (_avatarUploading) return;
  const file = document.getElementById('agentAvatarFile');
  if (file) file.click();
}

async function makeAvatarBlob(file) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch (_) {
    throw new Error('无法读取这张图片');
  }
  try {
    const side = Math.min(bitmap.width, bitmap.height);
    const sx = (bitmap.width - side) / 2;
    const sy = (bitmap.height - side) / 2;
    const canvas = document.createElement('canvas');
    canvas.width = 512;
    canvas.height = 512;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('无法处理图片');
    // 背景取纸色：JPEG 没有透明通道，透明 PNG 也不会变成黑底。
    ctx.fillStyle = '#f3ece0';
    ctx.fillRect(0, 0, 512, 512);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(bitmap, sx, sy, side, side, 0, 0, 512, 512);
    return await new Promise((resolve, reject) => {
      canvas.toBlob(
        blob => blob ? resolve(blob) : reject(new Error('无法生成头像')),
        'image/jpeg', 0.9);
    });
  } finally {
    if (bitmap && typeof bitmap.close === 'function') bitmap.close();
  }
}

async function uploadAgentAvatar(file) {
  if (!file.type || !file.type.startsWith('image/')) {
    setAgentSaveState('请选择图片文件', true);
    return;
  }
  if (file.size > AVATAR_MAX_BYTES) {
    setAgentSaveState('图片过大（上限 20MB）', true);
    return;
  }
  _avatarUploading = true;
  setAgentSaveState('上传中…');
  try {
    const blob = await makeAvatarBlob(file);
    const form = new FormData();
    form.append('file', blob, 'avatar.jpg');
    const resp = await fetch(S.API + '/api/agent/avatar', {
      method: 'POST',
      body: form,
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    _agent = { ..._agent, ...data };
    applyAgentAvatar(_agent.avatar_url);
    setAgentSaveState('头像已更新');
  } catch (err) {
    setAgentSaveState(err.message || '上传失败', true);
  } finally {
    _avatarUploading = false;
  }
}

export async function removeAgentAvatar() {
  if (!S.agentAvatarUrl) return;
  setAgentSaveState('移除中…');
  try {
    const resp = await fetch(S.API + '/api/agent/avatar', { method: 'DELETE' });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    _agent = { ..._agent, ...data };
    applyAgentAvatar('');
    setAgentSaveState('已移除头像');
  } catch (err) {
    setAgentSaveState(err.message || '移除失败', true);
  }
}

async function saveAgentName(commit = false) {
  clearTimeout(_nameTimer);
  const input = document.getElementById('agentNameInput');
  if (!input || !_agent) return;
  const old = (_agent.name || AGENT_DEFAULT_NAME).trim() || AGENT_DEFAULT_NAME;
  let name = input.value.trim();
  if (!name) {
    // 编辑过程中允许暂时空着（全选删除后可能马上要输新名字）。
    // 只有退出编辑时仍为空，才回退到当前已保存的名字。
    if (!commit) return;
    input.value = old;
    sizeAgentNameInput();
    setAgentSaveState('已恢复原名');
    return;
  }
  name = Array.from(name).slice(0, AGENT_NAME_MAX).join('');
  input.value = name;
  if (name === old) return;
  const seq = ++_nameSeq;
  setAgentSaveState('保存中…');
  _agent.name = name;
  applyAgentName(name);
  try {
    const resp = await fetch(S.API + '/api/agent/name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    if (seq !== _nameSeq) return;
    _agent.name = data.name || name;
    _agent.soul = applyNameToSoul(_agent.soul, _agent.name);
    _agent.soul_display = applyNameToSoul(_agent.soul_display || '', _agent.name);
    _agent.soul_chars = _agent.soul.length;
    applyAgentName(_agent.name);
    renderAgentMeta();
    setAgentSaveState('已保存');
  } catch (err) {
    if (seq !== _nameSeq) return;
    _agent.name = old;
    applyAgentName(old);
    input.value = old;
    setAgentSaveState(err.message || '保存失败', true);
  }
}

function initUserIdentity() {
  if (_userIdentityBound) return;
  _userIdentityBound = true;
  const input = document.getElementById('userNameInput');
  if (input) {
    let composing = false;
    sizeUserNameInput();
    input.addEventListener('input', () => {
      sizeUserNameInput();
      clearTimeout(_userNameTimer);
      // 输入法组字期间是拼音/候选字，不能当成名字落库。
      if (!composing) _userNameTimer = setTimeout(saveUserName, 700);
    });
    input.addEventListener('compositionstart', () => {
      composing = true;
      clearTimeout(_userNameTimer);
    });
    input.addEventListener('compositionend', () => {
      composing = false;
      sizeUserNameInput();
      clearTimeout(_userNameTimer);
      _userNameTimer = setTimeout(saveUserName, 400);
    });
    input.addEventListener('change', () => {
      if (composing) return;
      saveUserName();
    });
    input.addEventListener('keydown', e => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        clearTimeout(_userNameTimer);
        saveUserName();
        input.blur();
      }
    });
  }
  const genderRow = document.getElementById('userGenderRow');
  const genderTrigger = document.getElementById('userGenderTrigger');
  if (genderTrigger) {
    genderTrigger.addEventListener('click', e => {
      e.stopPropagation();
      setGenderPanelOpen(!(genderRow && genderRow.classList.contains('open')));
    });
  }
  if (genderRow) {
    genderRow.addEventListener('click', e => {
      const btn = e.target.closest('.user-gender-option');
      if (!btn) return;
      setGenderPanelOpen(false);
      saveUserGender(btn.dataset.gender || '');
    });
  }
  // 点面板/触发器以外的地方收起；只在打开时挂一次文档监听。
  document.addEventListener('click', e => {
    if (!genderRow || !genderRow.classList.contains('open')) return;
    if (!genderRow.contains(e.target)) setGenderPanelOpen(false);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && genderRow
        && genderRow.classList.contains('open')) {
      setGenderPanelOpen(false);
    }
  });
  const file = document.getElementById('userAvatarFile');
  if (file) {
    file.addEventListener('change', () => {
      const picked = file.files && file.files[0];
      if (picked) uploadUserAvatar(picked);
      file.value = '';
    });
  }
}

export function focusUserName() {
  const input = document.getElementById('userNameInput');
  if (!input) return;
  input.focus();
  input.select();
}

export function pickUserAvatar() {
  if (_userAvatarUploading) return;
  const file = document.getElementById('userAvatarFile');
  if (file) file.click();
}

async function saveUserName() {
  clearTimeout(_userNameTimer);
  const input = document.getElementById('userNameInput');
  if (!input || !_user) return;
  const old = (_user.name || '').trim();
  // 用户的名字允许为空：空 = 不注入称呼，不强行回退到某个默认名。
  const name = Array.from(input.value.trim()).slice(0, USER_NAME_MAX).join('');
  input.value = name;
  if (name === old) return;
  const seq = ++_userNameSeq;
  setUserSaveState('保存中…');
  _user.name = name;
  applyUserName(name);
  try {
    const resp = await fetch(S.API + '/api/user/name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    if (seq !== _userNameSeq) return;
    _user.name = data.name || name;
    applyUserName(_user.name);
    setUserSaveState('已保存');
  } catch (err) {
    if (seq !== _userNameSeq) return;
    _user.name = old;
    applyUserName(old);
    input.value = old;
    setUserSaveState(err.message || '保存失败', true);
  }
}

async function uploadUserAvatar(file) {
  if (_userAvatarUploading) return;
  _userAvatarUploading = true;
  setUserSaveState('上传中…');
  try {
    const blob = await makeAvatarBlob(file);
    const form = new FormData();
    form.append('file', blob, 'avatar.jpg');
    const resp = await fetch(S.API + '/api/user/avatar', {
      method: 'POST',
      body: form,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    _user = { ..._user, ...data };
    applyUserAvatar(_user.avatar_url);
    setUserSaveState('头像已更新');
  } catch (err) {
    setUserSaveState(err.message || '上传失败', true);
  } finally {
    _userAvatarUploading = false;
  }
}

async function removeUserAvatar() {
  if (_userAvatarUploading) return;
  _userAvatarUploading = true;
  setUserSaveState('移除中…');
  try {
    const resp = await fetch(S.API + '/api/user/avatar', { method: 'DELETE' });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    _user = { ..._user, ...data };
    applyUserAvatar(_user.avatar_url);
    setUserSaveState('头像已移除');
  } catch (err) {
    setUserSaveState(err.message || '移除失败', true);
  } finally {
    _userAvatarUploading = false;
  }
}

export async function openSoulEditor() {
  if (!_agent) await loadAgentProfile();
  const view = document.getElementById('soulView');
  const settings = document.getElementById('agentSettings');
  const editor = document.getElementById('soulEditor');
  if (!view || !settings || !editor) return;
  // 编辑器显示替换后的文本；保存时未改动的行会映射回占位符模板。
  _soulTemplate = normalizeSoul(_agent.soul);
  _soulOriginal = resolveSoulText(_soulTemplate);
  editor.value = _soulOriginal;
  _soulDirty = false;
  settings.hidden = true;
  view.hidden = false;
  updateSoulMeta();
  editor.focus();
  editor.setSelectionRange(0, 0);
  editor.scrollTop = 0;
}

export function closeSoulEditor() {
  if (_soulDirty && !confirm('SOUL.md 有未保存的修改，确定离开吗？')) return;
  const view = document.getElementById('soulView');
  const settings = document.getElementById('agentSettings');
  if (view) view.hidden = true;
  if (settings) settings.hidden = false;
}

function onSoulInput() {
  const editor = document.getElementById('soulEditor');
  if (!editor) return;
  _soulDirty = editor.value !== _soulOriginal;
  updateSoulMeta();
}

function updateSoulMeta() {
  const editor = document.getElementById('soulEditor');
  if (!editor) return;
  const text = editor.value;
  const chars = text.length;
  _soulDirty = text !== _soulOriginal;
  const charEl = document.getElementById('soulCharCount');
  if (charEl) {
    charEl.textContent = chars.toLocaleString('zh-CN') + ' 字'
      + (chars > SOUL_WARN_CHARS ? ' · 偏长' : '');
    charEl.classList.toggle('warn', chars > SOUL_WARN_CHARS);
  }
  const status = document.getElementById('soulStatus');
  if (status) status.textContent = _soulDirty ? '未保存' : '已保存';
  const boundary = document.getElementById('soulBoundaryHint');
  if (boundary) {
    boundary.textContent = (text.trim() && !/^##\s+边界/m.test(text))
      ? '未检测到「## 边界」，扮演模式将使用兜底边界'
      : '';
  }
  const saveBtn = document.getElementById('soulSaveBtn');
  if (saveBtn) saveBtn.disabled = !_soulDirty;
  const revertBtn = document.getElementById('soulRevertBtn');
  if (revertBtn) revertBtn.disabled = !_soulDirty;
}

export async function saveSoul() {
  const editor = document.getElementById('soulEditor');
  if (!editor) return;
  // 编辑器里是渲染文本，落盘前把没改过的行换回 {user}/{ta} 模板。
  const content = mergeSoulEdits(_soulTemplate, _soulOriginal, editor.value);
  if (!content.trim()) {
    alert('SOUL.md 不能为空。想恢复出厂人格请用「恢复出厂」。');
    return;
  }
  if (!_soulDirty) return;
  const status = document.getElementById('soulStatus');
  if (status) status.textContent = '保存中…';
  const saveBtn = document.getElementById('soulSaveBtn');
  if (saveBtn) saveBtn.disabled = true;
  try {
    const resp = await fetch(S.API + '/api/agent/soul', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    _agent = { ..._agent, ...data };
    _soulTemplate = normalizeSoul(_agent.soul || content);
    _soulOriginal = resolveSoulText(_soulTemplate);
    editor.value = _soulOriginal;
    _soulDirty = false;
    updateSoulMeta();
    renderAgentMeta();
  } catch (err) {
    if (status) status.textContent = '保存失败';
    alert('保存失败：' + (err.message || err));
    updateSoulMeta();
  }
}

export function revertSoul() {
  if (!_soulDirty) return;
  if (!confirm('还原到上次保存的内容？当前未保存的修改会丢弃。')) return;
  const editor = document.getElementById('soulEditor');
  if (!editor) return;
  editor.value = _soulOriginal;
  _soulDirty = false;
  updateSoulMeta();
}

export async function resetSoul() {
  if (!confirm('恢复出厂人格会覆盖当前 SOUL.md，并自动备份旧文件。继续吗？')) return;
  const status = document.getElementById('soulStatus');
  if (status) status.textContent = '恢复中…';
  try {
    const resp = await fetch(S.API + '/api/agent/soul/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    _agent = { ..._agent, ...data };
    const editor = document.getElementById('soulEditor');
    _soulTemplate = normalizeSoul(_agent.soul);
    _soulOriginal = resolveSoulText(_soulTemplate);
    if (editor) editor.value = _soulOriginal;
    _soulDirty = false;
    updateSoulMeta();
    renderAgentMeta();
    if (status) status.textContent = '已恢复出厂';
  } catch (err) {
    if (status) status.textContent = '恢复失败';
    alert('恢复失败：' + (err.message || err));
  }
}

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
  root.style.setProperty('--t-reader-measure', (s.readerMeasure || '54') + 'em');
  root.style.setProperty('--t-chat-font-size', s.chatFontSize + 'px');
  root.style.setProperty('--t-chat-font-color', s.chatFontColor);
  root.style.setProperty('--t-notes-font-size', s.notesFontSize + 'px');
  root.style.setProperty('--t-selection-color', s.selectionColor);
  root.style.setProperty('--t-reader-selection-color', s.readerSelectionColor);
  root.style.setProperty('--t-selection-bg',
    _selectionBg(s.selectionHlColor, s.selectionHlOpacity, s.selectionHlBrightness));
  root.style.setProperty('--t-reader-selection-bg',
    _selectionBg(s.readerSelectionHlColor, s.readerSelectionHlOpacity, s.readerSelectionHlBrightness));
  // 原生 EPUB 渲染激活时同步注入 iframe（主路径直通，消除 observer 防抖延迟；
  // 主题系统是唯一写入口，显式调用比等 MutationObserver 更可靠）。
  EpubReader.applyThemeFromDom();
}

/* 高亮背景 = 取色器颜色 × 亮度，再叠透明度。亮度在 RGB 上直接乘，
   0.6–1.4 与正文字体/背景亮度的量纲一致，感观上是同一套「调亮调暗」。 */
function _hexToRgb(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(hex || '').trim());
  if (!m) return null;
  return { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) };
}

function _selectionBg(color, opacity, brightness) {
  const rgb = _hexToRgb(color);
  if (!rgb) return color;
  const k = Number.isFinite(parseFloat(brightness)) ? parseFloat(brightness) : 1;
  const a = Number.isFinite(parseFloat(opacity)) ? parseFloat(opacity) : 1;
  const clamp = (v) => Math.max(0, Math.min(255, Math.round(v * k)));
  return `rgba(${clamp(rgb.r)}, ${clamp(rgb.g)}, ${clamp(rgb.b)}, ${Math.max(0, Math.min(1, a))})`;
}

function updateThemeSummaries(s) {
  const reader = document.getElementById('readerSummary');
  if (reader) reader.textContent = `${s.readerFontSize}px · ${FONT_LABELS[s.readerFontFamily] || '等宽'} · ${s.readerMeasure}em`;
  const chat = document.getElementById('chatSummary');
  if (chat) chat.textContent = `${s.chatFontSize}px · ${s.chatFontColor}`;
  const notes = document.getElementById('notesSummary');
  if (notes) notes.textContent = `${s.notesFontSize}px`;
}

function setThemeSaveState(text, isError = false) {
  const el = document.getElementById('themeSaveState');
  if (!el) return;
  clearTimeout(_themeSaveTimer);
  el.textContent = text || '';
  el.classList.toggle('show', !!text);
  el.classList.toggle('err', !!isError);
  if (text) _themeSaveTimer = setTimeout(() => el.classList.remove('show'), 1800);
}

export function populateThemeForm(s) {
  document.getElementById('tReaderFontFamily').value = s.readerFontFamily;
  document.getElementById('tReaderFontSize').value = s.readerFontSize;
  document.getElementById('tReaderFontColor').value = s.readerFontColor;
  document.getElementById('tReaderFontColorVal').textContent = s.readerFontColor;
  var measureEl = document.getElementById('tReaderMeasure');
  if (measureEl) {
    measureEl.value = s.readerMeasure;
    document.getElementById('tReaderMeasureVal').textContent = s.readerMeasure + ' em';
  }
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
  document.getElementById('tSelectionHlColor').value = s.selectionHlColor;
  document.getElementById('tSelectionHlColorVal').textContent = s.selectionHlColor;
  document.getElementById('tSelectionHlOpacity').value = s.selectionHlOpacity;
  document.getElementById('tSelectionHlOpacityVal').textContent = parseFloat(s.selectionHlOpacity).toFixed(2);
  document.getElementById('tSelectionHlBrightness').value = s.selectionHlBrightness;
  document.getElementById('tSelectionHlBrightnessVal').textContent = parseFloat(s.selectionHlBrightness).toFixed(1);
  document.getElementById('tReaderSelectionHlColor').value = s.readerSelectionHlColor;
  document.getElementById('tReaderSelectionHlColorVal').textContent = s.readerSelectionHlColor;
  document.getElementById('tReaderSelectionHlOpacity').value = s.readerSelectionHlOpacity;
  document.getElementById('tReaderSelectionHlOpacityVal').textContent = parseFloat(s.readerSelectionHlOpacity).toFixed(2);
  document.getElementById('tReaderSelectionHlBrightness').value = s.readerSelectionHlBrightness;
  document.getElementById('tReaderSelectionHlBrightnessVal').textContent = parseFloat(s.readerSelectionHlBrightness).toFixed(1);
  updateThemeSummaries(s);
}

function readThemeForm() {
  return {
    readerFontFamily: document.getElementById('tReaderFontFamily').value,
    readerFontSize: document.getElementById('tReaderFontSize').value,
    readerMeasure: document.getElementById('tReaderMeasure').value,
    readerFontColor: document.getElementById('tReaderFontColor').value,
    readerFontBrightness: document.getElementById('tReaderFontBrightness').value,
    readerBgColor: document.getElementById('tReaderBgColor').value,
    readerBgBrightness: document.getElementById('tReaderBgBrightness').value,
    chatFontSize: document.getElementById('tChatFontSize').value,
    chatFontColor: document.getElementById('tChatFontColor').value,
    notesFontSize: document.getElementById('tNotesFontSize').value,
    selectionColor: document.getElementById('tSelectionColor').value,
    readerSelectionColor: document.getElementById('tReaderSelectionColor').value,
    selectionHlColor: document.getElementById('tSelectionHlColor').value,
    selectionHlOpacity: document.getElementById('tSelectionHlOpacity').value,
    selectionHlBrightness: document.getElementById('tSelectionHlBrightness').value,
    readerSelectionHlColor: document.getElementById('tReaderSelectionHlColor').value,
    readerSelectionHlOpacity: document.getElementById('tReaderSelectionHlOpacity').value,
    readerSelectionHlBrightness: document.getElementById('tReaderSelectionHlBrightness').value,
  };
}

/* 原生取色器拖动时连续触发 input：只把变量应用到右侧预览与色值文字，
   不落盘、不重填表单。松手关掉取色器才走 onThemeChange 保存。 */
const THEME_COLOR_FIELDS = [
  ['readerFontColor', 'tReaderFontColor'],
  ['readerBgColor', 'tReaderBgColor'],
  ['readerSelectionColor', 'tReaderSelectionColor'],
  ['readerSelectionHlColor', 'tReaderSelectionHlColor'],
  ['chatFontColor', 'tChatFontColor'],
  ['selectionColor', 'tSelectionColor'],
  ['selectionHlColor', 'tSelectionHlColor'],
];

// 滑块同样实时刷新示数：[字段, 元素 id, 小数位]
const THEME_RANGE_FIELDS = [
  ['selectionHlOpacity', 'tSelectionHlOpacity', 2],
  ['selectionHlBrightness', 'tSelectionHlBrightness', 1],
  ['readerSelectionHlOpacity', 'tReaderSelectionHlOpacity', 2],
  ['readerSelectionHlBrightness', 'tReaderSelectionHlBrightness', 1],
];

let _themeInputRaf = 0;
export function onThemeInput() {
  // 取色器可能一帧发多个 input；合到下一帧统一应用，避免重复写 iframe 样式。
  if (_themeInputRaf) return;
  _themeInputRaf = requestAnimationFrame(() => {
    _themeInputRaf = 0;
    const s = readThemeForm();
    applySettings(s);
    THEME_COLOR_FIELDS.forEach(([key, id]) => {
      const label = document.getElementById(id + 'Val');
      if (label) label.textContent = s[key];
    });
    THEME_RANGE_FIELDS.forEach(([key, id, digits]) => {
      const label = document.getElementById(id + 'Val');
      if (label) label.textContent = parseFloat(s[key]).toFixed(digits);
    });
  });
}

export function onThemeChange() {
  const s = readThemeForm();
  saveSettings(s);
  applySettings(s);
  populateThemeForm(s);
  setThemeSaveState('已自动保存');
}

/* 选中高亮的预览演示：原生取色器一打开就盖住页面，用户看不到正文，
   没法一边调一边选字看效果。所以打开「选中字色 / 高亮颜色」取色器期间，
   把预览台第一段正文临时画成选中态，关掉取色器（change / blur）即撤。
   演示层只加一个 class，颜色与透明度全部读 CSS 变量，拖动时自动跟着变。 */
let _selectionDemoOn = false;

function _applySelectionDemo(on) {
  _selectionDemoOn = !!on;
  const host = document.querySelector('#previewReader .preview-book-text');
  if (!host) return;
  const existing = host.querySelector('.preview-selection-demo');
  if (on) {
    if (existing) return;
    const text = host.textContent || '';
    if (!text) return;
    // 预览正文用 \n\n 分段；只高亮第一段。
    const cut = text.indexOf('\n\n');
    const head = cut >= 0 ? text.slice(0, cut) : text;
    const tail = cut >= 0 ? text.slice(cut) : '';
    host.textContent = '';
    const span = document.createElement('span');
    span.className = 'preview-selection-demo';
    span.textContent = head;
    host.appendChild(span);
    if (tail) host.appendChild(document.createTextNode(tail));
  } else if (existing) {
    // 读回 textContent 再赋值，会把演示 span 拍平成纯文本。
    host.textContent = host.textContent;
  }
}

export function onSelectionPreview(on) {
  _applySelectionDemo(on);
}

// 兜底：原生取色器点「取消」时不保证发 change / blur，演示层会挂住。
// 捕获阶段先跑：点到别的控件就先撤，点到这几个控件本身则忽略（元素自己的
// pointerdown 再把演示打开，净效果仍是「开」）。
const _SELECTION_PREVIEW_SEL = [
  '#tReaderSelectionColor', '#tReaderSelectionHlColor',
  '#tReaderSelectionHlOpacity', '#tReaderSelectionHlBrightness',
].join(', ');
document.addEventListener('pointerdown', (e) => {
  if (!_selectionDemoOn) return;
  const target = e.target;
  if (target && target.closest && target.closest(_SELECTION_PREVIEW_SEL)) return;
  _applySelectionDemo(false);
}, true);

export function themeSectionKey(e, name) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    switchPreview(name);
  }
}

export function switchPreview(name) {
  document.querySelectorAll('.theme-section').forEach(s => {
    const on = s.dataset.section === name;
    s.classList.toggle('active-section', on);
    s.classList.toggle('expanded', on);
    const head = s.querySelector('.theme-section-head');
    if (head) head.setAttribute('aria-expanded', on ? 'true' : 'false');
    const body = s.querySelector('.theme-section-body');
    if (body) body.toggleAttribute('inert', !on);
  });
  const scope = document.getElementById('previewScope');
  if (scope) {
    const section = document.querySelector(`.theme-section[data-section="${name}"]`);
    const title = section && section.querySelector('.theme-section-title h3');
    if (title) scope.textContent = title.textContent.trim();
  }

  document.querySelectorAll('.preview-stage').forEach(s => s.classList.remove('active'));
  const stage = document.getElementById('preview' + name.charAt(0).toUpperCase() + name.slice(1));
  if (stage) stage.classList.add('active');
  refreshBenchPreview(name);
}

// ── Live preview bench ─────────────────────────────────────────
// 右栏预览台不画样张：阅读用上次打开的书，交互用上次会话，笔记用本书
// 真实浮窗。全部只读——chunk 不传 record=1，/api/books 不触发
// touch_book_opened，也不写会话或批注。
const PREVIEW_CHUNKS = 3;
const PREVIEW_MESSAGES = 4;
const PREVIEW_MESSAGE_CHARS = 180;
const _previewSeq = { reader: 0, chat: 0, notes: 0 };
// bc_view 在切到主题页末尾会被 saveViewState 用当前空值覆盖；首次预览
// 解析出的书/会话 id 先留在模块里，后面切段仍指向同一个现场。
let _previewLastBook = '';
let _previewLastSession = '';
// 笔记预览的底图复用阅读预览已经读出的真实正文，避免同一个书块再拉一次。
let _previewPassage = null;   // {bookId, kicker, text}

function _previewViewState() {
  try {
    return JSON.parse(localStorage.getItem('bc_view') || '{}') || {};
  } catch (e) {
    return {};
  }
}

function _previewBookId() {
  if (S.currentBook) return S.currentBook;
  if (_previewLastBook) return _previewLastBook;
  const id = _previewViewState().bookId || '';
  if (id) _previewLastBook = id;
  return id;
}

function _previewSessionId() {
  if (S.currentSessionId) return S.currentSessionId;
  if (_previewLastSession) return _previewLastSession;
  const id = _previewViewState().sessionId || '';
  if (id) _previewLastSession = id;
  return id;
}

function _previewEmpty(title, hint) {
  const wrap = document.createElement('div');
  wrap.className = 'preview-empty';
  const head = document.createElement('strong');
  head.textContent = title;
  wrap.appendChild(head);
  if (hint) {
    const sub = document.createElement('span');
    sub.textContent = hint;
    wrap.appendChild(sub);
  }
  return wrap;
}

function _activePreviewName() {
  const section = document.querySelector('.theme-section.active-section');
  return section ? section.dataset.section : 'reader';
}

export function refreshBenchPreview(name) {
  const target = name || _activePreviewName();
  if (target === 'reader') refreshReaderPreview();
  else if (target === 'chat') refreshChatPreview();
  else if (target === 'notes') refreshNotesPreview();
}

function _bookTouch(b) {
  return Number(b.last_read_at || 0) || Number(b.last_opened_at || 0) || 0;
}

async function _findPreviewBook(bookId) {
  const resp = await fetch(S.API + '/api/books');
  if (!resp.ok) throw new Error('books ' + resp.status);
  const data = await resp.json();
  const books = data.books || [];
  if (bookId) {
    const found = books.find(b => String(b.id) === String(bookId)) || null;
    if (found) _previewLastBook = found.id;
    return found;
  }
  // 没有当前书（例如刷新后直接落在主题页）时，仍按「上次打开/读过」挑一本。
  const touched = books
    .filter(b => _bookTouch(b))
    .sort((a, b) => _bookTouch(b) - _bookTouch(a));
  const picked = touched[0] || null;
  if (picked) _previewLastBook = picked.id;
  return picked;
}

function _bookLabel(title) {
  const clean = String(title || '').trim();
  if (!clean) return '';
  return /^《.*》$/.test(clean) ? clean : '《' + clean + '》';
}

async function _fetchPreviewChunk(bookId, chapter, chunk) {
  const url = S.API + '/api/books/' + encodeURIComponent(bookId)
    + '/chunk?chapter=' + chapter + '&chunk=' + chunk;
  const resp = await fetch(url);
  if (!resp.ok) return null;
  const data = await resp.json();
  return data.error ? null : data;
}

function _previewPosition(book) {
  let chapter = Number(book.last_chapter);
  let chunk = Number(book.last_chunk);
  if (!Number.isInteger(chapter) || chapter < 0) chapter = 0;
  if (!Number.isInteger(chunk) || chunk < 0) chunk = 0;
  return { chapter, chunk };
}

async function _loadPreviewPassage(book) {
  const bookId = String(book.id);
  let { chapter, chunk } = _previewPosition(book);
  let first = await _fetchPreviewChunk(bookId, chapter, chunk);
  if (!first && (chapter !== 0 || chunk !== 0)) {
    chapter = 0;
    chunk = 0;
    first = await _fetchPreviewChunk(bookId, 0, 0);
  }
  if (!first) return null;
  const text = String(first.content || '').trim();
  if (!text) return null;
  const chapterTitle = String(first.chapter_title || '').trim();
  const kicker = [_bookLabel(book.title), chapterTitle].filter(Boolean).join(' · ');
  return { bookId, kicker, text };
}

function _setReaderPlaceholder(inner, node) {
  inner.textContent = '';
  inner.classList.remove('real-content');
  inner.style.display = '';
  inner.style.textAlign = '';
  inner.style.alignItems = '';
  inner.style.justifyContent = '';
  inner.appendChild(node);
}

function _renderReaderText(inner, kicker, text) {
  inner.textContent = '';
  inner.classList.add('real-content');
  inner.style.display = 'block';
  inner.style.textAlign = 'left';
  inner.style.alignItems = '';
  inner.style.justifyContent = '';
  if (kicker) {
    const label = document.createElement('p');
    label.className = 'preview-book-kicker';
    label.textContent = kicker;
    inner.appendChild(label);
  }
  const body = document.createElement('div');
  body.className = 'preview-book-text';
  body.textContent = text;
  inner.appendChild(body);
}

async function refreshReaderPreview() {
  const inner = document.querySelector('#previewReader .book-page-inner');
  if (!inner) return;
  const seq = ++_previewSeq.reader;
  const requestedId = _previewBookId();
  _setReaderPlaceholder(inner, _previewEmpty('正在读取上次阅读…', ''));
  try {
    const book = await _findPreviewBook(requestedId);
    if (seq !== _previewSeq.reader) return;
    if (!book) {
      _setReaderPlaceholder(inner, _previewEmpty(
        requestedId ? '找不到上次打开的书' : '还没有打开的书',
        requestedId ? '它可能已经被移出书库。' : '在阅读页打开一本书后，这里会显示上次读到的正文。'));
      return;
    }
    const bookId = String(book.id);
    let { chapter, chunk } = _previewPosition(book);
    let first = await _fetchPreviewChunk(bookId, chapter, chunk);
    if (seq !== _previewSeq.reader) return;
    if (!first && (chapter !== 0 || chunk !== 0)) {
      chapter = 0;
      chunk = 0;
      first = await _fetchPreviewChunk(bookId, 0, 0);
      if (seq !== _previewSeq.reader) return;
    }
    if (!first) {
      _setReaderPlaceholder(inner, _previewEmpty(
        '暂时读不到正文', '稍后再打开主题页，或先在阅读页翻一页。'));
      return;
    }
    const pieces = [String(first.content || '').trim()];
    let nextChunk = chunk + 1;
    for (let i = 1; i < PREVIEW_CHUNKS; i++) {
      if (nextChunk >= (Number(first.total_chunks) || 0)) break;
      const more = await _fetchPreviewChunk(bookId, chapter, nextChunk);
      if (seq !== _previewSeq.reader) return;
      if (!more) break;
      const piece = String(more.content || '').trim();
      if (piece) pieces.push(piece);
      nextChunk += 1;
    }
    const text = pieces.filter(Boolean).join('\n\n');
    if (!text) {
      _setReaderPlaceholder(inner, _previewEmpty(
        '这一页没有正文', '在阅读页翻到有内容的章节后再看。'));
      return;
    }
    const chapterTitle = String(first.chapter_title || '').trim();
    const kicker = [_bookLabel(book.title), chapterTitle].filter(Boolean).join(' · ');
    _renderReaderText(inner, kicker, text);
    _previewPassage = { bookId, kicker, text };
    if (_selectionDemoOn) _applySelectionDemo(true);
  } catch (e) {
    if (seq !== _previewSeq.reader) return;
    _setReaderPlaceholder(inner, _previewEmpty(
      '读取失败', '稍后再打开主题页试试。'));
  }
}

function _clipPreviewText(text, max) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  return clean.length > max ? clean.slice(0, max - 1) + '…' : clean;
}

function _readChatPreviewDom() {
  const rows = [];
  document.querySelectorAll('#chatLog > .msg.agent, #chatLog > .msg.user').forEach(node => {
    const bubble = node.querySelector('.bubble');
    const text = bubble ? bubble.textContent.trim() : '';
    if (!text) return;
    rows.push({ role: node.classList.contains('user') ? 'user' : 'agent', text });
  });
  return rows;
}

async function _fetchSessionPreviewRows(sid) {
  const resp = await fetch(S.API + '/api/sessions/' + encodeURIComponent(sid));
  if (!resp.ok) return [];
  const data = await resp.json();
  const rows = [];
  for (const m of (data.messages || [])) {
    const role = m.role === 'assistant' ? 'agent' : m.role;
    if (role !== 'agent' && role !== 'user') continue;
    const text = String(m.content || '')
      .replace(/<think>[\s\S]*?<\/think>/gi, '')
      .trim();
    if (text) rows.push({ role, text });
  }
  return rows;
}

async function _latestPreviewSessionId() {
  const known = _previewSessionId();
  if (known) return known;
  const resp = await fetch(S.API + '/api/sessions/list?limit=1&exclude_mode=writing');
  if (!resp.ok) return '';
  const data = await resp.json();
  const first = (data.sessions || [])[0];
  if (first) {
    _previewLastSession = first.id;
    return first.id;
  }
  return '';
}

function _setChatPreview(log, node) {
  log.textContent = '';
  log.appendChild(node);
}

function _renderChatPreview(log, rows) {
  log.textContent = '';
  rows.slice(-PREVIEW_MESSAGES).forEach(m => {
    const row = document.createElement('div');
    row.className = 'msg ' + m.role;
    if (m.role === 'user' && S.userAvatarUrl) {
      const avatar = document.createElement('div');
      avatar.className = 'user-turn-avatar';
      const img = document.createElement('img');
      img.alt = '';
      img.src = /^(https?:|data:|blob:)/.test(S.userAvatarUrl)
        ? S.userAvatarUrl
        : S.API + S.userAvatarUrl;
      avatar.appendChild(img);
      if (S.userName) avatar.title = S.userName;
      row.appendChild(avatar);
    }
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = _clipPreviewText(m.text, PREVIEW_MESSAGE_CHARS);
    row.appendChild(bubble);
    log.appendChild(row);
  });
}

async function refreshChatPreview() {
  const log = document.getElementById('previewChatLog');
  if (!log) return;
  const seq = ++_previewSeq.chat;
  const domRows = _readChatPreviewDom();
  if (domRows.length) {
    _renderChatPreview(log, domRows);
    return;
  }
  _setChatPreview(log, _previewEmpty('正在读取最近会话…', ''));
  try {
    const sid = await _latestPreviewSessionId();
    if (seq !== _previewSeq.chat) return;
    if (!sid) {
      _setChatPreview(log, _previewEmpty(
        '还没有会话', '在聊天里发一条消息后，这里会显示最近对话。'));
      return;
    }
    const rows = await _fetchSessionPreviewRows(sid);
    if (seq !== _previewSeq.chat) return;
    if (!rows.length) {
      _setChatPreview(log, _previewEmpty(
        '这段会话还没有消息', '在聊天里发一条消息后，这里会显示最近对话。'));
      return;
    }
    _renderChatPreview(log, rows);
  } catch (e) {
    if (seq !== _previewSeq.chat) return;
    _setChatPreview(log, _previewEmpty('读取失败', '稍后再打开主题页试试。'));
  }
}

function _setNotesContext(passage) {
  const stage = document.getElementById('previewNotes');
  const inner = document.getElementById('previewNotesContext');
  if (!stage || !inner) return;
  const text = passage && passage.text ? passage.text : '';
  stage.classList.toggle('has-context', !!text);
  inner.textContent = '';
  if (!text) return;
  if (passage.kicker) {
    const label = document.createElement('p');
    label.className = 'preview-book-kicker';
    label.textContent = passage.kicker;
    inner.appendChild(label);
  }
  const body = document.createElement('div');
  body.className = 'preview-notes-context-text';
  body.textContent = text;
  inner.appendChild(body);
}

function _renderPreviewNotes(host, notes, currentChapter, emptyText) {
  host.textContent = '';
  const panel = document.createElement('div');
  panel.className = 'notes-mini-panel preview-notes-mini';
  const header = document.createElement('div');
  header.className = 'notes-mini-header';
  const title = document.createElement('span');
  title.className = 'notes-mini-title';
  const icon = document.createElement('i');
  icon.setAttribute('data-lucide', 'message-square');
  title.appendChild(icon);
  title.appendChild(document.createTextNode(' 本书笔记'));
  header.appendChild(title);
  panel.appendChild(header);
  const body = document.createElement('div');
  body.className = 'notes-mini-body notes-list';
  if (!notes || !notes.length) {
    const empty = document.createElement('div');
    empty.className = 'notes-empty';
    empty.textContent = emptyText || '本书暂无笔记';
    body.appendChild(empty);
  } else {
    notes.forEach(n => {
      const chapter = n.chapter_index || 0;
      const card = document.createElement('div');
      card.className = 'note-card' + (chapter === currentChapter ? ' current' : '');
      const quote = document.createElement('div');
      quote.className = 'note-quote';
      const selected = String(n.selected_text || '');
      quote.textContent = selected.slice(0, 60) + (selected.length > 60 ? '...' : '');
      card.appendChild(quote);
      const noteText = String(n.note_text || '').trim();
      if (noteText) {
        const text = document.createElement('div');
        text.className = 'note-text';
        text.textContent = noteText;
        card.appendChild(text);
      }
      const meta = document.createElement('div');
      meta.className = 'note-meta';
      meta.textContent = '第' + (chapter + 1) + '章'
        + (chapter === currentChapter ? ' · 当前' : '');
      card.appendChild(meta);
      body.appendChild(card);
    });
  }
  panel.appendChild(body);
  host.appendChild(panel);
  if (window.lucide) window.lucide.createIcons();
}

async function refreshNotesPreview() {
  const host = document.getElementById('previewNotesHost');
  if (!host) return;
  const seq = ++_previewSeq.notes;
  const requestedId = _previewBookId();
  const cached = _previewPassage;
  if (cached && (!requestedId || String(cached.bookId) === String(requestedId))) {
    _setNotesContext(cached);
  } else {
    _setNotesContext(null);
  }
  _renderPreviewNotes(host, [], 0, '加载中...');
  try {
    const book = await _findPreviewBook(requestedId);
    if (seq !== _previewSeq.notes) return;
    if (!book) {
      _setNotesContext(null);
      _renderPreviewNotes(host, [], 0,
        requestedId ? '找不到上次打开的书' : '未打开书籍');
      return;
    }
    const bookId = String(book.id);
    let passage = _previewPassage
      && String(_previewPassage.bookId) === bookId
      ? _previewPassage
      : null;
    if (!passage) {
      passage = await _loadPreviewPassage(book);
      if (seq !== _previewSeq.notes) return;
      if (passage) _previewPassage = passage;
    }
    _setNotesContext(passage);
    const sid = String(book.stable_book_id || '').trim();
    if (!sid) {
      _renderPreviewNotes(host, [], 0, '本书暂无笔记');
      return;
    }
    const qs = 'stable_book_id=' + encodeURIComponent(sid);
    const resp = await fetch(S.API + '/api/annotations?' + qs + '&type=note&limit=200');
    if (!resp.ok) throw new Error('annotations ' + resp.status);
    const data = await resp.json();
    if (seq !== _previewSeq.notes) return;
    let rows = data.annotations || [];
    // 旧进程的 ?type= 分支可能忽略 stable_book_id：看到别的书就退回
    // 按书取再在前端筛，和阅读页浮窗同一套兼容逻辑。
    if (rows.some(n => n.stable_book_id && n.stable_book_id !== sid)) {
      const resp2 = await fetch(S.API + '/api/annotations?' + qs + '&limit=200');
      if (!resp2.ok) throw new Error('annotations ' + resp2.status);
      const data2 = await resp2.json();
      if (seq !== _previewSeq.notes) return;
      rows = data2.annotations || [];
    }
    const notes = rows.filter(
      n => (n.type === 'note' || n.type === 'both') && n.stable_book_id === sid
    );
    notes.sort((a, b) =>
      (a.chapter_index || 0) - (b.chapter_index || 0) ||
      (a.chunk_index || 0) - (b.chunk_index || 0)
    );
    const currentChapter = String(S.currentBook) === String(bookId)
      ? (S.currentChapter || 0)
      : (Number(book.last_chapter) || 0);
    _renderPreviewNotes(host, notes, currentChapter, '本书暂无笔记');
  } catch (e) {
    if (seq !== _previewSeq.notes) return;
    _renderPreviewNotes(host, [], 0, '加载失败');
  }
}

export function resetTheme() {
  localStorage.removeItem('bc_theme');
  applySettings(THEME_DEFAULTS);
  populateThemeForm(THEME_DEFAULTS);
  setThemeSaveState('已恢复默认外观');
}

// Collapsed section bodies stay out of the tab order from the first paint on.
document.querySelectorAll('.theme-section').forEach(s => {
  const body = s.querySelector('.theme-section-body');
  if (body) body.toggleAttribute('inert', !s.classList.contains('expanded'));
});

// ── HTML onclick access ───────────────────────────────────────────
window.onThemeChange = onThemeChange;
window.onThemeInput = onThemeInput;
window.onSelectionPreview = onSelectionPreview;
window.switchPreview = switchPreview;
window.themeSectionKey = themeSectionKey;
window.resetTheme = resetTheme;
window.openSoulEditor = openSoulEditor;
window.closeSoulEditor = closeSoulEditor;
window.saveSoul = saveSoul;
window.revertSoul = revertSoul;
window.resetSoul = resetSoul;
window.reloadAgentSoul = reloadAgentSoul;
window.focusAgentName = focusAgentName;
window.pickAgentAvatar = pickAgentAvatar;
window.removeAgentAvatar = removeAgentAvatar;
window.focusUserName = focusUserName;
window.pickUserAvatar = pickUserAvatar;
window.removeUserAvatar = removeUserAvatar;
