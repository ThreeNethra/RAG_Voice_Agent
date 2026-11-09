"""python -m voicecall   (see --help)"""
import argparse
import asyncio
import sys
from dataclasses import fields
from pathlib import Path

from .config import Config, load_env
from .server import ROOT, App


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="voicecall", description="Voice agent for your documents")
    for f in fields(Config):
        if f.type in ("bool",):
            ap.add_argument("--" + f.name.replace("_", "-"), dest=f.name, action=argparse.BooleanOptionalAction, default=None)
        elif f.name != "allowed_origins":
            ap.add_argument("--" + f.name.replace("_", "-"), dest=f.name, type={"int": int, "float": float}.get(f.type, str), default=None)
    ap.add_argument("--allowed-origin", dest="allowed_origins", action="append", default=None)
    a = ap.parse_args()
    load_env(ROOT / ".env")
    cfg = Config.load(ROOT / "config.json", **vars(a))
    print("starting: loading the document index and the speech models (the first start downloads the recogniser, ~500 MB)...", flush=True)
    app = App(cfg, ROOT)
    for line in app.warm():
        print("  ready:", line, flush=True)
    if not app.invites.list():
        print("  note: no invite codes yet. Create one:  python manage.py invite --name me --groups all-staff,exec")

    async def run():
        srv = await app.start()
        print(f"\nListening on http://{cfg.host}:{srv.port}   (open it in Chrome or Edge)\nCtrl+C to stop.", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
