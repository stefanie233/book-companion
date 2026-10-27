"""EPUB 探查工具 —— 供导入 agent 自主调用的只读探针。

职责边界
--------
本模块**不判断章节结构**（哪些文件是 body）。那属于结构层：agent 读每个
html 文件的开头若干字自行判断，本模块只负责把那些字递过去。

本模块服务于**特征层**：body 内部有哪几种文本形态。窄门里叙述正文
(calibre14, ``font-size:1em``) 与信件 (calibre19, ``0.875em``) 的段均长
都是 72-73 字，统计上无法区分，只有 CSS 定义能分开 —— 这正是本模块存在
的理由。若这层不识别，下游提取会把信件的口吻当成叙述者的口吻。

四个工具全部只读、无副作用、可独立调用。形态命名由 agent 自行归纳（书籍
文字形态过于自由，无法定义通用词表），本模块只提供原料，不预设任何词表。

    probe_spine_list(reader)                  列文件（含前 N 字，供结构判断）
    probe_tag_themes(reader, spine_indices)   统计 <p class> 主题分布
    probe_css_rules(reader, selectors)        给出选择器的真实 CSS 定义
    probe_grep(reader, pattern, ...)          检索文本，回报所属标签主题
"""

from __future__ import annotations

import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path

from file_readers.epub_reader import EpubReader, _clean_html
from file_readers.html_skeleton import (
    _clean_inline_tags, _split_by_h_tags, heading_tag_of,
)

# ── Patterns ─────────────────────────────────────────────────────────────

# HTML 里 <p> 不允许嵌套，非贪婪匹配到第一个 </p> 即可。
_P_RE = re.compile(r"<p(\s[^>]*)?>(.*?)</p>", re.DOTALL | re.IGNORECASE)
_CLASS_RE = re.compile(r'class="([^"]*)"')
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_WS_RE = re.compile(r"\s+")
# <head> 里的 <title> 常是「未知」这类占位串，会挤占递给 agent 的前 N 字。
_HEAD_RE = re.compile(r"<head[^>]*>.*?</head>", re.DOTALL | re.IGNORECASE)
# 目录/导航片段几乎全是 <a>，正文几乎没有 —— 比 head 更硬的结构信号。
_A_RE = re.compile(r"<a[\s>]", re.IGNORECASE)


def _p_selector(attrs: str) -> str:
    """``<p>`` 的 class 属性 → 主题选择器（``p.calibre14`` / 无 class 时 ``p``）。"""
    m = _CLASS_RE.search(attrs or "")
    cls = m.group(1).strip() if m else ""
    return f"p.{cls}" if cls else "p"


def _iter_p(html: str):
    """遍历 HTML 里的 ``<p>``，产出 ``(selector, text)``；空文本跳过。"""
    for m in _P_RE.finditer(html):
        text = _clean_inline_tags(m.group(2))
        if text:
            yield _p_selector(m.group(1) or ""), text


# ── 工具 1：列文件 ────────────────────────────────────────────────────────


def probe_spine_list(
    reader: EpubReader, head_chars: int = 100, skip_empty: bool = True,
) -> list[dict]:
    """列出 spine 项，每项附前 *head_chars* 字。

    ``head`` 是结构判断的原料 —— agent 靠它分辨哪些文件是 body、哪些是
    体例页。本函数只搬运，不做任何判断。

    *skip_empty* 默认跳过 ``chars == 0`` 的空占位页（Calibre 转换常留下一串
    461 字节的空文件，实测局外人有 13 个）。它们没有任何内容可判，递给 agent
    只会诱发瞎猜 —— 实测被反复判成 cover，把正文中间的空白页误当封面。
    跳过时 ``index`` 仍是真实 spine 序号，所以列表会是不连续的。

    ``paras`` / ``links`` 是补充的**结构信号**：Calibre 拆出来的目录页常常
    只剩几个字（head 里就一个「第一章」），光看 head 分不清它是正文标题还是
    导航链接；而这类文件 ``paras`` 为 0、``links`` 不为 0，一眼可辨 ——
    省掉 agent 逐个 ``read_book_file`` 去确认的轮次。
    """
    reader._ensure_loaded()
    out: list[dict] = []
    for si in range(len(reader._spine_ids)):
        try:
            html = _HEAD_RE.sub("", reader._read_spine_html(si))
        except Exception:
            continue
        text = _clean_html(html)
        if skip_empty and not text:
            continue
        out.append({
            "index": si,
            "href": reader._item_map.get(reader._spine_ids[si], ""),
            "label": reader.get_chapter_label(si),
            "chars": len(text),
            "paras": sum(1 for _ in _iter_p(html)),
            "links": len(_A_RE.findall(html)),
            "head": _WS_RE.sub(" ", text)[:head_chars].strip(),
        })
    return out


