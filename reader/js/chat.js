/* Liubai — chat: bubble UI, SSE v2, typewriter, think collapse. */

import { S } from './state.js';
import { updateUsage } from './context-usage.js';
import { captureViewportText } from './epub-reader.js';

// ── Utilities ──────────────────────────────────────────────────────

/** 聊天区是否跟随最新输出。
 *
 * 不能每帧量距离来决定：「离底部 150px 以内就回底」在流式输出时会把
 * 每一下滚轮都吃掉 —— 内容每帧都在长高，往上滚一格只有约 100px，
 * 永远落在保护圈里，下一帧就被拉回底部（实测上滚 100px 在 400ms 内
 * 归零，整段输出期间一格也滚不上去）。改成记用户意图：往上滚就停跟随，
 * 自己滚回底部才恢复。 */
const CHAT_STICK_SLACK = 8;
let _stickChat = true;
let _followTop = -1;          // 最近一次程序性跟随写下的 scrollTop

function _chatAtBottom(log) {
  return log.scrollHeight - log.scrollTop - log.clientHeight <= CHAT_STICK_SLACK;
}

/** 程序性回到底部。记下写到的位置，好让随之而来的 scroll 事件分得清
 *  是「我们自己滚的」还是「用户拖滚动条」（用户很少恰好停在这一格）。 */
function _writeChatBottom(log) {
  _stickChat = true;
  log.scrollTop = log.scrollHeight;
  _followTop = log.scrollTop;
}

/** 跟随输出；用户往上读历史时不拽回底部。 */
function autoScrollToBottom(log) {
  if (!log || !_stickChat) return;
  _writeChatBottom(log);
}

/** 新消息 / 换会话：无条件回到底部并恢复跟随。 */
export function stickChatToBottom(log) {
  log = log || document.getElementById('chatLog');
  if (log) _writeChatBottom(log);
}

function _bindChatScrollTracking() {
  const log = document.getElementById('chatLog');
  if (!log || log.dataset.stickBound) return;
  log.dataset.stickBound = '1';
  // 往上滚 = 想读历史（滚轮是最常见的一条路）
  log.addEventListener('wheel', function (e) {
    if (e.deltaY < 0) _stickChat = false;
    else if (_chatAtBottom(log)) _stickChat = true;
  }, { passive: true });
  log.addEventListener('touchmove', function () {
    if (!_chatAtBottom(log)) _stickChat = false;
  }, { passive: true });
  log.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowUp' || e.key === 'PageUp' || e.key === 'Home') {
      _stickChat = false;
    }
  });
  // 拖滚动条不产生 wheel：位置既不在底部、也不是我们刚写的那一格，
  // 那就是用户在滚 —— 停止跟随；滚回底部再恢复。
  log.addEventListener('scroll', function () {
    if (_chatAtBottom(log)) { _stickChat = true; return; }
    if (Math.abs(log.scrollTop - _followTop) > 4) _stickChat = false;
  }, { passive: true });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _bindChatScrollTracking);
} else {
  _bindChatScrollTracking();
}

// 写作页专属：agent 输出里的 .md 路径做成可点链接。这里只认语法，点击时再
// 解析真实位置（工作区文档 → 素材卡 → 章节），不依赖渲染瞬间的 S.docs 新鲜度。
const WRITE_FILE_LINK_RE = /(`?)((?:\.\/)?(?:工作区\/)?(?:[A-Za-z0-9_\-一-鿿《》「」·（）]+[\/]){0,6}[A-Za-z0-9_\-一-鿿《》「」·（）]+\.md)(`?)/g;
// 路径前面只允许空白或标点；贴着汉字/字母写的「见设定/…」不当作文件链接。
const LINK_BOUNDARY = ' \t\r\n*（(【["\'“‘：:，,、；;>';

function linkWriteFilePaths(html, links) {
  return html.replace(WRITE_FILE_LINK_RE,
    (match, tickOpen, path, _tickClose, offset, whole) => {
      const prev = offset > 0 ? whole[offset - 1] : '';
      if (prev && !LINK_BOUNDARY.includes(prev)) return match;
      const inner = (tickOpen ? '<code>' : '') + path
        + (tickOpen ? '</code>' : '');
      links.push(`<a class="write-file-link" data-write-path="${path}">`
        + inner + '</a>');
      return '\u0000FL' + (links.length - 1) + '\u0000';
    });
}

export function renderMarkdown(text) {
  let html = text;
  // HTML-escape first so every later rule runs on safe text.
  html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // Fenced code blocks: extract before any other rule so their content
  // stays verbatim (no bold/italic/heading mangling). Restored at the end.
  const codeBlocks = [];
  html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (_m, _lang, code) => {
    codeBlocks.push('<pre><code>' + code.replace(/\n$/, '') + '</code></pre>');
    return '\u0000CB' + (codeBlocks.length - 1) + '\u0000';
  });

  // 只在写作页挂文件链接；代码块已换成占位符，里面的路径不会被误伤。
  const fileLinks = [];
  if (typeof window !== 'undefined' && window._currentTab === 'write') {
    html = linkWriteFilePaths(html, fileLinks);
  }

  html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  html = html.replace(/\*(.+?)\*/g, '<i>$1</i>');
  html = html.replace(/`(.+?)`/g, '<code>$1</code>');
  html = html.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
  html = html.replace(/<\/ul>\s*<ul>/g, '');
  html = html.replace(/^---$/gm, '<hr>');

  // Paragraphs: blank line(s) split <p>. Single newlines inside a
  // paragraph are kept as soft wraps via CSS white-space:pre-wrap on
  // .bubble p, so multi-line plain text (error dumps, stacked lines)
  // doesn't collapse into one glued line under white-space:normal.
  html = html.replace(/\n{2,}/g, '</p><p>');
  html = '<p>' + html + '</p>';

  // Unwrap <p> around block-level elements (they are blocks already).
  html = html.replace(/<p><h([2-4])>/g, '<h$1>');
  html = html.replace(/<\/h([2-4])><\/p>/g, '</h$1>');
  html = html.replace(/<p>(<ul>)/g, '$1');
  html = html.replace(/(<\/ul>)<\/p>/g, '$1');
  html = html.replace(/<p>(<blockquote>)/g, '$1');
  html = html.replace(/(<\/blockquote>)<\/p>/g, '$1');
  html = html.replace(/<p>(<hr>)<\/p>/g, '$1');

  // Restore fenced code blocks (placeholders skipped all <p>/<br> rules).
  html = html.replace(/\u0000CB(\d+)\u0000/g, (_m, i) => codeBlocks[+i]);
  // A code block alone in a <p> needs the <p> dropped - <pre> is
  // block-level and invalid inside <p>.
  html = html.replace(/<p>(<pre>)/g, '$1');
  html = html.replace(/(<\/pre>)<\/p>/g, '$1');

  // 文件链接最后放回：占位符躲过了段落/加粗等规则，插入的 <a> 不再被改写。
  html = html.replace(/\u0000FL(\d+)\u0000/g,
    (_match, i) => fileLinks[+i] || '');

  return html;
}

function nameToHue(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return Math.abs(hash) % 360;
}

function sanitizeFilename(name) {
  return (name || '').replace(/[/\\:]/g, '_').slice(0, 80);
}

function shouldShowTimestamp(prevMs, nowMs) {
  if (!prevMs) return true;
  return (nowMs - prevMs) > 5 * 60 * 1000; // 5 分钟
}

const WEEKDAY_CN = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

/**
 * 微信/抖音风格时间戳格式化。
 * 聊天分隔符语义：标注"这条消息发于何时"，而非纯粹的相对时间。
 */
function formatChatTime(tsMs, nowMs) {
  const d = new Date(tsMs);
  const now = new Date(nowMs);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const hm = `${hh}:${mm}`;

  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((today - that) / 86400000);

  if (diffDays === 0) {
    const diffSec = Math.floor((nowMs - tsMs) / 1000);
    if (diffSec < 60) return '刚刚';
    if (diffSec < 3600) return Math.floor(diffSec / 60) + ' 分钟前';
    return hm;
  }
  if (diffDays === 1) return '昨天 ' + hm;
  if (diffDays < 7) return WEEKDAY_CN[d.getDay()] + ' ' + hm;
  if (d.getFullYear() === now.getFullYear())
    return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hm;
  return d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hm;
}

function createTimestampSep(timeMs) {
  const div = document.createElement('div');
  div.className = 'timestamp-sep';
  div.dataset.ts = String(timeMs);   // for periodic refresh (see refreshTimestampSeps)
  div.textContent = '── ' + formatChatTime(timeMs, Date.now()) + ' ──';
  return div;
}

// 相对标签（"刚刚"/"N 分钟前"）渲染后不会自己变老——定时重格式化保持准确。
// 1 小时后转为绝对时间（"HH:MM"）便不再变化，届时 textContent 比较相等即跳过。
setInterval(() => {
  document.querySelectorAll('.timestamp-sep[data-ts]').forEach(div => {
    const text = '── ' + formatChatTime(Number(div.dataset.ts), Date.now()) + ' ──';
    if (div.textContent !== text) div.textContent = text;
  });
}, 30000);

// ── DOM builders ───────────────────────────────────────────────────

function createAvatar(characterName, bookId) {
  const div = document.createElement('div');
  div.className = 'msg-avatar';
  const hue = nameToHue(characterName || '?');
  div.style.background = `hsl(${hue}, 55%, 45%)`;
  div.textContent = (characterName || '?')[0];

  if (characterName && bookId) {
    const img = document.createElement('img');
    img.src = `${S.API}/avatars/${bookId}/${sanitizeFilename(characterName)}.png`;
    img.onload = () => { div.textContent = ''; div.appendChild(img); };
    img.onerror = () => img.remove(); // keep default
  }
  return div;
}

/* 用户上传的 agent 头像：只在普通对话里出现，占「思考过程」上方一行。
   扮演模式已有角色头像（.msg-avatar），两者不叠。 */
function createAgentTurnAvatar() {
  const wrap = document.createElement('div');
  wrap.className = 'agent-turn-avatar';
  const img = document.createElement('img');
  img.alt = '';
  img.src = S.API + S.agentAvatarUrl;
  wrap.appendChild(img);
  return wrap;
}

function wantsAgentAvatar(role) {
  // Role-play is home-page-only and already renders the character avatar in
  // the bubble; a stale S.currentMode left from role-play must not hide the
  // agent avatar on the writing tab.
  const inRoleplay = S.currentMode === 'roleplay' && window._currentTab === 'home';
  return role === 'agent' && !!S.agentAvatarUrl && !inRoleplay;
}

/* 用户自己的头像：放在用户消息行内（不是 agent 那种独立一行）。
   .msg.user 是 row-reverse，所以第一个子节点会落在最右侧。 */
function createUserTurnAvatar() {
  const wrap = document.createElement('div');
  wrap.className = 'user-turn-avatar';
  const img = document.createElement('img');
  img.alt = '';
  const url = S.userAvatarUrl || '';
  img.src = /^(https?:|data:|blob:)/.test(url) ? url : S.API + url;
  wrap.appendChild(img);
  if (S.userName) wrap.title = S.userName;
  return wrap;
}

/** 头像资料可能在历史消息之后才到；这里补画/更新/移除已渲染的用户行。 */
export function syncUserAvatars(url) {
  const log = document.getElementById('chatLog');
  if (!log) return;
  if (url) S.userAvatarUrl = url;
  const src = url
    ? (/^(https?:|data:|blob:)/.test(url) ? url : S.API + url)
    : '';
  log.querySelectorAll('#chatLog > .msg.user').forEach(msg => {
    // 分叉编辑行是输入框，不是一条已发送的消息。
    if (msg.classList.contains('inline-editor-msg')) return;
    const existing = msg.querySelector('.user-turn-avatar');
    if (!url) {
      if (existing) existing.remove();
      return;
    }
    if (existing) {
      const img = existing.querySelector('img');
      if (img) img.src = src;
      if (S.userName) existing.title = S.userName;
      return;
    }
    const avatar = createUserTurnAvatar();
    msg.insertBefore(avatar, msg.firstElementChild || null);
  });
}

/** Bring the avatar on/off switch to turns already rendered.
 *
 * Session restore and /api/agent are two independent fetches; a restored
 * history can paint before the avatar URL is known. Rather than delaying the
 * conversation on the profile request, theme.js broadcasts and this walks
 * each agent turn — above its think rows / writing trace — inserting or
 * removing the avatar as needed.
 */
export function syncAgentAvatars(url) {
  const log = document.getElementById('chatLog');
  if (!log) return;
  if (url) S.agentAvatarUrl = url;
  const src = url ? S.API + url : '';
  log.querySelectorAll('.agent-turn-avatar').forEach(el => {
    if (!url) { el.remove(); return; }
    const img = el.querySelector('img');
    if (img) img.src = src;
  });
  if (!url) return;
  log.querySelectorAll('.msg.agent').forEach(msg => {
    // Role-play bubbles carry the character avatar inside .msg-avatar.
    if (msg.querySelector('.msg-avatar')) return;
    let first = msg;
    let prev = first.previousElementSibling;
    // addChat may have dropped this turn's timestamp *after* the think rows.
    // Skip it, then walk the turn, then lift the timestamp back above.
    const stamp = (prev && prev.classList && prev.classList.contains('timestamp-sep'))
      ? prev : null;
    if (stamp) prev = prev.previousElementSibling;
    while (prev && prev.classList && (
      prev.classList.contains('think-row')
      || prev.classList.contains('turn-head')
      || prev.classList.contains('turn-trace'))) {
      first = prev;
      prev = prev.previousElementSibling;
    }
    if (prev && prev.classList.contains('agent-turn-avatar')) {
      const img = prev.querySelector('img');
      if (img) img.src = src;
      return;
    }
    if (stamp) log.insertBefore(stamp, first);
    log.insertBefore(createAgentTurnAvatar(), first);
  });
}

