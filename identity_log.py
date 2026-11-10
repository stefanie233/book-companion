"""身份资料变更的证据日志（append-only）。

记录「哪个会话、什么时候、把哪个字段从什么改成了什么」。写入点只有两处：
``update_identity`` 工具（source=agent，带对话的 session_id）与设置页的
PUT 路由（source=settings，没有会话）。读取走
``SessionDB.list_identity_events``。

与 usage_log 共用 sessions.db 单例与测试隔离开关（``BOOK_COMPANION_USAGE_DB``）。
按项目惯例 best-effort：日志写失败绝不能阻断用户的改名动作。
"""
from __future__ import annotations

import datetime
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterable

log = logging.getLogger(__name__)

#: 工具执行时所在的对话会话；由 runtime.run_turn / Web chat 入口设置。
_SESSION_ID: ContextVar[str] = ContextVar("identity_session_id", default="")

IDENTITY_FIELDS: tuple[str, ...] = ("user_name", "user_gender", "agent_name")
SOURCE_AGENT = "agent"
SOURCE_SETTINGS = "settings"


def current_session_id() -> str:
    """当前工具调用所属的会话 id；不在对话里（如设置页）返回空串。"""
    return _SESSION_ID.get()


def enter_session(session_id: str):
    """把当前任务归属到一个会话，返回可传给 :func:`exit_session` 的 token。"""
    return _SESSION_ID.set(str(session_id or ""))


def exit_session(token) -> None:
    """恢复进入前的会话归属（子代理跑完必须恢复父回合，否则记错会话）。"""
    if token is not None:
        _SESSION_ID.reset(token)


@contextmanager
def session_scope(session_id: str):
    """把这一段执行归属到一个会话；嵌套安全，退出时恢复。"""
    token = enter_session(session_id)
    try:
        yield
    finally:
        exit_session(token)


def _store():
    # 延迟导入，避免 storage 在 import 期反向依赖。
    from usage_log import get_store
    return get_store()


def read_events(
    *,
    field: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """查询变更记录（新→旧），带可读时间与会话标题。读失败返回空结果。"""
    try:
        db = _store()
        rows, total = db.list_identity_events(
            field=field, session_id=session_id, limit=limit, offset=offset,
        )
    except Exception as exc:                        # noqa: BLE001
        log.warning("identity event read failed: %s", exc)
        return {"events": [], "total": 0, "error": str(exc)}
    events = []
    for row in rows:
        ts = int(row.get("time_created") or 0)
        item = {
            "field": row.get("field") or "",
            "old": row.get("old_value") or "",
            "new": row.get("new_value") or "",
            "source": row.get("source") or "",
            "session_id": row.get("session_id") or "",
            "time": datetime.datetime.fromtimestamp(ts / 1000).strftime(
                "%Y-%m-%d %H:%M:%S") if ts else "",
        }
        if item["session_id"]:
            try:
                info = db.get_session(item["session_id"])
                if info is not None:
                    item["session_title"] = info.title or ""
            except Exception:                       # noqa: BLE001
                pass
        events.append(item)
    return {"events": events, "total": total}


def record_changes(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    source: str,
    session_id: str | None = None,
    fields: Iterable[str] | None = None,
) -> list[str]:
    """把 ``old`` → ``new`` 之间真正变化的身份字段各记一行。

    返回实际写入的字段名；任何异常都吞掉（best-effort）。
    """
    wanted = tuple(fields) if fields is not None else IDENTITY_FIELDS
    changed = [
        f for f in wanted
        if (str(old.get(f) or "") != str(new.get(f) or ""))
    ]
    if not changed:
        return []
    sid = current_session_id() if session_id is None else str(session_id or "")
    written: list[str] = []
    try:
        db = _store()
        for field in changed:
            db.insert_identity_event(
                field,
                str(old.get(field) or ""),
                str(new.get(field) or ""),
                session_id=sid,
                source=source,
            )
            written.append(field)
    except Exception as exc:                        # noqa: BLE001
        log.warning("identity event log failed: %s", exc)
    return written
