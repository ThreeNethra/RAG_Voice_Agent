"""The service layer: everything between "an HTTP request arrived" and "the RAG engine ran".

The engine answers questions. A SERVICE must also decide WHO may ask, WHAT they may see, HOW MUCH one
request may cost, WHETHER to reuse an earlier answer, and WHAT TO RECORD so that a bad answer can be
debugged next week. This file is framework-free on purpose (no Flask here), so every rule below is
unit-testable without a web server, and you could put FastAPI, a CLI or a Slack bot on top of it.

  authenticate   API key -> Principal(user, groups). Unknown key = no access. (Real systems use SSO / OIDC
                 tokens; the shape is the same: something proves identity, identity maps to groups.)
  ask            validate -> rate-limit -> cache lookup -> retrieve (ACL inside) -> generate -> cache -> log.
  cache          keyed by (question, the caller's GROUPS, index version, config). Without the groups in the
                 key, the first exec to ask a question would serve the board minutes to the next intern.
  budgets        a hard cap on LLM calls and wall-clock time per request. One slot is always reserved for the
                 final answer, so a greedy query-rewriting step can never starve generation.
  logging        one JSON line per request: who, what, how long, which chunks, how many LLM calls, cache hit.
  fail closed    if the index was built under different access rules than the ones on disk, refuse to start.
"""
from __future__ import annotations

import hashlib
import logging
import json
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

from .access import acl_fingerprint
from .answer_eval import cited_ids
from .answering import AnswerConfig, run_rag
from .bm25 import TOKENIZER_VERSION
from .generate import make_llm
from .pipeline import Pipeline, SearchConfig
from .retrieve import LLMReranker, Retriever
from .store import index_dir, load_index, sync_index


class ServiceError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user: str
    groups: tuple


@dataclass
class ServiceConfig:
    data: str = "data/lumora"
    strategy: str = "structure"
    embedder: str = "tfidf"
    backend: str = "extractive"       # extractive | api | anthropic | ollama
    k: int = 5
    techniques: dict = field(default_factory=dict)   # server-side retrieval techniques, e.g. {"hyde": True}. NOT user-controlled.
    sufficiency: bool = False
    max_llm_calls: int = 6            # per request, including the final answer
    request_timeout_s: float = 60.0
    max_question_chars: int = 1000
    cache_size: int = 256
    cache_ttl_s: float = 600.0
    rate_per_min: int = 30            # per user
    max_auth_failures_per_min: int = 20   # per client address; 0 = off. Behind a proxy the address is the proxy's: configure ProxyFix
    log_dir: str = "logs"
    log_questions: bool = True        # set False if questions may contain personal data
    llm_cache: str | None = "reports/service_llm_cache.jsonl"
    system: str | None = None         # replaces the generation system prompt (e.g. spoken-style rules for voice)


class Counter:
    def __init__(self, max_calls: int, timeout_s: float):
        self.calls, self.max, self.deadline = 0, max_calls, time.monotonic() + timeout_s


class BudgetedLLM:
    """Wraps llm(system, user). `reserve` slots stay available for later, more important calls."""
    def __init__(self, llm, counter: Counter, reserve: int = 0):
        self.llm, self.c, self.reserve = llm, counter, reserve

    def __call__(self, system: str, user: str) -> str:
        if self.c.calls + self.reserve >= self.c.max:
            raise BudgetExceeded("LLM call budget used up")
        if time.monotonic() > self.c.deadline:
            raise BudgetExceeded("request time budget used up")
        self.c.calls += 1
        return self.llm(system, user)


class ResponseCache:
    """LRU + TTL. Thread-safe. The CALLER builds keys; see Service._cache_key for what must be in them."""
    def __init__(self, size: int, ttl: float):
        self.size, self.ttl, self.d, self.lock = size, ttl, OrderedDict(), threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self.lock:
            v = self.d.get(key)
            if v is None or time.monotonic() - v[0] > self.ttl:
                self.d.pop(key, None)
                self.misses += 1
                return None
            self.d.move_to_end(key)
            self.hits += 1
            return v[1]

    def put(self, key, value):
        with self.lock:
            self.d[key] = (time.monotonic(), value)
            self.d.move_to_end(key)
            while len(self.d) > self.size:
                self.d.popitem(last=False)

    def clear(self):
        with self.lock:
            self.d.clear()