/**
 * Render an expandable thinking row.
 *
 * @param {string} thinkText   reasoning text (may be partial while streaming)
 * @param {number|null} durationSec  seconds spent thinking; null/undefined
 *        for historical rows where the duration was not recorded
 * @param {boolean} [live]     true while the model is still thinking — the
 *        label shows a "思考中" state instead of a finished duration
 *
 * The returned node carries two streaming handles so the SSE handler can
 * update the row in place (rebuilding the node mid-stream would collapse
 * the row and lose the user's expand state):
 *   _setThinkText(text)  — replace body content
 *   _finishThink(sec)    — flip the label from live to finished
 */
export function createThinkRow(thinkText, durationSec, live) {
  const row = document.createElement('div');
  row.className = 'think-row';

  const toggle = document.createElement('button');
  toggle.className = 'think-toggle';

  function setMeta(sec, isLive) {
    toggle.classList.toggle('live', !!isLive);
    if (isLive) {
      toggle.textContent = '思考中…';
    } else if (sec !== undefined && sec !== null) {
      toggle.textContent = `思考过程 (${sec}s)`;
    } else {
      toggle.textContent = '思考过程';
    }
  }
  setMeta(durationSec, live);

  const body = document.createElement('div');
  body.className = 'think-body';
  body.innerHTML = thinkText ? renderMarkdown(thinkText) : '';

  toggle.onclick = function () {
    body.classList.toggle('open');
  };

  row.appendChild(toggle);
  row.appendChild(body);

  // Reasoning now arrives one token at a time (hundreds of `think` events
  // per turn). Re-running markdown over the whole block per token would
  // dominate the main thread and undo the point of streaming, so paints are
  // coalesced to at most one per animation frame — the text still grows
  // visibly in real time, just without a full re-parse per token.
  let _thinkDirty = false;
  let _thinkRaf = null;
  let _thinkLatest = thinkText || '';

  function paintThink() {
    _thinkRaf = null;
    if (!_thinkDirty) return;
    _thinkDirty = false;
    body.innerHTML = renderMarkdown(_thinkLatest || '');
  }

  row._setThinkText = function (text) {
    _thinkLatest = text || '';
    _thinkDirty = true;
    if (_thinkRaf === null) _thinkRaf = requestAnimationFrame(paintThink);
  };
  row._finishThink = function (sec) {
    // Flush synchronously so the final reasoning text is never left waiting
    // on a frame that may never come once the row is being torn down.
    if (_thinkRaf !== null) {
      cancelAnimationFrame(_thinkRaf);
      _thinkRaf = null;
    }
    _thinkDirty = false;
    body.innerHTML = renderMarkdown(_thinkLatest || '');
    setMeta(sec, false);
  };
  return row;
}

/* ── 写作进度 ──────────────────────────────────────────────────────
 * 写一整章要好几分钟，期间没有任何可见反馈。正文本身不进聊天窗（不贴
 * 整章是既有纪律），所以只挂一行「到哪一步 · 多少字」；工具一结束就撤。
 */
function setWriteProgress(agentDiv, state, event) {
  const stage = event.stage || '';
  const chars = Number(event.chars) || 0;
  const label = stage === 'settling' ? '✍ 正在结算账本'
    : stage === 'revising' ? '✍ 正在修订'
      : stage === 'retrying' ? '✍ 输出被截断，加大预算重写'
        : stage === 'assembled' ? '✍ 正在装配上下文'
          : '✍ 正在写正文';

  // 时间线模式：进度就写在那条工具的右侧状态里，不再单起一行。
  if (state && state._turnTrace) {
    const rec = _runningToolRow(state, event);
    if (rec) {
      // 工具行左边已经写着「写正文：第3章」，这里只补阶段与字数。
      const brief = stage === 'settling' ? '结算中'
        : stage === 'revising' ? '修订中'
          : stage === 'retrying' ? '重写中'
            : stage === 'assembled' ? '装配中' : '起草中';
      rec.status.textContent = chars
        ? `${brief} · ${chars.toLocaleString()} 字` : brief;
      return;
    }
  }

  let row = state._writeProgress;
  if (!row || !row.parentNode) {
    row = document.createElement('div');
    row.className = 'write-progress';
    agentDiv.parentNode.insertBefore(row, agentDiv);
    state._writeProgress = row;
  }
  row.textContent = chars ? `${label} · ${chars.toLocaleString()} 字` : label;
}

function clearWriteProgress(state) {
  const row = state._writeProgress;
  if (row && row.parentNode) row.parentNode.removeChild(row);
  state._writeProgress = null;
}

/* ── 回合时间线（写作页）─────────────────────────────────────────────
 * 一轮写作是几十次 ReAct：读设定、查资料、写正文、结算。聊天窗里只吐最终
 * 正文时，中间那段「活着吗」的空白长得让人以为卡死。这里把它摊成一条可
 * 折叠的时间线：
 *
 *   L0  `.turn-head`   已完成 · 27分1秒        ← 默认只留这一行
 *   L1  `.turn-trace`  思考行 / 旁白 / 工具行   ← 点开才看
 *   L2  `.tool-step-body`                       ← 工具行再点开看输出
 *
 * 只给写作页开（state._turnTrace 只在 writingCtx 下建），其他页面维持原样。
 * 结束时不自动收：作者手动动过就说明正在看。
 */

const TOOL_ICONS = {
  read: 'file-text', write: 'pencil-line', search: 'search', run: 'wrench',
};

function _fmtDuration(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}小时${m}分`;
  if (m) return `${m}分${s}秒`;
  return `${s}秒`;
}

/** 单个工具的耗时：秒级以下也给一位小数，别让快工具全是「0秒」。 */
function _fmtToolDuration(ms) {
  const sec = ms / 1000;
  if (sec < 10) return `${sec.toFixed(1)}秒`;
  if (sec < 60) return `${Math.round(sec)}秒`;
  return _fmtDuration(ms);
}

/* 展开/收起的高度动画。这几块的高度是内容撑出来的（工具行多少不定），
 * CSS 没法 transition 到 auto；用 WAAPI 把当次 scrollHeight 定格成 px 再
 * 过渡，动画结束把高度还给内容本身（不写内联样式）。
 *   commit(open) —— 真正改状态（hidden 或 .open 类），动画只负责视觉。
 * 快速连点时先取消上一段：取消不触发 onfinish，所以不会出现「关到一半
 * 被强制隐藏」的鬼影。 */
const _REVEAL_ANIM = { duration: 190, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' };

function _reducedMotion() {
  return !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

function animateReveal(el, open, commit) {
  if (!el || typeof el.animate !== 'function' || _reducedMotion()) {
    commit(open);
    return;
  }
  const prev = el._revealAnim;
  if (prev) {
    el._revealAnim = null;
    prev.onfinish = null;
    prev.cancel();
  }
  if (open) {
    commit(true);
    const h = el.scrollHeight;
    if (!h) return;
    el._revealAnim = el.animate(
      [{ height: '0px', opacity: 0 }, { height: `${h}px`, opacity: 1 }],
      _REVEAL_ANIM);
    return;
  }
  const h = el.offsetHeight;
  if (!h) { commit(false); return; }
  const anim = el.animate(
    [{ height: `${h}px`, opacity: 1 }, { height: '0px', opacity: 0 }],
    _REVEAL_ANIM);
  el._revealAnim = anim;
  anim.onfinish = () => {
    el._revealAnim = null;
    commit(false);
  };
}

/** L0：`已完成 · 27分1秒  ⌄` 那一行。 */
function makeTurnHead(labelText) {
  const head = document.createElement('div');
  head.className = 'turn-head';
  const btn = document.createElement('button');
  btn.className = 'turn-head-btn';
  btn.type = 'button';
  btn.setAttribute('aria-expanded', 'true');
  const label = document.createElement('span');
  label.className = 'turn-head-label';
  label.textContent = labelText || '';
  const chevron = document.createElement('i');
  chevron.setAttribute('data-lucide', 'chevron-down');
  chevron.className = 'turn-head-chevron';
  btn.appendChild(label);
  btn.appendChild(chevron);
  head.appendChild(btn);
  return { head, btn, label };
}

/** L1 的一条工具行（连 L2 的输出块一起建好，内容后填）。 */
function makeToolRow(labelText, kind, titleText) {
  const row = document.createElement('div');
  row.className = 'tool-step running';

  const head = document.createElement('button');
  head.className = 'tool-step-head';
  head.type = 'button';
  const icon = document.createElement('i');
  icon.setAttribute('data-lucide', TOOL_ICONS[kind] || 'wrench');
  const label = document.createElement('span');
  label.className = 'tool-step-label';
  // 标签由后端白名单生成（动作 + 对象），这里只当文本用，绝不 insertHTML。
  label.textContent = labelText || '工具';
  label.title = titleText || labelText || '';
  const status = document.createElement('span');
  status.className = 'tool-step-status';
  head.appendChild(icon);
  head.appendChild(label);
  head.appendChild(status);

  const body = document.createElement('div');
  body.className = 'tool-step-body';
  // L2：先给一行人话摘要，原始 JSON 收在 <details> 里——展开工具行不该先
  // 看到一串花括号。
  const summary = document.createElement('div');
  summary.className = 'tool-step-summary';
  const raw = document.createElement('details');
  raw.className = 'tool-step-raw';
  const rawToggle = document.createElement('summary');
  rawToggle.textContent = '原始输出';
  const pre = document.createElement('pre');
  pre.className = 'tool-step-output';
  raw.appendChild(rawToggle);
  raw.appendChild(pre);
  body.appendChild(summary);
  body.appendChild(raw);

  row.appendChild(head);
  row.appendChild(body);
  return { row, head, body, summary, pre, status, label };
}

/** 建时间线的空壳（回合开始就建，有内容才露头）。 */
function initTurnTrace(log, agentDiv, state) {
  const { head, btn, label } = makeTurnHead('进行中…');
  head.hidden = true;

  const trace = document.createElement('div');
  trace.className = 'turn-trace';
  trace.hidden = true;

  log.insertBefore(head, agentDiv);
  log.insertBefore(trace, agentDiv);

  state._turnHead = head;
  state._turnHeadLabel = label;
  state._turnTrace = trace;
  state._turnLog = log;
  state._turnStart = Date.now();
  state._toolRows = new Map();
  state._traceTouched = false;
  state._turnOutcome = '';
  state._clockTimer = null;

  // 时间线里任何一次点击都算「在看」：结束时不再自动收起。
  trace.addEventListener('click', () => { state._traceTouched = true; });
  btn.onclick = () => {
    state._traceTouched = true;
    const open = !head.classList.contains('open');
    head.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    animateReveal(trace, open, (next) => { trace.hidden = !next; });
  };
}

/** 时间线第一次有内容：露头 + 开始计时。 */
function ensureTurnTrace(state) {
  if (!state || !state._turnTrace) return null;
  if (state._turnHead.hidden) {
    state._turnHead.hidden = false;
    state._turnTrace.hidden = false;
    state._turnHead.classList.add('running', 'open');
    state._clockTimer = setInterval(() => {
      if (state._turnOutcome || state._epoch !== S.chatEpoch) return;
      state._turnHeadLabel.textContent =
        `进行中 · ${_fmtDuration(Date.now() - state._turnStart)}`;
    }, 1000);
  }
  return state._turnTrace;
}

/** 这一轮的旁白文本挪进时间线，气泡归零准备下一轮。 */
function freezeNarration(state, agentDiv) {
  if (!state || !state._turnTrace) return;
  const text = state._fullText || '';
  if (!text.trim()) return;
  const trace = ensureTurnTrace(state);
  const div = document.createElement('div');
  div.className = 'turn-narration';
  div.innerHTML = renderMarkdown(text);
  trace.appendChild(div);
  stopTypewriter(state);
  state._fullText = '';
  state._twIndex = 0;
  state._twDirty = false;
  setBubbleHTML(agentDiv, '');
}

function addToolStep(state, event) {
  const trace = ensureTurnTrace(state);
  if (!trace) return;
  const built = makeToolRow(event.label || event.name || '工具', event.kind,
                            event.label || event.name || '');
  built.row.classList.add('running');
  built.status.textContent = '运行中';
  if (event.id) built.row.dataset.toolId = event.id;
  built.head.onclick = () => {
    state._traceTouched = true;
    const open = !built.body.classList.contains('open');
    animateReveal(built.body, open,
                  (next) => built.body.classList.toggle('open', next));
  };
  trace.appendChild(built.row);

  const rec = {
    row: built.row, body: built.body, pre: built.pre, summary: built.summary,
    status: built.status, name: event.name, started: Date.now(),
  };
  if (event.id) state._toolRows.set(event.id, rec);
  else state._toolRows.set('name:' + event.name, rec);
  autoScrollToBottom(state._turnLog);
}

/** 找一条还在跑的记录：优先 id，老服务端没有 id 时按名字找最后一条。 */
function _runningToolRow(state, event) {
  if (!state || !state._toolRows) return null;
  if (event.id && state._toolRows.has(event.id)) return state._toolRows.get(event.id);
  let found = null;
  state._toolRows.forEach((rec, key) => {
    if (rec.name === event.name && rec.row.classList.contains('running')) found = rec;
  });
  return found;
}

function finishToolStep(state, event) {
  const rec = _runningToolRow(state, event);
  if (!rec) return;
  rec.row.classList.remove('running');
  if (event.ok === false) rec.row.classList.add('failed');
  rec.status.textContent = event.ok === false
    ? '失败' : _fmtToolDuration(Date.now() - rec.started);
  if (event.summary) rec.summary.textContent = event.summary;
  if (event.preview) {
    rec.pre.textContent = event.preview;
    rec.row.classList.add('has-output');
  }
  if (!event.summary && !event.preview) {
    rec.summary.textContent = '没有输出';
    rec.pre.textContent = '';
  }
  state._toolRows.delete(event.id || ('name:' + event.name));
  autoScrollToBottom(state._turnLog);
}

/** 回合收尾：定格耗时；没被手动展开过就收起。 */
function finishTurnTrace(state, outcome) {
  if (!state || !state._turnHead || state._turnHead.hidden) return;
  if (state._clockTimer) {
    clearInterval(state._clockTimer);
    state._clockTimer = null;
  }
  if (!state._turnOutcome) state._turnOutcome = outcome || 'done';
  const dur = _fmtDuration(Date.now() - state._turnStart);
  const head = state._turnHead;
  head.classList.remove('running');
  head.classList.add('done');
  if (state._turnOutcome === 'interrupted') {
    state._turnHeadLabel.textContent = `已中断 · ${dur}`;
  } else if (state._turnOutcome === 'round_limit') {
    state._turnHeadLabel.textContent = `已达轮次上限 · ${dur}`;
  } else if (state._turnOutcome === 'error') {
    state._turnHeadLabel.textContent = `出错了 · ${dur}`;
  } else {
    state._turnHeadLabel.textContent = `已完成 · ${dur}`;
  }
  if (!state._traceTouched) {
    head.classList.remove('open');
    const btn = head.querySelector('.turn-head-btn');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    // 到点了就慢慢收起，而不是啪一下消失。
    animateReveal(state._turnTrace, false,
                  (next) => { state._turnTrace.hidden = !next; });
  }
  // 还在跑的工具行不应留着「运行中」（中断/异常收尾）。
  if (state._toolRows) {
    state._toolRows.forEach((rec) => {
      if (rec.row.classList.contains('running')) {
        rec.row.classList.remove('running');
        rec.status.textContent = '未完成';
      }
    });
    state._toolRows.clear();
  }
}

/** 历史回放：把 sessions 接口返回的 steps 画成同一条时间线（默认收起）。
 *
 *  写作会话里 `sessions.py` 会把一轮 ReAct 的「思考 / 旁白 / 工具」按顺序
 *  存成 steps；这里用和实时完全相同的样式重建，刷新前后看到的是同一条线。
 */
export function renderTurnTraceFromSteps(steps) {
  const { head, btn, label } = makeTurnHead('');
  const trace = document.createElement('div');
  trace.className = 'turn-trace';
  trace.hidden = true;

  let tools = 0;
  (steps || []).forEach((step) => {
    if (!step) return;
    if (step.type === 'think') {
      if (step.text) trace.appendChild(createThinkRow(step.text, null, false));
    } else if (step.type === 'text') {
      if (!step.text) return;
      const div = document.createElement('div');
      div.className = 'turn-narration';
      div.innerHTML = renderMarkdown(step.text);
      trace.appendChild(div);
    } else if (step.type === 'tool') {
      tools += 1;
      const built = makeToolRow(step.label || step.name, step.kind, step.label);
      built.row.classList.remove('running');
      if (step.ok === false) built.row.classList.add('failed');
      built.status.textContent = step.ok === false ? '失败' : '';
      if (step.summary) built.summary.textContent = step.summary;
      if (step.output) {
        built.pre.textContent = step.output;
        built.row.classList.add('has-output');
      }
      built.head.onclick = () => built.body.classList.toggle('open');
      trace.appendChild(built.row);
    }
  });

  // 历史里没存耗时，用步数代替（比空着有用）。
  label.textContent = tools ? `已完成 · ${tools} 步` : '已完成';
  head.classList.add('done');
  btn.setAttribute('aria-expanded', 'false');
  let reopened = false;
  btn.onclick = () => {
    reopened = !reopened;
    head.classList.toggle('open', reopened);
    btn.setAttribute('aria-expanded', reopened ? 'true' : 'false');
    animateReveal(trace, reopened, (next) => { trace.hidden = !next; });
  };
  return { head, trace };
}

/** 发送前就被否决的动作（如引用失效）：一行提示，不落历史。 */
function noticeToLog(text) {
  const log = document.getElementById('chatLog');
  if (!log) return;
  const row = document.createElement('div');
  row.className = 'chat-notice';
  row.textContent = text;
  log.appendChild(row);
  autoScrollToBottom(log);
}

/** 会话级提示（撞轮次上限等）：一次性、不落历史。 */
function appendChatNotice(agentDiv, text) {
  const row = document.createElement('div');
  row.className = 'chat-notice';
  row.textContent = text;
  if (agentDiv.parentNode) {
    agentDiv.parentNode.insertBefore(row, agentDiv.nextSibling);
  }
}

/** Stamp the rendered row index on a live message.
 *
 * loadSession re-numbers every row at the end, but messages appended while
 * chatting never went through it — so their fork action read
 * `dataset.msgIndex == undefined`, which JSON-serialises to null and turns
 * "重新输入" into "copy the whole session". Keep the index current at the
 * single point where rows are appended.
 */
function stampMsgIndex(el) {
  if (!el) return;
  el.dataset.msgIndex = String(
    document.querySelectorAll('#chatLog > .msg').length - 1);
}

/** Adopt the persisted user-message id returned with the chat response.
 *
 * Forking needs the DB id (`up_to_message_id`). A live bubble has no id
 * until the page is reloaded; with this, "重新回复" works in the same
 * session it was sent in.
 */
function stampUserMessageId(resp, userDiv, state) {
  const uid = resp.headers.get('X-User-Message-Id');
  if (!uid || state._epoch !== S.chatEpoch) return;
  if (userDiv) {
    userDiv.dataset.msgId = uid;
    return;
  }
  // Voice input: the row is a voice bubble, not the text Div we created.
  const rows = document.querySelectorAll('#chatLog > .msg.user');
  const last = rows[rows.length - 1];
  if (last) last.dataset.msgId = uid;
}

function createBubble(role, html, avatarEl) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;

  if (role === 'agent' && avatarEl) {
    div.appendChild(avatarEl);
  } else if (role === 'user' && S.userAvatarUrl) {
    div.appendChild(createUserTurnAvatar());
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = html;
  div.appendChild(bubble);

  // Bottom action bar (hover-revealed): 重新输入 on user, 重新回复 on agent.
  // Both trigger a fork with exclusive=true so the new session ends right
  // before this user turn and the frontend re-sends the (edited) content.
  const actions = document.createElement('div');
  actions.className = 'msg-actions';
  const btn = document.createElement('button');
  btn.className = 'msg-action-btn';
  if (role === 'user') {
    btn.innerHTML = '<i data-lucide="pencil"></i>';
    btn.title = '重新输入:修改这条输入,从处分叉新分支';
    btn.onclick = function (e) {
      e.stopPropagation();
      forkEdit(div);
    };
  } else {
    // "重新回复" is a reload action, not an edit — use the circular arrow
    // so it reads differently from the pencil on the user's own turn.
    btn.innerHTML = '<i data-lucide="rotate-cw"></i>';
    btn.title = `重新回复:让${S.agentName || '留白'}重新回复,从处分叉新分支`;
    btn.onclick = function (e) {
      e.stopPropagation();
      forkRegenerate(div);
    };
  }
  actions.appendChild(btn);

  // Copy-to-clipboard button (same for both roles).
  const copyBtn = document.createElement('button');
  copyBtn.className = 'msg-action-btn';
  copyBtn.innerHTML = '<i data-lucide="copy"></i>';
  copyBtn.title = '复制文本';
  copyBtn.onclick = function (e) {
    e.stopPropagation();
    var text = bubble.textContent || '';
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        copyBtn.innerHTML = '<i data-lucide="check"></i>';
        setTimeout(function () { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; }, 1000);
      }).catch(function () {});
    } else {
      // Fallback for older browsers
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); copyBtn.innerHTML = '<i data-lucide="check"></i>'; setTimeout(function () { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; }, 1000); } catch (_) {}
      ta.remove();
    }
  };
  actions.appendChild(copyBtn);
  div.appendChild(actions);

  return div;
}

/**
 * Write into an agent bubble and keep the row's visibility in sync.
 *
 * The agent row is created *before* the request is sent so the typewriter has
 * a target the moment text starts arriving. While the model is still reasoning
 * there is nothing to show, yet the row still paints: `.bubble` contributes
 * its padding, border and 92%-wide flex box, and the hover action bar reserves
 * a line even at `opacity: 0` — a conspicuous empty shell parked right under
 * the thinking row. `is-pending` hides the whole row until real content lands,
 * and every write routes through here so the two states cannot drift apart.
 *
 * The element stays in the DOM (hidden, not removed) so `#chatLog > .msg`
 * indexing in sessions.js — used by fork and the branch switcher — is
 * unaffected.
 */
