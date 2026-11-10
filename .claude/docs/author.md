# 作者层（AuthorGraph）域文档

> 旧设计（1.0，已落地）：`docs/authorgraph/README.md`
> 本文件只记**已实施**的决策与理由；未实施的里程碑写在最后。
> 2026-10-26：M1（事实证据底座）、M2（正文声音采集）、M3（作者档案）与
> M4（写作风格透镜）已落地。

---

## 一、已实施：M1 证据底座（2026-10-26）

### 92. 双账本，单库

事实（`author_records`）与声音（`author_corpus` + `author_voice_traits`）
分开建账，但**仍是一个 `data/authors.db`**。拆库只会在跨库 join、备份和
迁移上收税；P1/P2 的数据量远没到需要拆的程度。

### 93. 来源不是证据，引文才是证据

- 新表 `author_sources` / `author_source_roles` / `author_evidence`。
- `source='web'` 的记录必须挂一条 `supports` 证据，且 `quote` **能在抓到的
  页面正文里逐字找到**（空白/全半角归一后）。
- 旧数据（只有 URL、没有正文）迁移为 `relation='legacy_url_only'`，
  quote 为空；UI/消费端必须能把它与「已核验」分开。
- 抓不到正文的页面 → `author_sources.status='blocked'`，**不产生 web 证据**。

### 94. 事实补全是一条管线，不是一次调用

`authorgraph/sources/`：registry（分级/URL 归一）→ query_plan（按 facet
补缺口）→ fetch（抓页 + 标准库正文抽取）→ extract（**一页一次** LLM）→
verify（逐字/年份/角色/tier）→ merge（去重/冲突只报告）→ pipeline（编排）。

- 一条 `"{name} 生平 简介"` 打天下的做法废弃。
- 顺序是决策：身份 → 书目 → 生平 → 关系 → 解释；**身份不明时只跑身份**。
- 失败分类可分辨：`zero_results` / `fetch_blocked` / `quote_not_found` /
  `role_mismatch` / `tier_too_low` / `budget_exhausted`。
- 触发：按需（`refresh_author.py --web` / 对话深挖），**不挂导入与图谱提取**。

### 95. 身份先于内容

`author_identifiers` + `identity_status`。Wikidata 按名字找候选，纯函数打分：
外部 ID 命中 → resolved；只有名字相同 → provisional；两个高分候选打平 →
conflict 并**停止自动补全**。任何情况下不自动合并同名作者。

两个务实的口子（否则新作者会死锁：没有生年 → 分数不到 0.8 → 永远
provisional → 规则又不许补生平 → 生年永远补不上）：

- **唯一精确同名候选** → resolved（标注「唯一同名候选」）。
- 身份仍不明时**放行其余 facet**，身份查询排第一，条目保持「待确认」
  并在 manifest 警告。冲突（conflict）仍然硬停。

### 96. 作者层 agent 的引用纪律

- `author_scout` 新增 `author_web_fetch`：搜索 → **抓正文** → 逐字引用。
- 落库时宿主拿引文回抓到的正文核对；只有网址 / 没抓页 / 引文对不上
  一律降级 `model`（不可注入）。
- `author_lookup` 的卡片标「网页·引文已核 / 未核验」与身份「待确认」。
- `author_deepen` 列入 `CONTEXT_CREATING_TOOLS`（它会 fork scout，不能递归）；
  `author_graph` 工具组在子代理里提升为 DIRECT（否则 scout 提示词说的
  `author_lookup` 实际不可见）。

### 97. `author_corpus` 重建与三种幂等键

v2 重建一次（旧表 `stable_book_id NOT NULL` 装不下外部访谈），保留 id 与
`author_corpus_v1_backup`。幂等键分三条：

- 书内新行：`(source_id, chunk_id, excerpt)`（partial unique）
- 外部行：`(source_id, excerpt)`（partial unique）
- **旧写入路径**：`(stable_book_id, chunk_id, excerpt) WHERE source_id IS NULL`
  —— 少了这条，`add_corpus()` 重跑会不断加料（实测踩到）。

迁移在 Python 里包事务（`_migrate_corpus_v2`），不走逐条 SQL 迁移引擎；
迁移前自动把 `authors.db` 复制到 `data/backups/`。

---

## 一-b、已实施：M2 声音采集（2026-10-26）

### 98. 声音只看正文，可见性由 position 决定

事实采集看序跋（`authorgraph/scope.py`），声音采集看正文
（`authorgraph/voice/scope.py`）—— 两套闸，不共享：

