"""Stage 5b: THE FULL ANSWER PATH, with two ways to refuse before the generator even runs.

    question -> Pipeline.search -> hits
              -> GATE        retrieval-side: do the retrieved passages even cover the question's terms?
              -> SUFFICIENCY LLM-side: do the passages contain what is needed to answer?
              -> generate    grounded prompt (prompt-level refusal is the third line of defence)

Why three layers? Each catches different failures, and each has a different price:
  gate         free, instant, catches questions about things the corpus never mentions
               ("Series D"), but is fooled by near-misses ("price of the CT-400" shares words
               with real pages).
  sufficiency  costs an LLM call, understands "related but different", can still be fooled.
  prompt       free (same call as the answer), but the model is tempted to be helpful.

The gate uses IDF COVERAGE: the share of the question's information-bearing words (weighted
by rarity) that occur somewhere in the retrieved passages. Words the corpus has never seen
count as maximally rare, so "Series D" or "Singapore headcount" drag coverage down.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .bm25 import BM25, tokenize
from .generate import ABSTAIN, SYSTEM, _units, answer, is_abstention
from .judge import json_object
from .pipeline import Pipeline, SearchConfig

SUFFICIENCY_SYSTEM = (
    "You check whether retrieved passages are enough to answer a question. Say sufficient=true only if "
    "the passages explicitly contain the information needed to answer the WHOLE question. If a needed fact "
    "is missing, or the passages concern a related but different thing (another product, year, entity), "
    'say false. Return ONLY JSON: {"missing": "what is missing, or empty", "sufficient": true}'
)


def retrieval_confidence(question: str, hits, bm25: BM25, k: int = 5) -> dict:
    q = set(tokenize(question))
    oov_idf = math.log(1 + len(bm25.tf))
    w = {t: bm25.idf.get(t, oov_idf) for t in q}
    total = sum(w.values()) or 1.0

    def cov(chunks) -> float:
        seen: set[str] = set()
        for c in chunks:
            seen |= set(tokenize(c.embed_text))
        return sum(x for t, x in w.items() if t in seen) / total

    def unit_cov() -> float:
        """Tightest version: how much of the question does the best SINGLE sentence / table row
        (plus its heading breadcrumb) cover? An answer usually sits in one sentence."""
        best = 0.0
        for h in hits[:k]:
            crumb = set(tokenize(h.chunk.title + " " + h.chunk.section))
            for unit, header in _units(h.chunk.text):
                toks = set(tokenize((header + " " if header else "") + unit)) | crumb
                best = max(best, sum(x for t, x in w.items() if t in toks) / total)
        return best

    top = [h.chunk for h in hits[:k]]
    return {"coverage": cov(top), "coverage_top1": cov(top[:1]), "unit_coverage": unit_cov(),
            "oov": sum(t not in bm25.idf for t in q) / max(len(q), 1)}


def sufficiency_check(question: str, hits, llm) -> bool:
    """True = enough to answer. Fails OPEN (returns True) if the judge errors or returns junk:
    the prompt-level refusal still protects us, and wrongly refusing is the costlier error here."""
    from .generate import format_context
    try:
        out = json_object(llm(SUFFICIENCY_SYSTEM, f"Passages:\n\n{format_context(hits)}\n\nQuestion: {question}"))
    except Exception:
        return True
    if not out or not isinstance(out.get("sufficient"), bool):
        return True
    return out["sufficient"]


@dataclass
class AnswerConfig:
    search: SearchConfig = field(default_factory=SearchConfig)
    backend: str = "auto"
    gate: float | None = None       # abstain if the chosen coverage signal is below this
    gate_signal: str = "coverage"    # coverage | coverage_top1 | unit_coverage
    sufficiency: bool = False       # LLM sufficiency check before answering (needs an LLM)
    system: str = SYSTEM


@dataclass
class Result:
    question: str
    text: str
    hits: list
    abstained: bool
    reason: str
    backend: str
    confidence: dict


def run_rag(question: str, pipe: Pipeline, cfg: AnswerConfig | None = None, llm=None) -> Result:
    cfg = cfg or AnswerConfig()
    hits = pipe.search(question, cfg.search)
    conf = retrieval_confidence(question, hits, pipe.r.bm25)
    if not hits:
        return Result(question, ABSTAIN, hits, True, "no results", "none", conf)
    if cfg.gate is not None and conf[cfg.gate_signal] < cfg.gate:
        return Result(question, ABSTAIN, hits, True, f"gate: {cfg.gate_signal} {conf[cfg.gate_signal]:.2f} < {cfg.gate}", "gate", conf)
    if cfg.sufficiency:
        if llm is None:
            raise RuntimeError("'sufficiency' needs an LLM (set ANTHROPIC_API_KEY or start Ollama)")
        if not sufficiency_check(question, hits, llm):
            return Result(question, ABSTAIN, hits, True, "sufficiency check: not enough information", "sufficiency", conf)
    text, used = answer(question, hits, cfg.backend, cfg.system, idf=pipe.r.bm25.idf, llm=llm)
    return Result(question, text, hits, is_abstention(text), "generator", used, conf)
