# recall — 参考素材召回评测

回答一个问题：**换成语义检索，素材召回到底有没有变好？**
只做离线评测，不接生产写作路径。

## 语料单元

| 单元 | 来源 | 为什么单独成条 |
|---|---|---|
| chunk | `books.db.book_chunks` | 完整场景，保上下文 |
| observation | `graph_*.db.graph_observations` | 人物/关系观察，短、可转写 |
| quote | `graph_*.db.graph_quotes` | 逐字对白，质感的直接来源 |

去重：同一本书里文字完全相同的单元只留一条；提取器旁注（「这一说法」「转述」「存疑」）不进索引。

## 索引

- 向量：`doubao-embedding-vision`（火山 coding plan，2048 维），L2 归一化后存 SQLite BLOB。
- 规模约 9.5k 单元，查询时 numpy 暴力余弦，**不需要 ANN / 向量库**。
- 词法：jieba + BM25，专名和生僻词靠它兜底。
- 混合：RRF（k=60）融合两路，取长补短。
- 嵌入只取头尾 1800 字；中间细节对主题贡献小，且能压 token。

## 评测集

1. **合成查询（有真值）**：从参考书里抽一段，让模型反写一句 20–40 字的
   「场景检索请求」（禁专名、禁照抄），再看原段能不能被自己的请求召回。
   确定性强，适合比较检索器。
2. **真实用例**：6 个写作用例的合同场景，检索限在该用例选的参考书内；
   没有真值，看门是否通过、top1 余弦、词法/向量重合度与样例。

## 运行

```powershell
$env:PYTHONIOENCODING="utf-8"
python -m writebench.recall build            # 首次建索引（可中断续跑）
python -m writebench.recall build --books sb-xxx   # 增量加一本，不要 --force
python -m writebench.recall eval --n 30      # 跑评测，产物在 C:\tmp\wb_index
python -m writebench.recall query "克制地写一个人等另一个人开口" --book sb-xxx

# qwen 索引（快、区分度更好）：另存一个库，别覆盖 doubao
$env:WB_INDEX_PATH="C:\tmp\wb_index\qwen.db"
$env:WB_EMBED_PROVIDER="p_383a2e3c"
python -m writebench.recall build --case-books --force --model qwen3.7-text-embedding
python -m writebench.recall eval --n 30
```

默认嵌入走 agent plan（`p_1aec1b69`）：coding plan 的 429 很密，
1 请求/秒仍会被挡；agent plan 可稳定到 2–3 请求/秒。

产物：`C:\tmp\wb_index\index.db`、`report.json`、`report.md`、`queries.json`。

## 已测：词法基线（6 个真实用例）

当前生产的 LIKE + 门（≥2 个检索词命中）：

| 用例 | 词命中 | 覆盖率 | 门 | 观察/引文/边/原文 |
|---|---|---|---|---|
| c01 | 5 | 0.455 | 过 | 5 / 6 / 6 / 3 |
| c02 | 4 | 0.333 | 过 | 2 / 2 / 0 / 2 |
| c03 | 6 | 0.500 | 过 | 1 / 1 / 4 / 3 |
| c04 | 5 | 0.417 | 过 | 6 / 6 / 0 / 3 |
| c05 | 4 | 0.333 | 过 | 3 / 5 / 4 / 3 |
| c06 | 6 | 0.500 | 过 | 4 / 6 / 6 / 3 |

结论：**门不是瓶颈**（6/6 都过），瓶颈是覆盖率和素材池厚度——
一半检索词在参考书里没有词面命中，每类只取到 1–6 条。

## 已测结果（5 本参考书，1,937 单元）

合成查询 30 条（10 chunk / 10 observation / 10 quote）：

| 模型 | 方法 | R@1 | R@5 | R@10 | MRR |
|---|---|---|---|---|---|
| doubao-embedding-vision | lexical | 0.60 | 0.70 | 0.767 | 0.647 |
| doubao-embedding-vision | vector | 0.467 | 0.70 | 0.833 | 0.576 |
| doubao-embedding-vision | hybrid 3:1 | **0.60** | **0.833** | **0.90** | **0.712** |
| qwen3.7-text-embedding | vector | **0.60** | 0.767 | 0.867 | 0.686 |
| qwen3.7-text-embedding | hybrid 3:1 | **0.60** | **0.867** | **0.90** | 0.69 |

真实用例材料质量（LLM 0–2，同一候选 3 次平均）：

| 模型 | 词法 | 向量 | 混合 3:1 |
|---|---|---|---|
| doubao | 0.81 / 可用 0.667 | 1.09 / 0.733 | **1.13 / 0.70** |
| qwen | 0.89 / 0.60 | **1.17 / 0.80** | 1.10 / 0.733 |

**余弦不能当门**：doubao 的无关查询 top1 余弦均值 0.334，跨书负样本 0.336，
真实用例 0.326–0.424——完全重叠；qwen 好一些（无关 0.388、跨书 0.487、
真实 0.514–0.64），但仍有交叉。绝对阈值会误杀 c02/c05。
语义相关性应交给蒸馏/审稿，词法门只做廉价预筛。

结论：

- 纯向量不能替代词法：专名、生僻词仍靠 BM25。
- 混合检索在 doubao 上最稳；qwen 的向量单独已很强，混合把 R@10 抬到 0.90。
- qwen 建索引 86 秒 / 1024 维；doubao 578 秒 / 2048 维（agent plan 限流后）。

## 已知局限

- 合成查询来自同一段原文，可能带词面重合，词法会因此偏高；判读时看
  chunk / observation 分开的指标。
- 真实用例没有人工标注，只能看样例与门，不产出 recall。
- 检索好不等于写作好：生产是否采用，还要过 writebench 的成对盲评。
