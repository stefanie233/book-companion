"""``authorgraph`` + ``storage.authors_db`` 的单元测试（纯函数 + 临时库，零 LLM）。

盯的五件事：

1. **剧透门 fail-closed** —— 推不出年份时返回 ``None``（无门），
   **不是**「无门 = 全放行」。书里那套门在这条上翻过车。
2. **逐字校验与重定位** —— 引文找不到出处要丢并报；找得到但在别的块里，
   改指到那一块（锚点是导出量，不该让模型猜）。
3. **宽容三层** —— 形状（dict/list）、取值（词表外**照报**不装作没判定）。
4. **``model`` 不入注入路径** —— 可展示不可注入，是这层唯一的硬约束。
5. **落库幂等** —— 同一本书重跑不产生重复条目。

跑法：``python scripts/test_author_graph.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from authorgraph.card import format_author_card, injectable   # noqa: E402
from authorgraph.gate import gate_year                        # noqa: E402
from authorgraph.harvest import normalize_result              # noqa: E402
from storage.authors_db import AuthorsDB                      # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def has(label: str, needle: str, hay: str) -> None:
    global FAILED
    if needle not in hay:
        print(f"FAIL {label}\n     没找到 {needle!r}\n     在 {hay[:300]!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


# ── 1. 剧透门 ─────────────────────────────────────────────────────────────

def test_gate() -> None:
    pairs = [(0, 1944), (3, 1952), (7, 1972), (9, None)]
    check("门：读到 ch0 → 1944", gate_year(pairs, 0), 1944)
    check("门：读到 ch3 → 1952", gate_year(pairs, 3), 1952)
    check("门：读到 ch5 → 1952（ch7 的 1972 不许露）", gate_year(pairs, 5), 1952)
    check("门：读到 ch99 → 1972", gate_year(pairs, 99), 1972)
    # 读者还没读到任何带年的块 → 推不出时间点 → 无门
    check("门：读到 ch-1 → None（fail-closed）", gate_year(pairs, -1), None)
    check("门：全无年 → None", gate_year([(0, None), (1, None)], 5), None)
    check("门：空语料 → None", gate_year([], 5), None)
    check("门：ch 非整数不参与", gate_year([("x", 1944)], 5), None)


# ── 2/3. 归一：逐字校验、重定位、宽容三层 ─────────────────────────────────

TEXT = {
    "c0": "我写这本书的时候，常常想起上海的秋天。",
    "c1": "一九四四年九月，我在南京。",
    "c2": "这本书里的故事，多半是听来的。",
}


def test_normalize() -> None:
    # 引文逐字为真且锚对 → 干净通过
    r, d = normalize_result({
        "author_name": "张爱玲", "position": "avatar",
        "corpus": [{"chunk_id": "c0", "excerpt": "常常想起上海的秋天",
                    "speaker": "author"}],
    }, TEXT)
    check("归一：正确引文无缺陷", d, [])
    check("归一：语料留下", len(r["corpus"]), 1)

    # 锚错块但引文为真 → 重定位（导出量现算）
    r, d = normalize_result({
        "corpus": [{"chunk_id": "c2", "excerpt": "常常想起上海的秋天"}],
    }, TEXT)
    check("归一：重定位到正确的块", r["corpus"][0]["chunk_id"], "c0")
    check("归一：重定位被报出来", any("reanchor" in x for x in d), True)

    # 引文在任何块里都找不到 → 丢弃 + 报 fabricated
    r, d = normalize_result({
        "corpus": [{"chunk_id": "c0", "excerpt": "我从未说过这句话"}],
    }, TEXT)
    check("归一：编造的引文被丢弃", r["corpus"], [])
    check("归一：编造被报出来",
          any("fabricated_excerpt" in x for x in d), True)

    # 空白差异不算编造（归一后比对）
    r, d = normalize_result({
        "corpus": [{"chunk_id": "c0", "excerpt": "我写这本书的时候，\n常常想起上海的秋天。"}],
    }, TEXT)
    check("归一：换行差异仍算逐字", len(r["corpus"]), 1)

    # 取值词表外 → 收成安全默认值**并报**（照报不装作没判定）
    r, d = normalize_result({"position": "memoir", "role": "ghostwriter"}, TEXT)
    check("归一：position 词表外 → unknown", r["position"], "unknown")
    check("归一：position 词表外被报", any("position" in x for x in d), True)
    check("归一：role 词表外 → author", r["role"], "author")

    # speaker 词表外 → other（不能默认成 author，否则会被当作者的话注入）
    r, _ = normalize_result({
        "corpus": [{"chunk_id": "c0", "excerpt": "常常想起上海的秋天",
                    "speaker": "narrator"}]}, TEXT)
    check("归一：speaker 词表外 → other", r["corpus"][0]["speaker"], "other")

    # 形状宽容：corpus 给 dict 而不是 list
    r, _ = normalize_result({
        "corpus": {"chunk_id": "c0", "excerpt": "常常想起上海的秋天"}}, TEXT)
    check("归一：corpus 为 dict 也收", len(r["corpus"]), 1)

    # 年份收宽：中文年 / 带后缀 / 越界
    r, _ = normalize_result({"birth_year": "1920年"}, TEXT)
    check("归一：'1920年' → 1920", r["birth_year"], 1920)
    r, _ = normalize_result({"birth_year": "约1944"}, TEXT)
    check("归一：'约1944' → 1944", r["birth_year"], 1944)
    r, _ = normalize_result({"birth_year": "很久以前"}, TEXT)
    check("归一：收不出 → None", r["birth_year"], None)
    r, _ = normalize_result({"birth_year": 99999}, TEXT)
    check("归一：越界 → None", r["birth_year"], None)
    r, _ = normalize_result({"birth_year": True}, TEXT)
    check("归一：bool 不算年份", r["birth_year"], None)

    # 编年条目：无原文依据 → 保留陈述但清空证据并报
    r, d = normalize_result({
        "records": [{"kind": "life_event", "statement": "生于上海",
                     "evidence_excerpt": "我生于上海", "chunk_id": "c0"}]}, TEXT)
    check("归一：无依据的编年仍保留陈述", r["records"][0]["statement"], "生于上海")
    check("归一：无依据的编年清空证据", r["records"][0]["evidence_excerpt"], "")
    check("归一：无依据被报", any("无原文依据" in x for x in d), True)

    # 空 statements 丢弃
    r, _ = normalize_result({"records": [{"statement": "  "}]}, TEXT)
    check("归一：空陈述丢弃", r["records"], [])

    # 合著者：署名里有几个人就记几个（实测《道德经》末署「张景　张松辉」，
    # 第一版只采到张景，另一位作者连同他那一半口吻一起丢了）
    r, _ = normalize_result({"author_name": "张景",
                             "co_authors": ["张松辉", "张景"]}, TEXT)
    check("归一：合著者收进来", r["co_authors"], ["张松辉"])
    check("归一：自己不算自己的合著者", "张景" in r["co_authors"], False)
    # 别名 ≠ 合著者：同一个人的不同写法不该变成两个人
    r, _ = normalize_result({"author_name": "张景", "co_authors": ["张松辉"],
                             "author_aliases": ["张松辉"]}, TEXT)
    check("归一：别名不重复算合著", r["co_authors"], [])

    # 自相矛盾：作者「不在书里」却又在书里说「我」。
    # 实测全库首跑 6 本判 absent，其中 5 本同时有 speaker=author 语料 ——
    # 根因是提示词把「论著」写成了 absent 的例子（已改）。这条守着它。
    _, d = normalize_result({
        "position": "absent",
        "corpus": [{"chunk_id": "c0", "excerpt": "常常想起上海的秋天",
                    "speaker": "author"}]}, TEXT)
    check("归一：absent + author 语料 → 报矛盾",
          any("自相矛盾" in x for x in d), True)
    # 但 absent + 非 author 语料是合法的（他人写的序）
    _, d = normalize_result({
        "position": "absent",
        "corpus": [{"chunk_id": "c0", "excerpt": "常常想起上海的秋天",
                    "speaker": "editor"}]}, TEXT)
    check("归一：absent + editor 语料不算矛盾",
          any("自相矛盾" in x for x in d), False)

    # **坐标由系统补，不由模型给。** 第一版漏了这一步，于是
    # author_corpus.chapter_index 全是 NULL —— 剧透门看起来装好了，
    # 实际永远不会亮（它按章号算）。这条守着那个 bug 不回来。
    r, _ = normalize_result(
        {"corpus": [{"chunk_id": "c0", "excerpt": "常常想起上海的秋天"}]},
        TEXT, {"c0": (7, 3)})
    check("归一：坐标由系统补",
          (r["corpus"][0]["chapter_index"], r["corpus"][0]["chunk_index"]),
          (7, 3))
    r, _ = normalize_result(
        {"corpus": [{"chunk_id": "c0", "excerpt": "常常想起上海的秋天"}]}, TEXT)
    check("归一：没给 meta 时坐标为 None（不瞎猜）",
          r["corpus"][0]["chapter_index"], None)


# ── 4. 消费端：model 不入注入路径 ─────────────────────────────────────────

AUTHOR = {"canonical_name": "史铁生", "nationality": "中国",
          "birth_year": 1951, "death_year": 2010, "era_note": ""}


def test_card() -> None:
    check("注入滤网：model 被滤掉",
          len(injectable([{"source": "model"}, {"source": "corpus"}])), 1)

    card = format_author_card(AUTHOR, position="narrator", records=[
        {"kind": "life_event", "year": 1951, "statement": "生于北京",
         "source": "corpus"},
        {"kind": "life_event", "year": 1998, "statement": "肾病加重",
         "source": "model"},
    ], corpus=[{"excerpt": "我常觉得这中间有着宿命的味道",
                "speaker": "author", "source": "corpus"}])
    has("卡片：有名字", "史铁生", card)
    has("卡片：有年份", "1951", card)
    has("卡片：有引文", "宿命的味道", card)
    # 硬约束：model 条目一个字都不许进注入
    check("卡片：model 条目未注入", "肾病加重" in card, False)
    check("卡片：model 条目不计数", "1998" in card, False)

    # avatar 位置必须把「借角色」这件事说给模型听
    av = format_author_card(AUTHOR, position="avatar", records=[
        {"kind": "work", "year": 1944, "statement": "写了这本书",
         "source": "corpus"}])
    has("卡片：avatar 提示借声音不借事实", "借一个角色", av)
    has("卡片：avatar 不许把角色经历说成自己的", "角色的经历", av)

    # 没有事实时诚实说没有，而不是留空让人自由发挥
    empty = format_author_card(AUTHOR, position="unknown")
    has("卡片：无事实时明说暂无", "暂无", empty)
    has("卡片：无事实时禁止编造", "不要凭空编造", empty)
    has("卡片：unknown 位置要更谨慎", "更要谨慎", empty)

    # 非 corpus 来源要带标签（可展示，但要说清出处）
    web = format_author_card(AUTHOR, position="narrator", records=[
        {"id": 1, "kind": "life_event", "year": 1951, "statement": "生于北京",
         "source": "web"}])
    has("卡片：web 未核验要标明", "网页·未核验", web)
    web_ok = format_author_card(AUTHOR, position="narrator", records=[
        {"id": 2, "kind": "life_event", "year": 1951, "statement": "生于北京",
         "source": "web"}], verified_records={"2"})
    has("卡片：web 引文已核要标明", "网页·引文已核", web_ok)
    has("卡片：身份待确认要说出来", "待确认",
        format_author_card(dict(AUTHOR, identity_status="provisional"),
                           position="narrator"))


# ── 5. 落库：幂等 + 门在库上 ──────────────────────────────────────────────

def test_db() -> None:
    from authorgraph.gate import reader_year
    from authorgraph.store import store_harvest

    tmp = Path(tempfile.mkdtemp()) / "authors.db"
    db = AuthorsDB(tmp)
    ident = {"book_id": "b1", "stable_book_id": "sb-1",
             "title": "传奇", "author": "张爱玲", "nationality": "中国"}
    result = {
        "author_name": "张爱玲", "co_authors": ["张松辉"],
        "author_aliases": ["Eileen Chang"],
        "nationality": "中国", "position": "avatar",
        "position_note": "自序里作者谈自己的写作",
        "role": "author",
        "corpus": [
            {"chunk_id": "c0", "excerpt": "我写这本书的时候",
             "speaker": "author", "text_kind": "preface", "era_year": 1944,
             "chapter_index": 0, "chunk_index": 0},
            {"chunk_id": "c1", "excerpt": "听来的故事",
             "speaker": "author", "text_kind": "preface", "era_year": 1952,
             "chapter_index": 3, "chunk_index": 0},
        ],
        "records": [{"kind": "work", "year": 1944, "statement": "写了这本书",
                     "evidence_excerpt": "我写这本书的时候", "chunk_id": "c0"}],
        "defects": [],
    }
    s1 = store_harvest(db, ident, result)
    check("落库：成功", s1["ok"], True)
    check("落库：语料 2 条", s1["corpus_added"], 2)
    check("落库：编年 1 条", s1["records_added"], 1)
    # 合著者另起一行（role=co-author），且**不继承**主作者的国籍生卒
    co = db.find_author("张松辉")
    check("落库：合著者已建", co is not None, True)
    check("落库：合著者 role 正确",
          [b["role"] for b in db.books_of(co["author_id"])], ["co-author"])
    check("落库：合著者不继承生卒", co["birth_year"], None)
    # 主作者不能同时占 author 和 co-author 两行 —— 模型有时把主作者的
    # role 填成 co-author（署名里确实有两个人），而主键含 role，
    # 于是同一个人的同一本书占了两行（实测张景）
    s3 = store_harvest(db, ident, dict(result, role="co-author"))
    check("落库：主作者 role 一律 author",
          sorted(b["role"] for b in db.books_of(s1["author_id"])), ["author"])
    check("落库：不会因 role 漂移多出一行", s3["ok"], True)

    # 重跑同一本 → 一条都不重复加
    s2 = store_harvest(db, ident, result)
    check("落库：重跑幂等（语料）", s2["corpus_added"], 0)
    check("落库：重跑幂等（编年）", s2["records_added"], 0)
    check("落库：总数不变", db.counts()["author_corpus"], 2)

    aid = s1["author_id"]
    check("落库：别名可查", db.find_author("Eileen Chang")["author_id"], aid)
    check("落库：位置已记",
          db.books_of(aid)[0]["position"], "avatar")
    # 主作者**和合著者**都标脏 —— 合著者也挂了这本书，作品房间要长出
    # 这一条。只标主作者的话，张松辉的作品房间永远是空的（实测踩到）。
    check("落库：落完标脏（主作者 + 合著者）", len(db.dirty_authors()), 2)

    # 门走**落库的坐标**（上面那条 bug 修好后它才真的会亮）
    check("库上门：读到 ch0 → 1944", reader_year(db, aid, "sb-1", 0), 1944)
    check("库上门：读到 ch2 → 1944（1952 不许露）",
          reader_year(db, aid, "sb-1", 2), 1944)
    check("库上门：读到 ch9 → 1952", reader_year(db, aid, "sb-1", 9), 1952)
    # 推不出年 → None（无门），绝不是「全放行」
    check("库上门：别的书 → None", reader_year(db, aid, "sb-other", 9), None)

    # 这一趟没判 position 时，不该把已判的抹成 unknown
    store_harvest(db, ident, {"author_name": "张爱玲", "corpus": [],
                              "records": [], "position": "unknown"})
    check("落库：不覆盖已判位置",
          db.books_of(aid)[0]["position"], "avatar")

    # 早期落下的行缺坐标 → 重跑时**就地补齐**（否则「重跑」是 no-op，
    # 而门按章号算，等于修不好）
    db.add_corpus(aid, "sb-2", "补坐标用的句子", chunk_id="c9")
    n0 = db.get_conn().execute(
        "SELECT COUNT(*) FROM author_corpus WHERE stable_book_id='sb-2'"
        " AND chapter_index IS NULL").fetchone()[0]
    db.add_corpus(aid, "sb-2", "补坐标用的句子", chunk_id="c9",
                  chapter_index=5, chunk_index=1)
    n1 = db.get_conn().execute(
        "SELECT COUNT(*) FROM author_corpus WHERE stable_book_id='sb-2'"
        " AND chapter_index IS NULL").fetchone()[0]
    check("落库：旧行缺坐标时重跑能补", (n0, n1), (1, 0))
    check("落库：补上的坐标正确", db.get_conn().execute(
        "SELECT chapter_index FROM author_corpus WHERE stable_book_id='sb-2'"
    ).fetchone()[0], 5)

    # --force 的干净重做：clear_book 只清**这一本书**贡献的料。
    # 不清的话，模型每轮挑的句子不同，唯一约束挡不住，语料越滚越多
    # （实测两轮 72 → 122）—— 台账会失真成「跑得越多知道得越多」。
    db.add_corpus(aid, "sb-3", "另一本书里的话", chunk_id="c20",
                  chapter_index=1)
    gone = db.clear_book("sb-1")
    check("落库：clear_book 清掉本书语料", gone["author_corpus"] > 0, True)
    check("落库：clear_book 不碰同作者别的书",
          db.get_conn().execute(
              "SELECT COUNT(*) FROM author_corpus WHERE stable_book_id='sb-3'"
          ).fetchone()[0], 1)
    check("落库：clear_book 后本书语料归零",
          db.get_conn().execute(
              "SELECT COUNT(*) FROM author_corpus WHERE stable_book_id='sb-1'"
          ).fetchone()[0], 0)

    # 没有名字 → 不建台账，但要说清原因
    bad = store_harvest(db, {"book_id": "b9", "stable_book_id": "sb-9",
                             "title": "无名"}, {"corpus": []})
    check("落库：无名不建台账", bad["ok"], False)
    check("落库：报出原因", bad["reason"], "no_author_name")
    db.close()


# ── 6. 环境上下文：太短的「序跋」不值得跑 ─────────────────────────────────

def _fake_books_db(chapters, chunks):
    """最小可用的 books.db 假体（只用得到 book_chapters / book_chunks）。"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE book_chapters (book_id TEXT, chapter_index INT,"
                 " title TEXT, chapter_type TEXT, level INT)")
    conn.execute("CREATE TABLE book_chunks (id TEXT, book_id TEXT,"
                 " chapter_index INT, chunk_index INT, content TEXT)")
    conn.executemany("INSERT INTO book_chapters VALUES (?,?,?,?,0)", chapters)
    conn.executemany("INSERT INTO book_chunks VALUES (?,?,?,?,?)", chunks)

    class _DB:
        def get_conn(self):
            return conn
    return _DB()


