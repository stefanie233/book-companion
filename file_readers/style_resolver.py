"""有效排版解析 —— 把 CSS 算成「这一段看起来什么样」。

为什么需要这一层
----------------
``probe_css_rules`` 给出的是 ``{font-size: 0.875em}`` 这样的**声明**。要让 agent
用它，它得在脑子里做层叠、继承、em 链三层符号计算 —— 这是 LLM 最不擅长的
事，却恰好是纯函数最擅长的事。本模块把这三层算完，输出**有效排版**：相对
正文基准的字号倍数、粗斜体、对齐、缩进。

窄门的叙述正文 ``p.calibre14``（1em）与信件 ``p.calibre19``（0.875em）段均长
都是 70-73 字，统计上无法区分 —— 但有效排版一眼可辨。有了它，agent 不需要
被告知「calibre19 是信」，它读一段样张就知道。

设计约定
--------
* **只用 stdlib**（``html.parser``）。仓库目前全是正则、无 DOM，而继承解析
  必须有树。本模块是仓库第一个结构化解析器，对 Calibre 的畸形 HTML 容错。
* 视觉类的键沿用 ``tag.class`` 选择器（与 ``book_text_forms`` / ``probe_tag_themes``
  同口径），但**另外**给出等价组 —— 视觉上等价的选择器归到同一组，合并与否
  不再需要 agent 自己对照 CSS 判断。
* 纯函数。除 :func:`parse_stylesheets` 读 zip 里的 css 外，不碰任何 I/O。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

# 根字号兜底。EPUB 极少显式声明 html 字号，浏览器默认即 16px。
_BASE_PX = 16.0

# 字号关键字 → 相对父级的倍数（CSS 规范 medium=16px 为基准）。
_NAMED_SIZES: dict[str, float] = {
    "xx-small": 0.58, "x-small": 0.69, "small": 0.83, "medium": 1.0,
    "large": 1.2, "x-large": 1.5, "xx-large": 2.0,
    "smaller": 0.83, "larger": 1.2,
}

# 承载文本的块级元素 —— 只有这些会成为「块」，进样张、进视觉类。
BLOCK_TAGS = frozenset({
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "blockquote", "td", "th", "figcaption", "caption", "dt", "dd", "pre",
})

# 容器链里不记这些 —— 每个块都在它们里面，记了只是噪音。
_BOILERPLATE = frozenset({"#document", "html", "body"})

# 整棵跳过的子树 —— 这些不是书的内容。
# ``head`` 必须跳过：``<title>未知</title>`` 这类占位串会被当正文块收进来
# （设计稿 7.2 已为同样的理由在 probe_spine_list 里剥过一次 head）。
_SKIP_SUBTREES = frozenset({"head", "script", "style", "title", "meta", "link"})

# 自闭合标签：不压栈（压了会把后续内容全吞进去）。
_VOID_TAGS = frozenset({
    "br", "hr", "img", "meta", "link", "input", "area", "base",
    "col", "embed", "source", "track", "wbr",
})

# ── 有效排版 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EffectiveStyle:
    """一个块**最终**呈现出来的排版，已算完层叠 + 继承 + em 链。

    ``size_ratio`` 是相对**正文基准**（``<body>`` 的实际字号）的倍数 —— 1.0 就是
    正文大小。用倍数而非 px，是因为 px 取决于根字号这个任意起点，倍数才是
    「比正文大还是小」这个真正有意义的量。
    """

    size_ratio: float = 1.0
    bold: bool = False
    italic: bool = False
    align: str = "left"          # left | center | right | justify
    indent_em: float = 0.0       # text-indent 折算成当前字号下的 em
    margin_top_em: float = 0.0
    page_break_before: bool = False
    # 归一化后的首个具体字体族（``kaiti`` / ``songti`` / …），没指定则空串。
    # **不进 sig()** —— 理由见 normalize_font_family。
    font_family: str = ""

    def sig(self) -> str:
        """稳定短签名 —— 视觉等价的选择器会得到同一个签名。

        字号量化到 0.05 一档：``0.875`` 与 ``0.87`` 应视作同一种排版，而
        ``0.875`` 与 ``1.0`` 必须分开（那正是信件与正文的区别）。
        """
        q = round(self.size_ratio / 0.05) * 0.05
        return "|".join((
            f"{q:.2f}",
            "b" if self.bold else "-",
            "i" if self.italic else "-",
            self.align,
            "pb" if self.page_break_before else "--",
        ))

    def describe(self, base: float | None) -> str:
        """给 agent 看的一句话排版描述（样张图例用）。

        *base* 是「**本书**正文」的字号倍数，由 :func:`body_base` 从视觉类里
        算出来 —— **不要**拿 ``<body>`` 当基准。多数书的 ``<body>`` 就是正文
        （ratio 1.0），但有些书的正文类自带 ``font-size: 1.33em``（人类简史
        实测：``p.calibre6`` 占全书 95% 的字），按 ``<body>`` 描述会把正文说成
        「比正文大 33%」—— 等于递一句错话给 agent，而这正是本层要消灭的东西。

        *base* **必填**（曾默认 1.0）：默认值让「忘了传」和「基准真的是 1.0」
        长得一样，于是静默产出错描述 —— ``zoom`` 那个 bug 就是这么来的。
        传 ``None`` = 明确表示「基准未知」，此时**整段不提字号** —— 宁可不说，
        也不说一句错话。
        """
        parts: list[str] = []
        if base:
            r = self.size_ratio / base
            # 阈值只用来决定**说不说「基准」**，显示的数字仍是真值 —— 量化成
            # 0.05 一档会把窄门信件的 0.875 说成「小 10%」（真值 12.5%）
            if abs(r - 1.0) < 0.05:
                parts.append("正文基准字号")
            else:
                pct = (r - 1.0) * 100
                parts.append(f"比正文{'大' if pct > 0 else '小'} {abs(pct):.0f}%")
        if self.font_family:
            parts.append(f"{self.font_family} 体")
        if self.bold:
            parts.append("加粗")
        if self.italic:
            parts.append("斜体")
        if self.align != "left":
            parts.append({"center": "居中", "right": "右对齐",
                          "justify": "两端对齐"}.get(self.align, self.align))
        if self.indent_em:
            parts.append(f"首行缩进 {self.indent_em:g}em")
        if self.page_break_before:
            parts.append("另起一页")
        return "、".join(parts)


@dataclass
class Block:
    """一个承载文本的块 —— 样张的最小单位，也是 agent 引用证据的坐标。

    ``block_index`` 是该文件内**非空块**的序号。它和 ``probe_tag_themes`` 的
    ``paragraph_index`` **不是同一把尺子**（那个只数 ``<p>``，本模块还数 h1-h6 /
    li / blockquote / 裸文本）。新工具集一律用 ``block_index``，别再混用 ——
    设计稿 7.8 的教训就是两个 index 混用导致误伤。
    """

    spine_index: int
    block_index: int
    tag: str
    selector: str            # tag.class —— 与 book_text_forms 同口径
    text: str
    style: EffectiveStyle
    classes: frozenset[str] = frozenset()   # 全部 class；selector 只取第一个
    # 从 body 到本块父级的**容器链**（形如 ``("blockquote.calibre18",)``）。
    #
    # 容器指「自身有块级后代、因而不作为块发出」的元素。不给这一层，样张里就
    # 看不见嵌套 —— 实测证伪趟必须 zoom 回原文才发现窄门的引文边界其实是
    # ``<blockquote class="calibre18">``，而分类器按字号分的层在边界上是错的。
    containers: tuple[str, ...] = ()


@dataclass
class VisualClass:
    """一个选择器在全书里的统计 + 样张。"""

    selector: str
    style: EffectiveStyle
    paragraphs: int = 0
    chars: int = 0
    spine_span: list[int] = field(default_factory=list)   # 出现过的 spine，升序
    samples: list[dict] = field(default_factory=list)     # [{spine, block, text}]
    equiv_group: int = -1    # 视觉等价组号；同号 = 排版完全相同

    @property
    def avg_len(self) -> int:
        return self.chars // self.paragraphs if self.paragraphs else 0


# ── CSS：解析与匹配 ───────────────────────────────────────────────────────


def parse_stylesheets(reader) -> dict[str, dict[str, str]]:
    """读出 EPUB 里所有样式表，返回 ``{选择器: {属性: 值}}``。

    直接复用 :func:`file_readers.epub_probe.probe_css_rules` —— 它已经把
    ``@import``/注释/多表覆盖处理过了，这里只取 ``rules`` 那部分。
    """
    from file_readers.epub_probe import probe_css_rules
    return probe_css_rules(reader).get("rules", {})


# 组合器（空格 / > / + / ~）—— 只取最右一段做匹配。
_COMBINATOR_RE = re.compile(r"[\s>+~]+")
# 伪类 / 伪元素 —— 对静态排版无影响，剥掉。
_PSEUDO_RE = re.compile(r"::?[a-zA-Z-]+(\([^)]*\))?")


def _parse_compound(sel: str) -> tuple[str, frozenset[str], str]:
    """右端复合选择器 → ``(tag, classes, id)``。空串表示该维度不限定。"""
    sel = _PSEUDO_RE.sub("", sel)
    id_m = re.search(r"#([\w-]+)", sel)
    tag_m = re.match(r"^([a-zA-Z][\w-]*)", sel)
    return (
        tag_m.group(1).lower() if tag_m else "",
        frozenset(re.findall(r"\.([\w-]+)", sel)),
        id_m.group(1) if id_m else "",
    )


def _specificity(sel: str) -> tuple[int, int, int]:
    """(id 数, class 数, tag 数) —— 标准 CSS 权重三元组。"""
    tag, classes, eid = _parse_compound(_COMBINATOR_RE.split(sel.strip())[-1])
    return (1 if eid else 0, len(classes), 1 if tag else 0)


def _build_rules(rules: dict[str, dict[str, str]]) -> list[tuple]:
    """把 ``{选择器: 属性}`` 展开成按优先级排好序的匹配表。

    后代/子代组合器只取**最右一段**做匹配（``.a p`` 按 ``p`` 匹配）—— 这是
    简化近似：会多匹配一些元素，但 EPUB 里 Calibre 生成的规则绝大多数是
    单段选择器，多匹配的代价是排版归类略粗，不会把文本搞丢。
    """
    out: list[tuple] = []
    for order, (sel, props) in enumerate(rules.items()):
        if not props:
            continue
        tag, classes, eid = _parse_compound(_COMBINATOR_RE.split(sel.strip())[-1])
        out.append((_specificity(sel), order, tag, classes, eid, props))
    # 权重升序 → 同权重按源码顺序 → 后者覆盖前者（CSS 层叠规则）
    out.sort(key=lambda r: (r[0], r[1]))
    return out


# ── DOM：容错建树 ─────────────────────────────────────────────────────────


class Node:
    """极简元素节点。文本以 ``str`` 直接混在 ``children`` 里。"""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict | None = None,
                 parent: Node | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent

    @property
    def classes(self) -> frozenset[str]:
        return frozenset((self.attrs.get("class") or "").split())

    def text(self) -> str:
        """该节点下全部后代文本；``<br>`` 折算成换行（题词两行靠它保住）。"""
        buf: list[str] = []

        def walk(n: Node) -> None:
            for c in n.children:
                if isinstance(c, str):
                    buf.append(c)
                elif c.tag == "br":
                    buf.append("\n")
                else:
                    walk(c)

        walk(self)
        return "".join(buf)

    def has_block_descendant(self) -> bool:
        """是否含块级后代 —— 用来挑「叶子块」，避免 div 包 p 时重复计一遍。"""
        for c in self.children:
            if isinstance(c, Node):
                if c.tag in BLOCK_TAGS or c.has_block_descendant():
                    return True
        return False


class _TreeBuilder(HTMLParser):
    """把 XHTML 建成 ``Node`` 树，对 Calibre 的畸形标签容错。

    容错策略：闭合标签找不到对应开标签时**忽略**（浏览器行为）；找到时把它
    之上的未闭合节点一并弹掉。Calibre 输出里 ``<p>`` 嵌 ``<p>`` 之类的畸形
    很常见，严格解析会直接抛异常。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs) -> None:
        node = Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        node = Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return
        # 没有对应开标签 —— 忽略，不抛

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].children.append(data)


