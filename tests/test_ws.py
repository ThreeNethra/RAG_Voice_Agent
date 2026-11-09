"""WebSocket and HTTP layer: handshake, framing, limits, origin check, static files."""
import asyncio
import json
import os
import struct
import urllib.request

from helpers import Client, check, finish, make_app, tmpdir

from voicecall.ws import MAX_MESSAGE


async def main():
    app = make_app(tmpdir())
    srv = await app.start()
    port = srv.port
    loop = asyncio.get_running_loop()
    get = lambda p, **kw: loop.run_in_executor(None, lambda: _get(f"http://127.0.0.1:{port}{p}", **kw))  # noqa: E731

    def _get(url, method="GET"):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5)
            return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    st, h, body = await get("/")
    check("GET / serves the page", st == 200 and b"Lumora Voice" in body)
    check("security headers present", "default-src 'self'" in h.get("Content-Security-Policy", "") and h.get("X-Frame-Options") == "DENY" and "microphone=(self)" in h.get("Permissions-Policy", ""))
    st, h, body = await get("/web/worklet.js")
    check("worklet is served as JavaScript", st == 200 and h["Content-Type"].startswith("application/javascript"))
    check("path traversal is refused", (await get("/web/../config.py"))[0] == 404 and (await get("/web/..%2fvoicecall/ws.py"))[0] == 404 and (await get("/web/%2e%2e/rag/voice.py"))[0] == 404)
    check("unknown path is 404", (await get("/nope"))[0] == 404)
    st, h, body = await get("/health")
    check("health works and leaks nothing secret", st == 200 and json.loads(body)["ok"] and "invite" not in body.decode().lower())
    check("POST is refused", (await get("/", method="POST"))[0] == 405)

    # WebSocket
    st, c = await Client.connect(port)
    check("handshake succeeds", st == 101 and c is not None)
    await c.send({"type": "hello", "invite": "wrong"})
    e = await c.wait(lambda e: e["type"] == "error")
    check("a wrong invite gets a clear error, not a crash", e and e.get("code") == "auth", str(e))
    await c.close()

    st, _ = await Client.connect(port, origin="https://evil.example")
    check("a foreign Origin is rejected (403)", st == 403, str(st))
    st, c = await Client.connect(port, origin=f"http://127.0.0.1:{port}")
    check("same Origin is accepted", st == 101)
    await c.close()

    # fragmentation + ping + big message + unmasked frame
    app.invites.create("t", ["all-staff"])
    code = app.invites.create("t2", ["all-staff"])
    st, c = await Client.connect(port)
    payload = json.dumps({"type": "hello", "invite": code}).encode()
    await c.raw(c.frame(1, payload[:10], fin=False) + c.frame(0, payload[10:], fin=True))
    check("a message split into fragments is reassembled", await c.wait(lambda e: e["type"] == "ready") is not None)
    await c.raw(c.frame(9, b"hi"))
    check("ping is answered (connection stays up)", not c.closed)
    await c.raw(c.frame(2, b"x" * 200, mask=False))
    await asyncio.sleep(0.2)
    check("an unmasked client frame closes the connection", c.closed)
    await c.close()

    st, c = await Client.connect(port)
    await c.raw(struct.pack("BB", 0x82, 0x80 | 127) + struct.pack(">Q", MAX_MESSAGE + 1) + os.urandom(4))
    await asyncio.sleep(0.2)
    check("an oversized frame closes the connection", c.closed)
    await c.close()

    st, c = await Client.connect(port)
    await asyncio.sleep(0.05)
    await c.raw(os.urandom(64))
    await asyncio.sleep(0.2)
    check("garbage bytes do not crash the server", (await get("/health"))[0] == 200)
    await c.close()
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"\x00\x01garbage\r\n\r\n")
    await w.drain()
    check("a malformed HTTP request gets 400", (await r.read(200)).startswith(b"HTTP/1.1 400"))
    w.close()
    await srv.stop()
    finish()


asyncio.run(main())
