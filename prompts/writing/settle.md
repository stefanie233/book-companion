# 你的任务

从刚写完的章节正文里，抽取**本章新产生的变化**，输出一份 JSON。

只记录「本章改变了什么」。复述已经成立的事实是错的——那会让账本
长出一堆重复值。没有变化就不提交对应操作，空数组是合法答案。

## 输出格式

```json
{
  "chapter_summary": "一句话：本章改变了什么",
  "key_events": ["可验证的事件，最多 5 条"],
  "fact_ops": [
    {"op": "set", "subject_type": "character", "subject_ref": "节点 id",
     "field": "location", "value": "新值", "since_chapter": 12,
     "evidence_text": "正文里逐字出现的一句话"}
  ],
  "promise_ops": [
    {"op": "plant", "id": "tmp-1", "title": "伏笔名", "kind": "foreshadow",
     "weight": 7, "planned_payoff_chapter": 18,
     "evidence_text": "正文里逐字出现的一句话"},
    {"op": "advance", "id": "已有承诺 id"},
    {"op": "resolve", "id": "已有承诺 id"},
    {"op": "defer", "id": "已有承诺 id"}
  ],
  "entity_ops": [],
  "unknown_claims": [],
  "tension": 50,
  "tension_note": "一句话说明这个值为什么"
}
```

`tension` 是本章的戏剧紧张度（0–100）：0 是纯日常收束，100 是生死/真相级峰值。
它是一个**估算**，作者手动设过的点会覆盖它，所以宁可给得保守。

## 规则

1. **``evidence_text`` 必须逐字摘自本章正文**，不能改写、不能概括、
   不能拼接两个句子。校验是字符串包含，不是语义匹配。
2. ``subject_ref`` 只能填上下文「核心角色状态」里出现的节点 id。
   新角色不要自造 id——放进 ``entity_ops`` 的 ``create``。
3. ``field`` 用短名：``location`` / ``goal`` / ``state`` / ``knowledge`` /
   ``relation`` / ``ability``。同一次结算里，同一角色同一字段只写一次。
4. 角色**不知道**某事时，用 ``{"op":"unknown","unknown_reason":"unclaimed"}``，
   不要写成 ``value:""``。
5. 伏笔的 ``id`` 由系统分配：新伏笔写临时 id（``tmp-1``），已有的写真实 id。
6. ``mention`` 不算推进，不要提交；``defer`` 只用于明确推迟。
7. 不确定的事不要写。**宁可少记，不要记错。**

只输出 JSON，不要 markdown 围栏，不要解释。