def test_scope() -> None:
    from authorgraph.scope import MIN_CANDIDATE_CHARS, candidate_chunks

    # 18 字的「序跋」（书名页被误分类）→ 不跑，并说明为什么
    db = _fake_books_db([("b1", 0, "序", "preface")],
                        [("c0", "b1", 0, 0, "局外人" * 6)])
    chunks, warns = candidate_chunks(db, "b1")
    check("上下文：极短序跋不跑", chunks, [])
    check("上下文：极短序跋要说明原因",
          any("不值得" in w for w in warns), True)

    # 够长的正常序跋照常返回
    long_text = "我写这本书的时候，常常想起上海的秋天。" * 20
    db = _fake_books_db([("b1", 0, "自序", "preface")],
                        [("c0", "b1", 0, 0, long_text)])
    chunks, warns = candidate_chunks(db, "b1")
    check("上下文：正常序跋照常返回", len(chunks), 1)
    check("上下文：正常序跋无警告", warns, [])
    check("上下文：长度门槛合理",
          len(long_text) > MIN_CANDIDATE_CHARS, True)

    # 没有序跋章 → 如实说，不是安静返回空
    db = _fake_books_db([("b1", 0, "第一章", "body")], [])
    chunks, warns = candidate_chunks(db, "b1")
    check("上下文：无序跋时说明", any("没有" in w for w in warns), True)


