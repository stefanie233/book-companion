"""Smoke tests for model_profiles + tokenizer_service — run:
    python scripts/test_model_profiles.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from model_profiles import ModelProfileStore


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # roundtrip + defaults
        store = ModelProfileStore(tmp / "config" / "model_profiles.json")
        profs = store.get_profiles()
        assert profs["tokenizer"]["path"] == ""  # 默认无预设，走估算兜底；真实路径需配置
        assert profs["models"]["glm-5.3-flash"]["context_window"] == 1048576
        assert profs["models"]["*"]["context_window"] == 128000

        # save + reload (cross-instance persistence)
        store.save_profiles(
            {"m1": {"context_window": 200000}},
            {"path": "D:/tok/x.json", "enabled": False},
        )
        again = ModelProfileStore(tmp / "config" / "model_profiles.json")
        assert again.resolve("m1")["context_window"] == 200000
        assert again.resolve("unknown")["context_window"] == 128000  # no '*' → hard default
        print("  roundtrip + persistence: ok")

        # corrupt file quarantined to .bak, defaults restored
        p = tmp / "config" / "model_profiles.json"
        p.write_text("{broken!!", encoding="utf-8")
        recovered = ModelProfileStore(p)
        assert "glm-5.3-flash" in recovered.get_profiles()["models"]
        assert p.with_name(p.name + ".bak").exists()
        print("  corrupt quarantine: ok")

        # save validation rejects
        for bad_models in (
            {"": {"context_window": 1}},                    # empty name
            {"m": {"context_window": -5}},                  # bad window
        ):
            try:
                ModelProfileStore(tmp / "v" / "mp.json").save_profiles(
                    bad_models, {"path": "", "enabled": True})
                raise AssertionError(f"expected ValueError for {bad_models}")
            except ValueError:
                pass
        try:
            ModelProfileStore(tmp / "v2" / "mp.json").save_profiles(
                {"m": {"context_window": 1}}, {"path": ""}, {"": "D:/x.json"})
            raise AssertionError("expected ValueError for empty vendor alias")
        except ValueError:
            pass
        print("  validation rejects: ok")

        # 分词器三级选路：显式覆盖 → 厂商别名（含前缀变体）→ 全局兜底
        sel = ModelProfileStore(tmp / "sel" / "mp.json")
        sel.save_profiles(
            {"glm-5.3-flash": {"context_window": 1048576,
                               "tokenizer_path": "D:/override/tok.json"},
             "*": {"context_window": 128000}},
            {"path": "D:/fallback/tok.json", "enabled": True},
            {"glm,zhipu": "D:/v/zhipu.json", "deepseek": "D:/v/deepseek.json"})
        assert sel.resolve_tokenizer("glm-5.3-flash") == "D:/override/tok.json"
        assert sel.resolve_tokenizer("ZHIPU/GLM-5.3-Flash") == "D:/v/zhipu.json"
        assert sel.resolve_tokenizer("vanchin/deepseek-v4-pro") == "D:/v/deepseek.json"
        assert sel.resolve_tokenizer("kimi-k3") == "D:/fallback/tok.json"
        sel.save_profiles({"*": {"context_window": 128000}},
                          {"path": "D:/fallback/tok.json", "enabled": False})
        assert sel.resolve_tokenizer("glm-5.3-flash") == ""   # 总开关关闭
        print("  tokenizer routing (override/vendor/fallback): ok")

        # 相对路径 (data/tokenizers/...) 锚到 store 所在项目根，不依赖 CWD
        relp = ModelProfileStore(tmp / "relp" / "mp.json")
        relp.save_profiles(
            {"*": {"context_window": 128000}},
            {"path": "data/tokenizers/mimo tokenizers/MiMo-V2.5/tokenizer.json",
             "enabled": True},
            {"mimo": "data/tokenizers/mimo tokenizers/MiMo-V2.5/tokenizer.json"})
        got = relp.resolve_tokenizer("mimo-v2.5")
        rp = Path(got)
        assert rp.is_absolute(), got
        assert rp.as_posix().endswith(
            "data/tokenizers/mimo tokenizers/MiMo-V2.5/tokenizer.json"), got
        print("  relative path resolution: ok")

        # tokenizer_service: real tokenizer vs fallback（默认无预设路径，
        # 无分词器文件的机器上跳过实算断言，只验兜底）
        from tokenizer_service import count_tokens
        # 全新实例默认预设为空 → 直接走估算兜底
        real = ModelProfileStore(tmp / "fresh" / "mp.json").resolve_tokenizer(
            "glm-5.3-flash")
        assert real == "", real
        n2, est2 = count_tokens("hello", r"D:\nonexistent\tok.json")
        assert est2 and n2 == 5 // 4
        print("  tokenizer counting + fallback: ok")

    print("all model_profiles tests passed")


if __name__ == "__main__":
    main()
