"""Book reader tools (moved from tools.py)."""
from __future__ import annotations

import json
from pathlib import Path
from permissions import PermissionMode
from tools.registry import ToolRegistry, ToolSpec, ToolExposure


def _tool_book_info(path: str) -> str:
    """Get book metadata and table of contents."""
    try:
        from file_readers import open_book
        reader = open_book(path)
        try:
            return reader.get_info()
        finally:
            if hasattr(reader, 'close'):
                reader.close()
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": f"Failed to read book: {exc}"})


def _tool_read_book(path: str, chapter_index: int = 0, max_chars: int = 8000) -> str:
    """Read a chapter from a book."""
    try:
        from file_readers import open_book
        reader = open_book(path)
        try:
            return reader.read_chapter(index=chapter_index, max_chars=max_chars)
        finally:
            if hasattr(reader, 'close'):
                reader.close()
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": f"Failed to read book: {exc}"})


def _tool_list_books(directory: str = "") -> str:
    """Scan a directory for EPUB/MOBI files."""
    scan_path = Path(directory) if directory else Path.cwd()
    if not scan_path.exists():
        return json.dumps({"error": f"Directory not found: {directory}"})

    epubs = sorted(scan_path.rglob("*.epub"))
    mobis = sorted(scan_path.rglob("*.mobi"))
    all_books = sorted(epubs + mobis, key=lambda p: p.name.lower())

    if not all_books:
        return f"(no EPUB/MOBI files found in {scan_path})"

    lines = [f"📚 Found {len(all_books)} books in {scan_path}", ""]
    for i, book in enumerate(all_books, 1):
        ext = book.suffix.lstrip(".").upper()
        size = book.stat().st_size
        size_str = f"{size / 1024:.0f} KB" if size < 1024**2 else f"{size / 1024**2:.1f} MB"
        lines.append(f"  {i:3d}. [{ext}] {book.name}  ({size_str})")
        # Show relative dir if nested
        rel = book.parent.relative_to(scan_path)
        if str(rel) != ".":
            lines.append(f"       📁 {rel}")
    return "\n".join(lines)


def _tool_book_search(path: str, query: str, max_results: int = 10) -> str:
    """Search inside a book for a keyword or phrase."""
    try:
        from file_readers import open_book
        reader = open_book(path)
        try:
            return reader.search(query, max_results=max_results)
        finally:
            if hasattr(reader, "close"):
                reader.close()
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": f"Failed to search book: {exc}"})


def _register_book_tools(registry: ToolRegistry) -> None:
    """Register book-related tools."""
    registry.register(ToolSpec(
        name="book_info",
        category="book",
        toolset="file_reading",
        # 只对「尚未入库的文件」有用（参数是磁盘路径）；读书回合里模型
        # 拿不到路径，调用必失败。旧轨迹里 book_search 就是这样空转一次。
        exposure=ToolExposure.DEFERRED,
        description=(
            "Get metadata and table of contents for an EPUB or MOBI file. "
            "Returns title, author, and full chapter list. "
            "Strongly prefer import_book + library_read for actual reading — "
            "library reads use pre-chunked ~800-char segments which are easier "
            "for the model to digest. Use this only to inspect unimported files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub or .mobi file",
                },
            },
            "required": ["path"],
        },
        execute=_tool_book_info,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="read_book",
        category="book",
        toolset="file_reading",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Read content from a book file (EPUB/MOBI) chapter by chapter. "
            "Specify chapter_index (0-based) to read a specific chapter. "
            "Use max_chars to limit output length (default 8000, 0 = unlimited). "
            "Content beyond max_chars is truncated. "
            "NOTE: prefer library_read for books already imported — it reads "
            "pre-chunked ~800-char segments that are easier for the model to "
            "digest. Use this only for books not yet in the library."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub or .mobi file",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index to read (default: 0)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default: 8000, 0 = unlimited)",
                },
            },
            "required": ["path"],
        },
        execute=_tool_read_book,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="list_books",
        category="book",
        toolset="file_reading",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Scan a directory for EPUB and MOBI book files. "
            "Returns a numbered list of books with file sizes. "
            "If no directory is specified, scans the current working directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory to scan (default: current directory)",
                },
            },
        },
        execute=_tool_list_books,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="book_search",
        category="book",
        toolset="file_reading",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Full-text search inside an EPUB book. "
            "Search for keywords, character names, or phrases across all chapters. "
            "Returns excerpted passages with chapter context. "
            "Use this to find where a character or topic appears, "
            "or to locate a specific passage the reader is referencing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub file",
                },
                "query": {
                    "type": "string",
                    "description": "Search keyword or phrase",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 10)",
                },
            },
            "required": ["path", "query"],
        },
        execute=_tool_book_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    return registry