def build_dom(html: str) -> Node:
    """HTML 字符串 → 节点树。解析失败也返回已建好的部分，不抛。"""
    b = _TreeBuilder()
    try:
        b.feed(html)
        b.close()
    except Exception:
        pass
    return b.root


# ── 长度解析与层叠 ────────────────────────────────────────────────────────

_LEN_RE = re.compile(r"^\s*(-?[\d.]+)\s*(px|em|rem|%|pt|ex|ch)?\s*$", re.IGNORECASE)

# font-weight 里算「粗」的值。
_BOLD_WEIGHTS = frozenset({"bold", "bolder", "600", "700", "800", "900"})


def _resolve_font_px(value: str, parent_px: float, root_px: float) -> float | None:
    """一个 font-size 声明 → 绝对 px。无法解析返回 None（调用方保持原值）。

    ``em`` 相对**父级**字号、``rem`` 相对**根**字号 —— 这是 em 链的关键，
    也是 agent 在脑子里最容易算错的地方。
    """
    v = (value or "").strip().lower()
    if not v:
        return None
    if v in _NAMED_SIZES:
        return parent_px * _NAMED_SIZES[v]
    m = _LEN_RE.match(v)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "px").lower()
    if unit == "px":
        return num
    if unit == "em":
        return num * parent_px
    if unit == "rem":
        return num * root_px
    if unit == "%":
        return num / 100.0 * parent_px
    if unit == "pt":
        return num * 96.0 / 72.0
    if unit in ("ex", "ch"):
        return num * parent_px * 0.5      # 近似：1ex ≈ 0.5em
    return None


