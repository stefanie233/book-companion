# ✈ 飞行记录仪（Context Flight Recorder）

临时的**提示词/上下文监控**工具：记录每个 ReAct 轮次发给 LLM 的完整请求
（system_prompt + messages + tools）、LLM 返回（含 thinking + tool_calls）、
以及工具执行结果。等上下文管理和提示词打磨好后即可卸载。

- 数据：`data/context_log.db`（SQLite，`context_snapshots` 表，WAL）
- 查看页：`/flight-recorder/viewer.html`（自包含，旧 URL `/context-log.html` 会重定向）
- API：`/api/context-log`（list / session / round / clear）
- Hermes 兼容：自动读取 `~/.hermes/context_log.db` 一起展示

## 集成点（改这几个文件时注意）

| 文件 | 集成点 |
|------|--------|
| `api_server.py` | `create_app` 调 `install_context_log_routes(app, context_recorder)` 一行；`run_server` __main__ 用 `create_recorder(...)`；SSE /api/chat 里有 `_recorder` / `_rec_input` guarded 记录块 |
| `main.py` | `from flight_recorder import create_recorder` + 构造；`context_recorder=` 参数透传若干处 |
| `runtime.py` | 无 import（duck-typed）：`self.context_recorder = None` + 4 处 guarded `record_round` 块 |

## 卸载清单

1. 删整个 `flight_recorder/` 文件夹
2. `api_server.py`：删 `install_context_log_routes(app, context_recorder)` 调用、
   `create_app` 的 `context_recorder` 参数、`run_server` __main__ 的 `create_recorder(...)`、
   SSE /api/chat 里的 `_recorder` / `_rec_input` guarded 块
3. `main.py`：删 `create_recorder` import/构造 + 各 `context_recorder=` 透传参数
4. `runtime.py`：删 `self.context_recorder = None` + 4 处 guarded `record_round` / `_tool_results` 块
5. 可选：删 `data/context_log.db`
