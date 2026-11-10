"""写作记忆的写入闸门 —— 只允许记忆维护器碰 作品记忆.md 与 设定/。

第二阶段若让 agent 直接编辑记忆目录，只能靠提示词与守卫约束；我们把它收成
可机械校验的 ops：模型只返回 JSON，路径、大小、可写范围在这里判定。判不过
整批拒绝，不做部分接受。
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from writegraph import docs as docs_mod

#: 近重复判定：字符 3-gram Jaccard。只报告，不自动删。
NEAR_DUPLICATE_RATIO = 0.85
#: audit 输出里每类最多列几组，避免把工具结果撑爆。
AUDIT_LIMIT = 20

_WS = re.compile(r"[ \t　]+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*")


BIBLE_MAX_CHARS = 3000
SETTING_MAX_CHARS = 65536
SETTINGS_MAX_FILES = 200
BIBLE_SECTIONS = ("这本书是什么", "写作约定", "忌讳")


# ── 去重与审计（零 LLM）────────────────────────────────────────────────

def normalize_text(text: Any) -> str:
    """统一换行/空白/NFC；只用于比较，不改磁盘上的原文。"""
    value = str(text or "").replace("﻿", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", value).strip()


def content_hash(text: Any) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


def note_hash(target: str, section: str, text: str, *,
              evidence: str = "", old: str = "") -> str:
    """一条 note 的幂等键；同内容同出处只留一条 pending。"""
    raw = "\n".join([str(target or ""), str(section or ""),
                     normalize_text(text), normalize_text(evidence),
                     normalize_text(old)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _normalized_line(line: str) -> str:
    line = _BULLET.sub("", line).strip()
    return _WS.sub(" ", line)


def _normalized_grams(text: str) -> set[str]:
    clean = re.sub(r"[#>*`\-\s:：，。！？、；“”‘’（）()《》…,.!?;]+", "",
                   normalize_text(text))
    return {clean[i:i + 3] for i in range(max(0, len(clean) - 2))}


def append_deduped(existing: str, incoming: str) -> tuple[str, int]:
    """按行做逐字去重；返回（真正要追加的片段，跳过的重复行数）。

    只跳过完全相同的归一化行，不做近重复删除——近重复留给 audit/作者。
    返回片段而不是整份全文，调用方才能用 append 写，不重写原文件。
    """
    old = normalize_text(existing)
    if not old:
        return normalize_text(incoming) + (
            "\n" if normalize_text(incoming) else ""), 0
    seen = {_normalized_line(line) for line in old.splitlines()
            if line.strip()}
    keep: list[str] = []
    skipped = 0
    for line in normalize_text(incoming).splitlines():
        if not line.strip():
            keep.append("")
            continue
        norm = _normalized_line(line)
        if norm in seen:
            skipped += 1
            continue
        seen.add(norm)
        keep.append(line)
    body = "\n".join(keep).strip()
    return (body + "\n" if body else ""), skipped


def snapshot(root: Path) -> dict[str, str]:
    """记忆相关文件 → 原文；作为下一次 consolidate 的 diff 基线。"""
    out: dict[str, str] = {}
    paths = [docs_mod.BIBLE_NAME]
    paths += [row["path"] for row in
              docs_mod.section_files(root, docs_mod.DOCS_DIR)]
    for path in paths:
        try:
            out[path] = docs_mod.read_file(root, path)["content"]
        except docs_mod.DocError:
            continue
    return out


def diff_snapshot(old: Mapping[str, str], new: Mapping[str, str], *,
                  max_chars: int = 12000) -> str:
    """路径级 unified diff；旧基线没有该文件时视为新增。"""
    chunks: list[str] = []
    for path in sorted(set(old) | set(new)):
        before = str(old.get(path) or "")
        after = str(new.get(path) or "")
        if before == after:
            continue
        if not before:
            chunks.append(f"### {path}（新增）\n{after.strip()[:3000]}")
            continue
        if not after:
            chunks.append(f"### {path}（删除）")
            continue
        diff = list(difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=path, tofile=path, lineterm=""))
        chunks.append("\n".join(diff))
        if sum(len(c) for c in chunks) > max_chars:
            chunks.append("…（diff 已截断）")
            break
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n…（diff 已截断）"
    return text


def _section_span(text: str, section: str) -> tuple[int, int] | None:
    """返回 ``## section`` 正文的行区间 [start, end)；找不到返回 None。"""
    lines = text.splitlines()
    name = str(section or "").strip()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## ") and stripped[3:].strip() == name:
            end = len(lines)
            for j in range(idx + 1, len(lines)):
                if lines[j].strip().startswith("## "):
                    end = j
                    break
            return idx + 1, end
    return None


