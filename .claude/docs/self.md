# 自我说明层（Self-Knowledge）设计稿

> 状态：M0 已落地（2026-11-05），真实模型调用验收通过；决策 154 已写入
> `CLAUDE.md` §四。卡片内容由作者手动维护。
> 读者：本项目开发者；面向用户的文案在 `prompts/self/cards/`。

## 1. 一句话

一个只读、按需调用的 `self_explain(question)` 工具：把「产品心智模型卡片」
和「本机状态探针」组合成回答。不全量注入，不建向量索引，不承担任何写操作。

## 2. 目标 / 非目标

**目标**
- 新人问「合同是什么 / 版本在哪 / 作者档案哪来的」，得到「是什么 + 在哪」。
- 回答「你这儿为什么不可用」：embedding 没绑、索引没建、作者层被门挡住。
- 工具失败时附带 `self_topic`，把报错变成一句人话 + 下一步。

**非目标**
- 不做 FAQ 页面；不检索 `.claude/docs`（受众与颗粒度都不同）。
- 不做全量注入，不做 embedding / BM25 检索（卡片数量不够格）。
- 不做写操作；不替代 UI 引导；不在扮演模式里解释产品机制。

## 3. 结构

```
prompts/self/index.json          卡片索引（机器可校验）
prompts/self/cards/<id>.md       卡片正文（人写，产品口吻）
services/self_knowledge.py       加载 + 匹配 + 组包
services/self_probes.py          探针注册表
tools/self.py                    ToolSpec 注册
scripts/test_self_knowledge.py   单测
```

调用链：

```
用户问句 -> LLM 调 self_explain -> 匹配 top1-3 卡片
        -> 卡片带 probe 才跑探针 -> 拼 JSON -> LLM 用用户语言回答
```

不新增依赖。M0 用 jieba 分词（已在 requirements），不上 BM25。

## 4. 三条硬规则

1. **卡片不写状态**：出现「当前未配置」这类句子一律驳回。状态只来自探针。
2. **探针不联网**：`ready` 只表示本地配置齐全，不表示 API 可达；可达性由
   控制台的「测试」按钮负责。
3. **工具不 raise**：失败返回 `{"matched": false, ...}` 或 `state=unknown`。

## 5. 卡片

### 5.1 索引条目

```json
{
  "id": "writing-version",
  "title": "版本（每次改动存一档）",
  "aliases": ["git", "版本", "提交", "存档", "快照", "回滚", "撤销",
              "退回去", "改坏了", "分支", "合并", "历史"],
  "pages": ["writing"],
  "where": "写作页左栏 → 版本控制",
  "probe": "vcs",
  "related": ["writing-contract", "writing-workspace"],
  "danger": ["discard", "rollback"],
  "file": "cards/writing-version.md"
}
```

约束：`id` 唯一；`aliases` 非空且收用户说法；`where` 只写页面 + 面板名，
不写按钮文案；`probe` 必须是 `self_probes.PROBES` 的键；`related` 必须存在；
`danger` 取值限于 `delete|discard|rollback|merge|overwrite|publish`。

### 5.2 正文格式

```markdown
## 一句话
每次提交，把当前整个作品存一档；之后可以回看、对比、退回任意一档。

## 展开
像游戏存档。一次提交保存的是整个作品工作区的快照（正文、设定、合同、
账本、素材），不是单个文件；没有「暂存区」，所以不存在「先挑几个文件
再提交」。分支是平行写几条线，合并是把一条线的改动并回来。

## 常见误区
- 以为提交只保存当前章。
- 以为「丢弃」可以撤销——丢弃的未提交改动不可恢复。
- 以为分支会复制一份文件占空间——它只是记录线。
```

`## 一句话` 必填，其余可省；单卡全文 ≤ 600 字。

### 5.3 合同卡（另一张样例）

```json
{
  "id": "writing-contract",
  "title": "合同（这一章的验收单）",
  "aliases": ["合同", "立约", "验收单", "这一章要写什么", "改合同", "结清"],
  "pages": ["writing"],
  "where": "写作页 → 章节卡 / 合同总览",
  "probe": null,
  "related": ["writing-workspace", "writing-version"],
  "danger": [],
  "file": "cards/writing-contract.md"
}
```

