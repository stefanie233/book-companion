"""素材库 —— 全局原料仓（决策 156）。

与作品工作区的 ``素材/`` 分开：这里是**不属于任何作品**的原料，跨作品共享，
不进写作 git，也不自动进上下文。作者与 agent 显式检索、显式提升。

一张卡 = 一个文件 = 一个观察/引文/例子。文件落盘为作者可读的 Markdown：

    # 诀别信
    > 素材 7f3a91c2 · 书《窄门》第 12 章 · 块 #3 · 采集 2026-11-05 14:32
    > 分类：情感 / 爱情 / 离别
    > 标签：书信 · 诀别 · 克制
    > 收录：作者指令 · 会话 2f8c…

    ## 原文
    > ……

    ## 说明
    以信仰为由拒绝爱人的一封信……

``material_id`` 是**摘录原文 + 出处**的哈希，不是路径。改名、换分类、
编辑说明都不会改变身份；同一段原文反复收录会被去重。分类是**一个**
主目录（一棵树只能有一个父亲），其余维度靠标签。

检索刻意做得很轻：关键词 + 标签 + 分类，几百张卡不需要向量索引。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: 库根目录：``data/writing/materials/``。
LIBRARY_DIR = "materials"

#: 删除不硬删：移到库内回收站，消费记录仍然指得到。
RECYCLE_DIR = "_回收站"

#: 合并日志：追加式，不参与 audit 遍历（只扫 .md）。
MERGE_LOG = "_合并日志.jsonl"

#: 回收站默认保留期。只由显式清理动作使用，不在启动时自动删。
RECYCLE_RETENTION_DAYS = 90

#: 只收 Markdown；长文请先拆卡。
SUFFIX = ".md"

#: 单卡上限。素材卡是碎片，写成长文说明该拆。
MAX_FILE_BYTES = 256 * 1024

#: 首次建库时种下的顶层模块。作者可以改名、删除、增加；只在库不存在时种。
DEFAULT_ROOTS = ("人物", "情感", "场景", "社会", "语言",
                 "时代", "事件", "写法", "待归类")

#: 未知分类的落点。
FALLBACK_CATEGORY = "待归类"

_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_ID_RE = re.compile(r"素材\s+([0-9a-fA-F]{6,})")
_TAG_SPLIT = re.compile(r"[·、,，;；\s]+")

#: 待归类卡头标记。加工后换成 ``> 加工：…``，状态不靠目录猜。
RAW_STATE = "状态：待归类"
#: 旧版卡头标记，只用于兼容读取；新卡不再写这个。
LEGACY_RAW_STATE = "状态：生肉"
COOK_MARK = "加工："
LOCATION_MARK = "定位："

#: 阅读页收录的待归类卡文件名：``《书名》-YYYYMMDD-HHMMSS(-n).md``。
_RAW_NAME_RE = re.compile(r"^《.*》-\d{8}-\d{6}(?:-\d+)?\.md$")
#: 卡片抬头里的采集时间（与前缀 ``· 采集 `` 同行）。
_COLLECTED_RE = re.compile(r"·\s*采集\s+(.+)$")


class MaterialError(RuntimeError):
    """可展示给作者的素材库错误（路径非法、文件太大、找不到）。"""


# ── 根目录与路径 ──────────────────────────────────────────────────────────

def library_root(project_dir: Path | str) -> Path:
    """全局素材库：``<project>/data/writing/materials``。"""
    return Path(project_dir) / "data" / "writing" / LIBRARY_DIR


def ensure_library(root: Path | str) -> Path:
    """建库并种下默认模块。已存在的库不补种（作者删掉的模块不复活）。"""
    root = Path(root)
    fresh = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if fresh:
        for name in DEFAULT_ROOTS:
            (root / name).mkdir(exist_ok=True)
    return root


def _segments(path: str, *, max_depth: int = 4) -> list[str]:
    raw = str(path or "").replace("\\", "/").strip("/")
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if not parts:
        raise MaterialError("路径不能为空")
    if len(parts) > max_depth:
        raise MaterialError(f"路径最多 {max_depth} 层")
    if any(part in (".", "..") for part in parts):
        raise MaterialError("路径越界")
    for part in parts:
        if _BAD_CHARS.search(part):
            raise MaterialError("路径里有非法字符")
        if part.startswith("."):
            raise MaterialError("不能使用隐藏路径")
    if parts[0] == RECYCLE_DIR:
        raise MaterialError("回收站是只读的")
    return parts


def normalize(path: str) -> str:
    """校验并归一成 ``分类/子类/<hash>-标题.md``。"""
    parts = _segments(path)
    if not parts[-1].lower().endswith(SUFFIX):
        raise MaterialError("只收 .md 文件")
    return "/".join(parts)


def normalize_dir(path: str) -> str:
    """分类目录：1–3 层，不带后缀。"""
    parts = _segments(path, max_depth=3)
    if parts[-1].lower().endswith(SUFFIX):
        raise MaterialError("这是文件，不是目录")
    return "/".join(parts)


def _resolve_rel(root: Path, rel: str) -> Path:
    base = Path(root).resolve()
    target = (Path(root) / rel).resolve()
    if target != base and base not in target.parents:
        raise MaterialError("路径越界")
    return target


def resolve(root: Path | str, path: str) -> Path:
    return _resolve_rel(Path(root), normalize(path))


def resolve_dir(root: Path | str, path: str) -> Path:
    return _resolve_rel(Path(root), normalize_dir(path))


# ── 卡片身份与正文 ────────────────────────────────────────────────────────

def excerpt_id(excerpt: str, source_ref: str = "") -> str:
    """摘录的稳定身份：原文归一后 + 出处，取 sha1 前 12 位。"""
    text = " ".join(str(excerpt or "").split())
    return hashlib.sha1(
        (text + "\n" + str(source_ref or "")).encode("utf-8")).hexdigest()[:12]


def _slug(title: str, *, limit: int = 18) -> str:
    text = re.sub(r'[\\/:*?"<>|\s]+', "-", str(title or "").strip())
    text = text.strip("-._")
    return text[:limit] or "素材"


def _normalize_tags(tags: str | Iterable[str] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = _TAG_SPLIT.split(tags)
    else:
        raw = [str(x) for x in tags]
    out: list[str] = []
    for tag in raw:
        tag = tag.strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:60]
    return "未命名素材"


def card_markdown(*, material_id: str, title: str, excerpt: str,
                  description: str = "", source_line: str = "作者自述",
                  category: str = FALLBACK_CATEGORY,
                  tags: Sequence[str] = (), origin: str = "",
                  session_id: str = "", instruction: str = "",
                  collected_at: str | None = None,
                  header_extra: Sequence[str] = ()) -> str:
    """生成一张素材卡的 Markdown。抬头字段保持可读、可正则解析。

    ``header_extra`` 是追加在标签之后的抬头行（不带 ``> ``），例如待归类卡的
    ``状态：待归类`` / ``定位：epubcfi(…)``。未知抬头行解析器会忽略，但会原样保留。
    """
    stamp = collected_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# {str(title or '').strip() or _first_line(excerpt)}", ""]
    lines.append(f"> 素材 {material_id} · {source_line} · 采集 {stamp}")
    lines.append(f"> 分类：{category}")
    tags = list(tags)
    if tags:
        lines.append("> 标签：" + " · ".join(tags))
    for extra in header_extra:
        text = str(extra or "").strip()
        if text:
            lines.append("> " + text)
    meta = [x for x in (origin, f"会话 {session_id}" if session_id else "") if x]
    if meta:
        lines.append("> 收录：" + " · ".join(meta))
    if str(instruction or "").strip():
        lines.append("> 指令：" + " ".join(str(instruction).split()))
    lines += ["", "## 原文", ""]
    body = str(excerpt or "").rstrip()
    if body:
        for line in body.splitlines():
            lines.append("> " + line if line.strip() else ">")
    else:
        lines.append(">")
    if str(description or "").strip():
        lines += ["", "## 说明", "", str(description).strip()]
    return "\n".join(lines).rstrip() + "\n"


def _sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current = ""
    buf: list[str] = []
    for line in str(text or "").splitlines():
        if line.startswith("## "):
            if current:
                out[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    return out


def _strip_quote(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^>\s?", "", line)
        lines.append(line)
    return "\n".join(lines).strip()


def parse_card(text: str, path: str = "") -> dict[str, Any]:
    """读回头部字段。非卡片文件（手写 Markdown）也能解析出标题。"""
    out: dict[str, Any] = {
        "path": path, "title": "", "material_id": "", "category": "",
        "tags": [], "excerpt": "", "description": "", "source_line": "",
        "instruction": "", "status": "", "processed_at": "", "location": "",
        "collected_at": "", "raw": False,
    }
    for line in str(text or "").splitlines():
        line = line.strip()
        if not out["title"] and line.startswith("# "):
            out["title"] = line[2:].strip()
        if not out["material_id"]:
            match = _ID_RE.search(line)
            if match:
                out["material_id"] = match.group(1)
                out["source_line"] = line.lstrip("> ").strip()
        if not out["category"] and line.startswith("> 分类："):
            out["category"] = line[len("> 分类："):].strip()
        if not out["tags"] and line.startswith("> 标签："):
            out["tags"] = _normalize_tags(line[len("> 标签："):])
        if not out["instruction"] and line.startswith("> 指令："):
            out["instruction"] = line[len("> 指令："):].strip()
        if not out["status"] and line.startswith("> 状态："):
            out["status"] = line[len("> 状态："):].strip()
        if not out["processed_at"] and line.startswith("> 加工："):
            out["processed_at"] = line[len("> 加工："):].strip()
        if not out["location"] and line.startswith("> 定位："):
            out["location"] = line[len("> 定位："):].strip()
    match = _COLLECTED_RE.search(str(out["source_line"] or ""))
    if match:
        out["collected_at"] = match.group(1).strip()
    out["raw"] = is_raw_card(out, path)
    sections = _sections(text)
    if "原文" in sections:
        out["excerpt"] = _strip_quote(sections["原文"])
    if "说明" in sections:
        out["description"] = sections["说明"].strip()
    if not out["title"]:
        out["title"] = _first_line(text)
    return out


def is_raw_card(card: Mapping[str, Any], rel: str = "") -> bool:
    """这还是一张待归类卡吗？

    判据按可信度排序：
      1. 有 ``> 加工：`` → 已加工卡，无论它在哪个目录；
      2. 卡头写着 ``状态：待归类``（旧卡可能是旧标记）→ 待归类卡；
      3. 兼容决策 165 的首批卡（没有状态行）：在 ``待归类`` 且文件名是
         ``《书名》-时间戳`` → 待归类卡。加工过的卡文件名是 ``<id>-<标题>``，
         不会误判。
    """
    if str(card.get("processed_at") or "").strip():
        return False
    status = str(card.get("status") or "")
    legacy_status = LEGACY_RAW_STATE.split("：", 1)[-1]
    if status and ("待归类" in status or legacy_status in status):
        return True
    parts = str(rel or "").replace("\\", "/").strip("/").split("/")
    return bool(parts
                and parts[0] == FALLBACK_CATEGORY
                and _RAW_NAME_RE.match(parts[-1]))


# ── 遍历 ──────────────────────────────────────────────────────────────────

def _iter_files(root: Path | str) -> Iterable[Path]:
    base = Path(root)
    if not base.is_dir():
        return
    for path in sorted(base.rglob(f"*{SUFFIX}")):
        rel = path.relative_to(base)
        if not rel.parts or rel.parts[0] == RECYCLE_DIR:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        yield path


def list_cards(root: Path | str) -> list[dict[str, Any]]:
    """全部卡片：路径、标题、分类、标签、身份、大小、修改时间。"""
    base = Path(root)
    out: list[dict[str, Any]] = []
    for path in _iter_files(base):
        rel = path.relative_to(base).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            stat = path.stat()
        except OSError:
            continue
        card = parse_card(text, rel)
        out.append({
            "path": rel,
            "title": card["title"],
            "material_id": card["material_id"],
            "category": card["category"] or Path(rel).parent.as_posix(),
            "tags": card["tags"],
            "raw": card["raw"],
            "source_line": card["source_line"],
            "size": stat.st_size,
            "modified": int(stat.st_mtime * 1000),
        })
    return out


def list_dirs(root: Path | str) -> list[dict[str, Any]]:
    """分类目录（含空目录），不含回收站。"""
    base = Path(root)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_dir():
            continue
        rel = path.relative_to(base)
        if not rel.parts or rel.parts[0] == RECYCLE_DIR:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        out.append({"path": rel.as_posix(), "kind": "dir"})
    return out


# ── 读 / 写 / 改名 / 回收站 ──────────────────────────────────────────────

def _entry(root: Path, rel: str, path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": rel, "size": stat.st_size,
            "modified": int(stat.st_mtime * 1000), "kind": "file"}


def read_card(root: Path | str, path: str) -> dict[str, Any]:
    rel = normalize(path)
    target = resolve(root, rel)
    if not target.is_file():
        raise MaterialError("文件不存在")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise MaterialError(f"文件太大（{size} 字节），先拆成几份")
    text = target.read_text(encoding="utf-8", errors="replace")
    row = _entry(Path(root), rel, target)
    row.update(parse_card(text, rel))
    row["content"] = text
    return row


def write_text(root: Path | str, path: str, content: str) -> dict[str, Any]:
    rel = normalize(path)
    target = resolve(root, rel)
    data = str(content or "").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise MaterialError(f"内容太大（{len(data)} 字节），先拆成几份")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(content or ""), encoding="utf-8")
    return _entry(Path(root), rel, target)


def create_file(root: Path | str, path: str, content: str = "") -> dict[str, Any]:
    rel = normalize(path)
    target = resolve(root, rel)
    if target.exists():
        raise MaterialError("同名文件已存在")
    return write_text(root, rel, content)


def create_dir(root: Path | str, path: str) -> dict[str, Any]:
    rel = normalize_dir(path)
    target = resolve_dir(root, rel)
    if target.exists():
        raise MaterialError("同名目录已存在")
    target.mkdir(parents=True)
    return {"path": rel, "kind": "dir"}


def rename_entry(root: Path | str, path: str, new_name: str, *,
                 kind: str = "file") -> dict[str, Any]:
    """原地改名；父目录不变。"""
    name = str(new_name or "").strip()
    if not name or "/" in name or "\\" in name or _BAD_CHARS.search(name):
        raise MaterialError("名字不合法")
    if kind == "dir":
        rel = normalize_dir(path)
        if not name:
            raise MaterialError("目录名不能为空")
        target = resolve_dir(root, rel)
        parent = rel.rstrip("/").rsplit("/", 1)[0] if "/" in rel else ""
        dest_rel = f"{parent}/{name}" if parent else name
        resolve_dir(root, dest_rel)  # 路径合法性
    else:
        rel = normalize(path)
        if not name.lower().endswith(SUFFIX):
            name += SUFFIX
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        dest_rel = f"{parent}/{name}" if parent else name
        target = resolve(root, rel)
        resolve(root, dest_rel)
    dest = _resolve_rel(Path(root), dest_rel)
    if dest.exists():
        raise MaterialError("同名条目已存在")
    target.rename(dest)
    return {"path": dest_rel, "old_path": rel, "kind": kind}


def move_entry(root: Path | str, path: str, target_dir: str, *,
               kind: str = "file") -> dict[str, Any]:
    """换分类：保留文件名，只换父目录。"""
    rel = normalize(path) if kind != "dir" else normalize_dir(path)
    dest_dir = normalize_dir(target_dir)
    src = _resolve_rel(Path(root), rel)
    if not src.exists():
        raise MaterialError("文件不存在")
    if kind == "dir" and (dest_dir == rel or dest_dir.startswith(rel + "/")):
        raise MaterialError("不能把文件夹移进它自己")
    name = Path(rel).name
    dest_rel = f"{dest_dir}/{name}"
    if dest_rel == rel:
        raise MaterialError("已经在这个分类里")
    dest = _resolve_rel(Path(root), dest_rel)
    if dest.exists():
        raise MaterialError(f"目标分类已有同名项：{name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return {"path": dest_rel, "old_path": rel, "kind": kind}


# ── 回收站：进、看、回、清 ────────────────────────────────────────────────

_STAMP_RE = re.compile(r"^(\d{8})-(\d{6})")
_LEGACY_RECYCLE_RE = re.compile(r"^(\d{8}-\d{6})(?:-\d+)?-(.+)$")


def _stamp_time(name: str) -> float | None:
    match = _STAMP_RE.match(str(name or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2),
                                 "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def _tree_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def merge_plan(root: Path | str, short_path: str,
               long_path: str) -> dict[str, Any]:
    """校验「短卡并入长卡」：同书、短原文被长原文包含、两张都已加工。

    V1 只做包含型；不判断消费连线（那要查 DB，由 service 层做）。
    """
    base = Path(root)
    short_rel = normalize(short_path)
    long_rel = normalize(long_path)
    if short_rel == long_rel:
        raise MaterialError("不能把一张卡并进它自己")
    short_file = _resolve_rel(base, short_rel)
    long_file = _resolve_rel(base, long_rel)
    if not short_file.is_file() or not long_file.is_file():
        raise MaterialError("合并的两张卡都必须存在")
    short = parse_card(short_file.read_text(encoding="utf-8", errors="replace"),
                       short_rel)
    long = parse_card(long_file.read_text(encoding="utf-8", errors="replace"),
                      long_rel)
    if short["raw"] or long["raw"]:
        raise MaterialError("待归类卡先 cook 成已加工卡，再参与合并")
    short_book = book_of(str(short.get("source_line") or ""))
    long_book = book_of(str(long.get("source_line") or ""))
    if not short_book or short_book != long_book:
        raise MaterialError("V1 只合并同一本书的两张卡")
    a = _compare_text(str(short.get("excerpt") or ""))
    b = _compare_text(str(long.get("excerpt") or ""))
    if len(a) < 15:
        raise MaterialError("短卡原文太短，无法判断包含关系")
    if a != b and a not in b:
        raise MaterialError("短卡原文没有整段出现在长卡里；V1 只做包含型合并")
    tags_added = [tag for tag in _normalize_tags(short.get("tags"))
                  if tag not in _normalize_tags(long.get("tags"))]
    raw_source = str(short.get("source_line") or "")
    display_source = re.sub(
        r"^素材\s+[0-9a-fA-F]{6,}\s*·\s*", "", raw_source).strip()
    return {
        "from": short_rel, "into": long_rel,
        "from_id": str(short.get("material_id") or ""),
        "into_id": str(long.get("material_id") or ""),
        "from_title": str(short.get("title") or ""),
        "into_title": str(long.get("title") or ""),
        "book": short_book,
        "why": ("原文相同" if a == b else "短卡原文整段包含在长卡里"),
        "tags_added": tags_added,
        "from_source": display_source or raw_source,
        "from_description": str(short.get("description") or ""),
    }


def _merged_card_text(long_text: str, short: Mapping[str, Any],
                      plan: Mapping[str, Any]) -> str:
    """只改长卡抬头，``## 原文`` 起的正文逐字节保留。"""
    head, sep, body = long_text.partition("## 原文")
    if not sep:
        raise MaterialError("长卡没有「## 原文」段，无法合并")
    lines = head.splitlines()
    short_tags = _normalize_tags(short.get("tags"))
    tag_idx = next((i for i, line in enumerate(lines)
                    if line.startswith("> 标签：")), None)
    if tag_idx is not None:
        old = _normalize_tags(lines[tag_idx][len("> 标签："):])
        merged_tags = old + [t for t in short_tags if t not in old]
        lines[tag_idx] = "> 标签：" + " · ".join(merged_tags)
    elif short_tags:
        lines.append("> 标签：" + " · ".join(short_tags))
    while lines and not lines[-1].strip():
        lines.pop()
    marker = (f"> 合并自：{plan['from']}（素材 {plan['from_id']}；"
              f"{plan['from_source']}）")
    if marker not in lines:
        lines.append(marker)
    new_head = "\n".join(lines) + "\n\n"
    if ("## 说明" not in body
            and str(short.get("description") or "").strip()):
        body = (body.rstrip() + "\n\n## 说明\n\n"
                + str(short["description"]).strip() + "\n")
    return new_head + "## 原文" + body