def parse_inline_style(attr: str) -> dict[str, str]:
    """``style="font-size:0.9em;color:red"`` → ``{"font-size": "0.9em", ...}``。"""
    out: dict[str, str] = {}
    for decl in (attr or "").split(";"):
        key, sep, val = decl.partition(":")
        if sep and key.strip():
            out[key.strip().lower()] = val.strip()
    return out


# ── 遍历：算出每个块的有效排版 ────────────────────────────────────────────


@dataclass
class _Ctx:
    """继承下来的排版上下文。CSS 里 font-size/weight/style/align/family 都是继承属性。"""

    font_px: float = _BASE_PX
    bold: bool = False
    italic: bool = False
    align: str = "left"
    font_family: str = ""


# 通用族 —— 首项是它们时说明不了任何事（``serif`` 与不声明在视觉上等价），
# 不能当作「用了别的字体」的证据。
_GENERIC_FAMILIES = frozenset({
    "serif", "sans-serif", "monospace", "cursive", "fantasy",
    "system-ui", "ui-serif", "ui-sans-serif", "ui-monospace", "inherit",
})

# CJK 字体别名 → 拉丁名。全库 429 条 font-family 声明里，同一个字体常以
# 「楷体」/「KaiTi」/「STKaiti」/「kaiti」四种写法出现，不归一会把等价组打碎。
_FAMILY_ALIASES: dict[str, str] = {
    "楷体": "kaiti", "楷体_gb2312": "kaiti", "华文楷体": "kaiti", "方正楷体": "kaiti",
    "stkaiti": "kaiti", "stkai": "kaiti", "mkai prc": "kaiti", "kai": "kaiti",
    "dk-kaiti": "kaiti",
    "宋体": "songti", "方正书宋": "songti", "华文宋体": "songti",
    "stsong": "songti", "song": "songti", "dk-xiaobiaosong": "songti",
    "方正小标宋简体": "songti",
    "黑体": "heiti", "simhei": "heiti", "方正兰亭黑简体": "heiti",
    "dk-heiti": "heiti", "mying hei s": "heiti", "hei": "heiti",
    "仿宋": "fangsong", "fang song": "fangsong",
}


