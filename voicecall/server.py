"""Wires everything together and serves the call page and the call socket."""
from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rag.generate import build_system
from rag.service import JsonLog, Service, ServiceConfig

from .brain import CallAgent
from .call import Call, Engines
from .invites import Invites
from .stt import make_stt
from .tts import make_tts
from .ws import Response, Server, WebSocket, static_handler

ROOT = Path(__file__).resolve().parent.parent
HELLO_TIMEOUT = 10.0


VOICE_RULES = ("7. Your answer will be SPOKEN ALOUD on a phone call. Use at most three short sentences in plain words. "
               "No lists, tables, headings or markdown. Lead with the answer itself, then at most one useful detail. "
               "Still cite with [n] after claims (the citations are removed before speaking).")


def make_service(cfg, root: Path = ROOT, llm=None) -> Service:
    return Service(root, ServiceConfig(data=cfg.data, embedder=cfg.embedder, backend=cfg.backend, k=cfg.k, log_dir=cfg.log_dir,
                                       sufficiency=cfg.backend != "extractive", rate_per_min=60, log_questions=cfg.log_text,
                                       llm_cache=None, system=build_system(extra_rules=VOICE_RULES) if cfg.voice_prompt else None), llm=llm)


class App:
    def __init__(self, cfg, root: Path = ROOT, stt=None, tts=None, service=None, llm=None):
        self.cfg, self.root = cfg, Path(root)
        self.service = service or make_service(cfg, self.root, llm)
        self.stt = stt or make_stt(cfg, self.root / cfg.data / "corpus")
        self.tts = tts or make_tts(cfg)
        self.brain = CallAgent(self.service, cfg.spoken_rewrite, cfg.followup_rewrite)
        self.invites = Invites(self.root / cfg.invites)
        log = JsonLog(self.root / cfg.log_dir / "voice.jsonl")
        self.eng = Engines(cfg, self.stt, self.tts, self.brain, log.write, ThreadPoolExecutor(max_workers=4, thread_name_prefix="voice"))
        self.calls = 0
        self.static = static_handler(self.root / "web")
        self.server: Server | None = None

    def warm(self) -> list[str]:
        """Load the models now so the first caller does not wait."""
        return [self.stt.load(), self.tts.load()]

    async def http(self, req) -> Response:
        r = await self.static(req)
        if r is not None:
            return r
        if req.path == "/health":
            return Response.json({"ok": True, "calls": self.calls, "stt": self.stt.name, "tts": self.tts.name,
                                  "index": self.service.version[:12], "backend": self.cfg.backend})
        return Response(404, b"not found")

    async def ws(self, ws: WebSocket, req) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT)
            hello = json.loads(raw) if isinstance(raw, str) else {}
        except (asyncio.TimeoutError, ValueError):
            await ws.send_text(json.dumps({"type": "error", "message": "no hello"}))
            return
        if hello.get("type") != "hello":
            return
        try:
            principal, inv, _rec = self.invites.verify(str(hello.get("invite", "")), req.peer)
        except PermissionError as e:
            await ws.send_text(json.dumps({"type": "error", "code": "auth", "message": str(e)}))
            return
        if self.calls >= self.cfg.max_calls:
            await ws.send_text(json.dumps({"type": "error", "code": "busy", "message": "all lines are busy, try again in a minute"}))
            return
        if not self.invites.begin(inv):
            await ws.send_text(json.dumps({"type": "error", "code": "busy", "message": "you already have a call open"}))
            return
        self.calls += 1
        call = Call(ws, principal, self.eng, inv)
        t0 = time.monotonic()
        self.eng.log({"kind": "call_start", "call": call.id, "user": principal.user, "peer": req.peer})
        try:
            await call.run()
        finally:
            self.calls -= 1
            self.invites.end(inv, (time.monotonic() - t0) / 60.0)

    async def start(self) -> Server:
        self.server = Server(self.http, self.ws, self.cfg.host, self.cfg.port, allowed_origins=self.cfg.allowed_origins)
        return await self.server.start()
