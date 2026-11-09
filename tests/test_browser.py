"""The real page in headless Chromium with a FAKE microphone (a WAV that loops: silence, a voice-like burst, silence).
Checks the whole path: AudioWorklet capture -> WebSocket -> endpointing -> (scripted) recognition -> engine -> voice -> playback."""
import asyncio
import threading
import time
import wave
from pathlib import Path

import numpy as np
from helpers import check, finish, make_app, noise, tmpdir

from voicecall.audio import float_to_pcm16, tone

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP playwright is not installed")
    raise SystemExit(0)

tmp = Path(tmpdir())
x = np.concatenate([noise(1.0, seed=1), tone(1.6, amp=0.35), noise(5.0, seed=2)])
from voicecall.audio import resample  # noqa: E402
x48 = resample(x, 16000, 48000)
wav = tmp / "mic.wav"
with wave.open(str(wav), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(float_to_pcm16(x48))

HOSTILE = "<img src=x onerror=window.__pwned=1> <script>window.__pwned=1</script>"
app = make_app(tmp, texts=["What is the Growth plan base price?", HOSTILE, "What is the uptime SLA for the Growth plan?", "goodbye"] + [""] * 20)
loop = asyncio.new_event_loop()
ready = threading.Event()


def serve():
    asyncio.set_event_loop(loop)
    loop.run_until_complete(app.start())
    ready.set()
    loop.run_forever()


threading.Thread(target=serve, daemon=True).start()
ready.wait(10)
base = f"http://127.0.0.1:{app.server.port}"
code = app.invites.create("browser", ["all-staff"])

COUNT = """() => { window.__starts = 0; const o = AudioBufferSourceNode.prototype.start;
  AudioBufferSourceNode.prototype.start = function(...a){ window.__starts++; return o.apply(this, a); }; }"""


def wait(pred, t=12.0):
    end = time.time() + t
    while time.time() < end:
        try:
            v = pred()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(0.05)
    return None


with sync_playwright() as pw:
    br = pw.chromium.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", f"--use-file-for-fake-audio-capture={wav}", "--autoplay-policy=no-user-gesture-required"])
    ctx = br.new_context(viewport={"width": 420, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.add_init_script("(" + COUNT + ")()")
    pg.goto(base)
    check("page loads with the join form", pg.is_visible("#code") and not pg.is_visible("#call"))
    pg.fill("#code", "wrong-code")
    pg.click("#join-btn")
    pg.click("#orb")
    check("a wrong code returns to the join form with a message", wait(lambda: pg.is_visible("#join-error") and "not valid" in pg.inner_text("#join-error")) is not None)
    pg.fill("#code", code)
    pg.click("#join-btn")
    check("a valid code shows the call card", pg.is_visible("#call"))
    check("controls are disabled before the call", pg.is_disabled("#end") and pg.is_disabled("#text"))
    pg.click("#orb")
    check("the greeting appears in the log", wait(lambda: pg.locator("#log li.agent").count() >= 1) is not None)
    check("the greeting is played (audio buffers scheduled)", wait(lambda: pg.evaluate("window.__starts") >= 1) is not None)
    check("the page shows who is signed in", "browser" in pg.inner_text("#who"))
    check("the call becomes 'Listening' after the greeting finishes playing", wait(lambda: pg.inner_text("#status") == "Listening", 30) is not None, pg.inner_text("#status"))
    check("the fake microphone's speech is heard and answered", wait(lambda: pg.locator("#log li.you").count() >= 1 and pg.locator("#log li.agent").count() >= 2, 25) is not None)
    li = pg.locator("#log li.you").first.inner_text()
    check("the transcript shows what was recognised", "Growth plan base price" in li, li)
    ans = pg.locator("#log li.agent").nth(1).inner_text()
    check("the answer is grounded and shows sources", ("1,950" in ans or "1950" in ans) and "Sources:" in ans, ans[:200])
    check("latency is displayed", wait(lambda: "Replied" in pg.inner_text("#metrics")) is not None, pg.inner_text("#metrics"))
    check("the answer is spoken (more audio scheduled)", pg.evaluate("window.__starts") >= 2)

    # hostile text from the recogniser (second loop of the fake microphone) must stay text
    check("hostile recognised text is shown as text and never runs", wait(lambda: pg.locator("#log li.you").count() >= 2, 30) is not None and pg.evaluate("window.__pwned") is None and pg.locator("#log img, #log script").count() == 0)

    # typed question, stop button, mute
    wait(lambda: pg.inner_text("#status") in ("Listening", "Thinking…", "Speaking. Talk to interrupt"), 5)
    pg.fill("#text", "What is the Growth plan base price?")
    pg.press("#text", "Enter")
    check("a typed question is answered", wait(lambda: pg.locator("#log li.you:has-text('Growth plan base price')").count() >= 2, 10) is not None)
    pg.click("#mute")
    check("mute toggles and is announced", pg.get_attribute("#mute", "aria-pressed") == "true" and pg.inner_text("#mute") == "Unmute")
    pg.click("#mute")
    pg.keyboard.press("Escape")
    check("Escape does not break the call", pg.is_enabled("#end"))
    pg.screenshot(path=str(tmp / "call.png"))

    pg.click("#end")
    check("End call returns to the idle state and releases the microphone", wait(lambda: pg.inner_text("#status").startswith("Call ended")) is not None and pg.is_disabled("#end"))
    check("no script errors in the whole session", not errs, str(errs[:2]))
    check("no horizontal scrolling on a phone-width screen", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
    ctx.close()

    # saved invite is remembered
    ctx = br.new_context()
    pg = ctx.new_page()
    pg.goto(base)
    pg.fill("#code", code); pg.check("#remember"); pg.click("#join-btn")
    pg.reload()
    check("'remember on this device' skips the join form next time", pg.is_visible("#call") and not pg.is_visible("#join"))
    ctx.close()

    # microphone denied
    br2 = pw.chromium.launch()
    ctx = br2.new_context()
    pg = ctx.new_page()
    pg.goto(base)
    pg.fill("#code", code); pg.click("#join-btn"); pg.click("#orb")
    check("a blocked or missing microphone gives a clear message", wait(lambda: pg.is_visible("#error") and ("microphone" in pg.inner_text("#error").lower()), 8) is not None, pg.inner_text("#error") if pg.is_visible("#error") else "no error shown")
    check("and the page stays usable", pg.inner_text("#status") == "Tap to call")
    ctx.close(); br2.close()
    br.close()

loop.call_soon_threadsafe(loop.stop)
print("screenshot:", tmp / "call.png")
finish()
