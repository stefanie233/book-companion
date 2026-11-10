"""Download SenseVoiceSmall ONNX model for Liubai ASR.

Downloads only the 6 essential files (~231 MB total) from two ModelScope repos,
avoiding the 936 MB PyTorch weight in the base repo.

Usage:
    python scripts/download_asr_model.py

Files downloaded:
    From iic/SenseVoiceSmall-onnx:
        model_quant.onnx  (230 MB)  — quantized ONNX model
        am.mvn            (11 KB)   — acoustic model normalization
        config.yaml       (2 KB)    — model architecture config
        tokens.json       (344 KB)  — token vocabulary
    From iic/SenseVoiceSmall:
        chn_jpn_yue_eng_ko_spectok.bpe.model  (369 KB) — SentencePiece tokenizer
        configuration.json                     (0.4 KB) — metadata

Output: data/models/iic/SenseVoiceSmall/
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> int:
    # Ensure UTF-8 output on Windows (GBK default breaks Unicode chars)
    sys.stdout.reconfigure(encoding="utf-8")

    # ── Resolve paths ──────────────────────────────────────────────────
    project_dir = Path(__file__).resolve().parent.parent
    target_dir = project_dir / "data" / "models" / "iic" / "SenseVoiceSmall"

    if target_dir.exists():
        existing = list(target_dir.iterdir())
        if any(f.name == "model_quant.onnx" for f in existing):
            print(f"模型已存在于 {target_dir}")
            print("如需重新下载，请先删除该目录。")
            return 0
        # Directory exists but no ONNX model — clean and re-download
        print(f"目录已存在但缺少模型文件，清理后重新下载...")
        shutil.rmtree(target_dir)

    # ── Check modelscope ───────────────────────────────────────────────
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("请先安装 modelscope: pip install modelscope")
        return 1

    # ── Stage downloads into a temp cache, then merge ──────────────────
    tmp_base = project_dir / "data" / "models" / ".download_cache"
    tmp_base.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: ONNX model + configs (4 files, ~231 MB)
        print("下载 ONNX 模型 (来自 iic/SenseVoiceSmall-onnx)...")
        onnx_dir = snapshot_download(
            "iic/SenseVoiceSmall-onnx",
            cache_dir=str(tmp_base),
            allow_patterns=[
                "model_quant.onnx",
                "am.mvn",
                "config.yaml",
                "tokens.json",
            ],
        )
        print(f"  -> 完成: {onnx_dir}")

        # Step 2: Tokenizer from base repo (2 files, ~370 KB)
        print("下载分词器 (来自 iic/SenseVoiceSmall)...")
        base_dir = snapshot_download(
            "iic/SenseVoiceSmall",
            cache_dir=str(tmp_base),
            allow_patterns=[
                "chn_jpn_yue_eng_ko_spectok.bpe.model",
                "configuration.json",
            ],
        )
        print(f"  -> 完成: {base_dir}")

        # ── Merge into target ──────────────────────────────────────────
        target_dir.mkdir(parents=True, exist_ok=True)

        total_size = 0
        for src_dir in (onnx_dir, base_dir):
            for f in Path(src_dir).iterdir():
                if f.is_file():
                    dst = target_dir / f.name
                    shutil.copy2(f, dst)
                    total_size += dst.stat().st_size

        print()
        print(f"模型就绪: {target_dir}")
        print(f"总大小: {total_size / 1024 / 1024:.1f} MB")
        print()
        print("文件列表:")
        for f in sorted(target_dir.iterdir()):
            size_kb = f.stat().st_size / 1024
            print(f"  {f.name}  ({size_kb:.1f} KB)" if size_kb < 1024 else f"  {f.name}  ({size_kb / 1024:.1f} MB)")

    finally:
        # Clean up download cache
        shutil.rmtree(tmp_base, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