正文：合同不是提纲，是这一章写完要逐条勾的验收单；一章一份。动笔前规定
必须发生什么、什么不能点破；结算时逐条报兑现；改合同等于重新立约，旧的
兑现记录作废。

### 5.4 M0 卡片清单（6 张）

| id | 一句话 | 探针 |
|---|---|---|
| writing-version | 每次提交存一档，可回看/回退 | vcs |
| writing-contract | 合同是验收单，不是提纲 | - |
| writing-workspace | 工作区四块：记忆/设定/计划稿/素材 | - |
| author-layer | 作者档案来自序跋/网页/正文样本，带出处 | authors |
| model-binding | 每个调用点绑一条连接 + 模型 | provider |
| semantic-recall | 语义召回是显式选项，需要绑定 embedding | semantic |

## 6. 匹配器

`services/self_knowledge.py`

```python
def explain(question: str, *, depth: str = "short", topic: str = "",
            ctx: dict | None = None) -> dict
```

打分（不做向量、不上 BM25）：

| 信号 | 分值 |
|---|---|
| `topic` 精确命中 id | 200 |
| alias 在问句里出现 | 100 |
| title 是问句子串 | 30 |
| title 分词交集 | 4/词 |
| 「一句话」分词交集 | 1/词，封顶 10 |
| 当前页面在 `pages` 里 | 10 |

阈值 20，取 top 3。全部低于阈值 → `matched=false` + `suggestions`（前 3 个
标题），提示模型说「我不确定」，禁止自由发挥。

分词用 jieba，**懒加载**（首次调用时 import，不在启动路径上）。别名匹配在
归一化后的字符串上做（去空白与标点、英文小写），保证「Git」和「git」同分。

索引与卡片在首次调用时加载一次并缓存；开发模式下按 mtime 失效，生产不热更。

## 7. 探针

`services/self_probes.py`

```python
ProbeContext = dict  # config / db / book_id / page / reading_chapter / writing_ctx
PROBES: dict[str, Callable[[ProbeContext], dict]]

def run_probe(key: str, ctx: ProbeContext) -> dict:
    try:
        return PROBES[key](ctx)
    except Exception as exc:               # noqa: BLE001
        return {"state": "unknown", "error": type(exc).__name__}
```

统一返回 `{"state": ..., "facts": {...}, "remedy": "..."}`。`state` 必须来自
每个探针自己的枚举；`unknown` 表示探针本身失败，模型不得据此推断原因。

### 7.1 provider

源：`provider_store.get_active_store(config)` + `Store.resolve(slot, config)`。

```json
{"state": "partial",
 "facts": {"chat": {"ready": true, "model": "deepseek-chat"},
           "embedding": {"ready": false}, "tts": {"ready": true},
           "asr": {"ready": false}, "web": {"ready": true, "mode": "tavily"}},
 "missing": ["embedding", "asr"]}
```

脱敏：只报模型名与绑定状态；不报 key、base_url、绝对路径。`state` 取
`ok | partial | unconfigured`。联网的 `mode` 只报 `native | tavily | none`。

### 7.2 semantic

源：`embedding_service.resolve_endpoint(config)`、`storage.embedding_db`。

状态：`unconfigured | no_index | model_mismatch | ok`。facts 带 `model`、
`vectors`、`books`、`index_models`（最多 3 个）。`model_mismatch` 的 remedy
必须说明「换绑模型后要重建索引」，否则用户会以为功能坏了。

### 7.3 authors

源：当前 `book_id` → `stable_book_id` → `author_books` → `author_id`；
`authorgraph.gate.gate_state()`；`authorgraph.coverage.coverage_block()`。

状态：`no_book | no_author | no_harvest | no_dated | all_ahead | ok`。

`no_dated` 与 `all_ahead` 必须分开（决策 105 / `gate_state` 已区分）：
前者是采集没抽到年份，后者是语料全在读者还没读到的章节——后者不是故障。
这是「作者档案为什么是空的」最容易被误判的一题。

