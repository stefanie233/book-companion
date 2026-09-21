"""SessionService — the business-logic layer bridging CLI ↔ SQLite storage.

Replaces direct SessionStore / session.py persistence calls throughout the app.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from config import Config
from content import ContentBlock, ConversationMessage, TokenUsage
from session import Session, _block_to_dict, _dict_to_block
from storage.db_session import SessionDB
from storage.models import MessageRow, PartRow, SessionRow
from services.session_info import SessionInfo


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return str(uuid.uuid4().hex[:16])


class SessionService:
    """Manages the full session lifecycle: CRUD, fork, migration."""

    def __init__(self, db: SessionDB, config: Config, hook_pipeline=None):
        self._db = db
        self._config = config
        self._hook_pipeline = hook_pipeline  # Optional HookPipeline for lifecycle events
        self._migrated = False

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def ensure_migrated(self) -> None:
        """No-op — JSONL migration already completed in a previous version."""
        self._migrated = True

    # ── CRUD ───────────────────────────────────────────────────────────────

    def create(self, *, model: str | None = None, mode: str = "immersive",
               parent_id: str | None = None) -> Session:
        """Create a new in-memory Session. NOT persisted until first save()."""
        from session import Session as SessionModel
        session = SessionModel()
        session.model = model or self._config.model
        session.mode = mode
        if parent_id:
            from session import SessionFork
            session.fork = SessionFork(parent_session_id=parent_id)
        return session

    def list(self, *, limit: int = 100, offset: int = 0,
             root_only: bool = False, search: str | None = None,
             project_id: str | None = None,
             character_name: str | None = None) -> list[SessionInfo]:
        """List sessions (metadata only, no messages)."""
        rows = self._db.list_sessions(
            limit=limit, offset=offset,
            root_only=root_only, search=search,
            project_id=project_id,
            character_name=character_name,
        )
        return [self._row_to_info(r) for r in rows]

    def get(self, session_id: str) -> SessionInfo | None:
        """Get session metadata by ID."""
        row = self._db.get_session(session_id)
        return self._row_to_info(row) if row else None

    def list_children(self, parent_id: str) -> list[SessionInfo]:
        """List direct child sessions (forks) of a parent, oldest first."""
        rows = self._db.list_children(parent_id)
        return [self._row_to_info(r) for r in rows]

    def count_children_batch(self, parent_ids: list[str]) -> dict[str, int]:
        """Batch count direct children for multiple parents."""
        return self._db.count_children_batch(parent_ids)

    def search_content(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search across session messages (FTS5 + LIKE fallback)."""
        return self._db.search_session_content(query, limit=limit)

    def load_messages(self, session_id: str) -> Session | None:
        """Full session load: metadata + all messages with parts."""
        row = self._db.get_session(session_id)
        if not row:
            return None
        from session import Session as SessionModel, SessionFork, SessionCompaction
        session = SessionModel()
        session.session_id = row.id
        session.created_at_ms = row.time_created
        session.updated_at_ms = row.time_updated
        session.model = row.model
        session.mode = row.mode
        session.compaction = SessionCompaction(
            count=row.compaction_count,
            removed_message_count=0,
            summary="",
        ) if row.compaction_count > 0 else None
        session.fork = SessionFork(parent_session_id=row.parent_id) if row.parent_id else None

        # Load messages
        msg_dicts = self._db.load_messages(session_id)
        for md in msg_dicts:
            usage_data = json.loads(md["usage"]) if md.get("usage") else None
            usage = TokenUsage(
                input_tokens=usage_data.get("input", 0),
                output_tokens=usage_data.get("output", 0),
            ) if usage_data else None
            blocks = [_dict_to_block(json.loads(p["data"])) for p in md["parts"]]
            session.messages.append(ConversationMessage(
                role=md["role"],
                blocks=blocks,
                usage=usage,
                id=md["id"],
                time_created=md.get("time_created", 0),
            ))

        # Set title from DB
        session.title = row.title

        # Mark all loaded messages as already persisted
        session._last_persisted_count = len(session.messages)

        # on_session_load hook (fire-and-forget)
        if self._hook_pipeline:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._hook_pipeline.dispatch(
                    "on_session_load",
                    type("Ctx", (), {
                        "session_id": session_id,
                        "extra": {"message_count": len(session.messages)},
                    })(),
                ))
            except RuntimeError:
                pass

        return session

    def save(self, session: Session) -> None:
        """Full persist: upsert session metadata + all messages."""
        # on_session_save hook (fire synchronously since hooks may be async)
        if self._hook_pipeline:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._hook_pipeline.dispatch(
                    "on_session_save",
                    type("Ctx", (), {
                        "session_id": session.session_id,
                        "extra": {"message_count": len(session.messages)},
                    })(),
                ))
            except RuntimeError:
                pass  # No event loop, skip hook

        # Upsert session row
        srow = SessionRow(
            id=session.session_id,
            model=session.model,
            mode=session.mode,
            message_count=len(session.messages),
            compaction_count=session.compaction.count if session.compaction else 0,
            time_created=session.created_at_ms,
            time_updated=_now_ms(),
        )
        existing = self._db.get_session(session.session_id)
        if existing:
            self._db.update_session(
                session.session_id,
                model=srow.model,
                mode=srow.mode,
                message_count=srow.message_count,
                compaction_count=srow.compaction_count,
                time_updated=srow.time_updated,
            )
        else:
            self._db.insert_session(srow)

        # Incremental message save — only persist messages not yet in DB
        new_messages = session.messages[session._last_persisted_count:]
        if new_messages:
            self._insert_messages(session.session_id, new_messages)
            session._last_persisted_count = len(session.messages)

    def append_message(self, session_id: str, message: ConversationMessage,
                        *, mode: str | None = None, title: str | None = None,
                        project_id: str | None = None,
                        character_name: str | None = None) -> None:
        """Incremental append — one message, no full rewrite.

        Updates session's message_count and time_updated along the way.
        If the session row doesn't exist yet, ``mode``, ``title``,
        ``project_id`` and ``character_name`` are written in the initial INSERT.
        """
        # Ensure session row exists (it may have been created in-memory
        # only, deferred from create() to avoid empty-session clutter).
        if self._db.get_session(session_id) is None:
            self._db.insert_session(SessionRow(
                id=session_id,
                project_id=project_id or "",
                mode=mode or "immersive",
                title=title,
                character_name=character_name or "",
                time_created=_now_ms(),
                time_updated=_now_ms(),
            ))
        mid = _new_id()
        usage_json = None
        if message.usage:
            usage_json = json.dumps({
                "input": message.usage.input_tokens,
                "output": message.usage.output_tokens,
            })
        self._db.insert_message(MessageRow(
            id=mid, session_id=session_id,
            role=message.role, usage=usage_json,
            time_created=_now_ms(),
        ))
        for block in message.blocks:
            block_data = _block_to_dict(block)
            self._db.insert_part(PartRow(
                id=_new_id(), message_id=mid,
                session_id=session_id,
                type=block.type,
                data=json.dumps(block_data, ensure_ascii=False),
                time_created=_now_ms(),
            ))
        # Update counts
        self._db.update_session(
            session_id,
            message_count=self._db.get_message_count(session_id),
            time_updated=_now_ms(),
        )

    def update_title(self, session_id: str, title: str) -> None:
        """Derive and persist session title from first user message."""
        self._db.update_session(session_id, title=title[:200])

    def set_mode(self, session_id: str, mode: str) -> None:
        """Persist mode change."""
        self._db.update_session(session_id, mode=mode)

    def prune_content(self, session_id: str, max_tool_output_chars: int = 5000) -> int:
        """Truncate large tool outputs in a session. Returns count of parts modified."""
        return self._db.prune_large_parts(session_id, max_chars=max_tool_output_chars)

    def archive(self, session_id: str, *,
                prune_tool_outputs: bool = True,
                max_tool_output_chars: int = 5000) -> None:
        """Archive a session: optionally prune large tool outputs, then mark archived."""
        if prune_tool_outputs:
            n = self.prune_content(session_id, max_tool_output_chars=max_tool_output_chars)
            if n > 0:
                import logging
                logging.getLogger(__name__).debug(
                    "Pruned %d large parts in session %s", n, session_id[:12],
                )
        self._db.archive_session(session_id)

    def delete(self, session_id: str) -> None:
        """Delete session and all related messages/parts (cascade)."""
        self._db.delete_session(session_id)

    # ── Fork ───────────────────────────────────────────────────────────────

    def fork(self, parent_session_id: str, *,
             branch_name: str | None = None,
             up_to_message_id: str | None = None,
             exclusive: bool = False,
             fork_point_index: int | None = None) -> Session | None:
        """Create a new session that deep-copies parent's messages.

        If up_to_message_id is given, only copy messages up to that message.
        When exclusive=False (default), the message is included; when
        exclusive=True, it is excluded (copy strictly before it). The latter
        is used by the "重新输入"/"重新回复" flow so the new session ends at
        the previous agent message and the frontend re-sends the user content.

        fork_point_index is stored on the child row so the frontend can render
        a branch switcher at that message position.

        Original session is preserved unchanged. Returns new Session.
        """
        from session import Session as SessionModel, SessionFork
        parent = self.load_messages(parent_session_id)
        if not parent:
            return None

        child = SessionModel()
        child.model = parent.model
        child.mode = parent.mode
        child.title = parent.title  # inherit title (fixes "无标题" bug)
        child.fork = SessionFork(parent_session_id=parent_session_id, branch_name=branch_name)

        # Determine slice of messages to copy (fork-from-point support)
        messages_to_copy = parent.messages
        if up_to_message_id:
            for i, msg in enumerate(parent.messages):
                if getattr(msg, "id", None) == up_to_message_id:
                    end = i if exclusive else i + 1
                    messages_to_copy = parent.messages[:end]
                    break

        for msg in messages_to_copy:
            child.messages.append(msg)

        child.touch()

        # Inherit project_id / character_name from parent so forked sessions
        # appear in the correct book/character tab.
        parent_row = self._db.get_session(parent_session_id)

        # Persist fork
        srow = SessionRow(
            id=child.session_id,
            project_id=parent_row.project_id if parent_row else "",
            character_name=parent_row.character_name if parent_row else "",
            model=child.model,
            mode=child.mode,
            message_count=len(child.messages),
            parent_id=parent_session_id,
            title=child.title,
            time_created=child.created_at_ms,
            time_updated=child.updated_at_ms,
            fork_point_index=fork_point_index,
        )
        self._db.insert_session(srow)
        self._insert_messages(child.session_id, child.messages)
        return child

    def get_branches(self, session_id: str) -> dict:
        """Return branch info for the frontend switcher.

        {current: {id, title}, parent: {id, title, fork_point_index}|None,
         children: [{id, title, fork_point_index}, ...]}
        """
        cur_row = self._db.get_session(session_id)
        if not cur_row:
            return {"current": None, "parent": None, "children": []}

        current = {"id": cur_row.id, "title": cur_row.title or "(无标题)"}

        parent = None
        if cur_row.parent_id:
            prow = self._db.get_session(cur_row.parent_id)
            if prow:
                parent = {
                    "id": prow.id,
                    "title": prow.title or "(无标题)",
                    "fork_point_index": cur_row.fork_point_index,
                }

        children = []
        for crow in self._db.list_children(session_id):
            children.append({
                "id": crow.id,
                "title": crow.title or "(无标题)",
                "fork_point_index": crow.fork_point_index,
            })

        return {"current": current, "parent": parent, "children": children}


    # ── Internal helpers ───────────────────────────────────────────────────

    def _insert_messages(self, session_id: str, messages: list[ConversationMessage]) -> None:
        """Bulk-insert messages and their parts."""
        for msg in messages:
            mid = _new_id()
            usage_json = None
            if msg.usage:
                usage_json = json.dumps({
                    "input": msg.usage.input_tokens,
                    "output": msg.usage.output_tokens,
                })
            self._db.insert_message(MessageRow(
                id=mid, session_id=session_id,
                role=msg.role, usage=usage_json,
                time_created=_now_ms(),
            ))
            for block in msg.blocks:
                block_data = _block_to_dict(block)
                self._db.insert_part(PartRow(
                    id=_new_id(), message_id=mid,
                    session_id=session_id,
                    type=block.type,
                    data=json.dumps(block_data, ensure_ascii=False),
                    time_created=_now_ms(),
                ))

    @staticmethod
    def _row_to_info(row: SessionRow) -> SessionInfo:
        return SessionInfo(
            id=row.id,
            project_id=row.project_id,
            title=row.title,
            model=row.model,
            mode=row.mode,
            message_count=row.message_count,
            time_created=row.time_created,
            time_updated=row.time_updated,
            parent_id=row.parent_id,
            character_name=row.character_name or "",
            fork_point_index=row.fork_point_index,
        )