| position | 正文 | speaker | domain |
|---|---|---|---|
| `narrator` | 可采 | `author` | `nonfiction` |
| `avatar` | 可采 | `via_character` | `fiction` |
| `editor` / `recorder` / `absent` / `unknown` | **不采** | — | — |

`avatar` 的口吻可用、经历不可用；`editor`/`recorder` 书里的正文是别人的
文本。这不是保守，是错误方向不可逆。

### 99. 抽样必须确定性，注释与短块先筛掉

- 窗口按 `sha1(stable_book_id:chunk_id:start:end)` 排序，不用 `random()`；
  同一本书同参数必得同一批窗口。
- 每段 200–500 字、逐字子串（`author_corpus` 只存 excerpt）。
- 排除：非正文章类型、`head_scan` 的 copyright/promo/toc、Scout 排除、
  <80 字、逐字/近似重复（5-gram bottom-k 指纹）、**正文混注释的块**
  （注号 ≥3；实测《房思琪》）、**块内目录页**（实测《当下的力量》
  第一章第一块就是 Contents/目录，模型只能自报）。
- 三份「这块是什么」的判断（chapter_type / head_scan / Scout）走
  `bookgraph/selection.select_chunks` 一次用全，不各写一套。
- 每章配额按章数摊（`ceil(N/章数)+1`，下限 2）—— 固定 2 会把三章书的
  12 段预算卡在 6 段（实测）。
- 场景标签是**启发式提示**（引号比例/标点/词表），模型在输出里纠正。

### 100. 一次 run ≤2 次调用，证据只能来自本批

- 每批 6 段，最多 2 批；输入 ≤8000 字（设计稿 08 §四.3）。
- trait 的 `evidence_corpus_ids` 必须落在本批；编造的剔除，一条不剩整条丢弃。
- 只有 1 段证据 → `strength ≤ 0.4` + `provisional`。
- 证据要跨 ≥2 章（只按 chunk 数不够：同一章相邻块可能只是同一处文字的两次
  切片）。单章作品因此只能是 `provisional`。
- `voice_domain` 缺失按来源推断，并记 `inferred_domain`。
- 置信度 = 来源基数 × 证据充分度 × 跨批一致度（单批 0.85、一致 0.9、
  部分 0.7、冲突 0.4）。

### 101. 双域两道闸：写作用 `fiction/nonfiction`，序跋/访谈不进透镜

`select.py` 是唯一出口：只给 `active` + `transferable=1` +
`voice_domain ∈ (fiction, nonfiction)` + 语料层 `narration/essay`。
**窗口一律 `narration`**：对话密集的窗口仍是作者写下的叙述文字，用
`scene_type='dialogue'` 标记，不拆 `narrative_layer` —— 拆了写作透镜
（只取 narration/essay）会把对话场景整批丢掉（实测：林奕含笑对话场景
样本为 0）。`narrative_layer='dialogue'` 留给只摘角色对白的样本（M2 不产）。
`source_bound` **不建表**，只进 `author_voice_runs.manifest` —— 它永远
不该进写作。

### 102. 声音 run 不写 `author_records`；状态机在书级

`store_voice` 与 `store_harvest` 平级，互不调用。`author_books.voice_status`：
`none → running → ready/partial`；失败 `failed`、position 不可采 `skipped`。
`ready` 需 ≥6 段、≥3 场景、≥1 active trait，否则 `partial`。
**任何失败都不影响图谱提取。**

### 103. 幂等：内容 hash 命中跳过，`--force` 只清本书声音语料

- 键：`(book, voice_content_hash, prompt_version, model)`；命中 `ready/partial`
  且 hash 未变 → 不调用模型。`prompt_version` 是**规则版本**：改提示词、
  抽样、归一化都要 bump（现在是 `author-voice-v2`），否则代码变了会被
  误判成缓存命中。
- 语料写入**先查再插**：`INSERT OR IGNORE` 也会推进 AUTOINCREMENT，
  重跑复用旧行会让下一次新 id 跳号，而 id 是 trait 证据的锚。
- `--stale` **不自己算 hash**（它拿不到 head_scan/Scout 的裁剪，会与
  pipeline 算出不同的值），只把所有正文可见的书交给 pipeline，由缓存键
  决定跑不跑。改规则后 bump `PROMPT_VERSION` 即可全库失效。
- hash 变了 → 重采，旧 trait 按 revision 标 `superseded`，不删行。
- `--force` 清本书 `narrative_layer<>'paratext'` 的语料（保留序跋与事实），
  再重采。**规则版本/模型变了也走同一条清理** —— 旧样本是按旧尺子选的，
  留着会和新样本混成两套账。
