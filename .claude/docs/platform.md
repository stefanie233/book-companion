# 平台与运行时域 · 设计决策

> 自 `.claude/CLAUDE.md` §四 拆出（2026-10-23），共 18 条。
> 这些是**已定案**的决定：不要推翻，除非有强有力的新理由。
> 索引见 `.claude/CLAUDE.md` §四；新增决策先写这里，再在索引加一行。

### 01. 异步模型

**结论**：全 asyncio

**理由**：I/O 密集型

### 02. 上下文压缩

**结论**：纯结构化，不用 LLM

**理由**：省token、快、可预测

### 04. 会话持久化

**结论**：SQLite 三层 (sessions/messages/parts)

**理由**：-

### 05. 三种模式

**结论**：immersive / overview / divergent

**理由**：温柔书友 vs 犀利评论家

### 06. 子 Agent

**结论**：同步 + 复用 Runtime + 白名单

**理由**：隔离上下文

### 07. 工具暴露

**结论**：ToolExposure 四级 + BM25 tool_search

**理由**：DIRECT/DEFERRED/DIRECT_MODEL_ONLY/HIDDEN，每轮省 ~80% token

### 08. 工具 schema 的暴露层裁剪

**结论**：导入探针（`epub_probe` / `book_survey` / `epub_import`）与「按磁盘路径读未入库文件」的 4 个工具（`book_info` / `read_book` / `list_books` / `book_search`）对主 agent 一律 **DEFERRED**（tool_search 可发现）；子代理白名单内由 `SUBAGENT_PROMOTE_DEFERRED` 换 `ToolSpec` 提升为 DIRECT；`current_passage` 保持 DIRECT

**理由**：这 16 个工具为导入分析/未入库文件而存在，却默认 DIRECT：实测主 agent 一轮 32 个可见工具里占 **4,142/6,597 tokens（63%）**，而读书回合一次都不调（`epub_import` 一条描述就 3,061 字符 ≈790 tokens）。按路径的工具在读书回合里还有害 —— 模型拿不到路径，22:43 轨迹里 `book_search` 就是这样空转一次。提升必须 `dataclasses.replace` 换对象 —— `get_registry_for_toolsets` 登记的是同一个 `ToolSpec`，就地改 `exposure` 会把主 agent 的表一起改掉

### 09. 提示词管理

