"""Endpointer and audio helpers."""
import numpy as np
from helpers import SR, check, finish, noise

from voicecall.audio import Endpointer, float_to_pcm16, pcm16_to_float, resample, tone, wav_bytes, read_wav


def run(sig, speaking=False, ep=None, chunk=640):
    ep = ep or Endpointer()
    pcm, ev = float_to_pcm16(sig), []
    for i in range(0, len(pcm), chunk):
        ev += ep.feed(pcm[i:i + chunk], agent_speaking=speaking)
    return ev, ep


ev, ep = run(np.concatenate([noise(1.0), tone(1.5), noise(1.2)]))
kinds = [e[0] for e in ev]
check("one utterance: start then end", kinds == ["start", "end"], str(kinds))
check("utterance length is speech plus preroll and a short tail", 1.6 < len(ev[1][1]) / SR < 2.3, str(len(ev[1][1]) / SR))
a = ev[1][1]
check("the first syllable is not clipped (speech starts after a little preroll silence, not at sample 0)", np.abs(a[: int(0.08 * SR)]).max() < 0.05 and np.abs(a[int(0.2 * SR): int(0.5 * SR)]).max() > 0.05)

ev, _ = run(np.concatenate([noise(1.0), tone(0.1), noise(1.0)]))
check("a 100 ms click is ignored", ev == [], str(ev))
ev, _ = run(np.concatenate([noise(1.0), tone(0.2), noise(1.0)]))
check("a 200 ms burst is dropped as too short", [e[0] for e in ev] in (["start", "drop"], []), str([e[0] for e in ev]))

ev, _ = run(np.concatenate([noise(1.0), tone(1.0), noise(0.3), tone(1.0), noise(1.2)]))
check("a short pause inside a sentence does not split it", [e[0] for e in ev] == ["start", "end"], str([e[0] for e in ev]))
ev, _ = run(np.concatenate([noise(1.0), tone(1.0), noise(1.0), tone(1.0), noise(1.2)]))
check("a long pause makes two utterances", [e[0] for e in ev] == ["start", "end", "start", "end"], str([e[0] for e in ev]))

ev, _ = run(np.concatenate([noise(1.0), tone(1.5, amp=0.04), noise(1.2)]))
check("quiet speech (-30 dB) is still detected", [e[0] for e in ev] == ["start", "end"], str([e[0] for e in ev]))
ev, _ = run(noise(5.0, amp=0.01))
check("steady background hiss (-40 dB) is not speech", ev == [], str(ev))
ev, _ = run(np.concatenate([noise(2.0, amp=0.003), tone(1.0, amp=0.3), noise(1.5, amp=0.003)]))
check("speech over moderate noise is detected", [e[0] for e in ev] == ["start", "end"], str([e[0] for e in ev]))

ev, _ = run(np.concatenate([noise(1.0), tone(0.15), noise(1.0)]), speaking=True)
check("while the assistant speaks, a short burst does not interrupt", ev == [], str(ev))
ev, _ = run(np.concatenate([noise(1.0), tone(0.8), noise(1.0)]), speaking=True)
check("while the assistant speaks, real speech interrupts (start flagged barge)", ev and ev[0] == ("start", True), str(ev[:1]))
ev, _ = run(np.concatenate([noise(1.0), tone(1.0, amp=0.012), noise(1.0)]), speaking=True)
check("while the assistant speaks, faint noise does not interrupt", ev == [], str(ev))

ev, _ = run(np.concatenate([noise(0.5), tone(30.0)]))
check("a monologue is cut at the maximum length", any(e[0] == "end" for e in ev) and len(next(e for e in ev if e[0] == "end")[1]) / SR <= 20.5)

ep = Endpointer()
pcm = float_to_pcm16(np.concatenate([noise(1.0), tone(1.0), noise(1.0)]))
ev = []
for i in range(0, len(pcm), 7):                                  # awkward odd chunk sizes (7 bytes)
    ev += ep.feed(pcm[i:i + 7])
check("odd chunk sizes still work", [e[0] for e in ev] == ["start", "end"], str([e[0] for e in ev]))

x = tone(0.5)
check("pcm16 round trip", np.abs(pcm16_to_float(float_to_pcm16(x)) - x).max() < 1e-3)
check("resample changes length", abs(len(resample(x, 16000, 24000)) - 12000) <= 1 and len(resample(x, 16000, 16000)) == len(x))
y, sr = read_wav(wav_bytes(x, 16000))
check("wav round trip", sr == 16000 and len(y) == len(x))
finish()
