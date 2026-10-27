"""输出预算与截断识别 —— 钉住一个「修过一次又复发」的 bug。

症状：提取 10 次调用挂 7 次，日志里是 ``parse_failed`` + ``resp=0``，
读起来像「模型没说话」。真因是 ``max_tokens: 16000`` 对推理模型太小 ——
实测单批推理就烧掉 15406 token，正文一个字符轮不到，端点回
HTTP 200 + 空 content + ``finish_reason=length``。同一条 prompt 抬到 64000
就正常返回。

所以这里断言两件事：**预算够**，以及**撞上限要说成撞上限**（不是「说胡话」）。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.llm_call import (                                  # noqa: E402
    DEFAULT_OUTPUT_BUDGET, is_truncated, output_budget, reasoning_text,
    reasoning_tokens)
from bookgraph.explorer import run_explorer                       # noqa: E402
from config import Config                                         # noqa: E402

ROOT = Path(__file__).parent.parent
FAILED = []


def check(cond: bool, label: str) -> None:
    print(("  [OK]   " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILED.append(label)


# ── 纯函数 ────────────────────────────────────────────────────────────────

def test_budget() -> None:
    print("预算取用")
    cfg = Config()
    check(output_budget(cfg) == DEFAULT_OUTPUT_BUDGET,
          f"默认给 {DEFAULT_OUTPUT_BUDGET}")
    cfg.graph_max_tokens = 1234
    check(output_budget(cfg) == 1234, "cfg 能覆盖")
    cfg.graph_max_tokens = 0
    check(output_budget(cfg) == DEFAULT_OUTPUT_BUDGET,
          "0 = 未配置，退回默认（不把 0 当预算发出去）")


def test_truncation_detect() -> None:
    print("截断识别")
    length = {"choices": [{"finish_reason": "length"}]}
    stop = {"choices": [{"finish_reason": "stop"}]}
    check(is_truncated(length), "finish_reason=length → 截断")
    check(not is_truncated(stop), "finish_reason=stop → 不是截断")
    check(not is_truncated({}), "字段缺失 → 不猜，判为不是截断")
    check(reasoning_tokens(
        {"usage": {"completion_tokens_details": {"reasoning_tokens": 15406}}}
    ) == 15406, "推理 token 读得出来")
    check(reasoning_tokens({}) == 0, "非推理模型 → 0")
    # 只记 token 数等于只记「想了多久」。正文是**飞行记录仪抽帧**的依据。
    think = {"choices": [{"message": {"content": "答案",
                                      "reasoning_content": "先看这一段…"}}]}
    check(reasoning_text(think) == "先看这一段…", "思维链正文读得出来")
    check(reasoning_text({"choices": [{"message": {"content": "答案"}}]}) == "",
          "非推理模型 → 空串（不假装有）")
    check(reasoning_text({}) == "", "字段缺失 → 空串，不抛")


# ── 真 HTTP：撞上限要抬预算重试，且要报 truncated ─────────────────────────

EXPLORER_OK = {
    "new_nodes": [{"canonical_name": "史铁生", "kind": "character",
                   "why": "叙述者", "evidence": "我常觉得这中间有着宿命的味道"}],
    "new_edges": [{"source": "史铁生", "target": "地坛", "kind": "visits",
                   "chunk_id": "c1", "evidence": "我常觉得这中间有着宿命的味道"}],
    "new_observations": [], "new_aliases": [], "merge_candidates": [],
    "quotes": [], "summary": "ok",
}

CHUNKS = [{"chunk_id": "c1", "content": "我常觉得这中间有着宿命的味道。",
           "chapter_index": 0, "chunk_index": 0, "chapter_title": "我与地坛"}]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


async def _call(base: str) -> tuple[dict | None, str | None, list[dict]]:
    cfg = Config()
    cfg.api_base, cfg.api_key, cfg.model = base, "fake", "fake-model"
    logs: list[dict] = []
    import aiohttp
    from storage.graph_db import GraphDB
    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # 控制台「模型绑定」会按调用点覆盖 api_base/model（provider_store）。
        # 这个测试要打 fake provider，所以把 project_dir 指到空目录，
        # 让 providers_path 不存在 —— 否则请求会被路由到真实端点。
        cfg.project_dir = Path(tmp)
        gdb = GraphDB(Path(tmp) / "g.db")
        async with aiohttp.ClientSession() as sess:
            out = await run_explorer(
                sess, cfg, gdb, "b1", "我与地坛", "史铁生", CHUNKS, 1, 0,
                "系统提示", log_cb=logs.append)
        gdb.close()
    return out[0], out[1], logs


def _run_http(base: str, fault_times: int) -> None:
    _post(f"{base}/_mock/reset", {})
    _post(f"{base}/_mock/scenario", {"rules": [
        {"contains": "宿命的味道",
         "response": {"text": json.dumps(EXPLORER_OK, ensure_ascii=False)}}]})
    if fault_times:
        _post(f"{base}/_mock/fault",
              {"mode": "max_tokens", "times": fault_times})

    parsed, err, logs = asyncio.run(_call(base))
    reqs = [r for r in _get(f"{base}/_mock/requests")["requests"]
            if r["path"].endswith("/chat/completions")]
    budgets = [r.get("max_tokens") for r in reversed(reqs)]
    statuses = [l.get("status") for l in logs]

    print(f"  故障 {fault_times} 次 → 调用 {len(budgets)} 次，预算 {budgets}，"
          f"状态 {statuses}")
    return parsed, err, statuses, budgets


def main() -> int:
    test_budget()
    test_truncation_detect()

    print("撞上限 → 抬预算重试")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "fake_provider.py"),
         "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(f"{base}/_mock/requests", timeout=3).read()
                break
            except Exception:
                if time.monotonic() > deadline:
                    print("fake provider 没起来")
                    return 1
                time.sleep(0.2)

        # 第一次撞上限、第二次成功
        parsed, err, statuses, budgets = _run_http(base, 1)
        check(parsed is not None and err is None, "抬预算后这一批救回来了")
        check(statuses[:1] == ["truncated"],
              "第一次记 truncated（不是 parse_failed —— 那是误诊）")
        check(len(budgets) == 2 and budgets[1] > budgets[0],
              f"重试确实抬了预算 {budgets}")
        check(budgets[0] == DEFAULT_OUTPUT_BUDGET,
              "首次就用默认预算（不是写死的 16000）")

        # 一直撞上限 → 报 truncated，且不冒充成功
        parsed, err, statuses, budgets = _run_http(base, 99)
        check(parsed is None and err == "truncated",
              f"一直撞上限 → error_type=truncated（实际 {err}）")
        check(all(s == "truncated" for s in statuses),
              "三次都记 truncated，没有一次被误报成 parse_failed")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}")
        for f in FAILED:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
