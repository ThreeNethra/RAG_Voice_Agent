"""Stage 9: LLM-AS-JUDGE.

String matching can check "does the answer contain $1,950?" but not "is every claim in this
paragraph supported by the passages?". For that we use a second LLM call as a grader. Judges are
models, so they have known biases:

  leniency          they say "looks fine". (A small local model passed an invented "36 months",
                    a wrong sum and a flipped negation in our own calibration run.)
  verbosity bias    longer, confident answers score higher.
  self-preference   a model rates its own style higher: judge with a different, ideally stronger,
                    model than the generator (LUMORA_JUDGE_MODEL).
  constant output   a weak model may ignore the input and always answer the same thing.

Two defences are built in, because you cannot always have a bigger model:
  1. DON'T LET THE MODEL DO THE LOGIC. Correctness is a checklist (is each required fact stated?
     yes/no per fact) and CODE combines the answers. Small models are far better at one narrow
     yes/no than at a holistic verdict.
  2. DON'T TRUST THE VERDICT, VERIFY THE EVIDENCE. A claim only counts as supported if the quote
     the judge gives (a) really occurs in the passages and (b) contains the claim's numbers and
     names (or a number derivable from them). A lenient judge that says "supported" while quoting
     the wrong sentence is overruled in code.
What neither can do: catch a flipped negation or a subtle contradiction. That needs a competent
model, and `run_judge_check.py` tells you whether yours is one.

Two questions are judged separately, because they fail independently:
  FAITHFULNESS  is every claim supported by the retrieved passages?   (hallucination)
  CORRECTNESS   does the answer contain the right facts?              (usefulness)
An answer quoting a deprecated price list is faithful and wrong.
"""
from __future__ import annotations

import re

from .generate import format_context
from .textutil import derivable, last_json, numbers, words

FAITH_SYSTEM = """You are a strict fact-checker for a question-answering system.
You get numbered context passages and an ANSWER written from them.
1. List every distinct factual claim in the ANSWER. Ignore citation markers like [1].
2. For each claim decide whether the passages SUPPORT it: the passages state it, or it follows from them by simple arithmetic or logic. A claim that might be true in the real world but is not in the passages is NOT supported. Do not use outside knowledge. Compare numbers, names and negations literally.
3. For each claim copy the supporting evidence VERBATIM from the passages. The evidence must contain the claim's subject and every number and name the claim states; if one sentence is not enough, quote several separated by ' ... '. Leave evidence empty if nothing supports the claim.
If the ANSWER only says the information is not available, return {"claims": []}.
Return ONLY JSON: {"claims": [{"claim": "...", "evidence": "...", "supported": true}]}"""

CORRECT_SYSTEM = """You check an ANSWER against a list of REQUIRED FACTS for a QUESTION.
For each required fact decide whether the ANSWER states it. Wording may differ ('two years' states '24 months'). If a fact lists alternatives separated by ' | ', stating any one is enough.
Also decide: does the ANSWER contradict any required fact? Does the ANSWER refuse, i.e. say that the information is not available?
Return ONLY JSON: {"facts": [{"fact": "...", "stated": true}], "contradicts": false, "refuses": false}
If REQUIRED FACTS is (none), return an empty facts list and only fill in "refuses"."""


LAST_ERROR: list[str] = []  # why the most recent judge call returned None (shown by run_judge_check)


def _fail(why: str):
    LAST_ERROR.append(why)
    return None


def json_object(text: str) -> dict | None:
    return last_json(text, dict)


# ---- code-side verification of the judge's evidence -------------------------------------
_HYPHENATED = re.compile(r"\b[A-Za-z]+(?:-[A-Za-z0-9]+)+\b|\b[A-Za-z]+\d+[A-Za-z0-9]*\b")


def _identifiers(text: str) -> list[str]:
    """CT-300, BAT-24, FY2027, INC-2026-0312 are identifiers: they must appear in the passages as written.
    An ordinary hyphenated word ('annual-billing', 'well-known') is NOT: it is allowed to be a paraphrase
    ('billed annually'). Rule: an identifier contains a digit or starts with an ALL-CAPS part."""
    return [t for t in _HYPHENATED.findall(text)
            if any(ch.isdigit() for ch in t) or (t.split("-")[0].isupper() and len(t.split("-")[0]) >= 2)]