def merge_cards(root: Path | str, short_path: str,
                long_path: str) -> dict[str, Any]:
    """把包含型短卡并入长卡：长卡原文不变，短卡进回收站并记 merged_into。"""
    base = Path(root)
    plan = merge_plan(base, short_path, long_path)
    long_file = _resolve_rel(base, plan["into"])
    short_file = _resolve_rel(base, plan["from"])
    long_text = long_file.read_text(encoding="utf-8", errors="replace")
    short = parse_card(
        short_file.read_text(encoding="utf-8", errors="replace"),
        plan["from"])
    new_text = _merged_card_text(long_text, short, plan)
    write_text(base, plan["into"], new_text)
    try:
        recycled = recycle(base, plan["from"], extra={
            "merged_into": plan["into"],
            "merged_into_id": plan["into_id"],
            "merged_at": time.time(),
        })
    except Exception:
        # 回收失败就把长卡还原，不能留下「半合并」。
        write_text(base, plan["into"], long_text)
        raise
    logged = True
    try:
        with (base / MERGE_LOG).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": time.time(), **{k: plan[k] for k in
                                      ("from", "into", "from_id", "into_id",
                                       "book", "why", "tags_added",
                                       "from_source", "from_description")},
                "recycled": recycled.get("recycled"),
            }, ensure_ascii=False) + "\n")
    except OSError:
        logged = False
    return {**plan, "merged": True, "recycled": recycled.get("recycled"),
            "logged": logged}