- `scope_type='work'`、`scope_ref=stable_book_id`：重跑一本书不会把别的书
  的 trait 一并退役（与设计稿的 author 级默认不同，理由见偏差表）。

### 104. Scout 只选段，不分析风格

`bookgraph/scout.py` 的 `voice_candidates` 只增加输出 token（输入不变）：
12–20 段、覆盖五类场景、≥2 个安静段、不得选 excluded。解析宽容：缺失
→ 确定性抽样；不在本书/已排除 → 丢弃并报；词表外 → `other`。老
`scout_plan` 没有字段也不重跑 Scout。

---

## 一-c、已实施：M3 作者档案（2026-10-26）

### 105. 档案是纯投影，门只算一次

`authorgraph/dossier.py` 只把已有行重排成 DTO：**不调模型、不生成句子**。
`identity / timeline / works / voice / relations / themes / sources` 分区返回，
不把 records、claims、corpus、voice_traits 混成一个数组 —— 混在一起就会被
读成同一种可信度。

`gate_state()` 全层只算一次，所有区块共用。`book_id` 不传 = 显式的全集
视角（`gate.mode='no_book'`），不是「门坏了」；书对不上
（`unmapped`）、算不出年份（`no_dated`）、材料全在后文（`all_ahead`）
三种 fail-closed 也各有 mode，不混成一个空数组。

### 106. 时间轴保留，档案成为默认入口

`author-timeline.js` 的旧视图**不删**：档案页的「全屏时间轴」仍会切回去，
`/api/authors/{book_id}/{author_id}/timeline` 也保持原样。新入口
`openGraphAuthor()` → `window.openAuthorDossier()`；档案模块没加载时
（旧页面缓存）自动退回时间轴。两个模块不互相 import，靠 window 入口解耦。

档案页的年谱 tab 是**纵向年表**（年份 + 上下两轨的卡片），不是重写一遍
可缩放画布 —— 两套轴布局会各错一次，而这个项目已经在剧透门上栽过
「四处各错一处」。要看全景就点「全屏时间轴」。

### 107. 覆盖度是「还缺什么」的索引，不是分数

`authorgraph/coverage.py` 是纯函数：事实 0.30 / 作品 0.15 / 声音 0.30 /
关系 0.15 / 来源 0.10。声音 = 样本量 × 场景覆盖 —— 只堆同一类场景不算
「有声」。`missing` 报最缺的 2–3 项，能具体到 `voice.scene:ending`
就不只说「声音不够」。

阈值是经验值（事实 12 / 作品 6 / 样本 12 / 场景 5 / 关系 6 / 来源 8），
到了就够用，再采收益递减。UI 的 chips 用原始计数，title 里才是百分比。

### 108. 来源抽屉：已核 / 旧版 URL / 被门挡住，三种分开

- `quote_verified` 只数 `supports` 且引文非空的行；
  `legacy_url_only` 单独计数，UI 标「旧版 URL，正文未核验」。
- 冲突（`contradicts`）**双侧引文都保留**，不自动裁决。
- 有剧透门时，来源卡片仍在，但只带可见目标的引文，另报
  `hidden_quotes` —— 不把「有来源」和「能读引文」混为一谈。
- 抽屉是右侧栏 + Esc + 焦点陷阱；不是居中弹窗。

### 109. 声音按维度归组，不用雷达图

10 个维度各一行：维度名 + 定性刻度 + 置信度条 + 证据数。没有稳定结论的
维度显示「这个维度还没有稳定结论」，不补一条空的。同一维度多说法在库里
并列（设计允许），档案页逐条列出，透镜（M4）才按每维度一条挑。

正文文风（`fiction/nonfiction`）是唯一可进写作的域；序跋/访谈/`source_bound`
即使出现在档案里，也不提供「用于写作参考」入口。

### 110. 年份门算不出来时，声音仍按章放行

设计稿说 `no_dated` 时 `voice.samples=[]`。实测发现这会让**一半的作者档案
整页空掉**：9 本有声音的书里只有 4 本的序跋抽到了年份，其余 5 本一旦
带 `max_chapter` 就什么都不显示 —— 但正文样本本身有 `chapter_index`，
读者已读章节里的文字不是「未来的内容」。

所以口径改成：

- `records/claims/relations` 仍然 fail-closed（年份是它们唯一的锚）。
- `voice.samples` 与 trait 证据**按 `chapter_index <= max_chapter` 直接施门**；
  书的 stable id 对不上（`unmapped`）时才整块清空。
