"""Small text helpers shared by the evaluation and judge code."""
from __future__ import annotations

import json
import re

_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?")


def _num(tok: str) -> str:
    return f"{float(tok.replace('$', '').replace('%', '').replace(',', '').rstrip('.')):g}"


def numbers(text: str) -> set[str]:
    """Normalised numbers in a text ('$1,950' -> '1950', '15%' -> '15'), ignoring [n] citations."""
    return {_num(t) for t in _NUM.findall(_CITE.sub(" ", text))}


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("−", "-"))


def derivable(n: float, pool: list[float], tol: float = 0.005) -> bool:
    """Can n be produced from two numbers in pool by + - * / or a percentage step?
    (490 and 15 give 563.5 because 490 * 1.15.) Keep the pool SMALL: with many numbers almost
    anything is 'derivable', which would let invented numbers through."""
    def close(x):
        return abs(x - n) <= tol * max(1.0, abs(n))
    for a in pool:
        for b in pool:
            cands = [a + b, a - b, a * b, a * (1 + b / 100), a * (1 - b / 100), a * b / 100]
            if b:
                cands.append(a / b)
            if any(close(v) for v in cands):
                return True
    return False


def last_json(text: str, want=dict):
    """Return the LAST top-level JSON value of type `want` found in a model's reply, or None.

    Real models wrap JSON in code fences, add chatter, or (reasoning models) think out loud
    first, sometimes with braces in the thinking. Taking the last complete top-level value is
    far more reliable than a greedy regex from the first '{' to the last '}'."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    dec, i, found = json.JSONDecoder(), 0, None
    while i < len(text):
        if text[i] in "{[":
            try:
                obj, end = dec.raw_decode(text, i)
            except json.JSONDecodeError:
                i += 1
                continue
            if isinstance(obj, want):
                found = obj
            i = end
            continue
        i += 1
    return found
