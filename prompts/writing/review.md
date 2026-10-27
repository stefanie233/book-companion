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

## 输出

```json
{"criteria": [...], "findings": [...]}
```

只输出 JSON，不要围栏，不要解释。宁可少一条 finding，不要编证据。