class RateLimiter:
    """Sliding window per user. Protects the model quota (and your bill) from one noisy client."""
    def __init__(self, per_min: int):
        self.per_min, self.w, self.lock = per_min, {}, threading.Lock()

    def check(self, user: str) -> None:
        now = time.monotonic()
        with self.lock:
            q = self.w.setdefault(user, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_min:
                raise ServiceError(429, "rate limit exceeded", retry_after=max(1.0, 60 - (now - q[0])))
            q.append(now)


class FailureLimiter:
    """Throttle failed logins per client address, so API keys cannot be guessed at full speed.
    Over the limit, EVERY attempt from that address (even with a valid key) gets 429 until the window clears."""
    def __init__(self, per_min: int):
        self.per_min, self.w, self.lock = per_min, {}, threading.Lock()

    def _trim(self, q, now):
        while q and now - q[0] > 60:
            q.popleft()

    def check(self, client) -> None:
        if client is None or self.per_min <= 0:
            return
        now = time.monotonic()
        with self.lock:
            q = self.w.get(client)
            if q is None:
                return
            self._trim(q, now)
            if len(q) >= self.per_min:
                raise ServiceError(429, "too many failed authentication attempts", retry_after=max(1.0, 60 - (now - q[0])))

    def fail(self, client) -> None:
        if client is None or self.per_min <= 0:
            return
        now = time.monotonic()
        with self.lock:
            q = self.w.setdefault(client, deque())
            self._trim(q, now)
            q.append(now)
            if len(self.w) > 10000:   # bounded memory under a spray of source addresses
                for k in [k for k, v in self.w.items() if not v or now - v[-1] > 60][:5000]:
                    self.w.pop(k, None)


class JsonLog:
    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False)
        # backslashreplace: a lone surrogate in a hostile question must not make the write (and the request) fail
        with self.lock, self.path.open("a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(line + "\n")


class Service:
    def __init__(self, root, cfg: ServiceConfig | None = None, llm=None):
        self.root, self.cfg = Path(root), cfg or ServiceConfig()
        self.data = self.root / self.cfg.data
        users = self.data / "users.json"
        self.keys = json.loads(users.read_text(encoding="utf-8")).get("api_keys", {}) if users.exists() else {}
        self.cache = ResponseCache(self.cfg.cache_size, self.cfg.cache_ttl_s)
        self.limiter = RateLimiter(self.cfg.rate_per_min)
        self.auth_failures = FailureLimiter(self.cfg.max_auth_failures_per_min)
        self._reindex_lock = threading.Lock()
        log_dir = self.root / self.cfg.log_dir
        self.req_log, self.fb_log = JsonLog(log_dir / "requests.jsonl"), JsonLog(log_dir / "feedback.jsonl")
        self.recent: OrderedDict[str, str] = OrderedDict()   # request_id -> user, so feedback can't be spoofed
        self._lock = threading.Lock()
        self.started = time.time()
        if llm is not None:
            self.llm = llm
        elif self.cfg.backend == "extractive":
            self.llm = None
        else:
            self.llm = make_llm(self.cfg.backend, cache=str(self.root / self.cfg.llm_cache) if self.cfg.llm_cache else None)
        self.load()

    # ------------------------------------------------------------------ index lifecycle
    def load(self) -> None:
        d = index_dir(self.root, self.data, self.cfg.strategy, self.cfg.embedder)
        if not (d / "meta.json").exists():
            raise RuntimeError(f"no index at {d}: run  python manage.py index")
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta.get("tokenizer") != TOKENIZER_VERSION:
            raise RuntimeError("the index was built with an older text-processing version: "
                               f"run  python manage.py index  and restart")
        current = acl_fingerprint(self.data / "corpus")
        if meta.get("acl_fingerprint") != current:
            # The index carries permissions copied from acl.json at build time. If the file changed since,
            # serving would apply OLD permissions. Refuse rather than guess.
            raise RuntimeError("the index was built under different access rules than acl.json now has: "
                               f"run  python manage.py index  and restart")
        chunks, vectors, emb = load_index(d)
        retriever = Retriever(chunks, vectors, emb)
        version = meta.get("acl_fingerprint", "") + ":" + hashlib.sha256("".join(meta.get("chunk_hashes", ())).encode()).hexdigest()[:10]
        with self._lock:   # one atomic swap: in-flight requests finish on the old objects
            self.retriever, self.pos, self.version = retriever, {c.chunk_id: i for i, c in enumerate(chunks)}, version
        self.cache.clear()

    def reindex(self) -> dict:
        with self._reindex_lock:   # sync_index swaps directories: two at once would corrupt each other
            rep = sync_index(self.root, self.data, self.cfg.strategy, self.cfg.embedder)
            self.load()
        return rep

    # ------------------------------------------------------------------ auth
    def authenticate(self, api_key: str | None, client: str | None = None) -> Principal:
        self.auth_failures.check(client)
        rec = self.keys.get(api_key or "")
        if not rec:
            self.auth_failures.fail(client)
            raise ServiceError(401, "missing or invalid API key")
        return Principal(rec["user"], tuple(rec.get("groups", ())))

    # ------------------------------------------------------------------ ask
    def _cache_key(self, p: Principal, question: str):
        # EVERYTHING that can change the answer must be in the key: the text, WHO is asking (their groups
        # decide which documents exist for them), which index version, and the server configuration.
        c = self.cfg
        return (re.sub(r"\s+", " ", question.strip().lower()), tuple(sorted(p.groups)), self.version,
                c.backend, c.k, tuple(sorted(c.techniques.items())), c.sufficiency)

    def ask(self, p: Principal, question) -> dict:
        t0 = time.perf_counter()
        rid = uuid.uuid4().hex[:12]
        status, err, payload, llm_calls, cache_hit = 200, None, None, 0, False
        try:
            if not isinstance(question, str) or not question.strip():
                raise ServiceError(400, "'question' must be a non-empty string")
            try:
                question.encode("utf-8")
            except UnicodeEncodeError:
                raise ServiceError(400, "'question' contains invalid characters") from None
            if len(question) > self.cfg.max_question_chars:
                raise ServiceError(400, f"question longer than {self.cfg.max_question_chars} characters")
            self.limiter.check(p.user)
            key = self._cache_key(p, question)
            cached = self.cache.get(key)
            if cached is not None:
                payload, cache_hit = dict(cached), True
            else:
                payload, llm_calls = self._answer(p, question)
                self.cache.put(key, dict(payload))
        except ServiceError as e:
            status, err = e.status, e.message
            raise
        except BudgetExceeded as e:   # the final answer could not be afforded in time: say so, do not hang
            status, err = 504, str(e)
            raise ServiceError(504, "the request exceeded its time budget") from e
        except Exception as e:  # never leak internals to the client; the log has the detail
            status, err = 500, f"{type(e).__name__}: {str(e)[:200]}"
            raise ServiceError(500, "internal error (see the request log)") from e
        finally:
            ms = round((time.perf_counter() - t0) * 1000)
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "request_id": rid, "user": p.user, "groups": list(p.groups),
                   "status": status, "latency_ms": ms, "cache_hit": cache_hit, "llm_calls": llm_calls, "error": err}
            if payload is not None:
                rec.update(abstained=payload["abstained"], reason=payload["reason"],
                           chunks=[c["chunk_id"] for c in payload["citations"]], answer_chars=len(payload["answer"]))
            if self.cfg.log_questions and isinstance(question, str):
                rec["question"] = question[:300]
            try:
                self.req_log.write(rec)
            except Exception:   # a full disk must not turn a good answer into an error, but it must not be silent either
                logging.getLogger("rag.service").exception("could not write the request log")
            with self._lock:
                self.recent[rid] = p.user
                while len(self.recent) > 5000:
                    self.recent.popitem(last=False)
        payload.update(request_id=rid, cache_hit=cache_hit, latency_ms=ms, llm_calls=llm_calls)
        return payload

    def _answer(self, p: Principal, question: str):
        c = self.cfg
        with self._lock:
            retriever, pos = self.retriever, self.pos
        counter = Counter(c.max_llm_calls, c.request_timeout_s)
        reserve = 1 + (1 if c.sufficiency else 0)          # the final answer (and the sufficiency check) must always fit
        search_llm = BudgetedLLM(self.llm, counter, reserve) if self.llm else None
        final_llm = BudgetedLLM(self.llm, counter, 0) if self.llm else None
        techniques = dict(c.techniques) if (search_llm and c.max_llm_calls > reserve) else {}
        pipe = Pipeline(retriever, search_llm, {"llm": LLMReranker(search_llm)} if search_llm else {}, pos=pos)  # per request: pipelines hold per-run state
        scfg = SearchConfig(k=c.k, groups=p.groups, **techniques)
        res = run_rag(question, pipe, AnswerConfig(search=scfg, backend=c.backend, sufficiency=c.sufficiency and search_llm is not None, **({'system': c.system} if c.system else {})), final_llm)
        cited = set(cited_ids(res.text))
        cites = [{"n": i, "chunk_id": h.chunk.chunk_id, "source": h.chunk.source, "section": h.chunk.section or h.chunk.title,
                  "status": h.chunk.status, "cited": i in cited, "text": h.chunk.text[:600]}
                 for i, h in enumerate(res.hits, 1)]
        return {"answer": res.text, "abstained": res.abstained, "reason": res.reason, "citations": cites,
                "search_trace": pipe.trace[:12]}, counter.calls

    # ------------------------------------------------------------------ feedback
    def feedback(self, p: Principal, request_id, rating, comment="") -> None:
        if not isinstance(rating, str) or rating not in ("up", "down"):
            raise ServiceError(400, "'rating' must be 'up' or 'down'")
        if not isinstance(request_id, str):
            raise ServiceError(400, "'request_id' must be a string")
        with self._lock:
            owner = self.recent.get(request_id)
        if owner is None or owner != p.user:
            raise ServiceError(404, "unknown request_id")   # same answer for 'not yours' and 'not found': no probing
        self.fb_log.write({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "request_id": request_id, "user": p.user,
                           "rating": rating, "comment": str(comment)[:500]})

    def health(self) -> dict:
        return {"status": "ok", "index_version": self.version, "chunks": len(self.retriever.chunks), "backend": self.cfg.backend,
                "embedder": self.cfg.embedder, "uptime_s": round(time.time() - self.started),
                "cache": {"size": len(self.cache.d), "hits": self.cache.hits, "misses": self.cache.misses}}
