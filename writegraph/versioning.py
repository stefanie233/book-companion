"""写作 git 的纯函数：树/账本的规范化、指纹、差异。

存储层（``WriteGraphDB``）只负责读写行；这里不碰连接。所有差异都以
「章节版本指针」为单位——正文只存在 ``chapter_versions`` 里，提交不复制。
"""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence


TREE_FIELDS = ("chapter_id", "chapter_index", "title", "status",
               "volume_id", "version_id", "version", "content_hash",
               "word_count")


def canonical(value: Any) -> str:
    """稳定序列化：键排序、无空格、保留中文。指纹必须可复现。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def normalize_tree(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """只保留树字段并按 chapter_id 排序——章节顺序变化不算改动。"""
    out = []
    for e in entries:
        out.append({k: e.get(k) for k in TREE_FIELDS})
    out.sort(key=lambda x: str(x.get("chapter_id") or ""))
    return out


def tree_hash(entries: Iterable[Mapping[str, Any]]) -> str:
    return digest(normalize_tree(entries))


def ledger_hash(ledger: Mapping[str, Any]) -> str:
    return digest(ledger)


def short_id(commit_id: str) -> str:
    """``cm-1a2b3c4d5e6f`` → ``1a2b3c4d``。界面只展示这一段。"""
    text = str(commit_id or "")
    return text[-8:] if len(text) > 8 else text


def diff_trees(from_entries: Iterable[Mapping[str, Any]],
               to_entries: Iterable[Mapping[str, Any]]) -> dict[str, list]:
    """按 chapter_id 比较两棵树。正文相同但标题/状态变了也算 changed。"""
    before = {str(e.get("chapter_id")): dict(e) for e in from_entries}
    after = {str(e.get("chapter_id")): dict(e) for e in to_entries}
    added, removed, changed = [], [], []
    for cid, entry in after.items():
        if cid not in before:
            added.append(entry)
            continue
        old = before[cid]
        fields = [f for f in TREE_FIELDS
                  if f not in ("version_id", "version")
                  and old.get(f) != entry.get(f)]
        if old.get("content_hash") != entry.get("content_hash"):
            fields.append("content")
        if fields:
            changed.append({"chapter_id": cid,
                            "chapter_index": entry.get("chapter_index"),
                            "title": entry.get("title") or old.get("title") or "",
                            "fields": sorted(set(fields)),
                            "before": old, "after": entry})
    for cid, entry in before.items():
        if cid not in after:
            removed.append(entry)
    for rows in (added, removed, changed):
        rows.sort(key=lambda x: int(x.get("chapter_index") or 0))
    return {"added": added, "removed": removed, "changed": changed}


def unified_text_diff(old: str, new: str, *,
                      from_label: str = "before",
                      to_label: str = "after",
                      context: int = 3) -> str:
    old_lines = (old or "").splitlines()
    new_lines = (new or "").splitlines()
    out = difflib.unified_diff(old_lines, new_lines,
                               fromfile=from_label, tofile=to_label,
                               lineterm="", n=context)
    return "\n".join(out)


# ── 条目比较：判断两棵树里的同一章是否「等价」────────────────────────────
# version_id / version 是存储细节：同一段正文重新保存会产生新版本号，
# 内容没变就不该算改动。其余字段全比。

ENTRY_FIELDS = ("chapter_index", "title", "status", "volume_id",
                "content_hash", "word_count")


def entry_key(entry: Mapping[str, Any] | None) -> tuple | None:
    if entry is None:
        return None
    return tuple(entry.get(f) for f in ENTRY_FIELDS)


def _change_hunks(base: Sequence[str],
                  side: Sequence[str]) -> list[tuple[int, int, list[str]]]:
    """side 相对 base 的改动区间：(base 起点, base 终点, 替换成哪些行)。"""
    matcher = difflib.SequenceMatcher(None, list(base), list(side),
                                      autojunk=False)
    hunks: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append((i1, i2, list(side[j1:j2])))
    return hunks


def _overlaps(a: tuple[int, int, list[str]],
              b: tuple[int, int, list[str]]) -> bool:
    """改动区间是否重叠。两处同点插入也算冲突（谁先谁后没有答案）。"""
    a1, a2 = a[0], a[1]
    b1, b2 = b[0], b[1]
    if a1 == a2 and b1 == b2:
        return True
    return a1 < b2 and b1 < a2


def merge3_text(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """正文的三路合并：两侧改动的行区间不重叠就自动合，重叠判冲突。

    返回 ``(文本, 是否成功)``；失败时文本为空，调用方按冲突处理。
    相同改动（两侧都加同一句）只保留一份，不算冲突。
    """
    if ours == theirs:
        return ours, True
    if ours == base:
        return theirs, True
    if theirs == base:
        return ours, True

    b = (base or "").splitlines()
    ho = _change_hunks(b, (ours or "").splitlines())
    ht = _change_hunks(b, (theirs or "").splitlines())
    combined = list(ho)
    for hunk in ht:
        if hunk not in combined:
            combined.append(hunk)
    combined.sort(key=lambda h: (h[0], h[1]))

    for prev, cur in zip(combined, combined[1:]):
        if _overlaps(prev, cur):
            return "", False

    out: list[str] = []
    pos = 0
    for start, end, replacement in combined:
        if start < pos:
            return "", False
        out.extend(b[pos:start])
        out.extend(replacement)
        pos = end
    out.extend(b[pos:])
    return "\n".join(out), True


# ── 提交图：祖先、合并基、领先/落后 ──────────────────────────────────────

def ancestor_ids(commits: Mapping[str, Mapping[str, Any]],
                 start: str | None) -> set[str]:
    """从 start 沿两个父节点向上可达的提交集合（含 start）。"""
    seen: set[str] = set()
    stack = [start] if start else []
    while stack:
        cid = stack.pop()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        row = commits.get(cid) or {}
        for parent in (row.get("parent_id"), row.get("parent2_id")):
            if parent and parent not in seen:
                stack.append(str(parent))
    return seen


def merge_base(commits: Mapping[str, Mapping[str, Any]],
               a: str | None, b: str | None) -> str | None:
    """最佳共同祖先：共同祖先里再没有别的共同祖先的那个。"""
    if not a or not b:
        return None
    left = ancestor_ids(commits, a)
    right = ancestor_ids(commits, b)
    common = left & right
    if not common:
        return None
    reach = {cid: ancestor_ids(commits, cid) for cid in common}
    best = [cid for cid in common
            if not any(cid in reach[other]
                       for other in common if other != cid)]
    if not best:
        best = list(common)
    best.sort(key=lambda cid: (commits.get(cid, {}).get("time_created") or 0),
              reverse=True)
    return best[0]


def ahead_behind(commits: Mapping[str, Mapping[str, Any]],
                 head: str | None, other: str | None) -> tuple[int, int]:
    """``(other 领先 head, head 领先 other)``，等价于 git 的 ``--left-right``。"""
    if not head or not other:
        return (0, 0)
    mine = ancestor_ids(commits, head)
    theirs = ancestor_ids(commits, other)
    return (len(theirs - mine), len(mine - theirs))


LEDGER_KEYS = {
    "contracts": "chapter_id",
    "facts": "id",
    "promises": "id",
    "promise_edges": "id",
}


def _ledger_label(collection: str, row: Mapping[str, Any]) -> str:
    if collection == "contracts":
        return str(row.get("purpose") or row.get("chapter_id") or "")
    if collection == "facts":
        return f"{row.get('subject_ref') or ''} · {row.get('field') or ''}"
    if collection == "promises":
        return str(row.get("title") or row.get("id") or "")
    return f"{row.get('src_id') or ''} → {row.get('dst_id') or ''}"


ledger_label = _ledger_label


def diff_ledger(from_ledger: Mapping[str, Any],
                to_ledger: Mapping[str, Any]) -> dict[str, dict]:
    """四组账本表的增删改。值比较用完整行——账本只追加/关区间，不更新历史。"""
    result: dict[str, dict] = {}
    for collection, key in LEDGER_KEYS.items():
        before = {str(r.get(key)): dict(r)
                  for r in (from_ledger.get(collection) or [])}
        after = {str(r.get(key)): dict(r)
                 for r in (to_ledger.get(collection) or [])}
        added = [{"id": k, "label": _ledger_label(collection, r)}
                 for k, r in after.items() if k not in before]
        removed = [{"id": k, "label": _ledger_label(collection, r)}
                   for k, r in before.items() if k not in after]
        changed = [{"id": k, "label": _ledger_label(collection, after[k])}
                   for k in after if k in before and after[k] != before[k]]
        result[collection] = {"added": added, "removed": removed,
                              "changed": changed}
    return result
