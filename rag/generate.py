"""Stage 5: GENERATION.

Retrieval found passages; now the LLM writes the answer. Everything that makes
answers trustworthy happens in the prompt:

  1. ground:    "answer ONLY from the passages"
  2. cite:      number every passage, ask for [1]-style citations
  3. abstain:   give the model an explicit, exact way to say "not found"
  4. conflicts: tell it how to resolve stale-vs-current evidence
  5. exactness: numbers must be copied, never rounded or recalled

Backends: anthropic (Claude API), ollama (local model), extractive (no LLM,
just shows the best passage; lets the pipeline run with zero setup).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from .bm25 import tokenize
from .retrieve import Hit

ABSTAIN = "I don't have enough information in the provided documents to answer that."

SYSTEM_TEMPLATE = """You are a document question-answering assistant{org}.

Rules:
1. Answer ONLY using the numbered context passages. Never use outside knowledge, even if you know the answer.
2. Cite the passages you used with bracketed numbers like [1] or [2][3] right after each claim.
3. If the passages do not contain the answer, reply exactly: "{abstain}" and nothing else.
4. Copy numbers, dates, names and IDs exactly as written. Do not round or reinterpret them.
5. If passages disagree, prefer the one with status=current and the more recent date, and briefly mention the discrepancy.
6. Be concise: 1-4 sentences unless the question needs a list.{extra}"""


def build_system(org: str = "", extra_rules: str = "") -> str:
    """org: e.g. ' for Acme Corp'. extra_rules: domain rules, e.g. '7. Fiscal year runs April-March.'"""
    return SYSTEM_TEMPLATE.format(org=org, abstain=ABSTAIN, extra=("\n" + extra_rules) if extra_rules else "")


SYSTEM = build_system()


def format_context(hits: list[Hit]) -> str:
    parts = []
    for i, h in enumerate(hits, start=1):
        c = h.chunk
        parts.append(f"[{i}] (source: {c.source} | section: {c.section or c.title} | status: {c.status})\n{c.embed_text}")
    return "\n\n".join(parts)


def _anthropic(system: str, user: str, model: str | None = None) -> str:
    import anthropic
    client = anthropic.Anthropic()
    model = model or os.environ.get("LUMORA_MODEL", "claude-sonnet-5-5")
    msg = client.messages.create(model=model, max_tokens=700, temperature=0,
                                 system=system, messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in msg.content if b.type == "text")


_last_call = [0.0]
_pace_lock = threading.Lock()


def _pace(gap: float) -> None:
    """Space API calls at least `gap` seconds apart, even when many request threads call at once: each caller
    RESERVES the next free time slot under a lock, then sleeps outside it."""
    with _pace_lock:
        now = time.time()
        start = max(now, _last_call[0] + gap)
        _last_call[0] = start
    if start > now:
        time.sleep(start - now)


def _api(system: str, user: str, model: str | None = None) -> str:
    """Any OpenAI-compatible chat endpoint. Free tiers that speak this protocol:
         Google Gemini  LUMORA_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
         Groq           LUMORA_API_BASE=https://api.groq.com/openai/v1
         OpenRouter     LUMORA_API_BASE=https://openrouter.ai/api/v1   (models ending in ':free')
       plus LUMORA_API_KEY=<your key> and LUMORA_API_MODEL=<model name from the provider's list>.
       Free tiers are rate limited: LUMORA_API_RPM (default 10) spaces calls, and 429 replies are
       retried with backoff. The disk cache means each distinct prompt is only ever paid for once."""
    import time
    base = os.environ.get("LUMORA_API_BASE", "").rstrip("/")
    key = os.environ.get("LUMORA_API_KEY", "")
    model = model or os.environ.get("LUMORA_API_MODEL")
    if not (base and key and model):
        raise RuntimeError("api backend needs LUMORA_API_BASE, LUMORA_API_KEY and LUMORA_API_MODEL")
    gap = 60.0 / float(os.environ.get("LUMORA_API_RPM", "10"))
    payload = {"model": model, "temperature": 0,
                       "max_tokens": int(os.environ.get("LUMORA_API_MAX_TOKENS", "4096")),  # thinking models spend part of this on hidden reasoning
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}
    if os.environ.get("LUMORA_API_REASONING"):  # e.g. "low": less hidden thinking = faster, cheaper on quota
        payload["reasoning_effort"] = os.environ["LUMORA_API_REASONING"]
    body = json.dumps(payload).encode()
    timeout = float(os.environ.get("LUMORA_API_TIMEOUT", "300"))
    for attempt in range(6):
        _pace(gap)
        req = urllib.request.Request(base + "/chat/completions", body,
                                     {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            data = json.load(urllib.request.urlopen(req, timeout=timeout))
            text = data["choices"][0]["message"].get("content") or ""
            if not text.strip():  # e.g. the whole budget went on reasoning; raising keeps it out of the cache
                raise RuntimeError("api backend: empty reply (raise LUMORA_API_MAX_TOKENS or use a non-thinking model)")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", "replace")
            flat = detail.lower().replace(" ", "").replace("_", "")
            # Google words BOTH per-minute and per-day limits as "exceeded your current quota". Only a daily
            # or zero quota is hopeless; a per-minute limit clears if we wait, so wait and retry.
            hard = e.code == 429 and ("perday" in flat or "limit:0" in flat)
            if hard:
                raise RuntimeError(f"api backend: HTTP 429, DAILY quota used up for model '{model}' (try another model, "
                                   f"or retry tomorrow): {detail}") from None
            if e.code == 429:
                import re as _re
                m = _re.search(r"retry(?:delay)?\W+(?:in\W+)?\"?(\d+(?:\.\d+)?)\s*s", detail, _re.I)
                pause = min(90.0, float(m.group(1)) + 1 if m else 15.0 * (attempt + 1))
                if attempt < 5:
                    time.sleep(pause)
                    continue
            if e.code in (429, 500, 502, 503) and attempt < 5:
                time.sleep(min(60, 5 * 2 ** attempt))  # rate limited or overloaded: back off and retry
                continue
            raise RuntimeError(f"api backend: HTTP {e.code} for model '{model}': {detail}") from None
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if attempt < 2:  # transient network trouble or a slow reply: try again
                continue
            raise RuntimeError(f"api backend: no reply from model '{model}' after {attempt + 1} tries ({type(e).__name__}); "
                               "raise LUMORA_API_TIMEOUT or set LUMORA_API_REASONING=low") from None
    raise RuntimeError("api backend: retries exhausted")


_NOT_CHAT = ("embed", "nomic", "bge", "minilm", "code", "starcoder")  # embedding / code-completion models follow instructions badly


def pick_ollama_model(names: list[str]) -> str:
    """First pulled model that looks like a general chat model; code and embedding models are skipped
    (codellama as a grader returned garbage and was slow). Falls back to the first name."""
    chat = [n for n in names if not any(t in n.lower() for t in _NOT_CHAT)]
    return (chat or names)[0]


def _ollama(system: str, user: str, model: str | None = None) -> str:
    base = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = model or os.environ.get("LUMORA_OLLAMA_MODEL")
    if not model:  # use whatever chat model the user has pulled
        tags = json.load(urllib.request.urlopen(f"{base}/api/tags", timeout=5))
        model = pick_ollama_model([m["name"] for m in tags["models"]])
    # num_ctx: Ollama's default window (often 4096) silently TRUNCATES long prompts, so a prompt
    # with many passages loses its start or end with no error. Ask for more room explicitly.
    opts = {"temperature": 0, "num_ctx": int(os.environ.get("LUMORA_OLLAMA_CTX", "8192"))}
    body = json.dumps({"model": model, "stream": False, "options": opts, "keep_alive": "30m",
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(f"{base}/api/chat", body, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=300))["message"]["content"]


def _ollama_up() -> bool:
    try:
        urllib.request.urlopen(os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/api/tags", timeout=1)
        return True
    except Exception:
        return False


def pick_backend(name: str = "auto") -> str:
    if name != "auto":
        return name
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("LUMORA_API_KEY"):
        return "api"
    if _ollama_up():
        return "ollama"
    return "extractive"


def is_abstention(text: str) -> bool:
    t = text.lower().replace("\u2019", "'")
    return "don't have enough information" in t or "do not have enough information" in t


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*$])")
_SEPARATOR = re.compile(r"\|?[\s:\-|]+\|?")


def _raw_units(text: str):
    header, prev_table = None, False
    for line in text.split("\n"):
        s = line.strip()
        if not s or _SEPARATOR.fullmatch(s):
            continue
        if s.startswith("|"):
            if not prev_table:
                header, prev_table = s, True
                continue
            yield s, header
            continue
        prev_table = False
        for sent in _SENT.split(re.sub(r"^[-*]\s+", "", s)):
            if sent.strip():
                yield sent.strip(), None


def _units(text: str):
    """Answer candidates: each table row (with its header) and each sentence of prose/bullets.
    A FAQ-style question line is glued to the answer after it; on its own it would match the
    user's question perfectly and say nothing."""
    raw = list(_raw_units(text))
    i = 0
    while i < len(raw):
        unit, header = raw[i]
        if unit.rstrip("* ").endswith("?") and i + 1 < len(raw):
            yield unit + " " + raw[i + 1][0], header
            i += 2
        else:
            yield unit, header
            i += 1


