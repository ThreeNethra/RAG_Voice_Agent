"""Audio helpers and the end-of-speech detector.

The client sends 16 kHz mono signed 16-bit PCM. `Endpointer` finds where an utterance starts and ends from the
loudness of 20 ms frames against a slowly adapting noise floor, keeps 300 ms of audio from before the start (so the
first syllable is not clipped) and cuts the utterance after `endpoint_ms` of silence. While the assistant is
speaking, a longer and louder burst is required to count as an interruption, so a cough or a bit of echo does not
cut the answer off.
"""
from __future__ import annotations

import io
import math
import wave
from collections import deque
from dataclasses import dataclass

import numpy as np

SR = 16000
FRAME = 320                        # 20 ms at 16 kHz


def pcm16_to_float(b: bytes) -> np.ndarray:
    return np.frombuffer(b[: len(b) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear-interpolation resampler (plenty for speech; used for the loopback self test and tests)."""
    if src == dst or len(x) == 0:
        return x.astype(np.float32)
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as w:
        raw, ch, width, sr = w.readframes(w.getnframes()), w.getnchannels(), w.getsampwidth(), w.getframerate()
    if width != 2:
        raise ValueError("only 16-bit WAV is supported")
    x = pcm16_to_float(raw)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def wav_bytes(x: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(float_to_pcm16(x))
    return buf.getvalue()


def tone(seconds: float, hz: float = 220.0, amp: float = 0.3, sr: int = SR, harmonics: int = 3) -> np.ndarray:
    """A voice-like test signal: a few harmonics with a slow syllable-rate amplitude wobble."""
    t = np.arange(int(seconds * sr)) / sr
    x = sum(np.sin(2 * math.pi * hz * (k + 1) * t) / (k + 1) for k in range(harmonics))
    x = x / max(1e-9, np.abs(x).max())
    return (amp * x * (0.75 + 0.25 * np.sin(2 * math.pi * 4 * t))).astype(np.float32)


def db(x: np.ndarray) -> float:
    return 20.0 * math.log10(float(np.sqrt(np.mean(np.square(x))) if len(x) else 0.0) + 1e-9)


@dataclass
class EndpointConfig:
    endpoint_ms: int = 650         # silence that ends an utterance
    start_ms: int = 120            # voiced time that starts one
    barge_ms: int = 260            # voiced time that interrupts the assistant
    barge_margin_db: float = 18.0  # how far above the noise floor an interruption must be
    margin_db: float = 14.0        # normal speech margin above the noise floor
    abs_floor_db: float = -50.0    # never treat anything quieter than this as speech
    min_speech_ms: int = 250       # a shorter voiced total is a click or a cough: dropped
    max_utterance_s: float = 20.0
    preroll_ms: int = 300


class Endpointer:
    def __init__(self, cfg: EndpointConfig | None = None):
        self.cfg = cfg or EndpointConfig()
        self.reset()

    def reset(self) -> None:
        self.buf = bytearray()
        self.noise = -55.0
        self.calib = 0
        self.active = False
        self.voiced_run = 0
        self.silent_run = 0
        self.voiced_total = 0
        self.preroll: deque = deque(maxlen=max(1, self.cfg.preroll_ms // 20))
        self.utt: list = []
        self.level = -90.0
        self.started_as_barge = False

    def _update_noise(self, level: float) -> None:
        if self.calib < 10:                       # the first 200 ms define the floor
            self.noise = level if self.calib == 0 else (self.noise * self.calib + level) / (self.calib + 1)
            self.calib += 1
        elif level < self.noise:
            self.noise = self.noise * 0.9 + level * 0.1
        else:
            self.noise = self.noise * 0.995 + level * 0.005
        self.noise = min(-35.0, max(-75.0, self.noise))

    def feed(self, pcm: bytes, agent_speaking: bool = False) -> list:
        """Process bytes of 16 kHz PCM16. Returns events: ('start', barge:bool), ('end', float32 audio), ('drop',)."""
        c, events = self.cfg, []
        self.buf += pcm
        while len(self.buf) >= FRAME * 2:
            frame = bytes(self.buf[: FRAME * 2])
            del self.buf[: FRAME * 2]
            x = pcm16_to_float(frame)
            level = self.level = db(x)
            margin = c.barge_margin_db if (agent_speaking and not self.active) else c.margin_db
            voiced = level > max(self.noise + margin, c.abs_floor_db) and self.calib >= 10
            if not voiced:
                self._update_noise(level)
            if not self.active:
                self.preroll.append(x)
                self.voiced_run = self.voiced_run + 1 if voiced else 0
                need = (c.barge_ms if agent_speaking else c.start_ms) // 20
                if self.voiced_run >= need:
                    self.active, self.started_as_barge = True, agent_speaking
                    self.utt = list(self.preroll)
                    self.silent_run, self.voiced_total = 0, self.voiced_run
                    events.append(("start", agent_speaking))
                continue
            self.utt.append(x)
            if voiced:
                self.silent_run = 0
                self.voiced_total += 1
            else:
                self.silent_run += 1
            too_long = len(self.utt) * 20 >= c.max_utterance_s * 1000
            if self.silent_run * 20 >= c.endpoint_ms or too_long:
                keep_tail = 10                    # 200 ms of trailing silence is enough for the recogniser
                tail = max(0, self.silent_run - keep_tail)
                audio = np.concatenate(self.utt[: len(self.utt) - tail]) if self.utt else np.zeros(0, np.float32)
                voiced_ms = self.voiced_total * 20
                self.active, self.voiced_run, self.utt, self.silent_run = False, 0, [], 0
                self.preroll.clear()
                events.append(("end", audio) if voiced_ms >= c.min_speech_ms else ("drop",))
        return events
