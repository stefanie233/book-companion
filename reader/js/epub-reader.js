/* Book Companion — native EPUB rendering via epub.js (scrolled flow).

   Display layer only. The DB chunk model (chapter_index, chunk_index)
   stays the semantic authority — memory / graph / LLM / annotations all
   keep using it. The visible viewport text is mapped back through the
   /api/sync fingerprint bridge to (chapter, chunk); the viewport's CFI
   is stored alongside it as the restore anchor.

   ── 位置模型：CFI 权威 + 用户意图唯一化 ────────────────────────────
   旧实现从"视口几何"反推位置，再用三套共享时序闸门
   （_suppressUntil / _locating / _locateSeq）判断"这次滚动是不是程序性的"。
   但 fork 的 counter() 补偿产生的 scrollBy(…, true) 与用户滚动在 DOM 层
   完全同构，ignore 标志只消费一次 —— 这个判断原理上做不到，n 个写入源 ×
   n 个条件 = O(n²) 组合，修好 A 场景就激活 B 场景。

   新实现让这个判断**变得不必要**：
     · 唯一写入口是 _commitViewport()，只有「用户意图窗口」内的调用才落库；
     · 程序性位移的代码路径（resize 重排 / display+收敛 / 补偿）里
       **根本不调用它** —— 不是"调用后放弃"，是"没有这条调用"；
     · 两个显式窗口取代三套闸门：_userIntentUntil（人类输入余辉）、
       _programmaticUntil（显式定位操作期间）。二者取与，且用户一旦真
       滚动就立刻清零程序窗口，把控制权交还用户。

   副作用：硬刷新 / 开关控制台 / 开关交互页 / 切页四种场景**没有写入路径**，
   进度不再抖动 —— 不是"被抑制了"，是"不存在"。

   Mode is gated by S.renderMode ('native' | 'chunk'); any failure during
   init falls back cleanly to the chunk view. */

import { S } from './state.js';

let book = null;                    // epub.js Book
let rendition = null;               // epub.js Rendition
let destroyed = true;
let _initGen = 0;                   // init 代数：并发切书时过期的 init 自动作废
let _nativeBookId = null;           // 当前 rendition 归属的 bookId（进度上报以此为准）

// ── Position model (CFI-authoritative) ───────────────────────────────
let _committedCfi = '';             // 最近一次成功落库的视口 CFI（去重槽）
let _committedPosKey = '';          // CFI+top/focus section 去重键：边界处 CFI 不变、
                                    // section 变了也必须上报
let _lastMismatchKey = '';          // 位置错位告警去重（不是每 300ms 刷一条）
let _viewportCfi = '';              // 最近一次用户所处视口 CFI（切页/重排的恢复锚）
let _lastDbPos = null;              // last matched {chapter, chunk}（hint + UI）
let _lastChunkInfo = null;          // {total_chunks, chapter_title} from /chunk

// ── Report gates（两个显式窗口，不做"程序滚动"的推断）────────────────
let _userIntentUntil = 0;           // ms — 人类输入后的可上报窗口
let _programmaticUntil = 0;         // ms — 显式定位/重排进行中的窗口
let _throttleUntil = 0;             // ms — 上报节流
let _tickUntil = 0;                 // ms — 滚动抓取节流（与上报节流解耦）
let _lastExtractEmpty = false;      // 提取失败状态去重（只在变化时告警）
let _lastSyncFail = false;          // 网络失败状态去重
let _lastMatchMiss = false;         // 指纹未命中状态去重
let _engageBound = false;           // 用户交互监听已挂全局
let _programTimer = 0;              // 程序定位窗口续期定时器（display 长扫描期间持续静默）

// ── 恢复定位代数 ─────────────────────────────────────────────────────
// 恢复类定位（restoreViewport 的切页恢复、_armLateVerify 的迟到复核）都是
// 「把视口放回上次读到的地方」，且**都不是立刻执行**——前者等宿主布局稳定
// （≥2 帧），后者等补载静默（~2s）。用户点击笔记 / 图谱节点发起的显式跳转
// 是**更新的意图**：若不作废这些待触发的恢复，它们会在跳转落地后把视口
// 拉回原位（实测：笔记跳转 2 帧后被 restoreViewport 覆盖，表现为"点了没
// 反应"；迟到复核则会隔 2s 再覆盖一次）。显式跳转前调
// invalidatePendingRestore()，恢复路径各自比对自己捕获的代数即可。 */
let _restoreGen = 0;

/** 作废所有在途/待触发的恢复定位。显式跳转（笔记 / 图谱）前调用。 */
export function invalidatePendingRestore() {
  _restoreGen++;
}

/** 等阅读区真正可见（宿主拿到非零尺寸）再落跳转。返回是否等到（超时也继续）。
 *
 *  为什么必需：从别的 tab 点跳转时，`switchTab('reader')` 只是把
 *  `#page-reader` 的 `display:none` 摘掉 —— **同一个 tick 里浏览器还没布局**，
 *  `#bookPageNative` 仍是 0×0。此时 `rendition.display(cfi)` 是在零尺寸舞台上
 *  定位：section 渲染了，滚动偏移却按零高视口算；等浏览器补上布局，
 *  `resizeNative` 又因"宽度与 _lastResizeW 相同"跳过重排，于是没有任何一步
 *  再把视口纠正回来 —— 视口停在跳转前的位置。
 *  实测（点笔记卡片）：点击瞬间 host=0×0，20s 后 host=365×156 而视口纹丝不动；
 *  同一段代码在阅读页内同步调 jumpToCfi 则 8–27ms 精确落点。这也解释了为什么
 *  症状是**偶发**的：display() 是异步的，若浏览器恰好在其间完成布局就正常。
 *
 *  ⚠️ 必须让出**真实帧**，不能只做一次同步读：读 `clientWidth` 会强制同步
 *  重排，切 tab 后紧接着读就已经是 365×156（同步判断会立刻返回 true），但
 *  此刻 epub 的 view 容器还没走完这一帧的布局/挂载 —— 实测"同步读一次就跳"
 *  仍然失败，而同一段代码中间隔了几帧（多一次 CDP 往返）就精确落点。
 *  所以这里按 `_waitLayoutStableThen` 同款：连续 2 帧读到非零尺寸才放行。 */
export async function whenHostVisible(maxMs = 1500) {
  const t0 = Date.now();
  const first = document.getElementById('bookPageNative');
  // host.hidden = native 未启用（chunk 视图）→ 本函数不适用，立即放行，
  // 免得非 EPUB 书的跳转白等一个超时。
  if (!first || first.hidden) return false;
  let stable = 0;
  for (;;) {
    if (destroyed || !rendition) return false;
    const host = document.getElementById('bookPageNative');
    if (!host || host.hidden) return false;
    stable = (host.clientWidth > 0 && host.clientHeight > 0) ? stable + 1 : 0;
    if (stable >= 2) return true;
    if (Date.now() - t0 > maxMs) return false;
    await new Promise(r => requestAnimationFrame(r));   // 让出真实帧
  }
}

// ── 链路计数器（诊断用，零副作用）───────────────────────────────────
// 为什么需要：DevTools 的 console.table 在"选中→复制→粘贴"时经常整块丢失
// （实测用户贴回来的输出里表格只剩标题行），而滚动上报的断点恰好分布在
// 4 个环节上——事件有没有到、闸门有没有放行、抓取有没有拿到视口、请求有
// 没有发出。计数以**纯数字**形式由 __reader.audit(ms) 返回一行 JSON 字符串，
// 一次滚动就能把断点钉死，不再依赖控制台表格的可复制性。
const _diag = {
  tick: 0,           // _onScrollTick 被调用次数（事件层）
  tickDom: 0,        // ↳ 来自 document 捕获通道
  tickMgr: 0,        // ↳ 来自 manager 的 scroll/scrolled 事件
  tickProg: 0,       // ↳ 程序窗口内被静默
  tickThr: 0,        // ↳ 抓取节流期内被跳过
  tickDead: 0,       // ↳ 模块已销毁（destroyed / 无 rendition）
  commit: 0,         // 进入 _commitViewport（裁决层）
  dropDead: 0,       // ↳ 模块已销毁
  dropNoIntent: 0,   // ↳ 意图窗口未开（userIntent=0）
  dropProg: 0,       // ↳ 程序窗口内
  dropThr: 0,        // ↳ 上报节流期内
  dropNoVp: 0,       // ↳ _captureViewport 返回 null（抓取层）
  dropNoText: 0,     // ↳ 视口无文本
  dropDedup: 0,      // ↳ CFI 与上次落库值相同
  post: 0,           // 发出 /api/sync（网络层）
  postOk: 0,         // ↳ 命中
  postMiss: 0,       // ↳ 未命中
  postErr: 0,        // ↳ 请求异常
  displayTimeout: 0, // ↳ display() promise 超时未 resolve（效果已落地，见 _displayWithTimeout）
  lastCfi: '',       // 最近一次抓到的视口 CFI
  lastText: '',      // 最近一次抓到的视口文本
  lastTop: 0,        // 最近一次抓取时的 scrollTop
};

const REPORT_THROTTLE_MS = 400;
// 滚动抓取节流：_captureViewport 要遍历当前 section 的文本节点，不能每帧跑。
// 与 _throttleUntil（上报节流，由 _commitViewport 在真正放行时设置）解耦 ——
// 后者在"CFI 未变被去重挡回"时不更新，单靠它挡不住高频抓取。
const SCROLL_TICK_MS = 250;
// 人类输入的"意图余辉"：滚轮/触摸/按键/按下之后这段时间内的 scroll 视为
// 用户意图。取 700ms —— 覆盖惯性滚动收尾，又短于两次滚轮手势的典型间隔，
// 免得用户停手后程序补偿仍落在窗口内。
const USER_INTENT_MS = 700;
// 人类输入事件集合。程序性滚动永远不产生这些事件 —— 用它做"用户意图"的
// 判据是"已知"而非"推断"。touchmove 覆盖移动端惯性滚动；pointerdown
// 覆盖滚动条拖拽与点击。
const USER_INPUT_EVENTS = ['wheel', 'touchstart', 'touchmove', 'keydown', 'pointerdown'];
// 显式定位/重排的操作窗口。只用于挡住"用户点了按钮（pointerdown 会点亮
// 意图窗口）→ 程序随后重排"这条组合路径；用户一旦真的滚动，_markUserIntent
// 会立刻清零本窗口（交还控制权）。取 2500ms 覆盖 display + 收敛循环往返。
const PROGRAMMATIC_MS = 2500;
// 定位收敛循环节拍。原 200/300 固定值给每次开书强加 ≥700ms 纯 sleep——
// 实测两次都是 round 2 收敛（200ms 时布局已稳），说明 continuous
// 前置渲染的稳定窗口远小于 200ms。改 80ms 节拍自适应：布局未稳时 target
// 不一致自动多轮（每轮只多 80ms），稳定即收敛；校验窗口 120ms 防迟到的
// 补偿事件把视口推偏。收敛判定逻辑与轮次上限（14）不变。
const _CONVERGE_TICK_MS = 80;
const _CONVERGE_SETTLE_MS = 120;

// ── Anchor locating ─────────────────────────────────────────────────
let _sectionCache = null;           // [{index, text}] lazily built from spine
let _restoreChain = null;           // serialize programmatic jumps
let _themeObserver = null;          // re-apply theme when root CSS vars change
let _revealTimer = 0;               // 无感空降兜底定时器（定位卡住时强制揭开）
let _anchorGen = 0;                 // 程序定位代数：连续跳转/重排只留最新一轮

// ── Scroll reporter（订阅 epub.js manager 的 scrolled 事件）──────────
let _scrollScroller = null;         // 滚动容器（读 scrollTop 用）
let _scrollManager = null;          // manager 引用（卸载事件订阅用）
let _scrollHandler = null;          // manager 'scrolled' 监听
let _scrollDomHandler = null;       // DOM scroll 通道监听（与 manager 事件同等）

// ── 同书切页保活状态 ────────────────────────────────────────────────
// switchTab 只切 .page 的 display（切走后 scrollTop 丢失、容器塌成 0 尺寸），
// book/rendition/iframe 本来就全程存活——切回时走快路径：用 CFI 恢复视口
// 即可，免掉 fetch/解包/渲染/定位全链。_revealDone=true 才允许快路径：
// 上轮定位链未走完（遮罩未揭）时切回，仍走全链重建（行为与现状一致）。
let _revealDone = false;       // 定位收敛完成、遮罩已揭
let _lastResizeW = 0;          // 上次 resizeNative 生效尺寸（同尺寸跳过用）
let _lastResizeH = 0;
let _coldRestoring = false;    // 冷启动恢复窗口（initNative 建 rendition → reveal）
let _deferredResize = false;   // 冷启动窗口内被推迟的重排请求（收尾时补一次）
let _lastRenderedAt = 0;       // 最近一次 section 渲染完成的时刻（判"补载是否静默"）
let _hostResizeObs = null;     // #bookPageNative 尺寸观察（见 _installHostResizeObserver）
let _hostResizeRaf = 0;        // 观察回调的 rAF 合并句柄
let _hostResizePaused = false; // 拖拽分隔条期间暂停观察
let _pendingAnchorCfi = null;  // resize 链进行中的有效锚（防重入抓到 clear 后的 0 位）

let _engageDocs = new Set();   // 已绑定的 iframe document（销毁时清空）

// ── Public API ──────────────────────────────────────────────────────

export function isNative() {
  return !destroyed && !!rendition && !!book;
}

// ── Annotation-layer bridge（annotations.js 经此访问渲染内部）────────
// 标注层需要 rendition（selected/annotations API）与 book（CFI→Range），
// 但二者是模块私有且随切书/销毁轮换——经桥转发，切书后引用自动指向新书。
const _bookReadyCbs = [];       // [(bookId)] initNative 成功后触发
const _sectionRenderedCbs = []; // [(contents)] 每个 section iframe 渲染后触发

export function getRendition() { return destroyed ? null : rendition; }
export function getBook() { return destroyed ? null : book; }
export function getNativeBookId() { return destroyed ? null : _nativeBookId; }
export function onBookReady(cb) { _bookReadyCbs.push(cb); }
export function onSectionRendered(cb) { _sectionRenderedCbs.push(cb); }

function _notifyBookReady() {
  for (const cb of _bookReadyCbs) {
    try { cb(_nativeBookId); }
    catch (e) { console.warn('[epub-reader] bookReady 回调异常:', e); }
  }
}

function _notifySectionRendered(view) {
  const contents = view && view.contents;
  if (!contents) return;
  for (const cb of _sectionRenderedCbs) {
    try { cb(contents); }
    catch (e) { console.warn('[epub-reader] sectionRendered 回调异常:', e); }
  }
}

// 面板收起/展开、splitter 拖拽只改宿主容器宽度，不触发 window resize——
// epub.js 只监听 window，宿主变宽后 iframe 排版仍停留旧宽度：右侧露出
// 同色宿主背景（像被遮盖）、行尾文字卡在旧边界被切半。宿主尺寸变化后
// 由 app.js 调此函数重排。宿主不可见（如切到其他 tab）时 0 尺寸跳过。
//
// 重排必须"锚定视口内容"：宽度变化 → 每段行数变化 → 内容总高度大变，
// 而 rendition.resize 后 scrollTop 保持像素值——同一 scrollTop 对应的
// 内容整体漂移（变宽=内容变短=视口"前进"），实测收起交互页一次阅读
// 进度就从 4 抖到 24。锚用 CFI（视口顶 y → 文本节点 → cfiFromRange），
// 不用文本指纹：指纹的降级前缀 indexOf 会命中章内更早的重复出现，
// 锚定被"稳定地"收敛到章首（实测）。重排全程静默不落库。
export function resizeNative() {
  if (destroyed || !rendition) return;
  const host = document.getElementById('bookPageNative');
  if (!host || !host.clientWidth || !host.clientHeight) return;

  // ── 冷启动恢复窗口：initNative 正在按当前宿主尺寸建 rendition 并
  //    display(cfi) 恢复进度，此刻的重排请求是"宿主由 hidden 变可见 /
  //    内容撑开后滚动条出现"的副产物，不是用户改尺寸。
  //    放它进来会与 initNative 的 display(cfi) **并发竞争**（实测两条
  //    display 互相打断，落点偏 10 个 DOM 元素 = /4/192 → /4/182，并额外
  //    起一条收敛循环 → 用户感知"恢复抖动 + 收敛缓慢"）。
  //    处理：只记一个"有请求被推迟"，冷启动收尾时按终态尺寸补一次。
  if (_coldRestoring) { _deferredResize = true; return; }

  // 宽度未变则无需重排：`flow: 'scrolled'` 下 iframe 排版只有**宽度**决定
  // 换行（高度只决定可视窗口大小，不参与行宽计算）。而 fork 的
  // `manager.resize` 一旦发现 stage 尺寸变化就 `clear()`——清空全部 view +
  // `scrollTo(0,0)`。冷启动/滚动条出现时宿主高度常有 20px 级抖动
  // （实测 737→717），仅因高度变化就 clear() 会把刚恢复好的视口瞬间清零
  // → 视觉上跳到书首再 display 回来，即"幽灵滚动"。
  if (host.clientWidth === _lastResizeW) {
    _lastResizeH = host.clientHeight;
    _kickContinuousFill();
    return;
  }

  // 至此宽度确实变了 → 需要真重排。
  // ── 布局稳定门：宿主尺寸还在重排中就抓锚，抓到的是布局中间态 ──
  // 症状（实测 2026-09-14，房思琪 ch5）：展开交互页时 contentLeft 从
  // 1117px 收缩到 791px。同步调用的 resizeNative 首帧读到的是收缩中
  // 的中间宽度（flex 重排跨 1-2 帧），epub.js 按中间值排版后视口内容
  // 整体错位（约 71% 位移），其后的滚动事件带着错位视口的真实文本穿过
  // 意图窗口落库 —— ck=29 变 ck=14，恰好 ≈ 半本。
  // 修法：等 clientWidth 连续 2 轮 rAF 不变（布局到达终态）再执行重排；
  // 等待期 + 重排全程挂 _programmaticUntil（每帧续期），零上报。
  const w0 = host.clientWidth, h0 = host.clientHeight;
  _programmaticUntil = Date.now() + PROGRAMMATIC_MS;   // 等待期即程序窗口
  _waitLayoutStableThen(host, w0, h0, () => {
    _resizeNativeLocked(host, w0, h0);
  });
}