def _append_section(text: str, section: str, incoming: str) -> tuple[str, int]:
    lines = text.splitlines()
    span = _section_span(text, section)
    if span is None:
        raise ValueError(f"找不到小节：{section}")
    start, end = span
    raw_body = "\n".join(lines[start:end])
    body = raw_body.strip()
    addition, skipped = append_deduped(body, incoming)
    if not addition.strip():
        return text, skipped
    prefix = raw_body.rstrip()
    new_body = ((prefix + "\n\n") if prefix.strip() else "\n") \
        + addition.strip()
    new_lines = lines[:start] + new_body.splitlines() + lines[end:]
    return "\n".join(new_lines).rstrip() + "\n", skipped


def _replace_section(text: str, section: str, content: str) -> str:
    lines = text.splitlines()
    span = _section_span(text, section)
    if span is None:
        raise ValueError(f"找不到小节：{section}")
    start, end = span
    body = str(content or "").strip()
    new_lines = lines[:start] + (body.splitlines() if body else []) + lines[end:]
    return "\n".join(new_lines).rstrip() + "\n"


def _delete_bullet(text: str, section: str, target: str) -> tuple[str, int]:
    lines = text.splitlines()
    span = _section_span(text, section)
    if span is None:
        raise ValueError(f"找不到小节：{section}")
    start, end = span
    wanted = _normalized_line(target)
    kept: list[str] = []
    removed = 0
    for idx, line in enumerate(lines):
        if start <= idx < end and line.strip().startswith(("- ", "* ")) \
                and _normalized_line(line) == wanted:
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept).rstrip() + "\n", removed


def ensure_bible_schema(text: str) -> str:
    """补齐作品记忆的三个固定小节；缺节时机械重建，不丢已有正文。"""
    lines = normalize_text(text).splitlines()
    for section in BIBLE_SECTIONS:
        if any(line.strip() == f"## {section}" for line in lines):
            continue
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"## {section}", ""])
    return "\n".join(lines).rstrip() + "\n"


def find_content_duplicates(root: Path, text: str, *,
                            exclude: str = "") -> list[str]:
    """同内容 hash 的设定文件；返回路径列表（不含 exclude）。"""
    digest = content_hash(text)
    hits: list[str] = []
    for row in docs_mod.section_files(root, docs_mod.DOCS_DIR):
        path = str(row.get("path") or "")
        if path == exclude:
            continue
        try:
            current = docs_mod.read_file(root, path)["content"]
        except docs_mod.DocError:
            continue
        if content_hash(current) == digest:
            hits.append(path)
    return hits


def _bible_duplicate_bullets(root: Path) -> list[list[str]]:
    """作品记忆里归一化后重复的 bullet；返回重复行与出现的次数。"""
    try:
        text = docs_mod.read_bible(root, max_chars=1_000_000)
    except OSError:
        return []
    seen: dict[str, int] = {}
    examples: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip().startswith(("- ", "* ")):
            continue
        norm = _normalized_line(line)
        if not norm:
            continue
        seen[norm] = seen.get(norm, 0) + 1
        examples.setdefault(norm, line.strip())
    return [[examples[k], n] for k, n in seen.items() if n > 1][:AUDIT_LIMIT]


