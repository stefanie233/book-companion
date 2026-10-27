"""声音采集（M2）—— 从作者写下的正文里提炼**可迁移的写法**。

与事实账本的分工：

- ``authorgraph.sources``：查网页，只写事实（``author_records`` / claims）。
- ``authorgraph.voice``：读书里的正文，只写声音（``author_corpus`` +
  ``author_voice_traits``）。

两边共享作者身份与来源，但**不共享可见性闸**（事实只看序跋，声音看正文），
也**不共享写入口**（``store_harvest`` / ``store_voice`` 平级，禁止互调）。

入口：``harvest_voice``（编排）与 ``select_for_scene``（写作侧只读）。
"""

from __future__ import annotations

from authorgraph.voice.pipeline import harvest_voice
from authorgraph.voice.select import eligible_traits, select_for_scene
from authorgraph.voice.vocab import PROMPT_VERSION

__all__ = ["PROMPT_VERSION", "eligible_traits", "harvest_voice",
           "select_for_scene"]
