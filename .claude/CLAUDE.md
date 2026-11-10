<!-- LLM_CUTOFF -->
# Liubai - Claude Code 认路文件

> 每次会话启动时自动加载。结构细节用 codegraph 按需检索，决策论证按域拆在 `.claude/docs/`，
> 本文件只保留认路骨架、行为契约和决策索引。

---

## 一、项目身份

| 项目 | 值 |
|------|-----|
| 名称 | Liubai（留白） |
| 定位 | 基于 AI 的 CLI 阅读陪伴助手 |
| 语言 | Python 3.12 |
| 核心依赖 | asyncio + prompt_toolkit + aiohttp + OpenAI 兼容 API |
| 设计来源 | 原创为主；第三方参考与版权致谢见根目录 NOTICE |

运行：`python main.py` 或项目根目录 `.bat`。

---

## 二、架构骨架

```
启动: main.py -> config -> BookDB+SessionDB -> SessionService -> memory_store -> session -> persona -> Skills -> runtime
对话: 输入 -> /cmd(commands.py) 或 runtime.run_turn()
         -> stream LLM + ReAct (ToolExposure 四级 + BM25 tool_search + sub_agent 白名单)
         -> MemoryManager pipeline (prefetch + memory-context 注入 + sync_turn)
         -> compact (无 LLM) -> SQLite 持久化
提示词: prompts/*.md 模板; 指令文件注入已关闭 (LLM_CUTOFF 屏蔽)
记忆: MemoryStore(文件级 §分隔) + MemoryManager(编排) -> WingmanMemoryProvider(memory.db); L0-L3 管线 (L1原子/L2场景/L3画像)
图谱: Scout -> 骨架趟(skeleton) -> Explorer(逐批) -> 裁决环(adjudicate) -> Recorder -> graph.db -> formatter.py(角色信息注入) + graph-viz.js(前端)
阅读: reading/(prompts/tools/store/session/map) -> 逐章细读(工具+剧透门) -> notes+compact(reading.db) + 认路地图(逐章投影/卷级合成) -> scene_view/roleplay_kernel; 图谱退为投影
作者: 序跋采集(harvest) -> 台账(authors.db); 网页补全 -> sources/(计划→抓页→一页一抽→逐字校验→合并) -> source/evidence/record
      声音采集 -> voice/(可见性→分层抽样→1–2次小调用→trait) -> corpus/voice_traits（不写 record）
      细节采集 -> plan/(攻略：序跋/正文/点名章) -> details/(观察+钩子) -> sources/hook_close(联网闭合) -> authors.db; 页面走 jobs + 独立进程
      作者档案 -> dossier/(纯投影：身份→年谱→作品→声音→关系→主题→来源) -> /api/authors/* + reader/js/author-dossier.js
      写作透镜 -> voice/select(合规候选) -> writegraph/voice_lens(场景打分/去重) -> voice_card(700 tokens) -> budget 可选块
音色: voicedesign 铸造 -> voiceclone 复制 (cast+replicate); voice_roles 持久化; role-play 模式注入
```

子 Agent：同步 + 复用 Runtime + 白名单过滤 + 隔离 Session。Reader v1 仅 4 个读书工具。

---

## 三、核心模块入口

