"""A small HTTP + WebSocket (RFC 6455) server on asyncio, standard library only.

Why not a framework: the call path needs exactly one thing, a binary-capable WebSocket next to a few static
files, and keeping it in ~200 lines means nothing else has to be installed and every limit is visible here.
Limits: header size and time, message size, fragment count, open connections. Browsers must send an Origin
that matches the Host (blocks other websites from opening a call socket in a visitor's browser).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import struct
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEADER = 16 * 1024
MAX_MESSAGE = 1 << 20          # 1 MiB: audio frames are ~1 KB, text messages are tiny
MAX_FRAGMENTS = 64
HEADER_TIMEOUT = 10.0
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
       "connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()", "Content-Security-Policy": CSP,
}
STATUS = {200: "OK", 101: "Switching Protocols", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
          405: "Method Not Allowed", 426: "Upgrade Required", 431: "Request Header Fields Too Large", 503: "Service Unavailable"}


@dataclass
class Request:
    method: str
    path: str
    headers: dict
    peer: str = ""


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: dict = field(default_factory=dict)

    @staticmethod
    def json(obj, status=200):
        import json
        return Response(status, json.dumps(obj).encode(), "application/json", {"Cache-Control": "no-store"})


class Closed(Exception):
    pass


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str = ""):
        self.r, self.w, self.peer = reader, writer, peer
        self._send_lock = asyncio.Lock()
        self.closed = False

    async def _send_frame(self, op: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | op])
        head += bytes([n]) if n < 126 else (b"\x7e" + struct.pack(">H", n) if n < 65536 else b"\x7f" + struct.pack(">Q", n))
        async with self._send_lock:
            if self.closed:
                raise Closed()
            try:
                self.w.write(head + payload)
                await self.w.drain()
            except (ConnectionError, OSError):
                self.closed = True
                raise Closed() from None

    async def send_text(self, s: str) -> None:
        await self._send_frame(OP_TEXT, s.encode("utf-8"))

    async def send_bytes(self, b: bytes) -> None:
        await self._send_frame(OP_BIN, b)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed:
            try:
                await self._send_frame(OP_CLOSE, struct.pack(">H", code) + reason.encode()[:100])
            except Closed:
                pass
        self.closed = True
        try:
            self.w.close()
        except Exception:
            pass

    async def _read_frame(self):
        b1, b2 = await self.r.readexactly(2)
        fin, op, masked, n = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
        if b1 & 0x70:
            raise ValueError("reserved bits set")
        if not masked:
            raise ValueError("client frames must be masked")
        if n == 126:
            n = struct.unpack(">H", await self.r.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await self.r.readexactly(8))[0]
        if n > MAX_MESSAGE:
            raise OverflowError("frame too large")
        if op >= 0x8 and (n > 125 or not fin):
            raise ValueError("bad control frame")
        mask = await self.r.readexactly(4)
        data = bytearray(await self.r.readexactly(n))
        if n:   # unmask with one big-int XOR instead of a Python loop
            key = (mask * (n // 4 + 1))[:n]
            data = bytearray((int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big"))
        return fin, op, bytes(data)

    async def recv(self):
        """Next message: str (text), bytes (binary) or None when the peer closed. Raises Closed on protocol errors."""
        parts, kind = [], None
        total = 0
        try:
            while True:
                fin, op, data = await self._read_frame()
                if op == OP_PING:
                    await self._send_frame(OP_PONG, data)
                    continue
                if op == OP_PONG:
                    continue
                if op == OP_CLOSE:
                    await self.close(1000)
                    return None
                if op in (OP_TEXT, OP_BIN):
                    if parts:
                        raise ValueError("new message inside a fragmented one")
                    kind = op
                elif op != OP_CONT or not parts:
                    raise ValueError("unexpected continuation")
                parts.append(data)
                total += len(data)
                if total > MAX_MESSAGE or len(parts) > MAX_FRAGMENTS:
                    raise OverflowError("message too large")
                if fin:
                    msg = b"".join(parts)
                    return msg.decode("utf-8") if kind == OP_TEXT else msg
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.closed = True
            return None
        except OverflowError:
            await self.close(1009, "too large")
            return None
        except (ValueError, UnicodeDecodeError):
            await self.close(1002, "protocol error")
            return None


async def _read_head(reader) -> tuple[str, dict]:
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT)
    if len(raw) > MAX_HEADER:
        raise OverflowError
    lines = raw.decode("latin-1").split("\r\n")
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return lines[0], headers


def origin_ok(headers: dict, allowed: tuple = ()) -> bool:
    o = headers.get("origin")
    if not o:
        return True               # not a browser (scripts, tests): the invite code still guards the call
    host = urlsplit(o).netloc.lower()
    return host == headers.get("host", "").lower() or o in allowed


class Server:
    def __init__(self, http_handler, ws_handler, host="127.0.0.1", port=8765, max_conns=64, allowed_origins=()):
        self.http, self.wsh, self.host, self.port = http_handler, ws_handler, host, port
        self.max_conns, self.allowed = max_conns, tuple(allowed_origins)
        self.conns = 0
        self._srv = None

    async def start(self):
        self._srv = await asyncio.start_server(self._conn, self.host, self.port, limit=1 << 17)
        self.port = self._srv.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._srv:
            self._srv.close()
            await self._srv.wait_closed()

    async def _respond(self, w, resp: Response):
        h = {**SECURITY_HEADERS, **resp.headers}
        head = f"HTTP/1.1 {resp.status} {STATUS.get(resp.status, 'OK')}\r\nContent-Type: {resp.content_type}\r\nContent-Length: {len(resp.body)}\r\nConnection: close\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        w.write(head.encode("latin-1") + resp.body)
        await w.drain()

    async def _conn(self, reader, writer):
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        if self.conns >= self.max_conns:
            writer.close()
            return
        self.conns += 1
        try:
            try:
                line, headers = await _read_head(reader)
                method, target, _ = line.split(" ", 2)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OverflowError, ValueError, ConnectionError):
                try:
                    await self._respond(writer, Response(400, b"bad request"))
                except Exception:
                    pass
                return
            req = Request(method.upper(), unquote(urlsplit(target).path), headers, peer)
            if headers.get("upgrade", "").lower() == "websocket":
                await self._upgrade(req, reader, writer)
                return
            if req.method not in ("GET", "HEAD"):
                await self._respond(writer, Response(405, b"method not allowed"))
                return
            try:
                resp = await self.http(req)
            except Exception:
                resp = Response(500, b"internal error")
            if req.method == "HEAD":
                resp.body = b""
            await self._respond(writer, resp)
        except (ConnectionError, OSError):
            pass
        finally:
            self.conns -= 1
            try:
                writer.close()
            except Exception:
                pass

    async def _upgrade(self, req: Request, reader, writer):
        key = req.headers.get("sec-websocket-key", "")
        if req.headers.get("sec-websocket-version") != "13" or not key:
            await self._respond(writer, Response(400, b"bad websocket request"))
            return
        if not origin_ok(req.headers, self.allowed):
            await self._respond(writer, Response(403, b"origin not allowed"))
            return
        if req.path != "/ws/call":
            await self._respond(writer, Response(404, b"not found"))
            return
        accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer, req.peer)
        try:
            await self.wsh(ws, req)
        finally:
            await ws.close()


TYPES = {".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
         ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain"}


def static_handler(root: Path, index: str = "index.html", prefix: str = "/web/"):
    root = root.resolve()

    async def handle(req: Request) -> Response | None:
        path = req.path
        if path == "/":
            name = index
        elif path.startswith(prefix):
            name = path[len(prefix):]
        else:
            return None
        f = (root / name).resolve()
        if root not in f.parents or not f.is_file():
            return Response(404, b"not found")
        ctype = TYPES.get(f.suffix.lower()) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"   # fixed table: Windows' registry can map .js to text/plain, which browsers refuse for worklets
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return Response(200, f.read_bytes(), ctype, {"Cache-Control": "no-cache"})
    return handle