# ── 7. 身份归一化：同一个人的不同写法不能裂成两行 ─────────────────────────

def test_identity() -> None:
    from storage.authors_db import name_key

    check("归一化：间隔号 vs 项目符号",
          name_key("马歇尔·卢森堡") == name_key("马歇尔•卢森堡"), True)
    check("归一化：全角半角", name_key("Ｅｃｈｏ") == name_key("echo"), True)
    check("归一化：空白", name_key(" 张 景 ") == name_key("张景"), True)
    check("归一化：大小写", name_key("HARARI") == name_key("harari"), True)
    check("归一化：不同的人不撞", name_key("张景") == name_key("张松辉"), False)

    tmp = Path(tempfile.mkdtemp()) / "authors.db"
    db = AuthorsDB(tmp)
    a1 = db.upsert_author("马歇尔·卢森堡", nationality="美国")
    a2 = db.upsert_author("马歇尔•卢森堡")
    check("归一化：变体不再裂成两行", a1 == a2, True)
    check("归一化：总数仍是 1", db.counts()["authors"], 1)
    check("归一化：变体记成别名",
          db.find_author("马歇尔•卢森堡")["author_id"], a1)

    # 存量裂开的要能合 —— 且**搬全部引用表**（只搬一部分会让另一半永久失联）
    db2 = AuthorsDB(Path(tempfile.mkdtemp()) / "b.db")
    x = db2.upsert_author("珍·罗伯兹")
    db2.add_corpus(x, "sb-1", "赛斯说……", chunk_id="c1", chapter_index=0)
    db2.link_book(x, "sb-1", position="recorder")
    y = db2.upsert_author("珍•罗伯兹")            # 绕过 upsert 造一个裂口
    db2.get_conn().execute(
        "INSERT INTO authors (author_id, canonical_name, time_created,"
        " time_updated) VALUES (?,?,0,0)", (y + "x", "珍•罗伯兹"))
    db2.get_conn().commit()
    led = db2.dedupe_authors()
    check("合并：认出这一对", len(led), 1)
    # 语料必须还在 —— 只搬一部分引用表会让它指向一个不存在的 id，永久失联
    check("合并：语料没丢", db2.stats(x)["corpus"], 1)
    check("合并：位置没丢",
          db2.books_of(x)[0]["position"], "recorder")
    check("合并：只留一行", db2.counts()["authors"], 1)
    db.close()
    db2.close()


