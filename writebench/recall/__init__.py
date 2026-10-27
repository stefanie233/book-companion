"""参考素材召回评测：语义索引 + 混合检索。

只做离线评测，不接生产写作路径。产物落在 ``C:\\tmp\\wb_index``。
"""

from __future__ import annotations

INDEX_DIR = r"C:\tmp\wb_index"
EMBED_MODEL = "doubao-embedding-vision"
