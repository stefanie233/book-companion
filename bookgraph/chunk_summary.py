"""Chunk summaries — 「这一块在说什么」的一句话，随提取一起产出。

**为什么是提取器顺手做，而不是单独一趟。** 提取器已经读完这一批的每一块，
并且必须为每条断言找逐字 evidence —— 它对「这一块讲了什么」的把握是现成的。
再开一次调用等于让另一个模型把同样的正文再读一遍，换来的只有一致性下降。

**为什么存在 books.db 而不是 graph.db。** 简述属于「书」这一层：没有图谱的
书也可以有（补录脚本），消费方（library_search / 当前段落 / 历史压缩）全在
book layer。放 graph.db 会让每次读都要跨库 join，而两库的钟不是同一个。

输出形状宽容（与 llm_json 同一思路）：模型可能给 dict、也可能给 list；
键可能叫 chunk_id，也可能叫 id。**不知道的值原样丢弃并计数**，不猜。
"""

from __future__ import annotations

import re
from typing import Any

#: 一句话的上限。再长就不是「简述」而是原文的压缩版 —— 那样还不如直接读原文。
SUMMARY_MAX_CHARS = 120

_WS = re.compile(r"\s+")

#: 模型对 chunk_id 的两种走形：给真 id 加前缀（``chunk_<真id>``），或干脆
#: 按批次序号重写（``chunk_001`` / ``1``）。实测补录三本书，后者占失败批次的
#: 绝大多数 —— 提示词里的 JSON 示例写着 ``chunk_001``，模型就照着造。
_ORDINAL = re.compile(r"(?:chunk[_\-]?)?(\d{1,4})", re.IGNORECASE)
_CHUNK_PREFIXES = ("chunk_", "chunk-")


def _clean(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    text = _WS.sub(" ", text).strip()
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS].rstrip() + "…"
    return text


def normalize_chunk_summaries(
    raw: Any,
    valid_ids: list[str] | set[str] | None = None,
) -> dict[str, str]:
    """把模型输出归一成 ``{chunk_id: 一句话}``。

    *valid_ids* 给了就只保留本批的块 —— 模型偶尔会把别的块号写进来
    （尤其是回炉轮，前一轮的正文还留在上下文里）。
    """
    # 序号修复只对**有序**输入成立：set 的迭代顺序是任意的，拿它按序号
    # 对位会把简述安到错误的块上（比丢掉更糟）。宁可丢掉。
    ordered = isinstance(valid_ids, (list, tuple))
    order = list(valid_ids) if valid_ids is not None else []
    allowed = set(order) if valid_ids is not None else None
    out: dict[str, str] = {}

    def _resolve(cid: str) -> str | None:
        """把模型写的键还原成真实 chunk_id；认不出就返回 None。"""
        if allowed is None or cid in allowed:
            return cid
        for prefix in _CHUNK_PREFIXES:
            if cid.startswith(prefix) and cid[len(prefix):] in allowed:
                return cid[len(prefix):]
        m = _ORDINAL.fullmatch(cid)
        if m and ordered:
            n = int(m.group(1))
            if 1 <= n <= len(order):
                return order[n - 1]
        return None

    def _put(cid: Any, text: Any) -> None:
        raw_id = str(cid or "").strip()
        if not raw_id:
            return
        real_id = _resolve(raw_id)
        if real_id is None:
            return
        clean = _clean(text)
        if clean:
            out[real_id] = clean

    # 模型常把「裸 dict」再包一层：{"chunk_summaries": {...}}。键名不是合法
    # chunk_id，照直处理会被**静默丢光**（实测补录脚本首趟写入 0 条）。
    if isinstance(raw, dict):
        for wrap in ("chunk_summaries", "summaries"):
            inner = raw.get(wrap)
            if isinstance(inner, (dict, list)) and len(raw) <= 2:
                raw = inner
                break

    if isinstance(raw, dict):
        for cid, text in raw.items():
            if isinstance(text, dict):
                _put(cid, text.get("summary") or text.get("text")
                     or text.get("content"))
            else:
                _put(cid, text)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            _put(item.get("chunk_id") or item.get("id") or item.get("chunk"),
                 item.get("summary") or item.get("text") or item.get("content"))
    return out


def summaries_from_result(
    result: dict | None,
    valid_ids: list[str] | set[str] | None = None,
) -> dict[str, str]:
    """从 Explorer 结果里取简述。

    只认 ``chunk_summaries`` / ``summaries``（复数）。**不认 ``summary``** ——
    那个字段是「本批完成情况」的台账行（"本批 5 块完成。新建 X 实体…"），
    拿它当块简述会得到一批一模一样的废话。
    """
    if not isinstance(result, dict):
        return {}
    raw = result.get("chunk_summaries")
    if raw is None:
        raw = result.get("summaries")
    return normalize_chunk_summaries(raw, valid_ids)


def missing_ids(chunk_ids: list[str], summaries: dict[str, str]) -> list[str]:
    return [cid for cid in chunk_ids if cid not in summaries]
