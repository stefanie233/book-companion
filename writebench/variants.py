"""配方表。每个配方只改一个可解释的变量，A/B 才有意义。"""

from __future__ import annotations

WRITER = ("火山", "doubao-seed-2.1-pro")
JUDGE = ("qwen", "qwen3.8-max")
PAIR_JUDGE = ("qwen", "qwen3.8-flash")
FAST = ("qwen", "qwen3.8-max")

MATERIAL_SUFFIX = """
## 参考素材怎么用

- 素材只提供**写法与细节质感**，不是本书设定。不得搬用其中的人名、地名、作品名或情节。
- 至少把两条素材转化成**本文人物与场景**的具体动作或物件；不要照抄原句。
- 素材与本章无关时，忽略它。为了用而用，比不用更糟。
- 素材的优先级低于正典、合同与人物状态；冲突时一律以正典为准。
- **素材是材料，不是任务。** 不要制定写作计划、步骤或提纲，
  也不要复述素材；只输出本章正文。
"""

MATERIAL_SUFFIX_V2 = """
## 参考素材怎么用

- 素材只提供**观察方式与细节质感**，不是本书设定，也不是情节建议。
- 先按合同与人物状态写。合同已经定好「何时揭示、是否解释往事」的，
  一律照合同，素材不得改变信息给法。
- 从素材里挑 1-2 个具体的部位、动作或物件，落到本文人物身上；
  不要照抄原句，也不要把素材里的人物关系搬过来。
- 素材与本章无关时忽略它。为了用而用，比不用更糟。
- 不要用素材里的意象给本章点题或收尾；结尾落在此刻的动作或物件上。
- **素材是材料，不是任务。** 不要制定写作计划、步骤或提纲，
  也不要复述素材；只输出本章正文。
"""

LENGTH_SUFFIX = """
## 长度纪律

本章目标 {target} 字，下限 {floor} 字。写到结尾时如果还差，
回到本章最关键的那个瞬间，补一段可感的动作、对白或物件细节；
不要用总结、抒情或重复来凑字数。
"""


def recipe(name: str, **over: object) -> dict[str, object]:
    base = {"name": name, "model": WRITER, "judge": JUDGE,
            "distill_model": FAST, "reference": "none", "voice": False,
            "focus": False, "pov": False, "budget": 6000, "ref_chars": 1400,
            "prompt": "production", "suffix": "", "pipeline": "single",
            "temperature": None}
    base.update(over)
    return base


VARIANTS = {
    "base": recipe("base"),
    "base_pov": recipe("base_pov", pov=True),
    "ref_raw": recipe("ref_raw", reference="raw", suffix="material"),
    "ref_card": recipe("ref_card", reference="card", pov=True,
                       suffix="material"),
    "ref_card_v2": recipe("ref_card_v2", reference="card2", pov=True,
                          focus=True, suffix="material2"),
    "prod": recipe("prod", reference="card2", pov=True, focus=True,
                   prompt="production"),
    "ref_card_v2_len": recipe("ref_card_v2_len", reference="card2", pov=True,
                              focus=True, suffix="material2+length"),
    "ref_hybrid_v2_len": recipe("ref_hybrid_v2_len", reference="hybrid2",
                                pov=True, focus=True,
                                suffix="material2+length"),
    "ref_card_v2_repair": recipe("ref_card_v2_repair", reference="card2",
                                 pov=True, focus=True, suffix="material2",
                                 pipeline="repair"),
    "ref_card_voice2": recipe("ref_card_voice2", reference="card",
                              pov=True, voice=True, suffix="material"),
    "voice": recipe("voice", voice=True, pov=True),
    "ref_card_voice": recipe("ref_card_voice", reference="card", pov=True,
                             voice=True, suffix="material"),
    "ref_card_focus": recipe("ref_card_focus", reference="card", focus=True,
                             pov=True, suffix="material"),
    "ref_card_len": recipe("ref_card_len", reference="card", focus=True,
                           pov=True, suffix="material+length"),
    "ref_card_voice_len": recipe("ref_card_voice_len", reference="card",
                                 voice=True, focus=True, pov=True,
                                 suffix="material+length"),
    "ref_card_repair": recipe("ref_card_repair", reference="card", focus=True,
                              pov=True, suffix="material", pipeline="repair"),
    "ref_card_voice_repair": recipe(
        "ref_card_voice_repair", reference="card", voice=True, focus=True,
        pov=True, suffix="material", pipeline="repair"),
}


def get(name: str) -> dict[str, object]:
    if name not in VARIANTS:
        raise KeyError(name)
    return dict(VARIANTS[name])
