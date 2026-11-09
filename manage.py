"""Management tool.   python manage.py <command>

  invite --name Ravi --groups all-staff       create an invite code (shown once)
  invites                                     list invites
  revoke Ravi                                 disable an invite
  groups                                      document groups the access rules know
  index                                       (re)build the document index
  fetch-models                                download the Kokoro voice (about 330 MB)
  doctor                                      check this PC: Python, GPU, packages, models, index, model backend
  selftest                                    speak a sentence, listen to it again, answer it: tests the whole chain without a microphone
"""
import argparse
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from voicecall.config import Config, load_env  # noqa: E402

load_env(ROOT / ".env")

KOKORO = {"models/kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
          "models/voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"}


def cfg() -> Config:
    return Config.load(ROOT / "config.json")


def cmd_invite(a):
    from voicecall.invites import Invites
    c = cfg()
    groups = [g.strip() for g in a.groups.split(",") if g.strip()]
    known = set()
    acl = ROOT / c.data / "acl.json"
    if acl.exists():
        j = json.loads(acl.read_text(encoding="utf-8"))
        known = {g for r in j.get("rules", []) for g in r["groups"]} | set(j.get("default", []))
        known.discard("*")
    unknown = [g for g in groups if known and g not in known]
    if unknown:
        print(f"warning: no document rule mentions {unknown}. Known groups: {sorted(known)}")
    code = Invites(ROOT / c.invites).create(a.name, groups, a.minutes)
    print(f"\ninvite for {a.name}  (groups: {', '.join(groups) or 'none'}; {a.minutes:g} minutes a day)\n\n    {code}\n\nShown once. Give it to them privately.")


def cmd_invites(a):
    from voicecall.invites import Invites
    rows = Invites(ROOT / cfg().invites).list()
    for r in rows:
        print(f"{r['id']}  {r['name']:<16} groups={','.join(r['groups']) or '-':<28} {r['minutes_per_day']:g} min/day  {'DISABLED' if r['disabled'] else 'active'}")
    if not rows:
        print("no invites yet")


def cmd_revoke(a):
    from voicecall.invites import Invites
    print(f"disabled {Invites(ROOT / cfg().invites).revoke(a.who)} invite(s)")


def cmd_groups(a):
    j = json.loads((ROOT / cfg().data / "acl.json").read_text(encoding="utf-8"))
    for r in j["rules"]:
        print(f"{r['match']:<8} -> {', '.join(r['groups'])}")
    print(f"(default) -> {', '.join(j.get('default', []))}")


def cmd_index(a):
    from rag.store import sync_index
    c = cfg()
    rep = sync_index(ROOT, ROOT / c.data, "structure", c.embedder)
    print(f"index ready: {rep['chunks_total']} chunks ({rep['chunks_embedded']} new)")


def _download(url: str, dst: Path) -> None:
    """Download with certificate checking ON. Python's own certificate list can be incomplete on some Windows PCs, so try the
    certifi list (what pip uses), then Windows' built-in curl.exe (which uses the Windows certificate store)."""
    import ssl
    tmp = dst.with_suffix(".part")
    contexts = []
    try:
        import certifi
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        pass
    contexts.append(ssl.create_default_context())
    last = None
    for ctx in contexts:
        try:
            with urllib.request.urlopen(url, timeout=60, context=ctx) as r, open(tmp, "wb") as f:
                total, done, shown = int(r.headers.get("Content-Length", 0)), 0, 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total and done - shown > 25 << 20:
                        print(f"  {done >> 20} / {total >> 20} MB", flush=True)
                        shown = done
            tmp.replace(dst)
            return
        except (urllib.error.URLError, ssl.SSLError, OSError) as e:
            last = e
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        print("  Python could not verify the certificate; using Windows' curl instead...", flush=True)
        r = subprocess.run([curl, "-L", "--fail", "--progress-bar", "-o", str(tmp), url])
        if r.returncode == 0:
            tmp.replace(dst)
            return
    raise SystemExit(f"download failed: {last}\nDownload it in your browser instead and save it as {dst}:\n  {url}")


def cmd_fetch(a):
    (ROOT / "models").mkdir(exist_ok=True)
    for rel, url in KOKORO.items():
        dst = ROOT / rel
        if dst.exists() and dst.stat().st_size > 1_000_000:
            print("have", rel)
            continue
        print("downloading", rel, "...", flush=True)
        _download(url, dst)
    print("voice files are ready")


def _mark(ok, text, hint=""):
    print(("  ok   " if ok else "  FAIL ") + text + (f"\n         -> {hint}" if hint and not ok else ""))
    return ok


