"""Speech to text. `WhisperSTT` runs faster-whisper on the GPU (falls back to CPU if CUDA cannot start).

The recogniser is primed with words from the company documents (product codes, names) so "CT-300" is not heard
as "CT three hundred" or "seat 300".
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# What Whisper "hears" in silence or noise. Dropped when the clip is short; a real "thank you" after an answer is usually longer or handled as a command.
HALLUCINATIONS = {"you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "bye bye", "the end",
                  "so", "uh", "um", "oh", "hmm", "mm", "i", "okay", "ok", "yeah", "subtitles by the amara org community"}


@dataclass
class STTResult:
    text: str
    ms: int
    note: str = ""


def clean_transcript(text: str, seconds: float) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    norm = re.sub(r"[^\w\s]", "", t.lower()).strip()
    if not norm:
        return ""
    words = norm.split()
    if len(words) >= 4 and len(set(words)) <= max(1, len(words) // 4):      # "the the the the the" loops
        return ""
    if norm in HALLUCINATIONS and seconds < 1.6:
        return ""
    return t


def build_prompt(corpus_dir: str | Path, limit: int = 200) -> str:
    """A short list of the distinctive terms in the documents: capitalised names and tokens with digits, most frequent first."""
    c: Counter = Counter()
    for f in Path(corpus_dir).glob("*.md"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        c.update(re.findall(r"\b[A-Z]{2,}[A-Za-z]*-?\d*\b|\b[A-Z][a-z]+[A-Z]\w*\b|\b[A-Z][a-z]{3,}\b|\b[A-Za-z]+-\d+\b", text))
    stop = {"This", "That", "These", "Those", "There", "When", "What", "Which", "Section", "Table", "Note", "From", "With", "Each", "All", "The"}
    terms = [w for w, _ in c.most_common(80) if w not in stop]
    out = "Company documents. Terms: " + ", ".join(terms)
    return out[:limit * 2]


def add_cuda_dll_dirs() -> list[str]:
    """On Windows the CUDA libraries installed with pip (nvidia-cublas-cu12, nvidia-cudnn-cu12) live inside site-packages;
    ctranslate2 only finds them if those folders are on the DLL search path."""
    added = []
    if sys.platform != "win32":
        return added
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if nv.is_dir():
            for sub in nv.iterdir():
                b = sub / "bin"
                if b.is_dir():
                    try:
                        os.add_dll_directory(str(b))
                    except (OSError, AttributeError):
                        pass
                    os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
                    added.append(str(b))
    return added


class WhisperSTT:
    name = "faster-whisper"

    def __init__(self, model="small.en", device="auto", compute="auto", beam=1, prompt=""):
        self.model_name, self.device_req, self.compute_req, self.beam, self.prompt = model, device, compute, beam, prompt
        self.model, self.device, self.compute = None, "", ""
        self.lock = threading.Lock()           # one transcription at a time on the GPU

    def load(self) -> str:
        add_cuda_dll_dirs()
        from faster_whisper import WhisperModel
        tries = []
        if self.device_req in ("auto", "cuda"):
            tries.append(("cuda", self.compute_req if self.compute_req != "auto" else "float16"))
        if self.device_req in ("auto", "cpu"):
            tries.append(("cpu", self.compute_req if self.compute_req not in ("auto", "float16") else "int8"))
        err = None
        for dev, comp in tries:
            try:
                m = WhisperModel(self.model_name, device=dev, compute_type=comp)
                list(m.transcribe(np.zeros(16000, np.float32), language="en", beam_size=1)[0])   # surfaces missing CUDA DLLs now, not on the first call
                self.model, self.device, self.compute = m, dev, comp
                return f"{self.model_name} on {dev} ({comp})"
            except Exception as e:                       # noqa: BLE001 - any CUDA/driver failure means: try the next option
                err = e
        raise RuntimeError(f"could not load speech recognition: {err}")

    def transcribe(self, audio: np.ndarray) -> STTResult:
        t0 = time.perf_counter()
        if self.model is None:
            self.load()
        with self.lock:
            segs, _ = self.model.transcribe(audio, language="en", beam_size=self.beam, vad_filter=False, temperature=0.0,
                                            condition_on_previous_text=False, without_timestamps=True,
                                            initial_prompt=self.prompt or None)
            parts = [s.text for s in segs if getattr(s, "no_speech_prob", 0.0) < 0.7]
        text = clean_transcript(" ".join(parts), len(audio) / 16000)
        return STTResult(text, round((time.perf_counter() - t0) * 1000))


class ScriptedSTT:
    """For tests and dry runs: returns queued transcripts instead of listening."""
    name = "scripted"

    def __init__(self, texts=(), delay_s: float = 0.0):
        self.texts, self.delay_s, self.seen = list(texts), delay_s, []

    def load(self) -> str:
        return "scripted"

    def transcribe(self, audio: np.ndarray) -> STTResult:
        self.seen.append(len(audio) / 16000)
        if self.delay_s:
            time.sleep(self.delay_s)
        return STTResult(self.texts.pop(0) if self.texts else "", 1)


def make_stt(cfg, corpus_dir):
    return WhisperSTT(cfg.stt_model, cfg.stt_device, cfg.stt_compute, cfg.stt_beam, build_prompt(corpus_dir))