/** 非 2xx = 服务端在发出第一条 SSE 之前就崩了（未捕获异常返回 JSON）。
 *  不检查就会顺着「读 body、找不到 data: 行」一路走到流的尽头 ——
 *  界面上一句话都没有，看起来像请求根本没发出去。 */
async function reportHttpError(resp, agentDiv, state) {
  let msg = '请求失败：HTTP ' + resp.status;
  try {
    const err = await resp.json();
    if (err && err.error) msg = err.error;
  } catch (_) { /* 不是 JSON，就报状态码 */ }
  if (msg.length > 1200) msg = msg.slice(0, 1200) + '…';
  // The request may have outlived its view (branch/session switch while the
  // JSON body was being read). Never write it into the new chatHistory.
  if (state && state._epoch !== S.chatEpoch) return;
  setBubbleHTML(agentDiv, renderMarkdown(msg));
  S.chatHistory.push({ role: 'agent', content: msg });
}

function setBubbleHTML(agentDiv, html) {
  const bubble = agentDiv.querySelector('.bubble');
  if (!bubble) return;
  bubble.innerHTML = html;
  // renderMarkdown('') yields a bare "<p></p>" — no text, but still no content.
  const hasContent = !!(bubble.textContent.trim()
    || bubble.querySelector('pre, ul, blockquote, hr, img'));
  agentDiv.classList.toggle('is-pending', !hasContent);
}

// ── Chat display (bubble-style) ────────────────────────────────────

export function addChat(role, text, extraClass, timeMs) {
  const log = document.getElementById('chatLog');

  if (extraClass) {
    const old = log.querySelector('.msg.' + extraClass);
    if (old) old.remove();
  }

  const now = timeMs || Date.now();
  if (shouldShowTimestamp(S.chatLastTimestamp, now)) {
    log.appendChild(createTimestampSep(now));
  }
  S.chatLastTimestamp = now;

  const avatarEl = (role === 'agent' && S.currentMode === 'roleplay' && S.currentCharacterName)
    ? createAvatar(S.currentCharacterName, S.rpSelectedBookId || S.currentBook)
    : null;

  const html = role === 'agent' ? renderMarkdown(text) : '';
  const div = createBubble(role, html, avatarEl);
  if (role === 'user') {
    div.querySelector('.bubble').textContent = text;
  } else {
    // Keep replayed agent turns consistent with the live path: an empty
    // historical reply must not render as a blank bubble either.
    setBubbleHTML(div, html);
  }

  if (extraClass) div.classList.add(extraClass);
  if (wantsAgentAvatar(role)) {
    // History replay appends think rows *before* the text bubble, and
    // addChat may just have appended this turn's timestamp separator after
    // them. Walk back over the think rows so the avatar sits above the
    // thinking process; if a separator is in the way, lift it above the
    // avatar so the order reads timestamp → avatar → thinking → answer.
    const tail = log.lastElementChild;
    const stamp = tail && tail.classList && tail.classList.contains('timestamp-sep')
      ? tail : null;
    let anchor = null;
    let prev = stamp ? stamp.previousElementSibling : tail;
    while (prev && prev.classList && prev.classList.contains('think-row')) {
      anchor = prev;
      prev = prev.previousElementSibling;
    }
    const avatar = createAgentTurnAvatar();
    if (anchor) {
      if (stamp) log.insertBefore(stamp, anchor);
      log.insertBefore(avatar, anchor);
    } else {
      log.appendChild(avatar);
    }
  }
  log.appendChild(div);
  stampMsgIndex(div);
  autoScrollToBottom(log);
  return div;
}

// ── Fork: 重新输入 / 重新回复 ────────────────────────────────
//
// Both flows fork with exclusive=true so the new session copies messages
// strictly BEFORE the triggering user turn. The frontend then re-sends the
// user content via the normal /api/chat path:
//   - 重新输入: show an inline editor pre-filled with the original text;
//     the user edits and sends manually.
//   - 重新回复: auto-send the original user text immediately.
//
// fork_point_index is computed on the client (message position in the loaded
// array) and stored on the child row so the branch switcher can render at the
// right spot in either branch.

