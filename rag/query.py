"""Stage 7: QUERY TRANSFORMATION.

Users type questions; documents contain statements. The two rarely share
vocabulary, and some questions need several lookups. So we rewrite the query
before searching. Four techniques, cheapest first:

  expansion_terms   Pseudo-relevance feedback (no LLM). Search once, look at
                    the best chunks, and add their most distinctive terms
                    (especially identifiers like INC-2026-0312) to a second
                    search. Bridges documents that share an ID but not wording.
  decompose         LLM splits a multi-part question into separate searches.
  hyde              LLM writes a *fake answer*; we search with that. An answer
                    sounds like a document far more than a question does.
  followup_queries  Iterative / multi-hop: read what the first search found and
                    ask "what is still missing?" The LLM can then use names and
                    IDs it just discovered. This is the core idea of agentic RAG.

All LLM functions take `llm(system, user) -> str`, so they work with Claude,
Ollama, or a stub in tests.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable

from .bm25 import BM25, tokenize
from .textutil import last_json

LLM = Callable[[str, str], str]

_IDENTIFIER = re.compile(r"[a-z]+-\d|\d+-\d")  # ct-300, inc-2026-0312, 2026-03-31


def expansion_terms(query: str, hits, bm25: BM25, n_terms: int = 6, ids_only: bool = False) -> list[str]:
    """Pick the most distinctive terms from the top hits that the query lacks.

    ids_only=True keeps just identifier-like terms (INC-2026-0312, CT-300). Plain words
    cause "query drift" (the expansion pulls the search toward the top hit's own topic);
    identifiers are precise cross-document links, so they drift much less."""
    qtoks = set(tokenize(query))
    scores: Counter = Counter()
    for h in hits:
        for term, tf in Counter(tokenize(h.chunk.embed_text)).items():
            if term in qtoks or term not in bm25.idf:
                continue
            w = (1 + math.log(tf)) * bm25.idf[term]
            if _IDENTIFIER.search(term):
                w *= 3  # identifiers are the best "join keys" between documents
            elif ids_only:
                continue
            scores[term] += w
    return [t for t, _ in scores.most_common(n_terms)]


def _json_list(text: str) -> list[str] | None:
    """None = the reply held no JSON array at all (a failure); [] = a valid empty answer."""
    arr = last_json(text, list)
    if arr is None:
        return None
    return [s.strip() for s in arr if isinstance(s, str) and s.strip()]


DECOMPOSE_SYSTEM = (
    "You help a search engine answer questions over company documents. "
    "Split the user's question into 1 to 3 short, self-contained search queries, one per fact "
    "that must be looked up. If one lookup is enough, return the question unchanged. "
    "Phrase queries the way a document would (technical terms, not chatty wording). "
    "Return ONLY a JSON array of strings."
)

FOLLOWUP_SYSTEM = (
    "You are helping answer a question by searching documents. You get the question and the "
    "passages found so far. If they already contain everything needed, return []. "
    "Otherwise return a JSON array of up to 2 short search queries for the MISSING information. "
    "Reuse exact identifiers, names and terms from the passages when they help link documents. "
    "Return ONLY the JSON array."
)

HYDE_SYSTEM = (
    "Write a short passage (2-3 sentences) that could appear in internal company documentation "
    "and would answer the user's question. Invent plausible specifics if needed; the text is only "
    "used for search and never shown to anyone. Return only the passage."
)


def decompose(question: str, llm: LLM, max_queries: int = 3) -> list[str]:
    out = _json_list(llm(DECOMPOSE_SYSTEM, question))
    if out is None:
        raise ValueError("decompose: reply contained no JSON array")  # surfaced by Pipeline as a FAILED step
    return out[:max_queries] or [question]


def hyde(question: str, llm: LLM) -> str:
    out = re.sub(r"<think>.*?</think>", "", llm(HYDE_SYSTEM, question), flags=re.S).strip()
    # a refusal or a stub is not a hypothetical answer; searching with it would be worse than not using HyDE
    if len(out) < 20 or re.match(r"(i\'m sorry|i am sorry|sorry,|i cannot|i can\'t|i can not|as an ai)", out.lower()):
        raise ValueError("hyde: reply was a refusal or too short to use")
    return out


def followup_queries(question: str, hits, llm: LLM, n_passages: int = 3, max_chars: int = 700) -> list[str]:
    ctx = "\n\n".join(f"[{i}] ({h.chunk.source}) {h.chunk.embed_text[:max_chars]}"
                      for i, h in enumerate(hits[:n_passages], 1))
    out = _json_list(llm(FOLLOWUP_SYSTEM, f"Question: {question}\n\nPassages found so far:\n{ctx}"))
    if out is None:
        raise ValueError("followup: reply contained no JSON array")
    return out[:2]
