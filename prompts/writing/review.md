# 你的任务

审这一章，不改稿。输出一份 JSON，包含判据评分与问题清单。

## 评分（6 域 / 10 判据，每条 5 分）

| criterion | 判据 |
|---|---|
| `time_scene` | 时间推进、地点转换有依据 |
| `knowledge_boundary` | 角色只用此刻已知的信息行动 |
| `causality` | 行动有利益链，不是作者需要 |
| `character_fidelity` | 性格、口吻、行为与既有事实一致 |
| `relation_state` | 关系没有无过程地跳变 |
| `promise_payoff` | 本章该收的收了，该埋的埋了 |
| `chapter_change` | 局面/关系/信息/风险至少变一项 |
| `scene_function` | 每个场景有推进，不是流水账 |
| `narrative_voice` | 视角一致，没有解释腔 |
| `canon_consistency` | 与位置化事实、合同不冲突 |

每条给：`verdict`（pass / partial / fail / inconclusive）、
`earned`（0–5）、`evidence`（**正文里逐字出现的一句**）。

- 看不到依据就判 `inconclusive`，`earned` 写 0。**不要猜。**
- `earned > 0` 必须有 evidence；没有证据的得分会被系统作废。
- 问题多**不扣分**。分数只由判据决定。

如果上下文里给了「本章素材取舍」：只用它判断**有没有把没查到的 gap 当成事实写**，
以及声明要借的素材是否真的落成了本文的动作/物件。这是线索，不是新的评分项；
不要把「没用某条素材」当成问题。

## findings

```json
{"severity": "S1|S2|S3|S4",
 "category": "coherence|character|plot|pacing|prose|canon",
 "evidence": "正文里逐字出现的一句",
 "issue": "一句话说清问题",
 "fix_hint": "可执行方向，不写替换句"}
```

- `evidence` 必须逐字摘自正文；对不上的条目会被作废。
- **`fix_hint` 只给方向，禁止给整段替换文本。**
- S1：破坏主线/动机/正典/读者信任；S2：明显影响本章效果；
  S3：局部质量；S4：风格偏好。
- **`withhold` 检查：** 合同 `withhold` 里的条目（旧合同里
  `forbidden_cross` 以「不点破：」开头的也算），正文可以写进场景，但不许
  解释、不许让人物说穿；说破了记一条 S1/S2（category=canon），`evidence`
  用说破的那句原文。

## 本章目标核对（上下文给了清单时）

如果用户消息里出现「本章目标逐项核对（软件冻结清单）」，就在同一个 JSON
根对象里增加 `goalReviews` 数组，按清单 id 逐项返回 `status` / `description`
/ `evidence`。

- 清单由软件冻结：不得删项、改写目标或自创 id。
- 三态：`completed`（有正文动作与后果）/ `unmet`（正文明确延期、拒绝或出现
  相反结果）/ `unknown`（未提及、证据不足）。
- **没写到不等于没发生**：只写准备、承诺或部分完成，都不能判 `completed`。
- 只有 `unknown` 可以用 `evidence: []`；另外两态必须给逐字且唯一的引文。
- 目标与作者设定冲突时判 `unknown`，并在 description 说明冲突。

清单为空时不要输出 `goalReviews`。

## 输出

```json
{"criteria": [...], "findings": [...], "goalReviews": [...]}
```

只输出 JSON，不要围栏，不要解释。宁可少一条 finding，不要编证据。
