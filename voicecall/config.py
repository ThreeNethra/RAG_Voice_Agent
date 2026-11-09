"""Settings. Defaults suit one PC with an NVIDIA GPU; every field can be overridden on the command line
(`python -m voicecall --help`) or in config.json next to the project."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_origins: list = field(default_factory=list)   # extra origins for the call socket (the tunnel's own address is allowed automatically)

    # the document engine
    data: str = "data/lumora"
    embedder: str = "tfidf"            # tfidf | st (needs sentence-transformers; better matching)
    backend: str = "extractive"        # extractive | api (Gemini etc., see README) | ollama
    k: int = 5
    voice_prompt: bool = True          # ask the model for short spoken-style answers directly (no extra model call)
    spoken_rewrite: bool = False       # extra safety net: a second model call that shortens answers that are still long
    followup_rewrite: bool = True      # let the model turn "and for Enterprise?" into a full question

    # speech to text
    stt: str = "auto"                  # auto | whisper
    stt_model: str = "small.en"        # tiny.en | base.en | small.en | medium.en | distil-large-v3
    stt_device: str = "auto"           # auto | cuda | cpu
    stt_compute: str = "auto"          # auto | float16 | int8_float16 | int8
    stt_beam: int = 1

    # text to speech
    tts: str = "auto"                  # auto | kokoro | sapi
    tts_voice: str = "af_heart"
    tts_speed: float = 1.0
    kokoro_model: str = "models/kokoro-v1.0.onnx"
    kokoro_voices: str = "models/voices-v1.0.bin"

    # calls
    max_calls: int = 3                 # at the same time (the GPU is shared)
    max_call_minutes: float = 10.0
    idle_prompt_s: float = 25.0        # silence before "are you still there?"
    idle_hangup_s: float = 25.0        # more silence after that before hanging up
    endpoint_ms: int = 650
    invites: str = "invites.json"
    log_dir: str = "logs"
    log_text: bool = True              # write what people said/heard into logs (never audio); turn off for privacy

    @staticmethod
    def load(path: str | Path = "config.json", **overrides) -> "Config":
        cfg, p = Config(), Path(path)
        names = {f.name for f in fields(Config)}
        if p.exists():
            for k, v in json.loads(p.read_text(encoding="utf-8")).items():
                if k in names:
                    setattr(cfg, k, v)
        for k, v in overrides.items():
            if v is not None and k in names:
                setattr(cfg, k, v)
        return cfg


def load_env(path: str | Path = ".env") -> list[str]:
    """Read KEY=VALUE lines from a .env file into the environment (values already set in the terminal win).
    This is where the Gemini key lives; the file is git-ignored. Returns the names that were set."""
    p, done = Path(path), []
    if not p.exists():
        return done
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v
            done.append(k)
    return done
