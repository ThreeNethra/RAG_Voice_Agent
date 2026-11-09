"""Keyword (sparse) retrieval: BM25, written from scratch so you see every term.

BM25 scores a chunk higher when it contains rare query words, many times, in a
short chunk:

    score(q, d) = sum over query terms t of
        IDF(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg_len))

Why we need it even though embeddings exist: embeddings blur exact strings.
Ask for "INC-2026-0312" or "CT-300" or "Compass" and BM25 nails it.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

TOKENIZER_VERSION = "2"   # bump when tokenize() changes: stored tf-idf/hash vectors were built with the old rules and must not be reused

_STOP = set("a an and are as at be by for from has have how in is it of on or that the to was what when where which who whose will with do does did can i you your we our their".split())
_TOKEN = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")


_DASHES = {ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"}
_INVISIBLE = {ord(c): None for c in "\u00ad\u200b\u200c\u200d\u2060\ufeff"}


def normalize(text: str) -> str:
    """Make text typed, pasted or extracted in different ways comparable: full-width letters -> ASCII (NFKC),
    every dash -> '-', soft hyphens and zero-width characters removed. Used on documents AND queries."""
    return unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_INVISIBLE)


def tokenize(text: str) -> list[str]:
    """Keep identifiers whole ("ct-300") AND emit their parts ("ct", "300")."""
    text = re.sub(r"(?<=\d),(?=\d)", "", normalize(text).lower())  # 1,950 -> 1950
    out: list[str] = []
    for tok in _TOKEN.findall(text):
        if tok in _STOP:
            continue
        out.append(tok)
        if "-" in tok or "." in tok:
            out.extend(p for p in re.split(r"[-.]", tok) if p and p not in _STOP)
    return out


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(tokenize(d)) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = sum(self.len) / max(len(docs), 1)
        n = len(docs)
        df: Counter = Counter()
        for c in self.tf:
            df.update(c.keys())
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = set(tokenize(query))
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            out.append(s)
        return out