def claim_features(claim: str):
    """(numbers, names, identifiers) a quote must back up for the claim to count as supported."""
    found = _identifiers(claim)
    idents = {w.lower() for w in found}
    rest = claim
    for w in found:
        rest = rest.replace(w, " ")
    names = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z'’]*", rest):
        w, before = m.group(0), rest[:m.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?:;"
        if (len(w) >= 2 and w.isupper()) or (not sentence_start and w[0].isupper() and w[1:].islower()):
            names.add(w.lower())
    return numbers(rest), names, idents


def verify_claim(claim: str, evidence: str, context: str) -> tuple[bool, str]:
    ev = evidence.strip()
    if not ev:
        return False, "no evidence quoted"
    ew, cw = words(ev), set(words(context))
    if not ew or sum(w in cw for w in ew) / len(ew) < 0.8:
        return False, "quoted evidence does not appear in the passages"
    nums, names, idents = claim_features(claim)
    ev_nums = [float(x) for x in numbers(ev)]
    ctx_nums = {float(x) for x in numbers(context)}
    for n in nums:
        year_like = 1900 <= float(n) <= 2100 and "." not in n
        if float(n) in ev_nums or derivable(float(n), ev_nums):
            continue
        if year_like and float(n) in ctx_nums:  # a date mentioned in passing ("March 2026"), not a figure the quote must prove
            continue
        return False, f"number {n} is not in the quoted evidence"
    # Names only have to occur somewhere in the retrieved passages. Requiring them inside the quote was too strict:
    # real passages keep context (country, plan name, section) in headings and neighbouring lines, so a correct claim
    # like "Employees in the Netherlands get 25 days" quotes only the table row. An INVENTED name is still caught.
    for nm in names:
        if nm not in cw:
            return False, f"'{nm}' does not appear in the retrieved passages"
    for idt in idents:
        if not all(p in cw for p in words(idt)):
            return False, f"'{idt}' does not appear in the passages"
    return True, ""


def judge_faithfulness(answer: str, hits, llm) -> dict | None:
    """{"total","supported","score","unsupported","overruled","claims":[...]} or None if the judge failed.
    `overruled` = claims the judge called supported but whose evidence failed verification."""
    ctx = " ".join(h.chunk.embed_text for h in hits)
    try:
        raw = llm(FAITH_SYSTEM, f"Context passages:\n\n{format_context(hits)}\n\nANSWER:\n{answer}")
    except Exception as e:
        return _fail(f"model call failed: {type(e).__name__}: {str(e)[:300]}")
    out = json_object(raw)
    if not out or not isinstance(out.get("claims"), list):
        return _fail(f"reply was not the expected JSON: {raw[:200]!r}")
    rows = []
    for c in out["claims"]:
        if not isinstance(c, dict):
            continue
        claim, ev = str(c.get("claim", "")), str(c.get("evidence", ""))
        said = c.get("supported") is True
        ok, why = verify_claim(claim, ev, ctx) if said else (False, "judge said unsupported")
        rows.append({"claim": claim, "evidence": ev, "judge_supported": said, "supported": said and ok, "why": why})
    sup = [r["supported"] for r in rows]
    return {"total": len(rows), "supported": sum(sup), "score": sum(sup) / len(sup) if sup else 1.0,
            "unsupported": [r["claim"] for r in rows if not r["supported"]],
            "overruled": sum(r["judge_supported"] and not r["supported"] for r in rows), "claims": rows}


def judge_correctness(question: str, facts: list[str], answer: str, llm) -> dict | None:
    """{"correct","reason","facts","contradicts","refuses"} or None if the judge failed.
    The model only answers narrow yes/no questions; the verdict is computed here."""
    prompt = f"QUESTION: {question}\n\nREQUIRED FACTS: {' ; '.join(facts) if facts else '(none)'}\n\nANSWER:\n{answer}"
    try:
        raw = llm(CORRECT_SYSTEM, prompt)
    except Exception as e:
        return _fail(f"model call failed: {type(e).__name__}: {str(e)[:300]}")
    out = json_object(raw)
    if not out:
        return _fail(f"reply contained no JSON object: {raw[:200]!r}")
    rows, contradicts, refuses = out.get("facts"), out.get("contradicts"), out.get("refuses")
    if not isinstance(rows, list) or not isinstance(contradicts, bool) or not isinstance(refuses, bool):
        return _fail(f"JSON missing facts/contradicts/refuses as list/bool/bool: {raw[:200]!r}")
    if facts:
        # a judge that lists fewer facts than we asked about, or non-boolean verdicts, has failed
        if len(rows) < len(facts) or any(not isinstance(r, dict) or not isinstance(r.get("stated"), bool) for r in rows):
            return _fail(f"judge listed {len(rows)} of {len(facts)} required facts or gave non-boolean verdicts: {str(rows)[:200]}")
        correct = all(r["stated"] for r in rows) and not contradicts and not refuses
    else:
        correct = refuses  # unanswerable question: the right answer is a refusal
    missing = [str(r.get("fact", "")) for r in rows if isinstance(r, dict) and r.get("stated") is False]
    reason = ("refuses" if refuses else "") + (" contradicts a required fact" if contradicts else "") + \
             (f" missing: {'; '.join(missing)}" if missing else "")
    return {"correct": correct, "reason": reason.strip() or "ok", "facts": rows,
            "contradicts": contradicts, "refuses": refuses}
