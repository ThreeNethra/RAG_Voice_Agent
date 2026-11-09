"""Voice layer: makes the text RAG service something you can talk to.

The browser (or later a phone bridge) does the hearing and the speaking. THIS module is the part that stays the
same when the front door changes: it takes the words a caller said, decides whether they are a question or a
spoken command ("repeat that", "more", "goodbye"), resolves follow-ups ("and the Growth plan?"), asks the
existing Service (so access control, rate limits, budgets, caching and logging all apply unchanged) and turns
the written answer into something that sounds right when read aloud.

Nothing here needs a network, a model or a new dependency, so it is fully testable offline.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .service import Principal, Service, ServiceError

SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_SPOKEN_CHARS = 320        # about 20 seconds of speech: longer answers are served in pieces ("more")
MAX_SENTENCE_CHARS = 420      # one run-on sentence is cut at a comma instead of being read for a minute

GREETING = ("Hello, I'm an automated assistant for the company documents. This conversation may be logged. "
            "What would you like to know?")
ABSTAIN = ("I couldn't find that in the documents you have access to. Try asking it another way, "
           "or ask about something else.")
UNCLEAR = "Sorry, I didn't catch that. Could you say it again?"
GOODBYE = "Goodbye, thanks for asking."
THANKS = "You're welcome. Anything else I can help with?"
HELP = ("You can ask me questions about the company documents, like prices, policies or how something works. "
        "Say more to hear more, repeat to hear it again, sources to hear where it came from, or goodbye to finish.")
NOTHING_YET = "I haven't said anything yet."
NO_SOURCES = "I haven't looked anything up yet."
NO_MORE = "That's everything I have on that."

# ------------------------------------------------------------------ text -> speakable text
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]
_CUR = {"$": "dollars", "€": "euros", "₹": "rupees", "£": "pounds"}
_SCALE = {"k": "thousand", "m": "million", "b": "billion", "bn": "billion"}
_PER = {"mo": "month", "month": "month", "yr": "year", "year": "year", "user": "user", "seat": "seat", "hr": "hour",
        "hour": "hour", "day": "day", "min": "minute", "minute": "minute", "gb": "gigabyte", "request": "request"}

_CITE = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
_MONEY = re.compile(r"([$€₹£])\s?(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)(bn|[kKmMbB])?(?![A-Za-z0-9]|,\d)")   # also 1,55,000 (Indian digit grouping)
_RATE = re.compile(r"/\s?(mo|month|yr|year|user|seat|hr|hour|day|min|minute|gb|GB|request)\b", re.I)
_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_URL = re.compile(r"https?://\S+|www\.\S+")


def _money(m: re.Match) -> str:
    sym, num, scale = m.group(1), m.group(2), (m.group(3) or "").lower()
    if re.search(r",\d{2}(?!\d)", num):      # Indian grouping (1,55,000): voices misread it, a plain number is read correctly
        num = num.replace(",", "")
    return f"{num} {_SCALE[scale] + ' ' if scale else ''}{_CUR[sym]}"


def _date(m: re.Match) -> str:
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{_MONTHS[mo - 1]} {d}, {y}" if 1 <= mo <= 12 and 1 <= d <= 31 else m.group(0)


def _cells(line: str) -> list[str]:
    parts = [c.strip() for c in line.strip().strip("|").split("|")]
    while parts and not parts[-1]:
        parts.pop()
    return parts


def _tables_to_sentences(lines: list[str]) -> list[str]:
    """A markdown table read aloud as 'pipe Base price pipe...' is unusable. Pair each row with the header instead:
    '| Base price | $490 | $1,950 |' under '| Feature | Starter | Growth |' becomes 'Base price: Starter $490, Growth $1,950.'"""
    out, i = [], 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            out.append(lines[i]); i += 1; continue
        j = i
        while j < len(lines) and lines[j].lstrip().startswith("|"):
            j += 1
        rows = [_cells(r) for r in lines[i:j] if not _TABLE_SEP.match(r.strip())]
        rows = [r for r in rows if r]
        has_header = len(rows) >= 2 and any(_TABLE_SEP.match(r.strip()) for r in lines[i:j])
        has_header = has_header or (len(rows) >= 2 and len(rows[0]) == len(rows[1]) and not re.search(r"\d", " ".join(rows[0])))
        head = rows[0] if has_header else None
        for r in (rows[1:] if has_header else rows):
            if len(r) == 1:
                out.append(r[0])
            elif head and len(head) > 2:
                out.append(f"{r[0]}: " + ", ".join(f"{h} {v}" for h, v in zip(head[1:], r[1:]) if v))
            else:
                out.append(f"{r[0]}: " + ", ".join(v for v in r[1:] if v))
        i = j
    return out


def _clean_line(s: str) -> str:
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s)                  # headings
    s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", s)           # bullets and numbered items
    s = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", s)       # [text](url) -> text
    s = _URL.sub("a link", s)
    s = re.sub(r"[*_`]{1,3}(?=\S)([^*_`]+?)(?<=\S)[*_`]{1,3}", r"\1", s)   # **bold**, *it*, `code`
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s*\|\s*", ", ", s)                          # a stray pipe outside a table ("Severity: SEV-1 | Duration: 3h")
    s = re.sub(r"\*{2,}|_{2,}|`", "", s)                     # unbalanced emphasis left by a truncated passage
    return s.strip()


def _speakable(s: str) -> str:
    s = _MONEY.sub(_money, s)
    s = _RATE.sub(lambda m: " per " + _PER[m.group(1).lower()], s)
    s = _DATE.sub(_date, s)
    s = re.sub(r"(\d)\s?%", r"\1 percent", s)
    for a, b in ((r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"), (r"\bvs\.?(?=\s)", "versus"), (r"\betc\.", "and so on"),
                 (r"\bapprox\.", "approximately"), (r"\bN/A\b", "not applicable"), (r"\bw/(?=\s)", "with")):
        s = re.sub(a, b, s, flags=re.I)
    s = re.sub(r"\band/or\b", "or", s, flags=re.I)
    s = re.sub(r"\s/\s|(?<=[A-Za-z])/(?=[A-Za-z])", " or ", s)
    s = re.sub(r"\s\+\s", " plus ", s)
    s = s.replace('"', "").replace("\u201c", "").replace("\u201d", "")
    s = s.replace("&", " and ").replace("~", "about ").replace("≥", "at least ").replace("≤", "at most ").replace("→", " to ")
    s = re.sub(r"\s*[—–]\s*", ", ", s)
    s = re.sub(r"\s*\(\s*", ", ", s)
    s = re.sub(r"\s*\)", ",", s)
    s = re.sub(r"\s*,(\s*,)+", ",", s)
    s = re.sub(r",\s*([.!?:;])", r"\1", s)
    s = re.sub(r"\s+([.,!?;:])", r"\1", s)
    return re.sub(r"\s+", " ", s).strip(" ,")


def _wrap_long(sentence: str, limit: int = MAX_SENTENCE_CHARS) -> list[str]:
    if len(sentence) <= limit:
        return [sentence]
    out, cur = [], ""
    for piece in re.split(r"(?<=[,;])\s+", sentence):
        if cur and len(cur) + len(piece) + 1 > limit:
            out.append(cur.rstrip(",;") + "."); cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    if cur:
        out.append(cur)
    return [x for piece in out for x in ([piece] if len(piece) <= limit * 1.5 else [piece[:limit].rsplit(" ", 1)[0] + ".", piece[limit:].lstrip()])]


def sentences(text: str) -> list[str]:
    """The written answer as a list of sentences that read well aloud."""
    text = _CITE.sub("", text or "")
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    raw = [ln for ln in text.splitlines() if ln.strip()]
    if any(not re.match(r"\s*Q:", ln) for ln in raw):                 # FAQ pages: the "Q:" line only repeats the question
        raw = [re.sub(r"^\s*A:\s*", "", ln) for ln in raw if not re.match(r"\s*Q:", ln)]
    lines = _tables_to_sentences(raw)
    sents: list[str] = []
    for ln in lines:
        ln = _speakable(_clean_line(ln))
        if not re.search(r"[A-Za-z0-9]", ln):
            continue
        ln = ln.rstrip(":;,") if ln[-1] in ":;," else ln      # "you should:" is a lead-in, not a sentence to end with a colon
        if ln[-1] not in ".!?":
            ln += "."
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", ln):
            sents.extend(_wrap_long(s.strip()))
    return [s for s in sents if re.search(r"[A-Za-z0-9]", s)]


def pack(sents: list[str], max_chars: int = MAX_SPOKEN_CHARS) -> tuple[list[str], list[str]]:
    """Take sentences until the spoken length is reached; always at least one. Returns (taken, rest)."""
    taken, used = [], 0
    for s in sents:
        if taken and used + len(s) + 1 > max_chars:
            break
        taken.append(s); used += len(s) + 1
    return taken, sents[len(taken):]


def to_speech(text: str, max_chars: int = MAX_SPOKEN_CHARS) -> tuple[str, list[str]]:
    taken, rest = pack(sentences(text), max_chars)
    return " ".join(taken), rest


_ACRONYMS = {"faq", "hr", "sla", "api", "kpi", "okr", "gdpr", "dpa", "sso", "it", "cto", "ceo", "cfo", "ciso", "pii", "iot", "sdk"}


def humanize_source(name: str) -> str:
    """'13_regional_pricing_appendix.md' -> 'regional pricing appendix'"""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name or "")
    stem = re.sub(r"^\d+[_\-. ]+", "", stem)
    words = re.sub(r"[_\-]+", " ", stem).split()
    return " ".join(w.upper() if w.lower() in _ACRONYMS else w for w in words) or "the documents"


def source_phrase(names: list[str]) -> str:
    uniq = list(dict.fromkeys(humanize_source(n) for n in names))[:2]
    if not uniq:
        return ""
    return f"From the {uniq[0]}." if len(uniq) == 1 else f"From the {uniq[0]} and the {uniq[1]}."


# ------------------------------------------------------------------ spoken commands
_NOISE = {"uh", "um", "umm", "uhh", "hmm", "hm", "mm", "mhm", "er", "erm", "ah", "oh", "huh", "eh", "ok", "okay"}
_FILLER = {"please", "ok", "okay", "um", "uh", "hmm", "so", "well", "can", "could", "you", "would", "the", "just"}
_COMMANDS = {
    "end": {"goodbye", "good bye", "bye", "bye bye", "hang up", "end call", "end the call", "end this call", "that s all", "thats all",
            "that is all", "i m done", "im done", "that s it", "thats it", "nothing else", "thanks bye", "thank you bye",
            "thank you goodbye", "thanks goodbye", "no thanks", "no thank you"},
    "stop": {"stop", "cancel", "never mind", "nevermind", "quiet", "be quiet"},
    "repeat": {"repeat", "repeat that", "say that again", "say again", "what was that", "pardon", "come again", "sorry what",
               "what did you say", "repeat it", "again"},
    "more": {"more", "tell me more", "go on", "continue", "keep going", "what else", "more details", "more detail", "elaborate",
             "tell me the rest", "the rest", "yes", "yeah", "yep", "sure", "yes please"},
    "sources": {"sources", "source", "what are your sources", "where did you get that", "where is that from", "which document",
                "which documents", "what document", "where did that come from", "what s your source", "whats your source"},
    "greeting": {"hello", "hi", "hey", "hi there", "hello there", "hey there", "good morning", "good afternoon", "good evening"},
    "thanks": {"thanks", "thank you", "thank you very much", "thanks a lot", "cheers"},
    "help": {"help", "what can you do", "what can i ask", "what can i ask you", "how does this work"},
}


def classify(text: str) -> str | None:
    """A whole utterance that is a command, or None. Matching is on the WHOLE utterance, so 'stop payment fees' is a question."""
    t = " ".join(re.sub(r"[^\w\s']", " ", (text or "").lower()).replace("'", " ").split())
    if not t:
        return "unclear"
    for _ in range(2):                                   # strip leading/trailing politeness ('okay, repeat that please')
        w = t.split()
        if len(w) > 1 and w[0] in _FILLER:
            t = " ".join(w[1:])
        w = t.split()
        if len(w) > 1 and w[-1] == "please":
            t = " ".join(w[:-1])
    for intent, phrases in _COMMANDS.items():
        if t in phrases:
            return intent
    if not re.search(r"[A-Za-z]{2,}", t) or all(w in _NOISE for w in t.split()):
        return "unclear"
    return None


_FOLLOW = re.compile(r"^(and|also|then|but|so|what about|how about|what if|and what about|those|that|it|its|they|their|them|these|the same)\b", re.I)


# ------------------------------------------------------------------ sessions
@dataclass
class Session:
    last_question: str = ""
    last_spoken: str = ""
    rest: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    turns: int = 0
    touched: float = field(default_factory=time.time)


class SessionStore:
    """Per (user, session id), bounded and expiring. Keyed by the authenticated user so one caller can never read
    or continue another caller's conversation, even by guessing a session id."""

    def __init__(self, ttl_s: float = 1800.0, max_sessions: int = 1000):
        self.ttl, self.max, self.d, self.lock = ttl_s, max_sessions, OrderedDict(), threading.Lock()

    def get(self, user: str, sid: str) -> Session:
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.d.items() if now - v.touched > self.ttl]:
                del self.d[k]
            s = self.d.get((user, sid))
            if s is None:
                s = self.d[(user, sid)] = Session()
            s.touched = now
            self.d.move_to_end((user, sid))
            while len(self.d) > self.max:
                self.d.popitem(last=False)
            return s

    def drop(self, user: str, sid: str) -> None:
        with self.lock:
            self.d.pop((user, sid), None)

    def __len__(self):
        return len(self.d)