function _forkAndJump(msgId, forkPointIndex, content, opts) {
  opts = opts || {};
  const sid = S.currentSessionId;
  if (!sid) { alert('当前无会话，无法分叉'); return; }
  const body = {
    up_to_message_id: msgId,
    exclusive: true,
    fork_point_index: forkPointIndex,
  };
  fetch(S.API + '/api/sessions/' + sid + '/fork', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(function (r) { return r.json(); }).then(function (data) {
    if (data.error) { alert('分叉失败: ' + data.error); return; }
    // Stash pending action for the post-load hook (sessions.js loadSession).
    if (opts.edit) {
      S._pendingForkEdit = { content: content };
    } else if (opts.regenerate) {
      S._pendingForkRegenerate = { content: content };
    }
    if (window.loadSession) window.loadSession(data.id);
  }).catch(function (e) { alert('分叉失败: ' + e.message); });
}

function forkEdit(msgDiv) {
  const msgId = msgDiv.dataset.msgId || null;
  const idx = parseInt(msgDiv.dataset.msgIndex, 10);
  const bubble = msgDiv.querySelector('.bubble');
  const content = bubble ? bubble.textContent : '';
  _forkAndJump(msgId, idx, content, { edit: true });
}

function forkRegenerate(msgDiv) {
  // The agent bubble's fork point is its own index; the up_to_message_id
  // must be the preceding user message (exclusive copy stops before it).
  const agentIdx = parseInt(msgDiv.dataset.msgIndex, 10);
  const log = document.getElementById('chatLog');
  // Walk backwards from msgDiv to find the previous .msg.user
  let prev = msgDiv.previousElementSibling;
  let userId = null;
  let userContent = '';
  while (prev) {
    if (prev.classList && prev.classList.contains('msg') && prev.classList.contains('user')) {
      userId = prev.dataset.msgId || null;
      const b = prev.querySelector('.bubble');
      userContent = b ? b.textContent : '';
      break;
    }
    prev = prev.previousElementSibling;
  }
  if (!userId) { alert('找不到对应的用户消息'); return; }
  _forkAndJump(userId, agentIdx, userContent, { regenerate: true });
}

// Called by sessions.js after loadSession finishes. Handles the pending
// fork action: 重新输入 shows an inline editor, 重新回复 auto-sends.
export function handlePendingForkAction() {
  if (S._pendingForkEdit) {
    const { content } = S._pendingForkEdit;
    S._pendingForkEdit = null;
    showInlineEditor(content);
  } else if (S._pendingForkRegenerate) {
    const { content } = S._pendingForkRegenerate;
    S._pendingForkRegenerate = null;
    // Defer to next tick so loadSession's DOM work completes first.
    setTimeout(function () { window.sendMessage(content); }, 0);
  }
}

function showInlineEditor(prefill) {
  const log = document.getElementById('chatLog');
  const wrap = document.createElement('div');
  wrap.className = 'msg user inline-editor-msg';
  const ta = document.createElement('textarea');
  ta.className = 'inline-editor';
  ta.value = prefill || '';
  ta.rows = 3;
  const btn = document.createElement('button');
  btn.className = 'inline-editor-send';
  btn.textContent = '发送';
  const cancel = document.createElement('button');
  cancel.className = 'inline-editor-cancel';
  cancel.textContent = '取消';
  btn.onclick = function () {
    const v = ta.value.trim();
    if (!v) return;
    wrap.remove();
    window.sendMessage(v);
  };
  cancel.onclick = function () { wrap.remove(); };
  ta.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      btn.click();
    }
  });
  wrap.appendChild(ta);
  const bar = document.createElement('div');
  bar.className = 'inline-editor-bar';
  bar.appendChild(btn);
  bar.appendChild(cancel);
  wrap.appendChild(bar);
  log.appendChild(wrap);
  autoScrollToBottom(log);
  ta.focus();
}

// ── Typewriter engine ──────────────────────────────────────────────

let _twRafId = null;

function startTypewriter(bubble, state) {
  if (state._twActive) return;
  state._twActive = true;
  state._twIndex = 0;
  state._twLastFrame = 0;
  // First visible text has arrived — reveal the pending row.
  const row = bubble.closest('.msg');
  if (row) row.classList.remove('is-pending');

  const speedMs = S.currentMode === 'roleplay' ? 30 : 10;
  const PUNCT_PAUSE = 150;

  async function tick(now) {
    if (!state._twActive) return;
    // The visible conversation was replaced under us (branch switch / new
    // session). Stop silently — writing the old branch's text (or its voice
    // bubbles) into the new transcript is exactly the mismatch to avoid.
    if (state._epoch !== S.chatEpoch) {
      state._twActive = false;
      return;
    }

    if (now - state._twLastFrame < speedMs) {
      _twRafId = requestAnimationFrame(tick);
      return;
    }
    state._twLastFrame = now;

    const backlog = state._fullText.length - state._twIndex;
    // 模型输出是突发的：落后超过 80 字就按比例追赶（约半秒内追平），
    // 正常节奏仍是逐字。否则渲染会永远欠着模型一大段尾巴，停止时
    // 一重绘就像「先前被限速的内容突然全倒出来」。
    const step = backlog > 80
      ? Math.min(backlog, Math.ceil(backlog / 30))
      : 1;
    const ch = state._fullText[state._twIndex];
    if (S.currentMode === 'roleplay' && ch && /[。！？…—]/.test(ch)) {
      state._twLastFrame += PUNCT_PAUSE;
    }

    state._twIndex = Math.min(
      state._fullText.length, state._twIndex + step);
    bubble.innerHTML = renderMarkdown(state._fullText.slice(0, state._twIndex))
      + '<span class="typewriter-cursor">|</span>';
    autoScrollToBottom(document.getElementById('chatLog'));

    if (state._twIndex < state._fullText.length || !state._streamDone) {
      _twRafId = requestAnimationFrame(tick);
    } else {
      // Done
      const cursor = bubble.querySelector('.typewriter-cursor');
      if (cursor) cursor.remove();
      state._twActive = false;
      pushAgentHistory(state);

      // ── Render TTS voice bubbles from tts_speak tool ──────────
      if (state._voiceUrls && state._voiceUrls.length > 0) {
        for (const url of state._voiceUrls) {
          const audio = new Audio(url);
          await new Promise(r => { audio.onloadedmetadata = r; audio.onerror = r; setTimeout(r, 3000); });
          const dur = isFinite(audio.duration) ? Math.round(audio.duration) : 0;
          createVoiceBubble('agent', url, dur || 1);
        }
      }

      // ── Auto-TTS fallback (if agent didn't call tts_speak) ─────
      if (S.currentMode === 'roleplay' && S.currentCharacterName && state._fullText
          && (!state._voiceUrls || state._voiceUrls.length === 0)) {
        const voice = await getVoiceForCharacter(S.currentCharacterName);
        const ttsResult = await requestTTS(state._fullText.slice(0, 500), voice);
        if (ttsResult && ttsResult.url) {
          const ttsAudio = new Audio(ttsResult.url);
          await new Promise(r => { ttsAudio.onloadedmetadata = r; ttsAudio.onerror = r; setTimeout(r, 3000); });
          const dur = isFinite(ttsAudio.duration) ? Math.round(ttsAudio.duration) : 0;
          createVoiceBubble('agent', ttsResult.url, dur || 1);
        }
      }
    }
  }

  _twRafId = requestAnimationFrame(tick);
}

function stopTypewriter(state) {
  if (_twRafId) cancelAnimationFrame(_twRafId);
  state._twActive = false;
}

// ── SSE v2 event handler ───────────────────────────────────────────

function handleSSEEvent(event, state, agentDiv) {
  // Late frames from a stream whose view was replaced: drop them entirely
  // (including `done`'s usage update, which would corrupt the new session's
  // context ring).
  if (state._epoch !== S.chatEpoch) return;
  switch (event.type) {
    case 'think_start':
      state._thinkStartTime = Date.now();
      state._thinkBuf = '';
      // Create the row immediately so the reasoning is inspectable while it
      // is still streaming — previously the row only appeared on think_end,
      // so nothing was expandable until the whole answer had finished.
      if (!state._thinkRow) {
        const row = createThinkRow('', null, true);
        // 写作页的思考行是时间线的一步；其他页面仍是气泡上方的一行。
        const trace = state._turnTrace ? ensureTurnTrace(state) : null;
        if (trace) trace.appendChild(row);
        else agentDiv.parentNode.insertBefore(row, agentDiv);
        state._thinkRow = row;
      }
      break;

    case 'think':
      state._thinkBuf += event.content;
      if (state._thinkRow) {
        state._thinkRow._setThinkText(state._thinkBuf);
        autoScrollToBottom(document.getElementById('chatLog'));
      }
      break;

    case 'think_end': {
      const duration = ((Date.now() - (state._thinkStartTime || Date.now())) / 1000).toFixed(1);
      if (state._thinkRow) {
        // Keep the row even if blank (it was already shown as "思考中").
        state._thinkRow._setThinkText(state._thinkBuf);
        state._thinkRow._finishThink(duration);
      } else if (state._thinkBuf.trim()) {
        const thinkRow = createThinkRow(state._thinkBuf, duration, false);
        const trace = state._turnTrace ? ensureTurnTrace(state) : null;
        if (trace) trace.appendChild(thinkRow);
        else agentDiv.parentNode.insertBefore(thinkRow, agentDiv);
      }
      state._thinkBuf = '';
      state._thinkRow = null;
      break;
    }

    case 'text':
      state._fullText += event.content;
      // 已经按了停止：不再重启打字机（屏幕立刻停住）；在途的尾巴仍会
      // 收进 _fullText，最终定格时与服务端落盘的内容一致。
      if (!state._stopping && !state._twActive) {
        startTypewriter(agentDiv.querySelector('.bubble'), state);
      }
      break;

    case 'tool_start':
      // 写作页：先把这一轮的旁白收进时间线，再挂上即将执行的工具。
      // 其他页面没有时间线，保持静默（工具名不外露）。
      if (state._turnTrace) {
        freezeNarration(state, agentDiv);
        addToolStep(state, event);
      }
      break;

    case 'tool_progress':
      // 写作长工具的进度（只报阶段与字数，正文不上屏）。
      setWriteProgress(agentDiv, state, event);
      break;

    case 'round_limit':
      state._turnOutcome = 'round_limit';
      clearWriteProgress(state);
      appendChatNotice(agentDiv,
        `已达 ${event.rounds} 轮上限，这一轮可能还没做完——说「继续」我就接着做。`);
      break;

    case 'tool_end':
      finishToolStep(state, event);
      clearWriteProgress(state);
      // 模型记下称呼 / 性别 / 自己的名字后，设置页立刻跟上；提示词下一轮
      // 由 load_persona 现算，不会继续注入「初次见面」引导。
      if (event.name === 'update_identity') {
        document.dispatchEvent(new CustomEvent('bc:identity-changed'));
      }
      // 工作区文件的 git 状态要跟着工具走：助手刚写完设定，文件区就先亮
      // U/M，不必等整轮结束（refresh 那一刻会再对齐一次）。
      if (window._currentTab === 'write' && window.WritingUI
          && typeof window.WritingUI.refreshStatus === 'function'
          && (event.name === 'writing_docs'
              || event.name === 'writing_memory'
              || event.name === 'writing_git_change')) {
        window.WritingUI.refreshStatus();
      }
      if (event.name === 'tts_speak' && event.preview) {
        try {
          const result = JSON.parse(event.preview);
          if (result.type === 'voice' && result.url) {
            if (!state._voiceUrls) state._voiceUrls = [];
            state._voiceUrls.push(result.url);
          }
        } catch (_) { /* ignore parse errors */ }
      }
      break;

    case 'interrupted':
      // 服务端已停住这一轮：冻结打字机、把已收到的正文定格，尾部标记
      // 「已中断」。后面的 done 仍会到达，markInterrupted 是幂等的。
      state._streamDone = true;
      clearWriteProgress(state);
      markInterrupted(agentDiv, state);
      break;

    case 'error':
      stopTypewriter(state);
      finalizeThinkRow(state);
      clearWriteProgress(state);
      setBubbleHTML(agentDiv,
        renderMarkdown(state._fullText || event.message || '发生错误'));
      S.chatHistory.push({ role: 'agent', content: state._fullText || event.message });
      state._historyPushed = true;
      finishTurnTrace(state, 'error');
      break;

    case 'done':
      state._streamDone = true;
      clearWriteProgress(state);
      // 兜底：interrupted 帧没读到（连接被我们 aborted，或取消到得太晚
      // 服务端已正常收尾）时，本地的停止意图也要把界面收成「已中断」。
      if (event.interrupted || state._stopping) {
        markInterrupted(agentDiv, state);
      } else {
        finishTurnTrace(state, 'done');
      }
      // Guard against a stream that ends mid-reasoning (no think_end frame):
      // otherwise the row would stay labelled "思考中…" forever.
      finalizeThinkRow(state);
      if (event.usage) updateUsage(event.usage);
      // Same rule as the header: adopt whichever session the server used
      // (it may have replaced a cross-page/mode id), unless the view moved
      // on — in that case keep the id on the line that sent it.
      if (event.session_id) {
        if (!state || state._epoch === S.chatEpoch) {
          if (window.adoptSession) {
            window.adoptSession(event.session_id, {
              tab: state && state._sendTab,
              bookId: state && state._sendBook,
              workId: state && state._sendWork,
            });
          } else {
            S.currentSessionId = event.session_id;
          }
          window.saveViewState && window.saveViewState();
        } else if (window.recordTabSession && state && state._sendTab) {
          window.recordTabSession(state._sendTab, event.session_id,
            { bookId: state._sendBook, workId: state._sendWork });
        }
      }
      break;
  }
}

/** Close out an in-flight thinking row (used on done/error). */
function finalizeThinkRow(state) {
  if (!state._thinkRow) return;
  const sec = ((Date.now() - (state._thinkStartTime || Date.now())) / 1000).toFixed(1);
  state._thinkRow._setThinkText(state._thinkBuf);
  state._thinkRow._finishThink(sec);
  state._thinkRow = null;
  state._thinkBuf = '';
}

// ── User-initiated stop ───────────────────────────────────────────
// 一条流 = 一个 turn_id。停止按钮先通知服务端（可精确停到当前回合，
// 工具和后续 ReAct 轮次都不再跑），2.5s 内没等到终态再断连接兜底。

function newTurnId() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
  } catch (_) { /* insecure context */ }
  return 'turn-' + Date.now().toString(36) + '-' +
    Math.random().toString(36).slice(2);
}

/** 重载会话时也用它：中断注记不是正文，只画一枚轻标记。 */
export function createInterruptNote() {
  const div = document.createElement('div');
  div.className = 'interrupt-note';
  div.textContent = '已中断';
  return div;
}

function setSendButtonBusy(busy) {
  const btn = document.getElementById('sendBtn');
  if (!btn) return;
  btn.classList.toggle('stop', !!busy);
  btn.textContent = busy ? '停止' : '发送';
  btn.title = busy ? '停止生成' : '';
}

/** 每条 agent 回复只进一次 chatHistory（打字机、中断、中止三条路径共用）。 */
function pushAgentHistory(state) {
  if (!state || state._historyPushed || state._epoch !== S.chatEpoch) return;
  state._historyPushed = true;
  if (!state._fullText) return;
  S.chatHistory.push({ role: 'agent', content: state._fullText });
  if (S.chatHistory.length > 30) S.chatHistory = S.chatHistory.slice(-20);
}

/** 停止时只认「按下那一刻已经渲染出来的前缀」。
 *
 * ``_fullText`` 里通常还压着服务端已经吐完、打字机没来得及画的尾巴；
 * 中断时把整段重绘会让人以为被限速的内容突然全倒了出来。
 */
