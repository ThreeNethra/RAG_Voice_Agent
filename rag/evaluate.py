"""Stage 6: EVALUATION (retrieval side).

"It seems to work" is not engineering. We score the RETRIEVER on a golden set,
independent of any LLM, so we can change one thing (chunking, embedder, hybrid,
reranker) and see whether the number moves.

Metrics, per question (unanswerable questions are skipped here; they test the
generator's abstention instead):

  source_hit@k    did a gold source document appear in the top k chunks?
                  (mode "all" = every gold source must appear)
  fact_recall@k   what fraction of the required facts (e.g. "$89") appear
                  verbatim in the top-k text? This is the one that matters: if
                  the fact is not in the context, the LLM cannot answer.
  MRR             1 / rank of the first chunk from a gold source (rewards
                  putting the right evidence at the very top)
  stale_rate      did a deprecated/forbidden source appear in the top k?
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def load_golden(path: str | Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def _norm(s: str) -> str:
    return re.sub(r"[*,\s]+", " ", s.lower()).replace(" ", "")


def score_question(q: dict, hits) -> dict:
    srcs = [h.chunk.source for h in hits]
    gold = set(q["gold"])
    present = gold & set(srcs)
    hit = (present == gold) if q["mode"] == "all" else bool(present)
    blob = _norm(" ".join(h.chunk.embed_text for h in hits))
    facts = q["facts"]
    fact_recall = sum(_norm(f) in blob for f in facts) / len(facts)
    mrr = 0.0
    for r, s in enumerate(srcs, start=1):
        if s in gold:
            mrr = 1.0 / r
            break
    stale = any(s in set(q["not"]) for s in srcs)
    return {"hit": hit, "fact_recall": fact_recall, "mrr": mrr, "stale": stale}


def run(golden: list[dict], search_fn, k: int = 5, progress=None) -> dict:
    rows = []
    todo = [q for q in golden if q["gold"]]
    for n_done, q in enumerate(todo, 1):
        if progress:
            progress(n_done, len(todo))
        rows.append((q, score_question(q, search_fn(q["question"], k))))
    n = len(rows)
    agg = {
        "n": n,
        "source_hit": sum(r["hit"] for _, r in rows) / n,
        "fact_recall": sum(r["fact_recall"] for _, r in rows) / n,
        "mrr": sum(r["mrr"] for _, r in rows) / n,
        "stale_rate": sum(r["stale"] for _, r in rows) / n,
    }
    by_type: dict[str, list[float]] = {}
    for q, r in rows:
        by_type.setdefault(q["type"], []).append(r["fact_recall"])
    agg["fact_recall_by_type"] = {t: sum(v) / len(v) for t, v in by_type.items()}
    agg["failures"] = [(q["id"], q["question"], r["fact_recall"]) for q, r in rows if r["fact_recall"] < 1 or not r["hit"]]
    return agg
