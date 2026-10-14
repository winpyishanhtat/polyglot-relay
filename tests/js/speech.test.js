// Run with: node --test tests/js/speech.test.js tests/js/listen.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { createSpeaker, BCP47 } = require(path.join(__dirname, "..", "..", "ui", "speech.js"));

class Utterance {
  constructor(text) { this.text = text; }
}

// A speechSynthesis that "speaks" instantly (or fails) and records what it was given.
function fakeSynth(voices, { fail = false } = {}) {
  const synth = {
    spoken: [], cancelled: 0,
    getVoices: () => voices,
    speak(utterance) {
      synth.spoken.push(utterance);
      setImmediate(() => (fail ? utterance.onerror({ error: "synthesis-failed" }) : utterance.onend()));
    },
    cancel() { synth.cancelled += 1; },
  };
  return synth;
}

function fakeServer({ ok = true, error = "boom" } = {}) {
  const server = { requests: [], urls: { created: [], revoked: [] }, audios: [] };
  server.fetch = async (url, init) => {
    server.requests.push({ url, ...init, body: JSON.parse(init.body) });
    return ok
      ? { ok: true, blob: async () => "wav-bytes" }
      : { ok: false, json: async () => ({ error }) };
  };
  server.URL = {
    createObjectURL: (blob) => { server.urls.created.push(blob); return `blob:${server.urls.created.length}`; },
    revokeObjectURL: (url) => server.urls.revoked.push(url),
  };
  server.createAudio = (url) => {
    const audio = { url, paused: false, play() { setImmediate(() => audio.onended && audio.onended()); return Promise.resolve(); },
      pause() { audio.paused = true; } };
    server.audios.push(audio);
    return audio;
  };
  return server;
}

function speaker({ voices = [], synth, server = fakeServer() } = {}) {
  const speechSynthesis = synth || fakeSynth(voices);
  return {
    server, synth: speechSynthesis,
    speaker: createSpeaker({ speechSynthesis, SpeechSynthesisUtterance: Utterance, fetch: server.fetch, URL: server.URL,
      createAudio: server.createAudio, headers: () => ({ "Content-Type": "application/json", "X-Access-Token": "t" }) }),
  };
}

test("every language has a locale tag", () => {
  for (const code of ["en", "my", "id", "ms", "th", "vi", "tl", "km", "lo"]) assert.ok(BCP47[code], code);
});

test("a browser voice for the language is used, and the server is not asked", async () => {
  const { speaker: s, synth, server } = speaker({ voices: [{ lang: "en-GB" }, { lang: "th-TH", name: "Kanya" }] });
  assert.equal(await s.speak("sawasdee", "th"), "browser");
  assert.equal(synth.spoken[0].text, "sawasdee");
  assert.equal(synth.spoken[0].voice.name, "Kanya");
  assert.equal(server.requests.length, 0);
});

test("Filipino is found under either of its codes, and underscores in tags are tolerated", async () => {
  for (const tag of ["fil-PH", "tl-PH", "fil_PH"]) {
    const { speaker: s, server } = speaker({ voices: [{ lang: tag }] });
    assert.equal(await s.speak("kumusta", "tl"), "browser", tag);
    assert.equal(server.requests.length, 0);
  }
});

test("a language the browser cannot speak goes to the relay and plays the WAV it returns", async () => {
  const { speaker: s, server } = speaker({ voices: [{ lang: "en-US" }] });
  assert.equal(await s.speak("mingalaba", "my"), "server");
  assert.equal(server.requests.length, 1);
  assert.equal(server.requests[0].url, "/api/tts");
  assert.equal(server.requests[0].method, "POST");
  assert.deepEqual(server.requests[0].body, { text: "mingalaba", lang: "my" });
  assert.equal(server.requests[0].headers["X-Access-Token"], "t");
  assert.equal(server.audios[0].url, "blob:1");
  assert.deepEqual(server.urls.revoked, ["blob:1"]);
});

test("with no speech support in the browser at all the relay is used", async () => {
  const server = fakeServer();
  const s = createSpeaker({ fetch: server.fetch, URL: server.URL, createAudio: server.createAudio });
  assert.equal(await s.speak("hello", "en"), "server");
  assert.equal(s.hasBrowserVoice("en"), false);
});

test("a browser voice that fails falls back to the relay", async () => {
  const synth = fakeSynth([{ lang: "th-TH" }], { fail: true });
  const { speaker: s, server } = speaker({ synth });
  assert.equal(await s.speak("sawasdee", "th"), "server");
  assert.equal(server.requests.length, 1);
});

test("the relay's own error message is what the caller sees", async () => {
  const { speaker: s } = speaker({ server: fakeServer({ ok: false, error: "The daily speech quota is used up" }) });
  await assert.rejects(s.speak("hello", "lo"), /daily speech quota/);
});

test("an unreadable error reply still gives a message", async () => {
  const server = fakeServer();
  server.fetch = async () => ({ ok: false, json: async () => { throw new Error("html"); } });
  const { speaker: s } = speaker({ server });
  await assert.rejects(s.speak("hello", "lo"), /not available/);
});

test("captions are spoken one after another, in the order given", async () => {
  const order = [];
  const synth = fakeSynth([{ lang: "en-US" }]);
  synth.speak = (u) => { order.push(`start ${u.text}`); setTimeout(() => { order.push(`end ${u.text}`); u.onend(); }, 5); };
  const { speaker: s } = speaker({ synth });
  await Promise.all([s.speak("one", "en"), s.speak("two", "en"), s.speak("three", "en")]);
  assert.deepEqual(order, ["start one", "end one", "start two", "end two", "start three", "end three"]);
});

test("one failing caption does not stop the ones after it", async () => {
  const { speaker: s } = speaker({ server: fakeServer({ ok: false }) });
  const first = s.speak("a", "my");
  const second = s.speak("b", "my");
  await assert.rejects(first);
  await assert.rejects(second, /boom|not available/);
});

test("stop drops what is queued, cancels the browser and silences the audio", async () => {
  const server = fakeServer();
  let endByItself = false;
  server.createAudio = (url) => {                       // audio that never ends by itself, until told to
    const audio = { url, play() { if (endByItself) setImmediate(() => audio.onended()); return Promise.resolve(); },
      pause() { audio.paused = true; } };
    server.audios.push(audio);
    return audio;
  };
  const { speaker: s, synth } = speaker({ server });
  const playing = s.speak("long", "my");
  const queued = s.speak("later", "my");
  await new Promise((resolve) => setTimeout(resolve, 20));
  s.stop();
  assert.equal(await playing, "server");               // the playing clip ends promptly
  await assert.rejects(queued, /stopped/);
  assert.equal(server.audios[0].paused, true);
  assert.equal(synth.cancelled, 1);
  assert.equal(server.requests.length, 1);              // the queued one never reached the relay
  endByItself = true;
  assert.equal(await s.speak("after", "my"), "server"); // and the speaker works again
});
