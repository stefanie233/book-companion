"""作者细节的后台任务：攻略 / 抽取 / 钩子闭合。

长任务不能跑在服务进程里（会冻事件循环），所以这里只做三件事：
起一个独立子进程跑 ``scripts/run_author_details.py``、把 stdout 收进日志、
把状态暴露给轮询接口。**同一时间只允许一个任务** —— 子进程之间会抢 SQLite。
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

ACTIONS = {
    "plan": ("生成攻略", ["--plan"]),
    "extract": ("抽取细节", ["--extract"]),
    "close_hooks": ("闭合钩子", ["--close-hooks"]),
}
MAX_LINES = 400


def build_command(root: Path, author_name: str, action: str,
                  force: bool = False) -> list[str]:
    """拼独立进程命令。纯函数，测试不需要真的起进程。"""
    spec = ACTIONS.get(action)
    if spec is None:
        raise ValueError(f"未知动作：{action}")
    from runtime_paths import script_command

    args = ["--author", author_name, *spec[1]]
    if force and action == "extract":
        args.append("--force")
    return script_command("run_author_details.py", args, root=root)


def _now() -> float:
    return round(time.time(), 3)


class AuthorJobRunner:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.log_dir = self.root / "data" / "logs" / "author_jobs"
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active = ""
        self._lock = asyncio.Lock()

    # ── 查询 ──────────────────────────────────────────────────────────
    @staticmethod
    def snapshot(job: dict[str, Any], *, lines: bool = True) -> dict[str, Any]:
        out = {k: copy.deepcopy(v) for k, v in job.items()
               if k not in ("task", "proc") and (lines or k != "lines")}
        return out

    def active(self) -> dict[str, Any] | None:
        job = self._jobs.get(self._active)
        return self.snapshot(job) if job else None

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return self.snapshot(job) if job else None

    def list_for(self, author_id: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = [self.snapshot(j, lines=False) for j in self._jobs.values()
                if j.get("author_id") == author_id]
        rows.sort(key=lambda j: j.get("started_at") or 0, reverse=True)
        return rows[:max(1, limit)]

    # ── 启动与取消 ────────────────────────────────────────────────────
    async def start(self, *, author_id: str, author_name: str, action: str,
                    force: bool = False
                    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """返回 ``(新任务, 正在跑的任务)``；已有任务在跑时新任务为 None。"""
        if action not in ACTIONS:
            raise ValueError(f"未知动作：{action}")
        async with self._lock:
            running = self.active()
            if running and running.get("status") == "running":
                return None, running
            jid = "aj-" + uuid.uuid4().hex[:12]
            job: dict[str, Any] = {
                "job_id": jid, "author_id": author_id,
                "author_name": author_name, "action": action,
                "action_label": ACTIONS[action][0], "force": bool(force),
                "status": "running", "started_at": _now(), "ended_at": None,
                "returncode": None, "error": "", "cancel_requested": False,
                "lines": [], "line_count": 0, "tail": "", "summary": None,
                "log_path": "",
            }
            self._jobs[jid] = job
            self._active = jid
            job["task"] = asyncio.create_task(self._run(job))
            return self.snapshot(job), None

    async def shutdown(self) -> None:
        """服务退出时收掉子进程 —— 别把长任务留成孤儿。"""
        job = self._jobs.get(self._active)
        if not job or job.get("status") != "running":
            return
        await self.cancel(job["job_id"])
        proc = job.get("proc")
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:                          # noqa: BLE001
                pass

    async def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.get("status") == "running":
            job["cancel_requested"] = True
            proc = job.get("proc")
            if proc is not None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        return self.snapshot(job)

    # ── 子进程 ────────────────────────────────────────────────────────
    async def _run(self, job: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{job['job_id']}.log"
        job["log_path"] = str(log_path)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        extra: dict[str, Any] = {}
        if flags:                                      # 仅 Windows 认这个参数
            extra["creationflags"] = flags
        cmd = build_command(self.root, job["author_name"], job["action"],
                            job["force"])
        try:
            with open(log_path, "w", encoding="utf-8") as logf:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(self.root), env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, **extra)
                job["proc"] = proc
                assert proc.stdout is not None
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip()
                    self._append(job, line)
                    logf.write(line + "\n")
                    logf.flush()
                job["returncode"] = await proc.wait()
        except Exception as exc:                       # noqa: BLE001
            job["error"] = str(exc)
            job["returncode"] = -1
        finally:
            job.pop("proc", None)
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
            elif job.get("error") or job.get("returncode") not in (0, None):
                job["status"] = "failed"
            else:
                job["status"] = "done"
            job["ended_at"] = _now()

    @staticmethod
    def _append(job: dict[str, Any], line: str) -> None:
        lines = job["lines"]
        lines.append(line)
        if len(lines) > MAX_LINES:
            del lines[:len(lines) - MAX_LINES // 2]
        job["line_count"] += 1
        job["tail"] = line
        m = re.search(r"观察 \+(\d+).*钩子 \+(\d+)(?:.*闭合 (\d+))?.*失败 (\d+)",
                      line)
        if m:
            job["summary"] = {"observations": int(m.group(1)),
                              "hooks": int(m.group(2)),
                              "closed": int(m.group(3) or 0),
                              "failed": int(m.group(4))}
