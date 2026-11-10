"""Configuration loading from .env and settings.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_user_root() -> Path:
    from runtime_paths import user_root
    return user_root()


@dataclass
class Config:
    """Typed configuration loaded from environment and settings file."""
    api_key: str = ""
    api_base: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    # Agent 对外称呼（主题页可改）。只影响界面与提示词里的“你叫 X”，
    # 人格与文风仍由 SOUL.md 决定。
    agent_name: str = "留白"
    # 用户自己的称呼（设置页可改）。空 = 不注入提示词；头像单独存在 data/user。
    user_name: str = ""
    # 用户性别（设置页可改）："" 未设置 / "male" / "female"。
    # 只决定第三人称用「他 / 她 / ta」，不影响人格与文风。
    user_gender: str = ""
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
    # 用户根（可写）：data/、SOUL.md、settings.json、.env。源码运行时是仓库根，
    # 冻结后是 %LOCALAPPDATA%\LiuBai（或 BC_HOME / 便携目录）。
    project_dir: Path = field(default_factory=_default_user_root)
    # 程序根（只读）：skills/ prompts/ 等随安装包替换的资源。源码运行时 = 仓库根。
    resource_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent)
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
        # 技能是随版本发布的资源（含 Python 模块），不是用户数据。
        return self.resource_dir / "skills"

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
    """Find .env file: 用户根 → cwd → 程序根（源码运行与安装后都覆盖）。"""
    candidates = [
        project_dir / ".env",
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    seen: set[Path] = set()
    for p in candidates:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
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
    """Load configuration from .env and settings.json.

    ``project_dir`` 是**用户根**（可写数据）。不传时取 ``runtime_paths.user_root()``：
    源码运行 = 仓库根（与旧的 cwd 行为一致，除非从别的目录启动）；冻结后 =
    ``%LOCALAPPDATA%\\LiuBai``。程序根另行解析，技能等资源不受用户根影响。
    测试仍可显式传临时目录。
    """
    from runtime_paths import code_root, user_root

    project_dir = (project_dir or user_root()).resolve()

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
        agent_name=str(settings.get("agent_name") or "留白").strip(),
        user_name=str(settings.get("user_name") or "").strip(),
        user_gender=str(settings.get("user_gender") or "").strip().lower(),
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
        resource_dir=code_root(),
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


def _read_settings_dict(config: Config) -> dict:
    """Read settings.json, preserving fields Config doesn't model."""
    if not config.settings_path.exists():
        return {}
    try:
        loaded = json.loads(config.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_settings_dict(config: Config, settings: dict) -> None:
    tmp = config.settings_path.with_name(config.settings_path.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.settings_path)


def save_settings(config: Config) -> None:
    """Persist mutable settings back to settings.json.

    在原文件上增量更新，不整份重写 —— settings.json 里还有 scan_folder /
    refinement_model / graph 等不归 Config 管理的字段，整份重写会静默丢掉。
    """
    settings = _read_settings_dict(config)
    settings.update({
        "model": config.model,
        "agent_name": config.agent_name,
        "user_name": config.user_name,
        "user_gender": config.user_gender,
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
    })
    _write_settings_dict(config, settings)


def update_settings(config: Config, /, **updates) -> None:
    """Patch only the named keys — never re-persist environment-derived values.

    ``save_settings`` writes ``config.model``, which may have come from
    ``BOOK_COMPANION_MODEL`` rather than settings.json. A UI action that only
    changes one field (e.g. the agent name) must not silently freeze that env
    override into the file.
    """
    settings = _read_settings_dict(config)
    settings.update(updates)
    _write_settings_dict(config, settings)
