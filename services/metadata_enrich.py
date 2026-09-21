"""One-shot LLM metadata enrichment for imported books.

Fired once per newly imported book (background task from the API server):
clean the marketing-noise title (e.g. "作家榜经典：局外人（…北岛推荐版本！…）")
into the real book title, plus the author's name and nationality.

Pure logic module — takes an ``api_client`` with a ``complete()`` method and
a ``BookDB``; never imports api_server, so it can be unit-verified with a
fake client.  Any failure returns False and never raises, so the import
flow / HTTP response is never disturbed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from storage.db_base import _now_ms
import usage_log

logger = logging.getLogger(__name__)

# ── Limits ─────────────────────────────────────────────────────────────────

MAX_TITLE = 200          # aligned with _generate_title_async (main.py)
MAX_AUTHOR = 100
MAX_NATIONALITY = 30

# ── Prompt ─────────────────────────────────────────────────────────────────

ENRICH_PROMPT = """\
你是图书元数据清洗助手。你的唯一任务是从「文件名、当前书名、当前作者」中\
提取并清洗三样信息：书籍名称、作者国籍、作者姓名。只输出 JSON，不输出任何\
解释、前后缀或代码块标记。

规则：
1. 书籍名称：保留作品的真实名称。允许去除营销性杂质，例如「作家榜经典」\
「某某推荐」「必读」「完整版」「精装版」「无删减」「套装」等前缀、后缀、推荐语，\
以及广告性质的括号说明。但不得对书名进行改写、翻译或增删作品本身的字词。\
若无法判断真实书名，原样保留当前书名。
2. 作者姓名：只保留人名本身。去掉「著、编、译、主编、文、图」等职责后缀及其后的\
补充说明。若无法判断，保留当前作者；若确实没有作者信息，保留「Unknown」原值。
3. 作者国籍：使用标准中文国名（如「法国」「美国」「英国」「日本」「中国」\
「俄罗斯」等）。仅当你非常确定作者国籍时才填写；不确定或无法判断时，该字段\
填空字符串 ""。严禁猜测或编造。
4. 禁止编造：不得依据文件名中的宣传语、榜单或推荐信息臆造书籍名称、作者或国籍；\
不确定就留空或保留原值。

输出必须是严格 JSON，三个字段齐全，键名固定：
{"title": "书名", "author": "作者名", "author_nationality": "国籍或空字符串"}
"""


def build_enrich_user_message(
    filename: str, title: str, author: str, format: str,
) -> str:
    """Compact four-line feed for the LLM — no book content, stays cheap."""
    return (
        "请清洗以下图书的元数据，只输出 JSON。\n\n"
        f"文件名: {filename}\n"
        f"当前书名: {title}\n"
        f"当前作者: {author}\n"
        f"格式: {format}"
    )


# ── Output parsing (three-tier fallback) ───────────────────────────────────

def _parse_enrich_json(raw: str) -> dict | None:
    """Parse the LLM's JSON output, trying progressively looser extraction."""
    s = raw.strip()
    if not s:
        return None
    # 1. As-is
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 2. Strip markdown code fence
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE)
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 3. First { … last } substring
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(s[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


# ── metadata_json merge (three-state) ──────────────────────────────────────

def _merge_metadata_json(current: str | None, enriched: dict) -> str:
    """Merge the enrichment provenance into the existing metadata_json.

    * None (API import_book path) → fresh dict
    * valid JSON dict (epub_import cache_info) → keep all original keys
    * unparseable → rebuilt from scratch
    """
    base: dict = {}
    if current:
        try:
            parsed = json.loads(current)
            if isinstance(parsed, dict):
                base = dict(parsed)
        except json.JSONDecodeError:
            base = {}
    base["llm_enriched"] = enriched
    return json.dumps(base, ensure_ascii=False)


# ── Main entry ─────────────────────────────────────────────────────────────

async def enrich_book_metadata(api_client, db, book_id: str) -> bool:
    """One LLM call per book: clean title / author / author nationality.

    Reads the book + its original filename (from import_log — the bookbank
    copy path is a content hash, useless for the LLM), calls
    ``api_client.complete()`` once, validates the output, and updates the
    books row.  Returns True when the call succeeded (even with no changes),
    False on any failure — never raises.
    """
    try:
        book = db.get_book(book_id)
        if not book:
            return False

        # Original filename lives in import_log (bookbank path is the MD5)
        filename = Path(book.source_path).stem
        import_log = db.get_import_log_by_book(book_id)
        if import_log and import_log.source_filename:
            filename = import_log.source_filename

        user_message = build_enrich_user_message(
            filename, book.title or "", book.author or "", book.format,
        )
        # The provider's non-streaming endpoint has transient failures
        # (~1-in-4 on the free tier); retry before giving up so the one-shot
        # enrichment actually lands.  Still one logical call per book.
        raw = None
        for attempt in range(3):  # 1 initial + 2 retries
            if attempt > 0:
                await asyncio.sleep(attempt)
            with usage_log.source_scope("enrich", ref_id=book_id):
                raw = await api_client.complete(ENRICH_PROMPT, user_message)
            if raw:
                break
        if not raw:
            return False

        parsed = _parse_enrich_json(raw)
        if not parsed:
            return False

        # Field validation — title/author only overwrite when non-empty,
        # nationality accepts any string (empty = cleared, per prompt).
        new_title = book.title
        t = parsed.get("title")
        if isinstance(t, str) and t.strip():
            new_title = t.strip()[:MAX_TITLE]

        new_author = book.author
        a = parsed.get("author")
        if isinstance(a, str) and a.strip():
            new_author = a.strip()[:MAX_AUTHOR]

        new_nationality = book.author_nationality or ""
        n = parsed.get("author_nationality")
        if isinstance(n, str):
            new_nationality = n.strip()[:MAX_NATIONALITY]

        # No change → skip the write (avoids time_updated churn)
        if (new_title == book.title and new_author == book.author
                and new_nationality == (book.author_nationality or "")):
            return True

        enriched = {
            "title": new_title,
            "author": new_author,
            "author_nationality": new_nationality,
            "timestamp": _now_ms(),
        }
        merged = _merge_metadata_json(book.metadata_json, enriched)
        db.update_book(
            book_id,
            title=new_title,
            author=new_author,
            author_nationality=new_nationality,
            metadata_json=merged,
        )
        return True
    except Exception:
        logger.debug("Metadata enrichment failed for %s", book_id, exc_info=True)
        return False
