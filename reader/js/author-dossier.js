/* 作者档案 —— 设计稿 05。
 *
 * 时间轴是主脊，其他视图是抽屉；事实 / 声音 / 解释 / 来源四层视觉分开。
 * 不引入前端框架：DOM 拼字符串 + 事件委托，和 author-timeline.js 同一套做法。
 *
 * 三种「空」必须分开说：没采到 / 声音没提取 / 被剧透门挡住。
 * 来源抽屉不是弹窗：右侧滑出，Esc 关闭，焦点陷阱。
 */

import { S } from './state.js';
import { escapeHtml } from './utils.js';

const DIM_ORDER = [
  'narrative_distance', 'sentence_rhythm', 'paragraph_shape',
  'dialogue_density', 'figurative_language', 'emotional_expression',
  'information_reveal', 'lexical_texture', 'humor_irony', 'closure_pattern',
];
const DIM_LABEL = {
  narrative_distance: '叙述距离',
  sentence_rhythm: '句子节奏',
  paragraph_shape: '段落形态',
  dialogue_density: '对话密度',
  figurative_language: '修辞倾向',
  emotional_expression: '情绪表达',
  information_reveal: '信息揭示',
  lexical_texture: '词汇质地',
  humor_irony: '幽默与反讽',
  closure_pattern: '收束方式',
};
const DIM_HINT = {
  narrative_distance: '近 ←→ 远',
  sentence_rhythm: '短促 ←→ 绵长',
  paragraph_shape: '碎片 ←→ 块状',
  dialogue_density: '少 ←→ 多',
  figurative_language: '直白 ←→ 繁复',
  emotional_expression: '克制 ←→ 外露',
  information_reveal: '直给 ←→ 延迟',
  lexical_texture: '日常 ←→ 书面',
  humor_irony: '无 ←→ 强',
  closure_pattern: '闭合 ←→ 开放',
};
const VALUE_LABEL = {
  narrative_distance: { intimate: '极近', close: '近', medium: '适中',
                        distant: '远', variable: '说法不一' },
  sentence_rhythm: { short: '短促', balanced: '均衡', long: '绵长',
                     varied: '长短交错', variable: '说法不一' },
  paragraph_shape: { fragmented: '碎片', flowing: '流动', blocky: '块状',
                     list_like: '清单式', variable: '说法不一' },
  dialogue_density: { low: '少', medium: '中等', high: '多',
                      variable: '说法不一' },
  figurative_language: { sparse: '克制', medium: '适中', dense: '繁密',
                         variable: '说法不一' },
  emotional_expression: { explicit: '外露', action_mediated: '借动作',
                          ironic: '反讽', withheld: '克制',
                          variable: '说法不一' },
  information_reveal: { front_loaded: '前置', delayed: '延迟',
                        fragmented: '碎片', gradual: '渐次',
                        variable: '说法不一' },
  lexical_texture: { plain: '朴素', literary: '书面', colloquial: '口语',
                     archaic: '古雅', technical: '术语', mixed: '混合',
                     variable: '说法不一' },
  humor_irony: { none: '无', dry: '冷峻', warm: '温和', absurd: '荒诞',
                 bitter: '苦涩', variable: '说法不一' },
  closure_pattern: { resolved: '闭合', open: '开放', elliptical: '省略',
                     reversal: '反转', variable: '说法不一' },
};
const SCENE_LABEL = {
  opening: '开篇', transition: '过渡', dialogue: '对话',
  description: '描写', action: '动作', introspection: '内省',
  emotional_peak: '情绪高点', quiet: '静场', argument: '论辩',
  landscape: '景物', memory: '回忆', ending: '收束', other: '其他',
};
const DOMAIN_LABEL = {
  fiction: '虚构文风', nonfiction: '非虚构文风', paratext: '序跋口吻',
  public: '公共访谈', mixed: '混合', unknown: '未标注',
};
const POSITION_LABEL = {
  narrator: '叙述者', avatar: '借角色说话', editor: '整理者',
  recorder: '记录者', absent: '不在这本书里', unknown: '',
};
const REL_LABEL = {
  mentor: '师承', student: '学生', friend: '友人', family: '家庭',
  spouse: '配偶', lover: '伴侣', rival: '对手', influence: '影响',
  translator: '翻译', publisher: '出版', editor: '编辑',
  collaborator: '合作', colleague: '同事', other: '其他',
};
const SOURCE_LABEL = { corpus: '书里', web: '网页', graph: '书库', model: '推测' };
const MISSING_LABEL = {
  'facts.none': '编年事实（带逐字引文）', facts: '编年事实',
  works: '作品', 'voice.none': '声音样本', 'voice.scene': '场景覆盖',
  relations: '关系', sources: '已核来源',
};
const STATUS_LABEL = {
  active: '已确认', provisional: '暂定', rejected: '已拒绝',
  superseded: '已退役',
};
const VOICE_STATUS = {
  none: '这本书还没有跑声音提取。',
  pending: '声音提取还没开始。',
  running: '声音提取正在跑。',
  ready: '',
  partial: '有声音样本，但还不足以稳定判断文风。',
  failed: '上次声音提取失败了。',
  stale: '书的内容变了，声音样本需要重采。',
  skipped: '这本书的作者不在正文里，没有可提取的声音。',
};