function renderedPrefix(state) {
  if (!state) return '';
  if (state._stopping && state._stopIndex >= 0) {
    return state._fullText.slice(0, state._stopIndex);
  }
  return state._fullText;
}

function markInterrupted(agentDiv, state) {
  if (!state || state._epoch !== S.chatEpoch) return;
  stopTypewriter(state);
  finalizeThinkRow(state);
  const shown = renderedPrefix(state);
  if (shown) {
    state._twIndex = shown.length;
    setBubbleHTML(agentDiv, renderMarkdown(shown));
  } else {
    // 按下时一个字都还没画出来：保持空占位隐藏，绝不回填积压正文。
    state._twIndex = 0;
    setBubbleHTML(agentDiv, '');
  }
  pushAgentHistory(state);
  finishTurnTrace(state, 'interrupted');
  if (state._interruptedShown) return;
  state._interruptedShown = true;
  if (agentDiv && agentDiv.parentNode) {
    agentDiv.parentNode.insertBefore(createInterruptNote(), agentDiv.nextSibling);
  }
}

/** 停止当前流：先冻结界面，再让服务端停；最终落盘由服务端负责。 */
export function stopAgent() {
  const active = S._activeStream;
  if (!active || active.state._stopping) return;
  active.state._stopping = true;
  // 定格点 = 按下这一刻已经画出来的字数；之后收到的在途文本只进
  // _fullText（服务端落盘用），不再上屏。
  if (active.state._stopIndex < 0) {
    // v1 回退路径没有打字机，文本是一到就整段渲染的 —— 全量算已渲染。
    active.state._stopIndex = active.state._v1Fallback
      ? active.state._fullText.length
      : (active.state._twIndex || 0);
  }

  // 立刻冻结并补标记：服务端可能正卡在一个同步工具里，不能等它回话。
  // 后续到达的在途文本只进 _fullText（服务端落盘用），不再上屏。
  markInterrupted(active.agentDiv, active.state);

  if (active.turnId) {
    fetch(S.API + '/api/chat/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ turn_id: active.turnId }),
      keepalive: true,
    }).catch(function () {
      // 旧服务端没有这个接口 → 下面 2.5s 的 abort 仍然能停住。
    });
  }

  active.fallbackTimer = setTimeout(function () {
    try { active.abortCtrl.abort(); } catch (_) { /* already closed */ }
  }, 2500);
}

export function sendOrStop() {
  if (S._activeStream) stopAgent();
  else sendMessage();
}

// ── Send message (SSE streaming) ──────────────────────────────────

export async function sendMessage(overrideText) {
  const input = document.getElementById('chatInput');
  const isOverride = overrideText != null;
  const text = isOverride ? String(overrideText).trim() : input.value.trim();
  // 只挂了素材、没打字也算一次发送：附件本身就是这一轮的内容。
  const attaches = attachedMaterials.slice();
  if (!text && !attaches.length) return;
  // 一轮只允许一条流：流式期间按钮已经变成「停止」。
  if (S._activeStream) return;

  // 写作页：带上当前作品/章，回合结束后刷新工作台（工具可能建了书/章/正文）。
  const writingCtx = (window._currentTab === 'write' && window.WritingUI
    && typeof window.WritingUI.context === 'function')
    ? window.WritingUI.context() : null;
  const onReader = window._currentTab === 'reader';
  const isRoleplay = S.currentMode === 'roleplay';

  // 这一轮带哪段引用。出处必须和当前页面一致——切页不会清掉标签，
  // 阅读页圈的书里原文不能跟着进写作页，反之亦然。
  // Quote-injection only for manual input from the chat box; fork/regenerate
  // paths supply their own text and never carry a selection ref.
  let quoteText = '';
  let quoteSrc = '';
  if (!isOverride) {
    const tagEl = document.getElementById('selectionTag');
    const src = S.selectionSource;
    // 写作页的引用有三种：正文选区、对话引用、工作区文件/文件夹；
    // 阅读页只接受书里选区，别把写作页的附件带过去。
    const allowed = writingCtx
      ? (src === 'writing' || src === 'chat' || src === 'workspace')
      : (!isRoleplay && onReader && src !== 'writing' && src !== 'workspace');
    if (tagEl && tagEl.classList.contains('visible') && allowed
        && S.lastSelection) {
      if (writingCtx && src === 'writing') {
        // 圈完又改过正文：锚点可能已经不在本章里。带一段找不到的文字，
        // 模型只会在正文里瞎搜——宁可当场丢掉并说一声。
        const ed = document.getElementById('writeEditor');
        if (ed && ed.value.includes(S.lastSelection)) {
          quoteText = S.lastSelection;
          quoteSrc = src;
        } else {
          clearSelectionRef();
          noticeToLog('引用的那段已经不在正文里了，这次没带引用');
        }
      } else {
        quoteText = S.lastSelection;
        quoteSrc = src;
      }
    }
  }

  let fullText = text;
  if (attaches.length) {
    fullText = materialAttachBlock(attaches) + (text ? `\n\n${text}` : '');
    // 附件随这条消息送走，托盘立刻清空（与输入框同生命周期）。
    clearAttachments();
  }
  if (quoteText) {
    const srcEl = document.getElementById('selTagSrc');
    fullText = `[引用自${srcEl.textContent}: "${quoteText.slice(0, 200)}"]\n${fullText}`;
  }

  // User bubble
  const now = Date.now();
  const log = document.getElementById('chatLog');
  if (shouldShowTimestamp(S.chatLastTimestamp, now)) {
    log.appendChild(createTimestampSep(now));
  }
  S.chatLastTimestamp = now;
  // Manual user bubble (not via addChat — we want the bubble immediately)
  const userDiv = createBubble('user', '', null);
  const bubbleEl = userDiv.querySelector('.bubble');
  bubbleEl.textContent = text;
  if (attaches.length) {
    // 气泡里留一行附件名：托盘已经清了，回看时仍能知道这一轮带了什么。
    const line = document.createElement('div');
    line.className = 'bubble-attach';
    attaches.forEach((m) => {
      const chip = document.createElement('span');
      chip.className = 'bubble-attach-chip';
      chip.textContent = attachLabel(m.title || m.id);
      chip.title = m.title || m.id || '';
      line.appendChild(chip);
    });
    bubbleEl.prepend(line);
  }
  log.appendChild(userDiv);
  stampMsgIndex(userDiv);
  stickChatToBottom(log);
  S.chatHistory.push({ role: 'user', content: text });
  // 会话已经开始：撤掉模式预选，把这次会话的阅读模式固定下来。
  if (window.updateModePrelude) window.updateModePrelude();
  if (!isOverride) input.value = '';

  // 手改优先：先落盘再做 agent 的事，否则回来刷新会把未保存的改动吞掉。
  if (writingCtx && typeof window.WritingUI.flush === 'function') {
    try { await window.WritingUI.flush(); } catch (_) { /* 保存失败不拦对话 */ }
  }

  // Agent bubble placeholder — created now so the typewriter has a target as
  // soon as text arrives, but collapsed while empty so the reasoning phase
  // doesn't show a blank bubble under the thinking row.
  const avatarEl = (S.currentMode === 'roleplay' && S.currentCharacterName)
    ? createAvatar(S.currentCharacterName, S.rpSelectedBookId || S.currentBook)
    : null;
  const agentDiv = createBubble('agent', '', avatarEl);
  setBubbleHTML(agentDiv, '');
  log.appendChild(agentDiv);
  stampMsgIndex(agentDiv);
  if (wantsAgentAvatar('agent')) {
    // Insert first: think_start inserts its row immediately before agentDiv,
    // so the avatar stays above the thinking row.
    log.insertBefore(createAgentTurnAvatar(), agentDiv);
  }
  stickChatToBottom(log);

  // A fork loads its child before this turn is re-sent, so the row the
  // branch switcher anchors to only exists now. Re-render it (no-op when
  // the switcher is already there / the session has no branches).
  if (S.currentSessionId && window.renderBranchSwitchers) {
    window.renderBranchSwitchers(S.currentSessionId);
  }

  // Per-message streaming state (stored as properties on a plain object)
  const state = {
    _fullText: '',
    _thinkBuf: '',
    _thinkRow: null,
    _writeProgress: null,
    _thinkStartTime: 0,
    _twActive: false,
    _twIndex: 0,
    _twLastFrame: 0,
    _streamDone: false,
    _v1Fallback: false,
    // Which conversation this stream belongs to. See S.chatEpoch.
    _epoch: S.chatEpoch,
    // Stop freeze point: characters already painted when the user hit
    // stop. -1 = not stopped; never backfill the buffered tail.
    _stopIndex: -1,
  };

  const abortCtrl = new AbortController();
  S._chatAbort = abortCtrl;
  const turnId = newTurnId();
  S._activeStream = {
    turnId: turnId,
    abortCtrl: abortCtrl,
    state: state,
    agentDiv: agentDiv,
    fallbackTimer: null,
  };
  setSendButtonBusy(true);

  // 三个界面各用各的提示词与上下文：主页只带 home 语境，阅读页才带书、
  // 进度、选区。写作页的现场走 writing_work_id / writing_chapter_index。
  const page = writingCtx ? 'writing' : (onReader ? 'reader' : 'home');
  // 回复落在哪条会话线：中途切页也不能把新会话记到别的线上。
  const sendTab = window.getActiveChatTab ? window.getActiveChatTab() : 'home';
  const sendBook = S.currentBook || '';
  // 写作会话按作品归档：记这一轮实际发出去的作品，别在落盘时现读界面
  //（中途换作品会把旧对话记到新作品名下）。
  const sendWork = writingCtx ? (writingCtx.workId || '') : null;
  state._sendTab = sendTab;
  state._sendBook = sendBook;
  state._sendWork = sendWork;

  // 写作页：整轮摊成可折叠的时间线。其他页面保持原样（思考行 + 气泡）。
  if (writingCtx) initTurnTrace(log, agentDiv, state);

  try {
    const resp = await fetch(S.API + '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: abortCtrl.signal,
      body: JSON.stringify({
        message: (function () {
          // Inject voice emotion as context in roleplay mode
          if (S.currentMode === 'roleplay' && S._lastVoiceEmotion) {
            const emotion = S._lastVoiceEmotion;
            S._lastVoiceEmotion = null;  // consume once
            return `[读者说话时的情绪: ${emotion}]\n${fullText}`;
          }
          return fullText;
        })(),
        // 写作页的对话走写作模式：同一套 ReAct 循环，换一套提示词与工具。
        mode: writingCtx ? 'writing' : S.currentMode,
        page: page,
        ...(writingCtx ? {
          writing_work_id: writingCtx.workId || '',
          writing_chapter_index: writingCtx.index || 0,
          // 只在空白会话里定模式；已有会话的模式存在服务端，之后只能
          // 由 agent 经作者授权切换（决策 161）。
          ...(S.currentSessionId ? {} : {
            writing_mode: S.writingMode || 'standard',
          }),
        } : {}),
        stream: 'v2',
        // /api/chat/cancel 用同一个 id 精确停住这一轮
        turn_id: turnId,
        character_name: S.currentCharacterName || undefined,
        role_kind: S.currentRoleKind || 'character',
        role_ref: S.currentRoleRef || undefined,
        // 剧透门只在扮演模式有意义；主页/写作页不发。
        max_chapter: (isRoleplay && !writingCtx) ? S.currentMaxChapter : undefined,
        // 阅读进度只喂 reader-notes 召回通道（与 max_chapter 的剧透门无关）
        reading_chapter: (!writingCtx && onReader) ? S.currentChapter : undefined,
        book_id: writingCtx ? '' : (isRoleplay
          ? (S.rpSelectedBookId || S.currentBook || '')
          : (onReader ? (S.currentBook || '') : '')),
        ...(!writingCtx && !isRoleplay && onReader && S.currentBook ? {
          position: { chapter: S.currentChapter, chunk: S.currentChunk },
          // Chapters visible in the viewport (from /api/sync). The gate must
          // never sit below what is already on screen.
          visible_chapters: (S.visibleChapters && S.visibleChapters.length)
            ? S.visibleChapters : undefined,
          // 视口内实际渲染的文字（跨 section 拼接）。后端优先把它注入
          // <current-passage>，而不是一块可能盖不住视窗的 chunk。
          reading_viewport: (function () {
            try { return captureViewportText() || undefined; }
            catch (_) { return undefined; }
          })(),
        } : {}),
        // 引用：阅读页是「书中原文 / 对话」，写作页是「正文 / 对话」。
        selection: quoteText,
        selection_source: quoteSrc,
        // Structural coordinates of the selection (see state.js). The
        // backend uses them to anchor <selection-context> and to raise the
        // spoiler gate — a selected passage is text the reader has seen.
        ...(quoteSrc === 'book' && quoteText ? {
          selection_section: S.selectionSection,
          selection_chapter: S.selectionChapter,
          selection_chunk: S.selectionChunk,
          selection_cfi: S.selectionCfi || '',
          selection_anchor_text: S.selectionAnchorText || '',
        } : {}),
        history: (function () {
          const h = S.chatHistory;
          if (h.length && h[h.length - 1].role === 'user') {
            return h.slice(0, -1).slice(-6);
          }
          return h.slice(-6);
        })(),
        session_id: S.currentSessionId
      })
    });

    if (!resp.ok) { await reportHttpError(resp, agentDiv, state); return; }

    // Fork needs the persisted message id even before a page reload.
    stampUserMessageId(resp, typeof userDiv === 'undefined' ? null : userDiv, state);

    const sid = resp.headers.get('X-Session-Id');
    // Adopt the session the server actually persisted to. It differs from
    // the local id when the server had to create a new one (no id, or a
    // page/mode mismatch — session lines never mix). The epoch check keeps
    // a branch/tab switch made mid-stream from being overwritten; in that
    // case the id still belongs to the line that sent it.
    if (sid) {
      if (state._epoch === S.chatEpoch) {
        if (window.adoptSession) window.adoptSession(sid, { tab: sendTab, bookId: sendBook, workId: state._sendWork });
        else S.currentSessionId = sid;
        window.saveViewState && window.saveViewState();
      } else if (window.recordTabSession) {
        window.recordTabSession(sendTab, sid, { bookId: sendBook, workId: state._sendWork });
      }
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let sseBuffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      sseBuffer += decoder.decode(value, { stream: true });

      const lines = sseBuffer.split('\n');
      sseBuffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6);
        if (payload === '[DONE]') continue;

        try {
          const event = JSON.parse(payload);
          handleSSEEvent(event, state, agentDiv);
        } catch (_) {
          // Plain-text frame: the server is on the v1 contract. Reasoning
          // cannot be recovered here (it arrives inline as tags that v1
          // never separated), so say so instead of silently rendering a
          // reply with no visible thinking at all.
          if (!state._v1Fallback) {
            state._v1Fallback = true;
            console.warn('[chat] server sent a plain-text SSE frame; ' +
              'falling back to v1 rendering (reasoning unavailable). ' +
              'This usually means the server process is running stale code.');
          }
          state._fullText += payload;
          let filtered = state._fullText.replace(/<think>[\s\S]*?<\/think>/g, '');
          filtered = filtered.replace(/<think>[\s\S]*$/g, '');
          setBubbleHTML(agentDiv, renderMarkdown(filtered));
          autoScrollToBottom(log);
        }
      }
    }

    // Final flush
    if (!state._v1Fallback && state._fullText) {
      // Wait for typewriter to finish (max 30s)
      const startWait = Date.now();
      while (state._twActive && (Date.now() - startWait < 30000)) {
        await new Promise(r => setTimeout(r, 50));
      }
      if (state._twActive) {
        stopTypewriter(state);
        // Flush remaining text immediately
        state._twIndex = state._fullText.length;
        if (state._epoch === S.chatEpoch) {
          setBubbleHTML(agentDiv, renderMarkdown(state._fullText));
          pushAgentHistory(state);
        }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      // 两种中止共用一条路径：用户按停止（_stopping，补中断标记）与
      // 会话/分支切换（静默收尾）。后者不改当前视图。
      stopTypewriter(state);
      finalizeThinkRow(state);
      if (state._stopping) {
        markInterrupted(agentDiv, state);
      } else {
        setBubbleHTML(agentDiv, renderMarkdown(state._fullText));
      }
      finishTurnTrace(state, 'interrupted');
      return;
    }
    stopTypewriter(state);
    setBubbleHTML(agentDiv, renderMarkdown('请求失败: ' + e.message));
    finishTurnTrace(state, 'error');
  } finally {
    if (S._activeStream && S._activeStream.abortCtrl === abortCtrl) {
      clearTimeout(S._activeStream.fallbackTimer);
      S._activeStream = null;
      setSendButtonBusy(false);
    }
    if (S._chatAbort === abortCtrl) S._chatAbort = null;
    // 兜底收尾：连接正常结束却没等到 done（服务端异常早退）时，计时器
    // 和「运行中」状态不能一直挂着。已收尾的回合重复调用是无害的。
    finishTurnTrace(state, state._turnOutcome || 'done');
    // 工具可能建了作品/章节、写了正文——回合结束后把工作台对齐到磁盘。
    if (writingCtx && window.WritingUI
        && typeof window.WritingUI.refresh === 'function') {
      window.WritingUI.refresh();
    }
  }
}

