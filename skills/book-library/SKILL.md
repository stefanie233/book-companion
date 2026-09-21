---
name: book-library
description: 书籍库系统 — 导入、存储、搜索、阅读已入库书籍
auto_activate: true
trigger_keywords: "书库, 导入, library, 搜索, 书籍, 入库, 读书, 阅读, 章节, 这本书, EPUB, MOBI, book_id, FTS5, bookbank, ZLibrary"
mode_constraints: "immersive, overview"
---

# 书籍库系统

书伴有一个基于 SQLite 的本地书籍库，将 EPUB/MOBI 文件解析并存储为结构化数据，支持快速章节读取和跨书全文搜索。

## Workflow Guidance

快速参考：
- 原始文件放在用户指定的卸货区文件夹（示例默认：`D:\Books\`；实际以用户告知路径为准），导入后自动复制到 `data/bookbank/`
- `import_book <path>` 导入 → MD5 内容去重 → 自动清洗 → `library_read` 读取
- `library_search <query>` 跨书全文搜索，`library_list` 查看已入库书籍
- 每本书的 `book_id` 是文件内容的 MD5 前 16 位

## 数据流全景

```
[卸货区文件夹]（示例：D:\Books） ← 原始 EPUB/MOBI 文件
    │
    │ import_book <path>
    │   ├─ MD5(文件内容) → book_id（同内容去重，异译共存）
    │   ├─ 复制到 data/bookbank/{book_id}.epub（自包含本地副本）
    │   ├─ 逐章解析 → 存入 book_chapters（status='raw'）
    │   └─ post_tool hook → clean_book() 自动清洗 → status='clean'
    │
    ▼
data/book-companion.db         ← SQLite 数据库（与 sessions 库共用文件）
    ├─ books                   元数据：id, title, author, format, status
    ├─ book_chapters           逐章清洁文本：(book_id, chapter_index) 唯一索引
    ├─ book_categories         树状分类（parent_id 自引用）
    └─ book_fts                FTS5 虚拟表 + 3 触发器自动同步
```

## 可用工具

| 工具 | 用途 | 示例 |
|------|------|------|
| `import_book` | 导入 EPUB/MOBI 入库（含去重） | `import_book <卸货区>\人类简史.epub` |
| `library_list` | 列出书库中所有书籍（标题/格式/章节/字数/状态） | `library_list` |
| `library_read` | 从 DB 读取章节（无需解析文件，极快） | `library_read book_id=<id> chapter_index=0` |
| `library_search` | FTS5 跨书全文搜索（返回摘录 + 位置上下文） | `library_search 自由意志` |

## 导入流程详解

1. **`import_book(path)`** → `LibraryService.import_book()`
2. 计算文件内容 MD5 → `book_id`（前 16 位 hex）
3. 查 `books` 表 → 已存在则返回已有记录 + `is_new=False`
4. 复制文件到 `data/bookbank/{book_id}.epub`（`data/` 完全自包含）
5. 用 `EpubReader` / `MobiReader` 逐章解析（MOBI 整体作为单章）
6. 章节入 `book_chapters`，状态 `raw`
7. Hook 系统自动触发 `clean_book(book_id)` — 四步清洗管道
8. 清洗后状态更新为 `clean`

## 去重策略

- **内容 hash** = 文件级别的 MD5 — 路径无关、文件名无关
- 同一个 EPUB 从不同位置导两次 → 第二次返回已有记录
- 同一本书的不同译本 → 内容不同 → 不同 book_id → 共存
- 同名文件可能覆盖 `data/bookbank/` 中的本地副本（但 ID 不变）

## 清洗管道

导入后自动执行的四步清洗（通过 `post_tool` hook，规则定义在 `book-cleaner` skill）：

1. **重复行检测** — 统计非空行频率，超过阈值 → 页眉/页脚 → 移除
2. **页码移除** — 独立数字行（`^\s*\d{1,4}\s*$`）
3. **空白规范化** — 连续空行合并为单个空行
4. **编码残留** — 零宽字符、BOM、连续标点符号

清洗后可调用 `clean_book(book_id, rules={...})` 自定义规则重新清洗。

## FTS5 全文搜索

- 虚拟表 `book_fts` 通过 3 个 SQLite 触发器与 `book_chapters` 自动同步
- 默认 `unicode61` tokenizer — 对 CJK 按单字索引
- `library_search <query>` 返回章节摘录 + 书目标题/作者/章节名
- 搜索范围：所有已入库书籍的全部章节

## 常见工作流

**首次导入一本书：**
```
import_book <卸货区>\书名.epub
→ 返回 book_id + 章节数 + 字数 → 自动清洗 → 可读
```

**浏览书库并阅读：**
```
library_list                    → 查看所有书 + book_id
library_read <book_id> 0        → 读第 0 章
library_read <book_id> 5        → 读第 5 章
```

**跨书搜索主题：**
```
library_search 存在主义         → 在所有书的章节中搜索
```

**检查导入质量：**
```
library_list status=raw         → 查看未清洗完成的
library_read <book_id> 0        → 抽查章节内容
```

## 设计决策

- **单库而非一书一库** — 跨书搜索无 `ATTACH` 开销，逻辑隔离靠 WHERE 子句
- **内容 hash 做 book_id** — 去重精确到字节级别
- **本地副本自包含** — `data/bookbank/` 独立于卸货区文件夹（示例：D:\Books），后者可随时清空
- **Hook 驱动清洗** — 导入和清洗解耦，清洗规则可独立修改（见 `book-cleaner` skill）