const TABS = [
  ['timeline', '年谱'], ['works', '作品'], ['voice', '声音'],
  ['relations', '关系'], ['themes', '主题'], ['sources', '来源'],
];

let _dossier = null;
let _bookId = '';
let _authorId = '';
let _tab = 'timeline';
let _lastFocus = null;
let _drawerOpen = false;

/* ══ 入口 ═════════════════════════════════════════════════════════════ */

export async function openAuthorDossier(bookId, authorId) {
  if (!authorId) return;
  _bookId = bookId || '';
  _authorId = authorId;
  _tab = 'timeline';
  _dossier = null;
  showView();
  const body = document.getElementById('adoBody');
  if (body) body.innerHTML = '<div class="rp-loading">读作者台账…</div>';
  const q = new URLSearchParams();
  if (_bookId) q.set('book_id', _bookId);
  // 沉浸模式带读者当前章 → 同一条剧透门；全局模式不带 = 显式全集视角。
  const gated = S.currentMode === 'roleplay' && S.currentMaxChapter != null;
  if (gated) q.set('max_chapter', String(S.currentMaxChapter));
  try {
    const resp = await fetch(S.API + '/api/authors/'
      + encodeURIComponent(authorId) + '/dossier?' + q.toString());
    const data = await resp.json();
    if (!resp.ok || !data.found) {
      throw new Error(data.error || data.note || ('HTTP ' + resp.status));
    }
    _dossier = data;
    render(data);
  } catch (e) {
    if (body) {
      body.innerHTML = '<div class="rp-error">加载失败：'
        + escapeHtml(e.message) + '</div>'
        + '<div class="ado-fallback"><button type="button"'
        + ' onclick="adoFallbackTimeline()">只看时间轴</button></div>';
    }
  }
}

export function closeAuthorDossier() {
  closeAuthorDrawer();
  const view = document.getElementById('authorDossierView');
  if (view) view.hidden = true;
  const layout = document.getElementById('graphPageLayout');
  if (layout) layout.style.display = '';
  const page = document.getElementById('page-graph');
  if (page) page.classList.remove('atl-mode');
  const btn = document.getElementById('graphAuthorBtn');
  if (btn) btn.classList.remove('active');
  _dossier = null;
}

function showView() {
  const view = document.getElementById('authorDossierView');
  if (!view) return;
  const timeline = document.getElementById('authorTimelineView');
  if (timeline) timeline.hidden = true;
  view.hidden = false;
  const layout = document.getElementById('graphPageLayout');
  if (layout) layout.style.display = 'none';
  const page = document.getElementById('page-graph');
  if (page) page.classList.add('atl-mode');
  const btn = document.getElementById('graphAuthorBtn');
  if (btn) btn.classList.add('active');
}

/** 档案加载失败/用户点「只看时间轴」时，退回旧的时间轴视图。 */
export function adoFallbackTimeline() {
  const view = document.getElementById('authorDossierView');
  if (view) view.hidden = true;
  if (typeof window.openAuthorTimelineView === 'function') {
    window.openAuthorTimelineView(_bookId, _authorId);
  }
}

/* ══ 头部与 tabs ══════════════════════════════════════════════════════ */

function pct(v) { return Math.round((Number(v) || 0) * 100); }

function valueZh(dim, v) {
  const map = VALUE_LABEL[dim] || {};
  return map[v] || v || '—';
}

function render(d) {
  const a = d.author || {};
  const title = document.getElementById('adoTitle');
  if (title) title.textContent = a.canonical_name || '作者档案';
  const sub = document.getElementById('adoSub');
  if (sub) {
    const years = (a.birth_year || a.death_year)
      ? (a.birth_year || '?') + '–' + (a.death_year || '?') : '';
    const pos = (d.position && d.position.position
      && d.position.position !== 'absent')
      ? (POSITION_LABEL[d.position.position] || '') : '';
    sub.textContent = [a.nationality, years, pos].filter(Boolean).join(' · ');
  }
  // 「用于写作参考」只在正文声音真的可用时出现：序跋/访谈再多也不给
  // 这个入口（设计稿 06 §一）。写作侧还有一道 voice_domain 闸。
  const writeBtn = document.getElementById('adoWriteBtn');
  if (writeBtn) {
    // 用覆盖度的全量计数，不用当前页可能被剧透门挡过的条目 ——
    // 写作透镜不按读者章节施门（它只借表达方式，不给剧情）。
    const counts = (d.coverage && d.coverage.counts) || {};
    writeBtn.hidden = !((counts.voice_samples || 0) > 0
      && (counts.traits || 0) > 0);
  }
  const body = document.getElementById('adoBody');
  if (!body) return;
  body.innerHTML = overview(d) + '<div id="adoTabBody" class="ado-tab-body"></div>';
  renderTabs(d);
  renderTab();
}