def extractive_answer(question: str, hits: list[Hit], idf: dict | None = None, max_units: int = 2) -> str:
    """A NO-LLM baseline generator: quote the sentences/table rows that best match the question,
    with [n] citations. It cannot paraphrase, add up, or combine facts, which is precisely what
    makes it a useful yardstick for what a real LLM adds."""
    q = set(tokenize(question))
    idf = idf or {}
    scored = []
    for rank, h in enumerate(hits, 1):
        for unit, header in _units(h.chunk.text):
            toks = set(tokenize((header + " " if header else "") + unit))
            score = sum(idf.get(t, 1.0) for t in q & toks) / (1 + 0.02 * len(toks))
            if score > 0:
                scored.append((score, rank, unit, header))
    if not scored:
        return ABSTAIN
    scored.sort(key=lambda t: -t[0])
    parts = [(f"{header}\n" if header else "") + f"{unit} [{rank}]" for _, rank, unit, header in scored[:max_units]]
    return "\n\n".join(parts)


def complete(system: str, user: str, backend: str = "auto", model: str | None = None) -> str:
    """Plain LLM call with no retrieval. Used for query rewriting and LLM reranking."""
    backend = pick_backend(backend)
    if backend == "anthropic":
        return _anthropic(system, user, model)
    if backend == "ollama":
        return _ollama(system, user, model)
    if backend == "api":
        return _api(system, user, model)
    raise RuntimeError("no LLM backend available (set ANTHROPIC_API_KEY or start Ollama)")


