/* Book Companion — shared application state.
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
  API: 'http://127.0.0.1:8765',

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
  prevAnchor: null,
  nextAnchor: null,
  renderMode: 'chunk',            // 'chunk' (fallback text view) | 'native' (epub.js scrolled)

  // ── Selection ─────────────────────────────────────────────────
  lastSelection: '',
  lastRange: null,
  selectionSource: 'book',

  // ── Chat / mode ───────────────────────────────────────────────
  currentMode: 'immersive',
  currentCharacterName: '',
  currentMaxChapter: null,        // null=global, int=spoiler-free
  currentSessionId: '',           // active chat session; empty = new session
  chatHistory: [],                // [{role, content}] — sent to backend
  chatLastTimestamp: 0,           // ms — for time-separator logic

  // ── Shelf ─────────────────────────────────────────────────────
  shelfSort: { key: 'time', dir: 'asc' },
  shelfScene: 'day',              // 'day' (sunlit studio, default) | 'night' (evening library)

  // ── Roleplay setup transient state ────────────────────────────
  rpSelectedBookId: null,
  rpSelectedCharName: null,
  rpSelectedCharChapter: null,    // first appearance chapter (for immersive default)

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
