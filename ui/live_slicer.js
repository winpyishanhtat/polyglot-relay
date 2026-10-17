// Decides when to cut the live microphone stream into slices.
//
// Cutting every N seconds splits words (worst for Burmese, which has no spaces
// between words), sends silence to the transcriber (which may invent text for
// it), and gives each slice no natural start or end. Cutting at pauses fixes
// all three: a slice ends in a gap between phrases, and a slice with no speech
// in it is never sent.
//
// Pure logic with no browser APIs, so it can be tested under node. The page
// feeds it one loudness reading (RMS, 0..1) every ~50ms.

class LiveSlicer {
  constructor(options = {}) {
    this.minMs = options.minMs ?? 2000;      // never cut a slice shorter than this...
    this.maxMs = options.maxMs ?? 8000;      // ...or let one run longer than this
    this.pauseMs = options.pauseMs ?? 350;   // silence this long ends a phrase
    this.speechFloor = options.speechFloor ?? 0.015; // nothing quieter than this is speech
    this.noiseRatio = options.noiseRatio ?? 3;       // speech must beat the room noise by this factor
    this.noiseCap = options.noiseCap ?? 0.03;        // never assume the room is louder than this
    this.coldStart = options.coldStart ?? 0.06;      // before there is a noise estimate, only a reading this loud counts
    this.sendPeak = options.sendPeak ?? 0.02;        // a slice that never reached this is silence and is not sent
    this.noiseWindow = options.noiseWindow ?? 60;    // readings kept for the noise estimate (~3s at 50ms)
    this.noisePercentile = options.noisePercentile ?? 0.1; // the quietest tenth of them is taken as the room
    // Voice activity, beyond loudness:
    this.onsetFrames = options.onsetFrames ?? 2;     // speech must last this many readings (~100ms) to start:
                                                     // a click, a cough or a knocked table does not open a slice
    this.holdRatio = options.holdRatio ?? 0.6;       // once speech has started it keeps counting down to this
                                                     // fraction of the threshold, so soft word endings are not
                                                     // taken for a pause (hysteresis)
    this.voiceRatioMin = options.voiceRatioMin ?? 0.35; // when the page measures it: the share of energy in the
                                                     // voice band (300-3400 Hz) below which a sound is not a voice
                                                     // (hum, rumble, hiss, a fan)
    this.recent = [];
    this.above = 0;
    this.inSpeech = false;
    this.previous = 0;
    this.reset(0);
  }

  // Begin a new slice at nowMs.
  reset(nowMs) {
    this.sliceStart = nowMs;
    this.lastSpeechAt = null;
    this.heardSpeech = false;
    this.peak = 0;
  }

  // Room noise = a low percentile of recent readings (speech has gaps; a room
  // does not), not an average of the
  // first moments: if the speaker starts talking at once, an initial average
  // would take their voice for the room and then never hear speech again.
  noiseFloor() {
    if (this.recent.length < 10) return 0;
    const sorted = [...this.recent].sort((a, b) => a - b);
    return Math.min(sorted[Math.floor(sorted.length * this.noisePercentile)], this.noiseCap);
  }

  threshold() {
    // With under half a second of history there is no noise estimate yet, and
    // a noisy room's own hiss would pass for speech: only a clearly loud
    // reading counts until there is.
    if (this.recent.length < 10) return this.coldStart;
    return Math.max(this.speechFloor, this.noiseFloor() * this.noiseRatio);
  }

  // Whether the current slice is worth sending. Errs towards sending: a silent
  // slice only costs a transcription call, but dropping a quiet speaker's
  // words loses them for good. Only a slice that never rose above sendPeak
  // (genuine silence) is discarded.
  get hadSpeech() {
    return this.heardSpeech || this.peak >= this.sendPeak;
  }

  // Whether one reading is voice: loud enough (with hysteresis once speech has started), lasting long
  // enough, and - when the page can tell - with its energy where a voice's is.
  isVoice(rms, voiceRatio) {
    const voiced = voiceRatio === undefined || voiceRatio === null || voiceRatio >= this.voiceRatioMin;
    const level = this.inSpeech ? this.threshold() * this.holdRatio : this.threshold();
    if (!voiced || rms <= level) {
      this.above = 0;
      this.inSpeech = false;
      return false;
    }
    this.above += 1;
    if (this.inSpeech || this.above >= this.onsetFrames) {
      this.inSpeech = true;
      return true;
    }
    return false;
  }

  // Feed one reading. Returns {cut, reason, hadSpeech}: when cut is true the
  // current slice should end now (and hadSpeech says whether it is worth
  // sending); the caller then keeps going, as a new slice has already begun.
  // voiceRatio (optional, 0..1) is the share of the reading's energy in the voice band.
  push(rms, nowMs, voiceRatio) {
    this.recent.push(rms);
    if (this.recent.length > this.noiseWindow) this.recent.shift();
    if (voiceRatio === undefined || voiceRatio === null || voiceRatio >= this.voiceRatioMin) {
      // The quieter of two readings in a row: a sound must last to count, so a lone click is not a reason
      // to send a slice (and neither is a loud hum, above).
      this.peak = Math.max(this.peak, Math.min(rms, this.previous));
    }
    this.previous = rms;
    if (this.isVoice(rms, voiceRatio)) {
      this.heardSpeech = true;
      this.lastSpeechAt = nowMs;
    }
    const elapsed = nowMs - this.sliceStart;
    let reason = null;
    if (elapsed >= this.maxMs) {
      reason = "max";
    } else if (this.hadSpeech && elapsed >= this.minMs
               && this.lastSpeechAt !== null && nowMs - this.lastSpeechAt >= this.pauseMs) {
      reason = "pause";
    }
    if (!reason) return { cut: false, reason: null, hadSpeech: this.hadSpeech };
    const hadSpeech = this.hadSpeech;
    this.reset(nowMs);
    return { cut: true, reason, hadSpeech };
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = { LiveSlicer };
