"""Smoke tests for llm_json — run:
    python scripts/test_llm_json.py

覆盖三类：① 裸引号修复（含逗号消歧这个核心难点）；② 阶梯与类型契约；
③ 安全性质 —— 合法 JSON 不被改动、修复路径不产出泄漏对象。
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from llm_json import (loads_tolerant, repair_control_chars,
                      repair_fullwidth_punct, repair_unescaped_quotes)


def main() -> None:
    # ── ① 裸引号修复 ────────────────────────────────────────────────────
    # 逗号消歧：值内容里的引号后跟逗号，不能当成收尾引号（对象里逗号后面
    # 应该是下一个键，不像键就是内容里的引号）。
    for src, want in [
        ('{"a": "把爱情与"德行","圣洁"对立"}', {"a": '把爱情与"德行","圣洁"对立'}),
        ('{"a": "他说"你好",然后走了"}', {"a": '他说"你好",然后走了'}),
        ('{"a": "他说"你好" , 然后走了"}', {"a": '他说"你好" , 然后走了'}),
        ('{"a": "他"好", 然后", "b": "她"美"}',
         {"a": '他"好", 然后', "b": '她"美'}),
        ('{"a": "他说"你好""}', {"a": '他说"你好"'}),
        ('{"a": "他说"你好" 然后走了"}', {"a": '他说"你好" 然后走了'}),
    ]:
        got = loads_tolerant(src, expect=dict)
        assert got == want, f"{src!r}\n  got={got!r}\n want={want!r}"
    print("  bare-quote repair (comma disambiguation): ok")

    # 数组里的逗号后面是下一个**值**不是键，所以一定是收尾引号。
    got = loads_tolerant('[{"t":"他"好"", "n":1}]', expect=list)
    assert got == [{"t": '他"好"', "n": 1}], got
    got = loads_tolerant('{"ids": ["a1", "b2"], "x": "他"好""}', expect=dict)
    assert got == {"ids": ["a1", "b2"], "x": '他"好"'}, got
    print("  bare-quote repair (array comma): ok")

    # ── ② 阶梯与类型契约 ────────────────────────────────────────────────
    assert loads_tolerant('{"a":1}', expect=dict) == {"a": 1}
    assert loads_tolerant('前言\n```json\n{"a":1}\n```\n后记', expect=dict) == {"a": 1}
    assert loads_tolerant('前言\n```json\n[{"a":1}]\n```\n后记', expect=list) == [{"a": 1}]
    assert loads_tolerant('前言 [{"a":1}] 后记', expect=list) == [{"a": 1}]
    print("  ladder (direct / fence / bracket slice): ok")

    # 首{末} 与 首[末] 并存：保留两条旧阶梯各自的既有胜例
    assert loads_tolerant('{"a":1} [2,3]', expect=dict) == {"a": 1}
    assert loads_tolerant('{"a":1} [2,3]', expect=list) == [2, 3]
    print("  bracket+brace slices coexist: ok")

    # expect 类型契约：错类型一律 None（旧 scout 拿顶层数组会 AttributeError）
    assert loads_tolerant('{"a":1}', expect=list) is None
    assert loads_tolerant('[1,2]', expect=dict) is None
    assert loads_tolerant('{"a":1}', expect=None) == {"a": 1}
    print("  expect type contract: ok")

    # 边界
    assert loads_tolerant(None) is None
    assert loads_tolerant(123) is None
    assert loads_tolerant("") is None
    assert loads_tolerant("   ") is None
    print("  edge inputs: ok")

    # ── ③ 安全性质 ──────────────────────────────────────────────────────
    # 合法 JSON 不得被 repair 改动（含已有转义序列，不得二次转义）。
    rng = random.Random(20261008)
    alpha = list('ab"\\:,{}[] \t\n\r中é') + [" ", "\x00"]
    for _ in range(20_000):
        obj = {"".join(rng.choice(alpha) for _ in range(rng.randint(0, 6))):
               "".join(rng.choice(alpha) for _ in range(rng.randint(0, 10)))}
        s = json.dumps(obj, ensure_ascii=rng.random() < 0.5)
        assert repair_unescaped_quotes(s) == s, repr(s)
        assert loads_tolerant(s, expect=dict) == obj, repr(s)
    print("  valid JSON untouched (20k fuzz): ok")

    # 新加的两档修复同样必须是合法 JSON 上的恒等变换（字母表含全角标点）。
    alpha2 = list('ab"\\:,{}[] \t\n\r中é，：；')
    for _ in range(20_000):
        obj = {"".join(rng.choice(alpha2) for _ in range(rng.randint(0, 6))):
               "".join(rng.choice(alpha2) for _ in range(rng.randint(0, 10)))}
        s = json.dumps(obj, ensure_ascii=rng.random() < 0.5)
        assert repair_control_chars(s) == s, repr(s)
        assert repair_fullwidth_punct(s) == s, repr(s)
        assert loads_tolerant(s, expect=dict) == obj, repr(s)
    print("  valid JSON untouched, new repairs (20k fuzz): ok")

    # 修复路径不得产出「值内容泄漏成键」的合法但语义错误对象。
    assert loads_tolerant('{"":": ""}', expect=dict) is None
    print("  leaky-key guard: ok")

    # 深嵌套不得让 RecursionError 逃逸。
    assert loads_tolerant("[" * 20000 + "]" * 20000, expect=list) is None
    print("  deep nesting does not raise: ok")

    # 字符串字面量的内容是数据不是结构，不得从里面切出 {} / []。
    assert loads_tolerant('"[8]"', expect=list) is None
    assert loads_tolerant('"中{}"', expect=dict) is None
    print("  no structure fabricated from string literal: ok")

    # ── ④ 另两种语法走形（各自独立，绝不串联）────────────────────────────
    # 实测《我与地坛》某轮 65 次调用里 2 次栽在这两种形态上，各丢一整批 5-7 块。
    # (a) 结构位全角逗号 —— 无歧义，字符串**外**才换。
    got = loads_tolerant('{"a": "正文。",\n "b": "他说，好：真的"}', expect=dict)
    assert got == {"a": "正文。", "b": "他说，好：真的"}, got
    print("  fullwidth structural punct repair: ok")

    # (b) 字符串内字面换行 —— 字面控制字符在 JSON 字符串里永远非法。
    got = loads_tolerant('{"a": "上句\n下句"}', expect=dict)
    assert got == {"a": "上句\n下句"}, got
    print("  literal control char in string repair: ok")

    # 字符串**外**的换行是合法空白，一律不动（缩进不能被改成 \n 文本）。
    pretty = '{\n  "a": 1,\n  "b": "，："\n}'
    assert repair_control_chars(pretty) == pretty
    assert repair_fullwidth_punct(pretty) == pretty
    print("  outside-string newlines / in-string fullwidth untouched: ok")

    # **字符串没收尾**（模型漏了收尾引号）无从推断边界 —— 必须失败，
    # 绝不能修成「合法但把 evidence 吞进 why」的对象。
    assert loads_tolerant('{"why": "男人，\n     "evidence": "个子很高"}') is None
    print("  unterminated string still fails (no silent field swallowing): ok")

    print("ok")


if __name__ == "__main__":
    main()
