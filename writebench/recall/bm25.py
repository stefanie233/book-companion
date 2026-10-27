"""中文 BM25：jieba 分词 + 倒排索引，只用于评测对照。"""

from __future__ import annotations

import math
import re
from typing import Iterable, Sequence

import jieba

STOP = set("""的 了 在 是 我 你 他 她 它 我们 你们 他们 她们 这 那 一个 一种
一些 这个 那个 这样 那样 什么 怎么 因为 所以 但是 可是 而且 然后 已经
还是 就是 不是 没有 可以 应该 需要 必须 不要 不能 时候 地方 事情 东西
感觉 好像 仿佛 起来 出来 这里 那里 现在 过去 之后 之前""".split())
_PUNCT = re.compile(r"^[\W_]+$", re.UNICODE)


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for word in jieba.lcut(text or ""):
        word = word.strip()
        if not word or word in STOP or _PUNCT.match(word):
            continue
        if len(word) == 1 and not word.isascii():
            continue
        out.append(word.lower() if word.isascii() else word)
    return out


class BM25:
    def __init__(self, docs: Sequence[Sequence[str]],
                 *, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.dl: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        df: dict[str, int] = {}
        for idx, tokens in enumerate(docs):
            self.dl.append(len(tokens))
            tf: dict[str, int] = {}
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1
            for token, count in tf.items():
                self.postings.setdefault(token, []).append((idx, count))
                df[token] = df.get(token, 0) + 1
        self.avgdl = sum(self.dl) / self.n if self.n else 0.0
        self.idf = {t: math.log(1 + (self.n - d + 0.5) / (d + 0.5))
                    for t, d in df.items()}

    def search(self, query: Iterable[str], k: int = 10,
               allowed: set[int] | None = None) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for token in set(query):
            idf = self.idf.get(token)
            if not idf:
                continue
            for idx, tf in self.postings[token]:
                if allowed is not None and idx not in allowed:
                    continue
                dl = self.dl[idx] or 1
                denom = tf + self.k1 * (
                    1 - self.b + self.b * dl / (self.avgdl or 1))
                scores[idx] = (scores.get(idx, 0.0)
                               + idf * tf * (self.k1 + 1) / denom)
        return sorted(scores.items(), key=lambda kv: -kv[1])[:k]