/** 布局稳定后的实际重排（resizeNative 的续体）。
 *  w0/h0 是进入等待门时的首帧读数：若与终态不同（等待期间布局又动了），
 *  以终态为准再等一轮。终态下本次调用直接命中同尺寸跳过或执行重排。 */
function _resizeNativeLocked(host, w0, h0) {
  if (destroyed || !rendition) return;
  if (!host.clientWidth || !host.clientHeight) return;
  // 布局在等待期间又动了（极少见，拖拽/字体加载）：以新值再等一轮稳定。
  // 不递归 resizeNative——那会把"同尺寸跳过"与程序窗口重置一遍，且
  // 中间态尺寸可能命中过期的 _lastResizeW 造成误跳过。
  // ⚠️ 只比宽度：scrolled 下高度不影响换行，为高度抖动再等一轮纯属浪费。
  if (host.clientWidth !== w0) {
    const w1 = host.clientWidth, h1 = host.clientHeight;
    _waitLayoutStableThen(host, w1, h1, () => { _resizeNativeLocked(host, w1, h1); });
    return;
  }
  // 同尺寸跳过（等待期间可能已被 ResizeObserver 链路处理过）：切页往返
  // （switchTab→setChatPanelVisible→resizeNative）尺寸未变，而 fork 的
  // manager.resize 内部 clear() 会清空视图触发全量重渲染——同尺寸重排
  // 毫无意义，直接跳过。⚠️ 放在等待门之后而非入口：入口处的同尺寸跳过
  // 会把"布局还在动、读数恰好等于上次生效值"的中间态误判为无需重排。
  // ⚠️ 只比宽度：仅高度变化（滚动条出现/消失）就 clear() 是"幽灵滚动"的
  // 直接成因（视口被 scrollTo(0,0) 清零后再 display 回来）。
  if (host.clientWidth === _lastResizeW) {
    _lastResizeH = host.clientHeight;
    _kickContinuousFill();
    return;
  }

  // 锚：优先复用重入窗口内的旧锚（上一次 resize 链还在跑时视口已被 clear
  // 推到 0，重新抓锚会抓到书首）；其次用 _viewportCfi（用户最近所处位置，
  // 由 _commitViewport / jump 落点维护）——它是语义坐标，不会像几何抓锚
  // 那样被"clear 后残留的布局中间态"污染。
  const cfi = _pendingAnchorCfi || _viewportCfi || _captureViewportAnchorCfi() || null;
  _pendingAnchorCfi = cfi;
  // 重排是显式程序操作：全程 _programmaticUntil 覆盖，其间不做任何落库。
  // （旧实现在这里只有 800ms 静默闸门 + 挂 _locating，但 display 异步常超
  // 过该窗口，中间态就穿过过期闸门进了 DB——见 docs/reader-position-redesign.md）
  _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
  _programTimerArm();                // display 长扫描期间持续静默
  try { rendition.resize(host.clientWidth, host.clientHeight); }
  catch (_) { /* 重排失败不干扰阅读 */ }
  _lastResizeW = host.clientWidth; _lastResizeH = host.clientHeight;
  if (!cfi) { _programTimerClear(true); _kickContinuousFill(); return; }
  // fork 的 manager.resize 内部 clear()（清空视图 + scrollTo(0,0)，continuous
  // 未 override）——resize 后靠 scroll 事件渐进渲染书首，直接 _scrollToCfi
  // 与之打架（实测漂移很远 + 收敛循环不稳定）。改走 display(cfi) 让
  // manager 自己重渲染目标 section（与批注跳转同一条路径），再段落级精修。
  rendition.display(cfi)
    .catch(() => { /* display 失败仍尝试精修 */ })
    .then(() => _scrollToCfi(cfi))
    .finally(() => {
      if (_pendingAnchorCfi === cfi) _pendingAnchorCfi = null;
      _programTimerClear(true);      // 重排定位收敛，允许用户随后滚动上报
    });
}

/** 切走阅读 tab 前暂存视口锚（app.js 的 switchTab 调用）。
 *
 *  为什么必需：switchTab 只切 .page 的 CSS display，容器在 display:none 期间
 *  塌成 0 尺寸、滚动容器的 scrollTop 丢失。旧实现只在 initNative 快路径里
 *  恢复了像素 scrollTop，而切页根本不经过 initNative —— 视口塌到顶部后
 *  scrolled 事件上报，DB 里的真实进度被 (ch=0, ck=0) 覆盖，表现为"切页后
 *  进度收敛到 1/N"。现在改为在切走前把视口 CFI 存下来。 */
export function stashViewport() {
  if (destroyed || !rendition) return;
  const vp = _captureViewport();
  const cfi = (vp && vp.cfi) || _captureViewportAnchorCfi();
  if (cfi) _viewportCfi = cfi;
}

/** 切回阅读 tab 后按 CFI 恢复视口（app.js 的 switchTab 调用）。
 *
 *  display(cfi) 是确定性的：给它什么坐标，视口就落在哪。相较旧的"逐帧写
 *  scrollTop + 读回校验 + 20 帧不收敛降级"，这里不需要收敛判定，也不需要
 *  静默闸门 —— 程序路径本就不产生落库。
 *
 *  ⚠️ 同样要过布局稳定门：切回瞬间 host 尺寸可能仍是重排中间值，此时
 *  display(cfi) 后 _scrollToCfi 的收敛循环按中间布局计算目标，收敛到错误
 *  位置后布局才稳定——视口停在错误处（2026-09-14 实测切页恢复同样漂移
 *  约一个内容区宽度差）。等终态后再 display+精修。 */
export function restoreViewport() {
  if (destroyed || !rendition) return;
  const cfi = _viewportCfi;
  if (!cfi) return;
  const host = document.getElementById('bookPageNative');
  if (!host || !host.clientWidth || !host.clientHeight) return;
  _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
  const myGen = _restoreGen;      // 显式跳转会使本轮恢复作废（见 invalidatePendingRestore）
  const w0 = host.clientWidth, h0 = host.clientHeight;
  _waitLayoutStableThen(host, w0, h0, () => {
    if (destroyed || !rendition) return;
    if (myGen !== _restoreGen) return;
    if (!host.clientWidth || !host.clientHeight) return;
    // display:none 期间容器尺寸塌缩，epub.js 内部布局随之失效——尺寸有变时
    // 先重建布局，再定位。
    // ⚠️ 只比宽度：scrolled 下排版宽度决定换行，高度变化（滚动条等）不影响；
    // 而 `rendition.resize` 在 stage 尺寸变化时会 `clear()`（清空 view +
    // scrollTo(0,0)）——为 20px 高度抖动重建布局，换来的是视口被清零后
    // 再 display 回来的"幽灵滚动"。
    if (host.clientWidth !== _lastResizeW) {
      try { rendition.resize(host.clientWidth, host.clientHeight); }
      catch (_) { /* 重排失败仍尝试定位 */ }
      _lastResizeW = host.clientWidth; _lastResizeH = host.clientHeight;
    } else {
      _lastResizeH = host.clientHeight;   // 仅记录：高度不触发重建
    }
    // 视口顶已在目标锚上（调用方的 setChatPanelVisible 刚触发过 resizeNative
    // 并锚定成功）→ 不重复定位，省掉一次重渲染。比较用锚点键（元素路径），
    // 文本偏移范围可能因抓取时机不同。
    const cur = _captureViewport();
    if (cur && cur.cfi && _cfiAnchorKey(cur.cfi) === _cfiAnchorKey(cfi)) {
      _kickContinuousFill();
      return;
    }
    _programTimerArm();              // display 长扫描期间持续静默
    rendition.display(cfi)
      .catch(() => { /* 定位失败保留当前视口 */ })
      .then(() => _scrollToCfi(cfi))
      .finally(() => _programTimerClear(true));
  });
}

/** 观察 #bookPageNative 的真实尺寸变化，自动重排 epub。
 *
 *  为什么需要它：收起/展开交互页时，调用方（setChatPanelVisible /
 *  toggleChatPanel）会在 class 切换后**同步**调用 resizeNative()。此刻浏览器
 *  尚未完成布局，host.clientWidth 可能读到中间值/旧值 W1，于是
 *  `_lastResizeW = W1`；而 epub.js 的 manager.resize 内部也有同尺寸跳过
 *  （`_stageSize.width === n.width && _stageSize.height === n.height → 直接
 *  返回，不 clear/不 updateLayout`），两处跳过叠加后，布局真正稳定到 W2 时
 *  **再没有任何代码触发重排**——epub 内部滚动容器停留在 W1，其滚动条就悬在
 *  旧位置的内部（实测：棕色阅读区宽 1764px，滚动条卡在 x≈1192 处，右侧
 *  600+px 全是无法利用的空白），表现为"滚动条留滞在原地占用页面空间"。
 *
 *  ResizeObserver 在布局完成后触发，读到的是稳定尺寸，从根上兜住所有
 *  "调用方时机不对 / 容器尺寸由外部因素改变（窗口缩放、devtools 停靠、
 *  字体加载完成顶开容器等）"的场景。rAF 合并同一批尺寸变更，避免
 *  clear()→重渲染的级联被触发多次。 */
function _installHostResizeObserver() {
  const host = document.getElementById('bookPageNative');
  if (!host || typeof ResizeObserver !== 'function') return;
  if (_hostResizeObs) { _hostResizeObs.disconnect(); _hostResizeObs = null; }
  _hostResizeObs = new ResizeObserver(() => {
    if (destroyed) return;
    if (_hostResizePaused) return;     // 拖拽分隔条期间暂停（见 suspendHostResizeObserver）
    const _h = document.getElementById('bookPageNative');
    // rAF 合并：同一帧内的多次尺寸回调只重排一次
    if (_hostResizeRaf) return;
    _hostResizeRaf = requestAnimationFrame(() => {
      _hostResizeRaf = 0;
      if (destroyed || _hostResizePaused) return;
      // 尺寸与上次生效值一致时 resizeNative 自己会跳过，这里无需重复判断
      resizeNative();
    });
  });
  _hostResizeObs.observe(host);
}

/** 等宿主布局到达终态（clientWidth 连续 2 轮 rAF 不变）后执行 cont。
 *
 *  为什么需要：面板开合 class 切换与 flex 改写后的 1-2 帧内，clientWidth
 *  仍是中间值；epub.js resize 用它排版，行数/总高全错，视口漂移。
 *  ResizeObserver 在"布局完成后"触发的是**变化后那一帧**，而本函数要的
 *  是"变化后的第二个稳定帧"，两者互补。
 *
 *  · 全程每帧续期 _programmaticUntil —— 等待期属于显式重排操作；
 *  · 尺寸持续变化超过 30 轮（约 500ms）按当前值放行，不永久挂住
 *    （极端拖拽场景退化为旧行为，可接受）；
 *  · destroyed 时停止轮询、cont 不执行。
 *
 *  返回 true 表示已启动等待（cont 将异步执行），false 表示已销毁。 */