- `gate.mode` 保持 `no_dated/all_ahead`（它描述的是年份门），
  `voice.gate_mode='chapter_only'` 单独说明声音走的是章门。
- 来源只显示可见样本 / 可见目标的引文，`hidden_quotes` 报被挡数量。

### 111. 作品页只认书名号，不把写作事件当作品

`author_records.kind='work'` 既可能是作品名，也可能是「开始发表作品」
「参加编辑部聚会」这类写作事件。直接平铺成作品卡，作品页会变成一串句子
（实测史铁生 10 张卡里 8 张是事件）。

所以作品页 = 本地书 + 从 `work/publication` 条目里用 `《…》` 抽出的书名；
没有书名号的条目留在年谱的作品轨。`《X》编辑部 / 杂志社 / 出版社` 后面
跟机构词的不算作品。书名按 `name_key()` 去重，多个条目提到同一本书时
年份取第一个非空值。

### 112. 新接口与旧接口并存，缓存 5 分钟

新增：

```
GET /api/authors?q=&limit=&offset=&mode=      搜索/列表
GET /api/authors/{author_id}/dossier?book_id=&max_chapter=&sections=
GET /api/authors/{author_id}/voice?book_id=&max_chapter=
GET /api/authors/{author_id}/sources?book_id=&max_chapter=&target_type=&target_id=
```

旧 `/api/authors/{book_id}` 与 `/api/authors/{book_id}/{author_id}/timeline`
不改。搜索默认**前缀匹配**，`mode=fuzzy` 才子串；`mode=exact` 全等。
档案投影有 5 分钟进程内缓存（`clear_cache()` 可清）；TTL 是兜底，
写操作不自动清 —— 目前写入口都是脚本，重跑后最坏看到 5 分钟前的档案。

---

## 一-d、已实施：M4 写作风格透镜（2026-10-26）

### 113. 预算排序语义先修，再谈 `author_voice`

旧 `compile_blocks()` 按 `priority` **升序**迭代并优先保留，和文档「priority
越高越重要」正好相反；因为各块 priority 互不相同，`drop_order` 从未生效，
参考素材反而排在作品图前面。M4 先改成降序保留：

```python
sorted(optional, key=lambda e: (-e["priority"], e["drop_order"] or 999))
```

同时把旧块按设计稿推荐表重排（作品图 70 / 用户偏好 60 / 近三章 50 /
作者声音 40 / 参考素材 30）。**不保留双语义**：留两套判定，下一次加块
一定会把某一块放错位置。回归测试钉住「预算只够一个可选块时留 priority
高的」。

### 114. 检索键是场景，不是作者名

`writegraph/voice_lens.py` 是纯函数：上游 `authorgraph/voice/select.py`
已经从库里取出合规候选（正文层 + `fiction/nonfiction` + `transferable=1`），
这里只做「对本章表达层有没有用」：

```
score = 0.40 场景命中 + 0.25 合同/场景关键词 + 0.15 时期
      + 0.10 视角 + 0.10 来源质量
```

场景精确命中记 1.0，同组（内省/回忆/情绪高点、对话/论辩、场面类）记 0.5。
合同场景类型从 `purpose / exclusive_event / ending_state / must_hit_now`
里**显眼词**推断（对话、回忆、战斗、景物、内心……），认不出就不给
`scene_type`，不猜。

去重规则是「同一章只留最高分一条 + 同一 `content_hash` 只留一条」。
合同 `forbidden_cross` 会剥掉「不能写/禁止」词头后按短词匹配样本；命中即
排除，并报「几段与合同禁忌冲突」。

### 115. 注入的是规则映射，不是模型原话；例证只给正文

`writegraph/voice_lens.RULE_TEXT` 为 10 维 × 规范值写了一句话提示，每条
≤40 字。**不用 trait 的 `statement` 原文**：原话可能带作者专名、角色口癖或
生平事实，一旦进 prompt 就违反设计稿 06 §四.2。

`writegraph/voice_card.py` 只排版：标题写明「仅表达层，不覆盖本书正典」，
规则 3–8 条，例证 3–5 段（每段 ≤300 字，总计 ≤1200 字），脚注重申人物、
事实、时间线、承诺以正典为准。绝不出现「你是某某」。整块 ≤1400 字
（≈700 tokens）；空规则 + 空例证 → 整块省略。

### 116. 生成后检查只报告，不进正典