# ------------------------------------------------------------------ the agent
class VoiceAgent:
    def __init__(self, service: Service, sessions: SessionStore | None = None):
        self.service = service
        self.sessions = sessions or SessionStore()

    # ---- hooks: subclasses (the call server) replace these with model-based versions
    def resolve(self, sess: "Session", question: str) -> tuple[str, bool]:
        """Turn a follow-up into a standalone question. Default: only an explicit cue joins questions (gluing every short
        fragment onto the last topic would answer off-topic questions with the previous answer)."""
        if sess.last_question and _FOLLOW.match(question):
            return f"{sess.last_question[:300]} {question}", True
        return question, False

    def speak_form(self, res: dict, question: str) -> tuple[str, list[str]]:
        """(what to say now, what remains for 'more') from a service answer."""
        return to_speech(res["answer"])

    def _log(self, p: Principal, intent: str, sid: str, ms: int, text) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "voice", "user": p.user, "intent": intent, "latency_ms": ms,
               "session": sid[:8]}
        if self.service.cfg.log_questions and isinstance(text, str):
            rec["text"] = text[:300]
        try:
            self.service.req_log.write(rec)
        except Exception:   # a full disk must not break a conversation
            pass

    def turn(self, p: Principal, text, sid=None, event=None) -> dict:
        t0 = time.perf_counter()
        if sid is not None and (not isinstance(sid, str) or not SID_RE.match(sid)):
            raise ServiceError(400, "'session' must be 1-64 letters, digits, '-' or '_'")
        if event not in (None, "start"):
            raise ServiceError(400, "unknown 'event'")
        sid = sid or "default"
        if event != "start":
            if not isinstance(text, str):
                raise ServiceError(400, "'text' must be a string")
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                raise ServiceError(400, "'text' contains invalid characters") from None
            if len(text) > self.service.cfg.max_question_chars:
                raise ServiceError(400, f"text longer than {self.service.cfg.max_question_chars} characters")
        out = {"spoken": "", "intent": "answer", "end": False, "more": False, "rewritten": False, "sources": [],
               "request_id": None, "cache_hit": False, "llm_calls": 0, "session": sid}

        if event == "start":
            self.service.limiter.check(p.user)
            self.sessions.drop(p.user, sid)
            self.sessions.get(p.user, sid)
            out.update(spoken=GREETING, intent="greeting")
            return self._done(p, out, t0, text)

        sess = self.sessions.get(p.user, sid)
        intent = classify(text)
        if intent:
            self.service.limiter.check(p.user)       # commands cost no search, but they still count against the caller's rate
            return self._command(p, out, sess, intent, sid, t0, text)

        question, rewritten = self.resolve(sess, text.strip())
        res = self.service.ask(p, question)
        sess.turns += 1
        out.update(request_id=res["request_id"], cache_hit=res["cache_hit"], llm_calls=res["llm_calls"], rewritten=rewritten, searched_for=question)
        if res["abstained"]:
            sess.rest = []
            out.update(spoken=ABSTAIN, intent="abstain")
        else:
            body, rest = self.speak_form(res, question)
            cites = [c for c in res["citations"] if c["cited"]] or res["citations"][:1]
            sess.sources = list(dict.fromkeys(c["source"] for c in cites))
            sess.rest, sess.last_question = rest, question[:300]
            spoken = " ".join(x for x in (body, source_phrase(sess.sources), "Say more to hear the rest." if rest else "") if x)
            out.update(spoken=spoken, sources=[{"n": c["n"], "source": c["source"], "section": c["section"]} for c in cites])
        sess.last_spoken = out["spoken"]
        out["more"] = bool(sess.rest)
        return self._done(p, out, t0, text)

    def _command(self, p, out, sess, intent, sid, t0, text):
        out["intent"] = intent
        if intent == "end":
            out.update(spoken=GOODBYE, end=True)
            self.sessions.drop(p.user, sid)
        elif intent == "stop":
            out.update(spoken="Okay.")
        elif intent == "unclear":
            out.update(spoken=UNCLEAR)
        elif intent == "greeting":
            out.update(spoken="Hello. What would you like to know?")
        elif intent == "thanks":
            out.update(spoken=THANKS)
        elif intent == "help":
            out.update(spoken=HELP)
        elif intent == "repeat":
            out.update(spoken=sess.last_spoken or NOTHING_YET)
        elif intent == "sources":
            out.update(spoken=("I used " + " and ".join(f"the {humanize_source(s)}" for s in sess.sources[:3]) + ".") if sess.sources else NO_SOURCES)
        elif intent == "more":
            if sess.rest:
                taken, sess.rest = pack(sess.rest)
                out.update(spoken=" ".join(taken + (["Say more to keep going."] if sess.rest else [NO_MORE])))
                sess.last_spoken = out["spoken"]
            else:
                out.update(spoken=NO_MORE if sess.last_spoken else "What would you like to know?")
        out["more"] = bool(sess.rest)
        return self._done(p, out, t0, text)

    def _done(self, p, out, t0, text):
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        self._log(p, out["intent"], out["session"], out["latency_ms"], text)
        return out
