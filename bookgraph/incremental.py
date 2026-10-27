"""Incremental observation extraction from dialogue.

Extracts observations from conversation messages and writes them to graph.db.
This is the "evolution" mechanism - the graph grows as the user discusses
the book with the agent.

Trigger points:
  - on_pre_compress: before context compression discards old messages
  - on_session_end: fallback if no compaction occurred
  - (Web mode) after chat completion

Unlike Scout/Explorer (batch extraction from book text), this module
extracts from *dialogue* - the agent's discussion with the user about
the book. Observations are tagged source='dialogue' with lower initial
trust (0.3 vs 0.5 for book-sourced).

Only extracts observations for characters *already in the graph* - does
not create new nodes. New character discovery is Scout/Explorer's job.
"""

from __future__ import annotations

import logging
from typing import Any

from llm_json import loads_tolerant

from storage.graph_db import GraphDB, TABLE_NODES
import usage_log

log = logging.getLogger("bookgraph")

# Trust score for dialogue-sourced observations (lower than book's 0.5)
DIALOGUE_TRUST_SCORE = 0.3

# Max dialogue text to send to LLM (chars)
_MAX_DIALOGUE_CHARS = 8000

# Max character names to include in prompt
_MAX_CHARACTER_NAMES = 40

_EXTRACT_PROMPT = """你是一个书籍角色观察提取器。你会收到一段读者与AI伴读助手的对话，
以及当前图谱中已有的角色列表。你的任务是从对话中提取关于角色的**新观察**。

提取规则：
1. 只提取对话中**明确讨论**的角色观察。不编造，不推测，不脑补。
2. node_name 必须是下方"已有角色列表"中的名字。如果对话讨论了列表之外的角色，跳过。
3. field 只能是：personality / appearance / motivation / role_in_story / speaking_style
4. event_type：
   - fact: 对话中明确陈述的客观信息（如"作者在第3章写了她戴红帽子"）
   - perspective: 读者或助手的解读（如"我觉得她其实很孤独"）
   - opinion: 对话中引用的角色自己的观点
5. value: 简洁的观察内容（如"孤独但倔强""对父亲又爱又怕"），10-30字
6. subject: fact 填 null；perspective 填"读者"或"伴读助手"；opinion 填观点来源角色名
7. tags: 主题标签（如"成长""亲子关系"），可自创，不要滥用
8. 只提取有对话依据的观察。如果对话只是闲聊不涉及角色理解，返回空数组。
9. 不要提取对话中明确否定或质疑后未确认的观察。
10. 同一角色同一 field 如果对话中有多个不同角度的观察，可以分别输出多条。

已有角色列表：
__CHARACTERS__

对话内容：
__DIALOGUE__

直接输出 JSON，无额外文本，格式如下：
```json
{
    "new_observations": [
        {"node_name": "角色名", "field": "personality", "value": "观察内容",
         "event_type": "perspective", "subject": "读者",
         "tags": ["成长"]}
    ]
}
```

如果对话中没有值得提取的角色观察，返回：
```json
{"new_observations": []}
```
"""