`writegraph/voice_check.py` 全是确定性指标：平均句长、对话比例、总结句
比例、情绪直说密度、段落长度分布、相邻段开头重复。只产出 `info/warning`，
没有阻断字段，也不自动改写。报告进写作页「审稿」面板单列，不混进 LLM
审稿结论；LLM 复评以后接入也只能标 `judgment`。

### 117. 默认关闭；作品级默认存 `works.style_profile`

不新增表。`style_profile.author_refs` 最多两位，第一位作为当前默认；
每章请求可用 `?style_author_id=&style_scene=&style_period=&style_pov=`
覆盖。新增：

```
GET  /api/writing/works/{work_id}/style
PUT  /api/writing/works/{work_id}/style     {author_id, enabled, scene_type, period}
GET  /api/writing/works/{work_id}/chapters/{index}/style-check
GET  /api/writing/works/{work_id}/chapters/{index}/context?style_author_id=…
POST /api/writing/works/{work_id}/chapters/{index}/write?style_author_id=…
```

不选作者时 `author_voice` 块**完全不出现**，上下文与旧版一致。`stale/failed/
none/pending` 的作者不注入；`partial` 允许选但 UI 标「样本不足，仅供轻参考」。
`authors.db` 不存在时直接报 `author_db_unavailable`，**不为了查一下建空库**；
任何失败都只返回空块 + 诊断码，写作继续。

档案页只在「有正文样本 + 有可迁移 trait」时显示「用于写作参考」；点击后
切到写作页，把作者交给 `WritingUI.setStyleRef`，作品还没选就先记住、
等 `selectWork` 完成后落库。

---

## 二、与设计稿的偏差（有理由）

| 设计稿 | 实际 | 理由 |
|---|---|---|
| trafilatura/readability 抽正文 | 标准库 `html.parser` | 不为此加依赖；正文抽取只需段落与标题，够用且可测 |
| 未知域名 tier=4（只能做线索） | 未知域名 tier=3，manifest 报 `unclassified` | 出版社/大学/媒体域名无法穷举；逐字引文校验已挡住主要风险 |
| VIAF/ISNI/OpenLibrary 全部接入 | M1 只接 Wikidata | 中文作者覆盖优先；接口形状不同，留到 M5 |
| 外部访谈/演讲样本（`public`） | **M2 暂不做**，留 M5 | 公共声音要「只取作者直接引语」，与网页抽取的 claim 管线不同形；M2 先把正文 `narrative` 做扎实 |
| `source_bound` 建表 | 只进 `author_voice_runs.manifest` | 它永远不进写作透镜，建表只会多一条被误用的路径；UI 需要时再建 |
| 书籍重导时立刻标 `stale` | 声音 run 启动时检测内容 hash | M2 没有导入钩子；脚本是唯一触发点。M5 接导入/提取完成事件 |
| trait 的 scope 默认 author | `scope_type='work'`，按书分 | 重跑一本书不该把另一本书的 trait 标 superseded；跨书合并留给 M3/M4 的投影层 |
| 只按章节/长度/重复筛样本 | 另加「注号 ≥3 / 块内目录页」过滤 | 实测《房思琪》注释块被模型读成「不是叙事文本」、《当下的力量》第一章第一块是目录；抽样阶段拦住比让模型自报便宜 |
| 对话窗口 `narrative_layer=dialogue` | 窗口一律 `narration`，`scene_type` 标对话 | 设计 06 的透镜 SQL 只取 narration/essay；窗口是作者叙述，不是角色对白。拆层会让对话场景从透镜里消失 |
| 透镜每个维度可多条 trait | `eligible_traits` 默认每维度一条（最高置信） | 库里同一维度多说法是**账**，注入时并列会挤掉别的维度；UI 需要全量时传 `per_dimension=False` |
| 档案页时间轴复用可缩放画布 | 纵向年表 + 「全屏时间轴」切回旧画布 | 两套轴布局会各错一次；旧视图已经过真实数据校准，重写收益低于风险 |
| `works` 是扁平数组 | `works` 扁平数组 + `works_meta.counts` | 前端要显示本地/书目计数；把 meta 塞进数组会让每张卡重复一遍 |
| 档案 API 拆分接口懒加载 | 主接口一次返回全部区块；分接口仍保留 | 单作者数据量（几十条记录/几十条 trait）远小于拆包收益；分接口给别的消费端用 |
| 来源抽屉再请求一次详情 | 主接口已内联每条来源的引文 | 少一次往返；剧透门已经在这里过滤过，抽屉再查一次要重算门，容易两处不一致 |
| 缓存写操作自动失效 | 5 分钟 TTL，写入口手工 `clear_cache()` | 目前写入口都是独立脚本，不是 HTTP 写；提前做失效钩子没有真实调用方 |
| 覆盖度阈值来自设计稿公式 | 经验阈值（12/6/12/5/6/8） | 设计稿只给了比例，没给「满格」分母；中文作者档案的实际数据量下，这组值让 9 本书的分布能区分开 |
| 关系 `author_relations` 仍无提取写入 | 先给 `add_relation/relations_of` 与档案投影 | M3 只负责**展示**；关系提取在 M5 的网页 facet 管线里补，空表要如实说「还没提取」 |
| `no_dated` 时 `voice.samples=[]` | 声音按 `chapter_index` 施章门，`gate.mode` 仍报 `no_dated` | 实测 9 本有声音的书只有 4 本序跋抽到年份；整页空掉比「只看已读章节」更糟。正文样本有章号，已读章节不是未来内容 |
| M4 推荐表只给 `author_voice` 插位 | 旧块 priority 一并重排 | 旧语义与文档相反；只插新块会让 `drop_order` 继续失效，等于把 bug 留在原地。副作用：未选作者时**块内容一致、可选块顺序变为 priority 降序** |
| 一个作品最多 2 位参考作者 | P1 只取第一位（`author_refs[0]`） | 700 tokens 装不下两种完整风格；并列规则会互相打架。字段保留两位，消费端先取一位，UI 也说「默认」 |
| 风格检查并入审稿结果 | 独立 `style-check` 接口 + 审稿面板单列 | LLM 审稿是「这章有没有问题」，风格对照是「偏离你选的参考多少」；混在一起会让 `severity` 语义打架 |
| 场景卡提供 `scene_type` | 从合同显眼词推断，推断不出留空 | 当前 WriteGraph 没有场景卡字段；宁可少一个加权项，也不让模型猜 |
| 场景/时期是 SQL 硬筛 | 先放宽时期、再放宽场景，并报 warning | 多数作者只有 3–7 类场景，硬筛会出现「选了作者却 0 段例证」（实测 Harari 请求 `introspection` 时为 0）；场景是偏好不是门 |
| 超预算按整行截断到块尾 | 例证可裁，脚注必留 | 预算一紧就切掉「以本书正典为准」这句，等于把软约束读成硬指令；脚注比多一段例证重要 |
| 每章请求体传 `style_ref` | P1 用 query + 作品级默认 | query 可缓存、可调试；生成接口本来就是 GET 式 SSE（无 body） |

