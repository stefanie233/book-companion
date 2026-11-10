"""作品工作区 —— 作品记忆 / 设定 / 计划稿 / 素材，落在 ``data/writing/works/<work_id>/``。

照协作文档目录的组织方式，只是把「代码项目」换成「一本书」：

    作品记忆.md   这本书是什么、写作约定与忌讳。每次写作全文带上（低频层）。
    设定/         设定：人物 / 世界观 / 事件 / 地点……只带索引，读全文走工具。
    计划稿/       备选与设计：结局备选、章节设计。不进上下文，作者要看才读。
    素材/         书外收集的原料。不进上下文，作者与 agent 一起找合并项。

目录名是给作者看的，不用英文技术词；``.md`` 只是文件的真实后缀，页面上一律
显示去掉后缀的名字（作者不需要知道文件格式）。

为什么是**普通文件**而不是库表：作者可以用任何编辑器打开、备份、丢进他自己
的 git。写作 git 会把这三块一起提交——设定和正文是同一本书的两个部分，回滚
到某个版本时设定也该跟着回到那时（决策 127）。

路径全部走虚拟 POSIX 形式（``设定/人物/人物A.md``），写入前逐段校验；只允许
``作品记忆.md`` 与 ``设定/``、``计划稿/``、``素材/`` 下的 ``.md``，解析后的
真实路径必须仍在作品目录里（挡 ``..`` 与软链逃逸）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Iterable

#: 作品记忆的文件名。固定：它被无条件注入，改名等于换一套语义。
BIBLE_NAME = "作品记忆.md"

#: 三个分区。名字面向作者，不用 docs/plans 这类英文技术词。
DOCS_DIR = "设定"
PLANS_DIR = "计划稿"
MATERIALS_DIR = "素材"

#: 世界层的固定文件名与起步 facet（决策 142）。不够可以加。
WORLD_NAME = "世界.md"
WORLD_FACETS = ("时代与地理", "社会结构", "经济运转",
                "文化与日常", "时代情绪", "故事窄口")

#: 工作区里允许出现的顶层名字。别的一律拒绝——这里不是通用文件管理器。
ALLOWED_ROOTS = (BIBLE_NAME, DOCS_DIR, PLANS_DIR, MATERIALS_DIR)

#: 三个分区根。它们本身不能改名/删除，只作为新文件与新文件夹的容器。
SECTION_ROOTS = (DOCS_DIR, PLANS_DIR, MATERIALS_DIR)

#: 旧版目录名 → 新名字。打开工作区时顺手迁移，作者不用手动改名。
LEGACY_ROOTS = {"docs": DOCS_DIR, "plans": PLANS_DIR,
                "试错稿": PLANS_DIR}

#: 只收 Markdown。纯文本会被当成二进制/杂项，干脆不支持。
SUFFIX = ".md"

#: 单文件上限。设定文档不该长到这份上；超了说明该拆文件。
MAX_FILE_BYTES = 256 * 1024

#: 目录最深四层：设定/主要人物/人物A/年表.md 已经够深了。
MAX_DEPTH = 5

#: 一段路径里最长的名字（字符）。
MAX_SEGMENT = 60

_WORK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


class DocError(RuntimeError):
    """可展示给作者的工作区错误（路径非法、文件太大、找不到）。"""


def workspace_root(project_dir: Path | str, work_id: str) -> Path:
    """作品目录：``data/writing/works/<work_id>``。work_id 是库内 id，先验形。"""
    if not _WORK_ID_RE.match(str(work_id or "")):
        raise DocError("作品 id 非法")
    return Path(project_dir) / "data" / "writing" / "works" / str(work_id)


# ── 路径 ──────────────────────────────────────────────────────────────────

def _segments(path: str) -> list[str]:
    """逐段校验，返回归一后的段。任何不确定的输入都拒绝，不纠正。"""
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        raise DocError("路径不能为空")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise DocError("路径必须是相对路径")
    parts: list[str] = []
    for piece in raw.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            raise DocError("路径不能包含 ..")
        if len(piece) > MAX_SEGMENT:
            raise DocError(f"名字太长：{piece[:12]}…")
        if _BAD_CHARS.search(piece) or piece.startswith("."):
            raise DocError(f"名字里有不允许的字符：{piece}")
        parts.append(piece)
    if not parts:
        raise DocError("路径不能为空")
    if len(parts) > MAX_DEPTH:
        raise DocError("目录太深")
    return parts


def normalize(path: str) -> str:
    """校验并归一成 ``a/b.md``。任何不确定的输入都拒绝，不纠正。"""
    parts = _segments(path)
    head = parts[0]
    if head not in ALLOWED_ROOTS:
        raise DocError("只能写在 作品记忆.md、设定/、计划稿/、素材/ 里")
    if head == BIBLE_NAME:
        if len(parts) > 1:
            raise DocError("作品记忆只能是根目录的 作品记忆.md")
        return BIBLE_NAME
    if len(parts) == 1:
        raise DocError("这是目录，不是文件")
    if not parts[-1].lower().endswith(SUFFIX):
        raise DocError("只收 .md 文件")
    return "/".join(parts)


def resolve(root: Path, path: str) -> Path:
    """归一后的路径 → 真实路径，并确认没有逃出作品目录。"""
    return _resolve_rel(root, normalize(path))


def _resolve_rel(root: Path, rel: str) -> Path:
    base = Path(root).resolve()
    target = (Path(root) / rel).resolve()
    if target != base and base not in target.parents:
        raise DocError("路径越界")
    return target


def normalize_dir(path: str) -> str:
    """目录路径：只能是 设定/、计划稿/、素材/ 下的子目录。

    三个分区根本身不允许改名/删除——它们是工作区的固定骨架。
    """
    rel = "/".join(_segments(path))
    parts = rel.split("/")
    if parts[0] not in SECTION_ROOTS:
        raise DocError("只能建在 设定/、计划稿/、素材/ 里")
    if len(parts) < 2:
        raise DocError("这是分区根目录，不能改名或删除")
    return rel


def resolve_dir(root: Path, path: str) -> Path:
    return _resolve_rel(root, normalize_dir(path))


# ── 初始化 ────────────────────────────────────────────────────────────────

BIBLE_TEMPLATE = """# 《{title}》作品记忆

