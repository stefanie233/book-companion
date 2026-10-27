"""Provider/slot configuration store — the console's persistence layer.

Console-driven model switching: a **provider** is a connection profile
(base_url + api_key + cached model list), a **slot** is a consumption
point (main_llm / embedding / tts) pointing at one provider. v2: the
profile itself is kind-less — every cached model entry carries its own
kind, so one vendor connection serves llm/tts/asr models at once.
Resolution
order per slot:

    1. slot.provider_id  -> providers[id]      (console-configured, persisted)
    2. fallback          -> .env / settings.json values already in Config
    3. neither           -> Config left untouched (current behavior)

apply_to_config() writes resolved values back into the shared mutable
Config instance, so every consumer that reads config.* at request time
(OpenAIClient.stream, bookgraph, tools.tts_*) switches on the next call —
the same mechanism the /model CLI command has always used. Consumers are
not modified.

Storage discipline (lessons from the migration-add-column incident):
atomic writes via tmp + os.replace, corrupt file quarantined to .bak
with env-behavior fallback instead of a crash, and settings.json's
full-rewrite save path is deliberately NOT reused.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# ── Slot / kind vocabulary ──────────────────────────────────────────────

SLOT_MAIN_LLM = "main_llm"      # chat + all background pipelines (bookgraph, memory, enrich)
SLOT_EMBEDDING = "embedding"    # semantic retrieval (consumer pending; fields reserved)
SLOT_TTS = "tts"                # voices / tts_speak (tools.py reads tts_api_key/base)
SLOT_ASR = "asr"                # /api/asr cloud channel (unbound -> local SenseVoice)

SLOTS: tuple[str, ...] = (SLOT_MAIN_LLM, SLOT_EMBEDDING, SLOT_TTS, SLOT_ASR)
SLOT_KIND: dict[str, str] = {
    SLOT_MAIN_LLM: "llm",
    SLOT_EMBEDDING: "embedding",
    SLOT_TTS: "tts",
    SLOT_ASR: "asr",
}
KINDS: tuple[str, ...] = ("llm", "embedding", "tts", "asr")

STORE_VERSION = 3

# ── Route vocabulary: model binding per LLM call point ──────────────────
#
# Every LLM call point is bindable. ``id`` is dotted, and every prefix is a
# valid route key too — binding ``bookgraph`` covers the whole group, while
# ``bookgraph.adjudicate`` overrides just that leaf. Unbound falls back up
# the chain to the main model (``slots.main_llm``), then to .env.
#
# ``main: True`` marks an id that is not separately routable: the main chat
# model *is* the main_llm slot. Binding it separately would desync the
# context meter / session model from the model actually used.

ROUTE_GROUPS: tuple[tuple[str, str], ...] = (
    ("chat", "对话"),
    ("bookgraph", "图谱提取"),
    ("authorgraph", "作者图"),
    ("writing", "写作"),
    ("memory", "记忆"),
    ("import", "导入分析"),
    ("enrich", "元数据清洗"),
)
GROUP_LABELS: dict[str, str] = dict(ROUTE_GROUPS)

CALL_POINTS: tuple[dict, ...] = (
    # 对话
    {"id": "chat", "group": "chat", "label": "主对话", "main": True,
     "desc": "ReAct 主循环；等同于主模型槽位，不单独绑定"},
    {"id": "chat.title", "group": "chat", "label": "会话标题生成",
     "desc": "首轮结束后生成会话标题"},
    {"id": "sub_agent", "group": "chat", "label": "子 Agent",
     "desc": "Reader / 审查技能等派生代理（显式传入 client 的调用点除外）"},
    # 图谱
    {"id": "bookgraph.scout", "group": "bookgraph", "label": "Scout 摸底",
     "desc": "整本速览，定焦点"},
    {"id": "bookgraph.skeleton", "group": "bookgraph", "label": "骨架趟",
     "desc": "规范名 / 别名 / 身份 / 结构关系"},
    {"id": "bookgraph.head_scan", "group": "bookgraph", "label": "卷首扫描",
     "desc": "开篇章节的预读"},
    {"id": "bookgraph.context_setter", "group": "bookgraph", "label": "上下文设定",
     "desc": "叙事域简报与批次上下文"},
    {"id": "bookgraph.explorer", "group": "bookgraph", "label": "Explorer 逐批提取",
     "desc": "实体 / 关系 / 引文 / chunk 简述"},
    {"id": "bookgraph.adjudicate", "group": "bookgraph", "label": "裁决环",
     "desc": "断言级判决，rewrite 改证据"},
    {"id": "bookgraph.verify", "group": "bookgraph", "label": "证伪 / 机械校验",
     "desc": "提取结果的回炉校验"},
    {"id": "bookgraph.notes", "group": "bookgraph", "label": "读者笔记",
     "desc": "按篇生成的阅读笔记"},
    {"id": "bookgraph.incremental", "group": "bookgraph", "label": "增量更新",
     "desc": "新章节增量抽取"},
    # 作者图
    {"id": "authorgraph.harvest", "group": "authorgraph", "label": "作者采集",
     "desc": "从作品与生平材料提取作者层"},
    {"id": "authorgraph.enrich", "group": "authorgraph", "label": "作者丰化",
     "desc": "时间轴与生平细节补全"},
    # 写作
    {"id": "writing.draft", "group": "writing", "label": "正文生成",
     "desc": "按章合同流式写正文"},
    {"id": "writing.settle", "group": "writing", "label": "结算",
     "desc": "章节结算与状态更新"},
    {"id": "writing.review", "group": "writing", "label": "审稿",
     "desc": "结构化审稿"},
    {"id": "writing.revise", "group": "writing", "label": "改稿",
     "desc": "按审稿意见修订"},
    {"id": "writing.reference", "group": "writing", "label": "参考素材蒸馏",
     "desc": "把参考书候选压成可迁移的细节素材卡"},
    # 记忆
    {"id": "memory.l1_extract", "group": "memory", "label": "L1 抽取",
     "desc": "场景切分 + 原子记忆"},
    {"id": "memory.l1_dedup", "group": "memory", "label": "L1 去重",
     "desc": "新记忆与既有记忆的批量判重"},
    {"id": "memory.l2", "group": "memory", "label": "L2 场景聚合",
     "desc": "场景块归纳"},
    {"id": "memory.l3", "group": "memory", "label": "L3 画像生成",
     "desc": "persona.md 观察投影"},
    {"id": "memory.archive", "group": "memory", "label": "归档沉淀",
     "desc": "会话知识归档（phase1 / phase2）"},
    # 导入与清洗
    {"id": "import.analysis", "group": "import", "label": "导入分析",
     "desc": "立论 → 证伪 → 机械校验 → 回炉"},
    {"id": "enrich.metadata", "group": "enrich", "label": "元数据清洗",
     "desc": "书名 / 作者 / 国籍清洗"},
)

CALL_POINT_MAP: dict[str, dict] = {c["id"]: c for c in CALL_POINTS}
# ``chat`` is a group name too, but never a route key: the main chat model
# *is* main_llm. Binding it separately would desync the context meter and
# session.model from the model actually serving the call.
ROUTABLE_KEYS: frozenset[str] = (
    frozenset(k for k, c in CALL_POINT_MAP.items() if not c.get("main"))
    | frozenset(GROUP_LABELS)
) - {"chat"}


def route_chain(scenario: str) -> list[str]:
    """``bookgraph.explorer`` → [``bookgraph.explorer``, ``bookgraph``].

    The main-model fallback is appended by the resolver, not here.
    """
    parts = str(scenario or "").split(".")
    out: list[str] = []
    for i in range(len(parts), 0, -1):
        key = ".".join(parts[:i])
        if key in ROUTABLE_KEYS:
            out.append(key)
    return out


def guess_model_kind(model_id: str) -> str:
    """Best-effort kind from a model id — /models lists carry no kind
    metadata. Console rows stay editable, so a wrong guess is one
    dropdown click away."""
    s = str(model_id).lower()
    if "tts" in s or "speech" in s or "voice" in s:
        return "tts"
    if "asr" in s or "whisper" in s or "transcri" in s:
        return "asr"
    if "embed" in s:
        return "embedding"
    return "llm"


class ProviderInUseError(Exception):
    """Deletion refused: a slot still points at this provider."""


@dataclass
class ResolvedEndpoint:
    """One slot's effective connection after the console/.env fallback chain."""

    key: str
    base_url: str
    model: str
    source: str  # "console" | "env"
    provider_id: str = ""
    provider_name: str = ""


