<!-- LLM_CUTOFF -->
# Book Companion - Claude Code 认路文件

> 每次会话启动时自动加载。结构细节用 codegraph 按需检索，决策论证按域拆在 `.claude/docs/`，
> 本文件只保留认路骨架、行为契约和决策索引。

---

## 一、项目身份

| 项目 | 值 |
|------|-----|
| 名称 | Book Companion（书伴） |
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
作者: 序跋采集(harvest) -> 台账(authors.db); 网页补全 -> sources/(计划→抓页→一页一抽→逐字校验→合并) -> source/evidence/record
      声音采集 -> voice/(可见性→分层抽样→1–2次小调用→trait) -> corpus/voice_traits（不写 record）
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
| HTTP 层 | api_server.py (门面+组合根, 591 行) / api/routes/ (chat/graph/books/sessions/sync/writing/voice/console/annotations/avatar) / api/sse.py / api/prompt_builders.py / api/text_helpers.py / api/book_helpers.py | - |
| 会话 | session.py / services/session_service.py | sessions.db |
| 书库 | services/library_service.py / epub_import_service.py / file_readers/ | books.db |
| 导入分析 | services/import_analysis_service.py (立论→证伪→回炉) / structure_invariants.py (机械校验) / file_readers/style_resolver.py (有效排版) + book_proof.py (样张+切分实测) | - |
| 形态回流 | services/chunk_annotation.py (逐段渲染 `«形态名»` 进提取提示词) / scripts/backfill_chunk_sigs.py (补旧块签名) | - |
| LLM JSON 解析 | llm_json.py (宽容阶梯 + 裸引号修复；bookgraph/memory_pipeline/archive/services/wingman 共 12 处共用) | - |
| 记忆 | memory_store.py / memory_manager.py / memory_pipeline/ (L1/L2/L3) / wingman/service.py | memory.db |
| 图谱 | bookgraph/ (orchestrator/scope/selection/verify/invariants/llm_call) / storage/graph_db.py | graph.db |
| 作者层 | authorgraph/ (harvest/store/refresh/card/gate/identity/dossier/coverage) + sources/ (registry/query_plan/fetch/extract/verify/merge/pipeline) + voice/ (scope/sample/extract/normalize/traits/store/select/pipeline) / writegraph/voice_lens+voice_card+voice_check / api/routes/authors.py / reader/js/author-dossier.js / storage/authors_db.py | authors.db |
| 图谱运维 | scripts/run_graph_extraction.py (独立进程驱动，服务内跑会冻事件循环) / repair_graph.py / scope_report.py / graph_invariants.py | - |
| 音色 | voices.py / tools/voice.py (tts_speak, voice_design) | memory.db (voice_roles) |
| 前端 | reader/js/ (24 模块: app/chat/reader/shelf/roleplay/mode/sessions/console/usage/binding/writing/graph-viz/graph-progress-ui/context-usage 等，按功能拆分) | - |
| 配置 | config.py / persona.py / prompts/ / context_files.py | - |
| 上下文计量 | context_meter.py / tokenizer_service.py / scripts/prompt_budget.py | 圆环数据源 |
| 控制台 | provider_store.py / api_server (/api/console/*) / reader/js/console.js | data/config/providers.json |
| 联网搜索 | services/web_search_service.py (原生 web_search 主路 + Tavily 兜底) | - |

> 详细结构、调用关系、符号定位用 codegraph 检索（`codegraph_context` / `codegraph_search` / `codegraph_trace`）。
> 设计决策的**论证**按域拆在 `.claude/docs/`（graph / reader / import / llm / memory / platform / author）；动手改某域前先读对应文档，索引见 §四。

---

## 四、已定案的设计决策（索引）

> **不要推翻，除非有强有力的新理由。** 这里只有结论，论证在域文档里——
> **动手改某个域之前，先读该域文档的全文。**
> 新增决策：先写进域文档，再在这里加一行（只加名字+结论，不复制理由）。

### 平台与运行时域 → [`.claude/docs/platform.md`](docs/platform.md)（24 条）

- `01` **异步模型** — 全 asyncio
- `02` **上下文压缩** — 纯结构化，不用 LLM
- `04` **会话持久化** — SQLite 三层：sessions/messages/parts
- `05` **三种模式** — immersive / overview / divergent
- `06` **子 Agent** — 同步 + 复用 Runtime + 白名单隔离
- `07` **工具暴露** — ToolExposure 四级 + BM25 tool_search
- `08` **工具 schema 的暴露层裁剪** — 导入探针与「按路径读未入库文件」的工具对主 agent 一律 DEFERRED
- `09` **提示词管理** — prompts/*.md 模板，代码只留决策逻辑
- `10` **指令文件注入** — 已关闭（CLAUDE.md 与书伴 agent 无关）
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
- `90` **写作 agent 的轮次与上下文经济** — 版本现场进 system prompt；变更工具返回精简 status + 改动章首尾预览；自测清单按 phase 取；写作 ReAct 上限 40；真实自测 22/22、0 issue
- `91` **Web chat 上下文管理** — 工具调用/结果成对落盘；token 预算在 user 回合边界裁剪；老回合压成机械摘要只进请求不改库；真实会话 0 撞顶

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

### 阅读器与前端域 → [`.claude/docs/reader.md`](docs/reader.md)（12 条）

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
- `118` **写作页版本图** — 历史按 git log --graph 摊成泳道；第二父节点另起一列，单章过滤改置灰（`mark_chapter`）不断线
- `119` **写作页只做写** — 合同 / 上下文装配 / 结算从页面移除，交给写作 harness；侧栏只留审稿与版本

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

### 作者层域 → [`.claude/docs/author.md`](docs/author.md)（26 条）

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