function _waitLayoutStableThen(host, w0, h0, cont) {
  if (destroyed) return false;
  let lastW = w0, lastH = h0, stable = 0, frames = 0;
  const tick = () => {
    if (destroyed) return;
    _programmaticUntil = Date.now() + PROGRAMMATIC_MS;   // 等待期持续静默
    const w = host.clientWidth, h = host.clientHeight;
    // 稳定判据只取宽度：真正决定这里"该不该重排"的是宽度是否还在变
    // （面板开合的 flex 过渡改的就是宽度）。高度受滚动条出现/消失、
    // 图片加载等影响会持续抖 1-20px（实测冷启动 737→717），把它计入
    // 判据会让等待门反复重置、空转到 30 帧兜底（≈500ms）——这正是
    // "冷启动收敛缓慢"的一部分。lastH 仅记录，不参与判定。
    if (w === lastW) {
      stable++;                       // 相邻两轮宽度一致 → 布局趋稳
      lastH = h;
    } else {
      stable = 0; lastW = w; lastH = h;
    }
    frames++;
    if (stable >= 2 || frames >= 30) {
      cont();
      return;
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return true;
}

/** 暂停/恢复宿主尺寸观察。
 *
 *  拖拽分隔条时 contentLeft 宽度逐帧变化，observer 会每帧触发一次
 *  resizeNative —— 而 fork 的 manager.resize 内部 clear() 会触发全量重渲染
 *  （~300ms 级联），拖拽会严重卡顿。调用方在 mousedown 时 suspend、mouseup
 *  时 resume（resume 会补一次重排，保证终态正确）。 */
export function suspendHostResizeObserver() {
  _hostResizePaused = true;
  if (_hostResizeRaf) { cancelAnimationFrame(_hostResizeRaf); _hostResizeRaf = 0; }
}

export function resumeHostResizeObserver() {
  if (!_hostResizePaused) return;
  _hostResizePaused = false;
  resizeNative();      // 补一次：拖拽期间的中间尺寸全部作废，只按终态重排
}

/** 视口顶一次遍历同时取出 CFI 与文本（位置模型的核心读数）。
 *
 *  · cfi —— 视口顶 y 处文本节点前 20 字 → Range → Contents.cfiFromRange。
 *    带 section base，是 epub.js 自己的语义坐标：段落级、天然唯一（可当
 *    去重键），也是 display() 能确定性消费的恢复锚。
 *  · text —— 同一节点的后续 120 字，继续喂 /api/sync 的文本指纹桥映射到
 *    DB 的 (chapter, chunk)。
 *
 *  视口顶落在图片/纯空白上时返回 null（本次不上报、不锚定）。 */
function _captureViewport() {
  if (!rendition) return null;
  const scroller = _getScroller();
  if (!scroller) return null;
  const contents = rendition.getContents() || [];
  if (!contents.length) return null;

  const scrollerRect = scroller.getBoundingClientRect();
  const viewTop = scrollerRect.top;
  const viewBottom = scrollerRect.bottom;

  // 1. Structural census. Which sections are actually on screen, and how
  //    much of each? This is the renderer's own coordinate — no text
  //    fitting involved. Continuous scrolling renders neighbours; the
  //    intersection with the scroller rect filters them out.
  const visible = [];
  for (const c of contents) {
    const frame = c.window ? c.window.frameElement : null;
    if (!frame || !c.document) continue;
    const fr = frame.getBoundingClientRect();
    const px = Math.max(0, Math.min(fr.bottom, viewBottom) - Math.max(fr.top, viewTop));
    if (px <= 4) continue;
    visible.push({ c, rect: fr, visiblePx: Math.round(px) });
  }
  if (!visible.length) return null;

  // 2. Focus = the section occupying most of the viewport (reading intent).
  //    Top = the section at the viewport top (restore anchor). At a chapter
  //    boundary these differ: the top is the previous chapter's tail while
  //    the focus is the chapter the reader is actually looking at.
  let focus = visible[0];
  for (const v of visible) if (v.visiblePx > focus.visiblePx) focus = v;
  let topHit = visible.find(v => v.rect.top <= viewTop && v.rect.bottom > viewTop);
  if (!topHit) {
    topHit = visible.reduce((a, b) => (a.rect.top <= b.rect.top ? a : b));
  }

  const _textAt = (item, yViewport) => {
    const y = Math.max(0, yViewport - item.rect.top);
    const node = _textNodeAtY(item.c.document, y);
    return node ? _collectTextFrom(node, 0, 120) : '';
  };

  let cfi = '';
  try {
    const y = Math.max(0, viewTop - topHit.rect.top);
    const node = _textNodeAtY(topHit.c.document, y);
    if (node) {
      const range = topHit.c.document.createRange();
      range.setStart(node, 0);
      range.setEnd(node, Math.min(20, (node.nodeValue || '').length));
      cfi = topHit.c.cfiFromRange(range) || '';
    }
  } catch (_) { /* CFI 抓取失败：只上报文本，不影响落库 */ }

  const text = _textAt(topHit, viewTop);
  // Focus fingerprint: when the focus section starts below the viewport
  // top, this is its first text node (heading); the backend strips the
  // heading prefix before matching. Otherwise it is the text at the top
  // inside the focus section.
  const focusText = _textAt(focus, viewTop) || text;

  if (!cfi && !text && !focusText) return null;
  return {
    cfi,
    text,
    focusText,
    topSection: topHit.c.sectionIndex,
    focusSection: focus.c.sectionIndex,
    visibleSections: visible.map(v => ({
      sectionIndex: v.c.sectionIndex,
      visiblePx: v.visiblePx,
    })),
  };
}

/** 只要 CFI（重排锚定用）的薄封装。抓不到返回 null。 */
function _captureViewportAnchorCfi() {
  const vp = _captureViewport();
  return (vp && vp.cfi) || null;
}

/** y 处（或 y 下方最近）的非空文本节点 —— _captureViewport 的视口顶探针。 */
function _textNodeAtY(doc, yInFrame) {
  const body = doc.body || doc.documentElement;
  if (!body) return null;
  const walker = doc.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (!(n.nodeValue || '').trim()) continue;
    const el = n.parentElement;
    if (!el) continue;
    const r = el.getBoundingClientRect();   // iframe 内容不滚动：视口坐标 = 文档坐标
    if (r.bottom > yInFrame) return n;
  }
  return null;
}

export async function initNative(bookId, readingProgress) {
  const host = document.getElementById('bookPageNative');
  const chunkHost = document.getElementById('bookPage');
  if (!host || !chunkHost || typeof ePub !== 'function') return false;

  // 同书快路径：切页往返不重建（见上方保活状态注释）。视口位置按 CFI 恢复
  // ——display:none 期间 scrollTop 丢失，而像素级恢复在布局塌缩后无意义
  // （每段行数已变，同一 scrollTop 对应的内容整体漂移）。CFI 是语义坐标，
  // 与布局无关。
  if (!destroyed && book && rendition && _nativeBookId === bookId && _revealDone) {
    restoreViewport();
    return true;
  }

  // 换书前先彻底销毁上一本。epub.js 的 renderTo 不会清空宿主容器：旧书的
  // section iframe 会残留在连续滚动容器的顶部——新书 UI（书/章下拉）已切换
  // 而视口里仍是旧书内容（渲染与控件背离）；残留的 relocated 监听还会把
  // 旧书文本通过 /api/sync 记到新书的阅读进度上。
  destroyNative();

  const gen = ++_initGen;
  _nativeBookId = bookId;
  _bindUserEngage();
  // 冷启动恢复窗口开启：本函数接下来会按宿主**当前**尺寸 renderTo 并
  // display(cfi) 恢复进度。窗口内的所有重排请求一律推迟（见 resizeNative），
  // 避免与这里的 display(cfi) 并发竞争——实测并发会让落点偏 10 个 DOM
  // 元素（/4/192 → /4/182）并多起一条收敛循环。
  _coldRestoring = true;
  _deferredResize = false;
  let myBook = null;
  let myRendition = null;
  try {
    // 1. Fetch the original epub (same file the backend parsed).
    const resp = await _fetchWithTimeout(S.API + '/api/books/' + bookId + '/file', 20000);
    if (!resp.ok) throw new Error('file ' + resp.status);
    const buf = await resp.arrayBuffer();
    if (gen !== _initGen) return false;   // 已有更新的切书请求接管，本次作废

    // 2. Create book (10s guard against broken epubs hanging).
    //    先建局部实例，gen 校验通过才挂到全局——被取代时零残留。
    myBook = ePub(buf);
    // 2.5 内容净化：移除书内嵌 <script>。fork 渲染链：Section#load 触发
    //     spine.hooks.content（参数为已解析 Document）→ XMLSerializer 序列化
    //     → iframe.srcdoc。这些脚本反正会被 iframe 沙箱拦截（sandbox=
    //     "allow-same-origin"，未开 allowScriptedContent），只是每次序列化
    //     都刷一条 "Blocked script execution in 'about:srcdoc'" 控制台告警；
    //     解析期直接删掉，告警从源头消失且行为与之前完全一致（脚本本来
    //     就不执行）。注册必须在任何 section 加载（display/预加载/缓存构建）
    //     之前，紧贴 ePub(buf) 是最早的安全点。_findSectionForText 构建
    //     章节缓存时也会对全部 spine 项直调 sec.load()，钩子随之多触发
    //     N 次——正常现象，顺带让缓存文本更干净。
    myBook.spine.hooks.content.register((doc, section) => {
      try {
        doc.querySelectorAll('script').forEach((s) => s.remove());
      } catch (_) { /* 净化失败不影响渲染 */ }
      // 2.6 主题预注入：让 iframe **出生就带着**阅读主题样式。
      //     为什么必须在这里做（实测 2026-09-14 硬刷恢复"幽灵滚动"的真凶）：
      //     旧实现只在 'rendered' 事件后 50ms 调 applyThemeFromDom()，那时
      //     section 已按 EPUB 自带 CSS 完成排版；再注入
      //     `font-size / line-height:1.75 / font-family` 会让整篇**重排**——
      //     视口上方的内容变高，而 scrollTop 不变，于是视口顶的文本被推到
      //     更靠前的位置。实测 display 落点在 /4/192（「在小旅馆门口…」），
      //     注入后滑到 /4/168（「思琪一面拗着自己的手指…」）= 偏 24 个 DOM
      //     元素 ≈ 12 段。这不是"滚动"，是内容在视口底下长出来。
      //     钩子在 XMLSerializer 之前执行，样式随 srcdoc 一起进 iframe：
      //     iframe 首帧布局即最终布局，没有"先排错再重排"这一步。
      try { _injectThemeIntoDoc(doc); } catch (_) { /* 预注入失败：rendered 后仍会补注入 */ }
    });
    await Promise.race([myBook.ready, _timeout(10000)]);
    if (gen !== _initGen) {
      try { myBook.destroy(); } catch (_) { /* best-effort */ }
      return false;
    }

    // 3. Show the native host BEFORE renderTo — a hidden container has
    //    zero size and epub.js would lay out everything at height 0.
    host.hidden = false;
    chunkHost.hidden = true;
    // 无感空降：定位收敛完成前 host 保持透明。hidden 会把容器压成
    // zero size 让 epub.js 布局全崩（上面注释），opacity 只是不见、
    // 布局照常——用户看不到"书首/章节顶 → 收敛循环反复校正"的中间态，
    // 遮罩揭开时视口已停在上次阅读位置，即"打开书就在原地"。
    host.style.opacity = '0';
    // 记账：rendition 就是按这个尺寸建的。此后 width 未变的重排请求
    // （RO 因 scrollbar/内容撑开触发的高度抖动）都会被判为"无需重排"。
    _lastResizeW = host.clientWidth;
    _lastResizeH = host.clientHeight;
    myRendition = myBook.renderTo(host, {
      flow: 'scrolled',
      manager: 'continuous',
      spread: 'none',
      width: '100%',
      height: '100%',
      // 预加载距离：连续模式下向上滚到距已加载内容顶端 offset px 内才
      // prepend 上一 section。默认 500 太小——快速上滚时 section 异步
      // 加载完成后 counter 补偿会把 scrollTop 猛拉回去（章节边界回滚 bug）。
      // 调大到 8000：section 在用户到达边界前就加载完，补偿不可见；
      // 且 init 时能把前置内容（封面/扉页等）一次性加载全。
      // ⚠️ 2026-09-24 fork 补丁（epub.min.js continuous prepend 的 RESIZED
      //    处理器）：counter 补偿加视口守卫——只在 view 完全位于视口上方时
      //    补偿，且每次非零 heightDelta 都过守卫（原为一锤子 expanded 只补
      //    首次 reframe）。迟到加载不再把用户按整章高度弹回（章节边界回滚
      //    bug 的根治）；首次 reframe 之后的增高（字体重排等）重新纳入补偿。
      offset: 8000,
    });
    book = myBook;
    rendition = myRendition;
    destroyed = false;
    S.renderMode = 'native';

    // 4. Theme injection + listeners.
    applyThemeFromDom();
    _watchTheme();
    // 进度上报不再依赖 relocated：它只在 display/locate 流程 emit（滚动路径
    // 从不 emit），且 display 是程序性操作，本就不该产生落库。用户滚动的
    // 主信号是 manager 的 scrolled（见 _installScrollReporter）。
    // New sections render lazily while scrolling; re-inject the theme so
    // fresh iframes don't fall back to the epub's own styling, and bind
    // the user-engage listeners onto the fresh iframe document.
    rendition.on('rendered', (section, view) => {
      if (destroyed) return;
      _lastRenderedAt = Date.now();
      _bindUserEngageOnContents();
      // 滚动容器在首帧 section 渲染后才真正就绪 —— 补一次安装（幂等），
      // 补上 _getScroller() 的诊断值，也兜住"initNative 中段调用失败"。
      _installScrollReporter();
      _notifySectionRendered(view);
      setTimeout(() => applyThemeFromDom(), 50);
    });

    // 4.5 滚动进度保存（订阅 manager 的 scrolled 事件）。epub.js fork 的
    // continuous manager 在滚动路径上从不 emit "relocated"（全库仅
    // Rendition.display/locate 流程 emit）——依赖它滚动进度永远不落库
    // （native 模式进度丢失的根因）。但也不能直接监听 DOM scroll：那会把
    // manager 的位置补偿（counter 的 scrollBy(…, true)）一并捕获并落库，
    // 导致进度随机跳变。详见 _installScrollReporter 的注释。
    _installScrollReporter();

    // 4.6 宿主尺寸观察：收起/展开交互页、窗口缩放等都会改变阅读区宽度，
    //     而调用方的 resizeNative() 是同步调用（布局未完成时读到旧值）。
    //     observer 在布局稳定后补一次，兜住滚动条停在旧宽度的问题。
    _installHostResizeObserver();

    // 4.75 无感空降兜底：极端情况下定位卡住（收敛循环满轮/请求悬挂），
    // 6s 强制揭开遮罩——宁可看见跳动，不能让用户面对纸张色空白。
    clearTimeout(_revealTimer);
    _revealTimer = setTimeout(() => _revealHost(gen), 6000);

    // 5. Restore progress. CFI is the authoritative coordinate — a record
    //    carrying one is restored with a single deterministic display(cfi),
    //    no fingerprint chain and no convergence loop. Legacy records
    //    (written before the field existed) fall back to the chunk anchor.
    //    两者都是程序定位：_programmaticUntil 全程覆盖，不产生落库。
    if (readingProgress && readingProgress.cfi) {
      _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
      _programTimerArm();            // display 长扫描期间持续静默（防补偿漏窗）
      _committedCfi = readingProgress.cfi;
      _committedPosKey = '';   // section 尚未知：首次滚动必须上报一次
      _viewportCfi = readingProgress.cfi;
      try {
        // Bounded wait: in the continuous flow this promise can stay pending
        // forever even though the section is already rendered and the
        // scroller moved. A bare await here blocked everything after it —
        // including _syncMetaFor — leaving the previous book's chapter line
        // in the reader header (the 6s reveal fallback still showed the
        // page, so only the示数 was wrong).
        await _displayWithTimeout(readingProgress.cfi);
        // 冷启动特有：display 落点精确，但随后的相邻 section 补载 / 字体就绪
        // 仍可能把视口顶推走。先当场核对（常见路径直接过），不一致再等补载
        // 静默后用 display 语义重锚（见 _anchorColdStart）。
        await _anchorColdStart(readingProgress.cfi);
        await _scrollToCfi(readingProgress.cfi);
      } catch (e) {
        console.warn('[epub-reader] CFI 恢复失败，退化到 chunk 锚定位:', e);
        if (readingProgress.chapter != null) {
          await jumpToAnchor(readingProgress.chapter, readingProgress.chunk,
            { skipRecord: true });
        }
      } finally {
        _programTimerClear(true);    // 定位收敛完成，允许用户随后滚动上报
      }
      // 视口已归位，但示数还需要 total_chunks / 章节名 —— 取一次元信息并
      // 刷新 UI 与 hint（不带 record，不落库）。
      if (readingProgress.chapter != null) {
        await _syncMetaFor(readingProgress.chapter, readingProgress.chunk);
      }
      // 恢复落点（CFI=视口顶）与阅读意图可能不同：章节边界上 CFI 在上一章
      // 尾巴、focus 在下一章。补一次 ui-only 上报，把示数/visibleChapters
      // 对齐到 focus（不落库，DB 仍保留真正的阅读进度）。
      _commitViewport({ force: true, record: false });
    } else if (readingProgress && readingProgress.chapter != null) {
      await jumpToAnchor(readingProgress.chapter, readingProgress.chunk, { skipRecord: true });
    } else {
      await _displayWithTimeout(0);
      _kickContinuousFill();          // 首开无进度：display(0) 后立即驱动补载
      // 首开无进度时也要把示数刷成**这本新书**的。这里此前什么都不写，
      // positionMeta 就保留着上一本书的章节标题（切书时没有任何人清）。
      if (gen === _initGen) await _syncMetaFor(0, 0);
      // 同样补一次 ui-only 上报：填充 visibleChapters，供本轮聊天的门使用。
      if (gen === _initGen) _commitViewport({ force: true, record: false });
    }
    if (gen !== _initGen) {
      // 展示期间又被切换：仅当全局槽位仍指向自己时才清理，避免误伤新一轮
      _coldRestoring = false;
      if (rendition === myRendition) destroyNative();
      return false;
    }
    _revealHost(gen);   // 定位收敛完成 → 一次性淡入，视口已在目标位置
    // 冷启动窗口关闭：此刻再处理窗口内被推迟的重排请求。这里只需要比尺寸
    // ——若窗口期间宿主宽度真的变了（用户拖窗口/拖分隔条），resizeNative
    // 会照常走完整重排链；宽度没变（常见：scrollbar 引起的高度抖动）则
    // 直接跳过。由此把"冷启动期间 N 条重排链并发抢跑"收敛为至多一条。
    _coldRestoring = false;
    if (_deferredResize) {
      _deferredResize = false;
      resizeNative();
    }
    // 迟到的布局变化复核（不阻塞揭罩，非 cfi 恢复路径无需）
    if (readingProgress && readingProgress.cfi) _armLateVerify(readingProgress.cfi, gen);
    // 首帧 rendered 事件可以早于监听器就绪——display 完成后再补挂一次
    _bindUserEngageOnContents();
    _installScrollReporter();   // 同上：容器此刻一定就绪，补诊断值 / 兜住漏挂
    // 标注层：书就绪（此时初始 sections 已渲染完毕，回放可立即开始）
    _notifyBookReady();
    return true;
  } catch (e) {
    console.warn('[epub-reader] native 渲染失败，回退 chunk 模式:', e);
    _coldRestoring = false;   // 失败退出：冷启动窗口必须关，否则重排被永久推迟
    _deferredResize = false;
    // 自己仍是最新一轮 → 常规回退清理；被取代但全局仍指向自己 → 只清自己；
    // 全局已被新一轮接管 → 不动。
    if (gen === _initGen || (myRendition && rendition === myRendition)) {
      destroyNative();
    }
    return false;
  }
}

/** 揭开无感空降遮罩（幂等）。gen 校验：本轮已被新一轮 init 接管时，
 *  旧轮的 reveal/兜底定时器不再动 host——否则旧轮的 6s 兜底会把正在
 *  加载中的新书提前揭开，中间态重新可见。 */
function _revealHost(gen) {
  if (gen !== _initGen) return;
  const host = document.getElementById('bookPageNative');
  if (!host) return;
  clearTimeout(_revealTimer);
  _revealDone = true;
  host.style.transition = 'opacity 180ms ease-out';
  host.style.opacity = '1';
}

export function destroyNative() {
  _initGen++;                         // 作废进行中的 init，防过期写入
  clearTimeout(_revealTimer);
  _programTimerClear(true);           // 停掉续期器，杜绝泄漏
  _committedCfi = '';
  _committedPosKey = '';
  _lastMismatchKey = '';
  _viewportCfi = '';
  _userIntentUntil = 0;
  _programmaticUntil = 0;
  _throttleUntil = 0;
  _tickUntil = 0;
  _lastDbPos = null;
  _lastChunkInfo = null;
  S.visibleChapters = [];
  _pendingAnchorCfi = null;
  _coldRestoring = false;             // 冷启动窗口随本轮销毁一并关闭
  _deferredResize = false;
  _sectionCache = null;
  _revealDone = false;
  _lastResizeW = 0; _lastResizeH = 0;   // 新 rendition 需要真实 resize 建布局
  if (_themeObserver) { _themeObserver.disconnect(); _themeObserver = null; }
  _uninstallScrollReporter();
  _lastExtractEmpty = false;
  _lastSyncFail = false;
  _lastMatchMiss = false;
  _engageDocs.clear();                // iframe document 随 rendition 销毁，只清引用
  destroyed = true;                   // 先置位：destroy 过程触发的 relocated 不再上报
  if (_hostResizeObs) {               // 断开尺寸观察（destroyed 已置位，回调本也会早退）
    try { _hostResizeObs.disconnect(); } catch (_) { /* best-effort */ }
    _hostResizeObs = null;
  }
  if (_hostResizeRaf) { cancelAnimationFrame(_hostResizeRaf); _hostResizeRaf = 0; }
  if (rendition) {
    try { rendition.destroy(); } catch (_) { /* best-effort */ }
    rendition = null;
  }
  if (book) {
    try { book.destroy(); } catch (_) { /* best-effort */ }
    book = null;
  }
  S.renderMode = 'chunk';
  const host = document.getElementById('bookPageNative');
  if (host) {
    host.hidden = true;
    host.innerHTML = '';              // 双保险：清除残留的 section iframe
    host.style.opacity = '';          // 清空降遮罩残留（回退 chunk 模式时）
    host.style.transition = '';
  }
  const chunkHost = document.getElementById('bookPage');
  if (chunkHost) chunkHost.hidden = false;
}

/** Jump to a semantic chapter (chapter, chunk 0). Clamped to range. */
export function jumpToChapter(chapterIndex) {
  const sel = document.getElementById('chapterSelect');
  const max = sel && sel.options ? sel.options.length - 1 : 0;
  const idx = Math.max(0, Math.min(chapterIndex | 0, max));
  return jumpToAnchor(idx, 0);
}

/** display() 的"完成"信号：**有界等待**，不是裸 await。
 *
 *  为什么不能裸 await：fork 的 display promise 在某些状态下**永不 resolve**。
 *  实测（切 tab 后从笔记页跳转）：display 明明已经渲染了目标 section、滚动容器
 *  也移到位了（dump: sections=[7]、scroller.top=7627），promise 却 8s 不返回。
 *  裸 await 会把整条定位链卡死 —— 45s 后 jumpToAnchor 仍未 settle，之后每次
 *  跳转都挂在这条链上（_restoreChain 串行）。
 *  而 display 之后的收敛循环本来就是**按几何读回**校验落点的（_computeCfiScrollTop /
 *  _computeAnchorScrollTop），不依赖这个 promise；超时放行不会让落点变差，
 *  只会让"已经渲染好了却没人往下走"不再发生。
 *  display 明确失败（CFI 非法等）仍照常上抛给调用方。 */
async function _displayWithTimeout(target, ms = 2500) {
  const p = rendition.display(target);
  p.catch(() => { /* 超时后它的结局已无人关心，这里只是标记已处理 */ });
  await Promise.race([p, _wait(ms).then(() => { _diag.displayTimeout++; })]);
}

/** Jump to an exact CFI (annotation replay/bookmark navigation). Returns success.
 *  fork 的 display(cfi) 在 continuous 流里只消费到 section 级（视口停在
 *  section 顶）——精确到段落需要 display 后按 CFI→Range 几何位置收敛
 *  滚动（_scrollToCfi），与进度恢复的文本锚收敛同款思路。
 *
 *  ⚠️ display 的 promise 与它的**效果**是两回事：实测从别的 tab 点跳转时
 *  display 已经渲染好目标 section、滚动容器也移到位了，promise 却始终不返回
 *  （见 _displayWithTimeout）。所以落点必须**核验**，不能只看 display 有没有
 *  抛错；没到位就让出几帧重来。核验用 section 基（`epubcfi(/6/16`）而不是
 *  整串：落点 CFI 由 _captureViewport 从视口顶文本节点重新生成，元素路径会
 *  比目标差几级（/4/30 vs /4/32，同一处）。 */
export async function jumpToCfi(cfi) {
  if (destroyed || !rendition || !cfi) return false;
  // 显式程序定位：全程覆盖 _programmaticUntil，不产生落库。
  _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
  const key = _cfiAnchorKey(cfi);
  const secKey = _cfiSectionKey(cfi);
  let landed = false;
  for (let attempt = 0; attempt < 3 && !landed; attempt++) {
    if (attempt) await _wait(60);   // 让出几帧：等宿主布局/挂载落定再重来
    try {
      await _displayWithTimeout(cfi);   // section 级定位（落点=section 顶）
    } catch (e) {
      console.warn('[epub-reader] CFI 跳转失败:', e);
      return false;
    }
    await _scrollToCfi(cfi);          // 段落级精修（收敛循环，抗布局漂移）
    const cur = _captureViewport();
    landed = !!(cur && cur.cfi
      && (_cfiAnchorKey(cur.cfi) === key || _cfiSectionKey(cur.cfi) === secKey));
  }
  if (!landed) {
    console.warn('[epub-reader] CFI 跳转 3 次后视口仍未到位:', cfi);
  }
  _committedCfi = cfi;              // 落点即已知位置：写去重槽，避免随后
  _committedPosKey = '';            // 同上：section 未同步，首次上报放行
  _viewportCfi = cfi;               // 一次用户滚动把同一位置重复上报
  return true;
}

/** CFI 的「锚点键」——只取到元素路径（丢弃 `,/1:0,/1:20` 文本偏移段）。
 *  用于判断两个 CFI 是否指向同一 DOM 位置：文本偏移范围可能因抓取时机
 *  而不同（`/1:0,/1:20` 固定 20 字），但元素路径相同就是同一处。 */
function _cfiAnchorKey(cfi) {
  const s = cfi || '';
  const i = s.indexOf(',');
  return i >= 0 ? s.slice(0, i) : s;
}

/** CFI 的「section 键」——只取 `epubcfi(/6/16!` 这一段（spine base）。
 *  判"视口到没到目标 section"用它：_captureViewport 生成的落点 CFI 与目标
 *  CFI 的元素路径会差几级，但 section 基相同。 */
function _cfiSectionKey(cfi) {
  const s = cfi || '';
  const i = s.indexOf('!');
  return i >= 0 ? s.slice(0, i) : s;
}

/** CFI → Range → 视口滚动收敛循环。continuous 渲染异步增高会把 scrollTop
 *  推偏（前置 section 加载/prepend 补偿），与 _locateAndScrollToText 同款：
 *  以 Range 位置为锚反复校正，相邻两轮目标一致（布局稳定）即收敛。
 *  _anchorGen：连续程序定位（跳转/重排锚定）只留最新一轮，旧的自动退出。
 *
 *  ⚠️ 先决检查：视口已在目标锚上时**直接跳过几何精修**。
 *  为什么：连续模式补载期间 `_computeCfiScrollTop` 的几何换算不可靠
 *  （frameTop/topY 依赖随补载持续变化的布局），强行精修会把 `display(cfi)`
 *  的正确落点推偏。实测 2026-09-14：`display` 后视口精确在
 *  `epubcfi(/6/16!/4/186,...)`（第 92 段），精修后漂到 `/4/166`（第 82 段，
 *  = 前一个 chunk 的段首）——差 20 个 DOM 元素 ≈ 3 个 chunk，用户感知为
 *  "恢复精度只有 chunk 级"。display 是 epub.js 自身的确定性定位，比外部
 *  几何换算可信；只在 display 未落到目标锚时才走精修循环。 */
async function _scrollToCfi(cfi) {
  const myGen = ++_anchorGen;
  try {
    const cur = _captureViewport();
    if (cur && cur.cfi && _cfiAnchorKey(cur.cfi) === _cfiAnchorKey(cfi)) {
      if (!destroyed && myGen === _anchorGen) _viewportCfi = cfi;
      _kickContinuousFill();
      return;
    }
    await _scrollToCfiLoop(cfi);
  } catch (_) { /* 收敛失败不阻断调用方：视口停在 section 顶也已可用 */ }
  if (!destroyed && myGen === _anchorGen) {
    // 程序定位落点：只更新恢复锚（切页/重排沿用），**不上报**——写入权
    // 不在程序路径上。调用方（jumpToAnchor 的 step 3）负责刷 UI。
    _viewportCfi = cfi;
  }
  // 定位链收尾：清 ignore 残留 + 主动驱动一次连续补载（见 _kickContinuousFill）。
  _kickContinuousFill();
}

async function _scrollToCfiLoop(cfi) {
  const myGen = _anchorGen;
  const scroller = _getScroller();
  if (!scroller) return;
  let prevTarget = -1;
  let missed = false;    // 上轮目标未算出（未渲染/未解析）→ 下轮等长一点
  let missStreak = 0;    // 连续未渲染轮数（累计太长则放弃，保留 display 落点）
  // ── 布局静止门：continuous 补载（尤其展开交互页缩窄宽度后的全量重新
  //    分页）可持续数秒，scrollHeight 一路膨胀（实测 47263→102429）。
  //    若目标 section 未渲染就精修，_computeCfiScrollTop 恒返回 null，旧
  //    实现 14 轮(≈1.1s)耗尽后视口停在 display 落点，而补载继续把
  //    scrollTop 的语义位置前移 → 视口漂到章节中段（ck29→ck14，见 18:42）。
  //    修法：先等 scrollHeight 连续 3 轮不变（补载完成）再精修；等待期
  //    每轮续期程序窗口（零上报）。总预算放宽到 60 轮，未渲染间隔随
  //    持续时长拉长，防误判"目标失效"而过早放弃。 ──
  let lastH = -1, stableH = 0;
  for (let w = 0; w < 60; w++) {
    if (destroyed || myGen !== _anchorGen) return;
    _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
    await _wait(missed ? Math.min(250, 120 + missStreak * 20) : _CONVERGE_TICK_MS);
    if (destroyed || myGen !== _anchorGen) return;
    const target = await _computeCfiScrollTop(cfi);
    if (target === null) {
      // 目标 section 未渲染/未解析：不计数滚动高度稳定（补载可能仍在变），
      // 拉长下次等待、累积未渲染计数；超过 15 轮仍不可得则放弃精修。
      missed = true; missStreak++;
      if (missStreak > 15) return;    // 目标 CFI 疑似无效，保留 display 落点
      continue;
    }
    missed = false; missStreak = 0;
    // 目标已算出：确保滚动高度稳定（补载完成）再落位，避免被后续增高推偏。
    const h = scroller.scrollHeight;
    if (h === lastH) { stableH++; } else { stableH = 0; lastH = h; }
    if (stableH < 3) { continue; }     // 布局仍在膨胀：再等一轮
    scroller.scrollTop = Math.max(0, target - 24);   // 视口留 24px 边距
    if (prevTarget >= 0 && Math.abs(target - prevTarget) < 2) {
      // 收敛后再校验一次，防迟到的补偿事件把视口推偏
      await _wait(_CONVERGE_SETTLE_MS);
      if (destroyed || myGen !== _anchorGen) return;
      const again = await _computeCfiScrollTop(cfi);
      if (again !== null) scroller.scrollTop = Math.max(0, again - 24);
      return;
    }
    prevTarget = target;
  }
  console.warn('[epub-reader] CFI 精修: 收敛循环未稳定, 视口可能停在 section 顶');
}

/** CFI 的视口 scrollTop（与 _computeAnchorScrollTop 同一几何换算）：
 *  frameTop + Range.top - docRect.top。CFI 解析失败/目标未渲染返回 null。
 *
 *  ⚠️ 必须用 rendition.getRange(cfi)（Contents.getRange）而非 book.getRange(cfi)：
 *  book.getRange 从**原始 spine 文档**解析，返回的 range 的 ownerDocument 与
 *  渲染 iframe 的 document 是**不同对象引用**，`c.document === doc` 恒 false →
 *  _scrollToCfi 精修永远 null → 视口停在 display 落点，补载又把 scrollTop 语义
 *  前移 → 视口漂到章节中段（实测 2026-09-14：ck29 漂到 ck14，console 刷屏
 *  [cfi-diag] doc not in contents nFrames=1）。而 rendition.getRange 按 CFI 的
 *  spinePos 匹配**当前渲染的 Contents**，在 iframe 内生成 range，其
 *  getBoundingClientRect 才是真实视口坐标。 */
async function _computeCfiScrollTop(cfi) {
  if (!rendition || !book) return null;
  let range = null;
  try { range = await rendition.getRange(cfi); }
  catch (_) { return null; }
  if (!range || !range.startContainer) return null;
  const doc = range.startContainer.ownerDocument;
  const contents = (rendition.getContents() || []).find(c => c.document === doc);
  if (!contents) return null;                    // 目标 section 尚未渲染
  const frame = contents.window ? contents.window.frameElement : null;
  if (!frame) return null;
  const scroller = _getScroller();
  if (!scroller) return null;
  // collapsed CFI（position）的 rect 可能零宽有高；全零则退回首个 client rect
  let topY = null;
  const rangeRect = range.getBoundingClientRect();
  if (rangeRect && (rangeRect.width || rangeRect.height)) topY = rangeRect.top;
  else {
    const r0 = range.getClientRects()[0];
    if (r0) topY = r0.top;
    else return null;
  }
  const scrollerRect = scroller.getBoundingClientRect();
  const fr = frame.getBoundingClientRect();
  const frameTop = fr.top - scrollerRect.top + scroller.scrollTop;
  const docRect = contents.document.documentElement.getBoundingClientRect();
  return frameTop + (topY - docRect.top);
}

/** Jump to a semantic position: DB chunk text → text anchor → location.
 *  opts.skipRecord：程序恢复定位传 true → 落点只刷示数、不落库（恢复落点
 *  = section 顶 / 章节兜底，不是用户意图）。用户主动跳章（下拉/箭头）不传
 *  → 落点落库。
 *
 *  ⚠️ 落库的是**视口实际落点**，不是请求的目标位置（见 step 3）。这两者会
 *  背离：锚与章节都未命中时定位链刻意"保留当前视口"（防空降书首），若仍按
 *  目标写 UI/DB，示数就会撒谎、DB 会记下一个从未到过的位置。 */
export function jumpToAnchor(chapterIdx, chunkIdx, opts = {}) {
  if (destroyed || !book || !rendition) return Promise.resolve();
  const run = (_restoreChain || Promise.resolve()).then(async () => {
    // 显式程序定位窗口：从锚解析到收敛落点，全程不产生落库。
    // 用续期器而非一次性窗口：_locateAndScrollToText 里的 display(secIdx)
    // 是异步长扫描，补偿滚动可持续数秒，一次性窗口(2500ms)会过期漏窗。
    _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
    _programTimerArm();
    // 1. Fetch the DB chunk text as the anchor (first ~80 chars).
    //    ★ 不带 record=1：这里拿到的只是"想去哪"，不是"到了哪"。由 chunk
    //    接口落库目标位置，等于在定位**开始前**就写库——锚未命中、定位被
    //    "保留当前视口"跳过时，DB 里就留下一个从未到过的位置。落库改由
    //    step 3 按实际落点提交。
    let anchor = '';
    try {
      const r = await fetch(S.API + '/api/books/' + (_nativeBookId || S.currentBook)
        + '/chunk?chapter=' + chapterIdx + '&chunk=' + chunkIdx);
      if (r.ok) {
        const d = await r.json();
        _lastChunkInfo = {
          total_chunks: d.total_chunks || 1,
          chapter_title: d.chapter_title || '',
        };
        anchor = _normWs(d.content || '').slice(0, 80);
      }
    } catch (_) { /* anchor 缺失则回退章节标题/保留视口 */ }

    // 2. Anchor → section → scroll。整段位移属于显式定位，_programmaticUntil
    //    已覆盖，无需再挂任何豁免——没有任何一条代码路径会调用 _commitViewport。
    try {
      let secIdx = anchor ? await _findSectionForText(anchor) : null;
      if (secIdx == null) {
        // 锚未命中 → 次兜底：按章节标题定位。章节标题（"第三章 复乐园"）
        // 通常就落在目标 section 文本开头，虽不如 chunk 锚精确，但比
        // display(0) 高一个数量级。标题直接复用上面 /chunk 已返回的
        // ( _lastChunkInfo.chapter_title )，不再多发一次 /context 请求。
        secIdx = _findSectionForChapter(
          (_lastChunkInfo && _lastChunkInfo.chapter_title) || '');
      }
      if (secIdx != null) {
        // 章节兜底时 anchor 不在该 section 内，_computeAnchorScrollTop 会
        // 一直返回 null 空转 14 轮——传空 anchor 让它走 display(secIdx) 后
        // 直接落在 section 顶部（收敛循环首轮即判定）。
        const anchorInSection = _sectionCache
          && _sectionCache.some(s => s.index === secIdx
            && anchor && s.text.indexOf(anchor.slice(0, 20)) >= 0);
        await _locateAndScrollToText(secIdx, anchorInSection ? anchor : '');
      } else {
        // 三级兜底：连章节都定位不到时**保留当前视口**，绝不 display(0)。
        // display(0) 会把视口空降到全书第一章，其后 epub.js 逐章加载并触发
        // counter/check 位置补偿，30s 内持续上报 ch1→ch2→ch3… 的机械序列，
        // 把真实进度（如 ch5）彻底覆盖——这正是"切页后空降到 1/11"的根因。
        // 保留原地至少语义位置不变；用户滚动后自然对齐。
        console.warn('[epub-reader] 锚与章节均未命中，保留当前视口（不空降书首）',
          'ch=' + chapterIdx, 'ck=' + chunkIdx);
      }
    } catch (e) {
      console.warn('[epub-reader] 恢复定位异常，保留当前视口:', e);
      // 定位失败时**保留当前视口**——此前这里 display(0) 回退书首，是
      // "切页后空降第一章"的第二条入口（与 secIdx==null 分支同源）。
    }
    // 3. 落点提交：以**视口实际落点**刷新示数与落库，而不是请求的目标位置。
    //
    //    目标 ≠ 落点：锚与章节都没命中时，定位链会"保留当前视口"（防空降
    //    书首）。此时若仍按目标刷 UI/DB，示数就会撒谎——实测：用户正读
    //    第二章 失乐园（ch5 ck3），一次未命中的跳章把示数写成
    //    「第一章 乐园 · 段落 1/11」（= ch4 ck0），正文却仍在失乐园；更糟的
    //    是目标被落库后，下次打开会"恢复"到从未到过的位置（表现为回到
    //    章节开头）。
    //    落点由 _captureViewport 抓取 → /api/sync 映射为 (chapter, chunk)
    //    → 同时刷新示数与 hint。用户主动跳章（!skipRecord）落库；程序恢复
    //    （skipRecord）只刷示数。
    const landed = _captureViewport();
    if (landed && landed.text) {
      // hint 传**目标**：跳转是"要去哪"已知的显式操作，用它当 hint 让指纹
      // 先在目标章里找（见 _postPosition 的 hint 注释）。
      _postPosition(landed, !opts.skipRecord, true,
        { chapter: chapterIdx, chunk: chunkIdx });
    } else {
      // 抓不到视口（视口顶为图片/纯空白）：只按目标刷示数，**不写
      // _lastDbPos、不落库**——DB 里宁可留着上一次的真实位置，也不写一个
      // 没到过的目标；hint 更不能被污染（后续滚动会据它就近匹配）。
      S.currentChapter = chapterIdx;
      S.currentChunk = chunkIdx;
      _updateMetaFromPos({
        chapter_index: chapterIdx,
        chunk_index: chunkIdx,
        total_chunks: (_lastChunkInfo || {}).total_chunks || 1,
        chapter_title: (_lastChunkInfo || {}).chapter_title || '',
      });
      console.warn('[epub-reader] 跳转落点抓取失败，仅按目标刷示数（不落库）',
        ' ch=' + chapterIdx, 'ck=' + chunkIdx);
    }
    // 4. 落点 CFI 作为恢复锚（切页/重排沿用）。抓不到就保留原锚——下一轮
    //    重排用旧锚仍比"clear 后从几何抓锚"可靠（后者会抓到书首）。
    if (landed && landed.cfi) _viewportCfi = landed.cfi;
    // 定位链收尾：清 ignore 残留 + 驱动连续补载（见 _kickContinuousFill）。
    _kickContinuousFill();
    _programTimerClear(true);        // 定位收敛，允许用户随后滚动上报
  });
  _restoreChain = run.catch(() => {});
  return run;
}

/** 组装阅读区主题 CSS + 变量（从主文档 CSS 变量读取当前主题）。
 *
 *  抽出来是为了**两处共用同一份结果**：
 *    · iframe 出生前（spine.hooks.content）预注入 → 首帧就是最终排版；
 *    · 运行时主题切换（applyThemeFromDom）再注入 → 内容逐字符相同则跳过
 *      写入，避免无意义重排把视口推走（见 applyThemeFromDom 的 equal 判定）。
 *  两份 CSS 若不一致，预注入就白做了：rendered 后的补注入仍会重排。 */
function _readerThemeCss() {
  const root = getComputedStyle(document.documentElement);
  const read = v => root.getPropertyValue(v).trim();
  const isNight = document.documentElement.getAttribute('data-theme') === 'night';
  const bg = read('--t-reader-bg-color') || (isNight ? '#141a14' : '#f5f3ec');
  const fg = read('--t-reader-font-color') || (isNight ? '#a8b0a0' : '#222222');
  const fs = read('--t-reader-font-size') || '18px';
  const ff = read('--t-reader-font-family')
    || '"Noto Serif SC", "Source Han Serif SC", "Songti SC", Georgia, serif';
  const link = read('--t-link') || '#4a90d9';
  const sel = read('--t-reader-selection-color') || '#e8f0e0';
  const css = [
    'html, body { background-color: ' + bg + ' !important; }',
    'body { color: ' + fg + ' !important; font-size: ' + fs + ' !important;'
      + ' line-height: 1.75 !important; font-family: ' + ff + ' !important; }',
    // 书内 CSS（Calibre 转换产物尤甚）常在元素级写死 font-family，仅靠 body
    // 继承会被截断——实测 19 本样书 10 本中招。强制全树继承用户选择；
    // 代码语义元素回退等宽栈，避免把代码块也拉成正文字体。
    'body * { font-family: inherit !important; }',
    'body pre, body pre *, body code, body code *, body kbd, body kbd *,'
      + ' body samp, body samp *, body tt, body tt *, body var, body var *'
      + ' { font-family: "Cascadia Code", Consolas, "Courier New", monospace !important; }',
    'a, a:link, a:visited { color: ' + link + ' !important; }',
    '::selection { background: ' + sel + ' !important; }',
    // 标注层（iframe 内部分）——marker 样式随主题注入，幂等。
    // 收藏下划线不在 iframe：epub.js SVG Pane 挂在主文档 view 容器，样式在 style.css
    // 批注 marker = morphicons 驱动的 chevron path：亮蓝定色（不随正文），
    // 上标位（vertical-align 抬升）悬在选中字段结尾上方，不打断基线阅读流
    '.bc-ann-cmt { display:inline-flex; align-items:center; margin:0 3px;'
      + ' color:#3b82f6; cursor:pointer; vertical-align:0.42em;'
      + ' user-select:none; -webkit-user-select:none;'
      + ' transition: color .15s ease, transform .15s ease; }',
    '.bc-ann-cmt:hover { color:#2563eb; transform:translateY(1px); }',
    '.bc-ann-cmt svg { width:18px; height:18px; display:block; }',
    '.bc-ann-cmt svg path { fill:none; stroke:currentColor; stroke-width:2;'
      + ' stroke-linecap:round; stroke-linejoin:round; }',
  ].join('\n');
  return { css: css, bg: bg, vars: {
    '--t-reader-bg-color': bg,
    '--t-reader-font-color': fg,
    '--t-reader-font-size': fs,
    '--t-reader-font-family': ff,
  } };
}

/** 把主题样式写进**已解析但尚未序列化**的 section Document。
 *  在 spine.hooks.content 里调用 → 样式随 srcdoc 进入 iframe，首帧即终态。 */
function _injectThemeIntoDoc(doc) {
  if (!doc) return;
  const t = _readerThemeCss();
  const r = doc.documentElement;
  if (r) {
    for (const k in t.vars) r.style.setProperty(k, t.vars[k]);
  }
  let st = doc.getElementById ? doc.getElementById('bc-app-theme') : null;
  if (!st) {
    st = doc.createElement('style');
    st.id = 'bc-app-theme';
    (doc.head || r).appendChild(st);
  }
  st.textContent = t.css;
}

/** Re-apply day/night + reader settings into the epub iframes.
 * 直接操作每个 iframe 的 document（幂等、可重复调用）——不依赖 epub.js
 * themes API（其同 name 重复 register 行为不稳，会让主题页后续改动失效）。 */
export function applyThemeFromDom() {
  if (!rendition || destroyed) return;
  const t = _readerThemeCss();
  const css = t.css, bg = t.bg;
  const root = getComputedStyle(document.documentElement);
  const read = v => root.getPropertyValue(v).trim();
  let injected = 0, changed = 0;
  try {
    for (const c of rendition.getContents()) {
      const d = c.document;
      if (!d || !d.documentElement) continue;
      const r = d.documentElement;
      const st = d.getElementById('bc-app-theme');
      // 内容已一致 → 什么都不写。重写同样的 CSS 也会触发样式重算+重排，
      // 而重排会把视口顶的文本推走（scrollTop 不变、上方内容变高）——
      // 冷启动"幽灵滚动"的另一半就来自 rendered 后这次无意义的补注入。
      const same = st && st.textContent === css
        && r.style.getPropertyValue('--t-reader-font-size') === t.vars['--t-reader-font-size']
        && r.style.getPropertyValue('--t-reader-font-family') === t.vars['--t-reader-font-family'];
      if (same) { injected++; continue; }
      // 1) 变量注入 documentElement：EPUB 自身 CSS 若引用同名变量也一并统一
      for (const k in t.vars) r.style.setProperty(k, t.vars[k]);
      if (!st) {
        const el = d.createElement('style');
        el.id = 'bc-app-theme';
        (d.head || r).appendChild(el);
        el.textContent = css;
      } else {
        st.textContent = css;
      }
      injected++; changed++;
    }
  } catch (e) {
    console.warn('[epub-reader] 主题注入失败:', e);
  }
  const host = document.getElementById('bookPageNative');
  if (host) {
    host.style.background = bg;
    const bb = read('--t-reader-bg-brightness') || '1';
    const fb = read('--t-reader-font-brightness') || '1';
    host.style.filter = 'brightness(' + bb + ') brightness(' + fb + ')';
  }
  if (!injected) console.warn('[epub-reader] 主题注入: 当前无已渲染 iframe');
}

// ── 视口 → /api/sync（唯一落库入口）──────────────────────────────────

/** 上报去重键：CFI 相同但 section 变了（章节边界）必须再报一次。 */
function _posKey(vp) {
  return (vp.cfi || '') + '|' + (vp.focusSection ?? '') + '|' + (vp.topSection ?? '');
}

/** 唯一落库入口。只有「用户意图窗口」内的调用才会真正写入：
 *   · _userIntentUntil —— 人类输入（滚轮/触摸/按键/按下）后的余辉窗口；
 *   · _programmaticUntil —— 显式定位/重排进行中，其间的滚动不是用户意图。
 *  opts.force 供"用户跳章落点提交"使用：那是一次已完成的用户可见定位，
 *  不是猜测（见 jumpToAnchor step 3）。
 *
 *  程序性位移的代码路径**不调用本函数**（resizeNative / _scrollToCfi 的
 *  收敛循环里都没有这条调用）——这正是新旧架构的根本差别：旧实现在每条
 *  路径上都调用、再判断"要不要放弃"，n 源 × n 条件组合爆炸；新架构让那些
 *  路径**没有调用点**，判断因此变得不必要。 */
function _commitViewport(opts = {}) {
  if (destroyed || !rendition) { _diag.dropDead++; return; }
  _diag.commit++;
  const now = Date.now();
  // forced：调用方已确知"这是一次用户可见定位的收尾"（跳章落点提交）。此时
  // 跳过意图/节流裁决与 CFI 去重——它的目的恰恰是把被"目标值"覆盖的示数
  // 纠回真实落点，去重反而会把它挡在门外。
  const forced = !!opts.force;
  if (!forced) {
    if (now > _userIntentUntil) { _diag.dropNoIntent++; return; }   // 非用户意图：不写
    if (now < _programmaticUntil) { _diag.dropProg++; return; }     // 程序操作窗口内：不写
    if (now < _throttleUntil) { _diag.dropThr++; return; }         // 节流：只降频，不参与裁决
  }
  const vp = opts.vp || _captureViewport();
  if (!vp) {
    _diag.dropNoVp++;
    if (!_lastExtractEmpty) {
      _lastExtractEmpty = true;
      const sc = _getScroller();
      console.warn('[epub-reader] 视口顶文本提取为空(跳过上报). scroller=',
        !!sc, ' scrollTop=', sc ? sc.scrollTop : 'n/a',
        ' frames=', rendition.getContents ? rendition.getContents().length : 'n/a');
    }
    return;
  }
  _lastExtractEmpty = false;
  // 结构上报可以只带 focusText（视口顶落在插图/空白时）
  if (!vp.text && !vp.focusText) { _diag.dropNoText++; return; }
  if (!forced) {
    // 去重键 = CFI + top/focus section。只用 CFI 会在章节边界漏报：
    // 视口顶还停在同一段（CFI 不变），focus 已经换到下一章。
    const posKey = _posKey(vp);
    if (posKey && posKey === _committedPosKey) { _diag.dropDedup++; return; }
    _throttleUntil = now + REPORT_THROTTLE_MS;
  }
  _postPosition(vp, opts.record !== false, forced);
}

/** POST 一次位置。文本仍走后端指纹桥映射到 (chapter, chunk)，
 *  CFI 一并记录（下次恢复直接 display(cfi)，不再重放锚链）。
 *  · record=false → 只刷示数不落库（程序恢复的落点）；
 *  · force=true  → 命中后无条件刷新示数（落点纠偏，即使与上次相同）。 */
async function _postPosition(vp, record = true, force = false, hint = null) {
  const gen = _initGen;                       // 上报期间切书则本次作废
  _diag.post++;
  // hint = 上次已知位置：连续滚动上报必然相邻，后端据此把"全书重复出现的
  // 短文本"解析到最近的一次出现，而不是全书最早的那次（早期实现的
  // indexOf 全库首命中会把进度跨章倒拖：实测 ch5 → ch3 → ch1）。
  // 显式跳转传 hint 覆盖：跳转的目标是**已知**的（就是我们要去的那一块），
  // 用旧位置当 hint 会把指纹搜索先限定在旧章，跨章跳转得全库扫一遍才命中，
  // 同文重复时还可能命中旧章里的那一次。
  const h = hint || _lastDbPos;
  try {
    const res = await fetch(S.API + '/api/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        // 进度归属跟随渲染中的书（切书竞态下 S.currentBook 可能已指向新书）
        bookId: _nativeBookId || S.currentBook,
        // pageText = 视口顶（恢复锚）；focusText = 视口面积最大的 section
        // （阅读意图）。后端结构定章、文本定块。
        pageText: vp.text || vp.focusText || '',
        focusText: vp.focusText || '',
        cfi: vp.cfi || '',
        topSection: vp.topSection ?? null,
        focusSection: vp.focusSection ?? null,
        visibleSections: vp.visibleSections || [],
        chapterTitle: '',
        hintChapter: h ? h.chapter : null,
        hintChunk: h ? h.chunk : null,
        // 是否落库由调用方声明的事实决定（用户意图滚动 / 用户跳章落点 =
        // true；程序恢复落点 = false），本函数只负责搬运事实、不做推断。
        record: record,
      }),
    });
    const data = await res.json();
    if (destroyed || gen !== _initGen) return;   // await 间隙可能已切书
    if (data.matched && data.position) {
      _diag.postOk++;
      _lastMatchMiss = false;
      _lastSyncFail = false;
      _committedPosKey = _posKey(vp);
      if (vp.cfi) { _committedCfi = vp.cfi; _viewportCfi = vp.cfi; }
      // Chapters visible in the viewport (mapped by the backend). Sent with
      // chat so the spoiler gate never sits below what the reader can see.
      if (data.structural) {
        S.visibleChapters = data.structural.visible_chapters || [];
      }
      if (data.mismatch) {
        const mk = [
          data.mismatch.top_section, data.mismatch.focus_section,
          data.mismatch.top_chapter, data.mismatch.intent_chapter,
        ].join('|');
        if (mk !== _lastMismatchKey) {
          _lastMismatchKey = mk;
          console.warn('[epub-reader] position mismatch:',
            'top ch.' + data.mismatch.top_chapter,
            '→ intent ch.' + data.mismatch.intent_chapter,
            'visible=' + JSON.stringify(data.mismatch.visible_chapters || []));
        }
      }
      const p = data.position;
      const changed = !_lastDbPos
        || p.chapter_index !== _lastDbPos.chapter
        || p.chunk_index !== _lastDbPos.chunk;
      _lastDbPos = { chapter: p.chapter_index, chunk: p.chunk_index };
      if (changed || force) {                 // only touch UI on real change
        S.currentChapter = p.chapter_index;
        S.currentChunk = p.chunk_index;
        _updateMetaFromPos(p);
      }
    } else {
      _diag.postMiss++;
      // 未命中：不写 _committedCfi，下次滚动到同一位置会重试（只告警一次）。
      if (!_lastMatchMiss) {
        _lastMatchMiss = true;
        console.warn('[epub-reader] /api/sync 未命中:', data.reason || '(无原因)',
          ' fp=', vp.text.slice(0, 24));
      }
    }
  } catch (e) {
    _diag.postErr++;
    if (!_lastSyncFail) {
      _lastSyncFail = true;
      console.warn('[epub-reader] /api/sync 请求失败:', e && e.message);
    }
  }
}

