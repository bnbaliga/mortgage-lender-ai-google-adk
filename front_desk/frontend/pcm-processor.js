// AudioWorklet: mic (context rate, e.g. 48 kHz) -> 16 kHz mono 16-bit PCM in 40 ms chunks,
// plus an RMS level per chunk for client-side barge-in and the level ring.
// vs the DJ build: averages each output sample over its input window (a cheap low-pass,
// less aliasing for speech recognition) and batches 40 ms instead of posting every
// 128-frame render quantum (~375 tiny WebSocket messages a second).
const OUT_RATE = 16000;
const CHUNK = 640; // 40 ms @ 16 kHz

class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / OUT_RATE;
    this.acc = 0; this.n = 0; this.pos = 0;   // running average for the current output sample
    this.buf = new Int16Array(CHUNK); this.len = 0; this.sumsq = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i]; this.n++; this.pos++;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        const s = Math.max(-1, Math.min(1, this.acc / this.n));
        this.acc = 0; this.n = 0;
        this.buf[this.len++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        this.sumsq += s * s;
        if (this.len === CHUNK) {
          const out = this.buf;
          this.port.postMessage({ pcm: out.buffer, rms: Math.sqrt(this.sumsq / CHUNK) }, [out.buffer]);
          this.buf = new Int16Array(CHUNK); this.len = 0; this.sumsq = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm-processor", PCMProcessor);