def normalize_font_family(value: str) -> str:
    """``font-family`` 声明 → 归一化的**首个具体字体族**；取不到返回空串。

    全库实测取值形态很杂：裸词 ``kaiti``、带引号 ``"kaiti"``、逗号 fallback
    列表且中英混排（``stkai, "mkai prc", kai,"楷体"``）。归一四步：

    1. 按逗号拆 fallback，取**第一个** —— 后面的是它缺席时的替补
    2. 去引号、去空白、小写
    3. 查别名表（``楷体``/``KaiTi``/``STKaiti`` → ``kaiti``）
    4. 首个是通用族（``serif`` 等）就返回空 —— 它等于「没指定」

    **注意它不进** :meth:`EffectiveStyle.sig`：font-family 是继承属性，一个
    ``<div style="font-family:…">`` 会把家族盖到所有后代段（造成**合并**而非
    拆分）；而 ``font-family:serif`` 与不声明视觉相同却会被误拆。所以它只用于
    :meth:`EffectiveStyle.describe` 与等价组的**差异标注**。
    """
    for part in (value or "").split(","):
        s = part.strip().strip("'\"").strip().lower()
        if not s:
            continue
        if s in _GENERIC_FAMILIES:
            return ""                     # 通用族首项 = 没指定具体字体
        s = _FAMILY_ALIASES.get(s, s)
        # 「Songti SC」「Kaiti TC」是同一字体的地区变体写法，去掉后缀再查一次
        if s.endswith((" sc", " tc")):
            s = s[:-3].strip()
            s = _FAMILY_ALIASES.get(s, s)
        return s
    return ""


