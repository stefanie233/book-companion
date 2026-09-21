# Book Companion（书伴）— 初始化指南

基于 AI 的 CLI 阅读陪伴助手。Python 3.12 + asyncio + prompt_toolkit + SQLite。

## 快速开始

```bash
# 1. 运行初始化向导（装依赖 + 创建 .env + 自动启动）
setup.bat

# 2. 浏览器打开后，在「连接 AI 服务」引导弹窗中
#    填入接口地址 / API Key / 模型名，点「测试并连接」
```

浏览器会自动打开 `http://127.0.0.1:8765/`。

> 高级用法：也可以不开向导，手动 `copy .env.example .env` 并填写，再运行 `start.bat`。

## 技术栈

| 层 | 组件 |
|----|------|
| 语言 | Python 3.12 |
| 依赖 | aiohttp, prompt_toolkit, watchdog, rank-bm25, jieba |
| 异步 | asyncio + aiohttp |
| 终端 | prompt_toolkit |
| 存储 | SQLite (books.db / sessions.db / memory.db / graph.db) |
| 前端 | 原生 HTML/CSS/JS，零构建 |
| LLM | OpenAI 兼容 API（API Key + Base URL + Model） |
| 搜索 | BM25 (rank-bm25 + jieba) |
| ASR | SenseVoiceSmall ONNX (~231MB)，funasr_onnx 推理 |

## 可选组件

| 组件 | 大小 | 用途 |
|------|------|------|
| ASR 模型 (SenseVoiceSmall) | ~231 MB | 浏览器麦克风语音输入 |
| Tavily API Key | 免费注册 | Web 搜索工具 |

| MOBI 支持 | `pip install mobi` | 导入 MOBI 格式电子书 |

ASR 模型可在 `setup.bat` 中选择下载，也可稍后手动运行：

```bash
.venv\Scripts\pip install modelscope
python scripts\download_asr_model.py
```

## 项目结构

```
book-companion/
  main.py            # 入口：CLI REPL + API server
  setup.bat          # 初始化向导
  start.bat          # 启动脚本
  .env               # LLM API 配置（setup.bat 自动创建，可留空走网页引导）
  settings.json      # 运行参数（默认值开箱即用）
  data/              # 运行时数据（SQLite / 模型 / 缓存）
    bookbank/        # 导入的电子书文件
    models/          # ASR 模型
  reader/            # Web 前端（纯 HTML/CSS/JS，无需构建）
```

## 常见问题

### ASR 下载失败？

ModelScope 国内访问较慢，可重试。模型较大（~231MB），需要稳定的网络。ASR 不可用时其他功能不受影响。

### 端口冲突？

默认 8765。修改 `api_server.py` 底部的 `run_server()` 调用参数。

### 更新依赖？

```bash
.venv\Scripts\pip install -r requirements.txt --upgrade
```
