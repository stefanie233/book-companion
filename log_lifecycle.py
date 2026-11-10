"""日志的统一出口：路径解析、按大小轮转、按年龄回收（决策 147）。

`data/logs/` 下的东西分三类，生命周期各不相同：

  * **观测流水**（可丢）`api.log` / `memory.log` / `providers.log` /
    `reader_sync.log` —— 追加写没问题，问题是没有上界。走本模块轮转。
  * **审计投影**（可回放）`bookgraph.jsonl`、
    `data/writing/reference_cards/diagnostics.jsonl` —— 同样是追加写，
    但要压小（全文在 DB 里）并只留最近几份。
  * **飞行记录仪**（DB）`data/context_log.db` 与各 `graph.db` 的
    `graph_llm_logs` —— 不是文本文件，按年龄回收由
    `scripts/log_maintenance.py` 负责。

轮转命名用 `name.1`（最新）…`name.N`，与 logrotate 习惯一致；
`bookgraph.jsonl.1` 不会被 `scripts/backfill_usage.py` 误读（它只认精确路径）。
所有写入都是 best-effort：日志失败绝不能把业务带走。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: 默认单文件上限与保留份数。低频日志（providers / diagnostics）调用方
#: 各自传更小的值；高频日志（sync trace）用默认值足够。
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3


def log_dir() -> Path:
    """`data/logs/` 的绝对路径。

    历史调用点用 `Path("data/logs")`（相对 cwd），从别的目录启动服务就会
    把日志写到别处 —— 这也是测试污染生产日志的一半原因。统一从**用户根**
    解析（源码 = 仓库根，安装版 = %LOCALAPPDATA%\\LiuBai），冻结后日志
    不会写进只读的安装目录。
    """
    from runtime_paths import data_path

    path = data_path("logs")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rotate(path: Path, max_bytes: int, backup_count: int) -> None:
    """超限就把 `name` 挪成 `name.1`，旧的依次后移，最老的一份丢弃。"""
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        if backup_count <= 0:
            path.unlink()
            return
        oldest = path.with_name(f"{path.name}.{backup_count}")
        if oldest.exists():
            oldest.unlink()
        for i in range(backup_count - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{i + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass  # 轮转失败不阻断写入（下一次还会再试）


def append_line(
    path: Path,
    text: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> bool:
    """追加一行，写前检查体积。返回是否写成功（bug 不抛）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path, max_bytes, backup_count)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        return True
    except Exception:
        return False


def append_jsonl(
    path: Path,
    obj: dict[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> bool:
    """追加一条 JSON 记录（换行分隔）。"""
    try:
        line = json.dumps(obj, ensure_ascii=False)
    except Exception:
        return False
    return append_line(path, line, max_bytes=max_bytes,
                       backup_count=backup_count)


def sync_trace_enabled() -> bool:
    """Reader 同步轨迹的开关。

    每滚一次屏就写两行，日常使用纯噪声；只有排查视窗对齐/定章时才需要。
    默认关闭，`BC_READER_SYNC_TRACE=1` 打开（也认带前缀的完整变量名）。
    """
    for key in ("BC_READER_SYNC_TRACE", "BOOK_COMPANION_READER_SYNC_TRACE"):
        if os.environ.get(key, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def prune_rotated(
    *, days: int = 30, log_path: Path | None = None, dry_run: bool = False
) -> list[Path]:
    """删除超过 *days* 天的轮转文件（只动本模块产出/认可的名字）。

    认两种：`<name>.<n>` 与历史命名 `bookgraph-<ts>.jsonl`。活跃文件本身
    （无数字后缀）永远不动 —— 回收活跃日志是 `log_maintenance.py` 的活，
    它会在删除前把内容按需归档。
    """
    import time

    root = log_path or log_dir()
    cutoff = time.time() - days * 86400
    removed: list[Path] = []
    if not root.exists():
        return removed
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        is_rotated = name.rsplit(".", 1)[-1].isdigit() or (
            name.startswith("bookgraph-") and name.endswith(".jsonl"))
        if not is_rotated:
            continue
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            if not dry_run:
                p.unlink()
            removed.append(p)
        except OSError:
            continue
    return removed
