#!/usr/bin/env python3
"""Book Companion — 测试靶场生成器。

把整个项目复制到独立目录（代码与真实项目完全相同），换一个端口，
数据只读共享（bookbank junction）+ 独立 DB 快照，不污染真实库。

用法:
    python scripts/make_testbed.py --dest c:\\tmp\\bc2 [--port 8899] [--run]
         [--seed snapshot|fixture] [--tag TAG] [--provider fake|real]
    python scripts/make_testbed.py --capture-fixtures [--tag TAG]
    python scripts/make_testbed.py --verify-fixtures [--tag TAG]
    python scripts/make_testbed.py --list-fixtures
    python scripts/make_testbed.py --wipe --dest c:\\tmp\\bc2

迁移演练（Layer 2 核心）:
    改迁移代码前:   --capture-fixtures   把当前四库快照成基线 + manifest（行数台账）
    改迁移代码后:   --verify-fixtures    副本上用当前代码开旧库，比对行数无丢失
    （check_all.py 的 fixtures 检查自动做后一步）
    若某次迁移设计上就要搬行/删表，在 manifest 对应 db 条目加
    "moved_tables": ["表名"] 豁免，其余表仍严格比对。

生成后:
    cd <dest> && run_server.bat     # 起 API 服务（纯后端，无 REPL）
    python main.py                  # 完整 CLI（带 REPL），端口同样已改
    前端: http://127.0.0.1:<port>/

端口替换点（复制品内，不影响真实项目）:
    reader/js/state.js   S.API 硬编码地址
    main.py              web.TCPSite 端口
    api_server.py        run_server 默认端口 + 启动日志
    start.bat            健康检查 / 打开 URL
"""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8899
FIXTURES_DIR = PROJECT_ROOT / "data" / "fixtures"
TESTBED_MARKER = ".bc_testbed"
FAKE_PROVIDER_PORT = 8890

# 迁移演练：db 文件名 -> (module, owning class)，用当前代码开旧库
DB_OWNERS = {
    "books.db": ("storage.db", "BookDB"),
    "sessions.db": ("storage.db_session", "SessionDB"),
    "memory.db": ("wingman.db", "WingmanDatabase"),
}
# 每书图谱库（data/graphs/graph_*.db）统一归 GraphDB，按前缀识别
GRAPH_OWNER = ("storage.graph_db", "GraphDB")


def _iter_source_dbs(src_data: Path):
    """顶层 *.db + 每书图谱库（data/graphs/*.db），产出 (相对键, 路径)。"""
    for db in sorted(src_data.glob("*.db")):
        yield db.name, db
    gdir = src_data / "graphs"
    if gdir.exists():
        for db in sorted(gdir.glob("*.db")):
            yield f"graphs/{db.name}", db


def _owner_for(key: str):
    if key in DB_OWNERS:
        return DB_OWNERS[key]
    if key.startswith("graphs/"):
        return GRAPH_OWNER
    return None

# 复制代码时排除的目录/后缀（data 单独处理）
EXCLUDE_DIRS = {".git", ".claude", ".codegraph", ".impeccable",
                ".understand-anything", "__pycache__", ".venv", "data"}
EXCLUDE_EXTS = {".pyc"}

# 靶场 data/ 下需独立建空的子目录（logs/memory/sessions 等测试写入隔离）
DATA_SUBDIRS = ["logs", "memory", "sessions", "voice", "watch",
                "scenes", "models", "graphs", "config"]


def copytree_code(src: Path, dst: Path) -> None:
    """复制项目代码（排除 data/ 与元数据目录）。"""
    def _ignore(d: str, names: list[str]) -> set[str]:
        return {n for n in names if n in EXCLUDE_DIRS or Path(n).suffix in EXCLUDE_EXTS}
    shutil.copytree(src, dst, ignore=_ignore)


def make_junction(src: Path, dst: Path) -> None:
    """Windows junction（可跨卷，无需管理员）。失败抛异常。"""
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"mklink 失败: {(r.stderr or r.stdout or '').strip()}")


