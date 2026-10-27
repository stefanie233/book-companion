"""页面抓取与正文抽取 —— **证据的物理来源**。

搜索 API 返回的 ``content`` 是摘要（原生搜索甚至是模型二次总结），
不能拿它当 ``evidence_quote`` 的比对源（设计稿 03 §三.3）。所以必须真抓页。

三条纪律：

1. **只存短片段。** 内存里最多 4000 字，落库的 ``content_excerpt``
   由调用方截到 2000 字以内；不存 raw_html、不存整页。
2. **不绕过任何限制。** 看 robots.txt、不登录、不绕付费墙；被拒就是
   ``blocked``，不伪装成 ``empty``。
3. **不做依赖黑科技的事。** 项目没有 trafilatura/readability，也不为这一
   层加依赖 —— 用标准库 ``html.parser`` 抽正文，够用且可测。

失败分类必须可分辨：``timeout`` / ``blocked`` / ``empty`` /
``unsupported`` / ``http_error``。压成一句「抓不到」会让
「没这个人」和「网站不让抓」混在一起。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from authorgraph.sources.registry import canonical_url, host_of

log = logging.getLogger("authorgraph")

#: 标明身份与用途 —— 抓取纪律的一部分，不用浏览器 UA 伪装。
USER_AGENT = ("BookCompanion/0.1 (personal reading assistant; "
              "short-quote research; +https://github.com/)")

MAX_BYTES = 2_000_000
MAX_TEXT_CHARS = 4000

#: robots 缓存：host → (抓取时间戳, 是否允许)。同一次运行里不重复问。
_ROBOTS: dict[str, tuple[float, bool]] = {}
_ROBOTS_TTL = 3600.0


class _Extractor(HTMLParser):
    """把 HTML 压成「段落 + 标题」的纯文本。"""

    _SKIP = {"script", "style", "noscript", "svg", "canvas", "iframe",
             "nav", "footer", "header", "aside", "form", "button"}
    _BLOCK = {"p", "div", "section", "article", "main", "br", "li",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr",
              "td", "th", "ul", "ol", "figure", "figcaption"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.lang = ""
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "html":
            self.lang = dict(attrs).get("lang") or self.lang
        if t in self._SKIP:
            self._skip_depth += 1
            return
        if t == "title":
            self._in_title = True
        if t in self._BLOCK:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if t == "title":
            self._in_title = False
        if t in self._BLOCK:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        if data.strip():
            self.text_parts.append(data)

    def text(self) -> str:
        raw = "".join(self.text_parts)
        lines = [re.sub(r"[ \t ]+", " ", ln).strip()
                 for ln in raw.split("\n")]
        # 丢掉「首页 / 登录 / 分享」这类导航残渣：单行太短且无标点。
        kept = [ln for ln in lines
                if ln and (len(ln) >= 12 or re.search(r"[。！？.!?：:]$", ln))]
        return "\n".join(kept)


def _meta(html: str, names: tuple[str, ...]) -> str:
    for n in names:
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]*'
            rf'content=["\']([^"\']+)', html, re.I)
        if m:
            return m.group(1).strip()
    return ""


async def _robots_allowed(session: aiohttp.ClientSession, url: str) -> bool:
    host = host_of(url)
    if not host:
        return False
    hit = _ROBOTS.get(host)
    if hit and time.monotonic() - hit[0] < _ROBOTS_TTL:
        return hit[1]
    allowed = True
    try:
        async with session.get(
                f"https://{host}/robots.txt",
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                body = (await resp.text())[:100_000]
                allowed = _robots_parse(body, urlparse(url).path or "/")
    except Exception:                                 # noqa: BLE001
        allowed = True                                 # 取不到就按允许处理
    _ROBOTS[host] = (time.monotonic(), allowed)
    return allowed


def _robots_parse(body: str, path: str) -> bool:
    """最小 robots 解析：只看 ``User-agent: *`` 段里的 Disallow。"""
    stray, in_star, dis = False, False, []
    for line in body.splitlines():
        ln = line.split("#", 1)[0].strip()
        if not ln or ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            in_star = v == "*"
        elif k == "disallow" and in_star and v:
            dis.append(v)
    for rule in dis:
        if rule == "/" or path.startswith(rule.rstrip("*")):
            stray = True
    return not stray


async def _read_text(resp: aiohttp.ClientResponse) -> str:
    raw = await resp.content.read(MAX_BYTES)
    ctype = (resp.headers.get("Content-Type") or "").lower()
    m = re.search(r"charset=([\w-]+)", ctype)
    enc = m.group(1) if m else "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _language(lang_attr: str, text: str) -> str:
    """HTML lang 优先；没有就看正文里 CJK 占比（不引入语言检测依赖）。"""
    tag = (lang_attr or "").strip().lower().split("-")[0]
    if tag:
        return tag
    cjk = sum(1 for ch in text[:2000] if "一" <= ch <= "鿿")
    return "zh" if cjk > 40 else "en"


async def fetch_page(session: aiohttp.ClientSession, url: str, *,
                     timeout: float = 20.0, max_chars: int = MAX_TEXT_CHARS,
                     respect_robots: bool = True) -> dict[str, Any]:
    """抓一页。返回设计稿 03 §四.3 的形状；**永不 raise**。"""
    url = (url or "").strip()
    out: dict[str, Any] = {
        "url": url, "canonical_url": canonical_url(url), "title": "",
        "publisher": host_of(url), "language": "", "main_text": "",
        "content_hash": "", "published_at": "", "status": "error",
        "http_status": 0, "error": "",
    }
    if not url.startswith(("http://", "https://")):
        out["status"] = out["error"] = "unsupported"
        return out
    if respect_robots and not await _robots_allowed(session, url):
        out["status"], out["error"] = "blocked", "robots.txt disallows"
        return out
    try:
        async with session.get(
                url, headers={"User-Agent": USER_AGENT,
                              "Accept": "text/html,application/xhtml+xml"},
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True) as resp:
            out["http_status"] = resp.status
            if resp.status != 200:
                out["status"] = "http_error"
                out["error"] = f"HTTP {resp.status}"
                return out
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and not any(t in ctype for t in
                                 ("text/html", "text/plain", "xhtml")):
                out["status"], out["error"] = "unsupported", ctype[:80]
                return out
            html = await _read_text(resp)
            out["canonical_url"] = canonical_url(str(resp.url)) or out["canonical_url"]
    except Exception as exc:                          # noqa: BLE001
        kind = "timeout" if "imeout" in type(exc).__name__ or "imeout" in str(exc) else "error"
        out["status"], out["error"] = kind, str(exc)[:200]
        return out

    parser = _Extractor()
    try:
        parser.feed(html)
    except Exception:                                 # noqa: BLE001
        pass
    text = (parser.text() or "").strip()
    if not text:
        out["status"], out["error"] = "empty", "no text extracted"
        return out
    out.update({
        "title": (parser.title or _meta(html, ("og:title", "twitter:title"))
                  or "").strip(),
        "publisher": (_meta(html, ("og:site_name", "application-name"))
                      or host_of(url)),
        "published_at": _meta(html, ("article:published_time",
                                     "og:published_time", "date")),
        "language": _language(parser.lang, text),
        "main_text": text[:max_chars],
        "status": "ok",
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    })
    # 摘要片段：取前 2000 字，按段落边界切，避免半句。
    excerpt = text[:2000]
    cut = excerpt.rfind("\n")
    out["content_excerpt"] = excerpt[:cut] if cut > 600 else excerpt
    return out