---

## 三、未实施（按设计稿 07 的顺序）

- **M5 规模化**：批量队列、导入/提取完成事件（stale 自动标记）、来源缓存、
  外部 `public` 声音、主题/接受史聚类、关系提取写入。
- **M4 留下的小尾巴**：
  - 只会用第一位参考作者；多位时如何分区注入还没定。
  - 场景类型靠合同词推断；等 WriteGraph 有场景卡后换成显式字段。
  - 透镜 P2 的 LLM 复选没做（P1 规则召回足够；空数组合法那条留给 M5）。
- **M3 留下的小尾巴**：`author_relations` / `author_claims` 目前只有读与投影，
  没有提取写入方；档案页会把空表说成「还没提取」，不会假装没有关系。

---

## 四、测试入口

```bash
python scripts/test_author_sources.py      # 来源分级/引文校验/迁移/管线（零 token）
python scripts/test_author_enrich_e2e.py   # 真提示词 + fake provider + 真落库
python scripts/test_author_voice.py        # 声音：可见性/抽样/归一/合并/端到端
python scripts/test_author_dossier.py      # 档案：投影/剧透门/覆盖度/来源/搜索/路由
python scripts/test_author_details.py      # 攻略/依据闸门/钩子/Markdown 清洗（零 token）
python scripts/test_writing_voice.py       # 写作透镜：预算/打分/注入/降级/风格检查
python scripts/test_author_graph.py        # 旧作者层单测（含 agent 深挖通道）
python scripts/check_all.py                # L0 全量（以上已列入 module-tests）

# 声音采集
python scripts/run_voice_harvest.py --list
python scripts/run_voice_harvest.py --book 我与地坛 --dry-run
python scripts/run_voice_harvest.py --author 史铁生
```

---

## 五、采集攻略与细节（2026-11-05）

### 149. 攻略是先验，不是证据

新增 `author_exploration_plan`（`authorgraph/plan.py`）。它回答读之前
「先去哪、花多少、什么先跳过」，**不回答「什么是真的」**。

- 每条都有 `hypothesis / expected_basis / anchor_required /
  expected_value / expected_cost / verification_action`，必须可证伪。