function overview(d) {
  const a = d.author || {};
  const cov = d.coverage || {};
  const c = cov.counts || {};
  const chips = [
    ['事实', c.records || 0, cov.facts],
    ['作品', c.works || 0, cov.works],
    ['声音', c.voice_samples || 0, cov.voice],
    ['关系', c.relations || 0, cov.relations],
    ['来源', c.sources || 0, cov.sources],
  ].map(function (x) {
    return '<span class="ado-chip" title="覆盖度 ' + pct(x[2]) + '%">'
      + '<b>' + x[0] + '</b><span>' + x[1] + '</span></span>';
  }).join('');
  const ident = [];
  if (a.name_native && a.name_native !== a.canonical_name) {
    ident.push(a.name_native);
  }
  if (a.birth_place) ident.push('生于' + a.birth_place);
  if (a.death_place) ident.push('卒于' + a.death_place);
  if (a.primary_language) ident.push('语言：' + a.primary_language);
  if (a.aliases && a.aliases.length) {
    ident.push('别名：' + a.aliases.slice(0, 4).join(' / '));
  }
  let badge = '';
  if (a.identity_status && a.identity_status !== 'resolved') {
    badge = '<span class="ado-badge is-warn">身份待确认</span>';
  }
  const gate = d.gate || {};
  const gateHtml = gate.note
    ? '<div class="ado-gate' + (gate.mode === 'chapter' ? '' : ' is-warn')
      + '">' + escapeHtml(gate.note) + '</div>' : '';
  const missing = (cov.missing || []).length
    ? '<div class="ado-missing">还缺：'
      + escapeHtml((cov.missing || []).map(function (x) {
        return MISSING_LABEL[x] || x;
      }).join(' · ')) + '</div>' : '';
  return '<div class="ado-overview">'
    + '<div class="ado-ident">' + escapeHtml(ident.join(' · ')) + badge + '</div>'
    + '<div class="ado-chips">' + chips + '</div>'
    + missing + gateHtml + '</div>';
}

function renderTabs(d) {
  const el = document.getElementById('adoTabs');
  if (!el) return;
  const counts = {
    timeline: ((d.timeline && (d.timeline.work.length + d.timeline.life.length)) || 0),
    works: (d.works || []).length,
    voice: (d.voice && d.voice.traits.length) || 0,
    relations: (d.relations || []).length,
    themes: ((d.themes || []).length + (d.reception || []).length),
    sources: (d.sources && d.sources.items.length) || 0,
  };
  el.innerHTML = TABS.map(function (t) {
    return '<button type="button" role="tab" class="ado-tab"'
      + ' data-tab="' + t[0] + '" aria-selected="' + (t[0] === _tab) + '"'
      + ' aria-controls="adoTabBody" tabindex="' + (t[0] === _tab ? '0' : '-1')
      + '">' + t[1]
      + (counts[t[0]] ? '<span class="ado-tab-n">' + counts[t[0]] + '</span>' : '')
      + '</button>';
  }).join('');
  if (!el.dataset.bound) {
    el.dataset.bound = '1';
    el.addEventListener('click', function (e) {
      const b = e.target.closest('.ado-tab');
      if (b) selectTab(b.dataset.tab);
    });
    el.addEventListener('keydown', function (e) {
      const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (keys.indexOf(e.key) < 0) return;
      e.preventDefault();
      const idx = TABS.findIndex(function (t) { return t[0] === _tab; });
      let next = idx;
      if (e.key === 'ArrowLeft') next = (idx + TABS.length - 1) % TABS.length;
      if (e.key === 'ArrowRight') next = (idx + 1) % TABS.length;
      if (e.key === 'Home') next = 0;
      if (e.key === 'End') next = TABS.length - 1;
      selectTab(TABS[next][0], true);
    });
  }
}

export function adoSelectTab(tab) { selectTab(tab); }

function selectTab(tab, focus) {
  if (!TABS.some(function (t) { return t[0] === tab; })) return;
  _tab = tab;
  const el = document.getElementById('adoTabs');
  if (el) {
    for (const b of el.querySelectorAll('.ado-tab')) {
      const on = b.dataset.tab === tab;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    }
  }
  renderTab();
}

function renderTab() {
  const el = document.getElementById('adoTabBody');
  if (!el || !_dossier) return;
  const d = _dossier;
  if (_tab === 'timeline') el.innerHTML = tabTimeline(d);
  else if (_tab === 'works') el.innerHTML = tabWorks(d);
  else if (_tab === 'voice') el.innerHTML = tabVoice(d);
  else if (_tab === 'relations') el.innerHTML = tabRelations(d);
  else if (_tab === 'themes') el.innerHTML = tabThemes(d);
  else el.innerHTML = tabSources(d);
}

/* ══ 年谱 ═════════════════════════════════════════════════════════════ */

function sourceBadge(src, verified) {
  const label = SOURCE_LABEL[src] || src || '';
  if (!label) return '';
  return '<span class="ado-src is-' + escapeHtml(src || 'model') + '">'
    + escapeHtml(label) + (verified ? ' · 引文已核' : '') + '</span>';
}

