"""声音提取的一批调用 —— 普通 LLM 调用，**不是子代理**。

理由与 ``harvest.py`` 相同：输入完全确定（作者 + 这本书 + 6 段样本），
不需要去够别的东西；普通调用才能被 fake provider 零 token 驱动。
``temperature=0.1`` 是取证不是创作：同一批样本重跑应当得到同一份判断。

成本上限（设计稿 08 §四.3）：每批 ≤6 段、≤500 字/段、整个 run ≤2 次调用。
分书/分卷跑，不把单次调用撑爆。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import aiohttp

import usage_log
from bookgraph.llm_call import (finish_reason, output_budget, reasoning_text,
                                usage_tokens)
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from sub_agent.types import load_prompt

from authorgraph.voice.normalize import normalize_batch

log = logging.getLogger("authorgraph")

#: 一批的样本数。6 段 × 500 字 = 3000 字，留足判断与输出的余量。
BATCH_SIZE = 6
#: 一个 run 的调用上限（设计稿 08 §四.3）。
MAX_CALLS = 2

_FALLBACK = """你在读一位作者写的正文样本，提炼**怎么写**，不是**写了什么**。

只描述可执行的写法：句子长短、叙述距离、段落形态、对话密度、情绪怎么
落地、信息何时揭示、词汇质感、幽默方式、怎么收束。不要评价「优美/深刻」，
不要推断作者生平、心理、政治立场。

每条 trait 必须引用本批样本的 corpus_id，至少 2 段不同样本；只有 1 段时
strength 不超过 0.4。只能引用输入里给出的样本 —— 编造的 id 会被系统剔除。
作者的专名、地名、口癖、只在本书出现的意象写进 source_bound，不要混进 traits。

严格 JSON：
{"samples":[{"corpus_id":12,"scene_type":"introspection",
  "narrative_layer":"narration|dialogue","pov":"first|third_limited|third_omniscient|mixed|unknown",
  "period":"early|mid|late|","notes":""}],
 "traits":[{"dimension":"narrative_distance|sentence_rhythm|paragraph_shape|dialogue_density|figurative_language|emotional_expression|information_reveal|lexical_texture|humor_irony|closure_pattern",
  "voice_domain":"fiction|nonfiction|paratext|public|mixed|unknown",
  "value":"该维度的词表值","statement":"可执行的写法描述",
  "strength":0.8,"transferable":true,"evidence_corpus_ids":[12,18],
  "confidence":0.7}],
 "source_bound":[{"name":"","type":"lexical|imagery|name|other",
  "statement":"","evidence_corpus_ids":[12]}],
 "defects":[]}