// ── Avatar upload ─────────────────────────────────────────────────

export function uploadAvatar() {
  if (!S.currentCharacterName || !S.rpSelectedBookId) return;
  const input = document.getElementById('avatarFileInput');
  if (!input) return;

  input.onchange = async function () {
    const file = input.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    try {
      const resp = await fetch(
        `${S.API}/api/graph/${S.rpSelectedBookId}/characters/${encodeURIComponent(S.currentCharacterName)}/avatar`,
        { method: 'POST', body: formData }
      );
      if (resp.ok) {
        // Refresh avatars on all visible agent messages
        const avatars = document.querySelectorAll('.msg.agent .msg-avatar');
        avatars.forEach(a => {
          const img = a.querySelector('img');
          if (img) {
            img.src = `${S.API}/avatars/${S.rpSelectedBookId}/${sanitizeFilename(S.currentCharacterName)}.png?_=${Date.now()}`;
          }
        });
      }
    } catch (_) { /* ignore */ }
    input.value = '';
  };
  input.click();
}

// ── Selection handling ────────────────────────────────────────────

// 阅读页的引用高亮：浏览器一收缩 DOM 选区（点聊天框、点别处）高亮就没了，
// 所以引用成立后用 CSS Custom Highlight 单独标一段——它跟选区互不依赖。
// 老浏览器没有这个 API 时静默退化成「只有引用条」。
let _quoteHighlight = null;

function setQuoteHighlight(range) {
  try {
    if (!range || typeof Highlight !== 'function'
        || typeof CSS === 'undefined' || !CSS.highlights) return;
    _quoteHighlight = new Highlight(range);
    CSS.highlights.set('reader-quote', _quoteHighlight);
  } catch (_) { _quoteHighlight = null; }
}

function clearQuoteHighlight() {
  try {
    if (typeof Highlight === 'function' && typeof CSS !== 'undefined'
        && CSS.highlights) {
      CSS.highlights.delete('reader-quote');
    }
  } catch (_) { /* 没有这个 API 就没有高亮要撤 */ }
  _quoteHighlight = null;
}

/** 选区必须整段落在同一个消息气泡里，才谈得上「引用这段对话」。
 *
 * #chatLog 下除了消息气泡，还挂开场招呼语（.msg.agent 直接放文字，
 * 没有 .bubble）、时间戳、会话提示条。它们都不是一轮对话的正文，
 * 误触一句「这么晚还在……」就被当成引文带进下一轮，纯属事故。
 * 起止两端必须在同一个 .bubble 内 —— 跨气泡拖选不是一条引用。
 */
function _selectionBubble(sel) {
  if (!sel || !sel.rangeCount) return null;
  const owner = (node) => {
    if (!node) return null;
    const el = node.nodeType === 1 ? node : node.parentElement;
    return el ? el.closest('.bubble') : null;
  };
  const start = owner(sel.anchorNode);
  const end = owner(sel.focusNode);
  return (start && start === end) ? start : null;
}

// 「×」撤销引用时，一摘 .visible 标签就从布局里消失，mouseup 往往落在
// 它下面的元素上；而 mousedown 的 preventDefault 又刻意留着那段选区。
// mouseup 钩子若不认这一下，就会按同一段选区把刚撤掉的引用原地装回去
// —— 表现为「选中文字时点 × 没反应」。用一次性的指针标记跳过那一下；
// 下一次不落在 #selectionTag 上的 mousedown 会清掉标记，免得吞掉新选区。
let _clearQuotePointer = false;

document.addEventListener('mousedown', function (e) {
  const target = e.target;
  if (target && target.closest && target.closest('#selectionTag')) return;
  _clearQuotePointer = false;
});

document.addEventListener('mouseup', function (e) {
  if (_clearQuotePointer) {
    _clearQuotePointer = false;
    return;
  }
  setTimeout(function () {
    const sel = window.getSelection();
    const text = sel.toString().trim();
    if (!text || text.length < 2) {
      if (e.target.closest('.ann-popup') || e.target.closest('.toolbar-note-box')) return;
      if (typeof window.popupInteracting !== 'undefined' && window.popupInteracting) return;
      if (typeof window.hideAnnotationPopup === 'function') window.hideAnnotationPopup();
      // 在书页里点一下（选区塌陷）= 取消这次引用；点聊天面板不动。
      const pageEl = document.getElementById('bookPage');
      if (pageEl && e.target && pageEl.contains(e.target)) clearBookSelection();
      return;
    }

    const pageEl = document.getElementById('bookPage');
    const chatEl = document.getElementById('chatLog');
    if (pageEl.contains(sel.anchorNode)) {
      S.selectionSource = 'book';
      S.lastSelection = text;
      S.lastRange = sel.rangeCount > 0 ? sel.getRangeAt(0).cloneRange() : null;
      if (S.renderMode !== 'native') {
        // Chunk view: the visible chunk is the selected chunk. Native
        // selections are stamped by annotations.js with the epub section.
        S.selectionSection = null;
        S.selectionCfi = '';
        S.selectionChapter = S.currentChapter;
        S.selectionChunk = S.currentChunk;
        S.selectionAnchorText = text;
      }
      // 选中即引用：引用条 + 常驻高亮。以前这里只把选区存进 S，界面上
      // 什么都不显示（弹引用按钮的 popup 早已不存在），用户只能靠猜。
      showSelectionRef(text, '书中');
      setQuoteHighlight(S.lastRange);
      if (typeof window.popupInteracting === 'undefined' || !window.popupInteracting) {
        if (typeof window.showAnnotationPopup === 'function') window.showAnnotationPopup(sel, text);
      }
    } else if (chatEl.contains(sel.anchorNode)) {
      // 只引用气泡里的文字；招呼语、时间戳、提示条一律不算。
      if (!_selectionBubble(sel)) return;
      S.selectionSource = 'chat';
      S.lastSelection = text;
      S.selectionSection = null;
      S.selectionCfi = '';
      S.selectionChapter = null;
      S.selectionChunk = null;
      S.selectionAnchorText = '';
      clearQuoteHighlight();
      showSelectionRef(text, '聊天');
    }
  }, 10);
});

export function showSelectionRef(text, source) {
  const tag = document.getElementById('selectionTag');
  document.getElementById('selTagSrc').textContent = source;
  document.getElementById('selTagText').textContent = text.slice(0, 80);
  tag.classList.add('visible');
}

/**
 * 写作页：正文编辑器里圈中的一段 → 引用条（source=正文）。
 *
 * 编辑器是 <textarea>，它的选区不在 window.getSelection() 里（Chrome
 * 只给一个塌缩的 body 选区），只能由 writing.js 读 selectionStart/End
 * 再调这里。**不自动落盘**：发送前 sendMessage 会先 flush，并在那时
 * 复核这段文字还在不在正文里——圈完又改过的不带出去。
 */
export function setWritingSelection(text) {
  const t = String(text || '').trim();
  if (t.length < 2) return;
  S.selectionSource = 'writing';
  S.lastSelection = t;
  S.lastRange = null;
  S.selectionSection = null;
  S.selectionCfi = '';
  S.selectionChapter = null;
  S.selectionChunk = null;
  S.selectionAnchorText = '';
  showSelectionRef(t, '正文');
}

/**
 * 写作页：把工作区文件 / 文件夹「添加到聊天」。
 *
 * 与选正文不同，这里引用的是**路径**而不是正文：正文可能很长，也不该在
 * 发送时被复制一份。agent 拿到的是路径，动它之前自己用 writing_docs 读。
 * 文件夹以 `/` 结尾区分，提示词里会让模型先 list 再 read。
 */
export function setWorkspaceSelection(path, kind) {
  const raw = String(path || '').trim();
  if (!raw) return;
  const isDir = kind === 'dir';
  const text = isDir && !raw.endsWith('/') ? `${raw}/` : raw;
  S.selectionSource = 'workspace';
  S.lastSelection = text;
  S.lastRange = null;
  S.selectionSection = null;
  S.selectionCfi = '';
  S.selectionChapter = null;
  S.selectionChunk = null;
  S.selectionAnchorText = '';
  showSelectionRef(text, isDir ? '工作区文件夹' : '工作区文件');
}

/** 换章时收起写作页的引用条；阅读页的书里选区别被连带清掉。 */
export function clearWritingSelection() {
  if (S.selectionSource !== 'writing') return;
  clearSelectionRef();
}

/** 离开写作页时把工作区引用一起收掉（它只属于写作对话）。 */
export function clearWorkspaceSelection() {
  if (S.selectionSource !== 'workspace') return;
  clearSelectionRef();
}

/** 阅读页：书页里的选区塌陷（换页、点正文别处）= 取消这次引用。 */
export function clearBookSelection() {
  if (S.selectionSource !== 'book') return;
  clearSelectionRef();
}

export function handleQuote() {
  if (!S.lastSelection || S.lastSelection.length < 2) return;
  showSelectionRef(S.lastSelection, '书中');
}

// ── 素材附件托盘 ──────────────────────────────────────────────────
//
// 写作页「添加到会话」只把素材挂进托盘，不发送：作者还要在输入框里说自己
// 要什么，而且一次可以挂好几张。附件与输入框同生命周期——按下发送就随这
// 条消息一起送出，然后清空；不想带就点胶囊上的 ×。
//
// 传出的是**事实**（id / 标题 / 分类 / 路径），怎么读、要不要提升进作品、
// 用完后记不记 use，交给 agent 的写作提示词，UI 不写流程。

const attachedMaterials = [];

function materialAttachBlock(items) {
  const parts = items.map((m) => {
    const lines = [`素材 id：${m.id || ''}`];
    if (m.title) lines.push(`标题：${m.title}`);
    if (m.category) lines.push(`分类：${m.category}`);
    if (m.path) lines.push(`路径：${m.path}`);
    return lines.join('\n');
  });
  return `[本轮附加素材]\n${parts.join('\n\n')}`;
}

//: 托盘胶囊只露前四个字：素材标题动辄十几字，全显示就把输入框上方铺满了。
const ATTACH_LABEL_CHARS = 4;

function attachLabel(text) {
  const value = String(text || '').trim();
  return value.length > ATTACH_LABEL_CHARS
    ? `${value.slice(0, ATTACH_LABEL_CHARS)}…` : value;
}