function tabTimeline(d) {
  const t = d.timeline || {};
  const rows = [];
  (t.work || []).forEach(function (r) { rows.push(Object.assign({}, r, { room: 'work' })); });
  (t.life || []).forEach(function (r) { rows.push(Object.assign({}, r, { room: 'life' })); });
  const head = '<div class="ado-tl-summary">'
    + '<span class="ado-chip"><b>作品</b><span>' + (t.work || []).length + '</span></span>'
    + '<span class="ado-chip"><b>生平</b><span>' + (t.life || []).length + '</span></span>'
    + ((t.undated && (t.undated.work + t.undated.life))
      ? '<span class="ado-chip is-warn"><b>无年</b><span>'
        + (t.undated.work + t.undated.life) + '</span></span>' : '')
    + '<button type="button" class="ado-link-btn" onclick="adoOpenTimeline()">'
    + '全屏时间轴</button></div>';
  if (!rows.length && !(t.undated_items || []).length) {
    return head + '<div class="rp-empty">'
      + escapeHtml(t.note || '这位作者还没有任何带年份的条目。') + '</div>';
  }
  rows.sort(function (a, b) {
    return (a.year - b.year) || (a.id || 0) - (b.id || 0);
  });
  const groups = [];
  rows.forEach(function (r) {
    const last = groups[groups.length - 1];
    if (!last || last.year !== r.year) groups.push({ year: r.year, items: [r] });
    else last.items.push(r);
  });
  let html = head;
  html += groups.map(function (g) {
    return '<div class="ado-tl-row"><div class="ado-tl-year">' + g.year
      + '</div><div class="ado-tl-stack">'
      + g.items.map(function (r) {
        return '<button type="button" class="ado-card ado-' + r.room + '"'
          + ' onclick="adoOpenRecord(' + r.id + ')"'
          + ' title="' + escapeHtml(r.statement) + '">'
          + '<span class="ado-card-room">' + (r.room === 'work' ? '作品' : '生平') + '</span>'
          + '<span class="ado-card-text">' + escapeHtml(r.statement) + '</span>'
          + '<span class="ado-card-meta">' + sourceBadge(r.source, r.verified)
          + '</span></button>';
      }).join('') + '</div></div>';
  }).join('');
  const und = (t.undated_items || []);
  if (und.length) {
    html += '<div class="ado-undated"><button type="button" class="ado-link-btn"'
      + ' onclick="adoToggleUndated()">时间未知 · '
      + ((t.undated && (t.undated.work + t.undated.life)) || und.length)
      + ' 条 ▾</button><div id="adoUndatedList" class="ado-undated-list" hidden>'
      + und.map(function (r) {
        return '<div class="ado-undated-item"><span class="ado-card-room">'
          + (r.room === 'work' ? '作品' : '生平') + '</span>'
          + escapeHtml(r.statement) + sourceBadge(r.source, r.verified) + '</div>';
      }).join('') + '</div></div>';
  }
  return html;
}

export function adoOpenTimeline() {
  const view = document.getElementById('authorDossierView');
  if (view) view.hidden = true;
  if (typeof window.openAuthorTimelineView === 'function') {
    window.openAuthorTimelineView(_bookId, _authorId);
  }
}

export function adoToggleUndated() {
  const el = document.getElementById('adoUndatedList');
  if (el) el.hidden = !el.hidden;
}

/* ══ 作品 ═════════════════════════════════════════════════════════════ */

function tabWorks(d) {
  const items = d.works || [];
  const meta = d.works_meta || {};
  if (!items.length) {
    return '<div class="rp-empty">'
      + escapeHtml(meta.note || '还没有作品条目。') + '</div>';
  }
  return '<div class="ado-work-grid">' + items.map(function (w) {
    const local = w.kind === 'local';
    const bits = [w.year || '', w.role === 'co-author' ? '合著'
      : w.role === 'translator' ? '译' : w.role === 'editor' ? '编' : ''];
    return '<div class="ado-work' + (local ? ' is-local' : '') + '">'
      + '<div class="ado-work-top"><span class="ado-work-kind">'
      + (local ? '书库' : '书目') + '</span>'
      + (w.verified ? '<span class="ado-badge">引文已核</span>' : '')
      + '</div>'
      + '<div class="ado-work-title">' + escapeHtml(w.title) + '</div>'
      + '<div class="ado-work-meta">' + escapeHtml(bits.filter(Boolean).join(' · '))
      + '</div>'
      + (local && w.book_id
        ? '<button type="button" class="ado-link-btn"'
          + ' onclick="adoOpenBook(\'' + escapeHtml(w.book_id) + '\')">在书库打开</button>'
        : (SOURCE_LABEL[w.source]
          ? '<div class="ado-work-meta">' + sourceBadge(w.source, w.verified) + '</div>'
          : ''))
      + '</div>';
  }).join('') + '</div>';
}

export function adoOpenBook(bookId) {
  closeAuthorDossier();
  if (typeof window.openBookFromShelf === 'function') {
    window.openBookFromShelf(bookId);
  }
}

/* ══ 声音 ═════════════════════════════════════════════════════════════ */