# ── 工具 2：标签主题统计 ──────────────────────────────────────────────────


def probe_tag_themes(
    reader: EpubReader, spine_indices: list[int] | None = None,
) -> dict:
    """统计 ``<p class>`` 主题分布：段数、字数、均长、占比。

    *spine_indices* 限定统计范围（``None`` = 全部 spine）。特征层通常只传
    body 文件 —— 哪些是 body 是结构层的结论，由 agent 判断后传进来。

    只按 ``tag.class`` 字面分组，**不合并**视觉等价的主题（窄门的
    ``.calibre17`` 与 ``.c1`` 规则几乎相同却是两个键）。是否合并、按什么
    口径合并，交由 agent 对照 :func:`probe_css_rules` 自行决定。

    只统计 ``<p>`` 标签**内部**的文本。``<p>`` 之外的一切都不进 ``themes``
    —— 其他元素（``<h1>``/``<div>``/``<li>``…）的文本，以及没有标签包裹的
    散落内容（窄门里恰好是目录/扉页/版权页）—— 其总量单列在
    ``uncovered_chars``，让 agent 知道还有这么一块没被覆盖。
    """
    reader._ensure_loaded()
    n = len(reader._spine_ids)
    idxs = list(range(n)) if spine_indices is None else [
        i for i in spine_indices if 0 <= i < n
    ]

    paras: Counter = Counter()
    chars: Counter = Counter()
    all_chars = 0
    for si in idxs:
        try:
            html = _HEAD_RE.sub("", reader._read_spine_html(si))
        except Exception:
            continue
        all_chars += len(_clean_html(html))
        for sel, text in _iter_p(html):
            paras[sel] += 1
            chars[sel] += len(text)

    total_p = sum(paras.values())
    total_c = sum(chars.values())
    themes = [
        {
            "selector": sel,
            "paragraphs": cnt,
            "chars": chars[sel],
            "avg_len": chars[sel] // cnt if cnt else 0,
            "para_ratio": round(cnt / total_p, 4) if total_p else 0.0,
            "char_ratio": round(chars[sel] / total_c, 4) if total_c else 0.0,
        }
        for sel, cnt in paras.most_common()
    ]
    return {
        "spine_indices": idxs,
        "paragraph_total": total_p,
        "char_total": total_c,
        # <p> 之外的全部文本量（其他元素的文本 + 无标签散落内容）
        "uncovered_chars": max(0, all_chars - total_c),
        "theme_count": len(themes),
        "themes": themes,
    }


# ── 工具 3：CSS 真实定义 ──────────────────────────────────────────────────


def probe_css_rules(
    reader: EpubReader, selectors: list[str] | None = None,
) -> dict:
    """解析 EPUB 内的所有 CSS，返回选择器 → 属性字典。

    *selectors* 限定要哪些选择器（``None`` = 全部）。直接把
    :func:`probe_tag_themes` 给出的 ``selector`` 传进来即可 —— 形如
    ``p.calibre14`` 的主题选择器会自动回退匹配样式表里的 ``.calibre14``。

    传 ``None`` 会把样式表里**定义但从未使用**的类一并带出，那通常是噪音
    （窄门 39 个类只用了 26 个）；一般应先取主题统计、再按需查定义。

    同一选择器在多个样式表出现时，按文件顺序后者覆盖前者 —— 是 CSS 层叠
    的简化近似，不处理权重与继承。``.calibre22`` 这类未声明 ``font-size``
    的规则，其真实字号来自继承，本函数不会替它回溯。
    """
    reader._ensure_loaded()
    try:
        zf = reader._zip
        names = [n for n in zf.namelist() if n.lower().endswith(".css")]
    except Exception:
        return {"stylesheets": [], "rules": {}}

    rules: dict[str, dict] = {}
    for name in names:
        try:
            css = zf.read(name).decode("utf-8", "ignore")
        except Exception:
            continue
        css = _CSS_COMMENT_RE.sub("", css)
        for sel_blob, body in _CSS_RULE_RE.findall(css):
            props: dict[str, str] = {}
            for decl in body.split(";"):
                key, sep, val = decl.partition(":")
                if sep and key.strip():
                    props[key.strip().lower()] = val.strip()
            if not props:
                continue
            for one in sel_blob.split(","):
                one = one.strip()
                if one:
                    rules.setdefault(one, {}).update(props)

    if selectors is not None:
        picked: dict[str, dict] = {}
        for s in selectors:
            if s in rules:
                picked[s] = rules[s]
                continue
            dot = s.find(".")          # p.calibre14 → .calibre14
            if dot > 0 and s[dot:] in rules:
                picked[s] = rules[s[dot:]]
        rules = picked
    return {"stylesheets": names, "rules": rules}


