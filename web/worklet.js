// Runs on the audio thread: takes microphone audio at the device rate, resamples it to 16 kHz with a box filter
// (averaging, which also removes frequencies the recogniser cannot use) and posts 40 ms of PCM16 at a time.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.need = this.ratio;
    this.sum = 0;
    this.out = new Int16Array(640);
    this.o = 0;
    this.sq = 0;
  }
  emit() {
    const v = Math.max(-1, Math.min(1, this.sum / this.ratio));
    this.out[this.o++] = v * 32767;
    this.sq += v * v;
    this.sum = 0;
    this.need = this.ratio;
    if (this.o === this.out.length) {
      const rms = Math.sqrt(this.sq / this.o);
      this.port.postMessage({ pcm: this.out.buffer, rms }, [this.out.buffer]);
      this.out = new Int16Array(640);
      this.o = 0;
      this.sq = 0;
    }
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      const v = ch[i];
      if (this.need >= 1) {
        this.sum += v;
        this.need -= 1;
        if (this.need === 0) this.emit();
      } else {
        this.sum += v * this.need;
        const rem = 1 - this.need;
        this.emit();
        this.sum = v * rem;
        this.need = this.ratio - rem;
      }
    }
    return true;
  }
}
registerProcessor("capture", Capture);