def cmd_doctor(a):
    c = cfg()
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    ok = True
    ok &= _mark((3, 10) <= sys.version_info[:2] <= (3, 13), "Python version between 3.10 and 3.13 (speech libraries do not support newer ones yet)",
                "install Python 3.12 and run scripts\\setup.ps1 again, it builds a separate environment")
    for mod, why in (("numpy", "audio maths"), ("sklearn", "document search"), ("faster_whisper", "speech recognition"), ("kokoro_onnx", "natural voice"),
                     ("onnxruntime", "runs the voice")):
        try:
            importlib.import_module(mod)
            _mark(True, f"{mod} ({why})")
        except ImportError:
            _mark(False, f"{mod} ({why})", "pip install -r requirements-voice.txt")
            ok &= mod not in ("numpy", "sklearn", "faster_whisper")      # Kokoro can be missing: Windows' own voice is the fallback
    gpu = shutil.which("nvidia-smi")
    if gpu:
        out = subprocess.run([gpu, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
        _mark(True, f"NVIDIA GPU: {out}")
    else:
        _mark(False, "no NVIDIA GPU / driver found: speech recognition will run on the CPU (slower)", "install the NVIDIA driver; the voice still works")
    try:
        from voicecall.stt import add_cuda_dll_dirs
        add_cuda_dll_dirs()
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        _mark(n > 0, f"CUDA visible to the recogniser ({n} device)", "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12  (setup.ps1 does this)")
    except ImportError:
        pass
    for rel in ("models/kokoro-v1.0.onnx", "models/voices-v1.0.bin"):
        _mark((ROOT / rel).exists(), f"voice file {rel}", "python manage.py fetch-models  (Windows' own voice is used until then)")
    idx = ROOT / "index"
    ok &= _mark(idx.exists() and any(idx.iterdir()), "document index", "python manage.py index")
    ok &= _mark(bool(__import__("voicecall.invites", fromlist=["Invites"]).Invites(ROOT / c.invites).list()), "at least one invite code", "python manage.py invite --name me --groups all-staff,exec")
    print(f"\nanswer engine: backend={c.backend}")
    if c.backend == "api":
        for v in ("LUMORA_API_BASE", "LUMORA_API_KEY", "LUMORA_API_MODEL"):
            ok &= _mark(bool(os.environ.get(v)), f"{v} is set", "see README: Gemini setup")
    if c.backend == "ollama":
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
            _mark(True, "Ollama is running")
        except Exception:
            ok &= _mark(False, "Ollama is not running", "start the Ollama app, then: ollama pull qwen2.5:7b-instruct")
    s = socket.socket()
    free = s.connect_ex(("127.0.0.1", c.port)) != 0
    s.close()
    _mark(free, f"port {c.port} is free", "something else is using it, or the server is already running")
    _mark(bool(shutil.which("cloudflared")), "cloudflared (to share a link with friends)", "winget install Cloudflare.cloudflared   (only needed for sharing)")
    print("\nAll good." if ok else "\nFix the FAIL lines above, then run doctor again.")


def cmd_selftest(a):
    import numpy as np
    from rag.voice import classify  # noqa: F401
    from voicecall.audio import resample
    from voicecall.server import App
    c = cfg()
    print("loading (first run downloads the recogniser)...", flush=True)
    app = App(c, ROOT)
    for line in app.warm():
        print("  loaded:", line)
    phrase = "What is the Growth plan base price?"
    t0 = time.perf_counter()
    audio = app.tts.synth(phrase)
    t_tts = time.perf_counter() - t0
    x16 = resample(np.asarray(audio, np.float32), int(app.tts.sample_rate), 16000)
    t0 = time.perf_counter()
    heard = app.stt.transcribe(np.concatenate([np.zeros(8000, np.float32), x16, np.zeros(8000, np.float32)]))
    print(f"\nspoke   : {phrase}   ({len(audio) / app.tts.sample_rate:.1f}s of audio, synthesised in {t_tts * 1000:.0f} ms)")
    print(f"heard   : {heard.text}   ({heard.ms} ms)")
    from voicecall.invites import Invites
    from rag.service import Principal
    t0 = time.perf_counter()
    r = app.brain.turn(Principal("selftest", ("all-staff",)), heard.text or phrase, "selftest")
    print(f"answered: {r['spoken']}   ({(time.perf_counter() - t0) * 1000:.0f} ms)")
    good = "growth" in heard.text.lower() and "price" in heard.text.lower()
    print("\nSELFTEST", "PASSED" if good else "FAILED: the recogniser did not hear the sentence the voice spoke")
    sys.exit(0 if good else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("invite"); p.add_argument("--name", required=True); p.add_argument("--groups", default="all-staff"); p.add_argument("--minutes", type=float, default=30.0); p.set_defaults(f=cmd_invite)
    sub.add_parser("invites").set_defaults(f=cmd_invites)
    p = sub.add_parser("revoke"); p.add_argument("who"); p.set_defaults(f=cmd_revoke)
    sub.add_parser("groups").set_defaults(f=cmd_groups)
    sub.add_parser("index").set_defaults(f=cmd_index)
    sub.add_parser("fetch-models").set_defaults(f=cmd_fetch)
    sub.add_parser("doctor").set_defaults(f=cmd_doctor)
    sub.add_parser("selftest").set_defaults(f=cmd_selftest)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