# ── 工具 4：检索 ─────────────────────────────────────────────────────────


def probe_grep(
    reader: EpubReader,
    pattern: str,
    spine_indices: list[int] | None = None,
    limit: int = 50,
    snippet_chars: int = 80,
) -> dict:
    """在指定 spine 内检索正则 *pattern*，回报匹配段落及其所属标签主题。

    回报 ``selector`` 是为了让 agent 把检索结果和 :func:`probe_tag_themes`
    的统计对上 —— 例如搜一个叙述常用虚词，看它落在哪些主题，即可印证该主题
    是不是叙述正文。

    ``paragraph_index`` 是该文件内**非空** ``<p>`` 的序号，与
    :func:`probe_tag_themes` 的计数口径一致。*limit* 是硬上限，达到即返回
    并置 ``truncated``。
    """
    reader._ensure_loaded()
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return {"error": f"invalid regex: {exc}", "matches": []}

    n = len(reader._spine_ids)
    idxs = list(range(n)) if spine_indices is None else [
        i for i in spine_indices if 0 <= i < n
    ]

    matches: list[dict] = []
    for si in idxs:
        try:
            html = reader._read_spine_html(si)
        except Exception:
            continue
        for pi, (sel, text) in enumerate(_iter_p(html)):
            if not rx.search(text):
                continue
            matches.append({
                "spine_index": si,
                "paragraph_index": pi,
                "selector": sel,
                "text": text[:snippet_chars],
            })
            if len(matches) >= limit:
                return {"pattern": pattern, "matches": matches, "truncated": True}
    return {"pattern": pattern, "matches": matches, "truncated": False}


# ── 工具 5：标题轮廓 ──────────────────────────────────────────────────────


def _toc_depth_by_label(reader: EpubReader) -> dict[str, int]:
    """归一化目录标题 → 目录深度（与导入侧 ``_build_depth_index`` 同口径）。

    刻意在本模块内重写一遍，而不是从 ``services.epub_import_service``
    import —— 否则 file_readers 会反向依赖 services。口径必须一致：两边都
    是「去掉全部空白后取最浅深度」。
    """
    idx: dict[str, int] = {}
    for entry in getattr(reader, "_toc_all", []) or []:
        label, depth = entry.get("label"), entry.get("depth")
        if label and depth is not None:
            idx.setdefault(_WS_RE.sub("", label), depth)
    return idx


def probe_heading_outline(
    reader: EpubReader,
    spine_indices: list[int] | None = None,
    limit: int = 300,
) -> dict:
    """回报各文件的**标题轮廓**：标签名 + 文字 + 是否在书目录 + 目录深度。

    刻意复用 ``_split_by_h_tags`` —— 它给出的标题集合**就是导入时会切成章的
    那一批**（无正文的短标题已被它滤掉）。所以这不是「HTML 里所有 h 标签」，
    而是「环境会怎么切」。agent 据此分辨真小节（h3、in_toc=true）与图注/地图
    （h5/h6、in_toc=false，书目录里没有条目，只是文件内部结构）。

    ``limit`` 是标题条数硬顶，默认 300，建议不超过 500（人类简史 150 章量级）。
    """
    reader._ensure_loaded()
    depth_by_label = _toc_depth_by_label(reader)
    n = len(reader._spine_ids)
    idxs = list(range(n)) if spine_indices is None else [
        i for i in spine_indices if 0 <= i < n
    ]

    headings: list[dict] = []
    tag_hist: Counter = Counter()
    in_toc_count = 0
    truncated = False
    for si in idxs:
        try:
            html = reader._read_spine_html(si)
        except Exception:
            continue
        for text, _body, matched in _split_by_h_tags(html):
            if not text:
                continue
            tag = heading_tag_of(matched)
            depth = depth_by_label.get(_WS_RE.sub("", text))
            tag_hist[tag or "?"] += 1
            if depth is not None:
                in_toc_count += 1
            if len(headings) >= limit:
                truncated = True
                break
            headings.append({
                "spine_index": si,
                "tag": tag,
                "text": text,
                "in_toc": depth is not None,
                "toc_depth": depth,      # None = 不在书目录
            })
        if truncated:
            break

    return {
        "spine_indices": idxs,
        "heading_count": len(headings),
        "truncated": truncated,
        "tag_histogram": dict(tag_hist.most_common()),
        "in_toc_count": in_toc_count,
        "toc_total": len(getattr(reader, "_toc_all", []) or []),
        "headings": headings,
    }


