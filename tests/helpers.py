"""Shared test tools: a tiny check framework, a raw WebSocket test client, an app factory with fake speech engines."""
import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np  # noqa: E402

from voicecall.audio import SR, float_to_pcm16, tone  # noqa: E402
from voicecall.config import Config  # noqa: E402
from voicecall.server import ROOT, App  # noqa: E402
from voicecall.stt import ScriptedSTT  # noqa: E402
from voicecall.tts import ToneTTS  # noqa: E402

fails: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info and not ok else ""))
    if not ok:
        fails.append(name)


def finish():
    print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
    sys.exit(1 if fails else 0)


def noise(seconds, amp=0.0008, seed=3):
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR)) * amp).astype(np.float32)


def speech(seconds=1.2):
    """quiet room, speech-like burst, then silence long enough to end the utterance"""
    return np.concatenate([noise(0.5), tone(seconds), noise(1.0)])


class Client:
    def __init__(self, r, w):
        self.r, self.w, self.events, self.audio, self.closed = r, w, [], [], False
        self.task = asyncio.create_task(self._read())

    @staticmethod
    async def connect(port, origin=None, host=None, key=None):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        key = key or base64.b64encode(os.urandom(16)).decode()
        h = f"GET /ws/call HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        if origin:
            h += f"Origin: {origin}\r\n"
        w.write((h + "\r\n").encode())
        await w.drain()
        head = await r.readuntil(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        if status != 101:
            return status, None
        return status, Client(r, w)

    async def _read(self):
        try:
            while True:
                b1, b2 = await self.r.readexactly(2)
                n, op = b2 & 0x7F, b1 & 0xF
                if n == 126:
                    n = struct.unpack(">H", await self.r.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await self.r.readexactly(8))[0]
                data = await self.r.readexactly(n)
                if op == 1:
                    self.events.append(json.loads(data))
                elif op == 2:
                    self.audio.append(data)
                elif op == 8:
                    self.closed = True
                    return
                elif op == 9:
                    self.events.append({"type": "_ping"})
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed = True

    def frame(self, op, payload: bytes, fin=True, mask=True):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | op])
        mb = 0x80 if mask else 0
        head += bytes([mb | n]) if n < 126 else (bytes([mb | 126]) + struct.pack(">H", n) if n < 65536 else bytes([mb | 127]) + struct.pack(">Q", n))
        if mask:
            m = os.urandom(4)
            return head + m + bytes(b ^ m[i % 4] for i, b in enumerate(payload))
        return head + payload

    async def raw(self, b):
        self.w.write(b)
        await self.w.drain()

    async def send(self, obj):
        await self.raw(self.frame(1, json.dumps(obj).encode()))

    async def pcm(self, x):
        b = float_to_pcm16(x)
        for i in range(0, len(b), 1280):                       # 40 ms frames, like the browser
            await self.raw(self.frame(2, b[i:i + 1280]))
            if i % 12800 == 0:
                await asyncio.sleep(0)

    async def wait(self, pred, timeout=8.0):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            for e in self.events:
                if pred(e):
                    return e
            await asyncio.sleep(0.02)
        return None

    def types(self):
        return [e["type"] for e in self.events]

    async def close(self):
        try:
            self.w.close()
        except Exception:
            pass
        self.task.cancel()


def make_app(tmp, texts=(), tts_delay=0.0, **cfgkw):
    cfg = Config(invites=str(Path(tmp) / "invites.json"), log_dir=str(Path(tmp) / "logs"), port=0, **cfgkw)
    return App(cfg, ROOT, stt=ScriptedSTT(texts), tts=ToneTTS(tts_delay))


def tmpdir():
    return tempfile.mkdtemp(prefix="vc_test_")
