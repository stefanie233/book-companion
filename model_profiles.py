"""Model profile store — context windows for console 用量分析.

A ``model_profiles.json`` under ``data/config/`` maps model names to
{context_window} plus the tokenizer config used for token counting.
Mirrors provider_store.py's storage discipline: tmp + os.replace atomic
writes, corrupt files quarantined to .bak, defaults on anything missing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STORE_VERSION = 2

# 厂商分词器预设：key 为逗号分隔的匹配别名（子串匹配、大小写不敏感），
# 这样 ZHIPU/GLM-5.3-Flash、vanchin/deepseek-v4-pro 这类带前缀的模型名也能命中。
# 默认路径为空 —— 不随代码分发本机绝对路径。真实分词器路径由控制台
# 「用量分析」配置,落盘在 data/config/model_profiles.json（gitignored,
# 用户本机为 D:\tokenizers\...）。无配置时 resolve_tokenizer() 返回 ""，
# 走 chars//4 估算兜底。
_DEFAULT_VENDORS: dict[str, str] = {}

# 出厂默认上下文窗口。旗舰模型普遍 1M 起步（128K 是上一代口径），兜底按 1M 算，
# 免得圆环把 1M 窗口的模型按 128K 报成 800% 用量。单模型可在「用量分析」页或
# data/config/model_profiles.json 里覆写真实窗口。
DEFAULT_CONTEXT_WINDOW = 1_048_576


def _default_profiles() -> dict:
    return {
        "version": STORE_VERSION,
        "tokenizer": {
            "path": "",
            "enabled": True,
        },
        "vendors": dict(_DEFAULT_VENDORS),
        "models": {
            "glm-5.3-flash": {
                "context_window": 1048576,
                "note": "预置：窗口 1M",
            },
            "*": {"context_window": DEFAULT_CONTEXT_WINDOW},
        },
    }


@dataclass
class ModelProfileStore:
    """JSON-backed model profile registry (context windows + tokenizer)."""

    path: Path
    _data: dict = field(default_factory=dict, repr=False, init=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return _default_profiles()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("profiles root must be an object")
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            backup = self.path.with_name(self.path.name + ".bak")
            logger.warning(
                "model_profiles.json unreadable (%s); quarantined to %s",
                exc, backup.name,
            )
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return _default_profiles()

        base = _default_profiles()
        tok = data.get("tokenizer")
        if isinstance(tok, dict):
            base["tokenizer"].update(tok)
        vendors = data.get("vendors")
        if isinstance(vendors, dict) and vendors:
            base["vendors"] = {str(k): str(v) for k, v in vendors.items()}
        models = data.get("models")
        if isinstance(models, dict) and models:
            base["models"] = models
        return base

    # ── Access & persistence ─────────────────────────────────────────────

    def get_profiles(self) -> dict:
        """Full profile snapshot (safe to hand to the API layer)."""
        return {
            "version": self._data.get("version", STORE_VERSION),
            "tokenizer": dict(self._data["tokenizer"]),
            "vendors": dict(self._data.get("vendors") or {}),
            "models": {k: dict(v) for k, v in self._data["models"].items()},
        }

    def save_profiles(self, models: dict, tokenizer: dict,
                      vendors: dict | None = None) -> None:
        """Validate + atomically replace the whole store.

        Raises ValueError on any invalid entry (handler turns it into 400).
        """
        if not isinstance(models, dict) or not models:
            raise ValueError("models 不能为空")
        cleaned: dict[str, dict] = {}
        for name, prof in models.items():
            key = str(name or "").strip()
            if not key:
                raise ValueError("模型名不能为空")
            if not isinstance(prof, dict):
                raise ValueError(f"模型 {key} 的档案必须是对象")
            window = prof.get("context_window", DEFAULT_CONTEXT_WINDOW)
            if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                raise ValueError(f"模型 {key} 的 context_window 必须是正整数")
            entry = {"context_window": window}
            note = str(prof.get("note") or "").strip()
            if note:
                entry["note"] = note
            override = str(prof.get("tokenizer_path") or "").strip()
            if override:
                entry["tokenizer_path"] = override
            cleaned[key] = entry

        tok_in = tokenizer if isinstance(tokenizer, dict) else {}
        tok = {
            "path": str(tok_in.get("path") or "").strip(),
            "enabled": bool(tok_in.get("enabled", True)),
        }

        vendors_clean: dict[str, str] = {}
        for alias, path in (vendors or {}).items():
            a = str(alias or "").strip()
            if not a:
                raise ValueError("厂商匹配词不能为空")
            vendors_clean[a] = str(path or "").strip()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STORE_VERSION, "tokenizer": tok, "models": cleaned}
        if vendors_clean:  # 空表不落盘，保住默认预设（与 models 同规矩）
            payload["vendors"] = vendors_clean
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self.path)
        self._data = payload

    def resolve(self, model: str) -> dict:
        """Profile for a model: exact match → '*' fallback → sane defaults."""
        models = self._data["models"]
        prof = models.get(model) or models.get("*")
        if prof is None:
            prof = {"context_window": DEFAULT_CONTEXT_WINDOW}
        return dict(prof)

    def _resolve_to_project(self, p: str) -> str:
        """相对路径锚到项目根（self.path 是 <root>/data/config/model_profiles.json，
        上溯三级即项目根），这样 `data/tokenizers/...` 不依赖运行时 CWD。
        绝对路径原样返回。"""
        if not p:
            return p
        pth = Path(p)
        if pth.is_absolute():
            return p  # 原样透传，避免 Path 规范化改掉分隔符（/→\）
        pth = self.path.resolve().parents[2] / pth
        return str(pth)

    def resolve_tokenizer(self, model: str) -> str:
        """分词器路径：模型显式覆盖 → 厂商别名匹配 → 全局兜底 → ""。

        ``tokenizer.enabled=False`` 是总开关（全禁用，回退 chars//4 估算）。
        返回的路径锚到项目根（相对 → 绝对），可直接交给 Tokenizer.from_file。
        """
        if not self._data["tokenizer"].get("enabled", True):
            return ""

        override = str(self.resolve(model).get("tokenizer_path") or "").strip()
        if override:
            return self._resolve_to_project(override)

        name = (model or "").lower()
        if name:
            for alias_key, path in (self._data.get("vendors") or {}).items():
                for alias in str(alias_key).split(","):
                    a = alias.strip().lower()
                    if a and a in name:
                        return self._resolve_to_project(str(path or "").strip())

        return self._resolve_to_project(
            str(self._data["tokenizer"].get("path") or "").strip())


# ── Shared store (lazy, project-relative path like usage_log) ────────────

_store: ModelProfileStore | None = None


def get_profiles_store(config: object = None) -> ModelProfileStore:
    """Shared store instance; ``config.model_profiles_path`` wins when a
    Config is available (tests point project_dir elsewhere)."""
    global _store
    path = None
    if config is not None and hasattr(config, "model_profiles_path"):
        path = config.model_profiles_path
    if _store is None or (path is not None and _store.path != path):
        _store = ModelProfileStore(path or (
            Path(__file__).parent / "data" / "config" / "model_profiles.json"
        ))
    return _store


def init_for_tests(path: Path) -> None:
    """Rebind the shared store (scripts/test_model_profiles.py)."""
    global _store
    _store = ModelProfileStore(Path(path))