// ── 视口文本收集（供指纹桥映射到 chunk）─────────────────────────────

/** 从 startNode 起向后收集 maxChars 个规范化字符。 */
function _collectTextFrom(startNode, startOff, maxChars) {
  const doc = startNode.ownerDocument;
  const body = doc.body || doc.documentElement;
  if (!body) return '';
  const walker = doc.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  walker.currentNode = startNode;             // nextNode() starts AFTER this
  const parts = [];
  let total = 0;
  const tail = (startNode.nodeValue || '').slice(startOff);
  if (tail) { parts.push(tail); total += tail.length; }
  let n;
  let guard = 0;
  while (total < maxChars && (n = walker.nextNode()) && guard++ < 5000) {
    const s = n.nodeValue || '';
    if (s) { parts.push(s); total += s.length; }
  }
  return _normWs(parts.join(' ')).slice(0, maxChars);
}

// ── Anchor → location ────────────────────────────────────────────────

async function _findSectionForText(anchor) {
  if (!_sectionCache) {
    _sectionCache = [];
    let badSections = 0;
    const spine = book.spine;
    const items = (spine && spine.spineItems) || [];
    for (const sec of items) {
      try {
        // 必须传 book.load —— 否则 Section.load 回退到裸 XHR 请求相对
        // url（OEBPS/Text/...），对服务器 404；book.load 走 zip 归档。
        // 注意：fork 的 Section.load resolve 的是 documentElement（html
        // 元素）而非 document —— 它上面没有 .body/.documentElement 属性，
        // 按 document 的方式取 body 会得到 undefined、文本恒为空串
        // （曾导致恢复锚在全部 section miss → 每次开书都回退封面）。
        const el = await sec.load(book.load.bind(book));
        let root = el;
        if (el && el.body) {
          root = el.body;                     // 标准 document 形态
        } else if (el && el.documentElement && el.documentElement.body) {
          root = el.documentElement.body;     // 兼容真正的 document
        }
        const text = _normWs(root ? (root.innerText || root.textContent || '') : '');
        _sectionCache.push({ index: sec.index, text });
      } catch (_) { badSections++; /* 坏 section 跳过 */ }
    }
  }
  for (const len of [80, 60, 40, 20, 10]) {
    const sub = anchor.slice(0, len);
    if (!sub) continue;
    for (const it of _sectionCache) {
      if (it.text.indexOf(sub) >= 0) return it.index;
    }
  }
  // 未命中诊断：定位"锚前6字"所在的 section 并 dump 实际缓存文本
  // （JSON.stringify 显形不可见字符），或报告各节文本长度分布。
  const probe = anchor.slice(0, 6);
  const hitSec = _sectionCache.find(s => s.text.indexOf(probe) >= 0);
  if (hitSec) {
    const at = hitSec.text.indexOf(probe);
    console.warn('[epub-reader] 锚未命中诊断: 前6字命中 spine', hitSec.index, '@', at,
      '\n  缓存文本=', JSON.stringify(hitSec.text.slice(Math.max(0, at - 5), at + 60)),
      '\n  锚前60字=', JSON.stringify(anchor.slice(0, 60)));
  } else {
    const lens = _sectionCache.map(s => s.text.length);
    console.warn('[epub-reader] 锚未命中诊断: 前6字', JSON.stringify(probe),
      '在全部', _sectionCache.length, '节缓存中不存在; 各节文本长度=',
      JSON.stringify(lens));
  }
  return null;
}