### 7.4 vcs

源：`reading_ctx["writing_ctx"]` 取当前 `work_id`，
`writing_service.vcs_status(work_id)`。

状态：`no_work | unavailable | clean | dirty`。facts 带 `uncommitted`、
`branches`、`last_commit`、`last_message`。**只读**：不 commit、不 stash、
不 checkout。`unavailable` = 不是写作模式、拿不到请求级 `writing_svc`。

## 8. 工具

`tools/self.py`，在 `create_builtin_registry()` 注册。

| 项 | 值 |
|---|---|
| name | `self_explain` |
| toolset | `self`（写作模式要显式加入白名单） |
| exposure | `DIRECT_MODEL_ONLY`（主 agent 常驻，子 agent 不可见） |
| permission | `READ_ONLY` |
| params | `question`（普通提问）/ `topic`（错误联动）/ `depth`（short/full） |

**为什么不用 DEFERRED**：终端用户不会说「用 tool_search」。schema 只有三个
字段，常驻成本约 100 token，换可靠性值得。

**写作模式**：`build_writing_registry()` 目前只取 `WRITING_TOOLSET`，必须改成
`[WRITING_TOOLSET, "self"]`；同时把本轮 `ctx` 以 `writing_ctx` 挂进**请求级**
`registry.reading_ctx`，让 vcs 探针读到实时 work_id。不能写进共享 base registry
（并发请求会串现场）。

返回：

```json
{"matched": true,
 "cards": [{"id": "...", "title": "...", "where": "...",
            "summary": "...", "detail": "...(仅 depth=full)", "related": []}],
 "status": {"vcs": {"state": "dirty", "facts": {"uncommitted": 3}}},
 "guard": "涉及不可逆操作：丢弃未提交改动不可恢复。只解释与确认，不要执行。"}
```

`guard` 由卡片的 `danger` 生成；命中任何带 `danger` 的卡，本轮都只解释、
不代做。`depth=short` 不带 `detail`，总载荷 ≤ 2000 字符。

## 9. 提示词接线

只加一处常驻：`prompts/platform_hint.md` 的 Tool routing 加一条。

> 用户问功能怎么用、界面在哪、为什么某能力不可用，或工具错误里出现
> `self_topic` → 调 `self_explain`。先答一句话结论，再给「在哪」。用用户
> 的语言，不出现 embedding / DB / 索引 / token 等内部词。涉及删除、回滚、
> 合并、覆盖时只解释并请求确认，不要代做。

工具 description 承载其余约束：只引用返回内容；`matched=false` 就说不知道；
有 `guard` 时只解释。

### 9.1 失败联动

工具失败时统一带上主题键，不直接回答机制：

```json
{"ok": false, "error": "没有可用的 embedding：…", "self_topic": "semantic-recall"}
```

M0 先接两处：`writing_research(source=semantic)` 与作者层「无档案/无证据」。
模型看到 `self_topic` 后调 `self_explain(topic=...)`，用返回的卡片与探针回答。
错误原文仍保留，不替换成含糊话术。

## 10. 页面与模式

- `page` 在 `api/routes/chat.py` 已算出；请求组装时补进
  `reading_ctx["page"]`。只做加分，不做硬过滤。
- 写作模式：`build_writing_registry()` 把 `ctx`、`svc` 挂进请求级
  `reading_ctx`；不要写进共享 base registry。
- `roleplay`：工具返回 `{"disabled": true, "reason": "roleplay"}`；
  roleplay 提示词加一句「不要调用 self_explain」，人设不破。
- 其余模式（home/reader/writing，含 immersive/overview/divergent）可用。

## 11. 动作安全策略（2026-11-05 修正）

「能动手就不要只解释」只适用于只读与可逆动作。破坏性动作按下面的表走：

| 类型 | 例子 | 策略 |
|---|---|---|
| 只读 | 查状态、解释机制 | 直接回答 |
| 可逆且用户明确要求 | 新建作品、建章、草稿 | 可代做，做完告知 |
| 破坏性/不可逆 | 删书、删章、丢弃改动、回滚、合并、覆盖正文 | 一句话说明后果 + 等明确确认；同一轮不执行 |
| 对外发送 | 联网、导出 | 走既有授权链路，不在自我说明层新增 |

