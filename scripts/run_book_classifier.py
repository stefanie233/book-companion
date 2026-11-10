"""book_classifier 子代理的独立驱动壳。

用法:
  python scripts/run_book_classifier.py 窄门
  python scripts/run_book_classifier.py "C:\\path\\to\\book.epub"

为什么必须是独立进程：见记忆 service-graph-extraction-deadlock —— 在
api_server 进程内跑 agent/orchestrator 会冻住事件循环（getaddrinfo-executor），
真实链路验证只能独立进程驱动。

跑完后本次的完整 ReAct 轨迹（每一轮发给 LLM 的 system_prompt + messages +
工具返回）落在 data/context_log.db，在 /flight-recorder/viewer.html 可看。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 控制台默认 GBK，而 agent 的输出里带 emoji（🔧 工具调用标记）—— 不换编码
# 就会在 print 上抛 UnicodeEncodeError，把落库那步一起带走。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import load_config
from file_readers.style_resolver import split_selectors
from flight_recorder import create_recorder
from runtime import OpenAIClient
from services.library_service import _bookbank_dir
from storage.db import BookDB
from sub_agent import SubAgentRunner
from tools import create_builtin_registry


def resolve_epub(db: BookDB, arg: str) -> tuple[str, str, str]:
    """*arg* 可以是 .epub 路径，也可以是书名子串（从 books.db 查）。

    返回 (book_id, title, epub_path)；直接传路径时 book_id 为空串。
    """
    p = Path(arg)
    if p.suffix.lower() == ".epub" and p.is_file():
        return "", p.stem, str(p)
    cur = db.get_conn().cursor()
    cur.execute("SELECT id, title FROM books WHERE title LIKE ? LIMIT 5",
                (f"%{arg}%",))
    rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"!! 库里没有书名含 {arg!r} 的书")
    book_id, title = rows[0][0], rows[0][1]
    epub = _bookbank_dir(db) / f"{book_id}.epub"
    if not epub.exists():
        raise SystemExit(f"!! 书库缺 epub: {epub}")
    return book_id, title, str(epub)


def _load_json(text: str) -> dict | None:
    """从 agent 输出里挖 JSON —— 实现在 services.import_analysis_service，
    落库脚本与编排共用一份，避免两处漂移。"""
    from services.import_analysis_service import load_agent_json
    return load_agent_json(text)



def _flag_value(argv: list[str], flag: str) -> str:
    """取 ``--dump <path>`` 的值；没给返回空串。"""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return ""


def _pretty(text: str) -> str:
    data = _load_json(text)
    return json.dumps(data, ensure_ascii=False, indent=2) if data else text


def _html_norm(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html or "")
    m = re.search(r"(?is)<body[^>]*>(.*)</body>", html)
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", m.group(1) if m else html))


def _spine_texts(epub_path: str) -> list[str]:
    """每个 spine item 的规范化正文，下标 = reader 的压缩坐标（agent 看到的）。"""
    from file_readers.epub_reader import open_book
    reader = open_book(Path(epub_path))
    try:
        reader._ensure_loaded()
        out = []
        for i in range(len(reader._spine_ids)):
            try:
                out.append(_html_norm(reader._read_spine_html(i)))
            except Exception:
                out.append("")
        return out
    finally:
        reader.close()


def commit_overrides(db: BookDB, book_id: str, data: dict,
                     epub_path: str | None = None) -> tuple[int, int]:
    """把 agent 的判断写进覆盖层（``source='agent'``）。

    返回 (写入条数, 跳过条数)。**agent 说的是 spine 序号**（它看的是文件），
    而覆盖层的键是 ``chapter_index``。翻译按**正文**做：agent 的序号落在
    哪个 spine item，就把该 item 的正文里能找到「章开头」的章都算上 ——
    DB 里存的 ``spine_index`` 是 OPF 坐标（决策 146），与 reader 的压缩坐标
    不是一套，不能再拿它直接查表。没有 epub 时才退回旧的按列查表。

    翻译本身就是一道闸：agent 凭空补出来的序号（被跳过的空页、不存在的位置）
    找不到任何章，自然落不下去 —— 不必再单独校验。

    写进覆盖层而非原表：重导入只刷新原表，便利贴不丢；``source`` 又能把
    机器判断和人手判断分开，撕的时候互不牵连（见 storage/db.py）。
    """
    chapters = data.get("chapters") or {}
    rows = db.get_chapters(book_id)
    by_spine: dict[int, list[int]] = {}
    for ch in rows:
        if ch.spine_index >= 0:
            by_spine.setdefault(ch.spine_index, []).append(ch.chapter_index)
    spine_texts = _spine_texts(epub_path) if epub_path else []
    wrote = skipped = 0
    for key, new in chapters.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if spine_texts and 0 <= idx < len(spine_texts):
            text = spine_texts[idx]
            targets = [
                ch.chapter_index for ch in rows
                if text and len(head := re.sub(r"\s+", "", ch.content or "")[:24]) >= 10
                and head in text
            ]
        else:
            targets = by_spine.get(idx, [])
        if not targets:
            skipped += 1
            continue
        for ci in targets:
            db.set_chapter_override(book_id, ci, str(new), source="agent")
            wrote += 1
    return wrote, skipped


def commit_text_forms(db: BookDB, book_id: str, data: dict) -> int:
    """把 agent 判出的文本形态写进 ``book_text_forms``，返回条数。

    ``evidence`` 一并落库 —— 形态名称是 agent 自定的，没有依据就无从复核，
    而"可追溯"正是这套导入分析要立住的东西。

    **逗号要拆开落**：agent 会把排版相同的多个选择器合并成一条（实测人类简史
    ``"h5.sigilnotintoc,h5.sigilnotintoc1"``）。``set_text_form`` 拿 selector
    当主键，整串存下去就是一个永远命中不了任何段落、也没法逐段标注的键。
    """
    forms = data.get("text_forms") or []
    wrote = 0
    for form in forms:
        if not isinstance(form, dict):
            continue
        name = str(form.get("name") or "").strip()
        evidence = str(form.get("evidence") or "").strip()
        for selector in split_selectors(str(form.get("selector") or "")):
            db.set_text_form(book_id, selector, name, evidence)
            wrote += 1
    return wrote


def main() -> int:
    cfg = load_config()
    # 控制台连接档案覆盖 .env（与 main.py:365 同一条路径）。.env 里配的
    # 火山方舟 coding plan 订阅过期时，控制台槽位才是真正在用的那条连接。
    from provider_store import ProviderStore
    bound = ProviderStore(cfg.providers_path).apply_to_config(cfg)
    if bound:
        print(f"控制台连接: {bound}")
    if not cfg.api_key:
        print("!! 未检测到 api_key(.env BOOK_COMPANION_API_KEY / OPENAI_API_KEY)")
        return 2

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_commit = "--commit" in sys.argv
    arg = args[0] if args else "窄门"
    db = BookDB(cfg.books_db_path)
    db.initialize()          # 跑待应用迁移（如 book_text_forms 的 v21）
    book_id, title, epub = resolve_epub(db, arg)
    print(f"端点: {cfg.api_base}  模型: {cfg.model}")
    print(f"书  : {title}  id={book_id or '(未入库)'}")
    print(f"epub: {epub}\n")

    api_client = OpenAIClient(cfg)
    registry = create_builtin_registry(
        api_client=api_client, config=cfg, bookbank_db=db)
    recorder = create_recorder(cfg.project_dir)
    if recorder is None:
        print("!! 飞行记录仪构造失败，本次不落轨迹")
    runner = SubAgentRunner(api_client, registry, cfg,
                            context_recorder=recorder,
                            scenario="import.analysis")

    async def _run():
        return await runner.run(
            agent_type="book_classifier",
            description=f"分析《{title}》的章节体例与正文文本形态",
            prompt=f"EPUB 路径：{epub}",
        )

    out = asyncio.run(_run())

    print(f"── 状态: {out.status} ──")
    if out.error:
        print(f"error: {out.error}")
    if out.token_usage:
        print(f"token: {out.token_usage}")

    print("\n── 输出 ──")
    print(_pretty(out.result_text))

    data = _load_json(out.result_text)
    if data is None:
        print("\n!! 没能从输出里解出 JSON —— 上面打的是原始文本。"
              "加 --dump <文件> 可把原始输出落盘细看。")
    # 靶场调试用：原始输出落盘。解析失败时这是唯一能看到全文的途径
    # （飞行记录仪里也有，但要开浏览器）。
    dump = _flag_value(sys.argv, "--dump")
    if dump:
        Path(dump).write_text(out.result_text, encoding="utf-8")
        print(f"(原始输出已落盘: {dump})")

    if do_commit:
        print("\n── 落库 ──")
        if data is None:
            print("!! 输出里没有 JSON，不落库")
            return 1
        if not book_id:
            print("!! 这本书不在库里，无处可落（先导入再跑）")
            return 1
        # 落库靠 book_chapters.spine_index 翻译。旧库（v22 之前导入、没重导过）
        # 这一列全是 -1，翻译表为空 —— 拒绝，而不是退回按序号硬写。
        if not any(c.spine_index >= 0 for c in db.get_chapters(book_id)):
            print("!! 这本书的 chapters 没有 spine_index（v22 之前导入且未重导），"
                  "无法把 agent 的文件序号翻译成章序号。\n"
                  "   先跑：python scripts/reimport_books.py <书名>")
            return 2
        old_ch, old_tf = db.clear_agent_layer(book_id)   # 全量替换，不留残影
        wrote, skipped = commit_overrides(db, book_id, data, epub)
        print(f"覆盖层：清掉旧 {old_ch} 条，写入 {wrote} 条"
              f"（source='agent'），跳过 {skipped} 条越界 index")
        print(f"文本形态：清掉旧 {old_tf} 条，"
              f"写入 {commit_text_forms(db, book_id, data)} 条")

    if recorder is not None:
        print("\n── 飞行记录仪 ──")
        try:
            s = recorder.list_sessions()[0]
            print(f"session={s['session_id']}  轮次={s['round_count']}  "
                  f"字符={s['total_chars']}")
            print("看 /flight-recorder/viewer.html")
        except Exception as exc:
            print(f"(读记录仪失败: {exc})")
    return 0 if out.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