> 写作时这份记忆会全文带上。只写长期有效、不随章节变化的事：这本书是什么、
> 写作约定与忌讳。人物、世界观等细节写进「设定」；还没定下来的备选写进
> 「计划稿」；书外收集的原料写进「素材」。
> 账本、进度与上一章结果由系统给，这里不要复述——只写它们没有的东西。

## 这本书是什么

## 写作约定

## 忌讳
"""

def migrate_legacy(root: Path) -> None:
    """把旧版 ``docs/``、``plans/`` 并进 ``设定/``、``计划稿/``。

    同名的文件不覆盖也不丢：留在旧目录里，等作者自己处理。旧目录空了就收掉。
    只在打开工作区时顺手做一次，幂等。
    """
    root = Path(root)
    for old, new in LEGACY_ROOTS.items():
        src, dst = root / old, root / new
        if not src.is_dir():
            continue
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for item in list(src.iterdir()):
                target = dst / item.name
                if target.exists():
                    continue
                item.rename(target)
            src.rmdir()
        except OSError:
            continue


def ensure_workspace(root: Path, title: str = "") -> Path:
    """建目录并补一份作品记忆骨架。已存在就原样保留。"""
    root = Path(root)
    migrate_legacy(root)
    (root / DOCS_DIR).mkdir(parents=True, exist_ok=True)
    (root / PLANS_DIR).mkdir(parents=True, exist_ok=True)
    (root / MATERIALS_DIR).mkdir(parents=True, exist_ok=True)
    bible = root / BIBLE_NAME
    if not bible.exists():
        text = BIBLE_TEMPLATE.format(title=title or "未命名")
        bible.write_text(text, encoding="utf-8")
    return root


# ── 读 / 写 ───────────────────────────────────────────────────────────────

def _entry(root: Path, path: str) -> dict[str, Any]:
    target = resolve(root, path)
    stat = target.stat()
    return {"path": normalize(path), "size": stat.st_size,
            "modified": int(stat.st_mtime * 1000), "kind": "file"}


def _iter_files(root: Path) -> Iterable[Path]:
    base = Path(root)
    for top in (DOCS_DIR, MATERIALS_DIR, PLANS_DIR):
        directory = base / top
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob(f"*{SUFFIX}")):
            if any(part.startswith(".") for part in path.relative_to(base).parts):
                continue
            yield path


def list_files(root: Path) -> list[dict[str, Any]]:
    """工作区里的全部文件（作品记忆在前，其余按路径排）。"""
    base = Path(root)
    out: list[dict[str, Any]] = []
    bible = base / BIBLE_NAME
    if bible.is_file():
        stat = bible.stat()
        out.append({"path": BIBLE_NAME, "size": stat.st_size,
                    "modified": int(stat.st_mtime * 1000), "kind": "file"})
    for path in _iter_files(base):
        stat = path.stat()
        out.append({"path": path.relative_to(base).as_posix(),
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime * 1000), "kind": "file"})
    return out


def list_dirs(root: Path) -> list[dict[str, Any]]:
    """三个固定目录 + 它们下面所有子目录（含刚建的空文件夹）。

    空目录不会出现在 ``list_files`` 里，但作者打开「草稿」应该看见
    「设定」「计划稿」「素材」三块，以及自己刚建的空文件夹。
    """
    base = Path(root)
    roots = [name for name in (DOCS_DIR, MATERIALS_DIR, PLANS_DIR)
             if (base / name).is_dir()]
    out = [{"path": name, "kind": "dir"} for name in roots]
    for name in roots:
        for path in sorted((base / name).rglob("*")):
            if not path.is_dir():
                continue
            rel = path.relative_to(base)
            if any(part.startswith(".") for part in rel.parts):
                continue
            out.append({"path": rel.as_posix(), "kind": "dir"})
    return out


def read_file(root: Path, path: str) -> dict[str, Any]:
    target = resolve(root, path)
    if not target.is_file():
        raise DocError("文件不存在")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise DocError(f"文件太大（{size} 字节），先拆成几份")
    entry = _entry(root, path)
    entry["content"] = target.read_text(encoding="utf-8", errors="replace")
    return entry


def write_file(root: Path, path: str, content: str, *,
               append: bool = False) -> dict[str, Any]:
    target = resolve(root, path)
    text = str(content if content is not None else "")
    data = text.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise DocError(f"内容太大（{len(data)} 字节），先拆成几份")
    target.parent.mkdir(parents=True, exist_ok=True)
    if append and target.is_file():
        old = target.read_text(encoding="utf-8", errors="replace")
        if old and not old.endswith("\n") and text:
            text = "\n" + text
        with target.open("a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        target.write_text(text, encoding="utf-8")
    return _entry(root, path)


def delete_file(root: Path, path: str) -> dict[str, Any]:
    rel = normalize(path)
    if rel == BIBLE_NAME:
        raise DocError("作品记忆不能删；要清空就写入空内容")
    target = resolve(root, rel)
    if not target.is_file():
        raise DocError("文件不存在")
    target.unlink()
    # 顺手收掉空目录，别留一堆空壳；三个分区根留着占位。
    _prune_empty(target.parent, root)
    return {"deleted": rel}


def _prune_empty(parent: Path, root: Path) -> None:
    base = Path(root).resolve()
    stops = {base / name for name in SECTION_ROOTS}
    while True:
        parent = parent.resolve()
        if parent == base or parent in stops or not parent.is_dir():
            return
        if any(parent.iterdir()):
            return
        parent.rmdir()
        parent = parent.parent


def _clean_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise DocError("名字不能为空")
    if "/" in text or "\\" in text:
        raise DocError("名字里不能有路径分隔符")
    if len(text) > MAX_SEGMENT:
        raise DocError(f"名字太长：{text[:12]}…")
    if _BAD_CHARS.search(text) or text.startswith("."):
        raise DocError(f"名字里有不允许的字符：{text}")
    return text


def create_file(root: Path, path: str, content: str = "") -> dict[str, Any]:
    """新建一份空的（或带初始内容的）工作区文件。已存在就拒绝。"""
    rel = normalize(path)
    target = resolve(root, rel)
    if target.exists():
        raise DocError("文件已存在")
    text = str(content if content is not None else "")
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise DocError("内容太大，先拆成几份")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return _entry(root, rel)


def create_dir(root: Path, path: str) -> dict[str, Any]:
    """新建子文件夹。三个分区根不能当新文件夹名（已存在）。"""
    rel = normalize_dir(path)
    target = resolve_dir(root, rel)
    if target.exists():
        raise DocError("文件夹已存在")
    target.mkdir(parents=True)
    return {"path": rel, "kind": "dir"}


def rename_entry(root: Path, path: str, new_name: str, *,
                 kind: str = "file") -> dict[str, Any]:
    """原地改名（同一父目录）。文件补 .md，文件夹不加后缀。"""
    if kind not in ("file", "dir"):
        raise DocError("条目类型只能是 file / dir")
    rel = normalize_dir(path) if kind == "dir" else normalize(path)
    target = resolve_dir(root, rel) if kind == "dir" else resolve(root, rel)
    if kind == "dir" and not target.is_dir():
        raise DocError("文件夹不存在")
    if kind == "file" and not target.is_file():
        raise DocError("文件不存在")
    name = _clean_name(new_name)
    parent = rel.rsplit("/", 1)[0]
    if kind == "file":
        if not name.lower().endswith(SUFFIX):
            name += SUFFIX
        new_rel = normalize(f"{parent}/{name}")
        new_target = resolve(root, new_rel)
    else:
        new_rel = normalize_dir(f"{parent}/{name}")
        new_target = resolve_dir(root, new_rel)
    if new_rel == rel:
        return {"path": rel, "kind": kind, "changed": False}
    if new_target.exists():
        raise DocError("这个名字已经存在")
    try:
        target.rename(new_target)
    except OSError as exc:
        raise DocError(f"改名失败：{exc}") from exc
    if kind == "file":
        _prune_empty(target.parent, root)
    return {"path": new_rel, "kind": kind, "changed": True}


def delete_entry(root: Path, path: str, *, kind: str = "file") -> dict[str, Any]:
    """删除文件或整个文件夹（文件夹连内容一起删）。"""
    if kind == "file":
        return {**delete_file(root, path), "kind": "file"}
    if kind != "dir":
        raise DocError("条目类型只能是 file / dir")
    rel = normalize_dir(path)
    target = resolve_dir(root, rel)
    if not target.is_dir():
        raise DocError("文件夹不存在")
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise DocError(f"删除失败：{exc}") from exc
    _prune_empty(target.parent, root)
    return {"deleted": rel, "kind": "dir"}


# ── 注入用文本 ────────────────────────────────────────────────────────────

def _friendly(path: str) -> str:
    """设定文件的作者可见名字：去掉根目录与 .md（``设定/人物/人物A.md`` → ``人物/人物A``）。"""
    rel = path[len(DOCS_DIR) + 1:] if path.startswith(DOCS_DIR + "/") else path
    return rel[:-len(SUFFIX)] if rel.lower().endswith(SUFFIX) else rel


def label(path: str) -> str:
    """树与版本面板上的显示名：不带文件后缀。"""
    return path[:-len(SUFFIX)] if path.lower().endswith(SUFFIX) else path


def read_bible(root: Path, *, max_chars: int = 3000) -> str:
    """作品记忆正文；超长截断并显式标记，别让截断长得像原文结束。"""
    path = Path(root) / BIBLE_NAME
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（作品记忆超出上限，已截断）"
    return text


_FACET_STATUS = re.compile(r"^状态\s*[:：]\s*(定|悬|隐)\s*$")
_FACET_VIS = re.compile(r"^可见\s*[:：]\s*(读者已见|仅作者)\s*$")


def world_facets(root: Path) -> list[dict[str, Any]]:
    """解析 ``设定/世界.md`` 的 facet 与状态；文件不存在返回空。

    只认二级标题 ``## 名称``；标题可以跟说明文字（如
    ``## 时代与地理  年代、地点……``），按起步 facet 名做前缀匹配。
    ``status`` 为空表示作者还没标状态——不替作者判，交给「现在在哪」去说。
    """
    path = Path(root) / DOCS_DIR / WORLD_NAME
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    facets: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current is not None:
                facets.append(current)
            title = stripped[3:].strip()
            name = next((x for x in WORLD_FACETS if title.startswith(x)),
                        title)
            current = {"name": name, "title": title, "status": "",
                       "visibility": "", "has_content": False}
            continue
        if current is None:
            continue
        matched = _FACET_STATUS.match(stripped)
        if matched:
            current["status"] = matched.group(1)
            continue
        matched = _FACET_VIS.match(stripped)
        if matched:
            current["visibility"] = matched.group(1)
            continue
        if stripped:
            current["has_content"] = True
    if current is not None:
        facets.append(current)
    return facets


def _first_line_text(text: str, *, limit: int = 60) -> str:
    for line in (text or "").splitlines():
        clean = line.strip().lstrip("#").strip()
        if clean and not clean.startswith(">"):
            return clean[:limit]
    return ""


def _first_line(root: Path, rel: str, *, limit: int = 60) -> str:
    # 只读开头：索引每轮都要算，别为了一行把几十份设定整篇读一遍。
    try:
        with (Path(root) / rel).open("r", encoding="utf-8",
                                     errors="replace") as fh:
            text = fh.read(2048)
    except OSError:
        return ""
    return _first_line_text(text, limit=limit)


def section_files(root: Path, section: str) -> list[dict[str, Any]]:
    """某个分区下的全部文件。"""
    if section not in (DOCS_DIR, MATERIALS_DIR, PLANS_DIR):
        raise DocError("分区只能是 设定 / 素材 / 计划稿")
    prefix = section + "/"
    return [f for f in list_files(root) if f["path"].startswith(prefix)]


def dump_section(root: Path, section: str, *,
                 max_total: int = 40000) -> str:
    """整段正文（给记忆合并看全貌）；超过预算就按路径顺序截断。"""
    parts: list[str] = []
    total = 0
    for row in section_files(root, section):
        try:
            text = read_file(root, row["path"])["content"]
        except DocError:
            continue
        block = f"### {row['path']}\n{text.strip()}"
        if parts and total + len(block) > max_total:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def overview(root: Path, *, doc_limit: int = 20,
             material_limit: int = 12, plan_limit: int = 20) -> list[str]:
    """工作区索引：分区标题 + 条目 + 未列出计数（决策 129）。

    有上限就必须显式报「还有 N 份未列出」，不许静默截断——模型需要知道
    自己没看见什么。
    """
    out: list[str] = []
    spec = ((DOCS_DIR, doc_limit, False),
            (MATERIALS_DIR, material_limit, True),
            (PLANS_DIR, plan_limit, False))
    for section, cap, recent in spec:
        rows = section_files(root, section)
        total = len(rows)
        if recent:
            rows = sorted(rows, key=lambda r: -int(r.get("modified") or 0))
        rows = rows[:max(0, int(cap))]
        out.append(f"#### {section}（{total} 份）" if total
                   else f"#### {section}（空）")
        for row in rows:
            line = f"- {row['path']}｜{row['size']} 字节"
            if section != PLANS_DIR:
                head = _first_line(root, row["path"])
                if head:
                    line += f"｜{head}"
            out.append(line)
        hidden = total - len(rows)
        if hidden > 0:
            out.append(f'- …还有 {hidden} 份未列出，用 '
                       f'action="search" scope="{section}" 查')
    return out


def index_lines(root: Path, *, limit: int = 40) -> list[str]:
    """设定索引：路径 + 首行。只带索引、不带正文（读全文让模型自己调工具）。"""
    rows = section_files(root, DOCS_DIR)
    out = []
    for row in rows[:limit]:
        head = _first_line(root, row["path"])
        out.append(f"- {row['path']}｜{row['size']} 字节"
                   + (f"｜{head}" if head else ""))
    hidden = len(rows) - min(len(rows), limit)
    if hidden > 0:
        out.append(f'- …还有 {hidden} 份未列出，用 '
                   f'action="search" scope="{DOCS_DIR}" 查')
    return out


def search(root: Path, query: str, *, scope: str = "",
           limit: int = 10) -> dict[str, Any]:
    """在工作区里按关键词找条目：路径 + 行号 + 首行 + 命中片段。

    素材是碎片库存，首行摘要不一定写得出来，所以检索必须回到正文。
    ``scope`` 为空时查 设定 / 素材 / 计划稿 三处。
    """
    terms = [t for t in str(query or "").split() if t]
    if not terms:
        raise DocError("搜索词不能为空")
    sections = [scope] if scope else [DOCS_DIR, MATERIALS_DIR, PLANS_DIR]
    for name in sections:
        if name not in (DOCS_DIR, MATERIALS_DIR, PLANS_DIR):
            raise DocError("scope 只能是 设定 / 素材 / 计划稿")
    hits: list[dict[str, Any]] = []
    scanned = 0
    for name in sections:
        for row in section_files(root, name):
            scanned += 1
            try:
                text = (Path(root) / row["path"]).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
            first = _first_line_text(text)
            best: tuple[int, int, str] | None = None
            for idx, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                matched = {t for t in terms if t.lower() in low}
                if not matched:
                    continue
                score = len(matched)
                if best is None or score > best[0]:
                    best = (score, idx, line.strip()[:160])
            if best is not None:
                hits.append({"path": row["path"], "line": best[1],
                             "snippet": best[2], "first_line": first,
                             "score": best[0]})
    hits.sort(key=lambda h: (-int(h["score"]), h["path"], int(h["line"])))
    total = len(hits)
    cap = max(1, int(limit))
    out = hits[:cap]
    for item in out:
        item.pop("score", None)
    return {"query": str(query).strip(), "scope": scope or "全部",
            "hits": out, "total": total, "scanned": scanned,
            "truncated": total > len(out)}


def select_for_chapter(root: Path, *, context_text: str = "",
                       limit: int = 6, doc_chars: int = 1200,
                       total_chars: int = 4000) -> tuple[str, dict[str, Any]]:
    """按章节现场选设定，返回（正文块，manifest）。作品记忆不在这里。"""
    rows = section_files(root, DOCS_DIR)
    terms = _context_terms(context_text)
    scored: list[tuple[int, int, str, str]] = []
    for row in rows:
        path = str(row.get("path") or "")
        try:
            body = (Path(root) / path).read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        head = _first_line_text(body)
        low_path, low_head, low_body = path.lower(), head.lower(), body.lower()
        score = 0
        for term in terms:
            if term in low_path:
                score += 3
            elif term in low_head:
                score += 2
            elif term in low_body:
                score += 1
        scored.append((score, int(row.get("modified") or 0), path, body))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    selected: list[str] = []
    truncated: list[str] = []
    dropped: list[str] = []
    parts: list[str] = []
    used = 0
    for score, _modified, path, body in scored:
        if len(selected) >= max(1, int(limit)):
            dropped.append(path)
            continue
        text = body
        cut = False
        if len(text) > doc_chars:
            text = text[:doc_chars].rstrip() + "\n…（已截断）"
            cut = True
        block = f"## 设定：{_friendly(path)}\n{text}"
        if used + len(block) > total_chars and parts:
            dropped.append(path)
            continue
        if cut:
            truncated.append(path)
        parts.append(block)
        selected.append(path)
        used += len(block)
    text = "\n\n".join(parts)
    return text, {"selected": selected, "truncated": truncated,
                  "dropped": dropped, "terms": len(terms),
                  "chars": used, "reason": "contract/entity/keyword/recency"}


def _context_terms(text: str) -> set[str]:
    """章节现场 → 检索词；中文按 2–4 字、英文/数字按词。"""
    out: set[str] = set()
    for chunk in re.findall(r"[A-Za-z0-9_]{2,}|[一-鿿]{2,}",
                            str(text or "")):
        low = chunk.lower()
        if len(chunk) <= 6:
            out.add(low)
        else:
            out.add(low[:3])
            out.add(low[-3:])
    return out


def render_block(root: Path, *, bible_chars: int = 2000,
                 doc_chars: int = 1200, total_chars: int = 4000) -> str:
    """写作上下文用的整块设定：作品记忆 + 设定正文（计划稿与素材不进来）。"""
    parts: list[str] = []
    bible = read_bible(root, max_chars=bible_chars)
    if bible:
        parts.append("## 作品记忆\n" + bible)
    used = sum(len(p) for p in parts)
    for row in [f for f in list_files(root)
                if f["path"].startswith(DOCS_DIR + "/")]:
        if used >= total_chars:
            parts.append("…（设定过多，后面的没有带进来）")
            break
        try:
            text = (Path(root) / row["path"]).read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > doc_chars:
            text = text[:doc_chars].rstrip() + "\n…（已截断）"
        block = f"## 设定：{_friendly(row['path'])}\n{text}"
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)