function renderAttachTray() {
  const tray = document.getElementById('attachTray');
  if (!tray) return;
  tray.textContent = '';
  tray.hidden = attachedMaterials.length === 0;
  attachedMaterials.forEach((item, index) => {
    const chip = document.createElement('span');
    chip.className = 'attach-chip';
    // 胶囊上只剩四个字，完整标题与路径放悬停提示里。
    chip.title = [item.title, item.path].filter(Boolean).join('\n');
    chip.appendChild(elIcon('file-text'));
    const label = document.createElement('span');
    label.className = 'attach-chip-label';
    label.textContent = attachLabel(item.title || item.id || '素材');
    chip.appendChild(label);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'attach-chip-remove';
    remove.title = '取消这张素材';
    remove.setAttribute('aria-label', '取消这张素材');
    remove.appendChild(elIcon('x'));
    remove.addEventListener('click', () => {
      attachedMaterials.splice(index, 1);
      renderAttachTray();
    });
    chip.appendChild(remove);
    tray.appendChild(chip);
  });
  if (window.lucide && lucide.createIcons) lucide.createIcons();
}

function elIcon(name) {
  const icon = document.createElement('i');
  icon.setAttribute('data-lucide', name);
  icon.setAttribute('aria-hidden', 'true');
  return icon;
}

/** 挂一张素材到本轮；同一张（id 或路径相同）只留一份。 */
export function attachMaterial(item) {
  const raw = item || {};
  const id = String(raw.id || raw.material_id || '');
  const path = String(raw.path || '');
  if (!id && !path) return { added: false, reason: 'empty' };
  const dup = attachedMaterials.some(
    (m) => (id && m.id === id) || (path && m.path === path));
  if (dup) return { added: false, reason: 'duplicate', count: attachedMaterials.length };
  attachedMaterials.push({
    id,
    title: String(raw.title || ''),
    category: String(raw.category || ''),
    path,
  });
  renderAttachTray();
  return { added: true, count: attachedMaterials.length };
}

export function clearAttachments() {
  if (!attachedMaterials.length) return;
  attachedMaterials.length = 0;
  renderAttachTray();
}

export function clearSelectionRef() {
  const wasWriting = S.selectionSource === 'writing';
  clearQuoteHighlight();
  document.getElementById('selectionTag').classList.remove('visible');
  S.lastSelection = '';
  S.lastRange = null;
  S.selectionSection = null;
  S.selectionChapter = null;
  S.selectionChunk = null;
  S.selectionCfi = '';
  S.selectionAnchorText = '';
  if (wasWriting) {
    // 引用条和编辑器里的高亮是同一件事：撤掉引用，正文也不该还亮着。
    // 高亮有两份：选区镜像（失焦后画的）和 textarea 自己的选区。
    if (typeof window.hideWriteQuoteVeil === 'function') {
      window.hideWriteQuoteVeil();
    }
    // setSelectionRange 会顺手把编辑器滚到插入点，滚回原处。
    const ed = document.getElementById('writeEditor');
    if (ed && ed.selectionEnd > ed.selectionStart) {
      const top = ed.scrollTop;
      ed.setSelectionRange(ed.selectionEnd, ed.selectionEnd);
      ed.scrollTop = top;
    }
  }
}

// ── Voice: recording (ASR) ─────────────────────────────────────────

let _audioCtx = null;
let _mediaStream = null;
let _recordingTimer = null;
let _recordingStart = 0;
let _isRecording = false;

export function toggleRecording() {
  if (_isRecording) {
    stopRecording();
  } else {
    startRecording();
  }
}

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    _mediaStream = stream;
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    const source = _audioCtx.createMediaStreamSource(stream);

    // Use ScriptProcessor to capture raw PCM (mono, 16kHz)
    const bufferSize = 4096;
    const processor = _audioCtx.createScriptProcessor(bufferSize, 1, 1);
    const pcmChunks = [];

    processor.onaudioprocess = (e) => {
      if (!_isRecording) return;
      const input = e.inputBuffer.getChannelData(0);
      // Convert Float32 to Int16 PCM
      const buf = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        const s = Math.max(-1, Math.min(1, input[i]));
        buf[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      pcmChunks.push(buf);
    };

    source.connect(processor);
    processor.connect(_audioCtx.destination);

    _isRecording = true;
    _recordingStart = Date.now();
    document.getElementById('micBtn').classList.add('recording');
    document.getElementById('micBtn').textContent = '\u{1F534}';
    showRecordingStatus('录音中... 0:00');
    _recordingTimer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - _recordingStart) / 1000);
      const mm = String(Math.floor(elapsed / 60)).padStart(2, '0');
      const ss = String(elapsed % 60).padStart(2, '0');
      showRecordingStatus(`录音中... ${mm}:${ss}`);
      if (elapsed >= 30) stopRecording();
    }, 500);

    // Store for stop
    startRecording._processor = processor;
    startRecording._source = source;
    startRecording._pcmChunks = pcmChunks;

  } catch (e) {
    console.error('Mic error:', e);
    showRecordingStatus('麦克风不可用: ' + e.message);
    setTimeout(hideRecordingStatus, 3000);
  }
}

function stopRecording() {
  if (!_isRecording) return;
  _isRecording = false;

  if (_recordingTimer) { clearInterval(_recordingTimer); _recordingTimer = null; }

  const processor = startRecording._processor;
  const source = startRecording._source;
  const pcmChunks = startRecording._pcmChunks;

  if (processor) {
    processor.disconnect();
    source.disconnect();
  }
  if (_audioCtx) { _audioCtx.close(); _audioCtx = null; }
  if (_mediaStream) {
    _mediaStream.getTracks().forEach(t => t.stop());
    _mediaStream = null;
  }

  const micBtn = document.getElementById('micBtn');
  micBtn.classList.remove('recording');
  micBtn.textContent = '\u{1F504}';
  showRecordingStatus('识别中...');

  // Encode PCM → WAV blob
  if (!pcmChunks || pcmChunks.length === 0) {
    hideRecordingStatus();
    micBtn.textContent = '\u{1F3A4}';
    return;
  }

  const totalLength = pcmChunks.reduce((sum, c) => sum + c.length, 0);
  const wavBuf = new ArrayBuffer(44 + totalLength * 2);
  const view = new DataView(wavBuf);

  // WAV header
  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + totalLength * 2, true);
  writeString(view, 8, 'WAVE');
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);           // chunk size
  view.setUint16(20, 1, true);            // PCM
  view.setUint16(22, 1, true);            // mono
  view.setUint32(24, 16000, true);        // sample rate
  view.setUint32(28, 16000 * 2, true);    // byte rate
  view.setUint16(32, 2, true);            // block align
  view.setUint16(34, 16, true);           // bits per sample
  writeString(view, 36, 'data');
  view.setUint32(40, totalLength * 2, true);

  let offset = 44;
  for (const chunk of pcmChunks) {
    for (let i = 0; i < chunk.length; i++) {
      view.setInt16(offset, chunk[i], true);
      offset += 2;
    }
  }

  const wavBlob = new Blob([wavBuf], { type: 'audio/wav' });
  sendAudioForASR(wavBlob);
}

function writeString(view, offset, str) {
  for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
}

async function sendAudioForASR(wavBlob) {
  const micBtn = document.getElementById('micBtn');
  const duration = Math.round((Date.now() - _recordingStart) / 1000);

  // 1. Create voice bubble immediately (user side)
  const voiceUrl = URL.createObjectURL(wavBlob);
  createVoiceBubble('user', voiceUrl, duration);

  // 2. Send voice message to agent (ASR + LLM)
  sendVoiceMessage(wavBlob, duration);

  hideRecordingStatus();
  if (micBtn) micBtn.textContent = '\u{1F3A4}';
}

export function createVoiceBubble(role, audioUrl, durationSec, transcript, timeMs) {
  const log = document.getElementById('chatLog');
  const now = timeMs || Date.now();
  if (shouldShowTimestamp(S.chatLastTimestamp, now)) {
    log.appendChild(createTimestampSep(now));
  }
  S.chatLastTimestamp = now;

  const mm = String(Math.floor(durationSec / 60)).padStart(2, '0');
  const ss = String(durationSec % 60).padStart(2, '0');
  const durText = `${mm}:${ss}`;

  const div = document.createElement('div');
  div.className = 'msg voice-msg ' + role;

  if (role === 'agent') {
    const avatarEl = (S.currentMode === 'roleplay' && S.currentCharacterName)
      ? createAvatar(S.currentCharacterName, S.rpSelectedBookId || S.currentBook)
      : null;
    if (avatarEl) div.appendChild(avatarEl);
  } else if (role === 'user' && S.userAvatarUrl) {
    div.appendChild(createUserTurnAvatar());
  }

  // ── Voice bubble body ──────────────────────────────────────────
  const bubble = document.createElement('div');
  bubble.className = 'voice-bubble';

  const audio = new Audio(audioUrl);
  let _playing = false;

  const playBtn = document.createElement('button');
  playBtn.className = 'voice-play-btn';
  playBtn.innerHTML = '<i data-lucide="play"></i>';
  playBtn.onclick = function (e) {
    e.stopPropagation();
    if (_playing) { audio.pause(); playBtn.innerHTML = '<i data-lucide="play"></i>'; _playing = false; }
    else { audio.play(); playBtn.innerHTML = '<i data-lucide="pause"></i>'; _playing = true; }
  };
  audio.onended = function () { playBtn.innerHTML = '<i data-lucide="play"></i>'; _playing = false; };

  // Waveform bars
  const waveform = document.createElement('div');
  waveform.className = 'voice-waveform';
  for (let i = 0; i < 7; i++) {
    const span = document.createElement('span');
    waveform.appendChild(span);
  }

  const dur = document.createElement('span');
  dur.className = 'voice-dur';
  dur.textContent = durText;

  bubble.appendChild(playBtn);
  bubble.appendChild(waveform);
  bubble.appendChild(dur);

  // ── Build wrapper ─────────────────────────────────────────────
  const bodyWrap = document.createElement('div');
  bodyWrap.className = 'voice-body-wrap';
  bodyWrap.appendChild(bubble);

  // ── Transcript toggle + body ──────────────────────────────────
  const hasTranscript = transcript && transcript.trim();
  let transcriptBody = null;
  let transcriptToggle = null;
  let _transcriptOpen = false;

  if (hasTranscript) {
    // Toggle button ("转文字" / "收起")
    transcriptToggle = document.createElement('button');
    transcriptToggle.className = 'voice-transcript-toggle';
    transcriptToggle.textContent = '转文字';
    transcriptToggle.onclick = function (e) {
      e.stopPropagation();
      _transcriptOpen = !_transcriptOpen;
      if (_transcriptOpen) {
        transcriptBody.classList.add('open');
        transcriptToggle.textContent = '收起';
      } else {
        transcriptBody.classList.remove('open');
        transcriptToggle.textContent = '转文字';
      }
    };

    // Transcript body (hidden by default)
    transcriptBody = document.createElement('div');
    transcriptBody.className = 'voice-transcript-body';
    transcriptBody.textContent = transcript;
    bodyWrap.appendChild(transcriptBody);
  }

  div.appendChild(bodyWrap);

  // ── Inline action bar (below bubble, WeChat-style) ────────────
  if (hasTranscript) {
    const actions = document.createElement('div');
    actions.className = 'voice-actions';
    actions.appendChild(transcriptToggle);
    div.appendChild(actions);
  }

  // ── Right-click → context menu ────────────────────────────────
  div.addEventListener('contextmenu', function (e) {
    e.preventDefault();
    e.stopPropagation();
    _showVoiceContextMenu(e.clientX, e.clientY, {
      audio: audio,
      playBtn: playBtn,
      playing: function () { return _playing; },
      setPlaying: function (v) { _playing = v; },
      hasTranscript: hasTranscript,
      transcriptOpen: function () { return _transcriptOpen; },
      toggleTranscript: function () {
        if (!transcriptToggle || !transcriptBody) return;
        _transcriptOpen = !_transcriptOpen;
        if (_transcriptOpen) {
          transcriptBody.classList.add('open');
          transcriptToggle.textContent = '收起';
        } else {
          transcriptBody.classList.remove('open');
          transcriptToggle.textContent = '转文字';
        }
      },
    });
  });

  log.appendChild(div);
  stampMsgIndex(div);
  autoScrollToBottom(log);
  audio.load();
  return div;
}

// ── Voice context menu (singleton, WeChat / Douyin style) ─────────

let _vcmEl = null;
let _vcmState = null;

function _ensureVoiceContextMenu() {
  if (_vcmEl) return;
  _vcmEl = document.createElement('div');
  _vcmEl.className = 'voice-context-menu';
  _vcmEl.style.display = 'none';
  document.body.appendChild(_vcmEl);

  // Close on outside click
  document.addEventListener('click', function () {
    if (_vcmEl) { _vcmEl.style.display = 'none'; _vcmState = null; }
  });
  // Close on scroll (context menu anchored to a position that may scroll away)
  document.addEventListener('scroll', function () {
    if (_vcmEl) { _vcmEl.style.display = 'none'; _vcmState = null; }
  }, true);
}

function _showVoiceContextMenu(x, y, state) {
  _ensureVoiceContextMenu();
  _vcmState = state;

  const isPlaying = state.playing();
  const hasTranscript = state.hasTranscript;
  const transcriptOpen = state.transcriptOpen();

  // Build menu items
  const items = [];

  // Play / Pause
  items.push({
    icon: isPlaying ? '<i data-lucide="pause"></i>' : '<i data-lucide="play"></i>',
    label: isPlaying ? '暂停' : '播放',
    action: function () {
      if (isPlaying) {
        state.audio.pause();
        state.playBtn.innerHTML = '<i data-lucide="play"></i>';
        state.setPlaying(false);
      } else {
        state.audio.play();
        state.playBtn.innerHTML = '<i data-lucide="pause"></i>';
        state.setPlaying(true);
      }
    },
  });

  // Transcript toggle (only if transcript data exists)
  if (hasTranscript) {
    items.push({
      icon: '<i data-lucide="sticky-note"></i>',
      label: transcriptOpen ? '收起' : '转文字',
      action: function () {
        state.toggleTranscript();
      },
    });
  }

  // Render
  _vcmEl.innerHTML = items.map(function (it) {
    return '<div class="vcm-item" data-action="' + it.label + '">'
      + '<span class="vcm-icon">' + it.icon + '</span>'
      + '<span class="vcm-label">' + it.label + '</span>'
      + '</div>';
  }).join('');

  // Bind clicks
  var itemEls = _vcmEl.querySelectorAll('.vcm-item');
  itemEls.forEach(function (el, i) {
    el.onclick = function (e) {
      e.stopPropagation();
      items[i].action();
      _vcmEl.style.display = 'none';
      _vcmState = null;
    };
  });

  // Position (clamp to viewport)
  var menuW = 140;
  var menuH = items.length * 40 + 8;
  var left = Math.min(x, window.innerWidth - menuW - 8);
  var top = Math.min(y, window.innerHeight - menuH - 8);
  _vcmEl.style.left = left + 'px';
  _vcmEl.style.top = top + 'px';
  _vcmEl.style.display = 'block';
}