- `list_type` 只有用户能改白/黑；**模型先验一律 `grey`** —— 它只能
  影响预算排序，不能静默跳过一本书。黑名单是**目标级**的：同一 target
  被黑掉后，模型换 hypothesis 重开的新条目也继承 black，绕不过去。
- 身份 `provisional/conflict` 时不生成「某书是自传体」这类具体作品先验，
  只留通用条目（读序跋）。同名会串账，这是不可逆的。
- 规则攻略（`deterministic_plan`）零模型：有序跋就进灰名单；正文只在
  `position=narrator` 或用户标记 `read_as=autobiographical` 时进。
- 模型攻略（`model_plan`）一次调用，只认已入库书目；产出仍写 grey。
- **每个 target 只有一个坑**：`upsert_plan` 按 (author, target) 复用
  已有 `plan_id`，优先保留黑/白名单与已完成的那条；模型换措辞只更新
  假设，不再新增条目。抽取本来就按 target 合并，多条只是页面噪音。
- target 除 `#paratext` / `#body` 外，支持 `#chapter:<标题原词>`：目录里
  点名的自序、附录、年表、致辞即使被导入分析误标成 `body`，也能按标题
  精确读取；关键词必须原样出现在章标题里，由 `chunks_by_title` 取块。

### 150. 观察与钩子分表，依据类型 × 通道

新增 `author_observations` 与 `author_hooks`（`authorgraph/details.py`）。

- 观察收**纹理**：习惯、口癖、居所、身体、关系张力、写作现场、对人的
  判断、反复意象、忌讳。不是百科式生平。
- 每条观察带：`topic / statement / quote / basis / basis_quote /
  speaker / use / confidence`。`quote` 必须逐字命中候选块；找不到就
  重定位，再找不到丢弃并报 `fabricated_quote`。
- `basis` 与 `speaker` 是两道闸：译者序里的「作者自述」降级 `paratext`；
  投射来自译者/编者/出版方 → 丢弃；`paratext` 缺说话人 → `reported`。
- `use` 是第三道闸：投射不能进 `biography` / `roleplay_texture`，只能
  `dossier`；转述不能进 `roleplay_texture`。
- **置信度由代码推导**（`basis + speaker + 锚`），不让模型自报数字。
- 钩子收「提到了但没说清」：`quote / known / missing / why /
  suggested_action`。钩子不是事实，不进扮演/写作/时间轴；关闭时保留
  「从哪条钩子来」的链。`chapter_index`（库 v5）随钩子落库，档案读侧
  据此过剧透门；不知道章号时按「缺锚点不展示」处理。

### 151. 用户标记 `read_as` 是投射锚

`author_books.read_as='autobiographical'` 是用户给的书级锚。它让
`autobiographical_projection` 可以带依据落库，但**不会把投射升格成
传记事实**。没有用户标记或 `basis_quote` 的投射一律降级
`fiction_projection`，`use` 只能是 `dossier`。

### 152. DeepSeek Search/Fetch 作为作者层联网通道

新增 `authorgraph/sources/web_deepseek.py`，复用
`services/web_research_service.py` 的原始接口：

- **Search 只产线索**：`summary/snippet/content` 都不能当引文；要引就
  `fetch` 出正文。
- **Fetch 的 Markdown 先洗净**（标题/强调/链接/代码标记），再用
  `quote_located` 逐字比对 —— 不洗会把 `**` 当成原文。
- `enrich_author` 可按端点自动选 DeepSeek（`deepseek.com` + key），
  否则回退原有的原生搜索 + 本地抓页；两条通道的 `page` 形状一致。
- **主模型不必是 DeepSeek**：控制台按调用点绑定，作者链路可能跑在火山
  或别家。`web_deepseek.resolve()` 先看当前配置，不是 DeepSeek 就去
  控制台连接表里找一条 `deepseek.com` 连接（key 只在内存里用）。找不到
  才回退原生搜索；不静默换供应商。
- 钩子闭合走 `sources/hook_close.py`：`missing` 搜索 → 抓页 → 一页一问
  → 引文逐字命中才写 record/evidence 并 `close_hook`；对不上保持 open。
- 未来新增联网 API 只改适配器，不改验证层；搜索结果/答案类接口默认
  没有证据能力。

### 153. 细节采集是独立进程、按攻略跑

`scripts/run_author_details.py`：独立进程（服务内跑会冻事件循环）。

