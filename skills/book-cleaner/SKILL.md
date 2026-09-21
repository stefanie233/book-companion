---
name: book-cleaner
description: 书籍导入后自动清洗 — 移除页眉页脚、页码、编码残留等非正文内容
---

# 书籍数据清洗

此技能在书籍导入后自动触发（通过 `post_tool` hook），对存入 `book_chapters` 的原始文本执行清洗。

## 清洗规则

| 规则 | 说明 |
|------|------|
| `remove_repeated_headers` | 检测并移除重复出现的行（页眉/页脚），阈值：出现次数超过 行数/唯一行数×2 |
| `remove_page_numbers` | 移除独立成行的纯数字（页码） |
| `collapse_blank_lines` | 合并连续空行，最多保留一个空行 |
| `strip_trailing_punctuation_clutter` | 移除连续标点符号、零宽字符、BOM 等电子书残留 |

## 执行时机

- **触发方式**: `post_tool` hook，每次 `import_book` 执行完成后自动调用
- **处理范围**: 仅处理 `status='raw'` 的章节
- **清洗后**: 章节和书籍的 `status` 更新为 `'clean'`

## 清洗前后对比

清洗完成后，hook 会输出各章节的 `chars_before` / `chars_after` / `removed` 统计，供人工确认。

## 自定义规则

修改 `services/library_service.py` 中的 `DEFAULT_CLEANING_RULES` 字典，或通过 hook 回调传入自定义规则。