没有足够证据就返回空 traits；空数组不是失败。"""


def voice_prompt() -> str:
    return load_prompt("author_voice", _FALLBACK)


def build_voice_msg(author: dict[str, Any], book: dict[str, Any],
                    samples: list[dict[str, Any]]) -> str:
    """给提取调用的信：作者身份 + 这本书 + 本批样本。

    **不给**生平台账、其他作者的样本、研究者的评价 —— 三个「不给」分别
    防「把生平当风格」「把比较当特征」「把评论当作者写法」。
    """
    name = (author.get("canonical_name") or "").strip()
    years = ""
    by, dy = author.get("birth_year"), author.get("death_year")
    if by or dy:
        years = f"（{by or '?'}–{dy or '?'}）"
    lines = ["## 作者", f"  {name or '(未知)'}{years}"
             + (f"，{author['nationality']}" if author.get("nationality") else ""),
             "",
             "## 这本书",
             f"  《{book.get('title') or '(未知)'}》",
             f"  位置：{book.get('position') or 'unknown'}"
             f" —— {book.get('position_note') or ''}",
             f"  声音域：{book.get('domain') or 'unknown'}", "",
             "## 样本（请按 corpus_id 引用）", ""]
    for s in samples:
        lines += [
            f"[corpus_id={s['corpus_id']}] [ch{s.get('chapter_index', '?')}"
            f" 《{s.get('chapter_title') or ''}》]"
            f" [scene_hint={s.get('scene_hint') or 'other'}]",
            s.get("text") or "", "",
        ]
    return "\n".join(lines)


async def extract_batch(session: aiohttp.ClientSession, cfg: Config,
                        author: dict[str, Any], book: dict[str, Any],
                        samples: list[dict[str, Any]], *,
                        samples_by_id: dict[int, dict[str, Any]],
                        default_domain: str = "unknown",
                        prompt_version: str = "",
                        log_cb: Callable[[dict], None] | None = None
                        ) -> dict[str, Any]:
    """跑一批。返回 ``{status, batch, defects, response}``；**不 raise**。"""
    cfg = config_for(cfg, "authorgraph.voice")
    system_prompt = voice_prompt()
    user_msg = build_voice_msg(author, book, samples)
    body = {"model": cfg.model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_msg}],
            "stream": False, "temperature": 0.1,
            "max_tokens": output_budget(cfg)}
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    t0 = time.monotonic()
    aid = str(author.get("author_id") or "")

    def _emit(status: str, *, response: str = "", reasoning: str = "",
              finish: str = "", error_detail: str = "",
              tokens_in: int = 0, tokens_out: int = 0) -> None:
        usage_log.record(source="authorgraph", model=cfg.model,
                         tokens_in=tokens_in, tokens_out=tokens_out,
                         status=status, ref_id=aid)
        if log_cb:
            log_cb({"status": status, "system": system_prompt,
                    "prompt": user_msg, "response": response,
                    "reasoning_content": reasoning, "finish_reason": finish,
                    "error_detail": error_detail, "model": cfg.model,
                    "latency_ms": int((time.monotonic() - t0) * 1000)})

    try:
        async with session.post(
            f"{cfg.api_base}/chat/completions", json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                err = (await resp.text())[:200]
                log.warning("Voice extract API %s: %s", resp.status, err)
                _emit("api_error", error_detail=err)
                return {"status": "api_error", "batch": None,
                        "defects": [err], "response": ""}
            data = json.loads(await resp.text())
            content = ((data.get("choices") or [{}])[0]
                       .get("message") or {}).get("content") or ""
            reasoning = reasoning_text(data)
            finish = finish_reason(data)
            tokens_in, tokens_out = usage_tokens(data)
    except Exception as exc:                          # noqa: BLE001
        log.warning("Voice extract failed: %s", exc)
        _emit("exception", error_detail=str(exc)[:300])
        return {"status": "exception", "batch": None,
                "defects": [str(exc)], "response": ""}

    parsed = loads_tolerant(content, expect=dict)
    if not parsed:
        _emit("parse_failed", response=content, reasoning=reasoning,
              finish=finish, tokens_in=tokens_in, tokens_out=tokens_out)
        return {"status": "parse_failed", "batch": None,
                "defects": [f"解析失败（finish={finish!r}）"],
                "response": content}
    batch = normalize_batch(parsed, samples_by_id=samples_by_id,
                            default_domain=default_domain)
    _emit("ok", response=content, reasoning=reasoning, finish=finish,
          tokens_in=tokens_in, tokens_out=tokens_out)
    return {"status": "ok", "batch": batch,
            "defects": batch.get("defects") or [], "response": content,
            "tokens_in": tokens_in, "tokens_out": tokens_out}


def split_batches(samples: list[dict[str, Any]],
                  size: int = BATCH_SIZE) -> list[list[dict[str, Any]]]:
    """按阅读顺序切批，最多 ``MAX_CALLS`` 批；多出来的样本丢掉并报告。"""
    size = max(1, min(int(size), BATCH_SIZE))
    out = [samples[i:i + size] for i in range(0, len(samples), size)]
    return out[:MAX_CALLS]


__all__ = ["BATCH_SIZE", "MAX_CALLS", "build_voice_msg", "extract_batch",
           "split_batches", "voice_prompt"]
