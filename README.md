# Liubai（留白）

基于 AI 的本地阅读陪伴助手：CLI 内核 + Web 阅读界面。导入你的 EPUB，和一位**记得你读过什么、聊过什么**的书友一起读书——所有数据都留在你自己的机器上。

---

## ✨ 功能特性

- **📚 书库与导入** —— 扫描本地文件夹批量导入 EPUB，自动识别章节结构
- **💬 三种伴读模式** —— 沉浸伴读（不剧透）/ 俯瞰分析（全局视角）/ 发散探索（联网搜索与外部知识），人格由 `SOUL.md` 定义、可自行改写
- **🧠 分层长期记忆** —— L0–L3 管线（原子记忆 → 场景归纳 → 用户画像），画像与记忆可查看、可编辑
- **🕸️ BookGraph 知识图谱** —— 三段式抽取（Scout → Explorer → Recorder）构建人物/事件/地点关系网，SSE 实时提取进度，浏览器内力导向可视化
- **✍️ 写作工作台（WriteGraph）** —— 作品树、文档式编辑、版本图（泳道）、风格透镜与写作 git（暂存 / 提交 / 合并 / 撤销 / 摘取）
- **🖋️ 作者图谱（AuthorGraph）** —— 序跋采集、作者档案、时间线、正文声音采集与来源证据链
- **🎭 角色扮演 + 音色** —— 图谱角色信息注入对话；按描述铸造音色、按参考音频复制音色（依赖提供对应能力的语音 API）
- **🗜️ 结构化上下文压缩** —— 纯确定性算法压缩会话历史，无 LLM 参与，配合上下文用量计量
- **🧰 工具系统** —— 四级工具暴露 + BM25 工具检索 + 白名单子 Agent，控制每轮 token 开销
- **🌐 Web 前端** —— 书架 / 阅读 / 对话 / 图谱 / 作者档案 / 写作 / 角色扮演 / 控制台；会话分叉、恢复、导出 Markdown
- **🎙️ 可选语音输入** —— 本地 ASR 模型（约 231 MB，安装时可选）
- **🔒 本地优先** —— 六个 SQLite 库分立存储，API Key 只存本地

## 🚀 快速开始

### 环境要求