def _own_text(node: Node) -> str:
    buf: list[str] = []
    for c in node.children:
        if isinstance(c, str):
            buf.append(c)
        elif c.tag == "br":
            buf.append("\n")
    return "".join(buf)


def _norm(s: str) -> str:
    """折空白但**保住换行** —— 题词两行塌成一行就再也看不出它是题词了。

    ``\\r`` 先归一成 ``\\n``：Calibre 的正文里混着裸 ``\\r``（实测大棋局 4 章
    正文带 ``\\r`` 残字），不处理会当普通字符留在块文本里。
    """
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def _selector(node: Node) -> str:
    """``tag.class`` —— 与 ``epub_probe._p_selector`` / ``book_text_forms`` 同口径。

    只取**第一个** class：Calibre 会给同一个元素挂多个类，全带上会让同一个
    视觉形态裂成多个键，反而看不出规模。
    """
    cls = (node.attrs.get("class") or "").split()
    return f"{node.tag}.{cls[0]}" if cls else node.tag


# ── 选择器归一化 ──────────────────────────────────────────────────────────
# 三个生产者写出来的选择器长得不一样，要互相查表就得先归一。仓库里有两种写法：
#
#   _selector(node)                      →  p.calibre19   （点）
#   epub_reader._format_html_styled 的签名 →  p|calibre19   （竖线）
#
# 多 class 时两者还不一致：前者只取第一个（p.foo），后者取整个 class 属性
# （p|foo bar）。归一成 `tag.第一个class` —— 与 _selector 同口径。


def selector_key(sel: str) -> str:
    """选择器 → 查表键 ``tag.第一个class``；**没有 class 就只有 tag**。

    三种写法都要归一：

    * ``p.calibre19`` —— :func:`_selector` 的形态
    * ``p|calibre19`` / ``p|foo bar`` —— ``_format_html_styled`` 的段落签名
    * ``p|inline|<行内样式>`` —— 无 class、只有行内样式的段落（实测非暴力沟通
      757 段）。``inline`` 是**标记**不是类名，这一段没有 class，键就是 ``p``
    * ``p||`` —— 空 class 属性，同上

    把 ``inline`` 当成类名会得到 ``p.inline|`` 这种永远命中不了的键（实测踩到）。
    """
    s = (sel or "").strip()
    if not s:
        return ""
    if "|" in s:
        tag, _, rest = s.partition("|")
        rest = rest.split("|")[0].strip()      # ``p|inline|样式`` → 只取 ``inline``
        if not rest or rest == "inline":
            return tag.strip()
        return f"{tag.strip()}.{rest.split()[0]}"
    if "." in s:
        tag, _, rest = s.partition(".")
        parts = rest.split()
        return f"{tag}.{parts[0]}" if parts else tag
    return s