function traitRow(t, alternates) {
  const conf = pct(t.confidence);
  const strength = pct(t.strength);
  const alt = (alternates || []).length
    ? '<details class="ado-alt"><summary>同一维度的另 '
      + alternates.length + ' 种说法</summary>'
      + alternates.map(function (x) {
        return '<div class="ado-alt-row"><span class="ado-alt-value">'
          + escapeHtml(valueZh(x.dimension, x.value)) + '</span>'
          + '<span>' + escapeHtml(STATUS_LABEL[x.status] || x.status || '')
          + ' · 置信度 ' + pct(x.confidence) + '%</span>'
          + '<span class="ado-alt-text">' + escapeHtml(x.statement) + '</span>'
          + '<button type="button" class="ado-link-btn"'
          + ' onclick="adoOpenTrait(\'' + escapeHtml(x.trait_id)
          + '\')">看证据</button></div>';
      }).join('') + '</details>'
    : '';
  return '<div class="ado-dim">'
    + '<div class="ado-dim-head"><span class="ado-dim-name">'
    + escapeHtml(DIM_LABEL[t.dimension] || t.dimension) + '</span>'
    + '<span class="ado-dim-value">'
    + escapeHtml(valueZh(t.dimension, t.value)) + '</span></div>'
    + '<div class="ado-dim-hint">' + escapeHtml(DIM_HINT[t.dimension] || '') + '</div>'
    + '<div class="ado-dim-statement">' + escapeHtml(t.statement) + '</div>'
    + '<div class="ado-meter" title="置信度 ' + conf + '% · 强度 ' + strength + '%">'
    + '<i style="width:' + conf + '%"></i></div>'
    + '<div class="ado-dim-meta">'
    + '<span>置信度 ' + conf + '%</span>'
    + '<span>证据 ' + t.evidence_count + ' 段</span>'
    + '<span>' + (t.transferable ? '可迁移' : '作品绑定') + '</span>'
    + (t.status === 'provisional' ? '<span class="ado-badge is-warn">暂定</span>' : '')
    + '<button type="button" class="ado-link-btn"'
    + ' onclick="adoOpenTrait(\'' + escapeHtml(t.trait_id) + '\')">看证据</button>'
    + '</div>' + alt + '</div>';
}

function tabVoice(d) {
  const v = d.voice || {};
  if (v.gated && !(v.traits || []).length) {
    return '<div class="rp-empty">'
      + escapeHtml((d.gate && d.gate.note) || '作者材料被剧透门挡住。')
      + '</div>';
  }
  const statusNote = VOICE_STATUS[v.status] || '';
  let html = '';
  if (statusNote) {
    html += '<div class="ado-note">' + escapeHtml(statusNote) + '</div>';
  }
  if (v.gate_mode === 'chapter_only') {
    html += '<div class="ado-note">年份门算不出来（序跋没有带年的语料），'
      + '声音只按你已读到的章节展示；文风结论可能还在后面。</div>';
  }
  if (!(v.traits || []).length && !(v.samples || []).length) {
    return html + '<div class="rp-empty">'
      + escapeHtml(v.note || '这本书还没有声音样本。') + '</div>';
  }
  const byDim = {};
  (v.traits || []).forEach(function (t) {
    (byDim[t.dimension] = byDim[t.dimension] || []).push(t);
  });
  html += '<div class="ado-section-title">文风维度</div>';
  html += '<div class="ado-dims">' + DIM_ORDER.map(function (dim) {
    const rows = (byDim[dim] || []).slice().sort(function (a, b) {
      if (a.status !== b.status) return a.status === 'active' ? -1 : 1;
      return (b.confidence - a.confidence) || (b.strength - a.strength);
    });
    if (!rows.length) {
      return '<div class="ado-dim is-empty"><div class="ado-dim-head">'
        + '<span class="ado-dim-name">' + escapeHtml(DIM_LABEL[dim] || dim) + '</span>'
        + '<span class="ado-dim-value">—</span></div>'
        + '<div class="ado-dim-hint">' + escapeHtml(DIM_HINT[dim] || '') + '</div>'
        + '<div class="ado-dim-meta">这个维度还没有稳定结论</div></div>';
    }
    return traitRow(rows[0], rows.slice(1));
  }).join('') + '</div>';
  if ((v.samples || []).length) {
    html += '<div class="ado-section-title">例证段落</div>'
      + '<div class="ado-samples">' + v.samples.map(function (s) {
        return '<button type="button" class="ado-sample"'
          + ' onclick="adoOpenSample(' + s.corpus_id + ')">'
          + '<span class="ado-sample-text">' + escapeHtml(s.excerpt) + '</span>'
          + '<span class="ado-sample-meta">'
          + '<span class="ado-scene">' + escapeHtml(SCENE_LABEL[s.scene_type] || s.scene_type) + '</span>'
          + (s.chapter_index != null ? '<span>第 ' + (s.chapter_index + 1) + ' 章</span>' : '')
          + sourceBadge((s.source && s.source.kind) === 'book' ? 'corpus' : 'web', false)
          + '</span></button>';
      }).join('') + '</div>';
  }
  if (v.not_shown && v.not_shown.samples) {
    html += '<div class="ado-note">另有 ' + v.not_shown.samples
      + ' 段未展示'
      + (v.not_shown.reason === 'reader_chapter'
        ? ' —— 这些原话在你还没读到的位置。' : '。')
      + '</div>';
  }
  html += '<div class="ado-note is-muted">用于写作参考的入口在 M4 接入；'
    + '现在这里只做档案展示。</div>';
  return html;
}