/** 次兜底：按章节标题在 spine 缓存里定位 section（同步、零网络）。
 *
 *  chunk 锚未命中时不能立刻 display(0)（会把视口空降到全书首章，其后的
 *  章节加载补偿序列会持续污染进度 30s+）。章节标题（"第三章 复乐园"）
 *  通常就出现在目标 section 的文本开头，用它定位虽不如 chunk 锚精确，
 *  但落在正确章节内经 /api/sync 可快速收敛回真实 chunk。
 *
 *  title 来自调用方已拿到的 /chunk 响应（chapter_title），故本函数不发
 *  任何请求。标题被拆词/缺失时返回 null，由调用方走"保留视口"兜底。 */
function _findSectionForChapter(title) {
  if (!title || !_sectionCache || !_sectionCache.length) return null;
  const norm = _normWs(title);
  if (!norm) return null;
  // 标题常带"第X章/第X节"等前缀且与正文间有空白差异：整串匹配 + 后半段
  // 匹配（正文性文字，更抗前缀变体）双探针。
  const probes = [norm];
  const tail = norm.slice(Math.floor(norm.length / 2));
  if (tail.length >= 4 && tail !== norm) probes.push(tail);
  for (const p of probes) {
    for (const it of _sectionCache) {
      if (it.text.indexOf(p) >= 0) return it.index;
    }
  }
  return null;
}