# ── 7. 消费端：取数 → 施门 → 格式化（工具与扮演接线共用的一条路） ─────────

def test_consume() -> None:
    from authorgraph.card import load_author_card
    from authorgraph.gate import gate_state
    from authorgraph.store import store_harvest

    tmp = Path(tempfile.mkdtemp()) / "authors.db"
    db = AuthorsDB(tmp)
    ident = {"book_id": "b1", "stable_book_id": "sb-1",
             "title": "传奇", "author": "张爱玲", "nationality": "中国"}
    store_harvest(db, ident, {
        "author_name": "张爱玲", "position": "narrator", "role": "author",
        "corpus": [
            {"chunk_id": "c0", "excerpt": "我写这本书的时候", "speaker": "author",
             "era_year": 1944, "chapter_index": 0, "chunk_index": 0},
            {"chunk_id": "c5", "excerpt": "听来的故事", "speaker": "author",
             "era_year": 1952, "chapter_index": 5, "chunk_index": 0},
        ],
        "records": [
            {"kind": "work", "year": 1944, "statement": "写了这本书",
             "evidence_excerpt": "我写这本书的时候", "chunk_id": "c0"},
            {"kind": "life_event", "year": 1960, "statement": "搬去了别处",
             "evidence_excerpt": "听来的故事", "chunk_id": "c5"},
        ],
    })
    aid = db.find_author("张爱玲")["author_id"]

    # 按 id 取 —— 扮演会话存的**就是** id，这条路必须通
    check("消费：get_author 按 id", db.get_author(aid)["canonical_name"], "张爱玲")
    check("消费：get_author 空 id 不炸", db.get_author(""), None)
    check("消费：authors_of_book 有行",
          [r["role"] for r in db.authors_of_book("sb-1")], ["author"])

    # 门的三态。**「空卡片」有两个原因**，不分开就查不出来
    g0 = gate_state(db, aid, "sb-1", 0)
    check("门态：读到 ch0 → 1944", g0["gate"], 1944)
    check("门态：reason=open", g0["reason"], "open")
    ga = gate_state(db, aid, "sb-1", -1)
    check("门态：读者在全部语料之前 → all_ahead", ga["reason"], "all_ahead")
    check("门态：ahead 计数", ga["ahead"], 2)

    # 卡片：不设门 = 全给（这是「全局模式」那个显式选择，不是默认值）
    free = load_author_card(db, author_id=aid, stable_book_id="sb-1",
                            reader_chapter=None)
    check("卡片：不设门 gate=no_gate", free["gate"], "no_gate")
    has("卡片：不设门给出 1960 的条目", "1960", free["card"])
    check("卡片：不设门不藏东西", free["not_shown"]["records"], 0)

    # 卡片：读到 ch0 → 门在 1944，1960 那条不许露
    gated = load_author_card(db, author_id=aid, stable_book_id="sb-1",
                             reader_chapter=0)
    check("卡片：ch0 gate=1944", gated["gate"], 1944)
    has("卡片：ch0 给出 1944 的条目", "1944", gated["card"])
    check("卡片：ch0 挡住 1960", "1960" in gated["card"], False)
    check("卡片：挡了几条报出来", gated["not_shown"]["records"], 1)

    # id 优先于名字：名字解析会漂（去重、改名），id 不会
    byid = load_author_card(db, name="完全不相干的名字", author_id=aid,
                            stable_book_id="sb-1")
    check("卡片：id 优先于名字", byid["found"], True)
    check("卡片：id 取到的是本人", byid["name"], "张爱玲")

    # 找不到要**如实说**，不是给一张空卡片
    miss = load_author_card(db, name="查无此人", stable_book_id="sb-1")
    check("卡片：查无此人 found=False", miss["found"], False)
    check("卡片：查无此人无 card", "card" in miss, False)

    # 语料都在读者后面时，note 要说清是**哪一种**空
    db.add_corpus(aid, "sb-2", "卷末的话", chunk_id="z0",
                  chapter_index=25, chunk_index=0, era_year=1990,
                  speaker="author", source="corpus")
    far = load_author_card(db, author_id=aid, stable_book_id="sb-2",
                           reader_chapter=3)
    check("卡片：全在读者之后 → unmapped", far["gate"], "unmapped")
    check("卡片：说清是 all_ahead", far["gate_reason"], "all_ahead")
    has("卡片：note 指到「还没读到的位置」", "还没读到", far["gate_note"])
    db.close()


