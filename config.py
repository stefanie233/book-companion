"""Configuration loading from .env and settings.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Typed configuration loaded from environment and settings file."""
    api_key: str = ""
    api_base: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    refinement_model: str = ""  # Model for refinement pipeline (defaults to model if empty)
    search_api_key: str = ""
    # 原生联网搜索的 Anthropic 兼容端点（如 https://api.deepseek.com/anthropic）。
    # 留空 = 按 api_base 自动推导（host 是 *.deepseek.com 时）。
    native_search_base: str = ""
    # Embedding (optional — if unset, keyword retrieval is used)
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_base_url: str = ""
    # TTS (MiMo) - defaults to api_key/api_base if unset (same key drives LLM + TTS)
    tts_api_key: str = ""
    tts_api_base: str = ""
    # Console TTS slot model override; empty = tools.py picks model from voice kind
    tts_model: str = ""
    # Console ASR slot — post-by /api/console via apply_to_config; also .env fallback
    asr_api_key: str = ""
    asr_api_base: str = ""
    asr_model: str = ""
    # 书源卸货区默认路径（为空 = 未配置，前端引导输入）。.env 的
    # BOOK_COMPANION_SCAN_FOLDER 优先于 settings.json 的 scan_folder。
    scan_folder: str = ""
    project_dir: Path = field(default_factory=Path.cwd)
    max_iterations: int = 50
    compact_preserve_recent: int = 4
    compact_max_tokens: int = 10_000
    auto_compact_threshold: int = 100_000
    # 图谱链路单次调用的输出预算（总 token：推理 + 正文）。推理模型光思考就
    # 要 15k+，给小了会「HTTP 200 但 content 为空」。0 = 用 bookgraph 默认值。
    graph_max_tokens: int = 64_000
    # Context meter alert line (注意力衰减早于窗口上限; 前端圆环刻度)
    context_alert_ratio: float = 0.35
    # 0 = unlimited (archive Phase 2 consolidation keeps size in check)
    memory_char_limit: int = 0
    user_memory_char_limit: int = 0
    # Memory subsystem file logging (memory_pipeline/wingman/etc -> data/logs/memory.log)
    memory_log_enabled: bool = True
    memory_log_level: str = "INFO"  # DEBUG for pipeline internals

    @property
    def memory_dir(self) -> Path:
        return self.project_dir / "data" / "memory"

    @property
    def sessions_dir(self) -> Path:
        return self.project_dir / "data" / "sessions"

    @property
    def soul_path(self) -> Path:
        return self.project_dir / "SOUL.md"

    @property
    def settings_path(self) -> Path:
        return self.project_dir / "settings.json"

    @property
    def providers_path(self) -> Path:
        """Console provider/slot store (data/config/providers.json)."""
        return self.project_dir / "data" / "config" / "providers.json"

    @property
    def model_profiles_path(self) -> Path:
        """Console model profiles: pricing + context windows (用量分析)."""
        return self.project_dir / "data" / "config" / "model_profiles.json"

    @property
    def skills_dir(self) -> Path:
        return self.project_dir / "skills"

    @property
    def books_db_path(self) -> Path:
        return self.project_dir / "data" / "books.db"

    @property
    def sessions_db_path(self) -> Path:
        return self.project_dir / "data" / "sessions.db"

    @property
    def writing_db_path(self) -> Path:
        """WriteGraph 的写作状态库（作品正典图另存 graphs/graph_wk_*.db）。"""
        return self.project_dir / "data" / "writing.db"

    @property
    def graphs_dir(self) -> Path:
        return self.project_dir / "data" / "graphs"

    def graph_db_path(self, stable_book_id: str) -> Path:
        """Return the graph database path for a stable_book_id."""
        return self.graphs_dir / f"graph_{stable_book_id}.db"

    @property
    def db_path(self) -> Path:
        """Deprecated. Use books_db_path or sessions_db_path instead."""
        import warnings
        warnings.warn("config.db_path is deprecated", DeprecationWarning, stacklevel=2)
        return self.project_dir / "data" / "book-companion.db"


def _find_dotenv(project_dir: Path) -> Path | None:
    """Find .env file, checking project dir then cwd."""
    candidates = [project_dir / ".env", Path.cwd() / ".env"]
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_dotenv(env_path: Path) -> dict[str, str]:
    """Minimal .env parser — no dependency on python-dotenv."""
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        result[key] = value
    return result


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(project_dir: Path | None = None) -> Config:
    """Load configuration from .env and settings.json."""
    project_dir = (project_dir or Path.cwd()).resolve()

    # Load .env
    env_path = _find_dotenv(project_dir)
    env = _load_dotenv(env_path) if env_path else {}

    # Load settings.json
    settings = _load_settings(project_dir / "settings.json")

    # Merge: environment takes precedence
    return Config(
        api_key=env.get("BOOK_COMPANION_API_KEY", env.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))),
        api_base=env.get("BOOK_COMPANION_API_BASE", env.get("OPENAI_API_BASE", "https://api.openai.com/v1")),
        model=env.get("BOOK_COMPANION_MODEL", settings.get("model", "gpt-4o")),
        refinement_model=env.get("REFINEMENT_MODEL", settings.get("refinement_model", "")),
        search_api_key=env.get("BOOK_COMPANION_SEARCH_API_KEY", ""),
        native_search_base=env.get("BOOK_COMPANION_NATIVE_SEARCH_BASE", ""),
        # Embedding (optional — activates semantic search when set)
        embedding_api_key=env.get("EMBEDDING_API_KEY", ""),
        embedding_model=env.get("EMBEDDING_MODEL", ""),
        embedding_base_url=env.get("EMBEDDING_BASE_URL", ""),
        tts_api_key=env.get("MIMO_API_KEY", ""),
        tts_api_base=env.get("MIMO_API_BASE", ""),
        project_dir=project_dir,
        max_iterations=settings.get("max_iterations", 50),
        compact_preserve_recent=settings.get("compact", {}).get("preserve_recent_messages", 4),
        compact_max_tokens=settings.get("compact", {}).get("max_estimated_tokens", 10_000),
        auto_compact_threshold=settings.get("compact", {}).get("auto_threshold", 100_000),
        graph_max_tokens=settings.get("graph", {}).get("max_tokens", 64_000),
        memory_char_limit=settings.get("memory", {}).get("char_limit", 0),
        user_memory_char_limit=settings.get("memory", {}).get("user_char_limit", 0),
        scan_folder=env.get("BOOK_COMPANION_SCAN_FOLDER",
                            settings.get("scan_folder", "")),
    )


def save_settings(config: Config) -> None:
    """Persist mutable settings back to settings.json."""
    settings = {
        "model": config.model,
        "max_iterations": config.max_iterations,
        "compact": {
            "preserve_recent_messages": config.compact_preserve_recent,
            "max_estimated_tokens": config.compact_max_tokens,
            "auto_threshold": config.auto_compact_threshold,
        },
        "memory": {
            "char_limit": config.memory_char_limit,
            "user_char_limit": config.user_memory_char_limit,
        },
    }
    config.settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