def split_selectors(sel: str) -> list[str]:
    """拆开分类器写的多选择器串 —— ``"h5.a,h5.b"`` → ``["h5.a", "h5.b"]``。

    分类器会把排版相同的多个选择器合并成**一条** text_form。实测两种分隔符都
    出现过：逗号（人类简史 ``h5.sigilnotintoc,h5.sigilnotintoc1``）与斜杠
    （灵魂永生 ``h2.xxx / h3.title2 / div.middle``）。而 ``set_text_form`` 拿
    selector 当主键、``_render_text_forms`` 整串打印 —— 不拆，这条形态永远
    命中不了任何段落，等于白判。

    两种分隔符在 CSS 选择器里都不会出现，可以放心拆。
    """
    return [p.strip() for p in re.split(r"[,/]", sel or "") if p.strip()]


def _em_of(value: str | None, font_px: float) -> float:
    """长度声明 → 折算成当前字号下的 em（缩进/边距的统一量纲）。"""
    if not value:
        return 0.0
    px = _resolve_font_px(value, font_px, font_px)
    if not px:
        return 0.0
    return round(px / font_px, 2) if font_px else 0.0


def _is_page_break(props: dict[str, str]) -> bool:
    v = (props.get("page-break-before") or props.get("break-before") or "").strip().lower()
    return v in ("always", "page", "left", "right")


def resolve_blocks(root: Node, rules: dict[str, dict[str, str]],
                   spine_index: int = 0) -> list[Block]:
    """遍历 DOM，产出每个文本块的**有效排版**。

    ``block_index`` 是本文件内非空块的序号（含 h1-h6 / li / 裸文本），与
    ``probe_tag_themes`` 的 ``paragraph_index`` **不是同一把尺子** —— 那个只数
    ``<p>``。新工具集一律用 ``block_index``，别再混用。
    """
    table = _build_rules(rules)
    blocks: list[Block] = []
    body_px = _BASE_PX
    idx = 0

    def decls(node: Node) -> dict[str, str]:
        """命中该节点的全部声明，按权重叠加；行内 style 最后压上（最高优先级）。"""
        props: dict[str, str] = {}
        for _spec, _order, tag, classes, eid, p in table:
            if (not tag or tag == node.tag) and classes <= node.classes \
                    and (not eid or eid == node.attrs.get("id")):
                props.update(p)
        props.update(parse_inline_style(node.attrs.get("style", "")))
        return props

    def emit(node: Node, text: str, style: EffectiveStyle,
             containers: tuple[str, ...]) -> None:
        nonlocal idx
        blocks.append(Block(spine_index, idx, node.tag, _selector(node), text,
                            style, node.classes, containers))
        idx += 1

    def walk(node: Node, ctx: _Ctx, containers: tuple[str, ...] = ()) -> None:
        nonlocal body_px
        if node.tag in _SKIP_SUBTREES:
            return
        # —— 本节点自身的声明 → 新的排版上下文 ——
        props = decls(node)
        px = _resolve_font_px(props.get("font-size", ""), ctx.font_px, _BASE_PX)
        font_px = px if (px and px > 0) else ctx.font_px
        fw = (props.get("font-weight") or "").strip().lower()
        bold = fw in _BOLD_WEIGHTS if fw else ctx.bold
        fst = (props.get("font-style") or "").strip().lower()
        italic = fst in ("italic", "oblique") if fst else ctx.italic
        ta = (props.get("text-align") or "").strip().lower()
        align = ta if ta in ("left", "center", "right", "justify") else ctx.align
        ff = normalize_font_family(props.get("font-family") or "") or ctx.font_family
        # HTML 表现型默认：这些标签不靠 CSS 也自带样式，漏了会把 <b> 读成普通文本
        if node.tag in ("b", "strong"):
            bold = True
        if node.tag in ("i", "em", "cite"):
            italic = True
        if node.tag == "center":
            align = "center"
        if node.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            bold = True
        if node.tag == "body":
            body_px = font_px

        eff = EffectiveStyle(
            size_ratio=(font_px / body_px) if body_px else 1.0,
            bold=bold,
            italic=italic,
            align=align,
            indent_em=_em_of(props.get("text-indent"), font_px),
            margin_top_em=_em_of(props.get("margin-top"), font_px),
            page_break_before=_is_page_break(props),
            font_family=ff,
        )

        # —— 块判定 ——
        # 子树里没有块级元素的节点 = 一个文本单元：整棵收下、**不再下钻**，
        # 否则 <p> 里嵌 <span> 会被计两遍。
        if not node.has_block_descendant():
            text = _norm(node.text())
            if text:
                emit(node, text, eff, containers)
            return

        # 有块级后代的容器：只收自己的**直接**文本，其余交给递归。直接文本得
        # 单独收，是因为它没有块级外壳；但只有 str 子节点算「直接」——
        # <div><a>散字</a><p>段</p></div> 里的散字在 <a> 里，要靠递归到 <a>
        # 才收得到（旧版只认 BLOCK_TAGS，这类散字整片消失：面纱的目录页
        # 因此丢了 1806 字）。
        direct = _norm(_own_text(node))
        if direct:
            emit(node, direct, eff, containers)

        # 本节点是容器（有块级后代却不作为块发出）→ 记进后代块的容器链，
        # 让「这一段在 blockquote 里」这件事在样张上直接可见。
        # #document/html/body 是每块都有的样板，记进去只是噪音。
        inner = (containers if node.tag in _BOILERPLATE
                 else containers + (_selector(node),))
        child_ctx = _Ctx(font_px, bold, italic, align, ff)
        for c in node.children:
            if isinstance(c, Node):
                walk(c, child_ctx, inner)

    walk(root, _Ctx())
    return blocks


