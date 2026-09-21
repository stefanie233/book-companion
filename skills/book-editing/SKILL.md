---
name: book-editing
description: 书籍库编辑 — 元数据修改、章节编辑、查找替换、分类管理
auto_activate: true
trigger_keywords: "编辑, 修改, 替换, 删除章节, 删除书, 删书, 改名, 作者, 分类, 查找替换, 回退, 版本, 快照, 历史版本, 撤销, library_edit, library_replace, library_delete_chapter, library_delete_book, library_manage_categories, library_list_versions, library_rollback, 改错, 纠错, 修正, 订正"
mode_constraints: "immersive, overview"
---

# 书籍库编辑能力

在书籍库（`book-library` skill）基础上，提供元数据修改、章节内容编辑、WPS 式查找替换和分类管理能力。

## 可用工具

| 工具 | 用途 | 权限 |
|------|------|------|
| `library_edit_book` | 修改书名、作者、分类 | 写 |
| `library_edit_chapter` | 整章重写内容或标题 | 写 |
| `library_replace` | 查找替换（推荐首选） | 写 |
| `library_delete_chapter` | 删除章节（不可逆） | 写 |
| `library_delete_book` | 删除整本书（不可逆） | 写 |
| `library_manage_categories` | 分类增删改查 | 写 |

## 工具选择指南

```
要改的东西               → 用什么工具
─────────────────────────────────────────────
书名/作者/分类错了        → library_edit_book
章节标题不对              → library_edit_chapter (只传 title)
整章内容需要重写          → library_edit_chapter (传 content)
文中某几处文字要改        → library_replace   ← 优先用这个！
某段话要删                → library_replace (new_text="")
删除整个章节              → library_delete_chapter（先确认）
创建/重命名/删除分类      → library_manage_categories
```

**原则：能替换不重写。** `library_replace` 是手术刀，`library_edit_chapter` 是重武器。只有需要整章替换时才用后者。

## library_replace 渐进式工作流

这是最常用的编辑工具。遵循**两阶段确认**模式：

### 阶段 1：预览扫描

```
library_replace(book_id, chapter_index, old_text="要找的文字", new_text="替换为")
```

- 默认 `dry_run=true`（只预览，不修改）
- 默认 `context=50`（前后各 50 字上下文）
- 显示每处匹配的 before/after 对比

### 阶段 2：渐进确认（按需）

如果 context=50 看不清语义，拉大上下文：

```
library_replace(book_id, chapter_index, old_text="...", new_text="...", context=200)
```

如果匹配太多（如预览被截断到 10 条），缩小范围后再操作。

### 阶段 3：执行

确认无误后：

```
library_replace(book_id, chapter_index, old_text="...", new_text="...", dry_run=false)
```

### 阶段 4：验证

```
library_search <new_text>   → 确认新版存在
library_search <old_text>   → 确认旧版已清除
```

## literal vs regex 模式

| 场景 | 模式 | 示例 |
|------|------|------|
| 改错别字 | literal | `old_text="象往"` `new_text="向往"` |
| 统一标点风格 | literal | `old_text="——"` `new_text="—"` |
| 删除广告/水印 | literal | `old_text="请关注XX公众号"` `new_text=""` |
| 格式化章节标题 | regex | `old_text="^第(\d+)章"` `new_text="第\1章 "` |
| 清理多余空行 | regex | `old_text="\n{3,}"` `new_text="\n\n"` |
| 移除所有英文 | regex | `old_text="[a-zA-Z]+"` `new_text=""` |

**regex 模式需要 dry_run 预览**——正则写错了匹配范围可能远超预期。

## 字符支持

Python 原生 Unicode 字符串操作，支持：
- 中文汉字、日文假名、韩文
- 全角/半角标点、emoji
- 空格、全角空格、换行符、制表符
- 零宽字符（BOM、ZWJ 等）

在 `old_text` 中直接写要查找的文字即可，无需转义（regex 模式下正则元字符需转义）。

## library_delete_chapter 注意事项

- **不可逆**——必须先向用户确认
- 删除后不重索引：其他章节保持原有 `chapter_index`（留空位），`library_read` 仍按索引升序返回
- 书的 `chapter_count` 和 `total_chars` 自动更新

## 分类管理

```
library_manage_categories action="list"                      → 查看全部分类
library_manage_categories action="create" name="科幻"        → 创建分类
library_manage_categories action="update" category_id="X" name="科学幻想"  → 重命名
library_manage_categories action="delete" category_id="X"    → 删除（书籍自动脱分类）
```

## 常见工作流

**改一本书的作者：**
```
library_edit_book book_id=<id> author="正确的作者名"
→ 返回更新后的书籍摘要
```

**修正章节内多处同一错别字：**
```
library_replace book_id=<id> chapter_index=3 old_text="象往" new_text="向往"
→ 预览匹配数 + 每处 before/after
→ 确认无误 → dry_run=false
→ library_search "向往"  + library_search "象往"  双向验证
```

**清理章节内广告插入：**
```
library_replace book_id=<id> chapter_index=0 old_text="【版权所有，禁止转载】" new_text="" mode="literal"
→ 预览后执行，或用 regex 匹配变体：old_text="【版权所有[^】]*】" mode="regex"
```

**整章标题重命名：**
```
library_edit_chapter book_id=<id> chapter_index=5 title="第六章：转折"
```

**按分类整理书库：**
```
library_manage_categories action="create" name="历史"
library_manage_categories action="list"
library_edit_book book_id=<id> category_id="<新分类ID>"
```

**删除整本书（不可逆！）：**
```
library_delete_book book_id=<id> confirm="delete <书名>"
→ 必须先向用户确认
→ confirm 参数必须精确匹配书名，防止误删
→ 会同时删除所有章节 + data/bookbank/ 中的本地副本
```

## library_delete_book 注意事项

- **不可逆**——删除后无法恢复，章节和本地文件均被清除
- **安全机制**：`confirm` 参数必须为 `"delete <书名>"`（精确匹配），否则拒绝执行
- 典型流程：先 `library_list` 确认要删的书 → 向用户确认 → `library_delete_book`

## 版本控制

每次编辑操作（`library_edit_chapter`、`library_replace`、`library_delete_chapter`）会自动在 `data/versions/` 保存一份快照。Chapter 内容完整保存为 txt 文件，元数据记录在 `book_versions` 表中。

| 工具 | 用途 | 权限 |
|------|------|------|
| `library_list_versions` | 查看章节的所有历史版本 | 读 |
| `library_rollback` | 回退到指定版本 | 写 |

**查看历史：**
```
library_list_versions book_id=<id> chapter_index=3
→ 列出时间/操作/字数差/版本ID
```

**回退到之前版本：**
```
library_rollback version_id=<vid>
→ dry_run=true 预览（默认）
→ 确认后 dry_run=false
→ 回退操作本身也会创建新版本（可再次回退）
```

**删除 data/versions/ 目录 → 快照文件丢失，但系统正常工作。** `book_versions` 表中记录仍在，但 `library_rollback` 会报 snapshot 文件缺失。
