"""来源分级、域名判定与 URL 归一 —— 采集管线的第一道筛子。

三条纪律，全部来自设计稿 03：

1. **搜索 API 只负责发现，页面正文才是证据。** 所以这一层只回答
   「这个网址值不值得抓、抓到了能支撑什么」，不判断内容真假。
2. **tier 不是唯一维度。** 同一篇访谈里，作者直接引语支撑 ``voice``，
   记者转述只支撑 ``biography`` —— 角色在 ``roles_of`` 里分开给。
3. **URL 归一是去重的前提。** 尾斜杠、UTM、host 大小写、fragment
   各变一次，同一页就会变成三条来源、三份证据。

``tier`` 词表：

| tier | 名称 | 能否直接当证据 |
|---:|---|---|
| 1 | 作者自述/官方/书 | 是（仍需逐字引文） |
| 2 | 权威参考（wiki/VIAF/图书馆） | 是；争议事实需第二来源 |
| 3 | 可信媒体/研究/出版社/机构 | 是 |
| 4 | 线索级（百科条目/豆瓣/博客） | 否，只用来找 1–3 |
| 5 | 不可用（内容农场/语录站/AI 摘要） | 否，永不写入 web |
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

#: 来源角色（``author_source_roles.role``）。
ROLES = ("voice", "biography", "bibliography", "relation",
         "theme", "reception", "context")

#: 来源种类（``author_sources.source_kind``）。
SOURCE_KINDS = ("author_text", "author_interview", "publisher",
                "institution", "reference", "media", "scholarship",
                "user", "book", "unknown")

#: 追踪参数 —— 去掉它们不改变页面身份。
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|spm$|ref$|ref_src$|"
                       r"from$|share_|mc_)", re.I)

#: 2 级：权威参考。带 ``.`` 前缀表示后缀匹配。
_TIER2 = ("wikipedia.org", "wikidata.org", "viaf.org", "isni.org",
          "openlibrary.org", "britannica.com", "nlc.cn", "loc.gov",
          "worldcat.org", "book.douban.com")
#: 3 级：可信媒体/研究/出版社/机构。
_TIER3 = ("theparisreview.org", "theguardian.com", "nytimes.com",
          "bbc.com", "bbc.co.uk", "newyorker.com", "theatlantic.com",
          "harpers.org", "poetryfoundation.org", "nobelprize.org",
          "bookerprize.com", "pulitzer.org", "chinawriter.com.cn",
          "chinawriter.org", "people.com.cn", "xinhuanet.com",
          "thepaper.cn", "chinadaily.com.cn", "news.cn", "cnki.net",
          "cqvip.com", "wanfangdata.com.cn")
#: 4 级：线索级。能发现，不能当证据。
_TIER4 = ("baike.baidu.com", "baike.so.com", "douban.com",
          "zhihu.com", "fandom.com", "wikiwand.com", "so.com",
          "sogou.com", "bing.com", "jianshu.com")
#: 5 级：不可用。语录站、AI 摘要站、内容农场。
#: 域名片段只看 **host**（"yulu" 出现在路径里不该判死）；路径规则单独列。
_TIER5_HOSTS = ("mingyan", "yulu", "quotationspage", "brainyquote",
                "azquotes", "poemhunter", "zhidao.baidu.com",
                "wenku.baidu.com", "360doc.com", "baijiahao.baidu.com",
                "toutiao.com")
_TIER5_PATHS = ("goodreads.com/quotes", "sohu.com/a/")

#: 后缀型机构域名 → 3 级（大学/作协/文学馆）。
_INSTITUTION_SUFFIX = (".edu.cn", ".edu", ".ac.cn", ".ac.uk",
                       ".org.cn", ".gov.cn", ".gov")


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def host_of(url: str) -> str:
    """小写 host；解析不出返回空串。"""
    return _host(url)


def _match(host: str, patterns: tuple[str, ...]) -> bool:
    """域名匹配：等于、以 ``.`` 结尾，或 ``host+path`` 含片段。"""
    if not host:
        return False
    for p in patterns:
        if "/" in p:                      # 带路径的规则（如 goodreads.com/quotes）
            continue
        if host == p or host.endswith("." + p):
            return True
    return False


def canonical_url(url: str) -> str:
    """页面身份。去 fragment、去追踪参数、host 小写去 ``www.``、去尾斜杠。

    路径**不做** unquote/大小写归一 —— 那会把两条真的不同的页面并成一条。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw if "://" in raw else "https://" + raw)
    except ValueError:
        return raw
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    scheme = (p.scheme or "https").lower()
    netloc = host
    if p.port and not ((scheme == "http" and p.port == 80)
                       or (scheme == "https" and p.port == 443)):
        netloc = f"{host}:{p.port}"
    path = re.sub(r"/+$", "", p.path or "") or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not _TRACKING.match(k)])
    return urlunparse((scheme, netloc, path, "", query, ""))


def source_id_for(key: str, *, prefix: str = "src") -> str:
    """稳定 id：同一 canonical key 永远得到同一个 id（重抓不新增行）。"""
    return f"{prefix}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def source_kind_of(url: str) -> str:
    """粗分来源种类。判定只服务「能支撑什么」，不追求学术分类。"""
    host, low = _host(url), (url or "").lower()
    if _match(host, _TIER4) and "douban.com/book" in low:
        return "bibliography"
    if _match(host, ("wikipedia.org", "wikidata.org", "viaf.org",
                     "isni.org", "openlibrary.org", "britannica.com",
                     "nlc.cn", "loc.gov", "worldcat.org")):
        return "reference"
    if _match(host, ("theparisreview.org", "newyorker.com",
                     "theguardian.com", "nytimes.com", "bbc.com",
                     "bbc.co.uk")):
        return "author_interview"
    if _match(host, _TIER3) or host.endswith(_INSTITUTION_SUFFIX):
        return "institution" if host.endswith(_INSTITUTION_SUFFIX) else "media"
    if "interview" in low or "访谈" in low:
        return "author_interview"
    return "unknown"


def tier_of(url: str) -> int:
    """来源等级 1–5。未知域名按 3 级处理（可当证据，但 manifest 会报）。"""
    host, low = _host(url), (url or "").lower()
    if any(p in host for p in _TIER5_HOSTS) or any(p in low for p in _TIER5_PATHS):
        return 5
    if _match(host, _TIER2):
        return 2
    if _match(host, _TIER3) or host.endswith(_INSTITUTION_SUFFIX):
        return 3
    if _match(host, _TIER4):
        return 4
    return 3


def roles_of(kind: str, tier: int) -> tuple[str, ...]:
    """这个来源**能支撑什么**。tier 4/5 一律只作线索（空元组）。

    注意：``author_interview`` 同时给 ``voice`` 与 ``biography`` ——
    作者直接引语可以当声音，记者转述只能当生平，由抽取层逐条再判。
    """
    if tier >= 4:
        return ()
    if kind == "book":
        return ("biography", "bibliography", "voice", "relation")
    if kind == "reference":
        return ("biography", "bibliography", "relation")
    if kind == "author_interview":
        return ("voice", "biography", "poetics")
    if kind in ("publisher", "institution"):
        return ("biography", "bibliography", "reception")
    if kind == "media":
        return ("biography", "reception", "theme")
    if kind == "scholarship":
        return ("theme", "reception", "context", "biography")
    return ("biography",)
