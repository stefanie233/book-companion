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
from dataclasses import dataclass, field
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

STORE_VERSION = 2


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

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._data = self._load()

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

    def _save(self) -> None:
        """Atomic write: never mutate the live file in place."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self.path)

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
        self._save()
        return dict(entry)

    def delete_provider(self, provider_id: str) -> None:
        for s in SLOTS:
            if self._data["slots"][s].get("provider_id") == provider_id:
                raise ProviderInUseError(
                    f"provider {provider_id} is bound to slot {s!r}; switch that slot first"
                )
        before = len(self._data["providers"])
        self._data["providers"] = [
            p for p in self._data["providers"] if p.get("id") != provider_id
        ]
        if len(self._data["providers"]) == before:
            raise ValueError(f"provider not found: {provider_id}")
        self._save()

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
        self._save()
        return dict(entry)

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
        return {
            "version": self._data.get("version", STORE_VERSION),
            "providers": [self._masked(p) for p in self._data["providers"]],
            "slots": slots,
        }