@dataclass
class ProviderStore:
    """JSON-backed provider registry + slot bindings, with env fallback."""

    path: Path
    _data: dict = field(default_factory=dict, repr=False, init=False)
    _loaded_stamp: int = field(default=0, repr=False, init=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._data = self._load()
        self._loaded_stamp = self._file_stamp()

    def _file_stamp(self) -> int:
        """mtime_ns of the backing file — 0 when absent (never raises).

        Nanoseconds, not seconds: the console saves then the next LLM call
        reloads — second-resolution mtime can miss that within one tick.
        """
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return 0

    def is_current(self, stamp: int) -> bool:
        return self._loaded_stamp == stamp

    # ── Encryption hooks (v2: swap for DPAPI/keyring, nothing else changes) ─

    def _encrypt(self, key: str) -> str:
        return key

    def _decrypt(self, key: str) -> str:
        return key

    # ── Persistence ──────────────────────────────────────────────────────

    @staticmethod
    def _default_data() -> dict:
        return {
            "version": STORE_VERSION,
            "providers": [],
            "slots": {
                s: {"provider_id": None, "model": "", "params": {}} for s in SLOTS
            },
            # Only *bound* routes are stored; absent key = inherit parent.
            "routes": {},
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._default_data()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            backup = self.path.with_name(self.path.name + ".bak")
            logger.warning(
                "providers.json unreadable (%s); quarantined to %s, "
                "falling back to .env behavior",
                exc,
                backup.name,
            )
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return self._default_data()

        base = self._default_data()
        base["providers"] = [p for p in data.get("providers", []) if isinstance(p, dict)]
        slots = data.get("slots", {})
        if isinstance(slots, dict):
            for s in SLOTS:
                if isinstance(slots.get(s), dict):
                    base["slots"][s].update(slots[s])
        routes = data.get("routes", {})
        if isinstance(routes, dict):
            for key, entry in routes.items():
                if key not in ROUTABLE_KEYS or not isinstance(entry, dict):
                    continue
                pid = entry.get("provider_id")
                model = str(entry.get("model") or "").strip()
                if not pid or not model:
                    continue  # an unbound route is simply absent
                base["routes"][key] = {
                    "provider_id": str(pid),
                    "model": model,
                    "params": dict(entry.get("params") or {}),
                }
        for p in base["providers"]:
            self._normalize_legacy_provider(p)
        return base

    @staticmethod
    def _normalize_legacy_provider(p: dict) -> None:
        """v1 -> v2 migration: the record-level kind moves onto each model.

        Lazy — applied at load, persisted on the next _save. String models
        inherit the old record kind; dict models keep a valid kind of their
        own. The stale record-level key is dropped.
        """
        legacy = p.get("kind") if p.get("kind") in KINDS else "llm"
        models: list[dict] = []
        for m in p.get("models") or []:
            if isinstance(m, dict):
                mid = str(m.get("id") or "").strip()
                mk = m.get("kind") if m.get("kind") in KINDS else legacy
            else:
                mid, mk = str(m).strip(), legacy
            if mid:
                models.append({"id": mid, "kind": mk})
        p["models"] = models
        p.pop("kind", None)

    def _disk_routes(self) -> dict:
        """Routes currently on disk — best-effort, {} on any read failure."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw = data.get("routes")
            return dict(raw) if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self, op: str = "") -> None:
        """Atomic write: never mutate the live file in place.

        Defence in depth for route bindings: unless this save *is* an
        explicit unbind, carry over any route key that exists on disk but
        not in memory. A stale or exotic writer can then never erase
        bindings without saying so out loud.
        """
        if op != "route_unbind":
            for key, entry in self._disk_routes().items():
                if key in ROUTABLE_KEYS and isinstance(entry, dict):
                    self._data["routes"].setdefault(key, entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self.path)
        self._loaded_stamp = self._file_stamp()
        self._audit(op)

    def _audit(self, op: str) -> None:
        """Append one line per save — the paper trail for "my bindings
        vanished" reports. Records who wrote, with which route keys, and
        the call stack that did it."""
        try:
            import traceback as _tb
            rec = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "pid": os.getpid(),
                "op": op,
                "main": "{}/{}".format(
                    self._data["slots"][SLOT_MAIN_LLM].get("provider_id"),
                    self._data["slots"][SLOT_MAIN_LLM].get("model"),
                ),
                "routes": sorted(self._data["routes"]),
                "stack": ["%s:%s" % (f.name, f.lineno)
                          for f in _tb.extract_stack()[-5:-1]],
            }
            log_path = self.path.parents[1] / "logs" / "providers.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _refresh_if_stale(self) -> None:
        """Reload before mutating when another writer touched the file.

        ``_save`` writes the *whole* in-memory snapshot. A store instance
        loaded before someone else added routes (another process, a CLI
        run, an older app instance) would therefore write its stale copy
        back and **silently erase those routes** — observed as "changing
        the main model wipes my model bindings". Reload first, then apply
        the mutation, so a save can only ever merge onto the newest file.
        """
        stamp = self._file_stamp()
        if stamp != self._loaded_stamp:
            self._data = self._load()
            self._loaded_stamp = self._file_stamp()

    # ── Key masking ──────────────────────────────────────────────────────

    @staticmethod
    def mask_key(key: str) -> str:
        if not key:
            return ""
        return f"••••{key[-4:]}" if len(key) > 8 else "••••"

    # ── Provider CRUD ────────────────────────────────────────────────────

    def list_providers(self, kind: str | None = None) -> list[dict]:
        return [
            dict(p)
            for p in self._data["providers"]
            if kind is None or p.get("kind") == kind
        ]

    def get_provider(self, provider_id: str) -> dict | None:
        for p in self._data["providers"]:
            if p.get("id") == provider_id:
                return dict(p)
        return None

    def reveal_key(self, provider_id: str) -> str:
        """Decrypted api_key — internal use only (resolve / test calls)."""
        p = self.get_provider(provider_id)
        return self._decrypt(p.get("api_key", "")) if p else ""

    def model_kind(self, provider_id: str, model_id: str) -> str | None:
        """Catalog kind of (provider, model); None when model not cached."""
        p = self.get_provider(provider_id)
        if p is None:
            return None
        for m in p.get("models") or []:
            if m.get("id") == model_id:
                return m.get("kind")
        return None

    def upsert_provider(self, data: dict) -> dict:
        """Create or update a provider. api_key == "" on edit keeps the old key.

        v2: kind-less vendor connection — models arrive as {"id", "kind"}
        entries (bare strings tolerated, kind-guessed). ``name`` is optional,
        falling back to vendor, then the base_url host.
        Returns the stored entry (encrypted key — hand to mask_key for display).
        """
        self._refresh_if_stale()
        base_url = str(data.get("base_url", "")).strip()
        if not base_url:
            raise ValueError("base_url is required")

        now = datetime.now().isoformat(timespec="seconds")
        existing: dict | None = None
        pid = str(data.get("id") or "").strip()
        if pid:
            existing = self.get_provider(pid)
            if existing is None:
                raise ValueError(f"provider not found: {pid}")
            api_key = str(data.get("api_key") or "")
            if not api_key:
                api_key = existing.get("api_key", "")  # keep old key
        else:
            pid = f"p_{secrets.token_hex(4)}"
            while self.get_provider(pid) is not None:  # paranoia
                pid = f"p_{secrets.token_hex(4)}"
            api_key = str(data.get("api_key") or "")

        # vendor: vendor brand for two-tier filtering (MiMo / 千问 / DeepSeek…).
        # Absent key on edit = keep old; explicit "" clears (snapshot falls
        # back to the first token of name).
        if "vendor" in data:
            vendor = str(data.get("vendor") or "").strip()
        else:
            vendor = str((existing or {}).get("vendor") or "").strip()
        name = str(data.get("name", "")).strip() or vendor \
            or urlsplit(base_url).netloc or base_url

        # models: absent key on edit = keep the old catalog (same semantics
        # as vendor); present key replaces it wholesale.
        if "models" in data:
            models: list[dict] = []
            for m in data.get("models") or []:
                if isinstance(m, dict):
                    mid = str(m.get("id") or "").strip()
                    mk = str(m.get("kind") or "").strip()
                else:
                    mid, mk = str(m).strip(), ""
                if not mid:
                    continue
                if mk not in KINDS:
                    mk = guess_model_kind(mid)
                models.append({"id": mid, "kind": mk})
        else:
            models = [dict(m) for m in (existing or {}).get("models") or []]

        entry = {
            "id": pid,
            "name": name,
            "vendor": vendor,
            "base_url": base_url,
            "api_key": self._encrypt(api_key),
            "models": models,
            "extra": dict(data.get("extra") or {}),
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
        }
        providers = self._data["providers"]
        for i, p in enumerate(providers):
            if p.get("id") == pid:
                providers[i] = entry
                break
        else:
            providers.append(entry)
        self._save("provider_upsert")
        return dict(entry)

    def delete_provider(self, provider_id: str) -> None:
        self._refresh_if_stale()
        for s in SLOTS:
            if self._data["slots"][s].get("provider_id") == provider_id:
                raise ProviderInUseError(
                    f"provider {provider_id} is bound to slot {s!r}; switch that slot first"
                )
        for route, entry in self._data["routes"].items():
            if entry.get("provider_id") == provider_id:
                raise ProviderInUseError(
                    f"provider {provider_id} is bound to route {route!r}; "
                    f"switch that route first"
                )
        before = len(self._data["providers"])
        self._data["providers"] = [
            p for p in self._data["providers"] if p.get("id") != provider_id
        ]
        if len(self._data["providers"]) == before:
            raise ValueError(f"provider not found: {provider_id}")
        self._save("provider_delete")

    # ── Slots ────────────────────────────────────────────────────────────

    def get_slot(self, slot: str) -> dict:
        if slot not in SLOTS:
            raise ValueError(f"unknown slot: {slot!r}")
        return dict(self._data["slots"][slot])

    def set_slot(
        self,
        slot: str,
        provider_id: str | None = None,
        model: str | None = None,
        params: dict | None = None,
    ) -> dict:
        """Bind a slot to a provider (+ optional model). Returns updated slot."""
        self._refresh_if_stale()
        if slot not in SLOTS:
            raise ValueError(f"unknown slot: {slot!r}")
        entry = self._data["slots"][slot]
        if provider_id is not None:
            if provider_id == "":
                entry["provider_id"] = None  # explicit unbind -> .env behavior
            else:
                p = self.get_provider(provider_id)
                if p is None:
                    raise ValueError(f"provider not found: {provider_id}")
                # Kind check lives on the model now. Cached models are
                # enforced; unknown (hand-typed) names pass through —
                # wrong ones surface as runtime errors by design.
                if model:
                    mk = self.model_kind(provider_id, model)
                    if mk is not None and mk != SLOT_KIND[slot]:
                        raise ValueError(
                            f"model {model!r} is kind {mk!r}; slot {slot!r} "
                            f"expects {SLOT_KIND[slot]!r}"
                        )
                entry["provider_id"] = provider_id
        if model is not None:
            entry["model"] = str(model).strip()
        if params is not None:
            entry["params"] = dict(params)
        self._save("slot_set")
        return dict(entry)

    # ── Routes (per-call-point model binding) ────────────────────────────

    def get_route(self, key: str) -> dict:
        entry = self._data["routes"].get(key)
        return dict(entry) if entry else {
            "provider_id": None, "model": "", "params": {}
        }

    def set_route(
        self,
        key: str,
        provider_id: str | None = None,
        model: str | None = None,
        params: dict | None = None,
    ) -> dict:
        """Bind a call point (or a group key) to (provider, model).

        ``provider_id == ""`` unbinds — the key disappears and resolution
        falls through to its parent, then to the main model.
        """
        self._refresh_if_stale()
        if key not in ROUTABLE_KEYS:
            raise ValueError(f"unknown route key: {key!r}")
        if provider_id is not None:
            if provider_id == "":
                self._data["routes"].pop(key, None)
                self._save("route_unbind")
                return {"provider_id": None, "model": "", "params": {}}
            p = self.get_provider(provider_id)
            if p is None:
                raise ValueError(f"provider not found: {provider_id}")
            if not model:
                raise ValueError("model is required when binding a route")
            mk = self.model_kind(provider_id, str(model).strip())
            if mk is not None and mk != "llm":
                raise ValueError(
                    f"model {model!r} is kind {mk!r}; routes expect 'llm'"
                )
            entry = self._data["routes"].setdefault(
                key, {"provider_id": provider_id, "model": "", "params": {}}
            )
            entry["provider_id"] = provider_id
        else:
            entry = self._data["routes"].get(key)
            if entry is None:
                return {"provider_id": None, "model": "", "params": {}}
        if model is not None:
            entry["model"] = str(model).strip()
        if params is not None:
            entry["params"] = dict(params)
        self._save("route_set")
        return dict(entry)

    def _bound_route(self, scenario: str) -> tuple[str, dict] | None:
        """Nearest bound key for ``scenario`` (exact → parent prefixes)."""
        for key in route_chain(scenario):
            entry = self._data["routes"].get(key)
            if entry and entry.get("provider_id"):
                return key, entry
        return None

    def route_origin(self, scenario: str) -> str:
        hit = self._bound_route(scenario)
        return hit[0] if hit else ""

    def resolve_route(self, scenario: str, config: object) -> ResolvedEndpoint:
        """Effective endpoint for a call point; unbound → main_llm → .env."""
        hit = self._bound_route(scenario)
        if hit:
            key, entry = hit
            p = self.get_provider(entry.get("provider_id", ""))
            if p is not None:
                return ResolvedEndpoint(
                    key=self._decrypt(p.get("api_key", "")),
                    base_url=p.get("base_url", ""),
                    model=str(entry.get("model", "")),
                    source="console",
                    provider_id=p.get("id", ""),
                    provider_name=p.get("name", ""),
                )
            logger.warning(
                "route %s points at missing provider %s; using main model",
                key, entry.get("provider_id"),
            )
        return self.resolve(SLOT_MAIN_LLM, config)

    def config_for(self, scenario: str, config: object):
        """A Config copy with this call point's endpoint applied.

        Returns the *original* object when nothing is bound, so unbound
        paths keep today's behavior. Always resolves from the original
        (``_route_base``) — a view resolved for a child key must not erase
        the parent group's binding.
        """
        base = getattr(config, "_route_base", config)
        hit = self._bound_route(scenario)
        if hit is None:
            return base
        _, entry = hit
        p = self.get_provider(entry.get("provider_id", ""))
        if p is None:
            return base
        view = replace(
            base,
            api_key=self._decrypt(p.get("api_key", "")),
            api_base=p.get("base_url", ""),
            model=str(entry.get("model", "")),
        )
        try:  # plain dataclass — safe; guarded for exotic wrappers
            view._route_base = base      # type: ignore[attr-defined]
            view._route_id = scenario    # type: ignore[attr-defined]
        except Exception:
            pass
        return view

    # ── Resolution & hot-apply ───────────────────────────────────────────

    def resolve(self, slot: str, config: object) -> ResolvedEndpoint:
        """Console binding first; .env values already in Config as fallback."""
        if slot not in SLOTS:
            raise ValueError(f"unknown slot: {slot!r}")
        raw = self._data["slots"][slot]
        pid = raw.get("provider_id")
        if pid:
            p = self.get_provider(pid)
            if p is not None:
                return ResolvedEndpoint(
                    key=self._decrypt(p.get("api_key", "")),
                    base_url=p.get("base_url", ""),
                    model=str(raw.get("model", "")),
                    source="console",
                    provider_id=pid,
                    provider_name=p.get("name", ""),
                )
            logger.warning("slot %s points at missing provider %s; using .env", slot, pid)

        # .env fallback — load_config already merged env/settings into config
        if slot == SLOT_MAIN_LLM:
            return ResolvedEndpoint(
                config.api_key, config.api_base, config.model, "env"
            )
        if slot == SLOT_EMBEDDING:
            return ResolvedEndpoint(
                config.embedding_api_key,
                config.embedding_base_url,
                config.embedding_model,
                "env",
            )
        if slot == SLOT_TTS:
            return ResolvedEndpoint(config.tts_api_key, config.tts_api_base, "", "env")
        # SLOT_ASR — model deliberately "" on .env fallback (mirrors tts),
        # so a console binding is the *only* thing that turns on the cloud
        # channel; empty model = local SenseVoice.
        return ResolvedEndpoint(config.asr_api_key, config.asr_api_base, "", "env")

    def apply_to_config(self, config: object) -> dict[str, str]:
        """Write console-configured slots back into the shared Config instance.

        Slots without a provider binding are left untouched, so plain .env
        users see zero behavior change. model is only written when non-empty
        (an empty model would make OpenAIClient send a blank model name).
        Returns {slot: "provider/model"} for what actually changed.
        """
        changed: dict[str, str] = {}

        r = self.resolve(SLOT_MAIN_LLM, config)
        if r.source == "console":
            config.api_key = r.key
            config.api_base = r.base_url
            if r.model:
                config.model = r.model
            changed[SLOT_MAIN_LLM] = f"{r.provider_name}/{r.model or '?'}"

        r = self.resolve(SLOT_EMBEDDING, config)
        if r.source == "console":
            config.embedding_api_key = r.key
            config.embedding_base_url = r.base_url
            if r.model:
                config.embedding_model = r.model
            changed[SLOT_EMBEDDING] = f"{r.provider_name}/{r.model or '?'}"

        r = self.resolve(SLOT_TTS, config)
        # tts_model mirrors the slot model unconditionally: console binding
        # drives the TTS call, unbind/.env resets it to "" (voice-kind
        # mapping). env resolve returns model="", so this line both applies
        # and clears — no stale override survives a rebind.
        config.tts_model = r.model
        if r.source == "console":
            config.tts_api_key = r.key
            config.tts_api_base = r.base_url
            changed[SLOT_TTS] = f"{r.provider_name}/{r.model or '默认TTS'}"

        r = self.resolve(SLOT_ASR, config)
        # asr_model mirrors unconditionally (same idle-clear trick as tts):
        # non-empty model = /api/asr goes cloud, empty = local SenseVoice.
        config.asr_model = r.model
        if r.source == "console":
            config.asr_api_key = r.key
            config.asr_api_base = r.base_url
            changed[SLOT_ASR] = f"{r.provider_name}/{r.model or '默认ASR'}"

        return changed

    # ── API snapshot ─────────────────────────────────────────────────────

    def _masked(self, p: dict) -> dict:
        out = dict(p)
        out["api_key"] = self.mask_key(self._decrypt(p.get("api_key", "")))
        # Vendor fallback for pre-vendor records: first token of the display
        # name ("MiMo 标准" -> "MiMo"). The console groups filters by this.
        if not str(out.get("vendor") or "").strip():
            name = str(out.get("name") or "").strip()
            out["vendor"] = name.split()[0] if name else ""
        return out

    def snapshot(self, config: object) -> dict:
        """Everything GET /api/console/config needs — keys always masked."""
        self._refresh_if_stale()  # another process may have written since load
        slots = {}
        for s in SLOTS:
            raw = self._data["slots"][s]
            r = self.resolve(s, config)
            slots[s] = {
                "kind": SLOT_KIND[s],
                "provider_id": raw.get("provider_id"),
                "model": raw.get("model", ""),
                "source": r.source,
                "resolved": {
                    "key_masked": self.mask_key(r.key),
                    "base_url": r.base_url,
                    "model": r.model,
                    "provider_name": r.provider_name,
                },
            }
        routes = {k: dict(v) for k, v in self._data["routes"].items()}
        callpoints: list[dict] = []
        for cp in CALL_POINTS:
            cid = cp["id"]
            exact = self._data["routes"].get(cid)
            r = self.resolve_route(cid, config)
            callpoints.append({
                "id": cid,
                "group": cp["group"],
                "label": cp["label"],
                "desc": cp.get("desc", ""),
                "main": bool(cp.get("main")),
                "bound": bool(exact),
                "origin": self.route_origin(cid),  # "" = main model
                "provider_id": (exact or {}).get("provider_id") or "",
                "model": (exact or {}).get("model") or "",
                "resolved": {
                    "key_masked": self.mask_key(r.key),
                    "base_url": r.base_url,
                    "model": r.model,
                    "provider_name": r.provider_name,
                    "source": r.source,
                },
            })
        return {
            "version": self._data.get("version", STORE_VERSION),
            "providers": [self._masked(p) for p in self._data["providers"]],
            "slots": slots,
            "routes": routes,
            "groups": [
                {"id": gid, "label": label, "bound": gid in routes,
                 "routable": gid in ROUTABLE_KEYS}
                for gid, label in ROUTE_GROUPS
            ],
            "callpoints": callpoints,
        }


# ── Process-wide access (call sites must not thread the store) ──────────
#
# The console is the only writer; everyone else only needs the current
# bindings. A per-path cache + mtime check keeps "change then next call
# picks it up" without re-reading the JSON on every LLM call.

_ACTIVE: dict[str, ProviderStore] = {}


def get_active_store(config: object) -> ProviderStore | None:
    path = getattr(config, "providers_path", None)
    if path is None:
        return None
    path = Path(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    store = _ACTIVE.get(str(path))
    if store is None or not store.is_current(stamp):
        store = ProviderStore(path)
        _ACTIVE[str(path)] = store
    return store


def config_for(config: object, scenario: str):
    """Config view for ``scenario`` — the original when nothing is bound.

    This is the function every LLM call point imports:
    ``cfg = config_for(cfg, "bookgraph.explorer")``.
    """
    if not scenario:
        return config
    store = get_active_store(config)
    if store is None:
        return config
    return store.config_for(scenario, config)


def resolve_scenario(config: object, scenario: str) -> ResolvedEndpoint | None:
    store = get_active_store(config)
    return store.resolve_route(scenario, config) if store is not None else None
