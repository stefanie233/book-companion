/* Book Companion — chat: bubble UI, SSE v2, typewriter, think collapse. */

import { S } from './state.js';
import { updateUsage } from './context-usage.js';

// ── Utilities ──────────────────────────────────────────────────────

/** Auto-scroll to bottom only if user is near the bottom (within 150px).
 *  Prevents fighting the user when they scroll up to read history. */
function autoScrollToBottom(log) {
  if (!log) return;
  const dist = log.scrollHeight - log.scrollTop - log.clientHeight;
  if (dist < 150) log.scrollTop = log.scrollHeight;
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
      toggle.textContent = '\u{1F914} 思考中…';
    } else if (sec !== undefined && sec !== null) {
      toggle.textContent = `\u{1F914} 思考过程 (${sec}s)`;
    } else {
      toggle.textContent = '\u{1F914} 思考过程';
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
    btn.title = '重新回复:让书伴重新回复,从处分叉新分支';
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
  log.appendChild(div);
  stampMsgIndex(div);
  log.scrollTop = log.scrollHeight;
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
  log.scrollTop = log.scrollHeight;
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
        agentDiv.parentNode.insertBefore(row, agentDiv);
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
        agentDiv.parentNode.insertBefore(thinkRow, agentDiv);
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
      // Show a brief indicator next to the bubble
      break;

    case 'tool_end':
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
      markInterrupted(agentDiv, state);
      break;

    case 'error':
      stopTypewriter(state);
      finalizeThinkRow(state);
      setBubbleHTML(agentDiv,
        renderMarkdown(state._fullText || event.message || '发生错误'));
      S.chatHistory.push({ role: 'agent', content: state._fullText || event.message });
      state._historyPushed = true;
      break;

    case 'done':
      state._streamDone = true;
      // 兜底：interrupted 帧没读到（连接被我们 aborted，或取消到得太晚
      // 服务端已正常收尾）时，本地的停止意图也要把界面收成「已中断」。
      if (event.interrupted || state._stopping) {
        markInterrupted(agentDiv, state);
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
            });
          } else {
            S.currentSessionId = event.session_id;
          }
          window.saveViewState && window.saveViewState();
        } else if (window.recordTabSession && state && state._sendTab) {
          window.recordTabSession(state._sendTab, event.session_id,
            { bookId: state._sendBook });
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
  if (!text) return;
  // 一轮只允许一条流：流式期间按钮已经变成「停止」。
  if (S._activeStream) return;

  let fullText = text;
  // Quote-injection only for manual input from the chat box; fork/regenerate
  // paths supply their own text and never carry a selection ref.
  if (!isOverride) {
    const tagEl = document.getElementById('selectionTag');
    if (tagEl.classList.contains('visible')) {
      const srcEl = document.getElementById('selTagSrc');
      fullText = `[引用自${srcEl.textContent}: "${S.lastSelection.slice(0, 200)}"]\n${text}`;
    }
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
  userDiv.querySelector('.bubble').textContent = text;
  log.appendChild(userDiv);
  stampMsgIndex(userDiv);
  log.scrollTop = log.scrollHeight;
  S.chatHistory.push({ role: 'user', content: text });
  // 会话已经开始：撤掉模式预选，把这次会话的阅读模式固定下来。
  if (window.updateModePrelude) window.updateModePrelude();
  if (!isOverride) input.value = '';

  // 写作页：带上当前作品/章，回合结束后刷新工作台（工具可能建了书/章/正文）。
  const writingCtx = (window._currentTab === 'write' && window.WritingUI
    && typeof window.WritingUI.context === 'function')
    ? window.WritingUI.context() : null;
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
  log.scrollTop = log.scrollHeight;

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
  const onReader = window._currentTab === 'reader';
  const isRoleplay = S.currentMode === 'roleplay';
  const page = writingCtx ? 'writing' : (onReader ? 'reader' : 'home');
  // 回复落在哪条会话线：中途切页也不能把新会话记到别的线上。
  const sendTab = window.getActiveChatTab ? window.getActiveChatTab() : 'home';
  const sendBook = S.currentBook || '';
  state._sendTab = sendTab;
  state._sendBook = sendBook;

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
        } : {}),
        selection: (!writingCtx && !isRoleplay && onReader) ? S.lastSelection : '',
        selection_source: (!writingCtx && !isRoleplay && onReader) ? S.selectionSource : '',
        // Structural coordinates of the selection (see state.js). The
        // backend uses them to anchor <selection-context> and to raise the
        // spoiler gate — a selected passage is text the reader has seen.
        ...(!writingCtx && !isRoleplay && onReader && S.selectionSource === 'book'
          && S.lastSelection ? {
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
        if (window.adoptSession) window.adoptSession(sid, { tab: sendTab, bookId: sendBook });
        else S.currentSessionId = sid;
        window.saveViewState && window.saveViewState();
      } else if (window.recordTabSession) {
        window.recordTabSession(sendTab, sid, { bookId: sendBook });
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
      return;
    }
    stopTypewriter(state);
    setBubbleHTML(agentDiv, renderMarkdown('请求失败: ' + e.message));
  } finally {
    if (S._activeStream && S._activeStream.abortCtrl === abortCtrl) {
      clearTimeout(S._activeStream.fallbackTimer);
      S._activeStream = null;
      setSendButtonBusy(false);
    }
    if (S._chatAbort === abortCtrl) S._chatAbort = null;
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

document.addEventListener('mouseup', function (e) {
  setTimeout(function () {
    const sel = window.getSelection();
    const text = sel.toString().trim();
    if (!text || text.length < 2) {
      if (e.target.closest('.ann-popup') || e.target.closest('.toolbar-note-box')) return;
      if (typeof window.popupInteracting !== 'undefined' && window.popupInteracting) return;
      if (typeof window.hideAnnotationPopup === 'function') window.hideAnnotationPopup();
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
      if (typeof window.popupInteracting === 'undefined' || !window.popupInteracting) {
        if (typeof window.showAnnotationPopup === 'function') window.showAnnotationPopup(sel, text);
      }
    } else if (chatEl.contains(sel.anchorNode)) {
      S.selectionSource = 'chat';
      S.lastSelection = text;
      S.selectionSection = null;
      S.selectionCfi = '';
      S.selectionChapter = null;
      S.selectionChunk = null;
      S.selectionAnchorText = '';
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

export function handleQuote() {
  if (!S.lastSelection || S.lastSelection.length < 2) return;
  showSelectionRef(S.lastSelection, '书中');
}

export function clearSelectionRef() {
  document.getElementById('selectionTag').classList.remove('visible');
  S.lastSelection = '';
  S.lastRange = null;
  S.selectionSection = null;
  S.selectionChapter = null;
  S.selectionChunk = null;
  S.selectionCfi = '';
  S.selectionAnchorText = '';
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
  log.scrollTop = log.scrollHeight;
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
  log.scrollTop = log.scrollHeight;

  // 6. Send to LLM (reuse sendMessage logic)
  const state = {
    _fullText: '',
    _thinkBuf: '',
    _thinkRow: null,
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
  state._sendTab = sendTab;
  state._sendBook = sendBook;

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
              writing_chapter_index: window.WritingUI.context().index || 0 }
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
        if (window.adoptSession) window.adoptSession(sid, { tab: sendTab, bookId: sendBook });
        else S.currentSessionId = sid;
        window.saveViewState && window.saveViewState();
      } else if (window.recordTabSession) {
        window.recordTabSession(sendTab, sid, { bookId: sendBook });
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
      return;
    }
    console.error('Voice message error:', e);
    setBubbleHTML(agentDiv, renderMarkdown('请求失败: ' + e.message));
  } finally {
    if (S._activeStream && S._activeStream.abortCtrl === abortCtrl) {
      clearTimeout(S._activeStream.fallbackTimer);
      S._activeStream = null;
      setSendButtonBusy(false);
    }
    if (S._chatAbort === abortCtrl) S._chatAbort = null;
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

// ── HTML onclick access ───────────────────────────────────────────
window.sendMessage = sendMessage;
window.sendOrStop = sendOrStop;
window.stopAgent = stopAgent;
window.handleQuote = handleQuote;
window.clearSelectionRef = clearSelectionRef;
window.addChat = addChat;
window.uploadAvatar = uploadAvatar;
window.toggleRecording = toggleRecording;
window.handlePendingForkAction = handlePendingForkAction;
