"""Stage 8: ANSWER EVALUATION (deterministic checks; the LLM judge lives in judge.py).

Retrieval metrics say whether the evidence was found. These say what the system did with it.
Per answerable question:
  correct        every required fact appears in the answer (answer_facts, or facts if absent;
                 "a|b" = alternatives) and the system did not refuse
  over_refusal   it refused although the answer was in the corpus
  cites_valid    it cited at least one passage and every cited number exists
  cites_gold     at least one cited passage comes from a document that really holds the answer
  ungrounded     numbers in the answer that appear in NO retrieved passage (cheap hallucination
                 alarm; legitimately derived numbers must be listed in answer_facts)
Per unanswerable question:
  abstained      correct behaviour;   hallucinated = it answered anyway (the worse failure)

Remember what these cannot see: a fluent answer built on the wrong (stale) document passes every
check here except `correct`, and a hallucinated claim with no number in it passes all of them.
That is why judge.py and the calibration set exist.
"""
from __future__ import annotations

import re

from .judge import judge_correctness, judge_faithfulness
from .textutil import _CITE, _NUM, _num, numbers  # noqa: F401  (re-exported for callers and tests)


def norm(s: str) -> str:
    return re.sub(r"[*,\s]+", " ", s.lower().replace("’", "'")).replace(" ", "")


def fact_present(fact: str, text: str) -> bool:
    t = norm(text)
    return any(norm(a) and norm(a) in t for a in fact.split("|"))



def cited_ids(text: str) -> list[int]:
    ids: list[int] = []
    for m in _CITE.finditer(text):
        ids += [int(x) for x in re.split(r"\s*,\s*", m.group(1))]
    return ids


def ungrounded_numbers(answer: str, context: str, allowed_text: str = "") -> list[str]:
    known = numbers(context) | numbers(allowed_text)
    return sorted(n for n in numbers(answer) if n not in known)


def answer_facts(q: dict) -> list[str]:
    return q.get("answer_facts", q["facts"])


def score_one(q: dict, res, llm=None) -> dict:
    gold, facts = set(q["gold"]), answer_facts(q)
    out: dict = {"id": q["id"], "type": q["type"], "answerable": bool(gold), "abstained": res.abstained}
    if gold:
        found = [fact_present(f, res.text) for f in facts]
        out.update(fact_recall=sum(found) / len(found), correct=(not res.abstained) and all(found),
                   over_refusal=res.abstained)
    else:
        out.update(correct=res.abstained, hallucinated=not res.abstained)
    if not res.abstained:
        ids = cited_ids(res.text)
        n = len(res.hits)
        out["cites_valid"] = bool(ids) and all(1 <= i <= n for i in ids)
        if gold:
            out["cites_gold"] = any(1 <= i <= n and res.hits[i - 1].chunk.source in gold for i in ids)
        ctx = " ".join(h.chunk.embed_text for h in res.hits)
        out["ungrounded"] = ungrounded_numbers(res.text, ctx, " ".join(facts))
        if llm:
            f = judge_faithfulness(res.text, res.hits, llm)
            c = judge_correctness(q["question"], facts if gold else [], res.text, llm)
            out["judge_faith"] = f["score"] if f else None
            out["judge_unsupported"] = f["unsupported"] if f else None
            out["judge_overruled"] = f["overruled"] if f else None
            out["judge_correct"] = c["correct"] if c else None
    elif llm:
        c = judge_correctness(q["question"], facts if gold else [], res.text, llm)
        out["judge_correct"] = c["correct"] if c else None
    return out


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def aggregate(recs: list[dict]) -> dict:
    ans = [r for r in recs if r["answerable"]]
    una = [r for r in recs if not r["answerable"]]
    answered = [r for r in ans if not r["abstained"]]
    agg = {
        "n_answerable": len(ans), "n_unanswerable": len(una),
        "correct": mean(r["correct"] for r in ans),
        "fact_recall": mean(r["fact_recall"] for r in ans),
        "over_refusal": mean(r["over_refusal"] for r in ans),
        "cites_valid": mean(r["cites_valid"] for r in answered),
        "cites_gold": mean(r["cites_gold"] for r in answered),
        "numbers_grounded": mean(not r["ungrounded"] for r in answered),
        "abstained_ok": mean(r["abstained"] for r in una) if una else float("nan"),
        "hallucinated": mean(r["hallucinated"] for r in una) if una else float("nan"),
        "judge_faith": mean(r.get("judge_faith") for r in recs),
        "judge_correct": mean(r.get("judge_correct") for r in recs),
    }
    by_type: dict[str, list] = {}
    for r in ans:
        by_type.setdefault(r["type"], []).append(r["correct"])
    agg["correct_by_type"] = {t: mean(v) for t, v in by_type.items()}
    return agg


def deterministic_verdict(answer: str, hits, facts: list[str], gold: list[str]) -> dict:
    """What the cheap string-level checks conclude about one answer. Used to measure how much of
    the truth they can see (run_judge_check.py): faithful / correct / cite_ok are True, False or None."""
    from .generate import is_abstention
    abst = is_abstention(answer)
    if abst:
        return {"faithful": True, "correct": (not gold), "cite_ok": None, "ungrounded": []}
    ctx = " ".join(h.chunk.embed_text for h in hits)
    bad = ungrounded_numbers(answer, ctx, " ".join(facts))
    ids = cited_ids(answer)
    n = len(hits)
    cite_ok = None
    if gold:
        cite_ok = any(1 <= i <= n and hits[i - 1].chunk.source in set(gold) for i in ids)
    return {"faithful": not bad,
            "correct": bool(gold) and all(fact_present(f, answer) for f in facts),
            "cite_ok": cite_ok, "ungrounded": bad}