def _cached_rules(reader) -> dict[str, dict[str, str]]:
    """样式表解析结果缓存在 reader 上 —— 一本书所有 spine 共用同一份 CSS。"""
    rules = getattr(reader, "_style_rules_cache", None)
    if rules is None:
        rules = parse_stylesheets(reader)
        try:
            reader._style_rules_cache = rules
        except Exception:
            pass
    return rules


def analyze_spine(reader, spine_index: int,
                  rules: dict[str, dict[str, str]] | None = None) -> list[Block]:
    """读一个 spine 文件 → 块列表（每块带算好的有效排版）。"""
    html = reader._read_spine_html(spine_index)
    return resolve_blocks(build_dom(html), rules if rules is not None
                          else _cached_rules(reader), spine_index)


# ── 视觉类聚合 ────────────────────────────────────────────────────────────


def aggregate_visual_classes(blocks: list[Block],
                             samples_per_class: int = 3) -> list[VisualClass]:
    """把块按选择器聚成视觉类，并标出**视觉等价组**。

    等价组是这一层的核心增量。窄门的 ``.calibre17`` 与 ``.c1`` 规则几乎相同
    却是两个选择器 —— 设计稿把「该不该合并」留给了 agent 自己对照 CSS 判断，
    现在机械算好：同组即排版完全相同。
    """
    by_sel: dict[str, VisualClass] = {}
    for b in blocks:
        vc = by_sel.get(b.selector)
        if vc is None:
            vc = VisualClass(selector=b.selector, style=b.style)
            by_sel[vc.selector] = vc
        vc.paragraphs += 1
        vc.chars += len(b.text)
        if b.spine_index not in vc.spine_span:
            vc.spine_span.append(b.spine_index)
        if len(vc.samples) < samples_per_class:
            vc.samples.append({
                "spine": b.spine_index,
                "block": b.block_index,
                "text": b.text[:200],
            })

    groups: dict[str, list[VisualClass]] = {}
    for vc in by_sel.values():
        groups.setdefault(vc.style.sig(), []).append(vc)
    # 组号按规模降序 —— 大组在前，agent 一眼看到的是主要形态
    for gi, group in enumerate(
            sorted(groups.values(), key=lambda g: -sum(v.paragraphs for v in g))):
        for vc in group:
            vc.equiv_group = gi

    for vc in by_sel.values():
        vc.spine_span.sort()
    return sorted(by_sel.values(), key=lambda v: -v.paragraphs)


