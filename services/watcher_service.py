"""Folder watcher: auto-import EPUB/MOBI files dropped into a watch directory.

Uses watchdog for filesystem events.  Deduplicates via MD5 content hash
(shared with library_service.import_book).  Runs in a background thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


@dataclass
class WatchEvent:
    """A single watcher action — import, skip, or error."""
    timestamp: int = 0                # time in ms
    type: str = ""                    # "imported" | "skipped" | "error"
    filename: str = ""
    title: str = ""
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "time": self.timestamp,
            "type": self.type,
            "filename": self.filename,
            "title": self.title,
            "message": self.message,
        }


class WatchService:
    """Monitor a folder.  On new .epub/.mobi files, auto-import via
    :func:`library_service.import_book`.  Dedup is automatic (MD5 hash).
    """

    # File suffixes we care about
    WATCHED_SUFFIXES = {".epub", ".mobi"}

    def __init__(self, db, watch_dir: str | Path):
        self._db = db
        self._watch_dir = Path(watch_dir)
        self._observer: Observer | None = None
        self._events: list[WatchEvent] = []   # rolling log, newest last

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Create watch dir, scan existing files, start watchdog observer."""
        self._watch_dir.mkdir(parents=True, exist_ok=True)

        # Scan existing files on startup (idempotent)
        self._scan_existing()

        # Start watching for new files
        self._observer = Observer()
        handler = _ImportHandler(self)
        self._observer.schedule(handler, str(self._watch_dir), recursive=False)
        self._observer.start()

    def stop(self) -> None:
        """Shut down the observer gracefully."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    # ── Status ───────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current watcher state for the API / frontend."""
        return {
            "watching": self._observer is not None and self._observer.is_alive(),
            "watch_dir": str(self._watch_dir.resolve()),
            "total_imported": sum(1 for e in self._events if e.type == "imported"),
            "total_skipped": sum(1 for e in self._events if e.type == "skipped"),
            "recent_events": [e.as_dict() for e in self._events[-20:]],
        }

    # ── Scanning ─────────────────────────────────────────────────────────

    def _scan_existing(self) -> int:
        """Scan for .epub/.mobi already in the watch dir.  Returns count."""
        count = 0
        for f in sorted(self._watch_dir.iterdir()):
            if f.suffix.lower() in self.WATCHED_SUFFIXES:
                self._try_import(f)
                count += 1
        return count

    # ── Import logic ─────────────────────────────────────────────────────

    def _try_import(self, filepath: Path) -> None:
        """Attempt import with dedup.  Append a WatchEvent to the log."""
        from services.library_service import import_book, _hash_file

        filename = filepath.name

        # Compute hash for dedup check (same hash function as import_book)
        try:
            book_id = _hash_file(filepath)
        except Exception as exc:
            self._add_event("error", filename, message=f"无法读取文件: {exc}")
            return

        # Dedup: already imported?
        existing = self._db.get_book(book_id)
        if existing is not None:
            self._add_event(
                "skipped", filename,
                title=existing.title,
                message=f"已入库：跳过",
            )
            return

        # Import
        try:
            book, is_new = import_book(self._db, str(filepath))
            if is_new:
                self._add_event(
                    "imported", filename,
                    title=book.title,
                    message=f"✅ 已导入《{book.title}》（{book.chapter_count}章）",
                )
            else:
                self._add_event(
                    "skipped", filename,
                    title=book.title,
                    message=f"已入库：跳过",
                )
        except Exception as exc:
            self._add_event("error", filename, message=f"导入失败: {exc}")

    def _add_event(self, type: str, filename: str, *,
                   title: str = "", message: str = "") -> None:
        e = WatchEvent(
            timestamp=int(time.time() * 1000),
            type=type,
            filename=filename,
            title=title,
            message=message,
        )
        self._events.append(e)
        # Keep the log bounded
        if len(self._events) > 50:
            self._events = self._events[-50:]


# ── Watchdog handler ─────────────────────────────────────────────────────


class _ImportHandler(FileSystemEventHandler):
    """Route watchdog events back to WatchService."""

    def __init__(self, service: WatchService):
        super().__init__()
        self._service = service
        self._pending: set[str] = set()  # paths being processed

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() not in WatchService.WATCHED_SUFFIXES:
            return
        # Debounce: ignore if already processing this file
        if event.src_path in self._pending:
            return
        self._pending.add(event.src_path)
        try:
            # Wait for the file to finish copying
            if _wait_stable(path):
                self._service._try_import(path)
        finally:
            self._pending.discard(event.src_path)


# ── File-stability helper ────────────────────────────────────────────────


def _wait_stable(filepath: Path, timeout: float = 10.0,
                 interval: float = 1.0, stable_checks: int = 3) -> bool:
    """Wait until *filepath* size stays constant for *stable_checks* polls.

    Returns True once the file is stable, False on timeout.
    """
    deadline = time.time() + timeout
    unchanged = 0
    last_size = -1
    while time.time() < deadline:
        try:
            current_size = filepath.stat().st_size
        except OSError:
            time.sleep(interval)
            continue
        if current_size == last_size:
            unchanged += 1
            if unchanged >= stable_checks:
                return True
        else:
            unchanged = 0
            last_size = current_size
        time.sleep(interval)
    # Timeout — check one last time
    return unchanged >= stable_checks