// ── Voice message: send (ASR + LLM) ──────────────────────────────

async function sendVoiceMessage(wavBlob, durationSec) {
  // 同 sendMessage：一轮只允许一条流，流式期间不再叠一条语音回合。
  if (S._activeStream) return;
  const log = document.getElementById('chatLog');

  // 1. Upload recording → persistent URL
  let voiceUrl = '';
  try {
    const upForm = new FormData();
    upForm.append('file', wavBlob, 'recording.wav');
    const upResp = await fetch(S.API + '/api/voice/upload', { method: 'POST', body: upForm });
    if (upResp.ok) {
      const upData = await upResp.json();
      voiceUrl = upData.url || '';
    }
  } catch (e) { console.error('Voice upload error:', e); }

  // 2. ASR + emotion
  let voiceText = '';
  let voiceEmotion = '';
  try {
    const form = new FormData();
    form.append('file', wavBlob, 'recording.wav');
    const resp = await fetch(S.API + '/api/asr', { method: 'POST', body: form });
    if (resp.ok) {
      const data = await resp.json();
      voiceText = data.text || '';
      voiceEmotion = data.emotion || '';
    }
  } catch (e) { console.error('ASR error:', e); }

  if (!voiceText) {
    addChat('agent', '(语音识别失败，请重试)');
    return;
  }

  // Store history
  S.chatHistory.push({ role: 'user', content: voiceText });
  // 语音同样算会话开始：撤掉模式预选，锁定这次会话的阅读模式。
  if (window.updateModePrelude) window.updateModePrelude();

  // 3. Build message with emotion context
  let message = voiceText;
  if (S.currentMode === 'roleplay' && voiceEmotion) {
    message = `[读者说话时的情绪: ${voiceEmotion}]\n${voiceText}`;
  }

  // 4. Build voice metadata for persistence
  const voiceMeta = voiceUrl ? {
    type: 'voice_input',
    voice_url: voiceUrl,
    duration_ms: durationSec * 1000,
    transcript: voiceText,
    emotion: voiceEmotion || '',
    format: 'wav'
  } : null;

  // 5. Create agent bubble + streaming placeholder (collapsed until it has
  //    content — see setBubbleHTML for why).
  const avatarEl = (S.currentMode === 'roleplay' && S.currentCharacterName)
    ? createAvatar(S.currentCharacterName, S.rpSelectedBookId || S.currentBook)
    : null;
  const agentDiv = createBubble('agent', '', avatarEl);
  setBubbleHTML(agentDiv, '');
  log.appendChild(agentDiv);
  stampMsgIndex(agentDiv);
  if (wantsAgentAvatar('agent')) {
    // Insert first: think_start inserts its row immediately before agentDiv,
    // so the avatar stays above the thinking row.
    log.insertBefore(createAgentTurnAvatar(), agentDiv);
  }
  stickChatToBottom(log);

  // 6. Send to LLM (reuse sendMessage logic)
  const state = {
    _fullText: '',
    _thinkBuf: '',
    _thinkRow: null,
    _writeProgress: null,
    _thinkStartTime: 0,
    _twActive: false,
    _twIndex: 0,
    _twLastFrame: 0,
    _streamDone: false,
    _v1Fallback: false,
    // Which conversation this stream belongs to. See S.chatEpoch.
    _epoch: S.chatEpoch,
    // Stop freeze point: characters already painted when the user hit
    // stop. -1 = not stopped; never backfill the buffered tail.
    _stopIndex: -1,
  };

  const abortCtrl = new AbortController();
  S._chatAbort = abortCtrl;
  const turnId = newTurnId();
  S._activeStream = {
    turnId: turnId,
    abortCtrl: abortCtrl,
    state: state,
    agentDiv: agentDiv,
    fallbackTimer: null,
  };
  setSendButtonBusy(true);

  const isWriting = window._currentTab === 'write' && !!window.WritingUI;
  const onReader = window._currentTab === 'reader';
  const isRoleplay = S.currentMode === 'roleplay';
  const page = isWriting ? 'writing' : (onReader ? 'reader' : 'home');
  // 回复落在哪条会话线：中途切页也不能把新会话记到别的线上。
  const sendTab = window.getActiveChatTab ? window.getActiveChatTab() : 'home';
  const sendBook = S.currentBook || '';
  const sendWork = isWriting ? (window.WritingUI.context().workId || '') : null;
  state._sendTab = sendTab;
  state._sendBook = sendBook;
  state._sendWork = sendWork;

  if (isWriting) initTurnTrace(log, agentDiv, state);

  try {
    const resp = await fetch(S.API + '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: abortCtrl.signal,
      body: JSON.stringify({
        message: message,
        mode: isWriting ? 'writing' : S.currentMode,
        page: page,
        ...(isWriting
          ? { writing_work_id: window.WritingUI.context().workId || '',
              writing_chapter_index: window.WritingUI.context().index || 0,
              ...(S.currentSessionId ? {} : {
                writing_mode: S.writingMode || 'standard',
              }) }
          : {}),
        stream: 'v2',
        turn_id: turnId,
        voice: voiceMeta || undefined,
        character_name: S.currentCharacterName || undefined,
        role_kind: S.currentRoleKind || 'character',
        role_ref: S.currentRoleRef || undefined,
        // 剧透门只在扮演模式有意义；主页/写作页不发。
        max_chapter: (isRoleplay && !isWriting) ? S.currentMaxChapter : undefined,
        // 阅读进度只喂 reader-notes 召回通道（与 max_chapter 的剧透门无关）
        reading_chapter: (!isWriting && onReader) ? S.currentChapter : undefined,
        book_id: isWriting ? '' : (isRoleplay
          ? (S.rpSelectedBookId || S.currentBook || '')
          : (onReader ? (S.currentBook || '') : '')),
        history: (() => {
          const h = S.chatHistory;
          if (h.length && h[h.length - 1].role === 'user') return h.slice(0, -1).slice(-6);
          return h.slice(-6);
        })(),
        session_id: S.currentSessionId
      })
    });

    if (!resp.ok) { await reportHttpError(resp, agentDiv, state); return; }

    // Fork needs the persisted message id even before a page reload.
    stampUserMessageId(resp, typeof userDiv === 'undefined' ? null : userDiv, state);

    const sid = resp.headers.get('X-Session-Id');
    // Adopt the session the server actually persisted to. It differs from
    // the local id when the server had to create a new one (no id, or a
    // page/mode mismatch — session lines never mix). The epoch check keeps
    // a branch/tab switch made mid-stream from being overwritten; in that
    // case the id still belongs to the line that sent it.
    if (sid) {
      if (state._epoch === S.chatEpoch) {
        if (window.adoptSession) window.adoptSession(sid, { tab: sendTab, bookId: sendBook, workId: state._sendWork });
        else S.currentSessionId = sid;
        window.saveViewState && window.saveViewState();
      } else if (window.recordTabSession) {
        window.recordTabSession(sendTab, sid, { bookId: sendBook, workId: state._sendWork });
      }
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let sseBuffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      sseBuffer += decoder.decode(value, { stream: true });
      const lines = sseBuffer.split('\n');
      sseBuffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6);
        if (payload === '[DONE]') continue;
        try {
          handleSSEEvent(JSON.parse(payload), state, agentDiv);
        } catch (_) {
          state._fullText += payload;
          setBubbleHTML(agentDiv,
            renderMarkdown(state._fullText.replace(/<think>[\s\S]*?<\/think>/g, '')));
        }
      }
    }

    // Wait for typewriter
    if (state._fullText) {
      const startWait = Date.now();
      while (state._twActive && (Date.now() - startWait < 30000)) {
        await new Promise(r => setTimeout(r, 50));
      }
      if (state._twActive) {
        stopTypewriter(state);
        state._twIndex = state._fullText.length;
        setBubbleHTML(agentDiv, renderMarkdown(state._fullText));
      }
    }

    if (state._epoch === S.chatEpoch) {
      pushAgentHistory(state);
    }
    // Voice bubbles are rendered by startTypewriter on typewriter completion —
    // no duplicate rendering here.
  } catch (e) {
    if (e.name === 'AbortError') {
      // 同 sendMessage：用户停止 → 补中断标记；会话切换 → 静默收尾。
      stopTypewriter(state);
      finalizeThinkRow(state);
      if (state._stopping) {
        markInterrupted(agentDiv, state);
      } else {
        setBubbleHTML(agentDiv, renderMarkdown(state._fullText));
      }
      finishTurnTrace(state, 'interrupted');
      return;
    }
    console.error('Voice message error:', e);
    setBubbleHTML(agentDiv, renderMarkdown('请求失败: ' + e.message));
    finishTurnTrace(state, 'error');
  } finally {
    if (S._activeStream && S._activeStream.abortCtrl === abortCtrl) {
      clearTimeout(S._activeStream.fallbackTimer);
      S._activeStream = null;
      setSendButtonBusy(false);
    }
    if (S._chatAbort === abortCtrl) S._chatAbort = null;
    finishTurnTrace(state, state._turnOutcome || 'done');
  }
}

function showRecordingStatus(msg) {
  let el = document.getElementById('recStatus');
  if (!el) {
    el = document.createElement('div');
    el.id = 'recStatus';
    el.className = 'rec-status';
    document.getElementById('chatInputRow')?.parentNode?.insertBefore(
      el, document.getElementById('chatInputRow')
    );
  }
  el.textContent = msg;
  el.style.display = 'block';
}

function hideRecordingStatus() {
  const el = document.getElementById('recStatus');
  if (el) el.style.display = 'none';
}

// ── Voice: TTS playback ────────────────────────────────────────────

async function getVoiceForCharacter(charName) {
  if (!S.rpSelectedBookId) return '茉莉';
  try {
    const resp = await fetch(
      `${S.API}/avatars/${S.rpSelectedBookId}/${sanitizeFilename(charName)}_voice.json`
    );
    if (resp.ok) {
      const data = await resp.json();
      return data.voice || '茉莉';
    }
  } catch (_) { /* fall through */ }
  return '茉莉';
}

async function requestTTS(text, voice) {
  try {
    const resp = await fetch(S.API + '/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice })
    });
    if (!resp.ok) return null;
    return await resp.json();
  } catch (_) { return null; }
}

function attachVoiceBar(bubbleDiv, audioUrl) {
  // Remove any existing voice bar
  const existing = bubbleDiv.querySelector('.voice-bar');
  if (existing) existing.remove();

  const audio = new Audio(audioUrl);
  let playing = false;

  const bar = document.createElement('div');
  bar.className = 'voice-bar';

  const btn = document.createElement('button');
  btn.className = 'voice-play-btn';
  btn.innerHTML = '<i data-lucide="play"></i>';
  btn.onclick = () => {
    if (playing) {
      audio.pause();
      btn.innerHTML = '<i data-lucide="play"></i>';
      playing = false;
    } else {
      audio.play();
      btn.innerHTML = '<i data-lucide="pause"></i>';
      playing = true;
    }
  };

  audio.onended = () => {
    btn.innerHTML = '<i data-lucide="play"></i>';
    playing = false;
  };

  const waveform = document.createElement('div');
  waveform.className = 'voice-waveform';
  for (let i = 0; i < 5; i++) {
    const span = document.createElement('span');
    waveform.appendChild(span);
  }

  bar.appendChild(btn);
  bar.appendChild(waveform);
  bubbleDiv.appendChild(bar);

  // Preload
  audio.load();
}

// 取消引用的 × 走 mousedown，不走 click：click 要求按下与松开落在同一个
// 元素上，编辑器失焦那一瞬间的布局变化会让这一下整个落空（实测第一次点 ×
// 没有反应，要先在别处空点一次）。preventDefault 顺带让编辑器在这一下里
// 不失焦、选区不被收走。
(function bindSelectionClear() {
  const btn = document.querySelector('#selectionTag .ref-clear');
  if (!btn) return;
  btn.addEventListener('mousedown', (event) => {
    event.preventDefault();
    event.stopPropagation();
    // 这一下之后的 mouseup 不要拿还留着的选区把引用装回来。
    _clearQuotePointer = true;
    clearSelectionRef();
  });
})();

// ── HTML onclick access ───────────────────────────────────────────
window.sendMessage = sendMessage;
// 写作页「添加到会话」：只挂附件，不发送（发送由作者按）。
window.attachMaterial = attachMaterial;
window.clearAttachments = clearAttachments;
window.sendOrStop = sendOrStop;
window.stopAgent = stopAgent;
window.handleQuote = handleQuote;
window.clearSelectionRef = clearSelectionRef;
window.setWritingSelection = setWritingSelection;
window.clearWritingSelection = clearWritingSelection;
window.setWorkspaceSelection = setWorkspaceSelection;
window.clearWorkspaceSelection = clearWorkspaceSelection;
window.clearBookSelection = clearBookSelection;
window.addChat = addChat;
window.syncAgentAvatars = syncAgentAvatars;
window.syncUserAvatars = syncUserAvatars;
document.addEventListener('bc:agent-avatar', (ev) => {
  syncAgentAvatars(ev.detail && ev.detail.url);
});
document.addEventListener('bc:user-avatar', (ev) => {
  syncUserAvatars(ev.detail && ev.detail.url);
});
window.uploadAvatar = uploadAvatar;
window.toggleRecording = toggleRecording;
window.handlePendingForkAction = handlePendingForkAction;