def audit_workspace(root: Path) -> dict[str, Any]:
    """机械审计：只报告，不删/不合。"""
    rows = docs_mod.section_files(root, docs_mod.DOCS_DIR)
    texts: list[tuple[str, str]] = []
    for row in rows:
        path = str(row.get("path") or "")
        try:
            texts.append((path, docs_mod.read_file(root, path)["content"]))
        except docs_mod.DocError:
            continue

    by_hash: dict[str, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    for path, text in texts:
        by_hash.setdefault(content_hash(text), []).append(path)
        by_stem.setdefault(Path(path).stem.strip(), []).append(path)
    exact = [sorted(paths) for paths in by_hash.values() if len(paths) > 1]
    same_subject = [sorted(paths) for paths in by_stem.values()
                    if len(paths) > 1]

    near: list[dict[str, Any]] = []
    grams = {path: _normalized_grams(text) for path, text in texts}
    for i, (pa, ga) in enumerate(grams.items()):
        for pb, gb in list(grams.items())[i + 1:]:
            if not ga or not gb:
                continue
            ratio = len(ga & gb) / len(ga | gb)
            if ratio >= NEAR_DUPLICATE_RATIO:
                near.append({"a": pa, "b": pb, "ratio": round(ratio, 3)})
    near.sort(key=lambda item: (-item["ratio"], item["a"], item["b"]))

    empty = [path for path, text in texts
             if len(_normalized_line(text)) <= 80
             and len(normalize_text(text).splitlines()) <= 2]
    bible_dups = _bible_duplicate_bullets(root)

    return {
        "files": len(texts),
        "exact_duplicates": exact[:AUDIT_LIMIT],
        "near_duplicates": near[:AUDIT_LIMIT],
        "same_subject": same_subject[:AUDIT_LIMIT],
        "bible_duplicates": bible_dups,
        "empty_or_orphan": empty[:AUDIT_LIMIT],
        "counts": {
            "exact_duplicates": len(exact),
            "near_duplicates": len(near),
            "same_subject": len(same_subject),
            "bible_duplicates": len(bible_dups),
            "empty_or_orphan": len(empty),
        },
    }


@dataclass
class CheckedOps:
    ok: bool
    errors: list[str] = field(default_factory=list)
    bible: str = ""
    bible_ops: list[dict[str, str]] = field(default_factory=list)
    writes: list[dict[str, str]] = field(default_factory=list)
    appends: list[dict[str, str]] = field(default_factory=list)
    patches: list[dict[str, str]] = field(default_factory=list)
    merges: list[dict[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)


def validate_ops(ops: Mapping[str, Any], root: Path, *,
                 bible_max: int = BIBLE_MAX_CHARS,
                 setting_max: int = SETTING_MAX_CHARS,
                 max_files: int = SETTINGS_MAX_FILES) -> CheckedOps:
    """把模型返回的 ops 收成可执行集合；有一条不合格就整批拒绝。"""
    errors: list[str] = []
    bible = str(ops.get("bible") or "")
    if bible.strip():
        if len(bible) > bible_max:
            errors.append(f"作品记忆超过 {bible_max} 字（现在 {len(bible)} 字）")
        if not bible.lstrip().startswith("#"):
            errors.append("作品记忆必须以 Markdown 标题开头")

    bible_ops: list[dict[str, str]] = []
    for item in ops.get("bible_ops") or []:
        if not isinstance(item, Mapping):
            errors.append("bible_ops 里有非对象")
            continue
        op = str(item.get("op") or "").strip()
        section = str(item.get("section") or "").strip()
        content = str(item.get("content") or "")
        target = str(item.get("text") or "")
        if op not in ("append_section", "replace_section", "delete_bullet"):
            errors.append(f"bible_ops 未知 op：{op}")
            continue
        if section not in ("这本书是什么", "写作约定", "忌讳"):
            errors.append(f"bible_ops 未知小节：{section or '(空)'}")
            continue
        if op == "delete_bullet":
            if not target.strip():
                errors.append(f"bible_ops delete_bullet 缺 text：{section}")
                continue
        elif not content.strip():
            errors.append(f"bible_ops 内容为空：{section}")
            continue
        bible_ops.append({"op": op, "section": section,
                          "content": content, "text": target})
    if bible.strip() and bible_ops:
        errors.append("bible 与 bible_ops 不能同时给")

    current = {row["path"] for row in
               docs_mod.section_files(root, docs_mod.DOCS_DIR)}
    contents: dict[str, str] = {}
    for path in current:
        try:
            contents[path] = docs_mod.read_file(root, path)["content"]
        except docs_mod.DocError:
            contents[path] = ""
    writes: list[dict[str, str]] = []
    seen: set[str] = set()
    write_paths: set[str] = set()
    write_hashes: dict[str, str] = {
        content_hash(text): path for path, text in contents.items()}
    for item in ops.get("writes") or []:
        if not isinstance(item, Mapping):
            errors.append("writes 里有非对象")
            continue
        path = str(item.get("path") or "").strip()
        content = str(item.get("content") or "")
        if not path.startswith(docs_mod.DOCS_DIR + "/"):
            errors.append(f"只能写 {docs_mod.DOCS_DIR}/：{path or '(空)'}")
            continue
        if not content.strip():
            errors.append(f"内容为空：{path}")
            continue
        if len(content) > setting_max:
            errors.append(f"文件太大：{path}")
            continue
        if path in seen:
            errors.append(f"同一路径写了两次：{path}")
            continue
        digest = content_hash(content)
        other = write_hashes.get(digest)
        if other and other != path:
            errors.append(f"内容与 {other} 重复：{path}；先合并")
            continue
        seen.add(path)
        write_paths.add(path)
        write_hashes[digest] = path
        writes.append({"path": path, "content": content})
    if len(current | seen) > max_files:
        errors.append(f"设定文件超过 {max_files} 份")

    appends: list[dict[str, str]] = []
    for item in ops.get("appends") or []:
        if not isinstance(item, Mapping):
            errors.append("appends 里有非对象")
            continue
        path = str(item.get("path") or "").strip()
        section = str(item.get("section") or "").strip()
        content = str(item.get("content") or "")
        if not path.startswith(docs_mod.DOCS_DIR + "/"):
            errors.append(f"append 只能写 {docs_mod.DOCS_DIR}/：{path}")
            continue
        if path in write_paths:
            errors.append(f"同一路径不能既 write 又 append：{path}")
            continue
        if not content.strip():
            errors.append(f"append 内容为空：{path}")
            continue
        if path not in current:
            errors.append(f"append 目标不存在：{path}")
            continue
        if section and _section_span(contents.get(path, ""), section) is None:
            errors.append(f"append 小节不存在：{path} / {section}")
            continue
        appends.append({"path": path, "section": section,
                        "content": content})

    patches: list[dict[str, str]] = []
    for item in ops.get("patches") or []:
        if not isinstance(item, Mapping):
            errors.append("patches 里有非对象")
            continue
        path = str(item.get("path") or "").strip()
        old = str(item.get("old") or "")
        new = str(item.get("new") or "")
        if not path.startswith(docs_mod.DOCS_DIR + "/"):
            errors.append(f"patch 只能写 {docs_mod.DOCS_DIR}/：{path}")
            continue
        if path in write_paths or any(a["path"] == path for a in appends):
            errors.append(f"同一路径不能既 write/append 又 patch：{path}")
            continue
        if path not in current:
            errors.append(f"patch 目标不存在：{path}")
            continue
        if not old.strip():
            errors.append(f"patch 缺 old：{path}")
            continue
        if contents.get(path, "").count(old) != 1:
            errors.append(f"patch 的 old 必须唯一出现：{path}")
            continue
        patches.append({"path": path, "old": old, "new": new})

    merges: list[dict[str, str]] = []
    for item in ops.get("merges") or []:
        if not isinstance(item, Mapping):
            errors.append("merges 里有非对象")
            continue
        source = str(item.get("from") or "").strip()
        target = str(item.get("into") or "").strip()
        if not source.startswith(docs_mod.DOCS_DIR + "/") \
                or not target.startswith(docs_mod.DOCS_DIR + "/"):
            errors.append(f"merge 只能操作 {docs_mod.DOCS_DIR}/")
            continue
        if source == target:
            errors.append("merge 的 from 与 into 相同")
            continue
        if source not in current:
            errors.append(f"merge 源不存在：{source}")
            continue
        if target not in current and target not in write_paths:
            errors.append(f"merge 目标不存在：{target}")
            continue
        if source in write_paths:
            errors.append(f"merge 源不能同批 write：{source}")
            continue
        if source in {a["path"] for a in appends} \
                or source in {p["path"] for p in patches}:
            errors.append(f"merge 源不能同批 append/patch：{source}")
            continue
        merges.append({"from": source, "into": target})

    deletes: list[str] = []
    for raw in ops.get("deletes") or []:
        path = str(raw.get("path") if isinstance(raw, Mapping)
                   else raw or "").strip()
        if not path.startswith(docs_mod.DOCS_DIR + "/"):
            errors.append(f"只能删 {docs_mod.DOCS_DIR}/：{path or '(空)'}")
            continue
        if path in write_paths:
            errors.append(f"同一路径不能既 write 又 delete：{path}")
            continue
        if path not in deletes:
            deletes.append(path)
    merged_from = {m["from"] for m in merges}
    if set(deletes) & merged_from:
        errors.append("同一文件不能既 merge 又 delete")
    touched = ({a["path"] for a in appends}
               | {p["path"] for p in patches}
               | {m["into"] for m in merges})
    if set(deletes) & touched:
        errors.append("同一文件不能既 append/patch/merge 目标又 delete")
    return CheckedOps(ok=not errors, errors=errors, bible=bible.strip(),
                      bible_ops=bible_ops, writes=writes, appends=appends,
                      patches=patches, merges=merges, deletes=deletes)


def apply_ops(root: Path, ops: CheckedOps) -> dict[str, Any]:
    """按校验过的集合落盘。作品记忆没给就保持原样；删不存在的文件不算错。"""
    applied: dict[str, Any] = {
        "bible": False, "bible_ops": [], "writes": [], "appends": [],
        "patches": [], "merges": [], "deletes": [],
        "skipped_duplicates": 0, "errors": []}
    if ops.bible:
        docs_mod.write_file(root, docs_mod.BIBLE_NAME,
                            ensure_bible_schema(ops.bible))
        applied["bible"] = True
    elif ops.bible_ops:
        try:
            text = docs_mod.read_file(root, docs_mod.BIBLE_NAME)["content"]
        except docs_mod.DocError:
            text = ""
        text = ensure_bible_schema(text)
        for item in ops.bible_ops:
            try:
                if item["op"] == "append_section":
                    text, skipped = _append_section(
                        text, item["section"], item["content"])
                    applied["skipped_duplicates"] += skipped
                elif item["op"] == "replace_section":
                    text = _replace_section(
                        text, item["section"], item["content"])
                else:
                    text, removed = _delete_bullet(
                        text, item["section"], item["text"])
                    applied["skipped_duplicates"] += removed
            except ValueError as exc:
                applied["errors"].append(str(exc))
                continue
            applied["bible_ops"].append(item["op"])
        docs_mod.write_file(root, docs_mod.BIBLE_NAME, text)
        applied["bible"] = True
    for item in ops.writes:
        docs_mod.write_file(root, item["path"], item["content"])
        applied["writes"].append(item["path"])
    for item in ops.appends:
        try:
            existing = docs_mod.read_file(root, item["path"])["content"]
        except docs_mod.DocError:
            applied["errors"].append(f"append 目标不存在：{item['path']}")
            continue
        if item.get("section"):
            try:
                text, skipped = _append_section(
                    existing, item["section"], item["content"])
            except ValueError as exc:
                applied["errors"].append(str(exc))
                continue
            docs_mod.write_file(root, item["path"], text)
            applied["skipped_duplicates"] += skipped
        else:
            addition, skipped = append_deduped(existing, item["content"])
            applied["skipped_duplicates"] += skipped
            if addition.strip():
                docs_mod.write_file(root, item["path"], addition, append=True)
        applied["appends"].append(item["path"])
    for item in ops.patches:
        try:
            text = docs_mod.read_file(root, item["path"])["content"]
        except docs_mod.DocError:
            applied["errors"].append(f"patch 目标不存在：{item['path']}")
            continue
        if text.count(item["old"]) != 1:
            applied["errors"].append(
                f"patch 的 old 不再唯一：{item['path']}")
            continue
        docs_mod.write_file(root, item["path"],
                            text.replace(item["old"], item["new"], 1))
        applied["patches"].append(item["path"])
    for item in ops.merges:
        try:
            target = docs_mod.read_file(root, item["into"])["content"]
            source = docs_mod.read_file(root, item["from"])["content"]
        except docs_mod.DocError as exc:
            applied["errors"].append(f"merge 读取失败：{exc}")
            continue
        addition, skipped = append_deduped(target, source)
        applied["skipped_duplicates"] += skipped
        if addition.strip():
            docs_mod.write_file(root, item["into"], addition, append=True)
        try:
            docs_mod.delete_file(root, item["from"])
        except docs_mod.DocError as exc:
            applied["errors"].append(f"merge 源删除失败：{exc}")
            continue
        applied["merges"].append({"from": item["from"],
                                  "into": item["into"]})
    for path in ops.deletes:
        try:
            docs_mod.delete_file(root, path)
        except docs_mod.DocError:
            continue
        applied["deletes"].append(path)
    try:
        bible_text = docs_mod.read_file(
            root, docs_mod.BIBLE_NAME)["content"]
        fixed = ensure_bible_schema(bible_text)
        if fixed != bible_text:
            docs_mod.write_file(root, docs_mod.BIBLE_NAME, fixed)
            applied["bible"] = True
    except docs_mod.DocError:
        pass
    return applied


def manifest(root: Path) -> dict[str, str]:
    """记忆相关文件 → 内容 hash（用于发现作者改动/删除）。"""
    out: dict[str, str] = {}
    paths = [docs_mod.BIBLE_NAME]
    paths += [row["path"] for row in
              docs_mod.section_files(root, docs_mod.DOCS_DIR)]
    for path in paths:
        try:
            text = docs_mod.read_file(root, path)["content"]
        except docs_mod.DocError:
            continue
        out[path] = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return out


def diff_manifest(old: Mapping[str, str],
                  new: Mapping[str, str]) -> dict[str, list[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    return {"added": added, "removed": removed, "changed": changed}