/* ══ 关系 / 主题 / 来源 ══════════════════════════════════════════════ */

function tabRelations(d) {
  const items = d.relations || [];
  const meta = d.relations_meta || {};
  if (!items.length) {
    return '<div class="rp-empty">'
      + escapeHtml(meta.note || '还没有关系条目。') + '</div>';
  }
  const groups = {};
  items.forEach(function (r) {
    (groups[r.relation_type] = groups[r.relation_type] || []).push(r);
  });
  return Object.keys(groups).map(function (type) {
    return '<div class="ado-section-title">' + escapeHtml(REL_LABEL[type] || type)
      + '<span class="ado-count">' + groups[type].length + '</span></div>'
      + '<div class="ado-rel-list">' + groups[type].map(function (r) {
        const years = (r.year_start || r.year_end)
          ? ((r.year_start || '?') + (r.year_end ? '–' + r.year_end : '')) : '';
        return '<div class="ado-rel">'
          + '<div class="ado-rel-name">'
          + (r.other_author_id
            ? '<button type="button" class="ado-link-btn"'
              + ' onclick="adoOpenAuthor(\'' + escapeHtml(r.other_author_id) + '\')">'
              + escapeHtml(r.other_name) + '</button>'
            : escapeHtml(r.other_name))
          + (r.other_identity_status && r.other_identity_status !== 'resolved'
            ? '<span class="ado-badge is-warn">待确认</span>' : '')
          + '</div>'
          + '<div class="ado-rel-meta">' + escapeHtml([years,
            '置信度 ' + pct(r.confidence) + '%'].filter(Boolean).join(' · ')) + '</div>'
          + (r.statement ? '<div class="ado-rel-statement">'
            + escapeHtml(r.statement) + '</div>' : '')
          + ((r.evidence || []).length
            ? '<button type="button" class="ado-link-btn"'
              + ' onclick="adoOpenRelation(\'' + escapeHtml(r.relation_id) + '\')">看证据</button>'
            : '')
          + '</div>';
      }).join('') + '</div>';
  }).join('');
}

function tabThemes(d) {
  const themes = d.themes || [];
  const reception = d.reception || [];
  if (!themes.length && !reception.length) {
    return '<div class="rp-empty">'
      + escapeHtml((d.notes && d.notes.themes) || '还没有主题陈述。') + '</div>';
  }
  const card = function (c, cls) {
    return '<div class="ado-theme ' + cls + '">'
      + '<div class="ado-theme-top">'
      + (c.theme_key ? '<span class="ado-theme-key">' + escapeHtml(c.theme_key) + '</span>' : '')
      + '<span class="ado-badge">' + (cls === 'is-reception' ? '接受史' : '主题') + '</span>'
      + (c.year ? '<span class="ado-theme-year">' + c.year + '</span>' : '')
      + '</div>'
      + '<div class="ado-theme-text">' + escapeHtml(c.statement) + '</div>'
      + '<div class="ado-dim-meta"><span>置信度 ' + pct(c.confidence) + '%</span>'
      + '<span>' + escapeHtml(STATUS_LABEL[c.status] || c.status || '') + '</span>'
      + ((c.evidence || []).length
        ? '<button type="button" class="ado-link-btn"'
          + ' onclick="adoOpenClaim(\'' + escapeHtml(c.claim_id) + '\')">看证据</button>'
        : '')
      + '</div></div>';
  };
  let html = '';
  if (themes.length) {
    html += '<div class="ado-section-title">主题<span class="ado-count">'
      + themes.length + '</span></div><div class="ado-theme-list">'
      + themes.map(function (c) { return card(c, 'is-theme'); }).join('') + '</div>';
  }
  if (reception.length) {
    html += '<div class="ado-section-title">接受史<span class="ado-count">'
      + reception.length + '</span></div><div class="ado-theme-list">'
      + reception.map(function (c) { return card(c, 'is-reception'); }).join('') + '</div>';
  }
  html += '<div class="ado-note is-muted">主题与接受史是解释，不是事实；'
    + '每条都应能点开来源。</div>';
  return html;
}

