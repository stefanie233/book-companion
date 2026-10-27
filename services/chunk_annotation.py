"""把段落级排版签名渲染成提取器看得懂的**形态标签**。

为什么需要这一层
----------------
导入分析 agent 读完整本样张后判出 ``book_text_forms``（``p.calibre19`` = 书信）。
此前它只以「选择器 → 形态名」的映射表注入提取器的 system prompt，而提取器读的
``book_chunks.content`` 是**扁平文本** —— 映射表无处落地，等于告诉它「这本书里
有信」却不说「你手上这段里哪几行是信」。

实测窄门第三章 chunk 0（846 字 / 10 段）：

    p|calibre14  我们到校的第二天，我收到这样一封信：
    p|calibre19  亲爱的杰罗姆：          ← 信
    p|calibre19  对于你的提议我考虑了很久……
    p|right      阿莉莎                  ← 落款
    p|calibre14  我们停止相爱！怎么可能有这种事！……

叙述、书信、落款混在一坨。而 ``book_chunks.paragraph_sigs`` **逐段标对了**，
只是全仓无人读。本模块就是那个读取方。

铁律：**只在内存里渲染，绝不写回库**
------------------------------------
``book_chunks.content`` 上建着 FTS 索引（``book_fts``），且指纹匹配
（``library_service._fp_hit``）靠它定位阅读进度与批注。内容一旦带标签：
搜索被污染、指纹全 miss、进度与批注 relink 全断。所以标签只存在于**送给 LLM
的那份字符串**里，库里的 ``content`` 一个字节都不动。
"""

from __future__ import annotations

import json
import re

from file_readers.style_resolver import selector_key, split_selectors

# 标签定界符用 «» 而不是 [] —— 正文里本来就有方括号（窄门的译者注 ``[1]``、
# 人类简史的图注 ``图1``），用方括号会让模型分不清哪个是标注。
_TAG_OPEN = "«"
_TAG_CLOSE = "»"

# 形态名上限。分类器自定的名字可以很长（人类简史实测「楷体成段引文（故事/
# 诗歌/文书/广告/法条）」），逐段前缀会把提示词撑大 37%；截短后实测只涨 3%。
# 取 10 是为了让常见名完整（「地图标题与表格标题」正好 9 字）。
_NAME_MAX = 10


# 分类器的命名习惯：占比不足 2% 的形态归入「其他」，写成 `其他：xxx` 或
# `其他（xxx）`。对逐段标签来说，「其他」三个字不承载信息 —— 剥掉它，
# 剩下的才是要告诉提取器的东西。
_MISC_RE = re.compile(r"^其他\s*[：:·]?\s*")
_PAREN_RE = re.compile(r"[（(]([^）)]*)[）)]")


def short_name(name: str) -> str:
    """形态名 → 逐段标签用的短名（≤ *NAME_MAX* 字）。

    三步，都是为了不让标签变成噪音：

    1. 剥掉 `其他：` 前缀与尾部斜杠（实测库里存着 `其他：楷体引文/`）
    2. 括号处理分两种：剥完前缀**以括号开头**的（`其他（小字号特排段）`），
       括号里才是信息；否则括号里多半是举例，取括号**之前**的主体
       （`楷体成段引文（故事/诗歌/…）` → `楷体成段引文`）
    3. 仍超长才硬截

    名字退化成空（如整条就叫「其他」）时返回空串 —— 调用方不加标签。
    """
    s = (name or "").strip()
    s = _MISC_RE.sub("", s).strip().strip("/").strip()
    if not s:
        return ""
    m = _PAREN_RE.search(s)
    if m:
        head = s[:m.start()].strip()
        s = head or m.group(1).strip()
    return s.strip()[: _NAME_MAX]


def form_map(text_forms) -> dict[str, str]:
    """``[{"selector","name"}]`` → ``{归一化选择器: 短形态名}``。

    走 :func:`split_selectors` —— 分类器会把多个选择器逗号连成一条，不拆就
    一条都命中不了。
    """
    out: dict[str, str] = {}
    for f in text_forms or []:
        if not isinstance(f, dict):
            continue
        name = short_name(str(f.get("name") or ""))
        if not name:
            continue
        for sel in split_selectors(str(f.get("selector") or "")):
            key = selector_key(sel)
            if key:
                out.setdefault(key, name)
    return out


def _as_list(v) -> list | None:
    """列可能是 JSON 字符串（落库形态）也可能是已解析的列表。"""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return None
    return v if isinstance(v, list) else None


def annotate_chunk(content: str, sigs, offsets, forms: dict[str, str]) -> str:
    """逐段加 ``«形态名» `` 前缀；**查不到形态的段落原样输出**。

    每段都标，不做「主导形态免标」的特殊化 —— 书信体小说里书信本身就是主导
    形态，那样最该标的反而没标。

    任何一处对不上（段数不等、偏移越界或重叠）就**整体放弃、原样返回**：
    宁可没有标签，也不能渲染出一段错位的文本骗过提取器。
    """
    if not content or not forms:
        return content
    spans = _as_list(offsets)
    sig_list = _as_list(sigs)
    if not spans or not sig_list or len(spans) != len(sig_list):
        return content

    out: list[str] = []
    pos = 0
    for span, sig in zip(spans, sig_list):
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            return content
        start, length = int(span[0]), int(span[1])
        # start < pos = 偏移重叠；越界同理 —— 都是坏数据，整体放弃
        if start < pos or start + length > len(content):
            return content
        out.append(content[pos:start])
        para = content[start:start + length]
        name = forms.get(selector_key(str(sig)))
        out.append(f"{_TAG_OPEN}{name}{_TAG_CLOSE} {para}" if name else para)
        pos = start + length
    out.append(content[pos:])
    return "".join(out)


def annotate_chunks(chunks: list[dict], forms: dict[str, str]) -> None:
    """就地给一批 ``chunk_dicts`` 的 ``content`` 加标签（改的是副本，不是库）。

    调用方传进来的 dict 是 ``get_chunks()`` 现组的，改它不碰数据库。
    """
    if not forms:
        return
    for ch in chunks:
        text = annotate_chunk(
            ch.get("content") or "", ch.get("paragraph_sigs"),
            ch.get("paragraph_offsets"), forms)
        ch["content"] = text
