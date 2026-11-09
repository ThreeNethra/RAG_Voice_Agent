"""The brain of a call: the document engine plus model-based follow-ups and spoken answers.

`CallAgent` extends the engine's VoiceAgent in two places:
  * resolve(): "and for Enterprise?" becomes a full question using the previous turn (model rewrite, guarded);
  * speak_form(): a long written answer becomes at most three short spoken sentences.
Both are guarded: a rewrite may not contain any number that is not in the source text, must be short and single-line,
and on any failure (no model, rate limit, odd output) the engine's deterministic behaviour is used instead.
"""
from __future__ import annotations

import re

from rag.voice import _FOLLOW, VoiceAgent, pack, sentences, to_speech

NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

FOLLOW_SYS = ("You rewrite a spoken follow-up into one self-contained question, using the previous exchange for the missing subject. "
              "Output only the question on one line, no quotes. If the follow-up is already self-contained, output it unchanged.")
SPEAK_SYS = ("You turn a written answer into what a person would say aloud on a phone call. Use at most three short sentences "
             "(under 60 words in total). Keep every number, name, unit and date exactly as written. Add nothing that is not in the answer. "
             "No lists, no markdown, no citations like [1]. If the answer says the information is not available, say that briefly.")


def numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUM.findall(text or "")}


def safe_followup(original: str, rewritten: str) -> str | None:
    r = (rewritten or "").strip().strip('"\'`')
    if not r or "\n" in r or len(r) > 300 or len(r.split()) < 2:
        return None
    if r.lower().startswith(("i cannot", "i can't", "sorry", "as an ai")):
        return None
    if not numbers(r) <= numbers(original):                              # a rewrite may only repeat numbers it was given
        return None
    return r


def safe_speech(answer: str, rewritten: str) -> str | None:
    r = re.sub(r"\s+", " ", (rewritten or "")).strip()
    if not r or len(r) > 520 or re.search(r"\[\d+\]|[*#`|]", r):
        return None
    if not numbers(r) <= numbers(answer):
        return None
    return r


class CallAgent(VoiceAgent):
    def __init__(self, service, spoken_rewrite=True, followup_rewrite=True, **kw):
        super().__init__(service, **kw)
        self.spoken_rewrite, self.followup_rewrite = spoken_rewrite, followup_rewrite
        self.model_calls = 0

    def _llm(self, system: str, user: str) -> str | None:
        if not self.service.llm:
            return None
        try:
            self.model_calls += 1
            return self.service.llm(system, user)
        except Exception:                         # a rate limit or outage must never end a call: fall back
            return None

    def resolve(self, sess, question: str):
        if not (sess.last_question and _FOLLOW.match(question)):
            return question, False
        if self.followup_rewrite:
            raw = self._llm(FOLLOW_SYS, f"Previous question: {sess.last_question}\nPrevious answer: {sess.last_spoken[:400]}\nFollow-up: {question}")
            ok = safe_followup(sess.last_question + " " + sess.last_spoken + " " + question, raw or "")
            if ok:
                return ok, True
        return super().resolve(sess, question)

    def speak_form(self, res: dict, question: str):
        body, rest = to_speech(res["answer"])
        # already short: say it as it is. Only longer answers are worth a model call.
        if not (self.spoken_rewrite and (rest or len(body) > 200)):
            return body, rest
        raw = self._llm(SPEAK_SYS, f"Question: {question}\n\nWritten answer:\n{res['answer']}")
        ok = safe_speech(res["answer"], raw or "")
        if not ok:
            return body, rest
        short, extra = to_speech(ok)
        if extra:                                             # the model ignored the length limit: keep the deterministic version
            return body, rest
        return short, [s for s in sentences(res["answer"])]    # "more" then reads the full written answer in pieces
