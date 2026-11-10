"""真实调用：让写作 agent 自主编辑工作区，再跑两段式记忆维护。

用法:
  python scripts/run_writing_memory_test.py
  python scripts/run_writing_memory_test.py --keep     # 保留临时目录

为什么独立进程：与其它真实链路测试一致，避免在 api_server 进程里跑 agent
冻住事件循环。本脚本只碰临时目录，不写项目的 data/。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import load_config                                   # noqa: E402
from memory_store import MemoryStore                             # noqa: E402
from provider_store import ProviderStore                         # noqa: E402
from runtime import ConversationRuntime, OpenAIClient            # noqa: E402
from services.writing_service import WritingService              # noqa: E402
from session import Session                                      # noqa: E402
from storage.writing_db import WritingDB                         # noqa: E402
from tools import ToolRegistry                                   # noqa: E402
from writegraph import docs as docs_mod                          # noqa: E402
from writegraph.chat_tools import build_writing_registry         # noqa: E402

TRANSCRIPT = """作者：这段别写太满，留白。
作者：人物A说话要短，别用书面语。紧张的时候会反复摸表链。
作者：地点A是铁皮的，雨声很响——这条记一下，以后雨夜场景都能用。
"""

CONTENT = (
    "雨从傍晚下到夜里，邮局的铁皮地点A被砸得咚咚响。\n\n"
    "人物A站在檐下，手插在旧大衣口袋里，拇指一下一下蹭着表链。\n"
    "「船还来吗。」他问。\n"
    "配角没抬头：「等。」\n"
    "人物A就不说了。他把表链摸得更快，眼睛盯着码头尽头那盏灯。\n\n"
    "雨没有小下来的意思。铁皮上的声音一阵密一阵疏，像有人在上面走。\n"
    "配角递给他半支烟，他摇头。\n"
    "「你不抽？」\n"
    "「不抽。」\n"
    "「冷？」\n"
    "「不冷。」\n\n"
    "配角笑了一声，把烟自己点上。两个人就站在地点A底下等，谁也没再说话。\n"
    "远处传来汽笛，很闷，像从水底冒上来的。\n"
    "人物A把表链松开，又攥紧。\n"
    "「来了。」他说。\n"
)

USER_MESSAGE = (
    "我们刚定了三件事：雨夜场景别写太满，留白；人物A说话要短，"
    "紧张时反复摸表链；地点A是铁皮的，雨声很响。"
    "把长期有效的部分记进工作区，然后跑一次记忆维护。"
    "transcript 就用我这句话原文。"
)


def _setup(tmp: Path, *, real: bool = False):
    if not real:
        shutil.copytree(ROOT / "prompts", tmp / "prompts")
        (tmp / "skills").mkdir(parents=True, exist_ok=True)
    cfg = load_config(ROOT)
    bound = ProviderStore(cfg.providers_path).apply_to_config(cfg)
    if not real:
        cfg.project_dir = tmp
    api_client = OpenAIClient(cfg)
    svc = WritingService(cfg, db=WritingDB(cfg.writing_db_path),
                         api_client=api_client)
    return cfg, api_client, svc, bound


def _seed(svc: WritingService, title: str = "雨夜来信") -> str:
    wid = svc.create_work(title)
    ch = svc.create_chapter(wid, title="等船")
    svc.db.save_draft(wid, ch["id"], CONTENT)
    svc.set_contract(ch["id"], purpose="人物A在地点A下等船",
                     must_hit_now=["船到"],
                     arc_ref="计划稿/第一卷/第1章.md")
    return wid


def _snapshot(svc: WritingService, wid: str):
    root = svc.docs_root(wid)
    files = {row["path"]: docs_mod.read_file(
        root, row["path"])["content"] for row in docs_mod.list_files(root)}
    return root, files


async def run_agent(cfg, api_client, svc, wid: str) -> str:
    registry = build_writing_registry(
        ToolRegistry(), svc, {"work_id": wid, "chapter_index": 1})
    session = Session(session_id="writing-memory-live", mode="writing")
    rt = ConversationRuntime(
        session, api_client, registry, cfg, MemoryStore(cfg),
        system_prompt=svc.agent_prompt(wid, 1), scenario="writing")
    parts: list[str] = []
    async for chunk in rt.run_turn(USER_MESSAGE):
        parts.append(chunk)
        print(chunk, end="", flush=True)
    print()
    return "".join(parts)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--real-data", action="store_true",
                        help="写进真实 data/，建一个「记忆自测-」作品")
    parser.add_argument("--title", default="",
                        help="作品名；--real-data 时默认「记忆自测-雨夜来信」")
    args = parser.parse_args()
    real = bool(args.real_data)
    tmp = (ROOT if real else Path(tempfile.mkdtemp(prefix="bc-memory-")))
    cfg, api_client, svc, bound = _setup(tmp, real=real)
    title = args.title or ("记忆自测-雨夜来信" if real else "雨夜来信")
    print(f"控制台连接: {bound or '(默认)'}")
    print(f"端点: {cfg.api_base}  模型: {cfg.model}")
    print(f"数据目录: {tmp}\n")
    if not cfg.api_key:
        print("!! 未配置 api_key")
        return 2
    wid = _seed(svc, title)
    try:
        await asyncio.wait_for(
            run_agent(cfg, api_client, svc, wid), timeout=900)
    except Exception as exc:                     # noqa: BLE001
        print(f"\n[agent 失败] {type(exc).__name__}: {exc}")

    root, files = _snapshot(svc, wid)
    status = svc.memory_status(wid)
    print("\n── 工作区 ──")
    for path in sorted(files):
        print(f"- {path}（{len(files[path])} 字）")
    print("\n── 记忆状态 ──", status)

    settings = [p for p in files if p.startswith("设定/")]
    if status["pending"] == 0 and not settings:
        print("\n[兜底] agent 没有产出记忆，直接跑两段式管线")
        out = await svc.maintain_memory(wid, 1, transcript=TRANSCRIPT)
        print("兜底结果:", out)
        root, files = _snapshot(svc, wid)
        status = svc.memory_status(wid)
        settings = [p for p in files if p.startswith("设定/")]

    bible = docs_mod.read_bible(root)
    ok_bible = ("留白" in bible) or ("短" in bible)
    ok_setting = bool(settings)
    print("\n── 验收 ──")
    print(f"{'ok  ' if ok_bible else 'FAIL'} 作品记忆含约定")
    print(f"{'ok  ' if ok_setting else 'FAIL'} 设定已生成：{settings}")
    print(f"记忆状态：{status}")
    if real:
        print(f"真实作品：{wid}")
        print(f"工作区：{svc.docs_root(wid)}")
        print("不需要时在写作台删除这个「记忆自测-」作品即可。")
    elif not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"保留：{tmp}")
    return 0 if (ok_bible and ok_setting) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


