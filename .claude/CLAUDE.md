<!-- LLM_CUTOFF -->
# Book Companion - Claude Code 认路文件

> 每次会话启动时自动加载。结构细节用 codegraph 按需检索，本文件只保留认路骨架和行为契约。

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
图谱: Scout -> Explorer -> Recorder -> graph.db -> formatter.py(角色信息注入) + graph-viz.js(前端)
音色: voicedesign 铸造 -> voiceclone 复制 (cast+replicate); voice_roles 持久化; role-play 模式注入
```

子 Agent：同步 + 复用 Runtime + 白名单过滤 + 隔离 Session。Reader v1 仅 4 个读书工具。

---

## 三、核心模块入口

| 域 | 入口文件 | 存储 |
|----|----------|------|
| 主引擎 | main.py / runtime.py / tools.py / commands.py / compact.py | - |
| 会话 | session.py / services/session_service.py | sessions.db |
| 书库 | services/library_service.py / epub_import_service.py / file_readers/ | books.db |
| 记忆 | memory_store.py / memory_manager.py / memory_pipeline/ (L1/L2/L3) / wingman/service.py | memory.db |
| 图谱 | bookgraph/ / storage/graph_db.py | graph.db |
| 音色 | voices.py / tools.py (tts_speak, voice_design) | memory.db (voice_roles) |
| 前端 | reader/js/ (14 模块: app/chat/reader/shelf/roleplay/mode/sessions/graph-viz/graph-progress-ui + greetings/search/state/theme/utils) | - |
| 配置 | config.py / persona.py / prompts/ / context_files.py | - |
| 上下文计量 | context_meter.py / tokenizer_service.py / scripts/prompt_budget.py | 圆环数据源 |
| 控制台 | provider_store.py / api_server (/api/console/*) / reader/js/console.js | data/config/providers.json |

> 详细结构、调用关系、符号定位用 codegraph 检索（`codegraph_context` / `codegraph_search` / `codegraph_trace`）。

---

## 四、已定案的设计决策

不要推翻，除非有强有力的新理由。

| 决策 | 结论 | 理由 |
|------|------|------|
| 异步模型 | 全 asyncio | I/O 密集型 |
| 上下文压缩 | 纯结构化，不用 LLM | 省token、快、可预测 |
| 记忆存储 | MEMORY.md / USER.md 纯文本，§ 分隔 | 对标 Claude Code |
| 会话持久化 | SQLite 三层 (sessions/messages/parts) | - |
| 三种模式 | immersive / overview / divergent | 温柔书友 vs 犀利评论家 |
| 子 Agent | 同步 + 复用 Runtime + 白名单 | 隔离上下文 |
| 工具暴露 | ToolExposure 四级 + BM25 tool_search | DIRECT/DEFERRED/DIRECT_MODEL_ONLY/HIDDEN，每轮省 ~80% token |
| 提示词管理 | prompts/*.md 模板 | 代码只保留决策逻辑 |
| 指令文件注入 | 已关闭 | CLAUDE.md 给 Claude Code 用，与书伴 agent 无关 |
| 延迟会话创建 | 首条消息才写 DB | 消除空会话 |
| TBD 审查 | time_reviewed=0 哨兵 | 拆表时升级 review_status 列 |
| 记忆检索 | 渐进式 ≤3/4-10/≥10 | 两趟法兜底 |
| 数据库分库 | books.db + sessions.db + memory.db + graph.db | BaseDatabase 共享基类 |
| EPUB 切分 | LLM 识别标题模式 + 机械切分 | 两步工作流 |
| BookGraph | Scout -> Explorer -> Recorder + SSE | Scout 预分组省 token；Recorder 纯 SQL |
| 角色信息注入 | format_role_kernel() 纯函数 -> system prompt | f9743b3 起废弃 LLM 生成 + 文件缓存 |
| 图谱存储 | graph.db (nodes/edges/aliases/quotes/merge_candidates) | name->alias 解析链 |
| 音色消费场景 | 仅角色扮演模式 agent 语音输出 | 阅读 TTS 已砍 |
| TTS 同 key | tts_api_key/base 默认空，回退 api_key/base | LLM 即 mimo 标准 key |
| 音色铸造 | voicedesign 铸造 + voiceclone 复制 | 一次铸造多次复制；voiceclone 支持标签 |
| 角色音色持久化 | voice_roles 表 (memory.db v8) | book_id+character -> voice_id+prompt |
| 前端模块化 | reader/js/ 14 模块 | 按功能拆分 |
| 画像管线 | persona_docs 表（git 式版本控制）→ data/memory/persona.md；L3 触发 P1/P2/P4（50 条阈值） | 可追溯 + 稳定画像 |
| 画像权威性 | persona.md（AI 观察投影）注入在 USER.md 前；冲突处理 TBD | 观察投影更接近描述性真实 |
| 上下文计量 | 总量=末轮 API usage（权威）；明细=本地占比×总量（和恒等）；预警线 0.35 | 块级五块不做段级拆分 |
| 分词器选路 | 模型显式覆盖 → 厂商别名表（子串匹配，key 可逗号多别名）→ 全局兜底 → chars//4 估算 | 三家分词器同句计数实测不同，单分词器不准 |

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

1. 聊天上下文管理（`/api/chat` 硬编码 3 轮 history）
2. 音色系统真实 API 联调 + CLONE_VOICES 自建音色注册
3. Koodo Reader fork + Hook 注入（翻页文本提取 -> /api/sync）
4. 日常生活中的自我呈现：验证 font_size=4 切分的 35 章完整性
5. MOBI NCX 完整集成（位置数据，非仅标签）
6. 拆表/并表：方案 B（`review_status` 列）
7. BookGraph 合并候选审核 UI（merge_candidates 无前端消费）
8. 多角色同时扮演 + 角色间对话

---

*认路地图：结构骨架 + 行为契约。代码细节用 codegraph 检索，历史变更查 git log。*