# ── 8. 作者级：工序 4（编年校验）/ 工序 5（作品房间） ──────────────────────

def test_refresh() -> None:
    from authorgraph.refresh import (audit_records, can_check, room_of,
                                     work_room_rows)

    # 房间是 kind 的**投影**，不落库 —— 加一个 kind 只改这张表
    check("房间：work → 作品", room_of("work"), "work")
    check("房间：publication → 作品", room_of("publication"), "work")
    check("房间：life_event → 生平", room_of("life_event"), "life")
    check("房间：relation → 生平", room_of("relation"), "life")

    TEXT = {"c1": "一九四四年九月，我在南京。", "c2": "我写这本书的时候。"}
    # 逐字为真 → 干净
    f = audit_records([{"id": 1, "statement": "去了南京", "year": 1944,
                        "evidence_excerpt": "一九四四年九月，我在南京。",
                        "chunk_id": "c1", "source": "corpus"}], TEXT)
    check("工序4：逐字为真不报缺陷", [x.code for x in f], [])
    # 空白差异不算错（与图谱 fabricated_evidence 同一把尺子）
    f = audit_records([{"id": 2, "statement": "x", "year": 1944,
                        "evidence_excerpt": "一九四四年九月， 我在南京。",
                        "chunk_id": "c1", "source": "corpus"}], TEXT)
    check("工序4：空白归一后仍算原文", [x.code for x in f], [])
    # 锚对了块但原文不在里面 → 报
    f = audit_records([{"id": 3, "statement": "x", "year": 1944,
                        "evidence_excerpt": "这句话不在书里",
                        "chunk_id": "c1", "source": "corpus"}], TEXT)
    check("工序4：原文找不到 → 报", [x.code for x in f],
          ["evidence_not_verbatim"])
    # 年份像章号 → 报
    f = audit_records([{"id": 4, "statement": "x", "year": 25,
                        "evidence_excerpt": "我在南京", "chunk_id": "c1",
                        "source": "corpus"}], TEXT)
    check("工序4：年份越界 → 报", [x.code for x in f], ["year_out_of_range"])
    # 结束年早于起始年 → 报
    f = audit_records([{"id": 5, "statement": "x", "year": 1950, "year_end": 1940,
                        "evidence_excerpt": "我在南京", "chunk_id": "c1",
                        "source": "corpus"}], TEXT)
    check("工序4：年份倒序 → 报", [x.code for x in f], ["year_order"])
    # web/model 的出处不是逐字原文 —— **不能拿书的原文去比对网页摘要**
    f = audit_records([{"id": 6, "statement": "x", "year": 1944,
                        "evidence_excerpt": "https://example.com/a",
                        "source": "web"}], TEXT)
    check("工序4：web 出处不查逐字", [x.code for x in f], [])
    # 没有可比对的正文 → **不报**（校验器不该比被校验者更脆弱）
    f = audit_records([{"id": 7, "statement": "x", "year": 1944,
                        "evidence_excerpt": "随便什么", "chunk_id": "zz",
                        "source": "corpus"}], {})
    check("工序4：无比对源不报缺陷", [x.code for x in f], [])
    check("工序4：但也不算查过", can_check(
        {"evidence_excerpt": "随便什么", "chunk_id": "zz", "source": "corpus"},
        {}), False)
    check("工序4：graph 来源不可查", can_check(
        {"evidence_excerpt": "", "source": "graph"}, TEXT), False)

    # 工序 5：作品房间
    links = [
        {"stable_book_id": "sb-1", "role": "author"},
        {"stable_book_id": "sb-2", "role": "co-author"},
        {"stable_book_id": "sb-3", "role": "translator"},   # 不是他的作品
        {"stable_book_id": "sb-4", "role": "author"},       # 没有书名
    ]
    titles = {"sb-1": "面纱", "sb-2": "道德经", "sb-3": "别人的书"}
    rows = work_room_rows(links=links, titles=titles, existing=[])
    check("工序5：只补作者/合著且有书名的", [r["statement"] for r in rows],
          ["《面纱》", "《道德经》"])
    check("工序5：年份一律留空（书在库里≠知道哪年写的）",
          [r["year"] for r in rows], [None, None])
    check("工序5：来源标 graph（本地这一侧的事实）",
          {r["source"] for r in rows}, {"graph"})
    # 采集已经写过这本书 → 不再补，免得同一件事记两遍
    rows = work_room_rows(links=links, titles=titles,
                          existing=[{"statement": "一九九六年写《面纱》"}])
    check("工序5：已覆盖的不重复补", [r["statement"] for r in rows],
          ["《道德经》"])
    check("工序5：同一本书只补一次",
          len(work_room_rows(links=links + links, titles=titles, existing=[])), 2)


