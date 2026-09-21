---
name: review
description: 会话审查 — 自动审查历史会话，提取偏好/洞察/修正/边界为结构化 moments
auto_activate: false
trigger_keywords: "审查, review, 回顾, 复盘, 总结会话"
mode_constraints: ""
hooks_module: "hooks.py"
tools_module: "tools.py"
---

# 会话审查（Review Hook + Skill）

## 概述

审查系统在新会话开始时自动触发，批量审查所有未审查的历史会话。工作流分为两步：

1. **关键词打标** — LLM 给每个会话打上贴切的关键词（用于后续 RAG 检索）
2. **按 mode 分组审查** — 同一交互模式的会话合并审查，提取有长期价值的 moments

流水账会话自动 pass，不浪费存储。

## 与记忆系统的关系

- 审查提取的 moments 存储在 `wingman.db` 的 `moments` 表中
- 关键词存入 `sessions.keywords`，供 `ContextBrief` 的 RAG 检索使用
- 审查完成后盖戳 `sessions.time_reviewed`，已审查+已归档的会话不再重复审查

## Workflow Guidance

当用户说"审查会话"或"review"时：

1. **自动审查** — 新会话启动时 hook 自动触发，无需手动干预
2. **手动审查** — 使用 `/review` 命令或 `review_sessions` 工具手动触发
3. **审查范围** — 默认审查所有未审查+未归档的会话，也可指定特定 session_id

## 审查标准

审查不是记录一切——只记录具有**跨会话价值**的内容：

| 值得记录 | pass（跳过） |
|---------|-------------|
| 读者明确表达的偏好/厌恶 | 日常寒暄 |
| 有新意的洞察或连接 | 简单的工具调试 |
| 观点的明确转变 (before → after) | 纯信息查询（无情感投入） |
| 设定的交互边界 | 模式切换等操作指令 |

---

## 工具参考

| 工具 | 用途 |
|------|------|
| `review_sessions` | 手动触发会话审查 |
| `/review` | 斜杠命令，审查所有未审查会话 |
| `/review <session_id>` | 审查指定会话 |