function tabSources(d) {
  const s = d.sources || {};
  if (!(s.items || []).length) {
    return '<div class="rp-empty">'
      + escapeHtml(s.note || '还没有来源台账。') + '</div>';
  }
  const counts = s.counts || {};
  let html = '<div class="ado-tl-summary">'
    + '<span class="ado-chip"><b>T1</b><span>' + (counts.tier1 || 0) + '</span></span>'
    + '<span class="ado-chip"><b>T2</b><span>' + (counts.tier2 || 0) + '</span></span>'
    + '<span class="ado-chip"><b>T3</b><span>' + (counts.tier3 || 0) + '</span></span>'
    + '<span class="ado-chip is-warn"><b>旧版</b><span>' + (counts.legacy || 0) + '</span></span>'
    + '</div>';
  html += '<div class="ado-src-list">' + s.items.map(function (src) {
    const host = hostOf(src.url || src.canonical_url);
    return '<button type="button" class="ado-src-item"'
      + ' onclick="adoOpenSource(\'' + escapeHtml(src.source_id) + '\')">'
      + '<span class="ado-src-tier">T' + src.tier + '</span>'
      + '<span class="ado-src-main"><span class="ado-src-title">'
      + escapeHtml(src.title || host || src.kind) + '</span>'
      + '<span class="ado-src-meta">' + escapeHtml([host,
        (src.roles || []).join(' / '), src.kind].filter(Boolean).join(' · ')) + '</span>'
      + '</span>'
      + '<span class="ado-src-nums">' + src.quote_verified + ' 条已核'
      + (src.legacy_only ? ' · ' + src.legacy_only + ' 旧版' : '')
      + (src.hidden_quotes ? ' · ' + src.hidden_quotes + ' 被挡' : '')
      + '</span></button>';
  }).join('') + '</div>';
  if (s.not_shown) {
    html += '<div class="ado-note">另有 ' + s.not_shown + ' 个来源未展示。</div>';
  }
  if ((d.conflicts || []).length) {
    html += '<div class="ado-section-title is-warn">冲突</div>'
      + '<div class="ado-conflicts">' + d.conflicts.map(function (c) {
        return '<div class="ado-conflict"><div class="ado-conflict-target">'
          + escapeHtml(c.statement || (c.target_type + ':' + c.target_id))
          + '</div>' + (c.contradicts || []).map(function (e) {
            return '<blockquote>' + escapeHtml(e.quote || '（无引文）')
              + '</blockquote>';
          }).join('') + '</div>';
      }).join('') + '</div>';
  }
  return html;
}

function hostOf(url) {
  if (!url) return '';
  try { return new URL(url).hostname.replace(/^www\./, ''); }
  catch (_) { return url.slice(0, 48); }
}

/* ══ 抽屉 ═════════════════════════════════════════════════════════════ */

function evidenceHtml(ev) {
  const rows = (ev || []).map(function (e) {
    const legacy = e.relation === 'legacy_url_only';
    const quote = legacy ? '旧版 URL 证据，正文未核验'
      : (e.quote || '（无引文）');
    return '<div class="ado-quote' + (legacy ? ' is-legacy' : '') + '">'
      + '<blockquote>' + escapeHtml(quote) + '</blockquote>'
      + '<div class="ado-quote-meta">'
      + (e.locator ? '<span>' + escapeHtml(e.locator) + '</span>' : '')
      + '<span>' + (legacy ? '未核验'
        : escapeHtml(e.relation || 'supports')) + '</span>'
      + (e.target_statement ? '<span>' + escapeHtml(e.target_statement) + '</span>' : '')
      + '</div></div>';
  }).join('');
  return rows || '<div class="ado-note">这里还没有可展示的引文。</div>';
}

function openDrawer(title, bodyHtml) {
  const drawer = document.getElementById('adoDrawer');
  const scrim = document.getElementById('adoScrim');
  const t = document.getElementById('adoDrawerTitle');
  const b = document.getElementById('adoDrawerBody');
  if (!drawer || !b) return;
  _lastFocus = document.activeElement;
  if (t) t.textContent = title || '来源';
  b.innerHTML = bodyHtml;
  drawer.hidden = false;
  if (scrim) scrim.hidden = false;
  _drawerOpen = true;
  const close = drawer.querySelector('.ado-drawer-close');
  if (close) close.focus();
}

export function closeAuthorDrawer() {
  const drawer = document.getElementById('adoDrawer');
  const scrim = document.getElementById('adoScrim');
  if (drawer) drawer.hidden = true;
  if (scrim) scrim.hidden = true;
  _drawerOpen = false;
  if (_lastFocus && typeof _lastFocus.focus === 'function') {
    try { _lastFocus.focus(); } catch (_) { /* 元素已不在 */ }
  }
  _lastFocus = null;
}

export function adoOpenSource(sourceId) {
  const s = ((_dossier && _dossier.sources && _dossier.sources.items) || [])
    .find(function (x) { return x.source_id === sourceId; });
  if (!s) return;
  const meta = [s.title, 'T' + s.tier, s.kind,
    (s.roles || []).join(' / '), s.status,
    s.retrieved_at ? new Date(s.retrieved_at).toLocaleDateString() : '']
    .filter(Boolean).map(escapeHtml).join(' · ');
  const body = '<div class="ado-drawer-meta">' + meta + '</div>'
    + (s.url ? '<a class="ado-drawer-url" href="' + escapeHtml(s.url)
      + '" target="_blank" rel="noopener">' + escapeHtml(s.url) + '</a>' : '')
    + '<div class="ado-note">' + s.quote_verified + ' 条引文已核'
    + (s.legacy_only ? ' · ' + s.legacy_only + ' 条只有旧版 URL' : '')
    + (s.hidden_quotes ? ' · ' + s.hidden_quotes + ' 条被剧透门挡住' : '')
    + '</div>'
    + evidenceHtml(s.evidence);
  openDrawer(s.title || hostOf(s.url) || '来源', body);
}