def cached(llm, path, tag: str = ""):
    """Wrap llm(system, user) with an on-disk cache (JSON lines, keyed by a hash of the prompt).

    Why: a slow local model makes a full evaluation take hours; stop it at any point and re-run,
    and every call already made returns instantly. Also makes runs repeatable: the same prompt
    gives the same reply, so you can change code AFTER the LLM step and compare fairly. Delete
    the file to force fresh answers. Failed calls (exceptions) are never cached."""
    import hashlib
    import json
    from pathlib import Path
    path = Path(path)
    store: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                store[r["k"]] = r["v"]
            except (json.JSONDecodeError, KeyError):
                pass  # a half-written last line from an interrupted run

    lock = threading.Lock()   # the service shares one cached llm across request threads

    def wrapper(system: str, user: str) -> str:
        k = hashlib.sha256(f"{tag}\x00{system}\x00{user}".encode("utf-8")).hexdigest()  # tag = backend:model
        with lock:
            if k in store:
                wrapper.hits += 1
                return store[k]
        v = llm(system, user)   # the slow network call runs OUTSIDE the lock
        with lock:
            store[k] = v
            wrapper.misses += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"k": k, "v": v}) + "\n")
        return v

    wrapper.hits = wrapper.misses = 0
    return wrapper


def make_llm(backend: str = "auto", model: str | None = None, cache: str | None = None):
    """Returns llm(system, user) -> str, or None when no real LLM is available.
    cache=<file> keeps every reply on disk (see `cached`)."""
    b = pick_backend(backend)
    if b == "extractive":
        return None
    llm = lambda system, user: complete(system, user, b, model)  # noqa: E731
    if not cache:
        return llm
    shown = model or {"api": os.environ.get("LUMORA_API_MODEL"), "anthropic": os.environ.get("LUMORA_MODEL", "claude-sonnet-5-5"),
                      "ollama": os.environ.get("LUMORA_OLLAMA_MODEL")}.get(b) or ""
    return cached(llm, cache, tag=f"{b}:{shown}")


def make_judge_llm(backend: str = "auto", cache: str | None = None):
    """LLM for grading. Set LUMORA_JUDGE_MODEL to a DIFFERENT (ideally stronger) model than the
    generator, otherwise the judge favours its own style (self-preference bias)."""
    return make_llm(backend, os.environ.get("LUMORA_JUDGE_MODEL"), cache)


def answer(question: str, hits: list[Hit], backend: str = "auto", system: str = SYSTEM, idf: dict | None = None, llm=None) -> tuple[str, str]:
    """Returns (answer_text, backend_used). If `llm` (a possibly cached llm(system, user)) is given, it is used for generation."""
    backend = pick_backend(backend)
    if not hits:
        return ABSTAIN, backend
    user = f"Context passages:\n\n{format_context(hits)}\n\nQuestion: {question}"
    if llm is not None and backend != "extractive":
        return llm(system, user), backend
    if backend == "anthropic":
        return _anthropic(system, user), backend
    if backend == "api":
        return _api(system, user), backend
    if backend == "ollama":
        return _ollama(system, user), backend
    return extractive_answer(question, hits, idf), "extractive"