`self_explain` 本身永远 `READ_ONLY`，不注册任何写能力。确认后的执行仍走原
工具与权限链路，不因为「用户已经同意」而放宽。一次同意不跨轮外推。

后续统一的写操作拦截 hook 另立设计，不塞进这一层。

## 12. 维护与测试

`scripts/test_self_knowledge.py`，加入 `scripts/check_all.py` 的 `MODULE_TESTS`。

1. **索引校验**：id 唯一、file 存在、probe 键存在、related 存在、pages 合法、
   danger 合法。
2. **匹配黄金集**：12 条真实用户问法 → 期望 id；含一条无匹配。
3. **探针状态**：用 fake config / store 覆盖每个 state；断言序列化结果不含
   `sk-`、`http`、盘符路径。
4. **页面加分**：构造两张同分卡，writing 下写作卡排在前面。
5. **注册冒烟**：主 registry 有 `self_explain`；写作 registry 也有。
6. **术语泄漏**：黄金集回答里不得出现内部词表（人工评审 10 条）。

M1 再加：`where` 里的面板名在 `reader/js/` 可搜到（防 UI 改名后卡片指错）。

## 13. M0 范围与验收

- 6 张卡（版本 / 合同 / 工作区 / 作者层 / 模型绑定 / 语义召回）
- 4 个探针（provider / semantic / authors / vcs）
- 工具 + 提示词一条 + 2 处失败联动（`writing_research` 语义、作者工具）
- 单测；`python scripts/check_all.py` 通过

验收用三句新用户原话：「合同是啥」「我改坏了能退吗」「作者档案怎么是空的」。
三句都能答到「是什么 + 在哪 + 你这儿为什么」，且不含内部术语，即通过。

**真实调用结果（2026-11-05）**：模型并行调用两次 `self_explain`，回答分别
落到合同/版本与作者层；版本卡带出 `guard` 后，模型明确说「不会替你操作，
确认退到哪一档再动手」。作者层探针在未开书时返回 `no_book`，模型据此
回答「因为还没打开任何书」，没有编造档案内容。

语义召回探针在真实配置上报 `ok`（`doubao-embedding-vision` / 6035 块 /
27 本），模型据此判断「不是没配，而是没显式选 `semantic`」——这正是只靠
语料答不出来的那类问题。联网问题同理：探针报 `web ready (tavily)` 后，
模型没有硬造原因，而是说明「绑定已在，若仍失败需要具体报错」。

## 14. 风险

| 风险 | 处理 |
|---|---|
| 卡片写成开发文档 | 单卡 ≤ 600 字；审查禁内部词；不从 `.claude/docs` 复制 |
| 状态漂移 | 卡片禁写状态；探针每次现算，不缓存 |
| 模型自由发挥 | `matched=false` 明确说不知道；`unknown` 不得推断原因 |
| UI 改名 | `where` 只到面板名；M1 加 grep 校验 |
| 并发串上下文 | 写作现场只挂请求级 registry 的 `reading_ctx` |
| 自我说明挤占主线 | 不主动讲课；只在用户问或工具失败时触发 |

## 15. 开放问题

1. 探针报不报模型名？当前设计报（如 `deepseek-chat`），不报端点与 key。
2. 扮演模式是彻底禁用还是只降级？当前设计禁用。
3. 卡片的作者与发布节奏：每次产品改动是否强制补卡？建议在 PR 清单加一项。

---

## 附：实施顺序（M0 已按此顺序落地）

1. `services/self_probes.py` + 单测（先让探针能独立跑，不碰提示词）
2. `prompts/self/index.json` + 6 张卡 + 索引校验
3. `services/self_knowledge.py` 匹配器 + 黄金集
4. `tools/self.py` 注册 + `platform_hint.md` 一条
5. 写作 registry 白名单 + `reading_ctx` 接线
6. 2 处失败联动 + 真实问句验收

