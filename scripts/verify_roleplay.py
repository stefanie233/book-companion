"""书伴 · 扮演模式验收脚本

读 book-companion 的 sessions.db，统计每个 roleplay 会话的可量化指标。
改前跑一次留基线，改后再跑一次对比。

用法：
    python verify_roleplay.py                 # 所有 roleplay 会话
    python verify_roleplay.py --last 1        # 只看最近 1 个
    python verify_roleplay.py --detail        # 打印最近一次的正文
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# 默认读仓库自己的 sessions.db；也可用 --db 指定副本。
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "sessions.db"

# ---------------------------------------------------------------- 指标定义

# 翻案腔：不是X是Y / 并非X而是Y / 你以为…其实 / 看似…实则 / 越…越 / 与其说…不如说
FANAN_PATTERNS = [
    re.compile(r"不是[^。！？\n]{1,40}?[，,]\s*(?:而)?是"),
    re.compile(r"并非[^。！？\n]{1,40}?[，,]\s*(?:而)?是"),
    re.compile(r"你以为[^。！？\n]{1,40}?[，,]?\s*其实"),
    re.compile(r"看似[^。！？\n]{1,40}?[，,]?\s*(?:实则|其实)"),
    re.compile(r"与其说[^。！？\n]{1,40}?不如说"),
]

BOLD = re.compile(r"\*\*([^*\n]{2,40})\*\*")
# 星号包裹的动作标签：*清嗓子* / *（苦笑）* / *（沉默了很久）*。
# 前后断言排除 **加粗**（否则每个加粗都会被再算一次），组内用 lazy 让
# 全角括号被单独吃掉（旧写法会把 "苦笑）" 整个捕获）。
ACTION_TAG = re.compile(r"(?<!\*)\*(?!\*)[（(]?([^*\n]{1,24}?)[)）]?(?<!\*)\*(?!\*)")
# 方括号语音标签：[停顿] [轻声] [叹气] ...
BRACKET_TAG = re.compile(r"\[([^\[\]\n]{1,8})\]")

THINK = re.compile(r"<think>.*?</think>", re.S)
WS = re.compile(r"\s+")


def merge_count(patterns: list[re.Pattern], text: str) -> int:
    """多个模式命中后按位置合并去重，避免同一句被算两次。"""
    spans: list[tuple[int, int]] = []
    for p in patterns:
        for m in p.finditer(text):
            spans.append((m.start(), m.end()))
    if not spans:
        return 0
    spans.sort()
    merged = 1
    cur_end = spans[0][1]
    for s, e in spans[1:]:
        if s >= cur_end:
            merged += 1
            cur_end = e
        else:
            cur_end = max(cur_end, e)
    return merged


def short_paragraphs(text: str, limit: int = 20) -> list[str]:
    """独立成段且不超过 limit 字的句子——金句的近似代理指标。"""
    out = []
    for para in text.split("\n"):
        p = para.strip()
        if not p or p.startswith(("#", "*", "-", ">")):
            continue
        core = re.sub(r"^\[[^\]]{1,8}\]", "", p).strip()
        if 0 < len(core) <= limit and core[-1] in "。！？":
            out.append(core)
    return out


def load_session(con: sqlite3.Connection, sid: str) -> tuple[str, str]:
    user_parts: list[str] = []
    asst_parts: list[str] = []
    for mid, role in con.execute(
        "select id, role from messages where session_id=? order by time_created", (sid,)
    ):
        for (dtype, data) in con.execute(
            "select type, data from parts where message_id=?", (mid,)
        ):
            if dtype != "text":
                continue
            try:
                obj = json.loads(data)
                txt = obj.get("text", "") if isinstance(obj, dict) else str(obj)
            except Exception:
                txt = str(data)
            txt = THINK.sub("", txt or "")
            if role == "user":
                user_parts.append(txt)
            elif role == "assistant":
                asst_parts.append(txt)
    return "".join(user_parts), "".join(asst_parts)


def assistant_turns(con: sqlite3.Connection, sid: str) -> list[str]:
    """One entry per assistant message (think blocks stripped).

    会话合计看不出「第 2 轮又变长了」——长度预算是按轮算的，所以要按轮看。
    """
    out: list[str] = []
    for mid, role in con.execute(
        "select id, role from messages where session_id=? order by time_created", (sid,)
    ):
        if role != "assistant":
            continue
        buf: list[str] = []
        for (dtype, data) in con.execute(
            "select type, data from parts where message_id=?", (mid,)
        ):
            if dtype != "text":
                continue
            try:
                obj = json.loads(data)
                txt = obj.get("text", "") if isinstance(obj, dict) else str(obj)
            except Exception:
                txt = str(data)
            buf.append(THINK.sub("", txt or ""))
        out.append("".join(buf))
    return out


def report(con: sqlite3.Connection, rows: list[tuple], detail: bool) -> None:
    print(f"{'时间':<20}{'角色':<10}{'入':>6}{'出':>7}{'倍数':>7}{'轮':>4}"
          f"{'超300':>8}{'翻案':>6}{'加粗':>6}{'动作':>6}{'标签':>6}{'短段':>6}")
    print("-" * 98)
    for sid, title, cname, tu in rows:
        u, a = load_session(con, sid)
        un, an = len(WS.sub("", u)), len(WS.sub("", a))
        ratio = an / max(un, 1)
        fan = merge_count(FANAN_PATTERNS, a)
        bold = len(BOLD.findall(a))
        act = len(ACTION_TAG.findall(a))
        tag = len(BRACKET_TAG.findall(a))
        sp = len(short_paragraphs(a))
        turns = assistant_turns(con, sid)
        over = sum(1 for t in turns if len(WS.sub("", t)) > 300)
        import datetime
        ts = datetime.datetime.fromtimestamp((tu or 0) / 1000).strftime("%m-%d %H:%M")
        print(f"{ts:<20}{(cname or '(未设)')[:9]:<10}{un:>6}{an:>7}{ratio:>6.1f}x"
              f"{len(turns):>4}{over:>4}/{len(turns):<3}"
              f"{fan:>6}{bold:>6}{act:>6}{tag:>6}{sp:>6}")
        if detail:
            print("\n--- 正文 ---")
            print(a.strip()[:1500])
            print()

    print("\n判定标准（v2 目标）：单轮 ≤300 字 | 动作 0 | 标签 ≤2/轮；"
          "倍数 ≤3.0（参考）| 翻案 ≤1/轮 | 短段 ≤1/轮")
    print("注：翻案/加粗/动作/标签/短段 是会话合计，多轮会话请除以「轮」再看。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--last", type=int, default=0, help="只看最近 N 个")
    ap.add_argument("--detail", action="store_true", help="打印正文")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    db = Path(args.db)
    if not db.exists():
        print(f"[错误] 找不到数据库：{db}")
        return 1

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sql = ("select id, title, character_name, time_updated from sessions "
           "where mode='roleplay' order by time_updated desc")
    rows = con.execute(sql).fetchall()
    if not rows:
        print("没有 roleplay 会话。")
        return 0
    if args.last:
        rows = rows[: args.last]

    print(f"数据库：{db}")
    print(f"roleplay 会话：{len(rows)} 个（按时间倒序）\n")
    report(con, rows, args.detail)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