```bash
python scripts/run_author_details.py --list
python scripts/run_author_details.py --author 史铁生 --plan
python scripts/run_author_details.py --author 史铁生 --extract
python scripts/run_author_details.py --book 小团圆 --read-as autobiographical --extract
python scripts/run_author_details.py --author 史铁生 --close-hooks
python scripts/run_author_details.py --author 阿尔贝·加缪 --plans
python scripts/run_author_details.py --set-plan pl-xxxx black
python scripts/run_author_details.py --all --plan --extract --dry-run
```

`--plans` 列出 `plan_id / list_type / status`；`--set-plan` 是**用户**
改黑白名单的唯一入口（模型永远只能写 grey）。黑名单的攻略连
`--force` 也不跑。

幂等键：观察 `(author, book, chunk, quote, statement)`；钩子
`(author, book, chunk, quote, missing)`；攻略 `(author, book, target,
hypothesis)`。重跑就是恢复机制。**重跑的去重不看模型措辞**：同一原文
+ 同一 topic/basis/use 的观察复用旧行（同一句可支撑不同用途）；同一
原文已有 open 钩子时不再开第二条。正文抽样有三档放行：用户标记
`read_as=autobiographical`；作者以 `narrator` 写非虚构；或攻略预期
是投射且作者以 `narrator`/`avatar` 在场（只产出投射与档案纹理，不升
格成经历）。同一 target 的多条先验**任意一条**满足即可，且只抽样本，
不扫整本。标题含「序/前言/后记/附录/年表/致辞」等的章不参与正文均匀
抽样（避免把误分类的自序当小说正文），要读就用 `#chapter:` 点名。

### 155. 钩子闭合：检索词由模型出，答案只认页面

`run_author_details.py --close-hooks` 先调一次 `plan_queries`，把全部
开放钩子交给模型出 1–2 组**短关键词**，再逐钩子搜索/抓页/一问一答。

- **训练知识只管「去哪搜」**：人名、篇名、地名、机构名这类候选专名
  允许写进检索词（「柏大夫」→「柏晓利」），但结论不许写进去。候选
  专名是去核对，不是断言。
- **确定性兜底**：模型漏掉原标题专名时，再补一条「作者名 + 缺口首句」
  （按标点截断），插在模型第一组之后 —— 抓页有总数上限，兜底词不能
  被挤到队尾。
- 多组检索词的结果按 canonical URL 去重，抓页总数有上限（默认
  每词 5 条、总计 10 页）；tier ≥ 4 的线索级页面一律不抓。
- 一页一问只认这一页正文；`answered=true` 但引文对不上 → `quote_fail`
  并保持 open。**部分补全也算闭合**，`note` 写明还缺什么；钩子不是
  账本，闭合后仍保留原话与证据链。
- 跑不掉的钩子就保持 open —— 它是缺口索引，不是待办欠款。

### 157. 作者细节接口化：攻略可见，长任务独立进程

作者层的触发点是用户驱动；接口化只是把终端里的四个动作搬到作者档案页：

- **攻略先变成可见可审的东西**。`GET /api/authors/{id}/plans` 返回全部
  攻略（含黑名单，带书名与 pending 计数）；`PATCH .../plans/{plan_id}`
  是页面改名单的唯一入口，只收 `grey/white/black`。不做「模型建议按钮」
  —— 模型的先验本来就只能是 grey。
- **三个动作走后台任务**：`POST .../jobs` 收 `action`（`plan` /
  `extract` / `close_hooks`）和 `force`，起独立子进程跑
  `scripts/run_author_details.py`，不在 aiohttp 事件循环里调模型（与
  图谱提取同一条教训）。返回 `202 + job_id`，前端轮询
  `GET /api/authors/jobs/{job_id}`；状态机只有
  `running/done/failed/cancelled`。
- **串行**：同一时间只允许一个采集任务。子进程之间会抢 SQLite，
  而这是低频、用户主动触发的操作，不需要并发。已有任务在跑时返回
  `409` 并把当前任务带回去，让页面接着显示它的进度。
- **进度只报事实**：stdout 逐行进内存环形缓冲 + 落
  `data/logs/author_jobs/<job_id>.log`；结束行解析成摘要
  （观察/钩子/闭合/失败）。任务记录只在内存，服务重启后历史任务消失
  —— 日志还在，但不假装能恢复。
- **页面**：细节页签顶部三个按钮 + 「重跑已完成」；攻略列表按书分组，
  灰/白/黑三态可点；运行中显示日志尾部与「取消」。任务结束后自动
  重取档案（保持当前页签），观察与钩子即时出现。轮询 2 秒，不用 SSE：
  低频任务不值得再维护一条流。
