"""The full online pipeline: transform -> retrieve (many times) -> fuse -> rerank.

    question
       |-- base search (dense+sparse hybrid)
       |-- + expansion search          (cfg.expand)
       |-- + one search per sub-query  (cfg.decompose)
       |-- + HyDE search               (cfg.hyde)
       v
    fuse all ranked lists with RRF
       |-- + follow-up searches seeded by what was found (cfg.iterative), fuse again
       v
    rerank the top `pool` candidates (cfg.rerank = "cross" | "llm")
       v
    top k

Every technique ADDS candidate lists; RRF merges them. A chunk that several
lists agree on rises; the original ranking is never thrown away, so a bad
rewrite can dilute results but rarely destroys them.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .query import decompose, expansion_terms, followup_queries, hyde
from .retrieve import Hit, Retriever


@dataclass
class SearchConfig:
    mode: str = "hybrid"          # dense | sparse | hybrid for the base search
    k: int = 5
    pool: int = 30                # candidates kept per list / sent to the reranker
    include_deprecated: bool = False
    expand: bool = False          # pseudo-relevance feedback (no LLM needed)
    decompose: bool = False       # needs LLM
    hyde: bool = False            # needs LLM
    iterative: bool = False       # needs LLM
    rerank: str | None = None     # "cross" | "llm"
    expand_ids_only: bool = False # expansion uses identifiers only (less drift)
    max_per_doc: int = 0          # diversity: at most N chunks per source document (0 = off)
    groups: tuple | None = None   # caller's groups for access control; None = unrestricted (offline eval/admin only)


def diversify(hits: list[Hit], k: int, cap: int) -> list[Hit]:
    """Greedy per-document cap. Without it, one long document can fill every slot and
    starve questions that need evidence from a second document. (MMR is the smoother
    generalisation: penalise similarity to chunks already chosen.)"""
    if not cap:
        return hits[:k]
    picked, skipped, n = [], [], Counter()
    for h in hits:
        if n[h.chunk.doc_id] < cap:
            picked.append(h)
            n[h.chunk.doc_id] += 1
        else:
            skipped.append(h)
        if len(picked) == k:
            break
    return (picked + skipped)[:k]


def rrf(lists: list[list[Hit]], rrf_k: int = 60) -> list[Hit]:
    votes: dict[str, float] = {}
    keep: dict[str, Hit] = {}
    for lst in lists:
        for r, h in enumerate(lst, start=1):
            cid = h.chunk.chunk_id
            votes[cid] = votes.get(cid, 0.0) + 1.0 / (rrf_k + r)
            keep.setdefault(cid, h)
    order = sorted(votes, key=lambda c: -votes[c])
    return [Hit(keep[c].chunk, votes[c], r) for r, c in enumerate(order, start=1)]


class Pipeline:
    def __init__(self, retriever: Retriever, llm=None, rerankers: dict | None = None, pos: dict | None = None):
        self.r, self.llm, self.rerankers = retriever, llm, rerankers or {}
        self.pos = pos if pos is not None else {c.chunk_id: i for i, c in enumerate(retriever.chunks)}  # shareable: a service builds one Pipeline per request
        self.trace: list[str] = []
        self.stats: Counter = Counter()  # per-step counters: <step>_ok / <step>_failed / what changed

    def _need_llm(self, what: str) -> None:
        if self.llm is None:
            raise RuntimeError(f"'{what}' needs an LLM (set ANTHROPIC_API_KEY or start Ollama)")

    def _safe(self, name: str, fn, default):
        """Query rewriting is an optimisation, never a dependency. If the LLM errors or
        returns junk, log it and carry on with the lists we already have."""
        try:
            out = fn()
            self.stats[f"{name}_ok"] += 1
            return out
        except Exception as e:
            self.stats[f"{name}_failed"] += 1
            self.trace.append(f"[{name} FAILED: {type(e).__name__}: {str(e)[:80]}] continuing without it")
            return default

    def _search(self, q: str, mode: str, cfg: SearchConfig) -> list[Hit]:
        self.trace.append(f"[{mode}] {q[:150]}")
        return self.r.search(q, mode, cfg.pool, cfg.include_deprecated, groups=cfg.groups)

    def search(self, query: str, cfg: SearchConfig | None = None) -> list[Hit]:
        cfg = cfg or SearchConfig()
        self.trace = []
        lists = [self._search(query, cfg.mode, cfg)]

        if cfg.expand:
            terms = expansion_terms(query, lists[0][:2], self.r.bm25,
                                   n_terms=3 if cfg.expand_ids_only else 6, ids_only=cfg.expand_ids_only)
            if terms:
                lists.append(self._search(query + " " + " ".join(terms), "sparse", cfg))
        if cfg.decompose:
            self._need_llm("decompose")
            subs = self._safe("decompose", lambda: decompose(query, self.llm), [])
            if any(sq != query for sq in subs):
                self.stats["decompose_rewrote"] += 1
            for sq in subs:
                if sq != query:
                    lists.append(self._search(sq, cfg.mode, cfg))
        if cfg.hyde:
            self._need_llm("hyde")
            doc = self._safe("hyde", lambda: hyde(query, self.llm), "")
            if doc:
                lists.append(self._search(doc, "dense", cfg))

        fused = rrf(lists)

        if cfg.iterative:
            self._need_llm("iterative")
            # Hop 2 looks for MISSING evidence, so it must not compete in a vote against hop 1
            # (RRF rewards agreement, and hop 1's documents appear in every list). Instead:
            # keep only follow-up hits from documents hop 1 did not already cover, and reserve
            # a few result slots ("evidence budget") for the best of them.
            seen = {h.chunk.doc_id for h in fused[:cfg.k]}
            novel: list[Hit] = []
            fqs = self._safe("iterative", lambda: followup_queries(query, fused, self.llm), [])
            if fqs:
                self.stats["iterative_asked_followup"] += 1
            for fq in fqs:
                for h in self._search(fq, cfg.mode, cfg):
                    if h.chunk.doc_id not in seen and all(h.chunk.chunk_id != n.chunk.chunk_id for n in novel):
                        novel.append(h)
            if novel:
                self.stats["iterative_found_new_documents"] += 1
            budget = min(len(novel), max(1, cfg.k // 3))
            reserved = {h.chunk.chunk_id for h in novel[:budget]}
            tail = [h for h in fused[cfg.k - budget:] if h.chunk.chunk_id not in reserved]
            fused = fused[:cfg.k - budget] + novel[:budget] + tail

        if cfg.rerank:
            reranker = self.rerankers.get(cfg.rerank)
            if reranker is None:
                raise RuntimeError(f"reranker '{cfg.rerank}' is not available")
            pool = fused[:cfg.pool]
            cands = [(self.pos[h.chunk.chunk_id], h.chunk) for h in pool]
            self.trace.append(f"[rerank:{cfg.rerank}] {len(cands)} candidates")
            ranked = self._safe("rerank", lambda: reranker.rerank(query, cands), None)
            if ranked:
                new = [Hit(self.r.chunks[i], s, r) for r, (i, s) in enumerate(ranked, 1)]
                if new and fused and new[0].chunk.chunk_id != fused[0].chunk.chunk_id:
                    self.stats["rerank_changed_top1"] += 1
                fused = new

        return [Hit(h.chunk, h.score, r) for r, h in enumerate(diversify(fused, cfg.k, cfg.max_per_doc), 1)]
