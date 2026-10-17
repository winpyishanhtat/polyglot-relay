// Reads a caption aloud. The browser's own voices are used when it has one for the language (free, instant,
// offline); otherwise the relay's POST /api/tts (Gemini speech) returns a WAV to play. Burmese, Khmer and Lao
// are the languages browsers most often lack.
(function (root) {
  const BCP47 = {
    en: "en-US", my: "my-MM", id: "id-ID", ms: "ms-MY", th: "th-TH",
    vi: "vi-VN", tl: "fil-PH", km: "km-KH", lo: "lo-LA",
  };
  // A browser voice may be tagged with either the ISO 639-1 or the 639-2 code for a language.
  const VOICE_PREFIXES = { tl: ["fil", "tl"], my: ["my", "mya", "bur"], km: ["km", "khm"], lo: ["lo", "lao"] };

  function createSpeaker(env) {
    const synth = env.speechSynthesis || null;
    const Utterance = env.SpeechSynthesisUtterance || null;
    const post = env.fetch;
    const headers = env.headers || (() => ({ "Content-Type": "application/json" }));
    const makeAudio = env.createAudio || ((url) => new env.Audio(url));
    const urls = env.URL || { createObjectURL: () => "", revokeObjectURL: () => {} };

    let generation = 0;     // bumped by stop(): everything queued before it is dropped
    let tail = Promise.resolve();
    let playing = null;     // the Audio element currently playing, so stop() can silence it

    function browserVoices(lang) {
      if (!synth || !Utterance || typeof synth.getVoices !== "function") return [];
      const prefixes = VOICE_PREFIXES[lang] || [lang];
      return (synth.getVoices() || []).filter((v) =>
        prefixes.some((p) => String(v.lang || "").toLowerCase().replace("_", "-").split("-")[0] === p));
    }

    // The browser voice to use: the one chosen by name, else the first for the language.
    function browserVoice(lang, name) {
      const voices = browserVoices(lang);
      return (name && voices.find((v) => v.name === name)) || voices[0] || null;
    }

    function viaBrowser(text, lang, voice) {
      return new Promise((resolve, reject) => {
        const utterance = new Utterance(text);
        utterance.voice = voice;
        utterance.lang = voice.lang || BCP47[lang] || lang;
        utterance.onend = () => resolve("browser");
        utterance.onerror = (event) => reject(new Error((event && event.error) || "The browser could not speak this"));
        synth.speak(utterance);
      });
    }

    async function viaServer(text, lang, relayVoice) {
      const body = relayVoice ? { text, lang, voice: relayVoice } : { text, lang };
      const response = await post("/api/tts", { method: "POST", headers: headers(), body: JSON.stringify(body) });
      const left = response.headers && typeof response.headers.get === "function" ? response.headers.get("X-Speech-Quota-Left") : null;
      if (left !== null && left !== undefined && typeof env.onQuota === "function") env.onQuota(Number(left));
      if (!response.ok) {
        let message = "Speech is not available right now";
        try { message = (await response.json()).error || message; } catch (_) { /* keep the generic message */ }
        throw new Error(message);
      }
      const url = urls.createObjectURL(await response.blob());
      try {
        await new Promise((resolve, reject) => {
          const audio = makeAudio(url);
          playing = audio;
          audio.onended = () => resolve();
          audio.onerror = () => reject(new Error("The audio could not be played"));
          const started = audio.play();
          if (started && typeof started.catch === "function") started.catch(reject);
        });
      } finally {
        playing = null;
        urls.revokeObjectURL(url);
      }
      return "server";
    }

    // choice: undefined (best available), { browser: "voice name" }, or { relay: "Kore" } for the relay's voice.
    async function speakNow(text, lang, choice) {
      if (!(choice && choice.relay)) {
        const voice = browserVoice(lang, choice && choice.browser);
        if (voice) {
          try { return await viaBrowser(text, lang, voice); } catch (_) { /* fall through to the server voice */ }
        }
      }
      return viaServer(text, lang, choice && choice.relay);
    }

    return {
      // Speaks after anything already queued. Resolves with "browser" or "server"; rejects with a readable
      // message when neither could (and with "stopped" if stop() was called first).
      speak(text, lang, choice) {
        const mine = generation;
        const run = tail.then(() => {
          if (mine !== generation) throw new Error("stopped");
          return speakNow(String(text), lang, choice);
        });
        tail = run.catch(() => {});
        return run;
      },
      stop() {
        generation += 1;
        if (synth && typeof synth.cancel === "function") synth.cancel();
        if (playing && typeof playing.pause === "function") playing.pause();
        if (playing) { const audio = playing; playing = null; if (typeof audio.onended === "function") audio.onended(); }
      },
      hasBrowserVoice: (lang) => Boolean(browserVoice(lang)),
      browserVoiceNames: (lang) => browserVoices(lang).map((v) => v.name).filter(Boolean),
    };
  }

  const api = { BCP47, createSpeaker };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PolyglotSpeech = api;
})(typeof window !== "undefined" ? window : globalThis);