async function _locateAndScrollToText(secIdx, anchor) {
  await _displayWithTimeout(secIdx);
  const scroller = _getScroller();
  if (!scroller) {
    console.warn('[epub-reader] 定位: 未找到滚动容器, 视口停留原地');
    return;
  }
  // 章节级兜底（anchor 为空）：目标 section 已由 display 渲染，直接落在
  // 其顶部即可，不做段落级精修——锚不在本 section 内，精修循环必然空转。
  if (!anchor) {
    for (let i = 0; i < 6; i++) {
      if (destroyed) return;
      _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
      const c = (rendition.getContents() || []).find(x => x.sectionIndex === secIdx);
      const frame = c && c.window ? c.window.frameElement : null;
      if (frame) {
        const scRect = scroller.getBoundingClientRect();
        const fr = frame.getBoundingClientRect();
        const top = fr.top - scRect.top + scroller.scrollTop;
        scroller.scrollTop = Math.max(0, top);
        return;
      }
      await _wait(_CONVERGE_TICK_MS);
    }
    console.warn('[epub-reader] 章节兜底: 目标 section 未渲染, 视口停留原地');
    return;
  }
  // 布局收敛循环：continuous 渲染是异步的——display resolve 时前置
  // sections 还在加载增高，一次性 scrollTop 会被后续布局推偏（实测
  // 恢复 (15,6) 后视口被漂到 ch6-9）。以 anchor 文本位置为锚反复校正，
  // 相邻两轮目标值一致（布局稳定）即收敛；目标 section 未渲染就再等。
  let prevTarget = -1;
  let missed = false;    // 上轮目标未算出（尚未渲染）→ 下轮等长一点
  for (let i = 0; i < 14; i++) {
    if (destroyed) return;
    _programmaticUntil = Date.now() + PROGRAMMATIC_MS;   // 收敛期间保持程序窗口
    await _wait(missed ? 200 : _CONVERGE_TICK_MS);   // 未渲染轮给足异步渲染时间
    if (destroyed) return;
    const target = _computeAnchorScrollTop(secIdx, anchor);
    if (target === null) { missed = true; continue; }   // 尚未渲染，下轮再看
    missed = false;
    scroller.scrollTop = Math.max(0, target - 24);   // 视口留 24px 边距
    if (prevTarget >= 0 && Math.abs(target - prevTarget) < 2) {
      // 收敛后再校验一次，防迟到的补偿事件把视口推偏
      await _wait(_CONVERGE_SETTLE_MS);
      if (destroyed) return;
      const again = _computeAnchorScrollTop(secIdx, anchor);
      if (again !== null) scroller.scrollTop = Math.max(0, again - 24);
      return;
    }
    prevTarget = target;
  }
  console.warn('[epub-reader] 定位: 收敛循环14轮未稳定, 视口可能停在 section 顶部');
}

function _wait(ms) { return new Promise(r => setTimeout(r, ms)); }

/** 等 continuous 补载「静默」：既没有新 section 渲染完，滚动高度也稳定。
 *
 *  为什么冷启动恢复需要它（2026-09-14 实测）：`display(cfi)` 的落点是**精确**
 *  的，但连续模式下 display 命中 section 后会继续补载相邻 section——目标
 *  section **之前**的那些被 prepend 到容器顶部，容器高度凭空增高，而 scrollTop
 *  数值不变 → 视口顶对应到更靠前的文本。实测：display 落点在 `epubcfi(/6/16!/4/192)`
 *  （「在小旅馆门口…」），补载静默后视口却滑到 `/4/168`（「思琪一面拗着自己的手指…」）
 *  ——**偏 24 个 DOM 元素 ≈ 12 段**。这就是硬刷恢复"幽灵滚动"的另一半：不是滚动，
 *  是内容在视口底下长出来。
 *
 *  判据：`_lastRenderedAt` 距今 ≥ quietMs **且** scrollHeight 连续 3 次采样不变。
 *  全程续期程序窗口（零落库），带 maxMs 上限防永久挂住。 */
async function _waitRenderQuiet(maxMs, quietMs) {
  const t0 = Date.now();
  const hard = maxMs || 2500;
  const quiet = quietMs || 450;
  let lastH = -1, stable = 0;
  while (Date.now() - t0 < hard) {
    if (destroyed) return false;
    _programmaticUntil = Date.now() + PROGRAMMATIC_MS;   // 等待期静默
    await _wait(100);
    if (destroyed) return false;
    const sc = _getScroller();
    const h = sc ? sc.scrollHeight : -1;
    if (h === lastH) { stable++; } else { stable = 0; lastH = h; }
    if (stable >= 3 && Date.now() - _lastRenderedAt >= quiet) return true;
  }
  return false;
}

/** 冷启动专用的「落点自愈」：核对视口锚，不一致就等补载静默后用 display 重锚。
 *
 *  为什么重锚用 display 而不是几何精修：display 是 epub.js 自身的语义定位，
 *  给定 CFI 就是确定性的（实测一次到位）；而 `_computeCfiScrollTop` 的几何换算
 *  依赖 frameTop/topY，在补载期间不可靠（正是"精修把 /4/186 推成 /4/166"的成因）。
 *
 *  顺序上是「先立即核对、再按需等待」：主题已随 srcdoc 预注入（见
 *  spine.hooks.content 的 _injectThemeIntoDoc），iframe 首帧即终态，display
 *  落点通常当场就对——常见路径零额外等待，遮罩立刻可揭。只有当场不一致
 *  （残留的前置补载把视口推走）才进入静默等待 + 重锚，最多 3 轮。
 *
 *  返回：最终是否落在目标锚上。 */
async function _anchorColdStart(cfi) {
  const key = _cfiAnchorKey(cfi);
  const atKey = () => {
    const cur = _captureViewport();
    return cur && cur.cfi ? _cfiAnchorKey(cur.cfi) : '';
  };
  // 1) 立即核对（常见路径命中）
  if (atKey() === key) {
    return true;
  }
  // 2) 不一致 → 等补载静默再重锚
  for (let attempt = 0; attempt < 3; attempt++) {
    await _waitRenderQuiet(2500, 450);
    if (destroyed) return false;
    const cur = atKey();
    if (cur === key) return true;
    try {
      // 语义重锚：把被 prepend 推走的视口拉回。同样要有界 —— 这个
      // promise 在 continuous 流里可能永不 resolve，裸 await 会把
      // _anchorColdStart 连同其后的 _syncMetaFor（示数刷新）一起卡死。
      await _displayWithTimeout(cfi);
    } catch (_) { return false; }
  }
  return atKey() === key;
}

/** 冷启动「迟到漂移」复核（非阻塞，揭罩后运行）。
 *
 *  display 落点当场正确，不等于 1 秒后仍正确：字体就绪、前置 section 补载
 *  等异步布局事件都可能在此后把视口推走（实测过一次主题注入重排把
 *  /4/192 推成 /4/168）。与前一步的区别是**不阻塞揭罩**——先让用户看到
 *  书，再静默等补载完成复核一次；不一致且期间用户没有交互过，就用
 *  display 语义重锚。用户一旦滚过（_userIntentUntil 在未来）就彻底放手，
 *  绝不与用户抢视口。 */
function _armLateVerify(cfi, gen) {
  const key = _cfiAnchorKey(cfi);
  const myGen = _restoreGen;      // 期间发生显式跳转 → 复核作废（别把跳转拉回进度）
  _waitRenderQuiet(2000, 450).then(() => {
    if (destroyed || gen !== _initGen) return;
    if (myGen !== _restoreGen) return;
    // 期间用户已经滚动过 → 视口现在在哪由用户决定，绝不回拉
    if (Date.now() < _userIntentUntil) return;
    const cur = _captureViewport();
    const curKey = cur && cur.cfi ? _cfiAnchorKey(cur.cfi) : '';
    if (curKey === key) return;
    _programTimerArm();
    rendition.display(cfi)
      .catch(() => { /* 复核重锚失败保持现状 */ })
      .finally(() => _programTimerClear(true));
  }).catch(() => { /* 复核失败不影响阅读 */ });
}

/** 计算把 anchor 文本置于视口顶(含 24px 边距)所需的 scrollTop；
 *  目标 section 或 anchor 文本尚未渲染时返回 null。
 *  用 getBoundingClientRect 差值换算 frameTop，不依赖 offsetParent 定位链。 */
function _computeAnchorScrollTop(secIdx, anchor) {
  if (!rendition) return null;
  const scroller = _getScroller();
  if (!scroller) return null;
  const contents = rendition.getContents() || [];
  const c = contents.find(x => x.sectionIndex === secIdx);
  if (!c || !c.document) return null;
  const node = _findTextNode(c.document, anchor);
  if (!node || !node.parentElement) return null;
  const frame = c.window ? c.window.frameElement : null;
  if (!frame) return null;
  const scrollerRect = scroller.getBoundingClientRect();
  const fr = frame.getBoundingClientRect();
  const frameTop = fr.top - scrollerRect.top + scroller.scrollTop;
  const range = c.document.createRange();
  range.setStart(node, 0);
  range.setEnd(node, Math.min(20, (node.nodeValue || '').length));
  const docRect = c.document.documentElement.getBoundingClientRect();
  const yInDoc = range.getBoundingClientRect().top - docRect.top;
  return frameTop + yInDoc;
}

function _findTextNode(doc, needle) {
  // 跨文本节点累积匹配：chunk 开头的 anchor 常被内联标签（引号/着重号/
  // <em>…）拆成多个 text node，单节点 indexOf 永远命不中 80 字符指纹 →
  // 恢复定位退化为"章节开头"，该落点再经 /api/sync 回写就覆盖真实进度
  //（"每次重开都回到开头"的根源之一）。这里把整章文本按规范化空白拼成
  // 一条流并记录每个节点在流中的区间，命中后反查起点所在节点。
  const root = doc.body || doc.documentElement;
  if (!root) return null;
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const spans = [];          // [{node, start, end}] 节点在 winText 中的区间
  let winText = '';
  let n;
  while ((n = walker.nextNode())) {
    const t = _normWs(n.nodeValue || '');
    if (!t) continue;
    const start = winText ? winText.length + 1 : 0;   // 节点边界 = 一个空格
    winText = winText ? winText + ' ' + t : t;
    spans.push({ node: n, start, end: winText.length });
    for (const len of [80, 60, 40, 20, 10]) {
      const sub = needle.slice(0, len);
      if (!sub) continue;
      const at = winText.indexOf(sub);
      if (at >= 0) {
        for (const sp of spans) {
          if (at >= sp.start && at < sp.end) return sp.node;
        }
        return spans[0].node;
      }
    }
  }
  return null;
}

