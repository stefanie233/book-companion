"""Tool registry package — built-in tools split by toolset.

The public surface mirrors the former single-file ``tools.py`` so every
``from tools import X`` call site keeps working unchanged.
"""

from __future__ import annotations

from permissions import PermissionMode
from tools.author import _deepen_store, _deepen_task, _remember_page
from tools.builtin import create_builtin_registry
from tools.registry import ToolExposure, ToolSpec, ToolRegistry

__all__ = [
    "PermissionMode",
    "ToolExposure",
    "ToolSpec",
    "ToolRegistry",
    "create_builtin_registry",
    "_deepen_store",
    "_deepen_task",
    "_remember_page",
]