def recycle(root: Path | str, path: str, *, kind: str = "file",
           extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """删除 = 移进 ``_回收站/<时间戳>/``；不硬删，消费记录仍然指得到。

    每一项带一份 ``manifest.json`` 记原始相对路径：恢复要放回原分类，
    不能靠文件名猜。旧版扁平回收站仍然认（见 ``list_recycled``）。
    """
    rel = normalize(path) if kind != "dir" else normalize_dir(path)
    target = _resolve_rel(Path(root), rel)
    if not target.exists():
        raise MaterialError("文件不存在")
    trash = Path(root) / RECYCLE_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = trash / stamp
    counter = 2
    while base.exists():
        base = trash / f"{stamp}-{counter}"
        counter += 1
    dest = base / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(dest))
    manifest = {"rel": rel, "kind": kind, "name": Path(rel).name,
                "deleted_at": time.time()}
    manifest.update(dict(extra or {}))
    (base / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": rel, "recycled": base.relative_to(Path(root)).as_posix(),
            "kind": kind}


def empty_dir(root: Path | str, path: str) -> dict[str, Any]:
    """清空文件夹：把里面所有素材卡（含子分类）逐张移进回收站。

    只清文件，**保留文件夹与子分类本身**；每张卡都有自己的回收站记录，
    恢复时回到原来的相对路径。不是硬删——清理仍走回收站。
    """
    base = Path(root)
    rel = normalize_dir(path)
    if rel.split("/")[0] == RECYCLE_DIR:
        raise MaterialError("回收站不在这里清")
    target = _resolve_rel(base, rel)
    if not target.is_dir():
        raise MaterialError("文件夹不存在")
    moved: list[str] = []
    for file_path in sorted(target.rglob(f"*{SUFFIX}")):
        card_rel = file_path.relative_to(base).as_posix()
        try:
            row = recycle(base, card_rel, kind="file")
        except OSError as exc:
            raise MaterialError(f"{card_rel} 移入回收站失败：{exc}") from exc
        moved.append(str(row.get("recycled") or ""))
    return {"path": rel, "count": len(moved), "recycled": moved}


