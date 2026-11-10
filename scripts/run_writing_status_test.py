"""真实调用：让写作 agent 读工作区的 git 状态，并对单份文件看逐行差异。

用法:
  python scripts/run_writing_status_test.py
  python scripts/run_writing_status_test.py --real-data   # 写进真实 data/

独立进程运行（与其它真实链路测试一致，避免冻 api_server 的事件循环）。
默认只碰临时目录。验收点：
  1. agent 调 writing_docs(action="list")，拿到每份文件的 M / U / D；
  2. agent 调 writing_git(action="diff", file_path=...) 看单文件逐行差异；
  3. 回复里说的是真实内容（左眉的疤、配角被删），不是背术语。
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
from writegraph.chat_tools import build_writing_registry         # noqa: E402

CONTENT = (
    "雨从傍晚下到夜里，邮局的铁皮地点A被砸得咚咚响。\n\n"
    "人物A站在檐下，手插在旧大衣口袋里。\n"
    "「船还来吗。」他问。\n"
    "配角没抬头：「等。」\n"
)

USER_MESSAGE = (
    "我刚补了一条设定：人物A左眉有一道旧疤。配角那份文件我删了。"
    "你先看一眼工作区每份文件的 git 状态（改了、新的、删了分别是哪几份），"
    "再把人物A这份文件到底改了什么逐行告诉我——别贴全文。"
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


def _seed(svc: WritingService, title: str) -> str:
    wid = svc.create_work(title)
    ch = svc.create_chapter(wid, title="等船")
    svc.db.save_draft(wid, ch["id"], CONTENT)
    svc.set_contract(ch["id"], purpose="人物A在地点A下等船",
                     must_hit_now=["船到"])
    svc.docs_write(wid, "设定/人物/人物A.md", "# 人物A\n\n码头邮局的人。\n")
    svc.docs_write(wid, "设定/人物/配角.md", "# 配角\n\n船工，话少。\n")
    svc.docs_write(wid, "设定/地点/地点A.md", "# 地点A\n\n铁皮棚，雨声很响。\n")
    svc.vcs_commit(wid, "基线")
    # 作者/助手改过的现场：一份改、一份删（新文件留给工具自己建）。
    svc.docs_write(wid, "设定/人物/人物A.md",
                   "# 人物A\n\n码头邮局的人。\n\n左眉有一道旧疤。\n")
    svc.docs_delete(wid, "设定/人物/配角.md")
    return wid


async def run_agent(cfg, api_client, svc, wid: str,
                    calls: list[tuple[str, str]]) -> str:
    registry = build_writing_registry(
        ToolRegistry(), svc, {"work_id": wid, "chapter_index": 1})
    # 记下模型真实调了哪些工具——验收看的是 agent 侧，不只人工侧。
    raw_execute = registry.execute
    raw_execute_async = registry.execute_async

    def record(name: str, args: str) -> str:
        calls.append((name, args))
        return raw_execute(name, args)

    async def record_async(name: str, args: str) -> str:
        calls.append((name, args))
        return await raw_execute_async(name, args)

    registry.execute = record
    registry.execute_async = record_async
    session = Session(session_id="writing-status-live", mode="writing")
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
                        help="写进真实 data/，建一个「状态自测-」作品")
    parser.add_argument("--title", default="", help="作品名")
    args = parser.parse_args()
    real = bool(args.real_data)
    tmp = (ROOT if real else Path(tempfile.mkdtemp(prefix="bc-status-")))
    cfg, api_client, svc, bound = _setup(tmp, real=real)
    title = args.title or ("状态自测-雨夜来信" if real else "雨夜来信")
    print(f"控制台连接: {bound or '(默认)'}")
    print(f"端点: {cfg.api_base}  模型: {cfg.model}")
    print(f"数据目录: {tmp}\n")
    if not cfg.api_key:
        print("!! 未配置 api_key")
        return 2
    wid = _seed(svc, title)
    status = svc.vcs_status(wid)
    print("── 现场（服务端算出）──")
    for op in ("changed", "added", "removed"):
        for row in status["file_changes"][op]:
            print(f"- {row['status']} {row['status_label']}  {row['path']}")
    calls: list[tuple[str, str]] = []
    try:
        answer = await asyncio.wait_for(
            run_agent(cfg, api_client, svc, wid, calls), timeout=900)
    except Exception as exc:                     # noqa: BLE001
        print(f"\n[agent 失败] {type(exc).__name__}: {exc}")
        answer = ""

    # 状态来源有两个：文件区同款的 writing_docs list，或 writing_git status。
    # system prompt 的「版本现场」也带未提交文件，模型不一定再查一次。
    listed = [(n, a) for n, a in calls
              if (n == "writing_docs" and '"list"' in a)
              or (n == "writing_git" and '"status"' in a)]
    diffed = [(n, a) for n, a in calls if n == "writing_git"
              and '"diff"' in a and "file_path" in a]
    ok_list = bool(listed)
    ok_diff = bool(diffed)
    ok_mark = "左眉" in answer
    ok_del = "配角" in answer and ("删" in answer or "D" in answer)
    print("\n── 验收 ──")
    print(f"{'ok  ' if ok_list else 'FAIL'} agent 查了工作区状态")
    print(f"{'ok  ' if ok_diff else 'FAIL'} agent 看了单文件逐行差异")
    print(f"{'ok  ' if ok_mark else 'FAIL'} 回复里有真实改动（左眉）")
    print(f"{'ok  ' if ok_del else 'FAIL'} 回复里报了删除（配角）")
    print(f"工具调用：{[n for n, _ in calls]}")
    if real:
        print(f"真实作品：{wid}")
        print("不需要时在写作台删除这个「状态自测-」作品即可。")
    elif not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"保留：{tmp}")
    return 0 if (ok_list and ok_diff and ok_mark and ok_del) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