async def extract_from_dialogue(
    book_id: str,
    graph_db: GraphDB,
    api_client: Any,
    dialogue_text: str,
) -> int:
    """Extract observations from dialogue text, write to graph.db.

    Args:
        book_id: stable book id
        graph_db: GraphDB instance (for node lookup + write)
        api_client: ApiClient with async .complete(system, user) method
        dialogue_text: concatenated conversation text

    Returns: number of observations written.
    """
    if not dialogue_text or not dialogue_text.strip():
        return 0

    # Truncate to recent dialogue (keep the tail - most relevant)
    if len(dialogue_text) > _MAX_DIALOGUE_CHARS:
        dialogue_text = dialogue_text[-_MAX_DIALOGUE_CHARS:]

    # Fetch existing character names for the LLM to reference
    characters = _get_character_names(graph_db, book_id)
    if not characters:
        log.debug("incremental: no characters in graph for book %s, skipping", book_id)
        return 0

    char_list = "\n".join(f"  - {c}" for c in characters)
    prompt = _EXTRACT_PROMPT.replace("__CHARACTERS__", char_list)
    prompt = prompt.replace("__DIALOGUE__", dialogue_text)

    # Call LLM (non-streaming, 120s timeout for reasoning models)
    system = "你是一个精确的书籍角色观察提取器。只输出 JSON，不要输出其他内容。"
    try:
        with usage_log.source_scope("bookgraph", ref_id=book_id):
            try:
                raw = await api_client.complete(
                    system, prompt, timeout=120,
                    scenario="bookgraph.incremental")
            except TypeError:
                # complete() doesn't accept timeout parameter
                raw = await api_client.complete(system, prompt)
    except Exception:
        log.warning("incremental: LLM call failed", exc_info=True)
        return 0

    if not raw:
        return 0

    # Parse JSON from response
    observations = _parse_observations(raw)
    if not observations:
        log.debug("incremental: no observations parsed from LLM response")
        return 0

    # Write to graph.db
    written = 0
    for ob in observations:
        node_name = (ob.get("node_name") or "").strip()
        field = (ob.get("field") or "").strip()
        value = (ob.get("value") or "").strip()
        if not node_name or not field or not value:
            continue

        # Resolve node_name -> node_id (only existing characters)
        node_id = graph_db.name_to_node_id(book_id, node_name)
        if not node_id:
            log.debug("incremental: unresolved node_name '%s', skipping", node_name)
            continue

        event_type = (ob.get("event_type") or "perspective").strip()
        subject = ob.get("subject")
        obs_tags = [t.strip() for t in (ob.get("tags") or []) if t and t.strip()]

        # Register tags
        for t in obs_tags:
            graph_db.register_tag(book_id, t, event_type)

        ok = graph_db.add_observation(
            book_id, node_id, field, value,
            event_type=event_type, subject=subject,
            anchor_chapter=None,  # dialogue has no chapter anchor
            evidence=None,  # no chunk evidence
            tags=obs_tags,
            source="dialogue",
            trust_score=DIALOGUE_TRUST_SCORE,
        )
        if ok:
            written += 1

    if written > 0:
        graph_db.get_conn().commit()
        log.info("incremental: wrote %d observations from dialogue (book=%s)", written, book_id)
    else:
        graph_db.get_conn().commit()  # commit any tag registrations

    return written


def messages_to_dialogue_text(messages: list[Any]) -> str:
    """Convert conversation messages to dialogue text for LLM extraction.

    Handles both ConversationMessage objects (CLI mode) and dict messages
    (Web mode / on_pre_compress). Skips system and tool messages.
    """
    parts: list[str] = []
    for msg in messages:
        # ConversationMessage: has .role and .first_text()
        if hasattr(msg, "role") and hasattr(msg, "first_text"):
            role = msg.role
            text = msg.first_text() or ""
        # dict: has "role" and "content"
        elif isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                # content might be a list of blocks
                text = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            else:
                text = str(content)
        else:
            continue

        if not text or not text.strip():
            continue

        # Skip system and tool messages - they're not dialogue
        if role in ("system", "tool"):
            continue

        # Map role to readable label
        if role == "user":
            label = "读者"
        elif role == "assistant":
            label = "伴读助手"
        else:
            label = role

        parts.append(f"{label}: {text}")

    return "\n\n".join(parts)


def _get_character_names(graph_db: GraphDB, book_id: str) -> list[str]:
    """Fetch character canonical names from graph.db."""
    conn = graph_db.get_conn()
    rows = conn.execute(
        f"SELECT canonical_name FROM {TABLE_NODES} "
        "WHERE project_id=? AND kind='character' "
        "ORDER BY canonical_name LIMIT ?",
        (book_id, _MAX_CHARACTER_NAMES),
    ).fetchall()
    return [r["canonical_name"] for r in rows if r["canonical_name"]]


def _parse_observations(raw: str) -> list[dict]:
    """Parse LLM JSON response into list of observation dicts.

    阶梯与裸引号修复统一在 ``llm_json.loads_tolerant``。旧阶梯另有一个洞：
    响应是合法 JSON 但不是对象（如 ``[1,2]``）时 ``.get`` 抛 AttributeError
    且无人接 —— 现在被 ``expect=dict`` 挡在前面。
    """
    data = loads_tolerant(raw, expect=dict)
    if not data:
        log.warning("incremental: failed to parse LLM response")
        return []
    return data.get("new_observations", [])
