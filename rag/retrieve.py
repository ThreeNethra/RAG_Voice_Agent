"""Stage 4: RETRIEVAL.

Three ways to rank chunks for a question:

  dense   cosine similarity between embeddings   (good at meaning)
  sparse  BM25 keyword scoring                   (good at exact strings, IDs)
  hybrid  both, merged with Reciprocal Rank Fusion (RRF)

RRF in one line: each ranker votes 1/(k + rank) for every chunk it returns;
sum the votes. It ignores raw scores (cosine and BM25 live on different
scales), which is exactly why it is robust and why almost everyone uses it.

Metadata filtering happens BEFORE ranking, so a deprecated pricing page can
never crowd out the current one.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bm25 import BM25
from .access import can_see
from .chunking import Chunk
from .textutil import last_json


@dataclass
class Hit:
    chunk: Chunk
    score: float
    rank: int


class Retriever:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray, embedder, reranker=None):
        self.chunks, self.vectors, self.embedder, self.reranker = chunks, vectors, embedder, reranker
        self.bm25 = BM25([c.embed_text for c in chunks])

    # -- individual rankers return [(chunk_index, score)] best-first
    def _dense(self, query: str, allowed: np.ndarray, n: int):
        sims = self.vectors @ self.embedder.encode_query(query)
        sims = np.where(allowed, sims, -np.inf)
        idx = np.argsort(-sims, kind="stable")[:n]  # stable: equal scores keep chunk order, so results never depend on the sort algorithm
        return [(int(i), float(sims[i])) for i in idx if np.isfinite(sims[i])]

    def _sparse(self, query: str, allowed: np.ndarray, n: int):
        sc = np.array(self.bm25.scores(query))
        sc = np.where(allowed, sc, -np.inf)
        idx = np.argsort(-sc, kind="stable")[:n]
        return [(int(i), float(sc[i])) for i in idx if np.isfinite(sc[i]) and sc[i] > 0]

    def search(self, query: str, mode: str = "hybrid", k: int = 5,
               include_deprecated: bool = False, pool: int = 30, rrf_k: int = 60,
               rerank: bool = False, groups=None) -> list[Hit]:
        # THE access-control choke point: permissions are applied to the candidate set BEFORE any ranking,
        # so nothing a caller may not see can enter any list, fusion, rerank, prompt or cache.
        allowed = np.array([(include_deprecated or c.status != "deprecated") and can_see(c.acl, groups) for c in self.chunks])
        n = pool if (rerank or mode == "hybrid") else k

        if mode == "dense":
            ranked = self._dense(query, allowed, n)
        elif mode == "sparse":
            ranked = self._sparse(query, allowed, n)
        elif mode == "hybrid":
            votes: dict[int, float] = {}
            for lst in (self._dense(query, allowed, pool), self._sparse(query, allowed, pool)):
                for r, (i, _) in enumerate(lst, start=1):
                    votes[i] = votes.get(i, 0.0) + 1.0 / (rrf_k + r)
            ranked = sorted(votes.items(), key=lambda t: -t[1])[:n]
        else:
            raise ValueError(mode)

        if rerank and self.reranker is not None:
            ranked = self.reranker.rerank(query, [(i, self.chunks[i]) for i, _ in ranked])
        return [Hit(self.chunks[i], s, r) for r, (i, s) in enumerate(ranked[:k], start=1)]


class CrossEncoderReranker:
    """Stage 4b: RERANKING.

    A bi-encoder (our embedder) encodes query and chunk SEPARATELY, so it is fast
    but coarse. A cross-encoder reads query+chunk TOGETHER and scores relevance
    much more accurately, but is too slow to run over the whole corpus. So:
    retrieve ~30 candidates cheaply, rerank just those, keep the top k.
    """

    def __init__(self, model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        from sentence_transformers import CrossEncoder
        self.model = CrossEncoder(model)

    def rerank(self, query: str, cands: list[tuple[int, Chunk]]):
        scores = self.model.predict([(query, c.embed_text) for _, c in cands])
        order = np.argsort(-np.asarray(scores))
        return [(cands[j][0], float(scores[j])) for j in order]


class LLMReranker:
    """Listwise reranking with an LLM: show it the question and all candidates at
    once, ask for an ordering. Slower and costlier than a cross-encoder, but it
    follows instructions ("prefer current documents") and handles multi-part
    questions. Pointwise = score each candidate alone; listwise = rank them
    together (this one), which is cheaper (one call) and lets the model compare.
    """

    SYSTEM = (
        "You rank search results for a question. You get numbered passages. "
        "Return ONLY a JSON array of passage numbers, ordered from most to least useful "
        "for answering the question. Include every number exactly once. "
        "Prefer passages whose status is current over deprecated ones."
    )

    def __init__(self, llm, max_chars: int = 400, max_cands: int = 10):
        # Only the first max_cands candidates are shown to the model: a long list overflows a small
        # context window (silent truncation) and small models rank long lists badly anyway.
        self.llm, self.max_chars, self.max_cands = llm, max_chars, max_cands

    def rerank(self, query: str, cands: list[tuple[int, Chunk]]):
        tail = cands[self.max_cands:]
        cands = cands[:self.max_cands]
        listing = "\n\n".join(
            f"[{j}] (source: {c.source}, status: {c.status})\n{c.embed_text[:self.max_chars]}"
            for j, (_, c) in enumerate(cands))
        text = self.llm(self.SYSTEM, f"Question: {query}\n\nPassages:\n{listing}")
        arr = last_json(text, list)
        if arr is None:
            raise ValueError("rerank: reply contained no JSON array")  # surfaced by Pipeline as a FAILED step
        order: list[int] = []
        for x in arr:
            if isinstance(x, int) and 0 <= x < len(cands) and x not in order:
                order.append(x)
        order += [j for j in range(len(cands)) if j not in order]  # never lose a candidate
        out = [(cands[j][0], float(len(cands) + len(tail) - r)) for r, j in enumerate(order)]
        return out + [(i, float(len(tail) - r)) for r, (i, _) in enumerate(tail)]
