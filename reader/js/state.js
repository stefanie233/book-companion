/* Liubai — shared application state.
 *
 * All mutable shared state lives here. Modules import { S } and use
 * S.currentBook / S.currentMode / etc.  Assignments work because S is
 * a mutable object (no live-binding restrictions).
 *
 * Also exposed as window.S for HTML onclick handlers and for backward
 * compatibility with code that predates the module split.
 */

export const S = {

  // ── API ───────────────────────────────────────────────────────
  // 同源优先：页面由 API 服务器自己提供，换端口/换主机名都不必改代码。
  // 写死 127.0.0.1:8766 时，从 8777 打开页面能加载、但所有 fetch 都
  // "Failed to fetch"（前端看不出是发给谁的）。file:// 打开没有 origin，
  // 才回退到默认端口。
  API: (location.protocol === 'http:' || location.protocol === 'https:')
    ? location.origin
    : 'http://127.0.0.1:8766',

  // ── Reader state ──────────────────────────────────────────────
  currentBook: null,
  // Last book whose load actually SUCCEEDED. selectBook() compares the
  // dropdown value against this (not currentBook) to detect switches —
  // callers like openBookFromShelf pre-set currentBook before selectBook,
  // which would otherwise make bookChanged always false and leak the old
  // session into the new book.
  _lastLoadedBook: null,
  currentChapter: 0,
  currentChunk: 0,
  totalChunks: 0,
  // 全书进度：chapter_index → 本章之前的 chunk 累计；bookChunkTotal 是全书总数。
  // selectBook() 从 /context 的 structure 算好后写入，chunk/native 两种模式共用。
  bookChunkOffsets: null,
  bookChunkTotal: 0,
  prevAnchor: null,
  nextAnchor: null,
  renderMode: 'chunk',            // 'chunk' (fallback text view) | 'native' (epub.js scrolled)

  // ── Selection ─────────────────────────────────────────────────
  lastSelection: '',
  lastRange: null,
  selectionSource: 'book',
  // Structural coordinates of the selection (book text only). Native EPUB
  // selections carry the epub section (spine position); chunk-mode
  // selections carry the DB chapter/chunk. The backend resolves either to
  // (chapter, chunk) — the selection is "already seen" evidence, used to
  // anchor context and raise the spoiler gate.
  selectionSection: null,
  selectionChapter: null,
  selectionChunk: null,
  selectionCfi: '',
  // Selected text plus ~40 chars before / 120 after, from the same Range.
  // A bare word ("杨修") can occur in several chunks; the anchor text
  // disambiguates which chunk the reader actually selected.
  selectionAnchorText: '',
  // Chapters visible in the viewport, mapped by /api/sync. Sent with chat
  // so the gate never sits below what the reader can already see.
  visibleChapters: [],

  // ── Chat / mode ───────────────────────────────────────────────
  agentName: '留白',              // 主题页可改；由 /api/agent 同步
  agentAvatarUrl: '',             // 用户上传的 agent 头像；空 = 不显示
  userName: '',                   // 自己的称呼；/api/user 同步，空 = 未设置
  userGender: '',                 // 自己的性别：'' 未设置 / 'male' / 'female'
  userAvatarUrl: '',              // 自己的头像；空 = 不显示
  currentMode: 'immersive',
  // 写作页子模式：standard=高效 / detail=细致（决策 161）。只在空白会话
  // 里由预选药丸设置，开始对话后固定；之后只能由 agent 经作者授权切换。
  writingMode: 'standard',
  currentCharacterName: '',
  currentRoleKind: 'character',   // 'character'=图谱实体 | 'author'=作者层
  currentRoleRef: '',             // author 时是 author_id（稳定主键）
  currentMaxChapter: null,        // null=global, int=spoiler-free
  currentSessionId: '',           // active chat session; empty = new session
  // 主页 / 阅读页 / 写作页三条会话线各自独立（工作区存在 app.js 的
  // bc_tab_sessions；currentSessionId 永远只是「当前页这条线」的指针）。
  chatHistory: [],                // [{role, content}] — sent to backend
  chatLastTimestamp: 0,           // ms — for time-separator logic
  // Bumped whenever the visible conversation is replaced (loadSession /
  // newSession). In-flight streams capture it so a late typewriter tick or
  // error handler can never write a reply from the old branch into the new
  // session's chatHistory (which the backend uses as context).
  chatEpoch: 0,

  // ── Shelf ─────────────────────────────────────────────────────
  shelfSort: { key: 'time', dir: 'asc' },
  shelfScene: 'day',              // 'day' (sunlit studio, default) | 'night' (evening library)

  // ── Roleplay setup transient state ────────────────────────────
  rpSelectedBookId: null,
  rpSelectedBookTitle: '',        // remembered for the 「返回角色列表」 re-entry
  rpSelectedCharName: null,
  rpSelectedCharChapter: null,    // first appearance chapter (for immersive default)
  rpSelectedRoleKind: 'character',  // 'character' | 'author'
  rpSelectedRoleRef: '',            // author_id when kind='author'

  // ── Search ────────────────────────────────────────────────────
  searchMode: 'proximity',        // 'proximity' | 'chapter'
  searchQuery: '',                // current search query
  selectedSearchChapter: null,    // currently selected/active chapter (null = all)
  chapterSentenceOffset: 0,       // pagination offset for current view
  searchChapterList: [],          // cached chapter overview data
  searchHasMore: false,           // whether more sentences can be loaded
  searchTotal: 0,                 // total sentences for current view
  searchLoading: false,           // loading guard for scroll lazy-load
  searchPanelOpen: false,         // whether the search results panel is open

  // ── Graph prefetch ───────────────────────────────────────────
  graphPrefetchCache: {},           // bookId → { nodes, edges } from /api/graph/{id}/full
  graphPrefetchPending: {},         // bookId → true (dedup in-flight prefetches)

};

// ── Global exposure ────────────────────────────────────────────────
window.S = S;
