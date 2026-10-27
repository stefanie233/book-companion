"""``reuse_chunk_ids`` 的单元测试（纯函数，零 IO）。

盯的是一件事：**重导不许把图谱锚点冲掉**。

``graph.db`` 把 ``graph_chunks.chunk_id`` 当锚点，边、引文、观察、节点首现
全挂在它上面。重导换一批新主键，这些锚点就集体悬空 —— 实测 9 本里 8 本
交集为 0，其中人类简史 122/312、大棋局 55/144 事后也救不回来（章都重切了）。

判据只有一条：**同坐标 + 逐字节相同 → 复用旧主键**。差一个字节就不复用 ——
内容是新的，身份也该是新的。

跑法：``python scripts/test_chunk_reuse.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.library_service import reuse_chunk_ids      # noqa: E402
from storage.models import BookChunkRow                   # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def row(ci: int, ck: int, text: str, cid: str) -> BookChunkRow:
    return BookChunkRow(id=cid, book_id="b", chapter_index=ci, chunk_index=ck,
                        content=text, char_count=len(text), paragraph_count=1,
                        paragraph_offsets="[]")


def main() -> int:
    old = [row(0, 0, "甲", "old-0"), row(0, 1, "乙", "old-1"),
           row(1, 0, "丙", "old-2")]

    # ① 内容没变 → 复用
    new = [row(0, 0, "甲", "n-a"), row(1, 0, "丙", "n-c")]
    stats = reuse_chunk_ids(new, old)
    check("同坐标同内容 → 复用旧主键", [r.id for r in new], ["old-0", "old-2"])
    check("复用计数", (stats["reused"], stats["fresh"]), (2, 0))
    check("消失的块报出来", stats["disappeared"], [(0, 1)])

    # ② 差一个字节 → 不复用（内容是新的，身份也该是新的）
    new = [row(0, 0, "甲 ", "n-a")]
    check("差一个字节就不复用",
          [r.id for r in reuse_chunk_ids(new, old) and new], ["n-a"])

    # ③ 换坐标 → 新主键
    new = [row(2, 0, "甲", "n-a")]
    stats = reuse_chunk_ids(new, old)
    check("换了坐标就是新块", (new[0].id, stats["reused"], stats["fresh"]),
          ("n-a", 0, 1))

    # ④ 没有旧块（首次导入）→ 全部新主键，不炸
    new = [row(0, 0, "甲", "n-a")]
    stats = reuse_chunk_ids(new, [])
    check("首次导入原样通过", (new[0].id, stats["reused"], stats["old"]),
          ("n-a", 0, 0))

    # ⑤ 重导没有内容改动 → 一个主键都不该换
    new = [row(c.chapter_index, c.chunk_index, c.content, f"n-{i}")
           for i, c in enumerate(old)]
    stats = reuse_chunk_ids(new, old)
    check("内容全同 → 零新主键", (stats["reused"], stats["fresh"],
                                  stats["disappeared"]),
          (3, 0, []))

    print(f"\n{'FAILED ' + str(FAILED) if FAILED else 'ALL PASS'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