function _getScroller() {
  if (rendition && rendition.manager && rendition.manager.container) {
    return rendition.manager.container;
  }
  const host = document.getElementById('bookPageNative');
  if (!host) return null;
  for (const el of host.querySelectorAll('*')) {
    if (el.scrollHeight > el.clientHeight + 1) return el;
  }
  return host;
}

/** 程序定位/重排结束后，主动把连续补载推一把（清 ignore 残留 + 驱动 check）。
 *
 *  为什么必需：continuous manager 的"滚到边界自动补载相邻 section"完全由 DOM
 *  scroll 事件驱动，链路是
 *      onScroll() → `this.ignore ? (this.ignore = !1) : this._scrolled()`
 *                → `_scrolled()`（30ms 节流）→ `q.enqueue(() => this.check())`
 *  （以上为 epub.js fork 内部逻辑的注释描述，非本模块调用。）
 *  而 `ignore` 是**单次消费**的布尔，由程序性滚动置位：
 *      · `clear()` 里 `scrollTo(0, 0, true)`
 *      · `display()` 命中分支的 `moveTo()` → `scrollBy(0, top, true)`
 *  两次同步滚动通常只换来浏览器**一个** scroll 事件 → **残留 1 个 true**。
 *  残留期间用户的下一次滚动被静默吞掉：不 emit scrolled、不 check、不补载。
 *  若用户恰好这时滚到已加载内容的边界（容器里只有 offset 预算内的几个
 *  section），滚动条就"碰壁"卡死，必须再滚一次才解套 —— 表现为"一本书被
 *  截成几个区间"。
 *
 *  ⚠️ 旧实现有一条无意中的"清理器"：切页快路径的逐帧像素恢复会连续写
 *  scrollTop，每次写入都产生 scroll 事件、把残留的 ignore 消费干净。本
 *  重构删掉了像素恢复（改 CFI 语义定位），残留就没人清理了。
 *
 *  定位已结束 → ignore 的"忽略下一次滚动"语义不再成立，清位是语义正确的；
 *  再 enqueue 一次 check 让补载不依赖那次可能被吞掉的事件。check 是幂等的
 *  （只判断是否需要加载相邻 section），重复驱动无副作用。 */
function _kickContinuousFill() {
  const m = rendition && rendition.manager;
  if (!m || !m.q || typeof m.check !== 'function') return;
  try {
    m.ignore = false;
    m.q.enqueue(() => m.check());
  } catch (_) { /* best-effort：补载驱动失败不影响阅读主链路 */ }
}

// ── 用户意图 ─────────────────────────────────────────────────────────

/** 人类输入 → 打开上报窗口。这是 _commitViewport 放行的唯一依据。
 *
 *  为什么可以这样：滚轮/触摸/按键/按下是人类独有的信号，程序性滚动
 *  （counter 补偿、display 落点、收敛循环）永远不会产生它们。于是"这次
 *  滚动是不是用户的"由"推断"变成了"已知"。
 *
 *  同时清零 _programmaticUntil —— 用户一旦真的滚动，程序窗口立即让位，
 *  不会因为点击按钮时顺带触发的 pointerdown 把用户滚动挡在门外。 */
function _markUserIntent() {
  _userIntentUntil = Date.now() + USER_INTENT_MS;
  _programmaticUntil = 0;
}

/** 开启「程序定位窗口续期」：display/resize 等异步长扫描期间持续刷新
 *  _programmaticUntil，使补偿滚动始终处于程序窗口内、绝不落库。
 *
 *  为什么必需（2026-09-14 实测）：initNative / resizeNative / jumpToAnchor
 *  的 rendition.display(cfi) 是异步的——continuous 逐 section 渲染的补偿
 *  扫描可持续 10s+，远超 PROGRAMMATIC_MS(2500ms) 的一次性窗口。窗口过期后
 *  _onScrollTick 会把补偿滚动**自动点亮用户意图**并落库（日志 18:26:45 的
 *  8→35 匀速扫描全部 record=True，正源于此）。本函数让窗口在整条定位链
 *  期间持续有效，直到 _programTimerClear() 显式结束。 */
function _programTimerArm() {
  if (_programTimer) return;            // 已在续期，幂等
  _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
  _programTimer = setInterval(() => {
    _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
  }, 500);
}

/** 结束程序定位窗口续期。可传 stopNow：若为 true 立即清空窗口（定位已
 *  完成、允许用户随后的滚动上报），否则保留一个余辉窗口供落点稳定。 */
function _programTimerClear(stopNow = false) {
  if (_programTimer) {
    clearInterval(_programTimer);
    _programTimer = 0;
  }
  if (stopNow) {
    _programmaticUntil = 0;
  } else {
    _programmaticUntil = Date.now() + PROGRAMMATIC_MS;
  }
}

function _bindUserEngage() {
  if (_engageBound) return;
  _engageBound = true;
  for (const ev of USER_INPUT_EVENTS) {
    document.addEventListener(ev, _markUserIntent, { passive: true, capture: true });
  }
}

// iframe 内的 wheel/keydown 不冒泡到父文档——只挂父 document 的话收不到
// 正文里的输入，意图窗口打不开、进度永不落库。这里把同一组监听补挂到每个
// 已渲染 section 的 iframe document 上（rendered 事件后调用，Set 幂等去重）。
// 程序性滚动不产生这些事件，语义不受影响。
function _bindUserEngageOnContents() {
  if (!rendition || destroyed) return;
  try {
    for (const c of rendition.getContents() || []) {
      const doc = c && c.document;
      if (!doc || _engageDocs.has(doc)) continue;
      _engageDocs.add(doc);
      for (const ev of USER_INPUT_EVENTS) {
        doc.addEventListener(ev, _markUserIntent, { passive: true, capture: true });
      }
    }
  } catch (_) { /* best-effort：个别坏 section 不影响其余 */ }
}

/** 滚动上报挂载：主信号是 manager 自己 emit 的滚动事件。
 *
 *  ⚠️ 不用 DOM 的 scroll 作主信号。epub.js 的 continuous manager 会在
 *  prepend/display/收敛时用 `scrollBy(0, heightDelta, true)` 做位置补偿
 *  （counter()）—— 在 DOM 层面，程序性滚动与用户滚动产生的 scroll 事件
 *  完全同构，无法区分。而 manager 的 `onScroll(){ this.ignore ? this.ignore
 *  = !1 : this._scrolled() }`：程序性补偿被 ignore 标志消费掉、
 *  **根本不会 emit**，且内部还有 30ms 节流天然合并高频滚动。（该事件只在
 *  rendition 构造时定向桥接给 reportLocation，不转发给 rendition 的外部
 *  监听器，所以直接挂在 `rendition.manager` 上。）
 *
 *  ⚠️ 事件名：fork 抛的是 **`"scroll"`**（MANAGERS.SCROLL，滚动中立即到达）
 *  与 **`"scrolled"`**（MANAGERS.SCROLLED，停手 150ms 后的防抖收尾）。
 *  两者都挂 —— 只挂 "scrolled" 会漏掉整个连续滚动过程。
 *
 *  是否落库**不在这一层裁决** —— 本层只负责"视口变了就试一次"，放行条件
 *  全部集中在 _commitViewport 的显式窗口里。 */
function _installScrollReporter() {
  if (destroyed || !rendition) return;
  // 幂等：已挂载就只补诊断值 / 补挂迟到的 manager。
  if (_scrollDomHandler) {
    if (!_scrollScroller) _scrollScroller = _getScroller();
    // rendition.manager 是**异步**挂上去的（fork 在 start() 流程里执行
    // `this.manager = new this.ViewManager(...)`），首次安装时经常还没就绪
    // ——实测日志 `滚动上报已挂载: manager=false scroller=true`。这里补挂，
    // 让 manager 事件通道也能生效（DOM 通道本来就已经在工作）。
    if (!_scrollManager && rendition.manager
        && typeof rendition.manager.on === 'function') {
      _scrollManager = rendition.manager;
      _scrollManager.on('scroll', _scrollHandler);
      _scrollManager.on('scrolled', _scrollHandler);
      console.log('[epub-reader] 补挂 manager 滚动事件');
    }
    return;
  }
  const manager = rendition.manager;
  // ⚠️ 这里**不再**要求先找到滚动容器。DOM 通道挂在 document 捕获阶段，本就
  //    不需要预知容器是谁；而 _getScroller() 在本函数被调用时（initNative
  //    中段，rendition 刚 renderTo 完、section 还没渲染）经常返回 null。
  //    旧版据此直接 return，会让**整条链路**——包括根本不依赖容器的
  //    document 通道——一起静默。实测 `__reader.probeOnce().reporter`
  //    四个字段全 false 就是这个：函数在第 3 行就退出了。
  _uninstallScrollReporter();
  _scrollScroller = _getScroller();   // 诊断用，允许为 null
  _scrollManager = manager || null;
  _scrollHandler = () => { _diag.tickMgr++; _onScrollTick(); };
  // ⚠️ 事件名必须以 fork 的 epub.min.js 为准。ContinuousViewManager.scrolled()
  //    抛的是 **MANAGERS.SCROLL = "scroll"**（onScroll 里 30ms 节流后立即 emit，
  //    滚动过程中持续到达）；"scrolled"（MANAGERS.SCROLLED）是**停止滚动 150ms
  //    后的防抖收尾**，且被 `snapper && supportsTouch && needsSnap() ||`
  //    短路——桌面鼠标环境下它可能根本不 emit。只挂 "scrolled" 会让整个连续
  //    滚动过程静默（历史 bug：主信号看似健在、实则从未到达）。两个都接，
  //    重复的那次由 _throttleUntil(400ms) 与 CFI 去重天然合并。
  if (manager && typeof manager.on === 'function') {
    manager.on('scroll', _scrollHandler);
    manager.on('scrolled', _scrollHandler);
  } else {
    console.warn('[epub-reader] manager 不支持事件订阅，仅用 DOM 通道');
  }

  // DOM scroll 是**同等通道**，且挂在 **document 捕获阶段**而不是某一个元素上。
  //
  // 为什么不挂具体元素：滚动容器是被"猜"出来的（`rendition.manager.container`
  // → `stage.getContainer()`）。epub.js 的 stage 会随 resize/换书重建，容器
  // 对象可能被替换；一旦猜错，handler 永远收不到事件，而链路从外面看不出任何
  // 异常（旧实现还用心跳变量自欺：DOM 兜底见"主信号健在"就永不插手，于是
  // 双向死锁、零请求——实测 `__reader.forceCommit()` 能发出请求、滚动却一条
  // 都没有，正是这个形态）。
  //
  // scroll 事件不冒泡，但**捕获阶段仍会逐个祖先执行**，所以 document 上的
  // capture 监听能收到阅读区内任意层级容器发出的滚动——不需要猜容器是谁，
  // 也不受 stage 重建影响。
  _scrollDomHandler = () => {
    // 刻意**不做区域过滤**：_captureViewport 读的永远是阅读区视口，非阅读区
    // 的滚动（侧栏 / 聊天面板 / 目录）只会重复上报同一位置、被 CFI 去重挡回，
    // 无副作用；而过滤条件一旦误杀（滚动容器不在预期子树内、target 是
    // document 或 window），整条链就静默了。两边代价不对称 → 宁滥勿缺。
    _diag.tickDom++;
    _onScrollTick();
  };
  document.addEventListener('scroll', _scrollDomHandler, { passive: true, capture: true });
  console.log('[epub-reader] 滚动上报已挂载: manager=' + !!_scrollManager
    + ' scroller=' + !!_scrollScroller + ' (DOM 通道恒挂)');
}

/** 滚动上报的统一入口（manager 事件与 DOM scroll 共用）。
 *
 *  放行链只有两道，且都是**调用方显式声明的事实**：
 *    · _programmaticUntil —— 程序定位/重排进行中，期间的位移不是用户操作
 *    · _throttleUntil     —— 节流，前置在此处是为了避免每帧抓视口的开销
 *  ⚠️ 这里**不再**自动点亮 _userIntentUntil。意图窗口只能由真实的人类
 *  输入事件（_markUserIntent：wheel/touch/keydown/pointerdown，已挂到
 *  每个 iframe document）打开。旧实现在此处 `_userIntentUntil = now +
 *  USER_INTENT_MS`——一旦程序窗口过期，epub.js 的补偿扫描滚动就会被
 *  自动点亮为"用户意图"并落库（2026-09-14 实测 18:26:45 的 8→35 匀速
 *  扫描全部 record=True 即源于此）。把"点亮意图"从滚动通道剥离、只保留
 *  真实输入事件，从根上杜绝程序滚动伪装成用户操作。
 *
 *  恢复锚（_viewportCfi）仍在滚动通道更新（与意图无关，是语义坐标）。 */
function _onScrollTick() {
  _diag.tick++;
  if (destroyed || !rendition) { _diag.tickDead++; return; }
  const now = Date.now();
  if (now < _programmaticUntil) { _diag.tickProg++; return; }  // 程序窗口内：静默
  if (now < _tickUntil) { _diag.tickThr++; return; }           // 抓取节流（省掉每帧遍历文本节点）
  _tickUntil = now + SCROLL_TICK_MS;
  const vp = _captureViewport();
  if (vp) {
    _diag.lastCfi = vp.cfi || '';
    _diag.lastText = vp.text || '';
    const sc = _getScroller();
    _diag.lastTop = sc ? sc.scrollTop : 0;
  }
  if (vp && vp.cfi) _viewportCfi = vp.cfi;
  _commitViewport(vp ? { vp } : {});
}

function _uninstallScrollReporter() {
  if (_scrollHandler && _scrollManager) {
    try { _scrollManager.off('scroll', _scrollHandler); } catch (_) { /* best-effort */ }
    try { _scrollManager.off('scrolled', _scrollHandler); } catch (_) { /* best-effort */ }
  }
  if (_scrollDomHandler) {
    try {
      document.removeEventListener('scroll', _scrollDomHandler, true);
    } catch (_) { /* best-effort */ }
  }
  _scrollHandler = null;
  _scrollDomHandler = null;
  _scrollScroller = null;
  _scrollManager = null;
}

// ── UI refl/theme ────────────────────────────────────────────────────

/** 取一次 chunk 元信息（total_chunks / chapter_title）并刷新 UI 与 hint。
 *  只读：不带 record，不落库。用于 CFI 恢复后补齐示数 —— 视口已由
 *  display(cfi) 归位，这里不需要任何定位指令。 */
async function _syncMetaFor(chapterIdx, chunkIdx) {
  const gen = _initGen;   // 快速切书：旧书的元信息不许写进新书的示数
  try {
    const r = await fetch(S.API + '/api/books/' + (_nativeBookId || S.currentBook)
      + '/chunk?chapter=' + chapterIdx + '&chunk=' + chunkIdx);
    if (r.ok) {
      const d = await r.json();
      if (gen !== _initGen) return;
      _lastChunkInfo = {
        total_chunks: d.total_chunks || 1,
        chapter_title: d.chapter_title || '',
      };
    }
  } catch (_) { /* 元信息缺失不影响阅读 */ }
  if (gen !== _initGen) return;
  S.currentChapter = chapterIdx;
  S.currentChunk = chunkIdx;
  _lastDbPos = { chapter: chapterIdx, chunk: chunkIdx };
  _updateMetaFromPos({
    chapter_index: chapterIdx,
    chunk_index: chunkIdx,
    total_chunks: (_lastChunkInfo || {}).total_chunks || 1,
    chapter_title: (_lastChunkInfo || {}).chapter_title || '',
  });
}

function _updateMetaFromPos(p) {
  const ch = p.chapter_index || 0;
  const ck = p.chunk_index || 0;
  const total = p.total_chunks || 1;
  const meta = document.getElementById('positionMeta');
  if (meta) {
    // 章节标题优先。chapter_index 是 spine 序号，含版权页/推荐语/序等前置
    // 条目，直接 +1 当"第 N 章"显示会与章节下拉框（用真实标题）自相矛盾：
    // 实测房思琪 chapter_index 4 = 「第一章　乐园」，meta 却写"第 5 章"。
    const label = p.chapter_title
      ? String(p.chapter_title).replace(/\s+/g, ' ').trim()
      : ('第 ' + (ch + 1) + ' 章');
    meta.textContent = label + ' · 段落 ' + (ck + 1) + '/' + total;
  }
  const pos = document.getElementById('chunkPos');
  if (pos) pos.textContent = (ck + 1) + ' / ' + total;
  const chSel = document.getElementById('chapterSelect');
  if (chSel) chSel.value = String(ch);
  if (p.chapter_title && typeof window.updateContextBar === 'function') {
    const bs = document.getElementById('bookSelect');
    const title = bs && bs.selectedOptions && bs.selectedOptions[0]
      ? bs.selectedOptions[0].text : '';
    window.updateContextBar(title, p.chapter_title);
  }
}