def body_base(classes: list[VisualClass]) -> float:
    """本书「正文」的字号倍数 —— 取**占字最多**的那一类。

    这是「正文」在排版上的操作性定义：一本书里字数最多的那一类就是正文。
    比 ``<body>`` 可靠（见 :meth:`EffectiveStyle.describe`），也不需要 agent
    自己挑基准。
    """
    if not classes:
        return 1.0
    return max(classes, key=lambda c: c.chars).style.size_ratio


# 正文基准的**模块级**缓存，键是 ``(路径, mtime)``。
#
# 为什么不能挂在 reader 实例上：工具层（``tools._reader``）每次调用都新开一个
# reader，实例属性随上一个 reader 一起销毁 —— 于是 ``zoom`` 拿不到基准、退回
# ``<body>``，把正文说成「比正文大 33%」，与 ``book_overview`` 自相矛盾。
# 2026-10-08 人类简史的证伪趟正是抓到这个（同一类，两个工具两种说法）。
#
# 也不能改成跨调用共享 reader —— 文件句柄会整个 agent 会话不关。按
# ``(路径, mtime)`` 走模块级字典：同一进程内免费复用，源文件换了自动失效。
_BODY_BASE_CACHE: dict[tuple[str, float], float] = {}


def _reader_key(reader) -> tuple[str, float] | None:
    try:
        p = Path(str(getattr(reader, "path", "") or ""))
        return (str(p), p.stat().st_mtime)
    except Exception:
        return None


def cached_body_base(reader) -> float | None:
    """本书正文的字号倍数；**没算过返回 None**（不猜、不退回 1.0）。

    返回 None 而不是 1.0 是刻意的：1.0 会让 :meth:`describe` 把正文说成
    「比正文大 33%」，那是**错话**，比不说更糟。调用方要么先跑
    :func:`survey_spines`，要么显式处理未知。
    """
    key = _reader_key(reader)
    return _BODY_BASE_CACHE.get(key) if key else None


def equiv_groups(classes: list[VisualClass]) -> list[dict]:
    """把等价组整理成「一组 = 多个选择器 + 同一排版」的清单。

    分组按字号/粗细/斜体/对齐（``sig()``）—— **不含字体族**。但组内字体族不同
    时会补一条 ``font_note``：字号一致、字体不同是中文书的常规区分（人类简史
    ``p.calibre6`` 宋体 vs ``p.calibre17`` 楷体），agent 需要看见它，却不该因此
    把组拆开（见 :func:`normalize_font_family`）。
    """
    by_gid: dict[int, list[VisualClass]] = {}
    for vc in classes:
        by_gid.setdefault(vc.equiv_group, []).append(vc)
    base = body_base(classes)
    out: list[dict] = []
    for gid in sorted(by_gid):
        members = by_gid[gid]
        item = {
            "group": gid,
            "selectors": [m.selector for m in members],
            "paragraphs": sum(m.paragraphs for m in members),
            "chars": sum(m.chars for m in members),
            "style": members[0].style.describe(base),
        }
        fams = {m.style.font_family for m in members}
        if len(fams) > 1:
            item["font_note"] = (
                "组内字号排版相同但**字体不同**："
                + "、".join(f"{m.selector}={m.style.font_family or '(未指定)'}"
                            for m in members if m.style.font_family)
                + " —— 中文书里这常是引文/正文的分界，不要当成同一形态。")
        out.append(item)
    return out


def survey_spines(reader, spine_indices, samples_per_class: int = 3):
    """多个 spine 一起分析 → ``(blocks, classes, groups)``。

    读不动的文件跳过而不是抛 —— 导入期总会有坏 spine，不该让整本书分析失败。
    """
    rules = _cached_rules(reader)
    blocks: list[Block] = []
    for si in spine_indices:
        try:
            blocks.extend(analyze_spine(reader, si, rules))
        except Exception:
            continue
    classes = aggregate_visual_classes(blocks, samples_per_class)
    # 正文基准存进模块级缓存 —— 单块视图（zoom）拿不到视觉类清单，靠它才能用
    # 同一把尺子描述字号。见 _BODY_BASE_CACHE 上的注释。
    key = _reader_key(reader)
    if key is not None:
        _BODY_BASE_CACHE[key] = body_base(classes)
    return blocks, classes, equiv_groups(classes)
