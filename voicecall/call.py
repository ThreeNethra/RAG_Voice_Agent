"""One phone-style call: audio in, events and audio out, over a single WebSocket.

Wire format
  client -> server   binary: 16 kHz mono PCM16 (any chunk size)          text JSON: {"type": "text"|"interrupt"|"idle"|"end", ...}
  server -> client   binary: 4-byte little-endian sample rate + PCM16     text JSON: ready, state, user, assistant, clear, metrics, bye, error

States: listening -> thinking -> speaking -> listening.
Speaking over the assistant (barge-in) cancels its speech immediately; speaking again while it is still thinking cancels the
thinking and joins your two sentences, so a pause in the middle of a question does not split it.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from rag.service import ServiceError

from .audio import FRAME, SR, EndpointConfig, Endpointer, float_to_pcm16
from .ws import Closed, WebSocket

CARRY_S = 4.0                 # a second burst of speech this soon after an utterance is treated as its continuation
STILL_THERE = "Are you still there?"
TIMEOUT_BYE = "I haven't heard anything, so I'll end the call. Goodbye."
LIMIT_BYE = "We've reached the time limit for this call. Goodbye."
BUSY = "I'm getting a lot of requests. Give me a few seconds."
OOPS = "Sorry, something went wrong on my side. Please ask again."
_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Engines:
    cfg: object
    stt: object
    tts: object
    brain: object
    log: object                       # callable(dict)
    pool: ThreadPoolExecutor


def chunk_sentences(text: str, min_chars: int = 28) -> list[str]:
    """Sentences for synthesis; very short ones are joined to the next so the voice does not stutter."""
    out: list[str] = []
    for s in (x.strip() for x in _SPLIT.split(text or "") if x.strip()):
        if out and len(out[-1]) < min_chars:
            out[-1] += " " + s
        else:
            out.append(s)
    return out


class Call:
    def __init__(self, ws: WebSocket, principal, eng: Engines, invite_id: str):
        self.ws, self.p, self.e, self.invite = ws, principal, eng, invite_id
        self.cfg = eng.cfg
        self.id = uuid.uuid4().hex[:10]
        self.sid = secrets.token_hex(8)
        self.ep = Endpointer(EndpointConfig(endpoint_ms=self.cfg.endpoint_ms))
        self.state = "listening"
        self.turn_task: asyncio.Task | None = None
        self.idle_evt = asyncio.Event()
        self.idle_evt.set()
        self.last_activity = time.monotonic()
        self.started = time.monotonic()
        self.prompted = False
        self.carry: tuple | None = None          # (audio, time)
        self.cur_audio: np.ndarray | None = None
        self.muted_audio = False
        self.turns = 0
        self.chunks_sent = 0                     # binary audio frames sent; the client reports how many it has received and played
        self.closing = False

    # ------------------------------------------------------------------ helpers
    async def ev(self, **kw) -> None:
        try:
            await self.ws.send_text(json.dumps(kw))
        except Closed:
            self.closing = True

    async def set_state(self, s: str) -> None:
        if s != self.state:
            self.state = s
            await self.ev(type="state", state=s)
        if s == "listening":
            self.last_activity = time.monotonic()

    async def blocking(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(self.e.pool, fn, *a)

    def minutes(self) -> float:
        return (time.monotonic() - self.started) / 60.0

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        watchdog = asyncio.create_task(self._watchdog())
        try:
            await self.ev(type="ready", name=self.p.user.replace("invite:", ""), max_minutes=self.cfg.max_call_minutes, endpoint_ms=self.cfg.endpoint_ms)
            self._start_turn(self._greet())
            while not self.closing:
                msg = await self.ws.recv()
                if msg is None:
                    break
                if isinstance(msg, bytes):
                    await self._audio(msg)
                else:
                    await self._control(msg)
        finally:
            watchdog.cancel()
            if self.turn_task:
                self.turn_task.cancel()
            self.e.log({"kind": "call_end", "call": self.id, "user": self.p.user, "minutes": round(self.minutes(), 2), "turns": self.turns})

    def _start_turn(self, coro) -> None:
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        self.turn_task = asyncio.create_task(coro)

    async def _control(self, raw: str) -> None:
        try:
            m = json.loads(raw)
            t = m.get("type")
        except (ValueError, AttributeError):
            return
        if t == "idle":
            if m.get("chunks") == self.chunks_sent:      # only counts if the client has played everything we sent
                self.idle_evt.set()
        elif t == "interrupt":
            await self._interrupt()
        elif t == "end":
            self.closing = True
        elif t == "text":
            text = m.get("text")
            if isinstance(text, str) and 0 < len(text.strip()) <= 1000:
                await self._interrupt()
                self.last_activity = time.monotonic()
                self._start_turn(self._respond(None, text.strip(), time.monotonic()))

    async def _audio(self, pcm: bytes) -> None:
        if len(pcm) > 65536:
            return
        for kind, *rest in self.ep.feed(pcm, agent_speaking=(self.state == "speaking")):
            self.last_activity = time.monotonic()
            self.prompted = False
            if kind == "start":
                if self.state == "speaking":
                    await self._interrupt()
                elif self.state == "thinking" and self.turn_task and not self.turn_task.done():
                    self.turn_task.cancel()                       # the user is still talking: wait for the rest
                    if self.cur_audio is not None:
                        self.carry = (self.cur_audio, time.monotonic())
                    await self.set_state("listening")
            elif kind == "end":
                audio, now = rest[0], time.monotonic()
                if self.carry and now - self.carry[1] < CARRY_S + len(audio) / SR:
                    audio = np.concatenate([self.carry[0], np.zeros(int(0.15 * SR), np.float32), audio])
                self.carry = None
                self.cur_audio = audio
                self._start_turn(self._respond(audio, None, now))
            elif kind == "drop":
                if self.carry and self.state == "listening":     # a click interrupted the thinking: carry on with what we had
                    audio, self.carry = self.carry[0], None
                    self.cur_audio = audio
                    self._start_turn(self._respond(audio, None, time.monotonic()))

    # ------------------------------------------------------------------ turns
    async def _greet(self) -> None:
        res = await self.blocking(self.e.brain.turn, self.p, None, self.sid, "start")
        await self.ev(type="assistant", text=res["spoken"], sources=[])
        await self._speak(res["spoken"], None)

    async def _respond(self, audio, typed, t_end) -> None:
        metrics = {}
        try:
            await self.set_state("thinking")
            if typed is None:
                r = await self.blocking(self.e.stt.transcribe, audio)
                metrics["stt_ms"], text = r.ms, r.text
            else:
                text = typed
            if not text:                                          # noise, a cough, a hallucination
                self.cur_audio = None
                await self.set_state("listening")
                return
            await self.ev(type="user", text=text)
            t0 = time.perf_counter()
            try:
                res = await self.blocking(self.e.brain.turn, self.p, text, self.sid, None)
            except ServiceError as e:
                res = {"spoken": BUSY if e.status == 429 else OOPS, "intent": "error", "end": False, "sources": []}
            except Exception:                                      # noqa: BLE001 - a call must survive an engine bug
                res = {"spoken": OOPS, "intent": "error", "end": False, "sources": []}
            metrics["brain_ms"] = round((time.perf_counter() - t0) * 1000)
            self.turns += 1
            self.cur_audio = None
            await self.ev(type="assistant", text=res["spoken"], sources=res.get("sources", []), intent=res.get("intent"))
            self.e.log({"kind": "turn", "call": self.id, "user": self.p.user, "intent": res.get("intent"), "more": res.get("more"),
                        **metrics, **({"heard": text, "said": res["spoken"]} if self.cfg.log_text else {})})
            await self._speak(res["spoken"], t_end, metrics)
            if res.get("end"):
                await self._hangup()
        except asyncio.CancelledError:
            raise
        except Closed:
            self.closing = True

    async def _say(self, text: str, end: bool = False) -> None:
        await self.ev(type="assistant", text=text, sources=[])
        await self._speak(text, None)
        if end:
            await self._hangup()

    async def _hangup(self) -> None:
        await self.ev(type="bye")
        self.closing = True
        await self.ws.close(1000)

    async def _speak(self, text: str, t_end, metrics: dict | None = None) -> None:
        parts = chunk_sentences(text)
        if not parts:
            await self.set_state("listening")
            return
        await self.set_state("speaking")
        self.idle_evt.clear()
        total_s, first = 0.0, True
        nxt = asyncio.create_task(self.blocking(self.e.tts.synth, parts[0]))
        try:
            for i in range(len(parts)):
                x = await nxt
                if i + 1 < len(parts):
                    nxt = asyncio.create_task(self.blocking(self.e.tts.synth, parts[i + 1]))
                sr = int(self.e.tts.sample_rate)
                pcm = float_to_pcm16(np.asarray(x, np.float32))
                total_s += len(pcm) / 2 / sr
                head = sr.to_bytes(4, "little")
                for off in range(0, len(pcm), 48000):
                    self.chunks_sent += 1
                    await self.ws.send_bytes(head + pcm[off:off + 48000])
                if first and t_end is not None and metrics is not None:
                    metrics["first_audio_ms"] = round((time.monotonic() - t_end) * 1000)
                    await self.ev(type="metrics", **metrics)
                    self.e.log({"kind": "latency", "call": self.id, **metrics})
                first = False
            try:
                self.idle_evt.clear()
                await asyncio.wait_for(self.idle_evt.wait(), total_s + 5.0)
            except asyncio.TimeoutError:
                pass
            await self.set_state("listening")
        except asyncio.CancelledError:
            nxt.cancel()
            raise
        except Closed:
            self.closing = True

    async def _interrupt(self) -> None:
        if self.turn_task and not self.turn_task.done() and self.turn_task is not asyncio.current_task():
            self.turn_task.cancel()
        self.idle_evt.set()
        await self.ev(type="clear")
        self.cur_audio = None if self.state == "speaking" else self.cur_audio
        await self.set_state("listening")

    # ------------------------------------------------------------------ limits
    async def _watchdog(self) -> None:
        try:
            while not self.closing:
                await asyncio.sleep(0.5)
                idle = time.monotonic() - self.last_activity
                busy = self.turn_task is not None and not self.turn_task.done()
                if self.minutes() >= self.cfg.max_call_minutes and not busy:
                    self._start_turn(self._say(LIMIT_BYE, end=True))
                    return
                if self.state == "listening" and not busy:
                    if not self.prompted and idle >= self.cfg.idle_prompt_s:
                        self.prompted = True
                        self._start_turn(self._say(STILL_THERE))
                    elif self.prompted and idle >= self.cfg.idle_hangup_s:        # measured from the end of the prompt (set_state resets the clock)
                        self._start_turn(self._say(TIMEOUT_BYE, end=True))
                        return
        except asyncio.CancelledError:
            pass