function _watchTheme() {
  if (_themeObserver) return;                 // already watching
  let t = null;
  _themeObserver = new MutationObserver(() => {
    clearTimeout(t);
    t = setTimeout(() => applyThemeFromDom(), 150);   // 防抖，避免连续写入重放
  });
  _themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme', 'style', 'class'],
  });
}

// ── Small helpers ────────────────────────────────────────────────────

function _normWs(s) {
  return String(s || '').replace(/\s+/g, ' ').trim();
}

function _timeout(ms) {
  return new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms));
}

async function _fetchWithTimeout(url, ms) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms || 15000);
  try {
    return await fetch(url, { signal: ctrl.signal });
  } finally {
    clearTimeout(t);
  }
}

// ── 诊断句柄（只读，全部是 getter / 纯查询，不改变任何行为）────────────
// 供控制台检查"连续加载是否健康"：已挂载容器数、已渲染 section 数、滚动容器
// 几何、manager 的 ignore 残留与补载预算。这些量正好是"滚动条碰壁 / 书被截成
// 几个区间"的判据。不需要它时可整块删除，不影响任何逻辑。
if (typeof window !== 'undefined') {
  window.__reader = {
    get rendition() { return rendition; },
    get book() { return book; },
    get destroyed() { return destroyed; },
    container() { return _getScroller(); },
    dump() {
      const m = rendition && rendition.manager;
      const host = document.getElementById('bookPageNative');
      const cs = host ? host.querySelectorAll('.epub-container') : [];
      const c = _getScroller();
      const views = (m && m.views && m.views.all) ? m.views.all() : [];
      return {
        destroyed,
        host: host ? { w: host.clientWidth, h: host.clientHeight, hidden: host.hidden } : null,
        containers: cs.length,                      // 应恒为 1，>1 说明 renderTo 重入残留
        iframes: host ? host.querySelectorAll('iframe').length : 0,
        scroller: c ? { top: c.scrollTop, height: c.scrollHeight, client: c.clientHeight } : null,
        views: views.length,
        sections: views.map(v => (v && v.section ? v.section.index : null)),
        manager: m ? {
          ignore: m.ignore,
          scrollTop: m.scrollTop,
          offset: m.settings ? m.settings.offset : null,
          infinite: m.settings ? m.settings.infinite : null,
          bounds: m._bounds ? { w: m._bounds.width, h: m._bounds.height } : null,
          stage: m._stageSize ? { w: m._stageSize.width, h: m._stageSize.height } : null,
          queue: m.q ? m.q.length : null,
        } : null,
        cfi: { viewport: _viewportCfi, committed: _committedCfi },
        pos: _lastDbPos,
        gates: {
          userIntent: Math.max(0, _userIntentUntil - Date.now()),
          programmatic: Math.max(0, _programmaticUntil - Date.now()),
        },
      };
    },
    // 手动驱动一次连续补载。用于验证假设：碰壁时调用，若 sections 明显增加，
    // 即证明"补载被 ignore 残留吞掉"。
    kick() { _kickContinuousFill(); return 'enqueued'; },
    // ── 上报链路自检（只读，不改任何状态）────────────────────────────
    // 一次性回答"为什么滚动不上报"：信号是否挂上、视口能否抓到、闸门剩多少。
    // 判读：
    //   reporter.scrollHandler = false  → 主信号没挂上（事件名/容器问题）
    //   scroller.top 变了但 manager.scrollTop 没变 → onScroll 没跑（onScroll
    //     会同步 this.scrollTop）
    //   viewport = null                 → 视口顶探针抓不到文本节点
    //   gates.programmatic 长期 > 0     → 程序窗口被反复延长，上报被永久静默
    probeOnce() {
      const sc = _getScroller();
      const m = rendition && rendition.manager;
      const vp = _captureViewport();
      return {
        destroyed,
        scroller: sc
          ? { top: sc.scrollTop, h: sc.scrollHeight, client: sc.clientHeight }
          : null,
        managerScrollTop: m ? m.scrollTop : null,
        managerIgnore: m ? m.ignore : null,
        reporter: {
          scroller: !!_scrollScroller,
          manager: !!_scrollManager,
          scrollHandler: !!_scrollHandler,
          domHandler: !!_scrollDomHandler,
        },
        viewport: vp
          ? { cfi: String(vp.cfi || '').slice(0, 64), text: String(vp.text || '').slice(0, 48) }
          : null,
        gates: {
          userIntent: Math.max(0, _userIntentUntil - Date.now()),
          programmatic: Math.max(0, _programmaticUntil - Date.now()),
          throttle: Math.max(0, _throttleUntil - Date.now()),
        },
        dbPos: _lastDbPos,
      };
    },
    // 强制走一次上报（唯一写入口），用来把"链路不通"与"没触发"区分开。
    // 默认 record=false：只验证，不落库。forceCommit(true) 才真的写。
    forceCommit(record) {
      _userIntentUntil = Date.now() + USER_INTENT_MS;
      _programmaticUntil = 0;
      _throttleUntil = 0;
      _commitViewport({ force: true, record: !!record });
      return 'commit dispatched (record=' + !!record + ')';
    },
    // 监听 ms 毫秒内**所有** scroll 事件（document 捕获），回答那个唯一还
    // 没被证实的问题：滚动到底发生在哪个元素上、有没有到达我们挂的通道。
    // 用法：先滚动，再 await __reader.scrollWatch(3000) 期间继续滚动。
    // 判读：total = 0 → 根本没滚动（或滚的是 iframe 内部，跨文档不传播）；
    //       byTarget 里出现 ★ 条目 → manager.container 判断正确。
    scrollWatch(ms) {
      const hits = [];
      const h = (e) => {
        const t = e.target;
        const name = t === document ? 'document'
          : t === _scrollScroller ? '★ours(manager.container)'
            : t.id ? ('#' + t.id)
              : t.className ? ('.' + String(t.className).trim().split(/\s+/)[0])
                : t.tagName;
        hits.push({ target: name, top: t.scrollTop, our: t === _scrollScroller });
      };
      document.addEventListener('scroll', h, true);
      return new Promise((res) => setTimeout(() => {
        document.removeEventListener('scroll', h, true);
        const byTarget = {};
        for (const x of hits) {
          const k = x.target + ' | our=' + x.our;
          byTarget[k] = (byTarget[k] || 0) + 1;
        }
        const out = { total: hits.length, byTarget, lastTop: hits.length ? hits[hits.length - 1] : null };
        window.__lastScrollWatch = out;   // 供事后查看 byTarget（控制台打印会被折叠）
        console.log('[scrollWatch]', out);
        return res(out);
      }, ms || 3000));
    },
    // 链路计数器快照（纯对象，供程序读取）。
    diag() { return Object.assign({}, _diag); },

    /** 一次滚动体检 —— **唯一的排障入口**。
     *
     *  为什么不用 console.table：DevTools 的表格在"选中→复制→粘贴"时经常整块
     *  丢失（实测用户贴回来只剩标题行 `=== 这 3 秒的上报 ===`，表格内容消失），
     *  于是每轮都要再问一次。本函数把全部读数压成**一行 JSON 字符串**返回，
     *  粘贴零损耗。
     *
     *  用法（在控制台粘贴后**立刻**开始滚 3 秒）：
     *    __reader.audit(3000).then(s => console.log('AUDIT=' + s));
     *
     *  读法（delta 是这 3 秒内的增量）：
     *    scrollEvents=0                 → 事件源根本没响（滚的不是这个文档）
     *    tick=0 而 scrollEvents>0       → 我们的 handler 没挂上/已被卸载
     *    tick>0 但 commit=0             → 不可能（同一条链），检查 tickProg/tickThr
     *    tickProg>0 占多数              → 程序窗口(_programmaticUntil)被反复延长
     *    commit>0 但 post=0             → 被闸门/抓取挡下，看 drop* 哪一项在涨
     *    dropNoVp>0                     → _captureViewport 抓不到视口（几何/节点问题）
     *    dropDedup>0 占多数             → 视口 CFI 在滚动中**没变**（抓取陈旧）
     *    post>0                         → 请求已发出，剩下的是后端指纹桥的事 */
    audit(ms) {
      const snap = () => Object.assign({}, _diag);
      const before = snap();
      const hits = [];
      const h = (e) => {
        const t = e.target;
        hits.push(t === _scrollScroller ? '★ours'
          : t === document ? 'document'
            : t.id ? ('#' + t.id)
              : t.className ? ('.' + String(t.className).trim().split(/\s+/)[0])
                : String(t.tagName || '?'));
      };
      document.addEventListener('scroll', h, true);
      const t0 = (window.__syncTrace && window.__syncTrace.log)
        ? window.__syncTrace.log.length : -1;
      return new Promise((res) => setTimeout(() => {
        document.removeEventListener('scroll', h, true);
        const after = snap();
        const delta = {};
        for (const k of Object.keys(after)) {
          if (k === 'lastCfi' || k === 'lastText' || k === 'lastTop') continue;
          const d = after[k] - (before[k] || 0);
          if (d) delta[k] = d;
        }
        const byTarget = {};
        for (const k of hits) byTarget[k] = (byTarget[k] || 0) + 1;
        const trace = (window.__syncTrace && window.__syncTrace.log)
          ? window.__syncTrace.log.length : -1;
        const rows = (window.__syncTrace && window.__syncTrace.log)
          ? window.__syncTrace.log.slice(-6).map((e) => ({
            t: e.at,
            txt: e.req ? String(e.req.pageText || '').slice(0, 16) : '',
            rec: e.req ? e.req.record : null,
            hit: e.res
              ? (e.res.matched
                ? (e.res.position.chapter_index + 'ch/' + e.res.position.chunk_index
                   + 'ck/' + e.res.position.total_chunks)
                : 'MISS')
              : (e.err || 'pending'),
          }))
          : [];
        const out = {
          ms: ms || 3000,
          scrollEvents: hits.length,
          byTarget,
          traceDelta: (trace >= 0 && t0 >= 0) ? (trace - t0) : 'no-probe',
          diagDelta: delta,
          atStartGates: {
            user: Math.max(0, _userIntentUntil - Date.now()),
            prog: Math.max(0, _programmaticUntil - Date.now()),
            thr: Math.max(0, _throttleUntil - Date.now()),
          },
          lastCfi: String(_diag.lastCfi || '').slice(0, 44),
          lastText: String(_diag.lastText || '').slice(0, 18),
          lastTop: Math.round(_diag.lastTop),
          scrollerTop: (function () { const s = _getScroller(); return s ? Math.round(s.scrollTop) : null; })(),
          rows,
        };
        const s = JSON.stringify(out);
        console.log('AUDIT=' + s);
        return res(s);
      }, ms || 3000));
    },

    /** 快速上滚「空降回」取证 —— flingProbe。
     *
     *  为什么存在：该 bug 在 fork counter 加视口守卫后仍稳定复现。静态可推出
     *  四类候选（counter 超量 / 守卫假阴性 / 一锤子漏补 / 内容在视口上方长出），
     *  加上浏览器 CSS scroll anchoring 这个"第二个补偿者"（fork 写于无
     *  anchoring 的年代，两者叠加 = +2H 双重补偿），症状全部同构、读代码裁决
     *  不了。本探针记录：fork 的每次程序性 scrollTop 写入 + 每次无写入解释的
     *  回跳 + 现场几何（scrollTop/scrollHeight/视口顶文本），一次甩轮即可
     *  钉死责任方。
     *
     *  用法（控制台粘贴后立刻快速上滚，复现一次空降）：
     *    __reader.flingProbe(20000).then(s => console.log('FLING=' + s));
     *
     *  读法：
     *    counter{d:Δ} 后紧跟 jump{dTop≈+Δ}    → fork 单独补的（内容稳定，正常）
     *    counter{d:Δ} 后紧跟 jump{dTop≈+2Δ}   → 双重补偿实锤：scroll anchoring
     *                                          又补了一次 → 根治=容器加
     *                                          overflow-anchor:none（二选一，
     *                                          让 fork 独占补偿）
     *    jump{explained:false, dTop>200}      → 无任何程序性写入的回跳 → 浏览器
     *                                          自己干的（anchoring 单独超调）
     *    jump{dH>200} 且无 counter            → 视口上方内容长高且未被补偿
     *    prepend 后无 jump 无 counter          → 补载全程零位移（守卫生效） */
    flingProbe(ms) {
      const m = rendition && rendition.manager;
      const c = m && m.container;
      if (!m || !c) return Promise.resolve('{"error":"no manager — open a book first"}');
      return new Promise((res) => {
        const t0 = Date.now();
        const log = [];
        let lastTop = c.scrollTop;
        let lastH = c.scrollHeight;
        let lastProgAt = -1e9;    // 最近一次程序性写入时刻（jump 归因窗口）
        let inCounter = false;    // counter 内部调 scrollBy 时去重
        const push = (o) => { o.at = Date.now() - t0; if (log.length < 400) log.push(o); };
        const snap = () => {
          const vp = _captureViewport();
          return {
            top: Math.round(c.scrollTop),
            h: Math.round(c.scrollHeight),
            txt: vp && vp.text ? String(vp.text).slice(0, 16) : '',
            cfi: vp && vp.cfi ? _cfiAnchorKey(vp.cfi) : '',
          };
        };
        let anchor = '?';
        try { anchor = getComputedStyle(c).overflowAnchor; } catch (_) { /* 记为 ? */ }
        push(Object.assign({ k: 'init', anchor,
          offset: m.settings ? m.settings.offset : null }, snap()));
        // ── 拦截 fork 全部程序性写入/补载入口（own property 覆盖，delete 即还原）──
        const orig = {
          counter: m.counter && m.counter.bind(m),
          scrollBy: m.scrollBy && m.scrollBy.bind(m),
          scrollTo: m.scrollTo && m.scrollTo.bind(m),
          prepend: m.prepend && m.prepend.bind(m),
          append: m.append && m.append.bind(m),
        };
        m.counter = (t) => {
          inCounter = true;
          lastProgAt = Date.now();
          push(Object.assign({ k: 'counter', d: t && t.heightDelta, H: t && t.height }, snap()));
          try { return orig.counter(t); } finally { inCounter = false; }
        };
        m.scrollBy = (x, y, silent) => {
          lastProgAt = Date.now();
          if (!inCounter) push(Object.assign({ k: 'scrollBy', x, y, silent: !!silent }, snap()));
          return orig.scrollBy(x, y, silent);
        };
        m.scrollTo = (x, y, silent) => {
          lastProgAt = Date.now();
          push(Object.assign({ k: 'scrollTo', x, y, silent: !!silent }, snap()));
          return orig.scrollTo(x, y, silent);
        };
        m.prepend = (sec) => {
          lastProgAt = Date.now();
          push(Object.assign({ k: 'prepend', sec: sec ? sec.index : null }, snap()));
          return orig.prepend(sec);
        };
        m.append = (sec) => {
          push(Object.assign({ k: 'append', sec: sec ? sec.index : null }, snap()));
          return orig.append(sec);
        };
        // ── 回跳监测：上滚复现场景里 >200px 的正向 dTop 即弹回；负向是用户
        //    自己的甩轮（噪音，不记）。dH 大变化记为"内容长高"现场。 ──
        const onScroll = () => {
          const dTop = c.scrollTop - lastTop;
          const dH = c.scrollHeight - lastH;
          if (dTop > 200 || Math.abs(dH) > 200) {
            push(Object.assign({
              k: 'jump', dTop: Math.round(dTop), dH: Math.round(dH),
              explained: (Date.now() - lastProgAt) < 150,   // 紧跟程序性写入 → fork 干的
            }, snap()));
          }
          lastTop = c.scrollTop; lastH = c.scrollHeight;
        };
        document.addEventListener('scroll', onScroll, { passive: true, capture: true });
        const stop = () => {
          document.removeEventListener('scroll', onScroll, true);
          delete m.counter; delete m.scrollBy; delete m.scrollTo;   // 还原原型方法
          delete m.prepend; delete m.append;
          const s = JSON.stringify(log);
          window.__lastFlingProbe = log;   // 事后可逐步检查（控制台打印会折叠）
          console.log('FLING=' + s);
          res(s);
        };
        setTimeout(stop, ms || 20000);
      });
    },
  };
}