- Windows 10/11（一键脚本）；其他平台可手动运行（依赖均为跨平台库）
- [Python 3.12+](https://www.python.org/downloads/)
- 桌面版另需 `pywebview` / `pythonnet`（可选，见「桌面版」一节）

### 一键安装（Windows）

```bat
git clone <本仓库地址>
cd book-companion
setup.bat
```

`setup.bat` 会依次完成：检查 Python → 创建虚拟环境 `.venv` → 安装依赖 → 从 `.env.example` 复制出 `.env` → 询问是否下载本地语音识别模型（可选）→ 自动拉起服务。

### 手动安装（macOS / Linux）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

### 启动

```bat
start.bat        :: 启动 Agent + API 服务，等待就绪后自动打开浏览器
```

或直接 `python main.py`，然后访问 **<http://127.0.0.1:8766>**。

首次启动推荐**留着 API Key 不填**——服务起来后在网页引导里填写即可。

### 桌面版（可选）

仓库附带 PyInstaller 打包脚本，可生成原生窗口桌面版（应用名 **留白 / Liubai**，只含 Web 界面，不含 CLI）：

```powershell
pip install pywebview pythonnet      # 原生窗口依赖；Windows 使用 WebView2
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

加 `-Windowed` 去掉控制台窗口，加 `-Installer` 输出安装包（需要 Inno Setup 6）。
完整参数、安装包与便携模式说明见 [packaging/README.md](packaging/README.md)。

桌面版首次启动会把可写数据放在 `%LOCALAPPDATA%\LiuBai`（可用环境变量 `BC_HOME` 覆盖；
在 exe 同级放 `portable.flag` 可切换为便携模式）。源码运行时仍使用仓库根目录，两者互不影响。

## ⚙️ 配置

编辑根目录 `.env`（源自 `.env.example`）：

| 变量                              |  必填 | 说明                                                    |
| ------------------------------- | :-: | ----------------------------------------------------- |
| `BOOK_COMPANION_API_KEY`        |  ✅  | LLM API Key（OpenAI 兼容）                                |
| `BOOK_COMPANION_API_BASE`       |  ✅  | API 地址，默认 `https://api.openai.com/v1`                 |
| `BOOK_COMPANION_MODEL`          |  ✅  | 模型名，默认 `gpt-4o`                                       |
| `BOOK_COMPANION_SEARCH_API_KEY` |     | Tavily 搜索 Key（发散探索模式的联网能力，[免费申请](https://tavily.com)） |
| `EMBEDDING_API_KEY` 等           |     | 语义检索（可选）                                              |
| `BOOK_COMPANION_SCAN_FOLDER`    |     | 默认书源扫描目录（可选）                                          |

多 Provider 切换、模型绑定可在网页**控制台**页完成（存于 `data/config/providers.json`）。

## 📖 使用

### 三种模式

| 模式   | 指令           | 风格              |
| ---- | ------------ | --------------- |
| 沉浸伴读 | `/immersive` | 温柔书友，聊剧情但不剧透    |
| 俯瞰分析 | `/overview`  | 犀利评论家，全局结构与写作技法 |
| 发散探索 | `/divergent` | 联网联想，书外知识延展     |

### 常用斜杠命令

| 命令                                    | 说明            | 命令          | 说明             |
| ------------------------------------- | ------------- | ----------- | -------------- |
| `/help`                               | 全部指令列表        | `/sessions` | 列出 / 加载 / 删除会话 |
| `/immersive` `/overview` `/divergent` | 切换模式          | `/fork`     | 从现有会话分叉新分支     |
| `/memory`                             | 查看、搜索、增删记忆    | `/resume`   | 恢复最近会话         |
| `/persona`                            | 查看或重载人格       | `/export`   | 导出会话为 Markdown |
| `/graph_init <book_id>`               | 构建知识图谱        | `/compact`  | 手动压缩会话历史       |
| `/graph_stats`                        | 图谱统计          | `/review`   | 审查历史会话提取洞察     |
| `/cost`                               | 当前会话 token 用量 | `/config`   | 查看或修改配置        |

完整列表见 `/help`。

### Web 界面

浏览器访问 `http://127.0.0.1:8766`：

- **书架** —— 书源扫描导入、章节目录
- **阅读** —— 正文阅读 + 边读边聊
- **图谱** —— BookGraph 可视化（类型轮盘过滤、邻居聚焦、节点详情）
- **作者档案** —— AuthorGraph 档案页（身份、年谱、作品、声音、来源）
- **写作** —— 作品树、文档式编辑、版本图、风格透镜
- **角色扮演** —— 与书中角色对话，支持角色音色
- **控制台** —— Provider / 模型 / 用量管理

## 🏗️ 架构一览

```
启动: main.py → config → 各 Service → runtime（ReAct 循环）
对话: 输入 → /命令 或 run_turn → 流式 LLM + 工具调用
        → 记忆管线（召回注入 + 同步）→ 结构化压缩 → SQLite 持久化
图谱: Scout → 骨架趟 → Explorer → 裁决环 → Recorder → graph.db → 前端可视化 / 对话注入
作者: 序跋采集 → 来源补全 → 声音采集 → 档案投影 → authors.db → 档案页 / 写作透镜
写作: 素材召回 → 细节卡 / 风格透镜 → 组装 → 版本图（git）→ writing.db
音色: 描述铸造 → 参考音频复制 → 角色扮演语音输出
```

| 域   | 入口                                                           | 存储            |
| --- | ------------------------------------------------------------ | ------------- |
| 主引擎 | `main.py` `runtime.py` `tools/` `commands.py` `compact.py` `chat_context.py` | —             |
| API  | `api_server.py` `api/routes/`                                | —             |
| 书库  | `services/` `file_readers/`                                  | `books.db`    |
| 会话  | `session.py` `services/session_service.py`                   | `sessions.db` |
| 记忆  | `memory_store.py` `memory_manager.py` `memory_pipeline/`     | `memory.db`   |
| 图谱  | `bookgraph/` `storage/graph_db.py`                           | `graph.db`    |
| 作者  | `authorgraph/` `storage/authors_db.py`                       | `authors.db`  |
| 写作  | `writegraph/` `services/writing_service.py`                  | `writing.db`  |
| 音色  | `voices.py` `tools/voice.py`                                 | `memory.db`   |
| 前端  | `reader/`（25 个 ES 模块 + 内嵌库）                              | —             |
| 提示词 | `prompts/*.md` `persona.py`                                  | —             |

```
book-companion/
├── main.py               # 入口：CLI Agent + API 服务（127.0.0.1:8766）
├── runtime.py            # ReAct 对话循环、系统提示分层
├── api_server.py / api/  # HTTP 组合根 + 按域拆分的路由
├── tools/                # 工具注册表与内置工具集
├── setup.bat / start.bat # Windows 一键安装 / 启动
├── services/             # 书库、导入、会话、写作服务
├── memory_pipeline/      # L0–L3 记忆管线
├── bookgraph/            # 图谱抽取流水线
├── authorgraph/          # 作者档案、来源与声音采集
├── writegraph/           # 写作账本、透镜与版本图
├── writebench/           # 写作能力评测台（离线）
├── storage/              # 六库 SQLite 封装
├── reader/               # Web 前端（js/ 模块 + lib/ 内嵌库）
├── prompts/              # 提示词模板（Markdown）
├── skills/               # 可插拔技能
├── scripts/              # 开发与运维脚本
└── flight_recorder/      # 可选：上下文飞行记录器（删文件夹即卸载）
```

## 💾 数据存储

全部本地，六个 SQLite 库分立（共享 `BaseDatabase` 基类）：

| 库             | 内容                 |
| ------------- | ------------------ |
| `books.db`    | 书目、章节、正文分块         |
| `sessions.db` | 会话、消息、消息分片         |
| `memory.db`   | 分层记忆、画像、音色角色绑定     |
| `graph.db`    | 图谱节点 / 边 / 别名 / 引文 |
| `writing.db`  | 作品、章节草稿、版本图        |
| `authors.db`  | 作者身份、来源、证据、声音特征    |

配置与产物在 `data/`：`config/providers.json`、`memory/persona.md`、`scenes/`、`voice/refs/` 等。删库即重置，无云端依赖。

## 👥 Credits

- **视觉与交互设计**：[@20221217chen-hue](https://github.com/20221217chen-hue)（书架 / 阅读 / 图谱等页面的视觉布局、动效与响应式设计）
- **架构与代码实现**：@[stefanie233](https://github.com/stefanie233)

## 📄 许可证

[MIT](LICENSE) © 2026 stefanie233

本项目参考/派生自若干第三方项目（MIT / BSD-2-Clause / ISC），其版权声明逐字收录于 [NOTICE](NOTICE)；`reader/lib/` 下的内嵌前端库随分发原样保留各自许可头。