def _card_read_preview(path: Path, rel: str = "") -> tuple[str, str]:
    """回收站只读预览：标题 + 原文前 80 字；读不到就返回空。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    card = parse_card(text, rel)
    return (str(card.get("title") or ""),
            " ".join(str(card.get("excerpt") or "").split())[:80])


def list_recycled(root: Path | str) -> list[dict[str, Any]]:
    """回收站条目：原始路径、类型、删除时间、大小、原文预览。"""
    base = Path(root)
    trash = base / RECYCLE_DIR
    if not trash.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(trash.iterdir()):
        if entry.is_dir():
            try:
                manifest = json.loads(
                    (entry / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
            rel = str(manifest.get("rel") or "")
            kind = str(manifest.get("kind") or "file")
            stamp = _stamp_time(entry.name) or 0
            deleted = float(manifest.get("deleted_at") or stamp or 0)
            title = preview = ""
            if kind == "file" and rel:
                try:
                    payload = _resolve_rel(entry, rel)
                except MaterialError:
                    payload = None
                if payload is not None and payload.is_file():
                    title, preview = _card_read_preview(payload, rel)
            out.append({
                "path": entry.relative_to(base).as_posix(),
                "name": Path(rel).name if rel else entry.name,
                "original": rel,
                "kind": kind,
                "deleted_at": int(deleted * 1000),
                "size": _tree_size(entry / rel) if rel else 0,
                "title": title,
                "preview": preview,
                "merged_into": str(manifest.get("merged_into") or ""),
                "legacy": not bool(rel),
            })
        elif entry.suffix.lower() == SUFFIX:
            match = _LEGACY_RECYCLE_RE.match(entry.name)
            if not match:
                continue
            stamp = _stamp_time(match.group(1)) or 0
            title, preview = _card_read_preview(entry, entry.name)
            out.append({
                "path": entry.relative_to(base).as_posix(),
                "name": match.group(2),
                "original": match.group(2),
                "kind": "file",
                "deleted_at": int(stamp * 1000),
                "size": _tree_size(entry),
                "title": title,
                "preview": preview,
                "merged_into": "",
                "legacy": True,
            })
    out.sort(key=lambda row: -int(row.get("deleted_at") or 0))
    return out


def restore(root: Path | str, recycled: str, *,
            target: str = "") -> dict[str, Any]:
    """把回收站里的一项放回去；默认回原分类，也可以指定新路径。"""
    base = Path(root)
    rel = str(recycled or "").replace("\\", "/").strip("/")
    if not rel.startswith(RECYCLE_DIR + "/"):
        raise MaterialError("只能从回收站恢复")
    src = _resolve_rel(base, rel)
    if not src.exists():
        raise MaterialError("回收站里没有这一项")
    if src.is_dir():
        try:
            manifest = json.loads(
                (src / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        original = str(manifest.get("rel") or "")
        kind = str(manifest.get("kind") or "file")
        if not original:
            raise MaterialError("这条回收记录没有原始路径，无法恢复")
        payload = src / original
        if not payload.exists():
            raise MaterialError("回收站里找不到原始文件")
        dest_rel = str(target or "").strip() or original
        dest_rel = (normalize(dest_rel) if kind != "dir"
                    else normalize_dir(dest_rel))
        dest = _resolve_rel(base, dest_rel)
        if dest.exists():
            raise MaterialError(f"目标位置已有 {dest_rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(payload), str(dest))
        shutil.rmtree(src, ignore_errors=True)
        return {"restored": dest_rel, "kind": kind, "original": original}
    # 旧版扁平回收站：靠卡片抬头的分类放回去。
    match = _LEGACY_RECYCLE_RE.match(src.name)
    if not match:
        raise MaterialError("这条回收记录无法识别")
    name = match.group(2)
    dest_rel = str(target or "").strip()
    if not dest_rel:
        card = parse_card(src.read_text(encoding="utf-8", errors="replace"))
        category = str(card.get("category") or "").strip()
        dest_rel = f"{category}/{name}" if category else name
    dest_rel = normalize(dest_rel)
    dest = _resolve_rel(base, dest_rel)
    if dest.exists():
        raise MaterialError(f"目标位置已有 {dest_rel}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return {"restored": dest_rel, "kind": "file", "original": name}


def purge_recycled(root: Path | str, *, days: int = RECYCLE_RETENTION_DAYS,
                   now: float | None = None) -> dict[str, Any]:
    """显式清理回收站：删掉超过 ``days`` 天的条目。不在启动时自动跑。"""
    base = Path(root)
    trash = base / RECYCLE_DIR
    if not trash.is_dir():
        return {"purged": [], "count": 0, "days": int(days)}
    cutoff = (now if now is not None else time.time()) - max(0, int(days)) * 86400
    purged: list[str] = []
    for entry in sorted(trash.iterdir()):
        if entry.is_dir():
            stamp = _stamp_time(entry.name)
            if stamp is None or stamp > cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            purged.append(entry.relative_to(base).as_posix())
        elif entry.suffix.lower() == SUFFIX:
            match = _LEGACY_RECYCLE_RE.match(entry.name)
            stamp = _stamp_time(match.group(1)) if match else None
            if stamp is None or stamp > cutoff:
                continue
            entry.unlink(missing_ok=True)
            purged.append(entry.relative_to(base).as_posix())
    return {"purged": purged, "count": len(purged), "days": int(days)}


def recycled_summary(root: Path | str, *,
                     days: int = RECYCLE_RETENTION_DAYS,
                     now: float | None = None) -> dict[str, Any]:
    items = list_recycled(root)
    now_ms = (now if now is not None else time.time()) * 1000
    overdue = [row for row in items
               if row.get("deleted_at")
               and now_ms - int(row["deleted_at"]) > int(days) * 86400000]
    return {"count": len(items), "bytes": sum(int(r.get("size") or 0)
                                              for r in items),
            "overdue": len(overdue), "retention_days": int(days)}


# ── 审计：只报告，不改任何东西 ────────────────────────────────────────────

def _excerpt_prefix(text: str, limit: int = 60) -> str:
    return " ".join(str(text or "").split())[:limit]


_CARD_SOURCE_RE = re.compile(
    r"^素材\s+[0-9a-fA-F]{6,}\s*·\s*(.*?)\s*·\s*采集\s+.*$")
_SPAN_SUFFIX_RE = re.compile(r"\s*·\s*第\s*\d+(?:[–-]\d+)?\s*段$")
_BOOK_RE = re.compile(r"书《([^》]+)》")


def identity_source(source_line: str) -> str:
    """从卡片抬头还原身份计算用的出处（去掉身份、采集时间与段落跨度）。"""
    text = str(source_line or "").strip()
    match = _CARD_SOURCE_RE.match(text)
    if match:
        text = match.group(1)
    return _SPAN_SUFFIX_RE.sub("", text).strip()


def book_of(source_line: str) -> str:
    """出处里的书名（``书《X》`` → ``X``）；非书来源返回空串。"""
    match = _BOOK_RE.search(str(source_line or ""))
    return match.group(1).strip() if match else ""


def _compare_text(text: str) -> str:
    """只用于包含判断：折叠空白、去掉首尾引号；不改原文。"""
    return " ".join(str(text or "").split()).strip("“”\"'「」『』")


def _collected_ms(card: Mapping[str, Any]) -> int:
    """采集时间（毫秒）；卡头缺时间就退回文件修改时间。"""
    stamp = str(card.get("collected_at") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(stamp, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return int(card.get("modified") or 0)


def audit(root: Path | str, *, use_counts: Mapping[str, int] | None = None,
          unused_days: int = 90, now: float | None = None,
          sample: int = 8) -> dict[str, Any]:
    """盘点库的健康度。只读；重复与未消费都只给候选，不动文件。"""
    base = Path(root)
    if not base.is_dir():
        return {"total": 0, "used": 0, "unused": 0, "unused_over_days": 0,
                "broken": [], "broken_count": 0,
                "identity_mismatch": [], "identity_mismatch_count": 0,
                "duplicate_ids": [], "duplicate_id_count": 0,
                "near_duplicates": [], "near_duplicate_count": 0,
                "recycled": recycled_summary(
                    base, days=unused_days, now=now)}
    cards: list[dict[str, Any]] = []
    broken: list[dict[str, str]] = []
    mismatched: list[dict[str, str]] = []
    ids: dict[str, list[str]] = {}
    for path in _iter_files(base):
        rel = path.relative_to(base).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            stat = path.stat()
        except OSError:
            continue
        card = parse_card(text, rel)
        mid = str(card.get("material_id") or "")
        if not mid or not card.get("category"):
            broken.append({"path": rel,
                           "why": "缺身份" if not mid else "缺分类"})
        if mid:
            ids.setdefault(mid, []).append(rel)
            # 待归类卡还没有消费连线，身份错位也不算「原文被改过」；旧版卡的
            # 身份里带过 CFI（卡头存不下），重算必然对不上，统一豁免。
            if not card["raw"]:
                basis = identity_source(str(card.get("source_line") or ""))
                if (card.get("excerpt")
                        and excerpt_id(str(card["excerpt"]), basis) != mid):
                    mismatched.append({"path": rel, "material_id": mid,
                                       "why": "原文改过，身份不再对应内容"})
        cards.append({
            "path": rel, "material_id": mid, "title": card["title"],
            "excerpt": card["excerpt"], "source_line": card["source_line"],
            "category": card["category"] or category_of(rel),
            "modified": int(stat.st_mtime * 1000),
            "raw": bool(card.get("raw")),
            "collected_at": card.get("collected_at") or "",
        })
    duplicate_ids = [{"material_id": mid, "paths": paths}
                     for mid, paths in ids.items() if len(paths) > 1]
    near: list[dict[str, Any]] = []
    pair_rows: dict[tuple[str, ...], dict[str, Any]] = {}

    def add_near(key: str, rows: list[dict[str, Any]],
                 why: str) -> dict[str, Any] | None:
        if len(rows) < 2:
            return None
        paths = tuple(sorted(str(r["path"]) for r in rows[:4]))
        row = pair_rows.get(paths)
        if row is not None:
            return row
        row = {"why": why, "paths": list(paths),
               "ids": [r["material_id"] for r in rows[:4]],
               "prefix": key}
        pair_rows[paths] = row
        near.append(row)
        return row

    same_text: dict[str, list[dict[str, Any]]] = {}
    same_source: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for card in cards:
        text = " ".join(str(card.get("excerpt") or "").split())
        if len(text) >= 40:
            same_text.setdefault(text, []).append(card)
        if text:
            # 同一本书优先按书名归组；章/块跨度的写法不该拆开同一处引语。
            source = (book_of(str(card.get("source_line") or ""))
                      or str(card.get("source_line") or "").split(" · 第")[0])
            same_source.setdefault(
                (source, _excerpt_prefix(text)), []).append(card)
    for text, rows in same_text.items():
        if len(rows) > 1:
            add_near(_excerpt_prefix(text), rows, "原文相同")
    for (source, prefix), rows in same_source.items():
        if len(rows) > 1:
            add_near(prefix, rows, f"同出处近似：{source}")

    # 包含型重复：短摘句整段出现在长摘句里（同书），常见于「摘句卡 +
    # 补全上下文的整块卡」。只报告，并指出建议保留哪张。
    contained: list[dict[str, Any]] = []
    by_book: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        book = book_of(str(card.get("source_line") or ""))
        if book:
            by_book.setdefault(book, []).append(card)
    for book, rows in by_book.items():
        for idx, left in enumerate(rows):
            for right in rows[idx + 1:]:
                a = _compare_text(left["excerpt"])
                b = _compare_text(right["excerpt"])
                if min(len(a), len(b)) < 15 or a == b:
                    continue
                if a in b:
                    short, longer = left, right
                elif b in a:
                    short, longer = right, left
                else:
                    continue
                row = add_near(_excerpt_prefix(short["excerpt"]),
                               [longer, short], f"原文包含（同书《{book}》）")
                if row is None:
                    continue
                row["keep"] = longer["path"]
                row["recycle"] = short["path"]
                row["contained"] = True
                contained.append({
                    "why": f"原文包含（同书《{book}》）",
                    "book": book,
                    "paths": [longer["path"], short["path"]],
                    "ids": [longer["material_id"], short["material_id"]],
                    "keep": longer["path"],
                    "recycle": short["path"],
                    "prefix": _excerpt_prefix(short["excerpt"]),
                })
    now_ms = (now if now is not None else time.time()) * 1000
    # 待归类卡的生命周期：只报「收进来多久了」，不自动加工、不自动删。
    pending_rows = [c for c in cards if c.get("raw")]
    pending_aged = [c for c in pending_rows
                    if now_ms - _collected_ms(c)
                    > int(unused_days) * 86400000]
    counts = dict(use_counts or {})
    unused = [c for c in cards
              if not c["material_id"] or not counts.get(c["material_id"])]
    stale = [c for c in unused
             if now_ms - int(c["modified"]) > int(unused_days) * 86400000]
    used = len(cards) - len(unused)
    return {
        "total": len(cards),
        "used": used,
        "unused": len(unused),
        "unused_over_days": len(stale),
        "broken": broken[:sample],
        "broken_count": len(broken),
        "identity_mismatch": mismatched[:sample],
        "identity_mismatch_count": len(mismatched),
        "duplicate_ids": duplicate_ids[:sample],
        "duplicate_id_count": len(duplicate_ids),
        "near_duplicates": near[:sample],
        "near_duplicate_count": len(near),
        "contained_duplicates": contained[:sample],
        "contained_count": len(contained),
        "pending_count": len(pending_rows),
        "pending_over_days": len(pending_aged),
        "pending_over_sample": [c["path"] for c in pending_aged[:sample]],
        "recycled": recycled_summary(base, days=unused_days, now=now),
        "samples": {
            "unused": [c["path"] for c in unused[:sample]],
            "stale": [c["path"] for c in stale[:sample]],
        },
    }


# ── 收录 ──────────────────────────────────────────────────────────────────

def find_by_id(root: Path | str, material_id: str) -> str:
    """按身份找卡：只扫抬头，几百张卡足够快。"""
    needle = f"素材 {material_id}"
    for path in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if needle in text:
            return path.relative_to(Path(root)).as_posix()
    return ""


def category_of(rel: str) -> str:
    parent = Path(rel).parent.as_posix()
    return "" if parent == "." else parent


def _card_identity(card: Mapping[str, Any]) -> str:
    """卡片当前身份：原文 + 出处（书/章/块），不含 CFI 这类卡头存不下的坐标。

    待归类卡的身份以这里为准；旧版卡曾把 CFI 算进哈希，加工时一并纠正
    （待归类卡没有消费连线，换身份是安全的）。
    """
    excerpt = str(card.get("excerpt") or "").strip()
    if not excerpt:
        return str(card.get("material_id") or "")
    basis = identity_source(str(card.get("source_line") or ""))
    return excerpt_id(excerpt, basis)


def pending_cards(root: Path | str, *, limit: int = 20) -> dict[str, Any]:
    """待加工的待归类卡：按文件名时间升序（最早收的先加工）。

    ``duplicate_of`` 是已经存在的已加工卡路径（同文同源）；只报告，不合并。
    """
    base = Path(root)
    raws: list[tuple[str, dict[str, Any], str]] = []
    cooked: dict[str, list[str]] = {}
    for path in _iter_files(base):
        rel = path.relative_to(base).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        card = parse_card(text, rel)
        mid = _card_identity(card)
        if card["raw"]:
            raws.append((rel, card, mid))
        elif mid:
            cooked.setdefault(mid, []).append(rel)
    # 按采集时间升序（跨书名也是最早收的先加工）；卡头缺采集时间就排最后。
    raws.sort(key=lambda row: (row[1].get("collected_at") or "9999", row[0]))
    items: list[dict[str, Any]] = []
    for rel, card, mid in raws[:max(0, int(limit))]:
        items.append({
            "path": rel,
            "title": card.get("title") or "",
            "material_id": mid,
            "source": identity_source(str(card.get("source_line") or "")),
            "location": card.get("location") or "",
            "collected_at": card.get("collected_at") or "",
            "preview": " ".join(str(card.get("excerpt") or "").split())[:80],
            "duplicate_of": list(cooked.get(mid) or ()),
        })
    return {"items": items, "count": len(items), "total": len(raws)}


def cook(root: Path | str, path: str, *, title: str = "",
         category: str = FALLBACK_CATEGORY,
         tags: str | Iterable[str] | None = None,
         description: str = "",
         now: datetime | None = None) -> dict[str, Any]:
    """把一张待归类卡加工成已加工卡：同一张卡的状态迁移，不是重收。

    只改卡头与追加 ``## 说明``；``## 原文`` 及之后的正文逐字节保留，
    出处、采集时间、收录、定位全部原样带着走。文件从
    ``待归类/《书名》-时间戳.md`` 迁到 ``<分类>/<id>-<标题>.md``。
    """
    base = Path(root)
    rel = normalize(path)
    target = resolve(base, rel)
    if not target.is_file():
        raise MaterialError("文件不存在")
    text = target.read_text(encoding="utf-8", errors="replace")
    card = parse_card(text, rel)
    if not card["raw"]:
        raise MaterialError("这张卡已经加工过了")
    excerpt = str(card.get("excerpt") or "")
    if not excerpt.strip():
        raise MaterialError("卡里没有原文，不能加工")
    mid = _card_identity(card)
    if not mid:
        raise MaterialError("卡片缺身份，先补抬头")
    # 同文同源的已加工卡已存在：不覆盖、不合并，交给作者决定怎么处理这张待归类卡。
    duplicate = ""
    for other in _iter_files(base):
        other_rel = other.relative_to(base).as_posix()
        if other_rel == rel:
            continue
        try:
            other_card = parse_card(
                other.read_text(encoding="utf-8", errors="replace"), other_rel)
        except OSError:
            continue
        if not other_card["raw"] and _card_identity(other_card) == mid:
            duplicate = other_rel
            break
    if duplicate:
        return {"ok": False, "exists": True, "existing": duplicate,
                "material_id": mid, "path": rel}

    title = str(title or "").strip() or _first_line(excerpt)
    cat = normalize_dir(category or FALLBACK_CATEGORY)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")

    lines = text.splitlines()
    cut = next((i for i, line in enumerate(lines)
                if line.strip() == "## 原文"), -1)
    if cut < 0:
        raise MaterialError("不是标准素材卡（缺「## 原文」）")
    head_orig = lines[:cut]
    source_full = next((line.strip() for line in head_orig
                        if line.strip().startswith("> 素材")), "")
    if not source_full:
        source_full = f"> 素材 {mid} · 作者自述 · 采集 {stamp}"
    old_id = str(card.get("material_id") or "")
    if old_id and old_id != mid:
        # 旧版卡的身份带过 CFI；加工时统一到「原文 + 书/章/块」的哈希，
        # 抬头里的旧 id 一并换掉，避免文件名与身份再次分裂。
        source_full = source_full.replace(f"素材 {old_id}", f"素材 {mid}", 1)
    head = [f"# {title}", "", source_full, f"> 分类：{cat}"]
    tag_list = _normalize_tags(tags)
    if tag_list:
        head.append("> 标签：" + " · ".join(tag_list))
    for line in head_orig:
        clean = line.strip()
        if not clean or clean.startswith(("# ", "> 素材")):
            continue
        if clean.startswith(("> 分类：", "> 标签：", "> 状态：", "> 加工：")):
            continue
        head.append(clean)          # 收录 / 指令 / 定位 与未知抬头原样保留
    head.append(f"> 加工：{stamp}")
    # 手工先补过说明的待归类卡：新说明覆盖旧的；没给新说明就原样保留。
    desc_at = next((i for i in range(cut + 1, len(lines))
                    if lines[i].strip() == "## 说明"), -1)
    body_lines = lines[cut:desc_at] if desc_at >= 0 else lines[cut:]
    if desc_at >= 0 and not str(description or "").strip():
        description = "\n".join(lines[desc_at + 1:]).strip()
    body = "\n".join(body_lines).rstrip()
    cooked = "\n".join(head) + "\n\n" + body
    if str(description or "").strip():
        cooked += "\n\n## 说明\n\n" + str(description).strip()
    cooked += "\n"

    stem = f"{mid}-{_slug(title)}"
    rel_new = f"{cat}/{stem}{SUFFIX}"
    for n in range(2, 100):
        if not (Path(root) / rel_new).exists():
            break
        rel_new = f"{cat}/{stem}-{n}{SUFFIX}"
    write_text(root, rel_new, cooked)
    try:
        target.unlink()
    except OSError as exc:
        # 删不掉旧文件就回滚新文件，绝不留下两张同 id 的卡。
        try:
            (Path(root) / rel_new).unlink()
        except OSError:
            pass
        raise MaterialError(f"待归类卡删不掉（可能被占用）：{exc}") from exc
    return {"ok": True, "exists": False, "path": rel_new,
            "material_id": mid, "category": cat, "raw_path": rel}


def intake(root: Path | str, *, excerpt: str, title: str = "",
           category: str = FALLBACK_CATEGORY,
           tags: str | Iterable[str] | None = None,
           description: str = "", source_line: str = "作者自述",
           source_ref: str = "", origin: str = "", session_id: str = "",
           instruction: str = "", material_id: str = "") -> dict[str, Any]:
    """收一张卡。同摘要同出处已在库里就返回原路径，不重复写。

    ``source_ref`` 只参与身份计算：来源行里加「第 3–9 段」这类给人看的
    定位时，身份仍然按「原文 + 书章块」算，不随定位写法漂移。
    """
    text = str(excerpt or "").strip()
    if not text:
        raise MaterialError("摘录不能为空")
    mid = str(material_id or "").strip() or excerpt_id(
        text, source_ref or source_line)
    existing = find_by_id(root, mid)
    if existing:
        return {"ok": True, "exists": True, "path": existing,
                "material_id": mid}
    cat = normalize_dir(category or FALLBACK_CATEGORY)
    target_dir = Path(root) / cat
    target_dir.mkdir(parents=True, exist_ok=True)
    body = card_markdown(
        material_id=mid, title=title or _first_line(text), excerpt=text,
        description=description, source_line=source_line, category=cat,
        tags=_normalize_tags(tags), origin=origin, session_id=session_id,
        instruction=instruction)
    rel = f"{cat}/{mid}-{_slug(title or _first_line(text))}{SUFFIX}"
    target = _resolve_rel(Path(root), rel)
    if target.exists():
        # 身份没命中但路径撞了：加序号，绝不覆盖别人的卡。
        rel = f"{cat}/{mid}-{_slug(title or _first_line(text))}-2{SUFFIX}"
    write_text(root, rel, body)
    return {"ok": True, "exists": False, "path": rel, "material_id": mid,
            "category": cat}


def intake_raw(root: Path | str, *, excerpt: str, book_title: str = "",
               source_line: str = "作者自述", location: str = "",
               category: str = FALLBACK_CATEGORY,
               tags: str | Iterable[str] | None = None,
               origin: str = "", session_id: str = "",
               instruction: str = "", material_id: str = "",
               when: datetime | None = None) -> dict[str, Any]:
    """收录一张**待归类**卡：文件名按「书名 + 时间戳」，不做加工。

    与 :func:`intake` 的区别只在落盘命名与去重：
      - ``intake`` 是加工后的卡，路径 ``<hash>-<标题>.md``，同原文同出处返回旧路径；
      - ``intake_raw`` 是阅读页随手收集的原料，``待归类/《书名》-YYYYMMDD-HHMMSS.md``，
        同一段文字在不同时间收两次就是两个文件（作者之后自己归并）。
    抬头仍然用 :func:`card_markdown`，库的解析/盘点/回收站照常工作。
    """
    text = str(excerpt or "").strip()
    if not text:
        raise MaterialError("摘录不能为空")
    cat = normalize_dir(category or FALLBACK_CATEGORY)
    book = str(book_title or "").strip() or "未命名书"
    if not book.startswith("《"):
        book = f"《{book}》"
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    stem = f"{_slug(book, limit=40)}-{stamp}"
    # 身份只用「原文 + 书/章/块」：CFI 这类坐标卡头存不下，audit 重算时
    # 会永远对不上；它改存到 `> 定位：` 行，方便回查但不参与身份。
    mid = str(material_id or "").strip() or excerpt_id(text, source_line)
    target_dir = Path(root) / cat
    target_dir.mkdir(parents=True, exist_ok=True)
    rel = f"{cat}/{stem}{SUFFIX}"
    for n in range(2, 100):
        if not (Path(root) / rel).exists():
            break
        rel = f"{cat}/{stem}-{n}{SUFFIX}"
    extra = [RAW_STATE]
    if str(location or "").strip():
        # CFI 只做定位线索，不参与身份（卡头存不下，audit 重算会错位）。
        extra.append(f"{LOCATION_MARK}{str(location).strip()}")
    body = card_markdown(
        material_id=mid, title=stem, excerpt=text,
        source_line=source_line, category=cat,
        tags=_normalize_tags(tags), origin=origin, session_id=session_id,
        instruction=instruction, header_extra=extra,
        collected_at=(when or datetime.now()).strftime("%Y-%m-%d %H:%M"))
    write_text(root, rel, body)
    return {"ok": True, "exists": False, "path": rel, "material_id": mid,
            "category": cat}


def save_web_cards(root: Path | str, query: str, cards: Sequence[Mapping[str, Any]],
                   *, category: str = FALLBACK_CATEGORY) -> dict[str, Any]:
    """联网卡落进全局库：一卡一文件，同 URL + 同引文去重。"""
    paths: list[str] = []
    for card in cards or []:
        excerpt = str(card.get("excerpt") or "").strip()
        if not excerpt:
            continue
        url = str(card.get("url") or "").strip()
        title = str(card.get("title") or "").strip() or query
        source_line = f"网页 · {url}" if url else "网页"
        row = intake(
            root, excerpt=excerpt, title=title, category=category,
            tags=[str(card.get("site_name") or ""), str(card.get("kind") or "")],
            description=str(card.get("summary") or "").strip(),
            source_line=source_line, origin=f"联网检索「{query}」")
        if not row.get("exists") or row.get("path") not in paths:
            paths.append(str(row.get("path") or ""))
    return {"saved": paths[0] if paths else "", "saved_paths": paths}


# ── 检索 ──────────────────────────────────────────────────────────────────

def _search_terms(query: str) -> list[str]:
    """先用 jieba 抽名词/动词；抽不出来再按空白切。"""
    text = str(query or "").strip()
    if not text:
        return []
    try:
        from writegraph.reference import query_terms
        terms = [t for t in query_terms(text, limit=8) if t]
    except Exception:                                # noqa: BLE001
        terms = []
    return terms or [t for t in re.split(r"\s+", text) if t]


def _best_line(text: str, terms: Sequence[str]) -> str:
    best = (-1, "")
    for line in str(text or "").splitlines():
        low = line.lower()
        hits = sum(1 for term in terms if term.lower() in low)
        if hits > best[0] and line.strip():
            best = (hits, line.strip()[:160])
    return best[1]


def search(root: Path | str, query: str, *, limit: int = 10) -> dict[str, Any]:
    """关键词 + 标签 + 分类检索；标题命中权重最高。"""
    terms = _search_terms(query)
    hits: list[dict[str, Any]] = []
    for path in _iter_files(root):
        rel = path.relative_to(Path(root)).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        card = parse_card(text, rel)
        title = card["title"] or _first_line(text)
        tags = " ".join(card["tags"])
        category = card["category"] or category_of(rel)
        score = 0
        matched: set[str] = set()
        low = text.lower()
        for term in terms:
            t = term.lower()
            if t in title.lower():
                score += 5
                matched.add(term)
            if t in tags.lower():
                score += 4
                matched.add(term)
            if t in category.lower():
                score += 2
                matched.add(term)
            if t in low:
                score += 1
                matched.add(term)
        if not matched:
            continue
        hits.append({
            "path": rel,
            "title": title,
            "material_id": card["material_id"],
            "category": category,
            "tags": card["tags"],
            "excerpt": (card["excerpt"] or _best_line(text, terms))[:400],
            "description": card["description"][:300],
            "source_line": card["source_line"],
            "score": score,
            "snippet": _best_line(text, terms),
        })
    hits.sort(key=lambda row: (-int(row["score"]), row["path"]))
    out = hits[:max(1, int(limit))]
    for row in out:
        row.pop("score", None)
    return {"query": str(query).strip(), "terms": terms, "hits": out,
            "total": len(hits), "truncated": len(hits) > len(out)}


# ── 书籍原文 ──────────────────────────────────────────────────────────────

def split_paragraphs(text: str) -> list[str]:
    """按空行切段（``\\r\\n\\r\\n`` 也算）；没有空行的块就是一段。"""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"\n[ \t]*\n+", raw)
    return [part.strip() for part in parts if part.strip()]


def paragraph_span(text: str, start: int | None = None,
                   end: int | None = None) -> tuple[str, int, int, int]:
    """取连续几段（1-based，含两端），返回 ``(摘录, 起, 止, 总段数)``。

    阅读工具按 chunk 取原文，但索引粒度可以更细：作者要点的是块里的
    那封信，不是整块连带的后文。段落序号由 ``split_paragraphs`` 定，
    ``read_chunk`` 会把编号摆给模型看，模型只负责抄数字。
    """
    paras = split_paragraphs(text)
    total = len(paras)
    if total == 0:
        raise MaterialError("这块没有正文")
    first = int(start or 0) or 1
    last = int(end or 0) or first
    if first < 1 or last < 1:
        raise MaterialError("段落序号从 1 开始")
    if first > total or last > total:
        raise MaterialError(f"这块只有 {total} 段，给的是第 {first}–{last} 段")
    if first > last:
        raise MaterialError(f"段落范围反了：第 {first}–{last} 段")
    return "\n\n".join(paras[first - 1:last]), first, last, total


def book_title(books_db: Path | str, book_id: str) -> str:
    """按书 id 查书名；查不到返回空串（调用方自己兜底）。"""
    path = Path(books_db)
    if not path.exists() or not book_id:
        return ""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT title FROM books WHERE id=?",
                           (str(book_id),)).fetchone()
        return str(row[0] or "") if row else ""
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def read_book_chunk(books_db: Path | str, *, chunk_id: str = "",
                    book_id: str = "", chapter_index: int | None = None,
                    chunk_index: int | None = None) -> dict[str, Any]:
    """读回逐字原文；找不到返回空 dict，由调用方退回传入的 excerpt。"""
    path = Path(books_db)
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        select = ("SELECT ck.id, ck.book_id, ck.chapter_index,"
                  " ck.chunk_index, ck.content, b.title AS book_title"
                  " FROM book_chunks ck LEFT JOIN books b ON b.id=ck.book_id")
        row = None
        if chunk_id:
            row = conn.execute(select + " WHERE ck.id=?",
                               (str(chunk_id),)).fetchone()
        elif book_id and chapter_index is not None and chunk_index is not None:
            row = conn.execute(
                select + " WHERE ck.book_id=? AND ck.chapter_index=?"
                " AND ck.chunk_index=?",
                (str(book_id), int(chapter_index),
                 int(chunk_index))).fetchone()
        return dict(row) if row is not None else {}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


__all__ = [
    "LIBRARY_DIR", "RECYCLE_DIR", "RECYCLE_RETENTION_DAYS", "DEFAULT_ROOTS",
    "FALLBACK_CATEGORY", "MaterialError", "library_root", "ensure_library",
    "normalize", "normalize_dir", "resolve", "resolve_dir", "excerpt_id",
    "card_markdown", "parse_card", "list_cards", "list_dirs", "read_card",
    "write_text", "create_file", "create_dir", "rename_entry", "move_entry",
    "recycle", "empty_dir", "list_recycled", "restore", "purge_recycled",
    "recycled_summary", "find_by_id", "category_of", "intake", "intake_raw",
    "is_raw_card", "pending_cards", "cook", "RAW_STATE", "COOK_MARK",
    "save_web_cards", "search", "book_title", "read_book_chunk",
    "split_paragraphs",
    "paragraph_span", "audit",
]