# ── 9. 对话触发通道：子代理产出 → 台账 ────────────────────────────────────

def test_deepen() -> None:
    import json as _json
    import storage.authors_db as ADB
    from authorgraph.store import store_harvest
    from tools import _deepen_store, _deepen_task

    tmp = Path(tempfile.mkdtemp()) / "authors.db"
    real_resolve = ADB.resolve_authors_db_path
    ADB.resolve_authors_db_path = lambda *a, **k: tmp     # type: ignore
    try:
        db = ADB.AuthorsDB(tmp)
        store_harvest(db, {"book_id": "b1", "stable_book_id": "sb-1",
                           "title": "我与地坛", "author": "史铁生"},
                      {"author_name": "史铁生", "position": "narrator",
                       "role": "author",
                       "corpus": [{"chunk_id": "c0", "excerpt": "我常觉得这中间",
                                   "speaker": "author", "chapter_index": 1,
                                   "chunk_index": 0}]})
        db.close()

        ident = {"book_id": "b1", "stable_book_id": "sb-1", "title": "我与地坛"}
        URL = "https://zh.wikipedia.org/wiki/史铁生"
        # 抓页缓存：子代理必须先抓正文，落库时才校验引文（见 tools/author.py）
        from tools import _remember_page
        _remember_page({"url": URL, "canonical_url": URL, "title": "史铁生",
                        "main_text": "史铁生（1951—2010），1980 年秋肾衰初发。",
                        "content_hash": "h", "status": "ok"})

        def _raw(parsed: dict) -> str:
            return _json.dumps({"status": "completed",
                                "result_text": _json.dumps(parsed,
                                                           ensure_ascii=False)},
                               ensure_ascii=False)

        # 正常一趟：两条落库，一条伪造出处被降级
        out = _json.loads(_deepen_store(_raw({
            "answer": "他 1980 年秋肾衰初发。",
            "records": [
                {"kind": "life_event", "year": 1980, "statement": "肾衰初发",
                 "source": "web", "url": URL,
                 "evidence_quote": "1980 年秋肾衰初发"},
                {"kind": "work", "statement": "编的", "source": "web",
                 "url": "https://made-up.example/x",
                 "evidence_quote": "随便一句"},
            ],
            "sources": [URL],
        }), "史铁生", ident, None, "他什么时候病的？"))
        check("深挖：回答透传", out["answer"], "他 1980 年秋肾衰初发。")
        check("深挖：两条都落库", out["stored"], 2)
        check("深挖：伪造出处被报", any("fabricated_source" in d
                                        for d in out["defects"]), True)

        db = ADB.AuthorsDB(tmp)
        rows = {r["statement"]: r for r in db.records_for(
            db.find_author("史铁生")["author_id"], limit=50)}
        check("深挖：web 条目带逐字引文",
              rows["肾衰初发"]["evidence_excerpt"], "1980 年秋肾衰初发")
        ev = db.evidence_for("record", str(rows["肾衰初发"]["id"]))
        check("深挖：证据指回来源", ev[0]["source_url"], URL)
        check("深挖：伪造的降级成 model", rows["编的"]["source"], "model")
        # 落完标脏 —— 作者级的活（作品房间等）该重算了
        check("深挖：落完标脏", len(db.dirty_authors()), 1)
        # 同一条再来一次不重复
        again = _json.loads(_deepen_store(_raw({
            "answer": "x", "records": [
                {"kind": "life_event", "year": 1980, "statement": "肾衰初发",
                 "source": "web", "url": URL,
                 "evidence_quote": "1980 年秋肾衰初发"}], "sources": [URL],
        }), "史铁生", ident, None, "再问一次"))
        check("深挖：重复条目不重记", (again["stored"], again["skipped"]), (0, 1))
        db.close()

        # 子代理没按 JSON 输出 → 原话透传，**什么都不落库**
        bad = _json.loads(_deepen_store(
            _json.dumps({"status": "completed", "result_text": "我查了一下…"},
                        ensure_ascii=False), "史铁生", ident, None, "q"))
        check("深挖：没按 JSON 输出就不落库", bad["stored"], 0)
        has("深挖：原话透传", "我查了一下", bad["answer"])

        # 子代理失败 → 如实报
        fail = _json.loads(_deepen_store(
            _json.dumps({"status": "failed", "error": "超时"},
                        ensure_ascii=False), "史铁生", ident, None, "q"))
        check("深挖：子代理失败如实报", fail["error"], "超时")

        # 任务书要把台账已有的东西带上（否则会重搜、重记）
        task = _deepen_task(ident, "史铁生", "他晚年写了什么？",
                            {"found": True, "position": "narrator",
                             "card": "## 你要扮演的作者：史铁生"})
        has("深挖：任务书带作者名", "史铁生", task)
        has("深挖：任务书带问题", "他晚年写了什么？", task)
        has("深挖：任务书带已有台账", "不要重搜", task)
        has("深挖：任务书带书名", "我与地坛", task)
        task2 = _deepen_task(ident, "某人", "q", {"found": False})
        has("深挖：台账没有时说清", "还没有这个人", task2)
    finally:
        ADB.resolve_authors_db_path = real_resolve       # type: ignore

    # 子代理的工具表是**全新** registry，reading_ctx 默认空 ——
    # 不显式拷的话子代理里的 author_lookup 会一路返回「没打开书」。
    from sub_agent import SubAgentRunner
    from tools import create_builtin_registry
    reg = create_builtin_registry()
    reg.reading_ctx.update({"book_id": "b1", "max_chapter": 3})
    runner = SubAgentRunner(None, reg, None)
    plain = runner._build_sub_registry("reader")
    check("子代理：默认不带上下文（所以必须显式传）",
          dict(plain.reading_ctx), {})
    scout = runner._build_sub_registry("author_scout",
                                       {"book_id": "b1", "max_chapter": 3})
    check("子代理：显式传了就拷到", scout.reading_ctx.get("max_chapter"), 3)
    check("子代理：author_scout 拿得到 author_lookup",
          scout.get("author_lookup") is not None, True)
    check("子代理：author_scout 拿得到抓页工具",
          scout.get("author_web_fetch") is not None, True)
    check("子代理：author_scout 拿不到 author_deepen（防递归）",
          scout.get("author_deepen") is None, True)
    check("子代理：reader 拿不到 author_lookup",
          plain.get("author_lookup") is None, True)
    check("子代理：不污染父 registry", reg.reading_ctx.get("book_id"), "b1")


def main() -> int:
    test_gate()
    test_normalize()
    test_card()
    test_db()
    test_scope()
    test_identity()
    test_consume()
    test_refresh()
    test_deepen()
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())

