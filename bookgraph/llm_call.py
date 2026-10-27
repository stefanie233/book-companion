"""图谱链路调 LLM 的公共部分 —— 输出预算与截断识别。

五个调用点（scout / context_setter / head_scan / explorer / verifier）原本各自
写死 ``max_tokens: 16000``。这个数字对**推理模型**是错的：实测
``deepseek-v4.1-flash`` 单批推理就烧掉 15406 token，16000 的预算意味着正文
一个字符都轮不到 —— 端点回 HTTP 200 + **空 content**，被读成「模型没说话」，
重试三次（每次 ~25s）后整批丢弃。同一条 prompt 把预算抬到 64000 就
``finish_reason=stop`` 正常返回。

所以两件事：预算按模型给（可配、默认放宽），并且**把「被截断」和「说胡话」
分开报** —— 前者是预算问题，后者是模型问题，混成一个 ``parse_failed``
就永远查不出根因（这个 bug 上一次就是这么活下来的）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只在注解里用到，避免 bookgraph 包内的循环导入
    from config import Config

#: 默认输出预算。按**总** token 算（推理 + 正文），不是正文字数 ——
#: 推理模型的 reasoning 也从这个额度里扣。
DEFAULT_OUTPUT_BUDGET = 64_000

#: 撞上限后抬预算的天花板。实测端点对 200000 也不报错，但翻倍两次够用了 ——
#: 真正需要更多的时候，是模型或提示词出了问题，不是再给点额度能解决的。
MAX_OUTPUT_BUDGET = 256_000


def output_budget(cfg: Config) -> int:
    """这次调用给多少输出预算。``cfg.graph_max_tokens`` 为 0 时用默认值。"""
    return getattr(cfg, "graph_max_tokens", 0) or DEFAULT_OUTPUT_BUDGET


#: 限流退避（秒）。实测这个端点的 429 是 **burst 保护**，不是网络抖动：
#: 2/4 秒的退避在同一个号上等于没有退避 —— 一次窄门跑里 32 次 429 有 29 次
#: 就发生在两次重试之间，然后重试次数耗尽、**整批 5–7 块正文丢失**。
#: 它要的是「等久一点」，不是「再问一遍」。
THROTTLE_BACKOFF = (20, 45, 90, 180, 300)


def is_throttle(exc: Exception | None) -> bool:
    """429 / RequestBurstTooFast。"""
    text = str(exc or "")
    return "429" in text or "TooManyRequests" in text or "BurstTooFast" in text


def retry_delay(attempt: int, throttled: int, base: float = 2.0) -> float:
    """第 ``attempt`` 次重试前等多久。``throttled`` = 已经撞过几次限流。"""
    if throttled:
        return THROTTLE_BACKOFF[min(throttled - 1, len(THROTTLE_BACKOFF) - 1)]
    return base ** attempt


def finish_reason(data: dict) -> str:
    """端点的 finish_reason。缺失时返回空串（不猜）。"""
    choices = data.get("choices") or [{}]
    return str(choices[0].get("finish_reason") or "")


def is_truncated(data: dict) -> bool:
    """是否**因为撞上预算上限**而停 —— 与「模型说了胡话」是两回事。

    撞上限时端点可能回空 content（推理吃光了额度），也可能回半截 JSON。
    两种都必须报 ``truncated``：同样的预算重试是白费，加预算才有用。
    """
    return finish_reason(data) == "length"


def reasoning_text(data: dict) -> str:
    """思维链正文（``choices[0].message.reasoning_content``）。

    **只记 token 数等于什么都没记。** 实测一次提取调用 reasoning 占输出的
    62%，而模型在那 4,439 个 token 里把「他的确疯狂地爱着她」抄成了「她的
    确」—— 没有正文，「它当时在想什么」这个问题在库层面问不出来。端点返回
    这个字段就存下来；不返回（非推理模型）就是空串，不假装有。
    """
    try:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return str(msg.get("reasoning_content") or msg.get("reasoning") or "")
    except Exception:
        return ""


def usage_tokens(data: dict) -> tuple[int, int]:
    """``(prompt_tokens, completion_tokens)``，缺字段一律 0（不猜）。

    非 ReAct 的调用点（作者层采集/丰化）不在 ``OpenAIClient`` 里，不自己
    取这两个数，用量页就没有它们 —— token 是迭代成本唯一的量化依据。
    """
    usage = data.get("usage") or {}
    return (int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0))


def reasoning_tokens(data: dict) -> int:
    """推理 token 数（非推理模型为 0）。用来解释「为什么正文这么短」。"""
    usage = data.get("usage") or {}
    det = usage.get("completion_tokens_details") or {}
    return int(det.get("reasoning_tokens") or 0)


def cached_tokens(data: dict) -> int:
    """前缀缓存命中的输入 token 数。

    端点在 ``usage.prompt_tokens_details.cached_tokens`` 里回这个数（实测
    deepseek-v4.1-flash / -economy 都有）。不记它就永远只能猜「缓存命中率
    为什么低」—— 而命中率取决于**前缀是否稳定**，正是 harness 能改的东西。
    """
    usage = data.get("usage") or {}
    det = usage.get("prompt_tokens_details") or {}
    return int(det.get("cached_tokens") or 0)