# ── 解压产物 ──────────────────────────────────────────────────────────────

# 只放行文本类文件 —— images/ 里的 jpeg 读进来只会是乱码噪音。
_TEXT_EXTS = frozenset({".html", ".xhtml", ".htm", ".css", ".ncx", ".opf", ".xml"})
_MAX_LIMIT = 20000          # 单次返回硬顶，防 agent 一次拉爆上下文


def ensure_book_dir(epub_path: str | Path, out_dir: str | Path) -> Path:
    """惰性把 EPUB 解压到 *out_dir*；已解压则原样返回。

    以 ``META-INF/container.xml`` 是否存在作为「已解压」标记（EPUB 规范强制
    该文件存在，任何合规 EPUB 都有）。解压过程防 zip-slip —— 条目路径逃出
    *out_dir* 的一律跳过。
    """
    out = Path(out_dir)
    if (out / "META-INF" / "container.xml").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    with zipfile.ZipFile(epub_path) as zf:
        for member in zf.infolist():
            target = (out / member.filename).resolve()
            if not str(target).startswith(str(root)):
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return out


def read_book_file(
    book_dir: str | Path,
    path: str,
    offset: int = 0,
    limit: int = 4000,
    strip_tags: bool = False,
) -> dict:
    """读取解压后书籍目录里的一个文本文件，支持分页续读。

    Args:
        book_dir: 解压根目录（:func:`ensure_book_dir` 的返回值）。
        path: 相对 *book_dir* 的路径，如 ``"text/part0011.html"``。
        offset: 起始字符位置，续读时传上一次的 ``next_offset``。
        limit: 本次最多返回多少字符（默认 4000 ≈ 2700 token，硬顶 20000）。
        strip_tags: True 时剥掉标签只留正文；默认 False —— 保留标签是为了
            让 agent 看得见 ``<p class="…">`` 的包裹关系。开启后
            ``offset``/``limit``/``total_chars`` 都作用于**纯文本**，
            与关闭时不是同一把尺子，续读要沿用同一模式。

    按**字符**而非行切分：Calibre 生成的 html 一行常装下一整段 ``<p>``，
    按行切会让 agent 一次吞掉整段，粒度不可控。

    ``truncated`` 与 ``next_offset`` 要一起看 —— 缺了它们 agent 会误以为
    拿到的是全文（窄门最大单文件 51944 字符）。
    """
    root = Path(book_dir).resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)):
        return {"error": "path escapes book dir", "path": path}
    if target.suffix.lower() not in _TEXT_EXTS:
        return {"error": f"not a text file: {path}", "path": path}
    if not target.is_file():
        return {"error": f"no such file: {path}", "path": path}

    raw = target.read_text("utf-8", "ignore")
    if strip_tags:
        # 必须**先整体剥标签再切片**。反过来（先切再剥）会切出残缺的 HTML
        # 片段，_clean_html 对不完整结构的输出无意义（实测会返回 "<"）。
        # 代价是 strip_tags=True 时 offset/limit 作用于纯文本、total_chars
        # 也是纯文本长度，与 False 时不是同一把尺子。
        body = _clean_html(_HEAD_RE.sub("", raw))
    else:
        body = raw
    total = len(body)
    offset = max(0, min(offset, total))
    limit = max(1, min(limit, _MAX_LIMIT))
    chunk = body[offset:offset + limit]
    end = offset + limit
    return {
        "path": path,
        "total_chars": total,
        "offset": offset,
        "returned_chars": len(chunk),
        "truncated": end < total,
        "next_offset": end if end < total else None,
        "text": chunk,
    }