export function adoOpenRecord(id) {
  const t = (_dossier && _dossier.timeline) || {};
  const rec = (t.work || []).concat(t.life || []).find(function (r) {
    return String(r.id) === String(id);
  });
  if (!rec) return;
  openDrawer(rec.statement, '<div class="ado-drawer-meta">'
    + escapeHtml([rec.year, rec.kind, SOURCE_LABEL[rec.source] || rec.source]
      .filter(Boolean).join(' · ')) + '</div>'
    + (rec.url ? '<a class="ado-drawer-url" href="' + escapeHtml(rec.url)
      + '" target="_blank" rel="noopener">' + escapeHtml(rec.url) + '</a>' : '')
    + evidenceHtml(rec.evidence));
}

export function adoOpenSample(corpusId) {
  const v = (_dossier && _dossier.voice) || {};
  const s = (v.samples || []).find(function (x) {
    return String(x.corpus_id) === String(corpusId);
  });
  if (!s) return;
  openDrawer('例证段落 · ' + (SCENE_LABEL[s.scene_type] || s.scene_type),
    '<blockquote class="ado-quote">' + escapeHtml(s.excerpt) + '</blockquote>'
    + '<div class="ado-drawer-meta">'
    + escapeHtml([s.chapter_index != null ? '第 ' + (s.chapter_index + 1) + ' 章' : '',
      s.source && s.source.tier ? 'T' + s.source.tier : '',
      s.source && s.source.kind === 'book' ? '书里' : '网页'].filter(Boolean).join(' · '))
    + '</div>');
}

export function adoOpenTrait(traitId) {
  const v = (_dossier && _dossier.voice) || {};
  const t = (v.traits || []).find(function (x) { return x.trait_id === traitId; });
  if (!t) return;
  openDrawer(t.statement, '<div class="ado-drawer-meta">'
    + escapeHtml([DIM_LABEL[t.dimension] || t.dimension,
      valueZh(t.dimension, t.value),
      '置信度 ' + pct(t.confidence) + '%',
      t.transferable ? '可迁移' : '作品绑定'].filter(Boolean).join(' · '))
    + '</div>' + evidenceHtml(t.evidence));
}

export function adoOpenRelation(id) {
  const r = ((_dossier && _dossier.relations) || []).find(function (x) {
    return x.relation_id === id;
  });
  if (!r) return;
  openDrawer(r.other_name, '<div class="ado-drawer-meta">'
    + escapeHtml([REL_LABEL[r.relation_type] || r.relation_type, r.statement]
      .filter(Boolean).join(' · ')) + '</div>' + evidenceHtml(r.evidence));
}

export function adoOpenClaim(id) {
  const all = ((_dossier && _dossier.themes) || [])
    .concat((_dossier && _dossier.reception) || []);
  const c = all.find(function (x) { return x.claim_id === id; });
  if (!c) return;
  openDrawer(c.statement, evidenceHtml(c.evidence));
}

export function adoOpenAuthor(authorId) {
  // 关系里的「对方」是作者级的人，不是这本书里的位置 —— 不带书，
  // 否则会拿当前书的门去卡另一个人的档案。
  openAuthorDossier('', authorId);
}

/* Esc 关闭 + 焦点陷阱。抽屉是右侧栏，但仍然不能让人把焦点 Tab 到背后去。 */
document.addEventListener('keydown', function (e) {
  if (!_drawerOpen) return;
  if (e.key === 'Escape') { e.preventDefault(); closeAuthorDrawer(); return; }
  if (e.key !== 'Tab') return;
  const drawer = document.getElementById('adoDrawer');
  if (!drawer) return;
  const focusable = drawer.querySelectorAll(
    'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])');
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
});

/* 「用于写作参考」：切到写作页，把作者交给 WritingUI。
 *
 * 写作页可能还没选作品——WritingUI.setStyleRef 会先记住，等 selectWork
 * 完成后落进 works.style_profile。这里不直接写库，编排权留给写作服务。 */
function adoUseForWriting() {
  const author = (_dossier && _dossier.author) || {};
  const authorId = _authorId;
  const name = author.canonical_name || '';
  if (!authorId) return;
  closeAuthorDossier();
  if (typeof window.switchTab === 'function') window.switchTab('write');
  const apply = function () {
    if (window.WritingUI && typeof window.WritingUI.setStyleRef === 'function') {
      window.WritingUI.setStyleRef(authorId, name);
      return;
    }
    setTimeout(apply, 60);
  };
  setTimeout(apply, 0);
}

/* 每本书切换后重新绑定；事件委托挂在 document 上，不会重复。 */
window.openAuthorDossier = openAuthorDossier;
window.closeAuthorDossier = closeAuthorDossier;
window.adoFallbackTimeline = adoFallbackTimeline;
window.adoSelectTab = adoSelectTab;
window.adoOpenTimeline = adoOpenTimeline;
window.adoToggleUndated = adoToggleUndated;
window.adoOpenBook = adoOpenBook;
window.adoOpenSource = adoOpenSource;
window.adoOpenRecord = adoOpenRecord;
window.adoOpenSample = adoOpenSample;
window.adoOpenTrait = adoOpenTrait;
window.adoOpenRelation = adoOpenRelation;
window.adoOpenClaim = adoOpenClaim;
window.adoOpenAuthor = adoOpenAuthor;
window.adoUseForWriting = adoUseForWriting;
window.closeAuthorDrawer = closeAuthorDrawer;
