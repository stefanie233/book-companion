"""Smoke tests for provider_store — run: python scripts/test_provider_store.py

Covers the contracts the console depends on:
  roundtrip / mask / resolve fallback chain / corrupt-file quarantine /
  delete protection / apply_to_config semantics / v1 migration /
  cross-kind binding.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from provider_store import (
    ProviderInUseError,
    ProviderStore,
    SLOT_EMBEDDING,
    SLOT_MAIN_LLM,
    SLOT_TTS,
    guess_model_kind,
)


def make_config(**kw) -> Config:
    defaults = dict(
        api_key="env-main-key-1234567890",
        api_base="https://env.example/v1",
        model="env-model",
        embedding_api_key="",
        embedding_model="",
        embedding_base_url="",
        tts_api_key="",
        tts_api_base="",
    )
    defaults.update(kw)
    return Config(**defaults)


def make_store(tmp: Path, name: str) -> ProviderStore:
    return ProviderStore(tmp / name / "config" / "providers.json")


def test_roundtrip_and_mask(tmp: Path) -> None:
    store = make_store(tmp, "rt")
    p = store.upsert_provider(
        {"name": "MiMo", "base_url": "https://a.api/v1",
         "api_key": "sk-secret-abcdef123456",
         "models": [{"id": "mimo-v2.5-pro", "kind": "llm"}]}
    )
    assert p["id"].startswith("p_")
    assert p["api_key"] == "sk-secret-abcdef123456"  # raw entry, encrypt hook is passthrough

    masked = store.snapshot(make_config())["providers"][0]
    assert masked["api_key"] == "••••3456", masked["api_key"]

    # edit with empty key keeps old; edit with new key replaces
    store.upsert_provider({"id": p["id"], "name": "MiMo2",
                           "base_url": "https://b.api/v1", "api_key": ""})
    assert store.reveal_key(p["id"]) == "sk-secret-abcdef123456"
    store.upsert_provider({"id": p["id"], "name": "MiMo3",
                           "base_url": "https://b.api/v1", "api_key": "sk-new-9999"})
    assert store.reveal_key(p["id"]) == "sk-new-9999"

    # persistence across instances
    again = ProviderStore(store.path)
    assert again.reveal_key(p["id"]) == "sk-new-9999"
    print("  roundtrip + mask: ok")


def test_resolve_fallback_chain(tmp: Path) -> None:
    store = make_store(tmp, "fb")
    cfg = make_config()

    # no console config -> env source, config untouched
    r = store.resolve(SLOT_MAIN_LLM, cfg)
    assert r.source == "env" and r.key == "env-main-key-1234567890" and r.model == "env-model"
    changed = store.apply_to_config(cfg)
    assert changed == {}, changed

    # bind console provider -> console source, apply overwrites
    p = store.upsert_provider(
        {"name": "DS", "base_url": "https://ds.api/v1",
         "api_key": "sk-ds-key-000111",
         "models": [{"id": "deepseek-chat", "kind": "llm"}]}
    )
    store.set_slot(SLOT_MAIN_LLM, provider_id=p["id"], model="deepseek-reasoner")
    r = store.resolve(SLOT_MAIN_LLM, cfg)
    assert r.source == "console" and r.model == "deepseek-reasoner"
    changed = store.apply_to_config(cfg)
    assert cfg.api_key == "sk-ds-key-000111"
    assert cfg.api_base == "https://ds.api/v1"
    assert cfg.model == "deepseek-reasoner"
    assert changed[SLOT_MAIN_LLM] == "DS/deepseek-reasoner"

    # slot with empty model: model NOT clobbered, key/base still switch
    cfg2 = make_config()
    store.set_slot(SLOT_MAIN_LLM, provider_id=p["id"], model="")
    store.apply_to_config(cfg2)
    assert cfg2.model == "env-model"
    assert cfg2.api_base == "https://ds.api/v1"

    # tts slot applies key/base (consumer tools.py falls back internally)
    cfg3 = make_config(tts_api_key="env-tts", tts_api_base="https://env-tts/v1")
    pt = store.upsert_provider(
        {"name": "MiMo TTS", "base_url": "https://mimo.api/v1",
         "api_key": "sk-tts-8888",
         "models": [{"id": "mimo-v2.5-tts", "kind": "tts"}]}
    )
    store.set_slot(SLOT_TTS, provider_id=pt["id"])
    r = store.resolve(SLOT_TTS, cfg3)
    assert r.source == "console" and r.model == ""
    store.apply_to_config(cfg3)
    assert cfg3.tts_api_key == "sk-tts-8888" and cfg3.tts_api_base == "https://mimo.api/v1"

    # tts slot model passes through to config.tts_model (console override);
    # unbind resets it so the voice-kind mapping resumes
    store.set_slot(SLOT_TTS, provider_id=pt["id"], model="MiniMax/speech-2.8-hd")
    store.apply_to_config(cfg3)
    assert cfg3.tts_model == "MiniMax/speech-2.8-hd"
    store.set_slot(SLOT_TTS, provider_id="")
    store.apply_to_config(cfg3)
    assert cfg3.tts_model == ""
    print("  resolve fallback chain: ok")


def test_corrupt_file_quarantine(tmp: Path) -> None:
    store = make_store(tmp, "corrupt")
    store.upsert_provider({"name": "X", "base_url": "https://x/v1"})
    store.path.write_text("{not valid json!!", encoding="utf-8")

    recovered = ProviderStore(store.path)
    assert recovered.list_providers() == []
    bak = store.path.with_name(store.path.name + ".bak")
    assert bak.exists() and "{not valid json!!" in bak.read_text(encoding="utf-8")
    # store still usable afterwards
    recovered.upsert_provider({"name": "Y", "base_url": "https://y/v1"})
    assert len(recovered.list_providers()) == 1
    print("  corrupt file quarantine: ok")


def test_delete_protection_and_validation(tmp: Path) -> None:
    store = make_store(tmp, "del")
    p = store.upsert_provider(
        {"name": "EB", "base_url": "https://e/v1", "api_key": "k",
         "models": [{"id": "text-embed-3", "kind": "embedding"}]}
    )
    store.set_slot(SLOT_EMBEDDING, provider_id=p["id"], model="text-embed-3")

    try:
        store.delete_provider(p["id"])
        raise AssertionError("expected ProviderInUseError")
    except ProviderInUseError:
        pass

    store.set_slot(SLOT_EMBEDDING, provider_id="")  # explicit unbind
    assert store.get_slot(SLOT_EMBEDDING)["provider_id"] is None
    store.delete_provider(p["id"])  # now allowed
    assert store.get_provider(p["id"]) is None

    # validation errors
    for bad in (
        {"name": "x", "base_url": ""},                   # base_url required
        {"id": "p_nope", "name": "x", "base_url": "u"},  # unknown edit target
    ):
        try:
            store.upsert_provider(bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    # kind mismatch on bind — cached model kind enforced
    pl = store.upsert_provider(
        {"name": "L", "base_url": "u", "models": [{"id": "gpt-x", "kind": "llm"}]})
    try:
        store.set_slot(SLOT_TTS, provider_id=pl["id"], model="gpt-x")
        raise AssertionError("expected kind mismatch ValueError")
    except ValueError:
        pass
    # bare-string models get kind-guessed
    pg = store.upsert_provider(
        {"name": "G", "base_url": "u", "models": ["text-embedding-v3"]})
    assert store.model_kind(pg["id"], "text-embedding-v3") == "embedding"
    # optional name falls back to vendor, then host
    pn = store.upsert_provider({"vendor": "千问", "base_url": "https://dashscope.example/v1"})
    assert pn["name"] == "千问"
    ph = store.upsert_provider({"base_url": "https://api.host.example/v1"})
    assert ph["name"] == "api.host.example"
    print("  delete protection + validation: ok")


def test_v1_migration(tmp: Path) -> None:
    store = make_store(tmp, "mig")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({
        "version": 1,
        "providers": [
            {"id": "p_llm_ab01", "kind": "llm", "name": "Old", "vendor": "MiMo",
             "base_url": "https://o/v1", "api_key": "sk-old",
             "models": ["mimo-v2.5-pro", "mimo-v2.5-tts"]},
            {"id": "p_tts_cd02", "kind": "tts", "name": "Mixed", "vendor": "V",
             "base_url": "https://m/v1", "api_key": "",
             "models": ["x-voice", {"id": "e3", "kind": "embedding"}]},
        ],
        "slots": {"main_llm": {"provider_id": "p_llm_ab01",
                               "model": "mimo-v2.5-pro"}},
    }, ensure_ascii=False), encoding="utf-8")

    reloaded = ProviderStore(store.path)
    a = reloaded.get_provider("p_llm_ab01")
    assert a["models"] == [{"id": "mimo-v2.5-pro", "kind": "llm"},
                           {"id": "mimo-v2.5-tts", "kind": "llm"}]
    assert "kind" not in a
    b = reloaded.get_provider("p_tts_cd02")
    assert b["models"] == [{"id": "x-voice", "kind": "tts"},
                           {"id": "e3", "kind": "embedding"}]
    # slot binding survives migration
    assert reloaded.get_slot(SLOT_MAIN_LLM)["provider_id"] == "p_llm_ab01"
    # edit without models key keeps the migrated catalog; re-save persists v2
    reloaded.upsert_provider({"id": "p_llm_ab01", "name": "Old",
                              "base_url": "https://o/v1"})
    assert len(reloaded.get_provider("p_llm_ab01")["models"]) == 2
    assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == 3
    # v1/v2 files carry no routes — the key defaults to absent (inherit)
    assert reloaded.get_route("bookgraph")["provider_id"] is None
    print("  v1 migration: ok")


def test_cross_kind_binding(tmp: Path) -> None:
    store = make_store(tmp, "xkind")
    p = store.upsert_provider({
        "name": "MiMo", "base_url": "https://mimo/v1",
        "models": [{"id": "mimo-v2.5-pro", "kind": "llm"},
                   {"id": "mimo-v2.5-tts", "kind": "tts"}]})
    store.set_slot(SLOT_MAIN_LLM, provider_id=p["id"], model="mimo-v2.5-pro")
    store.set_slot(SLOT_TTS, provider_id=p["id"], model="mimo-v2.5-tts")
    try:
        store.set_slot(SLOT_MAIN_LLM, provider_id=p["id"], model="mimo-v2.5-tts")
        raise AssertionError("expected kind mismatch ValueError")
    except ValueError:
        pass
    # unknown (hand-typed) model names pass through — runtime errors by design
    store.set_slot(SLOT_MAIN_LLM, provider_id=p["id"], model="brand-new-model")
    assert store.model_kind(p["id"], "mimo-v2.5-tts") == "tts"
    assert store.model_kind(p["id"], "nope") is None
    assert store.model_kind("p_missing", "nope") is None
    # real-world ids guess correctly
    assert guess_model_kind("mimo-v2.5-pro") == "llm"
    assert guess_model_kind("mimo-v2.5-tts-voicedesign") == "tts"
    assert guess_model_kind("mimo-v2.5-asr") == "asr"
    assert guess_model_kind("cosyvoice-v2") == "tts"
    assert guess_model_kind("text-embedding-v3") == "embedding"
    print("  cross-kind binding + guess: ok")


def test_route_binding(tmp: Path) -> None:
    store = make_store(tmp, "routes")
    cfg = make_config()
    ds = store.upsert_provider({
        "name": "DS", "base_url": "https://ds/v1", "api_key": "sk-ds",
        "models": [{"id": "graph-fast", "kind": "llm"}]})
    db = store.upsert_provider({
        "name": "DB", "base_url": "https://db/v1", "api_key": "sk-db",
        "models": [{"id": "write-pro", "kind": "llm"}]})

    # unbound -> env; config_for returns the original object
    r = store.resolve_route("bookgraph.explorer", cfg)
    assert r.source == "env" and r.model == "env-model"
    assert store.config_for("bookgraph.explorer", cfg) is cfg

    # group binding covers every leaf
    store.set_route("bookgraph", provider_id=ds["id"], model="graph-fast")
    r = store.resolve_route("bookgraph.explorer", cfg)
    assert r.source == "console" and r.model == "graph-fast"
    assert r.base_url == "https://ds/v1" and r.key == "sk-ds"
    view = store.config_for("bookgraph.explorer", cfg)
    assert view.model == "graph-fast" and view.api_base == "https://ds/v1"
    assert cfg.model == "env-model"  # base Config untouched

    # leaf override beats group
    store.set_route("bookgraph.adjudicate", provider_id=db["id"], model="write-pro")
    assert store.resolve_route("bookgraph.explorer", cfg).model == "graph-fast"
    assert store.resolve_route("bookgraph.adjudicate", cfg).model == "write-pro"
    assert store.route_origin("bookgraph.adjudicate") == "bookgraph.adjudicate"
    assert store.route_origin("bookgraph.notes") == "bookgraph"

    # resolving a leaf from a group view must NOT erase the group binding
    gview = store.config_for("bookgraph", cfg)
    nview = store.config_for("bookgraph.notes", gview)
    assert nview.model == "graph-fast"

    # unbind leaf -> group again
    store.set_route("bookgraph.adjudicate", provider_id="")
    assert store.resolve_route("bookgraph.adjudicate", cfg).model == "graph-fast"

    # validation: unknown key / missing model
    for key, pid, model in (("nope.nope", ds["id"], "x"),
                            ("bookgraph.scout", ds["id"], "")):
        try:
            store.set_route(key, provider_id=pid, model=model)
            raise AssertionError(f"expected ValueError for {key}")
        except ValueError:
            pass
    # non-llm model rejected on a route
    pt = store.upsert_provider({"name": "T", "base_url": "u",
        "models": [{"id": "voice-x", "kind": "tts"}]})
    try:
        store.set_route("bookgraph.scout", provider_id=pt["id"], model="voice-x")
        raise AssertionError("expected kind mismatch ValueError")
    except ValueError:
        pass

    # delete protection extends to routes
    try:
        store.delete_provider(ds["id"])
        raise AssertionError("expected ProviderInUseError")
    except ProviderInUseError:
        pass

    # snapshot carries routes + per-call-point effective resolution
    snap = store.snapshot(cfg)
    assert snap["version"] == 3 and "bookgraph" in snap["routes"]
    cp = next(c for c in snap["callpoints"] if c["id"] == "bookgraph.explorer")
    assert cp["origin"] == "bookgraph" and not cp["bound"]
    assert cp["resolved"]["model"] == "graph-fast"
    assert any(g["id"] == "bookgraph" and g["bound"] for g in snap["groups"])
    print("  route binding: ok")


def test_active_store_reload(tmp: Path) -> None:
    cfg = make_config()
    cfg.project_dir = tmp / "proj"
    store = ProviderStore(cfg.providers_path)
    p = store.upsert_provider({"name": "DS", "base_url": "https://ds/v1",
                               "api_key": "k",
                               "models": [{"id": "m", "kind": "llm"}]})
    store.set_route("writing.review", provider_id=p["id"], model="m")

    from provider_store import config_for as active_config_for
    view = active_config_for(cfg, "writing.review")
    assert view.model == "m" and view.api_base == "https://ds/v1"

    # a write through another instance (the console) is seen on the next call
    other = ProviderStore(cfg.providers_path)
    other.set_route("writing.review", provider_id=p["id"], model="m2")
    assert active_config_for(cfg, "writing.review").model == "m2"
    print("  active store reload: ok")


def test_stale_writer_keeps_routes(tmp: Path) -> None:
    """Regression: changing the main model must not erase route bindings.

    The console saves the whole in-memory snapshot. An instance loaded
    before routes were written (another process / an older app object)
    used to write its stale copy back and wipe them. Mutations now reload
    first, so a save merges onto the newest file.
    """
    base = make_store(tmp, "stale")
    p = base.upsert_provider({
        "name": "A", "base_url": "https://a/v1", "api_key": "k",
        "models": [{"id": "m1", "kind": "llm"}, {"id": "m2", "kind": "llm"}]})

    base.set_route("bookgraph", provider_id=p["id"], model="m1")
    stale = ProviderStore(base.path)          # snapshot BEFORE the second bind

    # another writer adds a route after the stale snapshot
    fresh = ProviderStore(base.path)
    fresh.set_route("writing.review", provider_id=p["id"], model="m2")

    # stale instance now changes the main model — routes must survive
    stale.set_slot(SLOT_MAIN_LLM, provider_id=p["id"], model="m2")

    reread = ProviderStore(base.path)
    assert reread.get_slot(SLOT_MAIN_LLM)["model"] == "m2"
    assert "bookgraph" in reread._data["routes"]
    assert "writing.review" in reread._data["routes"]

    # same guarantee for a provider edit on the stale instance
    stale.set_route("bookgraph.adjudicate", provider_id=p["id"], model="m1")
    stale.upsert_provider({"id": p["id"], "name": "A2",
                           "base_url": "https://a/v1"})
    reread = ProviderStore(base.path)
    assert set(reread._data["routes"]) == {
        "bookgraph", "writing.review", "bookgraph.adjudicate"}
    print("  stale writer keeps routes: ok")


def test_save_never_drops_disk_routes(tmp: Path) -> None:
    """Even if in-memory routes are empty, a non-unbind save keeps disk ones."""
    store = make_store(tmp, "neverdrop")
    p = store.upsert_provider({
        "name": "A", "base_url": "https://a/v1", "api_key": "k",
        "models": [{"id": "m1", "kind": "llm"}, {"id": "m2", "kind": "llm"}]})
    store.set_route("bookgraph", provider_id=p["id"], model="m1")

    # simulate an exotic writer whose memory lost the routes
    store._data["routes"] = {}
    store._save("slot_set")
    assert "bookgraph" in ProviderStore(store.path)._data["routes"]

    # an explicit unbind must still be honoured (no resurrection)
    store.set_route("bookgraph", provider_id="")
    assert "bookgraph" not in ProviderStore(store.path)._data["routes"]
    print("  save never drops disk routes: ok")


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_roundtrip_and_mask(tmp)
        test_resolve_fallback_chain(tmp)
        test_corrupt_file_quarantine(tmp)
        test_delete_protection_and_validation(tmp)
        test_v1_migration(tmp)
        test_cross_kind_binding(tmp)
        test_route_binding(tmp)
        test_active_store_reload(tmp)
        test_stale_writer_keeps_routes(tmp)
        test_save_never_drops_disk_routes(tmp)
    print("all provider_store tests passed")


if __name__ == "__main__":
    main()