**结论**：prompts/*.md 模板

**理由**：代码只保留决策逻辑

### 10. 指令文件注入

**结论**：已关闭

**理由**：CLAUDE.md 给 Claude Code 用，与书伴 agent 无关

### 11. 延迟会话创建

**结论**：首条消息才写 DB

**理由**：消除空会话

### 12. TBD 审查

**结论**：time_reviewed=0 哨兵

**理由**：拆表时升级 review_status 列

### 14. 数据库分库

**结论**：books.db + sessions.db + memory.db + graph.db

**理由**：BaseDatabase 共享基类

### 67. 音色消费场景

**结论**：仅角色扮演模式 agent 语音输出

**理由**：阅读 TTS 已砍

### 68. TTS 同 key

**结论**：tts_api_key/base 默认空，回退 api_key/base

**理由**：LLM 即 mimo 标准 key

### 69. 音色铸造

**结论**：voicedesign 铸造 + voiceclone 复制

**理由**：一次铸造多次复制；voiceclone 支持标签

### 70. 角色音色持久化

**结论**：voice_roles 表 (memory.db v8)

**理由**：book_id+character -> voice_id+prompt

### 74. 上下文计量

**结论**：总量=末轮 API usage（权威）；明细=本地占比×总量（和恒等）；预警线 0.35

**理由**：块级五块不做段级拆分

### 75. 分词器选路

**结论**：模型显式覆盖 → 厂商别名表（子串匹配，key 可逗号多别名）→ 全局兜底 → chars//4 估算

**理由**：三家分词器同句计数实测不同，单分词器不准

### 83. 模型分流路由

**结论**：ProviderStore v3 增加 `routes` —— **每个 LLM 调用点**绑定 **(连接, 模型)**，id 用点号分级（`bookgraph.explorer`、`writing.review`…），组名本身也是可绑定的路由键。首版 **25 个调用点 / 7 个分组**；`chat` 只是分组名、**不能作为路由键**（对话模型由主模型槽唯一决定，否则水位计与 session.model 会和实际调用的模型脱节）。解析链 `精确 id → 逐级父前缀 → main_llm → .env`：未手动绑定的调用点一律跟随主模型。入口在控制台二级导航新增 **「模型绑定」** 页（与「驱动控制台」「用量分析」平行）；主模型那一行就是 `main_llm` 槽，不另设真相源。实现在**调用点**取 `config_for(config, id)` 生成 `dataclasses.replace` 配置视图，或给 `OpenAIClient.stream/complete` 传 `scenario=` 由客户端按次解析；绝不按调用改全局 Config。

**理由**：
- 现有 `apply_to_config` 只写全局唯一的 `config.model`，而 api_server 是多协程并发的 —— 聊天流式输出和图谱后台任务同时跑，按调用改全局必串味。配置视图让每个调用点拿自己的端点，互不写对方。
- 绑定单位必须是连接+模型：跨供应商（DeepSeek 提取 / 豆包写作）本来就是两个 base_url 两条 key，只换模型名做不到。
- 逐级父前缀让「全部调用点可绑定」不至于变成 26 行都要填：绑 `bookgraph` 组即覆盖组内全部，单独绑 `bookgraph.adjudicate` 只覆盖那一条；未绑任何一级才落到主模型。
- 配置视图要带 `_route_base` 指回原始 Config：否则「先按组解析、再按叶解析」时，叶级未绑定会把组级绑定冲掉。解析函数一律从 base 重算，保证顺序无关。
- **写前先重载**（`_refresh_if_stale`）：`_save` 写的是整份内存快照，陈旧实例（另一个进程 / 更早加载的 app 实例）改主模型时会把旧表整体写回，routes 被静默抹掉 —— 实测过一次「改主模型后绑定全丢」。所有 mutator 与 `snapshot` 先按 mtime 重载，保证保存只会合并到最新文件上。
- `usage_log` 的 source 保持粗粒度（chat / bookgraph / memory / writing / import / enrich / sub_agent…），不跟着调用点切碎 —— 那需要给 usage 表加列，与「按调用点换模型」是两件事。顺带补 `writing` / `import` 两个缺失埋点。
- TTS / ASR / Embedding 不是 LLM 调用点，仍留在「驱动控制台」的槽位带。

**操作契约与调试入口**（避免每次重新解释）：

- **UI 契约**：`控制台 → 模型绑定`（`reader/js/binding.js`）。**选择即保存**：选连接（换连接时保留同名模型，否则取该连接第一个 llm 模型）或改模型（失焦/回车）就落盘；选「跟随上级」或点「解绑」回退到上一级。与驱动控制台槽位带同一交互。
- **服务端自查**：`GET /api/console/config` 的 `routes` 只含已绑定的键；`callpoints[]` 每项带 `origin`（命中的上级键，空串 = 跟随主模型）和 `resolved`（实际生效的连接/模型）。判断「到底有没有绑上」看这两处，不看页面输入框。
- **写审计**：`data/logs/providers.log` 每次保存一行（`op` / `pid` / `main` / route 键 / 调用栈）。再遇到「绑定消失」，先看这里是谁写的；出现不同 PID = 同时跑了多个服务实例。
- **回归**：`scripts/test_provider_store.py` 的 `route binding` / `stale writer keeps routes` / `save never drops disk routes` 三组，改这一域前后都跑一遍。

### 87. 联网搜索：原生主路 + Tavily 兜底

**结论**：联网搜索统一走 `services/web_search_service.py` 的 `search(session, cfg, query, *, max_results, deep_search, topic)`，返回 `{hits, answer, provider, warnings}`。顺序是 **DeepSeek 原生服务端搜索 → Tavily**：原生搜索是 Anthropic Messages 兼容端点上的服务端工具（`{"type": "web_search_20250305", "name": "web_search"}`），一次 POST 完成「搜索 → 读 → 写」，不需要插件、不需要额外搜索 key；Tavily 只在原生不可用/失败/零结果时兜底。当前接入点只有 AuthorGraph 工序 6（`authorgraph/enrich.py`）；**主 agent 的 `web_search` 工具尚未接入原生搜索**，那是一次独立改动。

**正确调用姿势**（`services/web_search_service.py`）：

- 端点：`native_endpoint(cfg)` 解析 → 显式 `native_search_base` 优先，否则 `api_base` 的 host 是 `*.deepseek.com` 时推导为 `<scheme>://<host>/anthropic`。
- 请求：`POST <base>/v1/messages`，头 `Authorization: Bearer <key>` + `anthropic-version: 2023-06-01`，body 带 `model` / `max_tokens` / `messages` / `tools`（上面那个工具 + `max_uses`）。
- 结果：`content` 里出现 `server_tool_use` 与 `web_search_tool_result`；**URL 只认 `web_search_tool_result` 块**，正文摘要让模型按 URL 输出 JSON，只能给同 URL 条目补正文，不能新增条目（否则「伪造出处」从模型侧绕回来）。`usage.server_tool_use.web_search_requests` 是实际搜索次数。
- 两个硬条件与两个坑：
  1. **端点必须是 Anthropic 兼容口**，OpenAI 兼容的 `/v1` 上没有这个工具；
  2. **模型名必须是该端点认识的**（`deepseek-flash` / `deepseek-v4-pro`），`deepseek-flash[1m]` 里的 `[1m]` 是客户端后缀，必须剥掉；
  3. Claude Code 自带的 `WebSearch` 工具依赖 haiku 槽位模型：`ANTHROPIC_BASE_URL` 指向 DeepSeek 时，`ANTHROPIC_DEFAULT_HAIKU_MODEL` 若还是 `glm-*`，会报 `400 The supported API model names are ...` —— 这不是搜索不可用，是模型名映射错了；
  4. 原生搜索返回值没有明文正文（`encrypted_content` 不落明文），所以摘要由模型按 URL 回写；Tavily 的 `content` 才是搜索端给的原始片段。

**理由**：联网是 agent 后续常用的能力，不能绑死在一个第三方 key 上；而 DeepSeek 官方端点自带服务端搜索，不需要插件就能用，默认走它最省事。Tavily 留作兜底：它的结果带正文片段、出处可逐条核对，原生路径出问题时不至于整条链路失明。安全边界必须落在「URL 只认服务端结果块」——否则模型把编造的网址写进正文 JSON，出处校验就被绕过了。

### 88. 用户主动打断

**结论**：Web 对话的停止按钮走「显式取消 + 本地冻结」双通道。前端为每条流生成 `turn_id`，停止时 POST `/api/chat/cancel`；服务端 `_TurnCancelRegistry` 置位该轮的 `asyncio.Event`，ReAct 循环在流 chunk 之间、每个工具前后、每轮开始前检查。前端同时立刻冻结打字机；2.5s 内没收到 `interrupted`/`done` 终态就 `AbortController.abort()` 兜底。已生成的正文照常落盘为 assistant 消息，并追加一个 `interrupt` 内容块（文案来自 `prompts/interruption.md`）：该块不进 `content`、不渲染成正文；下一次请求由 `_history_from_session` 把注记拼在被截断消息的最前面。中断轮不写长期记忆、不喂图谱、不算上下文计量。

**理由**：
- 只断网停不住服务端：写失败前它可能已经进入下一轮 LLM / 下一个工具；而且断网写失败和真故障无法区分。显式取消能精确停到一轮，`turn_id` 也保证多标签页互不误伤。
- 注记必须落盘，不能只放内存：刷新、重启或换页签后，模型仍需知道上一轮没说完；`interrupt` 块与正文同属一条 assistant 消息，避免多插一条 user 消息破坏角色交替。
- 注记放在历史内容最前面，是因为 `_history_from_session` 只取 `content[:1500]` —— 放尾部会被长正文截掉。
- 同步工具会占住事件循环，取消请求只能在工具返回后被处理；所以这是「有界停止」：已运行的同步工具不抢占，只在返回点丢弃结果，剩余工具和后续轮次不再执行。
- 提前到达的取消（响应头返回前就按停止）由注册表的 `_early` 集合接住，`register()` 时直接置位。
- 停止定格看的是「按下瞬间**已经渲染出来的前缀**」，不是 `_fullText` 全量：SSE 到得比打字机快，积压的尾巴属于服务端落盘/下一轮上下文，上屏时不能一口气倒出来。打字机在积压 >80 字时按比例追赶（约半秒内追平），正常节奏仍是逐字。

### 89. HTTP 层与工具包拆分（api/ + tools/）

**结论**：`api_server.py`（原 6482 行 → 591 行）只留中间件、`create_app()` 路由表、健康检查与启动器，成为**门面 + 组合根**；handler 按域搬进 `api/routes/{chat,graph,books,sessions,sync,writing,voice,console,annotations,avatar}.py`，共用件单独成 `api/sse.py`（SSE 帧写入 + `_ThinkParser`）、`api/prompt_builders.py`（纯函数提示词）、`api/text_helpers.py`（选区定位 / 章类型标签）、`api/book_helpers.py`（书籍查找 / 翻页 / stable-id）。`tools.py`（原 4294 行）改为同名包 `tools/`，按 toolset 拆模块，`tools/builtin.py` 的 `create_builtin_registry()` 只做注册调度，`tools/__init__.py` 复刻旧模块的公开面（`ToolRegistry` / `ToolSpec` / `ToolExposure` / `PermissionMode` / `create_builtin_registry` / `_deepen_*`），所以全仓库 `from tools import X` 与 `from api_server import X` 的调用点零改动。

**三条硬约束**（拆分时踩过，改这块前必读）：

1. **路由注册顺序是正确性契约**：aiohttp 按注册顺序匹配，`/api/sessions/search`、`/api/sessions/list` 必须排在 `/api/sessions/{session_id}` 之前，`/api/books/scan-folder`、`/api/books/order` 必须排在 `/api/books/{book_id}` 之前。所以 **handler 可以搬，`create_app()` 里的 `add_*` 行一律不搬、不重排**；新增域在对应位置追加。
2. **`Path(__file__)` 的基准会变**：`api/routes/*` 与 `tools/*` 都深了一层，定位 `data/`、`roles/`、`prompts/` 一律用 `_APP_ROOT = Path(__file__).resolve().parents[N]`，不要再用 `Path(__file__).parent`。
3. **代码指纹必须覆盖整层**：`/api/health` 的 `api_server_sha256` 由 `_http_layer_files()`（`api_server.py` + `api/**/*.py`，路径 + 字节排序汇总）算出。只哈希入口文件会让「改了路由模块但进程仍是旧的」这条失效——那正是这个指纹存在的理由。`scripts/sse_stream_probe.py` 直接复用同一个函数，两边不会漂移。

**工具注册顺序**：`tools/builtin.py` 的调用顺序与原单文件一致（basic → web_search → books → library/probe/survey → weather → sessions → voice → agent → graph → tool_search → author）。它决定模型 `tools` 数组的顺序，稳定顺序才吃得到 prefix cache。

**理由**：单文件 6000+ 行时，改任何一个端点都要把整份文件读进上下文，而其中 80% 是别的域；按域切开后典型改动的阅读面从一个文件降到几百行。工具包同理：原文件一半篇幅是 JSON Schema 字面量，拆开后「改一个工具」只需看它所在的 toolset 模块。保留门面而不是改全部调用点，是为了让拆分本身可回滚、diff 只看搬运。

### 90. 写作 agent 的轮次与上下文经济

**结论**：写作模式的 ReAct 上限从 20 提到 40；写作 system prompt 增加「版本现场」（当前工作与最近自测作品的分支 / HEAD / clean / 暂存 / 最近 6 条提交）；`writing_git_change` 的变更操作统一返回精简 `status` 与 `chapters`（改动章号、字数、首尾 120 字预览）；`selftest_plan` 支持 `phase=A/B/C/D`、`step=C4` 只取一段；提示词要求「按阶段取清单、独立调用同轮并行、不重复核对已返回的字段」。

**理由**：

- 飞行记录仪实测（2026-10-25 21:17 会话，`session-1792934259692-0002`）：12 轮用户消息触发 **120 次 LLM 调用**、累计输入 **174 万字符**；**35 轮是纯只读核对**（status/log/reflog/read_chapter），91/120 轮只发一个工具调用；U5、U7 正好撞 20 轮上限。
- 跨轮历史只回灌最近 3 条纯文本（每条 ≤1500 字），**工具结果不进历史**。撞顶后用户说「继续」，agent 只能从 status/log/读章重新摸路——所以「同一会话只取一次清单」这类要求不可执行，必须让服务端把现场和阶段清单重新给到。
- 变更操作已经把新状态算出来了（commit/merge/restore 内部都调过 `vcs_status`），只是没返回；把 status 和改动章首尾预览带上，比让 agent 再花一整轮读章便宜得多。
- 自测清单全量 13KB，按阶段取后 2.5-4.2KB；阶段内 `do` 的依赖关系决定轮次，互不依赖的读仍应同轮并行。
- 40 轮是安全上限，不是目标；模型不再调工具就退出。它只负责让一个阶段在一轮里收口，避免「撞顶 → 用户手动继续 → 上下文全丢」的循环。

### 91. Web chat 的上下文管理（结构化历史 + token 预算）

**结论**：`/api/chat` 不再用 `history[-6:] + 每条 1500 字` 的纯文本切片。新增 `chat_context.py`，按四条不变量管理 Web 历史：

1. **记录**：工具轮在落盘时写成 assistant(text + tool_use) + role=tool(tool_result)，不再压成一条纯文本；最终轮单独落一条 assistant。
2. **配平**：发请求前补齐缺失的 call output（`aborted`）、剔除孤儿 output，并按 tool_call 顺序重排；打断时没跑到的 call 也补 `aborted`。
3. **预算**：按 UTF-8 字节 / 4 估算 token（中文不用字符数低估），在 user 回合边界裁剪；当前回合无论多大都完整保留。
4. **补位**：被裁掉的旧回合压成 `<conversation-summary>` 机械摘要（用户请求 + 调用过的工具），只进本次请求，不改写持久化会话。

工具输出回灌前按 6K tokens 做头尾截断（读正文类工具放宽到 24K，因为它的输出是模型接下来要改的对象）；`<think>` 不再回灌给模型。会话加载接口过滤 `role=tool` 与纯 tool_use 的 assistant 壳，避免前端空气泡。

**理由**：

- 旧路径在落盘那一刻就丢了工具证据：下一轮 `_history_from_session` 只能拼文本，40 轮写作会话 reload 后等于「用户说了几句、助手说了几句」，中间所有 commit id / 冲突信息全没了。这正是写作 agent 撞顶后必须重新摸路的根因。
- 按消息条数裁剪没有语义：6 条可能是 200 tokens，也可能是 20K；且从中间切会切出半对 call/output，API 直接拒绝。正确做法是按 token 预算、在回合边界切、发请求前配平，三件事缺一不可。
- 原始会话是用户可见的记录，压缩只该发生在「发给模型的视图」上；把压缩结果写回 DB 会让刷新后的聊天记录凭空少一截。压缩摘要也应作为一条 user fragment 注回 prompt，而不是改写持久化会话。
- 摘要只写机械事实（多少条、用户说过什么、调用过哪些工具），不做语义总结——摘要比原文更自信是上下文管理里最危险的失败模式。

**2026-10-26 真实会话复测**：同一套写作 git 自测（现 22 步，D5 新增检查点分支删除保护）在新上下文管理下连跑 5 轮。

- 旧会话（10-25）：120 次调用、35 轮纯只读、2 次撞 20 轮上限；12 个用户回合里含 3 轮 bug 修复。
- 新实现首轮：82 次调用、19 轮纯只读、0 次撞顶（上限 40），21/21 通过，抓出 2 条清单/返回面建议。
- 最终轮：89 次调用（22 步）、21 轮纯只读、max_round=34、0 次撞顶，**22/22 通过、0 条 issue**。
- 调用数下降约 26%（120→89，且步数从 21 增到 22）；纯只读轮下降 40%（35→21）。总输入字符从 1.74M 升到 3.73M——旧会话每轮只带 6 条纯文本，新历史保留了工具证据，代价是每轮上下文更完整，换来撞顶和「重新摸路」消失。

自测期间顺带修掉 5 处返回面/清单问题：`vcs_diff` 补目标侧 `previews`（B3 不用再切分支读章）、`stash_pop` 改为工作区预览且 D1 的 `clean` 期望修正、D3「改名→查 branches→删除」顺序、`head.branch` 按当前分支回填、自动检查点分支 force 删除前留 `lost-*` 标签（新增 D5）。