| 域 | 入口文件 | 存储 |
|----|----------|------|
| 主引擎 | main.py / runtime.py / tools/ (按 toolset 分包, 组合根 builtin.py) / commands.py / compact.py | - |
| HTTP 层 | api_server.py (门面+组合根) / api/routes/ (chat/graph/books/sessions/sync/writing/voice/console/annotations/avatar/authors/user) / api/author_jobs.py (作者采集后台任务) / api/sse.py / api/prompt_builders.py / api/text_helpers.py / api/book_helpers.py | - |
| 会话 | session.py / services/session_service.py | sessions.db |
| 书库 | services/library_service.py / epub_import_service.py / file_readers/ | books.db |
| 导入分析 | services/import_analysis_service.py (立论→证伪→回炉) / structure_invariants.py (机械校验) / file_readers/style_resolver.py (有效排版) + book_proof.py (样张+切分实测) | - |
| 形态回流 | services/chunk_annotation.py (逐段渲染 `«形态名»` 进提取提示词) / scripts/backfill_chunk_sigs.py (补旧块签名) | - |
| LLM JSON 解析 | llm_json.py (宽容阶梯 + 裸引号修复；bookgraph/memory_pipeline/archive/services/wingman 共 12 处共用) | - |
| 记忆 | memory_store.py / memory_manager.py / memory_pipeline/ (L1/L2/L3) / wingman/service.py | memory.db |
| 图谱 | bookgraph/ (orchestrator/scope/selection/verify/invariants/llm_call) / storage/graph_db.py | graph.db |
| 阅读会话 | reading/ (prompts/tools/store/session) / scripts/run_reading_session.py + inspect_reading_run.py (独立进程) | reading.db |
| 写作 | services/writing_service.py / writegraph/ (card/facts/lens/budget/state/versioning/docs/memory_write/memory_guard) / writegraph/chat_tools.py (harness 工具) / api/routes/writing.py / reader/js/writing.js | writing.db + data/writing/ (works/ 工作区) |
| 语义召回 | services/embedding_service.py (控制台 embedding 槽位) / writegraph/semantic.py (余弦近邻) / storage/embedding_db.py / scripts/build_embedding_index.py | data/embeddings.db |
| 作者层 | authorgraph/ (harvest/store/refresh/card/gate/identity/dossier/coverage/plan/details) + sources/ (registry/query_plan/fetch/extract/verify/merge/pipeline/hook_close) + voice/ (scope/sample/extract/normalize/traits/store/select/pipeline) / api/author_jobs.py + api/routes/authors.py / writegraph/voice_lens+voice_card+voice_check / reader/js/author-dossier.js / storage/authors_db.py | authors.db |
| 图谱运维 | scripts/run_graph_extraction.py (独立进程驱动，服务内跑会冻事件循环) / repair_graph.py / scope_report.py / graph_invariants.py | - |
| 音色 | voices.py / tools/voice.py (tts_speak, voice_design) | memory.db (voice_roles) |
| 前端 | reader/js/ (24 模块: app/chat/reader/shelf/roleplay/mode/sessions/console/usage/binding/writing/graph-viz/graph-progress-ui/context-usage 等，按功能拆分) | - |
| 配置 | config.py / persona.py / prompts/ / context_files.py | - |
| 上下文计量 | context_meter.py / tokenizer_service.py / scripts/prompt_budget.py | 圆环数据源 |
| 控制台 | provider_store.py / api_server (/api/console/*) / reader/js/console.js | data/config/providers.json |
| 联网搜索 | services/web_search_service.py (原生 web_search 主路 + Tavily 兜底) / services/web_research_service.py (写作: DeepSeek Search+Fetch) | - |

> 详细结构、调用关系、符号定位用 codegraph 检索（`codegraph_context` / `codegraph_search` / `codegraph_trace`）。
> 设计决策的**论证**按域拆在 `.claude/docs/`（graph / reading / reader / import / llm / memory / platform / author）；动手改某域前先读对应文档，索引见 §四。

---

## 四、已定案的设计决策（索引）

> **不要推翻，除非有强有力的新理由。** 这里只有结论，论证在域文档里——
> **动手改某个域之前，先读该域文档的全文。**
> 新增决策：先写进域文档，再在这里加一行（只加名字+结论，不复制理由）。

### 平台与运行时域 → [`.claude/docs/platform.md`](docs/platform.md)（28 条）

- `01` **异步模型** — 全 asyncio
- `02` **上下文压缩** — CLI 纯结构化；Web chat 走 123 的事件压缩（LLM 交接摘要 + 机械兜底）
- `04` **会话持久化** — SQLite 三层：sessions/messages/parts
- `05` **三种模式** — immersive / overview / divergent
- `06` **子 Agent** — 同步 + 复用 Runtime + 白名单隔离
- `07` **工具暴露** — ToolExposure 四级 + BM25 tool_search
- `08` **工具 schema 的暴露层裁剪** — 导入探针与「按路径读未入库文件」的工具对主 agent 一律 DEFERRED
- `09` **提示词管理** — prompts/*.md 模板，代码只留决策逻辑
- `10` **指令文件注入** — 已关闭（CLAUDE.md 与留白 agent 无关）
- `11` **延迟会话创建** — 首条消息才写 DB，消除空会话
- `12` **TBD 审查** — time_reviewed=0 哨兵，拆表时升级 review_status
- `14` **数据库分库** — books.db + sessions.db + memory.db + graph.db，共享 BaseDatabase
- `67` **音色消费场景** — 仅角色扮演模式 agent 语音输出，阅读 TTS 已砍
- `68` **TTS 同 key** — tts_api_key/base 默认空，回退 api_key/base
- `69` **音色铸造** — voicedesign 铸造一次 + voiceclone 多次复制（支持标签）
- `70` **角色音色持久化** — voice_roles 表（book_id+character → voice_id+prompt）
- `74` **上下文计量** — 总量取末轮 usage（权威），明细按本地占比，预警线 0.35
- `75` **分词器选路** — 模型覆盖 → 厂商别名表 → 全局兜底 → chars//4 估算
- `83` **模型绑定路由** — ProviderStore v3 的 routes：每个调用点绑(连接+模型)，点号分级 + 父级继承；控制台「模型绑定」页；调用点取配置视图，绝不按调用改全局 Config
- `87` **联网搜索** — 统一入口 `services/web_search_service.py`：DeepSeek 原生 `web_search_20250305`（Anthropic 兼容端点）主路 + Tavily 兜底；URL 只认服务端结果块；模型名剥 `[1m]`；Claude Code 的 WebSearch 走 haiku 槽位，base 是 DeepSeek 时 haiku 不能映射成 glm-*
- `88` **用户主动打断** — `turn_id` 显式取消 + 本地冻结兜底；部分回复 + `interrupt` 注记落盘，下一轮由 `_history_from_session` 回灌；中断轮不写记忆/图谱
- `89` **HTTP 层与工具包拆分** — api_server 只留组合根 + 门面，handler 进 api/routes/*；tools.py 改 tools/ 包按 toolset 分；路由注册顺序不可重排、路径用 `_APP_ROOT`、指纹覆盖 `api/**`
- `90` **写作 agent 的轮次与上下文经济** — 当前作品的版本现场进 system prompt（**不再注入「最近的自测作品」**，自测现场用 `writing_git(status)` 现查）；变更工具返回精简 status + 改动章首尾预览；自测清单按 phase 取；写作 ReAct 上限 40；真实自测 22/22、0 issue
- `91` **Web chat 上下文管理** — 工具调用/结果成对落盘；token 预算在 user 回合边界裁剪；老回合压成机械摘要只进请求不改库；真实会话 0 撞顶
- `123` **Web chat 压缩：事件式 checkpoint** — `session_compactions` 存 seq/raw_count/摘要；两次压缩间摘要逐字不变吃满前缀缓存；超预算才调 `chat.compact` 生成交接摘要，失败退机械摘要；预留/预算对齐、失效 checkpoint 作废、反复压缩告警
- `147` **日志的生命周期** — `log_lifecycle` 统一写入：观测流水中等轮转（5MB×3，启动删 30 天前轮转份），`reader_sync.log` 改 `BC_READER_SYNC_TRACE=1` 才写；审计投影缩预览 + 10MB×3（测试不再写生产）；DB 型（context_log / graph_llm_logs）走 `scripts/log_maintenance.py` 按年龄回收，默认演练
- `154` **自我说明层** — 只读 `self_explain`：原子主题卡（是什么/在哪/常见误区）+ 本机探针（provider/semantic/authors/vcs）；卡片禁写状态，探针现算且不联网；写作页要显式加入白名单并挂请求级现场；扮演模式禁用；命中 `danger` 只解释并请求确认，绝不同轮执行
- `184` **打包：程序根 / 用户根分离 + Web-only 冻结入口** — `runtime_paths.py` 定两个根（程序根只读资源 / 用户根 `%LOCALAPPDATA%\LiuBai`，`BC_HOME` 与 `portable.flag` 可覆盖）；`Config.project_dir` = 用户根、`resource_dir` = 程序根；冻结入口 `desktop.py`（Web-only，单实例复用，默认 pywebview/WebView2 原生窗口 + 关窗即退出，起不来退 Edge `--app` → 浏览器，`--app-window` 显式选 Edge），同一 exe 用 `--run-script` 跑 `scripts/*`；升级只换程序目录，首次运行仅缺失时播种 SOUL（settings.json 属个人配置，不打包不播种）；`packaging/` 存 spec/iss/build.ps1，产物不入库；发行包不带字体
- `170` **书籍在库状态日志** — `import_log` 即事件流：两条入库路径与删除都写（删除后保留书名/作者/稳定书号，无活动行也记）；重导复用稳定书号并累加 `import_count`；查询走 `list_import_log_events`，agent 只读工具 `library_event_log`（DEFERRED）

### 图谱域 → [`.claude/docs/graph.md`](docs/graph.md)（41 条）

- `26` **BookGraph** — Scout → Explorer → Recorder + SSE
- `27` **叙事域划分** — 目录判集类 × 机械守卫，两份都点头才划，否则单域
- `28` **叙事域的作用范围** — 只做两件事：批次不跨域 + user message 带域简报
- `29` **提取提示词** — 五课教学口吻，代码只留最小兜底
- `30` **实体准入（孤儿）** — 只给「被引用的声明实体」建节点，否则进缺陷清单
- `31` **实体准入（悬空边）** — 被引用却没声明的名字兜底建 kind=unknown + 上报，不丢边
- `32` **kind 去重 + 词表** — 同名跨 kind 复用 upsert_node；变体归一，词表外原样保留并上报
- `33` **提取回炉环 → 裁决环** — 不再重提整批：提取一次 → 机械修补 → 断言级判决 → 机械执行
- `34` **骨架趟** — 逐批提取前先看整本，只产出规范名/别名/身份/结构关系
- `35` **骨架身份不写结局** — identity 只写一出场就成立的事；可疑项带锚点章号报
- `36` **边去重** — 键为 (project, src, tgt, kind, event_type)，重复只计数、锚点取更早块
- `37` **提示词示例必须是虚构的** — 三个图谱提示词一律用虚构角色，禁真实书目、真名和章号
- `38` **提示词不许有体裁偏向** — 实体清单按体裁分档；论说类收概念与机构，relations 可空
- `39` **限流不是抖动** — 429 走 20/45/90/180/300 秒退避，重试放宽到 6–8
- `40` **机械体检要送进裁决** — 证据核不上以 ⚠ 标注进裁决，裁决者用 rewrite 改证据而非 drop
- `41` **被拒的修改要回灌一次** — 拒收后再问一轮（只补一次不递归），旧编号作废
- `42` **飞行记录仪存思维链** — graph_llm_logs.reasoning_content 存思维链正文
- `43` **空骨架不是成功** — entities/relations 皆空 → ok=False + 失败标签
- `44` **身份说明先写者赢** — upsert_node 的 description 默认只填空不覆盖
- `45` **角色卡的关键关系** — 按持久关系 → 对方度数 → 次数 → 章号取前三，同一对方最多两条
- `46` **关系词表** — 骨架与批次共用 RELATION_KINDS，边只写跨场景成立的关系
- `47` **书名号变体** — 同 kind 下 《X》 与 X 视为同一实体，跨 kind 不合并
- `48` **缺陷分阻塞/提示** — 只有阻塞项触发回炉；选轮按 (阻塞, 质疑, -内容量) 字典序
- `49` **引文共用锚点判据** — 多引文同锚点且至少一条不在该块才算缺陷
- `50` **逐字证据** — evidence_text 列 + 无锚点/无原文/原文不在块 三层检查
- `51` **锚点首现块** — 导出量，由 recorder 逐块扫正文算出，不让模型猜
- `52` **实体重要级** — 不落库：只有度数一种形态，现算现用且必须篇内相对
- `53` **读者笔记** — 独立 graph_notes 表，node_id 可空，生成侧只给本篇正文
- `54` **索引 detailed 层** — 就近档只钉 character，其余按度数 + 观察数排
- `55` **读取路径的名字解析** — locate_node：精确别名 → 精确正名 → 子串度数优先
- `56` **存量 kind 归一** — 只在写入生效；旧库靠 repair_graph --kinds，且必须先 --merge
- `57` **`merge_nodes` 的搬运范围** — 边+别名+观察+笔记+首现块全搬；计数用 rowcount 不用 total_changes
- `58` **`nodes_fts` 列名** — 必须叫 id（= nodes.id），不能叫 node_id
- `59` **剧透门** — 上限为 0-based chapter_index，api_server 唯一计算；无锚点一律不展示
- `60` **搜索不含章节节点** — search_nodes / search_all 一律排除 kind='chapter'
- `61` **图谱观察召回** — 先分词，实体优先 + 关键词兜底两趟，每实体/每词最多 2 条
- `62` **图谱输出预算** — 统一走 output_budget，finish_reason=length 报 truncated 并抬预算重试
- `63` **悬空引用** — 被引用却没声明、库里也没有 → unresolved_reference 回炉
- `65` **角色信息注入** — format_role_kernel() 纯函数 → system prompt，废 LLM 生成 + 文件缓存
- `66` **图谱存储** — graph.db：nodes / edges / aliases / quotes / merge_candidates
- `81` **chunk 简述** — book_chunks.summary，由 Explorer 顺手产出，有简述就不回原文片段

### 阅读器与前端域 → [`.claude/docs/reader.md`](docs/reader.md)（58 条）

- `71` **前端模块化** — reader/js/ 14 模块，按功能拆分
- `76` **跳转与恢复的优先级** — 显式跳转统一走 jumpToPosition()，且先于恢复链
- `77` **display 的完成信号** — 一律走 _displayWithTimeout() 有界等待，不裸 await
- `78` **视口指纹跨块** — 单块包含 miss 后按章拼接、定位偏移、映射回块
- `79` **图谱节点跳转** — 节点面板「跳转原文」→ jumpToPosition(bookId, chapter, chunk)
- `80` **分支切换与上下文一致** — 切换=整会话替换；锚点=fork 那条消息；后端按 session_id 回读重建 history
- `82` **阅读位置真相源** — sync 分开收恢复锚/阅读意图/可见章；结构定章，指纹只在该章内定块
- `84` **会话按页签分线** — 主页/阅读/写作三条线，本日恢复，换书重开；后端按 page+mode+project_id 拒绝跨线续写
- `85` **空会话预选阅读模式** — 三档分段只出现在阅读页空白会话的聊天框里，首条消息后消失并冻结该会话模式；新会话固定托盘第一位；导入书籍归书架标题旁
- `86` **阅读页默认书** — 无当前书时开「继续阅读」那本（pickContinueBook），不是下拉框第一项；bc_view 选书后再落盘，reader/bookId:null 也恢复阅读页
- `118` **写作页版本图** — 历史按 git log --graph 摊成泳道；第二父节点另起一列，单章过滤改置灰（`mark_chapter`）不断线；点提交展开改动清单（章/文件 + 中文状态，点开看单份 diff；账本单独一行走整条提交），泳道续线按 `after` 画满展开块
- `119` **写作页只做写** — 合同 / 上下文装配 / 结算从页面移除，交给写作 harness；侧栏只留审稿与版本
- `120` **写作轮次的机械验收与长流** — 终止要有后置条件（说了没写/工具没跑完/写了没结算→回灌续轮，上限 2）；空消息异常补类型名；流超时按 `total`+`sock_read` 分档 + `: ping` 心跳；`finish_reason=length` 抛截断并按预算翻倍重写；进度走 `tool_progress`（draft 阶段/字数、settling）；撞 40 轮发 `round_limit`；变更类工具后重拍写作现场
- `121` **结算的上下文与处方** — `settle()` 必须带「核心角色状态」（`facts.current` 求值后的主体 id，空账本明说进 entity_ops）；`validate_delta` 拒绝时给出可用 id + 处方；guard 只对引用/格式类拒绝催一次，证据类不催
- `122` **引用的出处跟页面走** — 阅读页「书中原文 / 对话」，写作页「正文 / 对话」；两页都是「选中即引用、选区没了引用就没了」（点 ×／点别处／换章换书／点导航栏），× 走 `mousedown`、触发面积 1.5×；对话引用只认**同一个气泡内**的选区（开场招呼语/时间戳/提示条不算，跨气泡拖选不算），× 的撤销不会被同一段选区的 mouseup 装回去；写作页读 textarea 选区 + 失焦后靠 `.write-veil` 镜像补画、松手把光标送进聊天框，发送前复核锚点、上限 1200 字；阅读页用 CSS Custom Highlight 常驻高亮、不抢焦点（Ctrl+C 要紧）；后端只认 `writing`/`workspace`/`chat` 三种写作 source
- `125` **写作台左栏是两个导航** — 作品（书名，项目浏览器：`正文/` + `工作区/`）与版本控制（VS Code 式 SCM 面板）；顶部第一行是独立的「切换作品」下拉（与「新建作品」同高），书名 tab 只切视图不换书；合同不做文件夹（章节的派生投影），章行挂方形徽章，整本审阅走「合同总览」；选中节点 ≠ 当前写作章；头部与风格条按视图显示
- `126` **草稿是作品工作区** — `data/writing/works/<id>/` 四块普通文件：`作品记忆.md`（只放身份与约定，全文进提示词，上限 3000 字）、`设定/`（提示词只带索引、按需读；写稿走 `work_docs` 预算块）、`计划稿/`（不进上下文）、`素材/`（项目外上下文，不自动召回）；目录用中文名、页面不显示 `.md` 后缀；写入只认这四处且只收 .md，页面可做文件管理（新建/改名/删除，决策 135），正文写归 `writing_docs`
- `127` **作品工作区进写作 git** — 提交存文件 hash 清单（`commits.files_hash` + `commit_files` + `work_file_blobs`）；有未提交设定也算不干净；回滚/切分支/合并/暂存都回放文件，冲突走 `kind=file`；旧提交无清单时不动磁盘
- `128` **合同是验收单** — 合同有 `active/closed` 生命周期：无作者批准、一章一约、改约即重新立约；结算时 agent 逐条申报 `outcomes`（只存不判），结清后 system prompt 只留一行；上一章未兑现项带进下一章现场
- `129` **写作工作区的四条不变量** — 单一归属 / 变化频率定位置 / 发现与检索分离 / 可推导不手写；索引不许静默截断，`writing_docs` 要有 `search`
- `130` **素材库是项目外上下文的合并项** — 与设定/计划稿同级根；不自动召回，作者与 agent 一起找合并项；跨库只读且只认 `素材/`；命中进 `external_reference`
- `131` **设定是作者 demo 的现形** — 交谈后沉淀，不导入不自动抽取；硬事实归账本、设定只收纹理（域分离，不用「以账本为准」兜底）
- `132` **计划的三条去向与弧线指针** — 落地顶注并入设定 / 废弃标注 / 弧段边界对账；合同顶部挂「当前弧」引用，draft 需要时一次读
- `133` **项目记忆维护走两段式** — 抽取(逐章, no-op 门) → 合并(只写 作品记忆/设定，机械闸门 + 文件 hash diff 判遗忘)；`writing_memory` 由 agent 在结算后显式调用，不藏在 settle 里；settle 只把本章标 dirty，下一轮 user 消息用 `<memory-pending>` 提醒，抽取成功才清脏
- `134` **沉浸阅读** — 单击正文进出；只留正文与底部百分比；`html.reading-focus` + CSS 驱动，进入延迟 300ms 防双击误触；版心默认 54em、主题页可调（36–72）；底栏同源调色 + 约 1.75 行缓入淡出（随字号缩放；不做正文模糊，罩整行会糊成重影），示数居中、用正文墨色；进出用 View Transitions 共享元素（书页放大到全屏、退反倒放；pending 阶段冻结 rAF，重排放在 ready 之后）
- `135` **工作区右键与时间线** — 文件/文件夹右键：新建文件 / 新建文件夹 / 重命名 / 添加到聊天 / 删除；页面只做文件管理，正文仍归 `writing_docs`；分区根受保护、文件夹删除递归且确认；文件「打开时间线」= 第一父节点的 git 历史 + 置顶 WORK 条，点开单文件逐行 diff；「添加到聊天」传路径，模型先 read/list
- `136` **文件区 git 状态徽章** — 界面是中文胶囊（已修改 / 未跟踪 / 已删除），agent 侧仍用 M/U/D 字母（无暂存区，所以没有 A）；胶囊只做悬停说明、点击无反馈；状态由 `vcs_status.file_changes` 统一计算，页面与 agent 同源；工具结束即刷新；版本控制栏点文件 → 中间栏单文件 diff
- `137` **版本 diff 的双行号与整行底色** — 旧行号 | 新行号两列：增行只走新号、删行只走旧号、上下文两边都走；增行整行浅绿、删行浅红（夜间换深色档）；长行折行不横滚（一条逻辑行可占多视觉行，行号只标第一行）；`---`/`+++` 文件头不渲染，`@@` 单独一行
- `138` **空项目** — 写作页下拉顶部固定「空项目」：不绑定作品、对话照常走写作模式、`workId` 置空且本地记住；服务端 `agent_prompt` 不再回退 `works[0]`，**连作品目录也不注入**（只报数量，书名/id 要看时调 `writing_status`），章节/工作区/版本现场一律不注入；左栏版本控制药丸与头部分支徽章一并收起（素材库不进 git）；工具报「建书或选书」，助手建了书自动退出空项目
- `139` **写作会话按作品收录** — 写作页会话面板只留「全部 / 按作品」两个页签（平铺列表挂作品名；按作品分组卡片，空项目垫底）；其它页面的列表与搜索排除写作会话、写作页签移出 DOM；`sessions.work_id`（迁移 14）+ 每轮重绑；换作品=显式结束写作会话线，会话线指针记 `workId` 并恢复
- `140` **批注气泡与正文指回** — 点角标时被批注的那段文字临时画蓝下划线（epub.js underline，与角标同色），气泡不再抄原文；换角标/点空白/滚屏/Esc 撤线，编辑面板打开期间保留；空白收起兜底挂 iframe 的 `pointerdown`，并让沉浸阅读的 overlay grace 吞掉这一下
- `141` **重排前后复位连续补载队列** — `rendition.resize()` 会同步 `clear()` 掉在途 check 正在 display 的 view，Promise 永久 pending 卡死 `manager.q`；前后各复位一次队列并由 `_kickContinuousFill()` 重跑 check，否则收起/展开交互页后翻不动
- `142` **写作工作流：阶段诊断 + 行为簇** — 认知段（世界/人物/定调/搭骨）是菜单不是流水线，落章段半线性，机械段（结算/校验/提交）仍线性；世界层落 `设定/世界.md`（六 facet 起步，可加；定/悬/隐 + 可见性），素材四源分级（`writing_research` 跨书查一条），减法靠合同 `withhold` 与结算「读者新知道」；现场加「现在在哪」（已有/缺/可做/建议/读者已知）
- `143` **阅读进度行级锚** — 抓锚取视口顶第一个完整可见行的行首字符偏移（CFI `:N`），恢复沿用 display(cfi)+_scrollToCfi；同一 chunk 内 cfi 原地更新最后一条记录（空 cfi 不覆盖），跨块才 append；行首按 `top >= 视口顶` 取，避免 24px 边距造成每次重开上退一行的棘轮
- `144` **阅读页注入视窗文字** — 发消息时前端抓视口内实际渲染的文字（按行裁切、跨 section 拼接）随 `reading_viewport` 发送；后端 `<current-passage>` 优先用它并按可见章对齐 chunk 边界，逐段标「第 x/y 块 · 块内约 a%」；跨块/跨章写明跨度，对齐失败原样送出并注明边界未知；结构映射只当候选（部分书 spine_index 与渲染器差位）
- `145` **写作联网：DeepSeek Search/Fetch** — `services/web_research_service.py` 直连 DeepSeek `/v1/tools/search|fetch`，只认 deepseek.com 端点；`writing_research` 支持 `source=web|auto`、`url`、`enable_web`、`save`；联网须作者授权（作品开关或本轮 `enable_web=true`，代码拒绝未授权），每轮最多 2 次，结果可落 `素材/网络/`，摘要不冒充逐字引文
- `146` **章表 spine_index 存 OPF 坐标** — 渲染器 sectionIndex = OPF itemref 原始序号；reader 跳前置页后的压缩坐标只供内部用。`_spine_opf_index` + `opf_spine_index()` 换算落库，历史库跑 `scripts/backfill_spine_index.py` 回填（默认演练，`--apply` 先备份；内容投票判坐标、旧 -1 行唯一命中反推，幂等）
- `148` **语义召回走 embedding 槽位，向量独立成库** — `writing_research(source="semantic")` 余弦近邻查书块，端点取控制台 embedding 槽位（无绑定退 `EMBEDDING_*`）；向量存 `data/embeddings.db`（chunk_id+model 主键、content_hash 增量），不进 books.db；`scripts/build_embedding_index.py` 增量建库（每批落库、断点续跑）；模型不匹配返回空并说明；语义必须显式选，`auto` 走素材库→关键词→联网
- `156` **全局素材库：文件做原料仓，索引记录消费** — `data/writing/materials/` 跨作品、不进写作 git、不自动召回；一卡一 `.md`，摘录哈希作身份，主分类树 + 标签、不写适用情景；生产两条主路（作者点名收录 / 写作中用上的一段顺手沉淀），索引粒度到段（`read_chunk` 编号 → `para_start/para_end`，原文由服务端切）；消费走 `writing.db` 的 `material_uses` 连线表（shortlist/merge/inline + 改编说明），不是分支也不是消费目录；提升是复制进作品 `素材/`，删除进回收站；`writing_research(source="materials")`，自动顺序为 正典→素材库→已入库书→训练知识→联网
- `157` **素材库维护：回收站闭环、改名换分类、审计只报告** — 回收站带 `manifest.json` 记原始路径，`restore` 回原分类、`purge_recycled` 默认 90 天且只在显式动作里跑（页面垃圾桶 = 清空回收站，传 `all=true` 并先确认）；回收站挂在素材库组末尾，缩进参考线只罩分类；`move_entry` 换分类保身份（删了重收会换身份、断消费连线），agent 工具 rename/move、页面右键；文件夹右键「清空文件夹」把里头（含子分类）的卡逐张送回收站，文件夹与子分类保留；`audit` 只报告未消费/近重复/身份重复/坏抬头/原文改过（身份对不上）/回收站积压，近重复含「短摘句被同书整块卡包含」并给出 keep/recycle 建议，待归类超期与回收站预览也只报告；合并与清理都要作者点头；借鉴的是维护骨架（显式触发、水位、保留期、报告与执行分离），不是自动合并
- `158` **参考素材由 agent 选坐标，服务端只读原文** — 删掉写稿前自动召回+蒸馏的隐藏管线；`writing_draft_chapter(refs=[...])` 按书块坐标或素材卡身份读回逐字原文注入 `external_reference`，不摘要、不合并、不设本数帽；`writing_references` 只是书架/检索范围，`writing_research(book=…)` 限定书库；预算只剩装配器总预算，manifest 回报截断/丢弃；并行子代理见 159
- `159` **素材侦察子代理只检索、只回坐标** — `writing_researcher` 白名单到 `writing_research` 一个工具（`include_tools` 收口），写作页请求级重注册 `Agent`（基础闭包看不到写作工具）；`streaming=False` 不把子代理中间话回灌主上下文；结果经 `loads_tolerant` + `resolve_refs` 试读，坏坐标单列 `dropped`；`resolve_refs(work_id=)` 支持本书 `素材/` 路径
- `160` **写作页回合时间线** — 工具调用三层折叠（回合头 / 步骤流 / 输出），流式展开、结束自动收起（作者动过就不收）；写作页 only，刷新用 `steps` 回放
- `162` **古早批注按原文找回位置** — 无 `cfi_range` 的旧批注按 `selected_text` 现算 CFI（上下文消歧、不唯一就不猜），不写回库；删除统一走 DELETE + `bcAnnotationDeleted` 同步正文层
- `161` **细致模式抬证据门槛不换流水线** — 写作页分段药丸「高效 / 细致」（后端 `standard/detail`），空白会话可选、开始对话后固定；细致只改三处：动笔前必交 `research_brief`（只验形状、不设配额）、装配预算 6000→9000、review 带上 brief；单章可覆盖；模式归作者，`writing_set_mode` 未授权只返回 `needs_authorization`，`remember` 必须同时授权；brief 入库不进 git
- `163` **收藏两种呈现：框选与下划线** — 悬停浮动条「收藏」向上弹出样式菜单（WAAPI 动画，触屏先出菜单）；两种样式都走自绘 svg 层（epub.js 的 Underline 跨段会退化成整栏块框、重放还会叠线）：框选取行框并集画**一个**圆角框，下划线逐文本节点取行框贴字画线，命中区只在边框/线附近，正文照常划词；`annotations.style` 只认 `underline|box`，空值照样机下划线，删改整层重画；正文屏蔽浏览器原生右键菜单（Edge mini menu 会盖住浮动条），聊天/笔记不受影响
- `164` **素材进对话先挂附件** — 「添加到会话」只往输入框上方挂胶囊（可多张、× 可撤、同 id/路径去重、只露前四字），按发送才随消息送出并清空；消息里传事实块（id/标题/分类/路径），怎么读/提升/记 use 交给写作提示词；只挂素材不打字也可发；卡片抬头只留标题、动作按钮成组靠右；抬头里的身份/会话 id 渲染成复制按钮（悬停「复制素材id / 复制会话id」），文件仍是纯 Markdown
- `165` **收录为素材：原始摘句落待归类，纸团飞向写作导航** — 浮动条新增入口，逐字原文存 `待归类/《书名》-YYYYMMDD-HHMMSS.md`（`intake_raw` 不去重，与加工卡分开）；后端按 book_id 取书名，章/块只在 chunk 模式传；纸团从选区内随机一点沿二次贝塞尔飞向 `#navWrite`，落地淡出 + 图标击中动画，减动效时不起飞
- `166` **笔记页：全屏两栏 + 服务端查询 + 5 秒撤销** — 整页 grid：左栏是导航列——顶部「批注 / 收藏」两段开关（先选类型再选书）+ 书籍排序菜单（与书架共用 `bc_shelf_sort`，五项、同 key 再点翻方向），下方书籍泳道（封面 + 书名 + 条数，`全部书籍`钉顶且与类型开关同宽内缩）；点一本收窄列表；正文页头大标题「阅读历程」，第二行排序靠左、窄搜索框（约 560px）靠右 + 总数；批注/收藏共用四种排序（A→Z / 最新 / 长→短 / 章节↑↓，章节再点翻方向），收藏平铺只改顺序不做分组；过滤/排序/分页交给 `query_annotations`（`q`/`sort`/`limit`/`offset` + `total`，`chapter` 兼容旧调用方），不再「拉 500 条前端过滤」；书→章分组只在按书/章排序时用；删除先隐藏、5 秒后才真发 DELETE，toast 可撤销；导出读数据不读 DOM；搜索响应按 seq 丢弃过期，老进程退回一次拉 500；**阅读页浮窗是「本书笔记」**（按钮 title 与抬头统一，按 `stable_book_id` 取本书并前端筛笔记；旧进程漏过滤时退 book-scoped 重取；换书自动刷新且旧响应按 seq 丢弃）；**颜色维度退役**（正文早不着色）：颜色筛选、六色选择、列表色点移除，`color` 列不动
- `167` **待归类整理：同一张卡的状态迁移，页面只出建议气泡** — `cook` 只补标题/分类/标签/一句话说明，原文逐字节不动、身份不变、文件迁出 `待归类`；`> 加工：` 判已加工卡、`> 状态：待归类` 判待归类卡（旧标记 `> 状态：生肉` 仍兼容），旧卡靠 `待归类/《书名》-时间戳` 兼容；同文同源已有已加工卡返回 `exists` 不覆盖；触发只走写作对话，空项目空白会话由 `pending_count` 挂 `#writeCookSuggest`，点击等于发出整理请求，不自动加工/合并/拆分；阅读页收录成功后 `WritingUI.refreshLibrary` 当场刷新素材库树（切回写作页 `open()` 作品未变会早退，不刷新）
- `168` **写作页 agent 输出里的文件路径可点开** — 写作页的 `.md` 路径（反引号/加粗/裸写）渲染成 `.write-file-link`，点击在工作区打开；渲染只识别形态，点击时解析（工作区文档 → 素材卡 → 章），找不到先刷新现场再报错；代码块里的路径不链，贴汉字的「见设定/…」不链，阅读页/首页不挂链接
- `169` **书架自定义排序：直接拖封面，手工序列固定正序** — 自定义不是第六种可翻方向的排序，`sortBooks` 忽略 `dir`、两页都不画箭头、旧 custom+desc 归一；选中自定义即收起面板并直接可拖（无整理模式/完成键），鼠标 8px 阈值起拖、松手自动保存，触屏 320ms 长按起拖，拖动结束的残留 click 被吞掉
- `171` **笔记/收藏变更日志** — `annotation_events` 是 append-only 事件流：三个 API 动作写 create/update/delete；更新只记实际变化字段，删除保留最终快照（原文/笔记全文），书名存快照；写入 best-effort 不阻断用户动作；查询走 `list_annotation_events`，agent 只读工具 `annotation_event_log`（DEFERRED）；写作文件变更继续用 `writing_git`，不另设日志
- `172` **Agent 设置页：左栏身份组 + 折叠外观 + 独立定义文稿 + 头像** — 称呼存 `settings.json` 并追加 `## 称呼` 进提示词（SOUL 标题/身份句读取时归一、不改写手稿，显式保存才落盘；空名只在退出编辑时回退）；左栏顶带下先是始终可见的「身份」组，再是「外观」折叠段，右侧实时预览台（左栏分段切换、双台等高配重、raised 桌面+限宽纸页；笔记为真浮窗叠真实书页，手机隐藏底图）；三块预览读真实书/会话/笔记且只读；SOUL 走独立单栏文稿视图（显式保存、`Ctrl/Cmd+S`、无 Markdown 预览）；保存前备份到 `data/backups/soul/`、原子写、空白拒绝、恢复出厂需 `confirm=true`；头像默认无，上传后存 `data/agent/avatar.*`，只在普通对话「思考过程」上方显示（扮演模式不叠加）
- `173` **用户资料：自己的头像与称呼** — 左栏身份组内加「我」行；`user_name` 存 `settings.json`、头像存 `data/user/avatar.*`，走 `/api/user`；称呼非空才在 `load_persona` 末尾追加 `## 用户`，空名合法、不写 SOUL/USER.md；用户头像进用户消息行右侧，历史消息按 `bc:user-avatar` 补画/换图/移除
- `174` **素材卡合并走许可门** — V1 只合并同书包含型（短摘句逐字出现在长卡里）且短卡未被消费；`writing_materials(merge, path=短卡, target=长卡)` 未带 `authorized` 只返回 `needs_authorization`，作者同意后才执行；长卡 id 与原文不变，标签取并集、补 `> 合并自` 抬头，短卡进回收站并在 manifest 记 `merged_into`，另写 `_合并日志.jsonl`；部分重叠/跨书/已消费卡只报告不自动合并
- `178` **上下文可核对 + 审稿目标清单** — 装配产出 `decision` 收据（只记块 id/额度/必需覆盖/丢弃与截断原因，不含正文）；透镜按命中段 ±1 取证据窗口并做包含去重，无关候选以 `no-relevant-passage` 显式省略；审稿前软件冻结本章目标（合同 `must_hit_now`/`must_preserve` + 设定里的 `【第N章必现】`），模型只填 `completed/unmet/unknown`，`unmet` 需明确延期/拒绝/相反结果证据，没写到只能判 `unknown`，引文须逐字且全文唯一，否则降级 `unknown`
- `175` **书架封面走缩略图 + 主动预热** — 封面统一请求 `/cover?thumb=1` 拿 400px 缩略图（导入时生成、存量由 `backfill_covers.py` 补齐，Pillow 缺失退回原图）；端点改 `FileResponse` + etag/304，不在协程里同步读文件；刷新后 6 并发预热全部缩略图，快滑不再等 lazy 的视口触发
- `176` **SOUL.md 用 `{user}` 指代用户称呼** — 手稿不写死人名，`load_persona` 按 `settings.json` 的 `user_name` 替换（空名退「用户」）；`/api/agent` 的 `soul` 仍是原始手稿（编辑器显示替换后的文本，保存时未改动的行按 LCS 映射回占位符），`soul_display` 与身份卡摘要用替换后的文本；改称呼立即反映
- `177` **用户性别与 `{ta}` 占位符** — 设置页「我」栏的「性别」状态行点击向下展开三档（女/男/不设置，带展开动画；不再写「阅读者 · 头像会显示…」说明行），存 `settings.json` 的 `user_gender`，走 `PUT /api/user/gender`；SOUL 手稿的第三人称用 `{ta}`，确认性别后换成「她/他」，未设置保持中性「ta」；静态提示词里指代读者的「他」改为「ta」，`## 用户` 段写明代词规则防模型自选
- `179` **短细纲可选 + 休眠线索提示** — `writing_draft_chapter(plan_first=true)` 先单独写一张 ≤6 条短细纲（purpose `writing.outline`，只组织原目标、不新增事实），再以「计划，不是事实依据」块注入正文请求；细纲为空或失败整轮失败，不静默跳过；默认关闭，多一次模型调用。卡片风险栏对超过 5 章没推进的开放承诺提一句，不升级为五类义务、不落库
- `180` **事实来源有权威级别** — `author(3) > import/derived(2) > delta(1)`：低级别来源不能覆盖或关闭高级别的开放事实；结算路径声明 `source_kind='author'` 直接拒绝（只有 `allow_author=True` 的作者侧入口能写）；卡片里作者来源的事实带「〔作者〕」标记，让 agent 知道这个字段为什么改不动
- `181` **同章重结算标 superseded** — 改稿后再结算同一章放行（只在旧行 `source_chapter == 当前章` 时）；旧行 `superseded=1`、区间收到 `[since, since]`，保留可追溯但不参与求值与校验；跨章同位置仍拒绝
- `182` **版本习惯归作者** — `works.git_habit`（v19）：`chapter`（默认，每章结算后自动留一版）/ `manual`（只在作者要求时存档）；安全检查点不取消；版本现场只展示一行；`set_habit` 只在作者明确要求时调用；`manual` 下结算返回 `git.committed=false`，必须如实告诉作者；**界面与提示词统一叫「存档」**（内部仍是 commit/commit_id）
- `183` **每条 user 输入自带换算时间** — 输入时间挂在每条 user 消息末尾（本地时间 + 周几 + UTC 偏移），CLI/Web 共用 `chat_context.with_input_time()`；历史按消息时间挂、没有时间戳不补；不再单独发 `<ephemeral-context>`；只发给模型，不落盘不显示
- `181` **初次见面引导按资料缺口注入** — `onboarding_directive` 每轮现算，只在 `user_name` 为空时注入一次克制的邀请（问称呼 + 可给我起名，不追问性别）；用户填上后下一轮自动消失，不存在陈旧注入；新增 `update_identity` 工具（DIRECT_MODEL_ONLY）让 agent 把对话里得到的称呼/性别/自己的名字落库，`tool_end` 广播 `bc:identity-changed` 让设置页立即刷新
- `182` **身份变更的证据日志** — sessions.db 的 `identity_events`（迁移 16，append-only）记录 field/old/new/session_id/source/time；`update_identity` 带对话会话、设置页记 settings；会话归属走 `identity_log` 的 ContextVar（子代理跑完恢复父回合），查询走 `list_identity_events` 与只读工具 `identity_event_log`（DEFERRED）

### 阅读会话域 → [`.claude/docs/reading.md`](docs/reading.md)（2 条）

- `124` **逐章阅读会话** — 稳定 system + 逐章追加正文/笔记；35% 窗口换窗（摘要块 + 最近 3 章）；工具按 current_chapter 设剧透门；写时逐字核引文；rich note + compact 先服务场景解释/扮演内核，图谱退为投影；飞行记录仪记 system+messages 供提示词微调；认路地图＝逐章投影（免费）+ 每 8–10 章一次卷级合成，**不接回逐章循环**（每章压缩会毁掉前缀缓存，约 2.4 倍输入成本）
- `126` **高亮＝引文分级** — kind(literary/kernel) + 受控 12 词 tag + who；逐字片段是唯一真源，章/块锚点只是加速器（不存偏移/CFI）；检索是纯函数 `highlights_for`，不建表；v4 及更早的书走 `tag_reading_quotes.py` 打标回填（不重读全本）；gold set 验收精度/漏检率

### 导入与排版域 → [`.claude/docs/import.md`](docs/import.md)（7 条）

- `15` **EPUB 切分** — LLM 识别标题模式 + 机械切分，两步走
- `16` **导入分析环境** — 给「有效排版」与整本样张，不给裸 CSS 与统计量
- `17` **导入分析编排** — 立论 → 证伪 → 机械校验 → 回炉，verifier 只拿结论
- `18` **导入分析提示词** — 教方法不教答案（禁书籍专属结论）；体裁优先于切分
- `19` **形态标签** — 逐段 «形态名» 只在读时渲染，绝不写回 content
- `20` **字体族** — 字体族进 EffectiveStyle 与等价标注，不进 sig()
- `64` **重导复用主键** — 同坐标 + 逐字节相同则复用旧 chunk 主键，register 前自动 relink

### LLM 输出与解析域 → [`.claude/docs/llm.md`](docs/llm.md)（5 条）

- `21` **LLM JSON 解析** — 直解 → 剥围栏 → 括号截取 → 全失败才修裸引号
- `22` **裸引号消歧** — 引号后跟逗号时看后面像不像「键」；数组里的逗号一律判收尾
- `23` **模型输出宽容三层** — 语法 / 形状 / 取值三层各自兜底，不认识的值照报
- `24` **解析失败要重试** — 非空但畸形的响应也重试，error_type 单报 parse_failed
- `25` **另两档语法修复** — 控制字符与全角标点各独立作用于原候选，绝不串联

### 作者层域 → [`.claude/docs/author.md`](docs/author.md)（33 条）

- `92` **双账本，单库** — 事实(author_records) / 声音(author_corpus+voice_traits) 分账，仍一个 authors.db
- `93` **来源不是证据，引文才是证据** — author_sources/evidence；web 必须逐字引文，旧数据标 legacy_url_only
- `94` **事实补全是一条管线** — facet 计划 → 搜索 → 抓页 → 一页一抽 → 逐字校验 → 合并；不挂导入/提取
- `95` **身份先于内容** — Wikidata + provisional/conflict，不自动合并；身份不明时只跑身份
- `96` **作者层 agent 的引用纪律** — author_web_fetch 抓页 + 落库逐字校验；author_deepen 不进子代理
- `97` **author_corpus 重建与幂等键** — 三种唯一键；Python 事务迁移 + 自动文件备份
- `98` **声音只看正文** — position 决定可见性；editor/recorder/absent/unknown 不采
- `99` **确定性抽样** — 窗口逐字 200–500 字，sha1 排序；三份裁剪判断一次用全，排短块/重复/注释/块内目录
- `100` **声音 run ≤2 次调用** — 每批 6 段；证据只认本批；单证据/单章降级
- `101` **写作用两道闸** — 只给 fiction/nonfiction + narration/essay；对话只标 scene_type 不拆层；source_bound 只进 manifest
- `102` **声音不写 records** — store_voice 与 store_harvest 平级；状态在 book 级
- `103` **声音幂等** — 内容 hash+规则版本+模型命中才跳过；--force 只清本书声音语料；语料先查再插
- `104` **Scout 只选声音候选段** — 不分析风格；老 plan 无字段走确定性抽样
- `105` **作者档案是纯投影** — 不调模型；门只算一次；no_book/unmapped/no_dated/all_ahead 四态分得开
- `106` **档案为默认入口，时间轴保留** — 档案页「全屏时间轴」切回旧画布；模块经 window 入口解耦
- `107` **覆盖度是缺口索引** — 事实/作品/声音/关系/来源加权；missing 具体到 voice.scene:X
- `108` **来源分已核/旧版/被挡** — quote 非空才算已核；冲突双侧保留；门内只带可见引文
- `109` **声音按维度归组，不用雷达图** — 10 维逐行；无结论不补空；只有正文文风可进写作
- `110` **年份门无年时声音按章放行** — records/claims 仍 fail-closed；样本/trait 证据按 chapter_index；voice.gate_mode=chapter_only
- `111` **作品页只认书名号** — 本地书 + 《…》抽取；写作事件留年谱；刊物/出版社名不算作品
- `112` **新档案接口与旧接口并存** — /api/authors + /{id}/dossier|voice|sources；前缀搜索；5 分钟缓存
- `113` **预算排序先修语义** — 可选块按 priority 降序保留；旧块一并按推荐表重排；留测试钉住
- `114` **检索键是场景不是作者** — 场景/关键词/时期/视角/来源质量打分；同章同原文去重；合同禁忌排除
- `115` **注入规则映射不用模型原话** — 10 维规范值各一句；例证只给正文；≤700 tokens；禁「你是某某」
- `116` **风格检查只报告** — 确定性指标；severity 只有 info/warning；独立 style-check，不混进 LLM 审稿
- `117` **默认关闭，选择存 style_profile** — author_refs 最多两位、消费第一位；query 覆盖；缺作者库不注入不建库
- `149` **攻略是先验，不是证据** — author_exploration_plan；黑白名单只有用户能改且按 target 生效，模型先验一律 grey；身份未定不生成具体作品先验；target 支持 `#chapter:<标题原词>` 点名误分类的序跋/附录；每个 target 只有一个坑（复用 plan_id，模型换措辞不新增）
- `150` **观察与钩子分表，依据 × 通道** — author_observations（纹理+basis/speaker/use）/ author_hooks；引文逐字命中，投射无锚降级，置信由代码推导
- `151` **用户标记 read_as 是投射锚** — author_books.read_as=autobiographical 只让投射可落库，不升格为传记事实
- `152` **DeepSeek Search/Fetch 是作者层联网通道** — Search 只产线索，Fetch 洗 Markdown 后逐字校验；enrich_author 可按端点自动选，回退原生
- `153` **细节采集独立进程按攻略跑** — scripts/run_author_details.py（--plan/--plans/--set-plan/--extract/--read-as/--dry-run）；观察/钩子/攻略各自幂等；正文三档放行，标题含序/附录/年表的章不参与均匀抽样；黑白名单只有 `--set-plan` 能改
- `155` **钩子闭合：检索词由模型出，答案只认页面** — plan_queries 一次给全部钩子出短关键词（候选专名可用，结论不许写进检索词）；确定性兜底「作者名+缺口首句」；多词结果去重后限页抓取，tier≥4 不抓；引文对不上不闭合
- `157` **作者细节接口化** — 攻略在档案页可见可审（GET/PATCH plans，黑白灰只有用户能改，界面不提供「模型建议」）；三个动作走 POST jobs 独立子进程 + 轮询，全局串行、状态在内存、日志落盘；细节页签三按钮 + 日志尾部 + 取消，完成自动重取档案

### 记忆与画像域 → [`.claude/docs/memory.md`](docs/memory.md)（4 条）

- `03` **记忆存储** — MEMORY.md / USER.md 纯文本，§ 分隔
- `13` **记忆检索** — 渐进式 ≤3 / 4-10 / ≥10，两趟法兜底
- `72` **画像管线** — persona_docs 版本控制 → persona.md；L3 触发 P1/P2/P4（50 条阈值）
- `73` **画像权威性** — persona.md（观察投影）注入在 USER.md 前

---

## 五、系统提示结构

`runtime._build_system_prompt()` 三层，以 `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` 分隔（prefix-cache 友好）：

- **STATIC**: 人格(SOUL.md) + 输出风格 + 技能列表 + 工具指南 + wingman画像 + 平台提示
- **DYNAMIC**: 当前模式 prompt + 激活 skill 全文 + keyword 引导 + 环境信息
- **SEMI-STATIC**: 指令文件 + 冻结记忆块 (MEMORY.md/persona.md/USER.md)
- **Ephemeral**(user message): 日期时间 + `<memory-context>` recall hook 注入

工具 schema 作为 `tools` 参数传 `api_client.stream()`，不在 system prompt 中。

---

## 六、编码约定

```python
from __future__ import annotations  # 类型注解全覆盖 + 延迟求值

@dataclass
class ToolSpec:
    name: str
    execute: Callable[..., str]          # 同步实现
    execute_async: Callable | None = None  # 异步版本
    permission_mode: PermissionMode = PermissionMode.WORKSPACE_WRITE

# 工具执行：返回错误消息给 LLM，不 raise
# 配置缺失：回退默认值，不崩溃
# 流式输出：async generator 逐 chunk yield
# 命名：文件 snake_case，类 PascalCase，常量 UPPER_SNAKE_CASE
```

**设计决策落盘规则**：论证写进对应的 `.claude/docs/<域>.md`，§四 索引只加一行（决策名 + 结论）。
索引与文档不复制同一段话——索引给"有这条、定了什么"，文档给"为什么"。改某域前先读该域文档。

**批处理文件（.bat）三条硬约束**（2026-09-21 实测，违反即乱码吞行）：
1. 编码 **GBK(cp936)**，且**不写 `chcp`**——文件与中文控制台默认代码页一致时 cmd 逐字节读；文件内切代码页会让 cmd 按旧代码页解出行边界，把整行劈开当命令执行。
2. 行尾必须 **CRLF**（Write 工具默认产出 LF，改完必须转回 CRLF）。`.gitattributes` 的 `*.bat text eol=crlf` 已钉死检出行为，不依赖各机器的 `core.autocrlf`；但**入库方向 git 拦不住**——仓库里存的是 LF（autocrlf 归一化所致），且退化到 LF 时 `git diff` 报"内容无差异"、`git add` 只给一行 warning，静默收下。改完 .bat 务必自己复查字节。
3. 不用制表符画框（`╔╚╝═─`）——`╚` 的 GBK 编码是 `A8 5E`，尾字节 `0x5E` 是 cmd 转义符 `^`，会吞掉后续字节。分隔线用 ASCII `=`/`-`。

---

## 七、工作流约定

```
执行: 先读本文件确认上下文 -> 修改 -> 验证 -> 确认
      优先编辑现有文件，不轻易创建新文件

不做: 不擅自重构非目标模块 / 不切换技术栈 / 不写多余文档 / 不创建不必要新文件

偏好: 先分析架构再动手 / 重视性价比 / 中文交流 / 深入细节
```

### ⚠️ 工具注册必检清单

最容易疏忽的 bug 来源——agent 不知道自己有什么工具就会"越狱"写临时脚本。

| 检查项 | 说明 |
|--------|------|
| 新工具注册到 `create_builtin_registry()`？ | 有条件注册时确认调用端传了对应参数 |
| Skill 的 tools_module 启动时手动注册？ | `auto_activate: false` 的 skill 必须显式 `register_skill_tools` |
| Skill 的 hooks_module 启动时手动注册？ | 同上，否则 hook 静默不触发 |
| 子 agent 白名单覆盖新工具？ | 检查 `sub_agent/types.py` 的 SUBAGENT_TOOLSETS |
| 搜索类工具描述注明 limit 建议值？ | 防止全量拉取 |
| 烟雾测试 `registry.get("name")` 非 None？ | 最简验证 |

---

## 八、下一阶段

1. 上下文管理的下一层：历史预算从 bytes/4 粗估换成模型 tokenizer；评估压缩摘要是否值得引入 LLM（当前按 02 定案保持纯结构化）
2. 音色系统真实 API 联调 + CLONE_VOICES 自建音色注册
3. Koodo Reader fork + Hook 注入（翻页文本提取 -> /api/sync）
4. 日常生活中的自我呈现：验证 font_size=4 切分的 35 章完整性
5. MOBI NCX 完整集成（位置数据，非仅标签）
6. 拆表/并表：方案 B（`review_status` 列）
7. BookGraph 合并候选审核 UI（merge_candidates 无前端消费）
8. 多角色同时扮演 + 角色间对话

---

*认路地图：结构骨架 + 行为契约。代码细节用 codegraph 检索，历史变更查 git log。*