def snapshot_db(src_db: Path, dst_db: Path) -> None:
    """用 sqlite backup API 做一致性快照（源库可能正被真实服务占用/WAL）。"""
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dst_db))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


# ── 迁移演练基线（fixtures）─────────────────────────────────────────────

def _table_counts(db_path: Path) -> dict:
    """所有真实表 + 行数——演练比对的台账基础（FTS 等虚表是派生索引，排除）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()
        virtual = {n for n, sql in rows
                   if sql and sql.lstrip().upper().startswith("CREATE VIRTUAL")}
        # FTS5 的影子表（``<虚表>_data`` / ``_idx`` / ``_docsize`` / ``_config``）
        # **不是业务行** —— 它们是索引的内部页结构，任何一次 ``rebuild`` /
        # ``OPTIMIZE`` 都会改变其行数，而**数据丢失的信号在内容表上**（影子表只是
        # 内容的镜像，内容少了内容表自己会报）。实测迁移 10 重建 ``nodes_fts`` 后，
        # 8 个图谱库的影子表全部变化、业务表一行没动 —— 不过滤就是 32 条假警报，
        # 而假警报多了这道闸就没人看了。
        # 旧 manifest 里记着这些表，但它们在新的 ``after`` 里不存在 → 走
        # ``_verify_one_db`` 的 ``tbl not in after`` 分支自动跳过，无需改台账。
        shadow = {n for n, _ in rows
                  if any(n.startswith(v + "_") for v in virtual)}
        names = [n for n, sql in rows
                 if n not in virtual and n not in shadow]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                for n in names}
    finally:
        conn.close()


def _schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone()
        if not has:
            return 0
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _resolve_fixture_dir(tag: str | None) -> Path:
    if not FIXTURES_DIR.exists():
        raise SystemExit("[错误] data/fixtures/ 不存在，先 --capture-fixtures")
    if tag:
        fdir = FIXTURES_DIR / tag
        if not (fdir / "manifest.json").exists():
            raise SystemExit(f"[错误] fixture 不存在: {tag}")
        return fdir
    cands = sorted(p for p in FIXTURES_DIR.iterdir()
                   if p.is_dir() and (p / "manifest.json").exists())
    if not cands:
        raise SystemExit("[错误] 没有任何 fixture，先 --capture-fixtures")
    return cands[-1]  # tag 用日期命名时 sorted 即最新


def capture_fixtures(tag: str) -> int:
    """把当前真实库快照成迁移演练基线：data/fixtures/<tag>/ + manifest 台账。"""
    src_data = PROJECT_ROOT / "data"
    dest = FIXTURES_DIR / tag
    if dest.exists() and any(dest.iterdir()):
        print(f"[错误] fixture 已存在且非空: {dest}（换 --tag 或先删除）")
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    if not list(_iter_source_dbs(src_data)):
        print("[错误] data/ 下没有可快照的 db")
        return 1
    manifest: dict = {"captured_at": datetime.now().isoformat(timespec="seconds"),
                      "dbs": {}}
    for key, db in _iter_source_dbs(src_data):
        target = dest / key
        target.parent.mkdir(parents=True, exist_ok=True)
        snapshot_db(db, target)
        entry = {"schema_version": _schema_version(target),
                 "tables": _table_counts(target)}
        manifest["dbs"][key] = entry
        print(f"  [OK] {key} v{entry['schema_version']}"
              f"（{len(entry['tables'])} 表入库账）")
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[完成] 基线已固化: {dest}")
    print("  之后每次改动跑 python scripts/check_all.py 即自动演练旧库迁移")
    return 0


def _verify_one_db(tdir: Path, db_name: str, info: dict) -> list:
    """在临时副本上用当前代码开旧库跑迁移，比对行数。返回问题列表。"""
    owner = _owner_for(db_name)
    if owner is None:
        print(f"  [--] {db_name}: 无 owning 类映射，跳过演练")
        return []
    sys.path.insert(0, str(PROJECT_ROOT))
    moved = set(info.get("moved_tables") or [])
    # schema_version 是迁移版本账本：旧基线被新代码打开时必然新增"已应用
    # 版本"记录，不属于业务行。演练只比对业务表行数无丢失，故豁免。
    skip_tables = {"schema_version"}
    problems: list = []
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / Path(db_name).name  # 键含 graphs/ 前缀，扁平化到临时根
        shutil.copy2(tdir / db_name, work)
        inst = getattr(importlib.import_module(owner[0]), owner[1])(work)
        try:
            inst.initialize()
        finally:
            closer = getattr(inst, "close", None)
            if closer:
                closer()
        after = _table_counts(work)
        ver_now = _schema_version(work)
    for tbl, cnt in info["tables"].items():
        if tbl in moved or tbl in skip_tables or tbl not in after:
            continue  # 迁移设计内的搬行/改名/删表
        if after[tbl] != cnt:
            problems.append(f"{db_name}.{tbl}: {cnt} -> {after[tbl]} 行")
    flag = "OK" if not problems else "FAIL"
    print(f"  [{flag}] {db_name} v{info['schema_version']} -> v{ver_now}"
          f"（{len(info['tables'])} 表比对）")
    return problems


def verify_fixtures(tag: str | None) -> int:
    """迁移演练：每个 fixture 副本用当前代码开库迁移，行数须与台账一致。"""
    if not FIXTURES_DIR.exists():
        print("[SKIP] data/fixtures/ 不存在（先 --capture-fixtures 固化基线）")
        return 0
    tags = sorted(p.name for p in FIXTURES_DIR.iterdir()
                  if p.is_dir() and (p / "manifest.json").exists())
    if tag:
        tags = [t for t in tags if t == tag]
        if not tags:
            print(f"[错误] fixture 不存在: {tag}")
            return 1
    if not tags:
        print("[SKIP] 没有任何 fixture")
        return 0
    print(f"[演练] {len(tags)} 个基线 × 当前代码:")
    all_problems: list = []
    for t in tags:
        tdir = FIXTURES_DIR / t
        manifest = json.loads((tdir / "manifest.json").read_text(encoding="utf-8"))
        print(f" - {t}（captured {manifest.get('captured_at', '?')}）")
        for db_name, info in manifest.get("dbs", {}).items():
            all_problems += _verify_one_db(tdir, db_name, info)
    if all_problems:
        print("\n[FAIL] 演练发现行数变化（确认是否设计内搬行）:")
        for p in all_problems:
            print(f"  {p}")
        return 1
    print("\n[PASS] 所有基线迁移后行数一致")
    return 0


def list_fixtures() -> int:
    if not FIXTURES_DIR.exists() or not any(FIXTURES_DIR.iterdir()):
        print("（空）还没有基线，用 --capture-fixtures 固化")
        return 0
    for tdir in sorted(FIXTURES_DIR.iterdir()):
        mf = tdir / "manifest.json"
        if not mf.exists():
            print(f"  [?] {tdir.name}: 缺 manifest.json")
            continue
        m = json.loads(mf.read_text(encoding="utf-8"))
        vers = ", ".join(f"{n.replace('.db', '')}:v{d['schema_version']}"
                         for n, d in m.get("dbs", {}).items())
        print(f"  {tdir.name}  {m.get('captured_at', '?')}  ({vers})")
    return 0


def write_fake_providers(dst: Path) -> None:
    """靶场 providers.json：三槽位全绑本地 fake_provider，零 token 离线跑。"""
    pid = "fake-provider"
    now = datetime.now().isoformat(timespec="seconds")
    data = {
        "version": 2,
        "providers": [{
            "id": pid, "name": "fake-provider", "vendor": "local",
            "base_url": f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/v1",
            "api_key": "fake-key",
            "models": [
                {"id": "fake-model", "kind": "llm"},
                {"id": "fake-embedding", "kind": "embedding"},
                {"id": "fake-tts", "kind": "tts"},
            ],
            "extra": {}, "created_at": now, "updated_at": now,
        }],
        "slots": {
            "main_llm": {"provider_id": pid, "model": "fake-model", "params": {}},
            "embedding": {"provider_id": pid, "model": "fake-embedding", "params": {}},
            "tts": {"provider_id": pid, "model": "fake-tts", "params": {}},
        },
    }
    cfg = dst / "data" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "providers.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def wipe_testbed(dest_str: str | None) -> int:
    """删除靶场目录——仅限带 .bc_testbed 标记的目录（防误删）。"""
    if not dest_str:
        print("[错误] --wipe 需要 --dest")
        return 1
    dest = Path(dest_str).resolve()
    if not dest.exists():
        print(f"[错误] 目录不存在: {dest}")
        return 1
    if not (dest / TESTBED_MARKER).exists():
        print(f"[拒绝] {dest} 缺 {TESTBED_MARKER} 标记"
              "（旧靶场 bc2 或非靶场目录），确认后请手动删除")
        return 1
    shutil.rmtree(dest)
    print(f"[OK] 已删除靶场: {dest}")
    return 0


def setup_data(src_data: Path, dst_data: Path, *,
               seed: str = "snapshot", tag: str | None = None) -> list:
    """建独立 data/：bookbank junction + DB（快照或 fixture）+ 空子目录。"""
    dst_data.mkdir(parents=True, exist_ok=True)
    warns: list = []

    # 1) bookbank → junction（读真实书文件，不复制、双写）
    make_junction(src_data / "bookbank", dst_data / "bookbank")

    # 2) DB：真实库一致性快照，或来自迁移演练基线（fixture），含每书图谱库
    if seed == "fixture":
        fdir = _resolve_fixture_dir(tag)
        for key, db in _iter_source_dbs(fdir):
            target = dst_data / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db, target)
        print(f"  [seed] DB 来自 fixture: {fdir.name}（首次启动将执行迁移）")
    else:
        for key, db in _iter_source_dbs(src_data):
            target = dst_data / key
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                snapshot_db(db, target)
            except Exception as exc:
                # backup 失败（如库被独占锁）：退化为直接复制三个文件
                try:
                    for f in db.parent.glob(db.name + "*"):
                        shutil.copy2(f, target.parent / f.name)
                    warns.append(f"{key}: backup 失败改用文件复制 ({exc})")
                except Exception as exc2:
                    warns.append(f"{key}: 快照失败，跳过 ({exc2})")

    # 3) 其余子目录建空（测试写入完全隔离）
    for sub in DATA_SUBDIRS:
        (dst_data / sub).mkdir(exist_ok=True)

    return warns


def apply_port_fixes(dst: Path, port: int) -> list[str]:
    """把复制品里的 8766 精确替换为靶场端口。返回每处替换统计。"""
    fixes = [
        ("reader/js/state.js", "API: 'http://127.0.0.1:8766',", "前端 S.API"),
        ("main.py", 'web.TCPSite(api_runner, "127.0.0.1", 8766, reuse_address=True)',
         "main.py 服务端口"),
        ("api_server.py", "port: int = 8766,", "api_server 默认端口"),
        ("api_server.py", "Book Companion API starting on http://127.0.0.1:8766",
         "api_server 启动日志"),
        ("start.bat", "127.0.0.1:8766", "start.bat 健康检查/URL"),
    ]
    reports: list[str] = []
    old, new = "8766", str(port)
    for rel, needle, label in fixes:
        p = dst / rel
        if not p.exists():
            reports.append(f"  [跳过] {rel} 不存在")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        cnt = text.count(needle)
        if cnt == 0:
            reports.append(f"  [警告] {rel} 未找到替换点: {label}")
            continue
        # 用占位符法避免 start.bat 等多次出现时误替换
        placeholder = f"__PORT_{label}__"
        staged = text.replace(needle, placeholder)
        staged = staged.replace(placeholder, needle.replace(old, new))
        p.write_text(staged, encoding="utf-8")
        reports.append(f"  [OK] {rel} ×{cnt} — {label} → :{port}")
    return reports


def write_run_script(dst: Path, port: int) -> None:
    """生成 run_server.bat：纯后端启动（无 REPL）。"""
    bat = dst / "run_server.bat"
    bat.write_text(
        "@echo off\r\nchcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        f"echo [靶场] 启动 API 服务 (端口 {port})...\r\n"
        "python api_server.py\r\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成 Book Companion 测试靶场 / 管理迁移演练基线")
    ap.add_argument("--dest", help="靶场目录（生成 / --wipe 时必填）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"靶场端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--run", action="store_true",
                    help="创建后立即后台启动服务并健康检查")
    ap.add_argument("--seed", choices=["snapshot", "fixture"], default="snapshot",
                    help="DB 来源：真实库快照（默认）或演练基线 fixture")
    ap.add_argument("--tag", help="fixture 标签（默认取最新）")
    ap.add_argument("--provider", choices=["fake", "real"], default="fake",
                    help="槽位绑定：fake-provider（默认，零 token）或沿用 .env")
    ap.add_argument("--capture-fixtures", action="store_true",
                    help="把当前真实库快照成演练基线")
    ap.add_argument("--verify-fixtures", action="store_true",
                    help="演练：当前代码开基线副本，比对行数")
    ap.add_argument("--list-fixtures", action="store_true")
    ap.add_argument("--wipe", action="store_true",
                    help="删除 --dest 靶场（仅限带标记目录）")
    args = ap.parse_args()

    if args.capture_fixtures:
        return capture_fixtures(args.tag or datetime.now().strftime("%Y%m%d"))
    if args.verify_fixtures:
        return verify_fixtures(args.tag)
    if args.list_fixtures:
        return list_fixtures()
    if args.wipe:
        return wipe_testbed(args.dest)
    return generate(args)


def generate(args) -> int:
    dest = Path(args.dest).resolve() if args.dest else None
    if dest is None:
        print("[错误] 生成靶场需要 --dest")
        return 1
    if dest == PROJECT_ROOT:
        print("[错误] --dest 不能是项目根目录")
        return 1
    if dest.exists() and any(dest.iterdir()):
        print(f"[错误] 目标目录非空: {dest}")
        return 1

    print(f"[1/6] 复制项目代码 → {dest}")
    copytree_code(PROJECT_ROOT, dest)

    print("[2/6] 搭建独立 data/（bookbank junction + DB）")
    warns = setup_data(PROJECT_ROOT / "data", dest / "data",
                       seed=args.seed, tag=args.tag)
    for w in warns:
        print(f"  [警告] {w}")

    print(f"[3/6] 端口替换 8766 → {args.port}")
    for r in apply_port_fixes(dest, args.port):
        print(r)

    print("[4/6] 生成 run_server.bat + 槽位绑定")
    write_run_script(dest, args.port)
    if args.provider == "fake":
        write_fake_providers(dest)
        print(f"  [seed] 三槽位 → fake-provider :{FAKE_PROVIDER_PORT}"
              "（用时先起 python scripts/fake_provider.py）")
    else:
        print("  [seed] 保持 .env 行为（真实 API）")

    print(f"[5/6] 写入靶场标记 {TESTBED_MARKER}")
    (dest / TESTBED_MARKER).write_text(
        json.dumps({"created_at": datetime.now().isoformat(timespec="seconds"),
                    "port": args.port, "seed": args.seed},
                   ensure_ascii=False), encoding="utf-8")

    print(f"[6/6] 完成。靶场就绪: {dest}")
    print()
    print("  启动 API 服务:  cd 靶场目录 && run_server.bat   (或 python api_server.py)")
    print("  完整 CLI:        python main.py")
    print(f"  前端:            http://127.0.0.1:{args.port}/")
    print("  说明: bookbank 是 junction（读真实书），DB 独立，测试写入不污染真实数据。")
    print(f"  用完删除:        python scripts/make_testbed.py --wipe --dest {dest}")

    if args.run:
        import time
        proc = subprocess.Popen(
            [sys.executable, "api_server.py"], cwd=dest,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ok = False
        for _ in range(40):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            try:
                import urllib.request
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{args.port}/api/health", timeout=2) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:
                continue
        if ok:
            print(f"  [OK] 服务已后台启动 (PID {proc.pid}) → http://127.0.0.1:{args.port}/")
            print("       停止: taskkill /F /PID %d" % proc.pid)
        else:
            print(f"  [错误] 服务未就绪 (PID {proc.pid}，请查看靶场日志)")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
