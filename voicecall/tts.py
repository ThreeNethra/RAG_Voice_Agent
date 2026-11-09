"""Text to speech. `KokoroTTS` is the natural-sounding local voice; `SapiTTS` is the Windows built-in voice used
as a zero-download fallback. Both return float32 mono audio and its sample rate."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

from .audio import read_wav


class KokoroTTS:
    name = "kokoro"

    def __init__(self, model: str, voices: str, voice: str = "af_heart", speed: float = 1.0):
        self.model_path, self.voices_path, self.voice, self.speed = model, voices, voice, speed
        self.k = None
        self.sample_rate = 24000
        self.lock = threading.Lock()

    def available(self) -> bool:
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False
        return Path(self.model_path).is_file() and Path(self.voices_path).is_file()

    def load(self) -> str:
        from kokoro_onnx import Kokoro
        self.k = Kokoro(self.model_path, self.voices_path)
        self.synth("Ready.")
        return f"kokoro voice {self.voice}"

    def synth(self, text: str) -> np.ndarray:
        if self.k is None:
            self.load()
        with self.lock:
            samples, sr = self.k.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        self.sample_rate = int(sr)
        return np.asarray(samples, dtype=np.float32)


_PS = r"""param([string]$TextFile,[string]$Out,[int]$Rate=0)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = $Rate
$s.SetOutputToWaveFile($Out)
$s.Speak([System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8))
$s.Dispose()
"""


class SapiTTS:
    name = "windows-sapi"

    def __init__(self, speed: float = 1.0):
        self.rate = max(-5, min(5, round((speed - 1.0) * 10)))
        self.sample_rate = 22050
        self.script = Path(tempfile.gettempdir()) / "lumora_sapi.ps1"

    def available(self) -> bool:
        return sys.platform == "win32"

    def load(self) -> str:
        self.script.write_text(_PS, encoding="utf-8")
        self.synth("Ready.")
        return "windows built-in voice"

    def synth(self, text: str) -> np.ndarray:
        if not self.script.exists():
            self.script.write_text(_PS, encoding="utf-8")
        with tempfile.TemporaryDirectory() as d:
            tf, out = Path(d) / "t.txt", Path(d) / "o.wav"
            tf.write_text(text, encoding="utf-8")
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script), "-TextFile", str(tf),
                            "-Out", str(out), "-Rate", str(self.rate)], check=True, capture_output=True, timeout=60)
            x, sr = read_wav(out.read_bytes())
        self.sample_rate = sr
        return x


class ToneTTS:
    """Tests and dry runs: audio whose length follows the text (about 14 characters per second)."""
    name = "tone"
    sample_rate = 24000

    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s

    def load(self) -> str:
        return "tone"

    def synth(self, text: str) -> np.ndarray:
        import time

        from .audio import tone
        if self.delay_s:
            time.sleep(self.delay_s)
        return tone(max(0.2, len(text) / 14.0), sr=self.sample_rate)


def make_tts(cfg):
    k = KokoroTTS(cfg.kokoro_model, cfg.kokoro_voices, cfg.tts_voice, cfg.tts_speed)
    if cfg.tts in ("auto", "kokoro") and k.available():
        return k
    if cfg.tts == "kokoro":
        raise RuntimeError("Kokoro is selected but the package or model files are missing: run  python manage.py doctor")
    s = SapiTTS(cfg.tts_speed)
    if cfg.tts in ("auto", "sapi") and s.available():
        return s
    raise RuntimeError("no voice available: install Kokoro (python manage.py fetch-models) or use Windows")
