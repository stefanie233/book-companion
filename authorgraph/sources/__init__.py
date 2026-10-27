"""来源管线：发现 → 抓取 → 抽引文 → 校验 → 合并 → 落库。

设计稿：``docs/authorgraph/02/03``。
本包只做判定与外部 IO，**不直接写库** —— 写入口仍是
``storage/authors_db.py``，编排在 ``pipeline.py``。

与 ``authorgraph/harvest.py`` 的分工：采集读**书里**的序跋，只产
corpus/records；本包读**书外**的网页，只产 source/evidence/record/claim。
两边共用「证据必须可定位」这一条，但证据的形状不同：
书里是逐字原文对块，网页是逐字引文对页面正文。
"""
