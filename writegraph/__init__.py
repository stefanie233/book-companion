"""WriteGraph 域包 —— 写作账本与注意力透镜。

分工与 ``authorgraph/`` 一致：**判定在这里，落库在 ``storage/writing_db.py``**。
本包全部是纯函数，不碰数据库连接；测试不需要 LLM，也不需要真实库。
"""

from __future__ import annotations

__all__ = ["facts", "promises", "state", "card"]
