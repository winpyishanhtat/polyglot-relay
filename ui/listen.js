// A phone- and projector-friendly page that shows what is being said in several languages at once: every
// spoken sentence appears with its translation in each language you have switched on, as they arrive.
// /listen shows all of them; /listen/th starts with just that one (more can be added); ?langs=th,my,km picks.
(function (root) {
  const POLL_MS = 1000;
  const STREAM_POLL_MS = 10000;    // with pushed captions, polling is only a safety net
  const WINDOW_MINUTES = 30;
  const EARLIER_SHOWN = 10;
  const MAX_QUEUED_SPEECH = 2;     // a caption read late is worse than one skipped

  // ---- pure helpers (tested in tests/js/listen.test.js) -------------------------------------------

  function languageFromPath(pathname) {
    const match = /^\/listen\/([a-z]{2,3})\/?$/.exec(pathname || "");
    return match ? match[1] : null;
  }

  // The languages to show: ?langs=th,my wins, then the language in the path, then what was chosen last time,
  // then all of them. Unknown codes are dropped and the order is always the page's own.
  function chooseLanguages(all, { search = "", pathname = "", stored = "" } = {}) {
    const known = all.map((entry) => entry.code);
    const clean = (list) => known.filter((code) => list.includes(code));
    const fromQuery = clean(String(new URLSearchParams(search).get("langs") || "").split(","));
    if (fromQuery.length) return fromQuery;
    const fromPath = languageFromPath(pathname);
    if (fromPath && known.includes(fromPath)) return [fromPath];
    const fromStore = clean(String(stored || "").split(","));
    return fromStore.length ? fromStore : known;
  }

  // Folds a /api/history reply into `chunks` (chunk_id -> {number, original, source, translations}) and returns
  // the translations that are new, in speaking order. A chunk appears once any chosen language has arrived;
  // the others fill in later, and a chunk translated late is still reported then, whatever its number.
  function mergeChunks(chunks, rows, langs) {
    const arrivals = [];
    for (const row of rows || []) {
      if (!row || !row.translations) continue;
      for (const lang of langs) {
        const translation = row.translations[lang];
        if (!translation || !translation.text) continue;
        let chunk = chunks.get(row.chunk_id);
        if (!chunk) {
          chunk = { id: row.chunk_id, number: row.number || 0, original: row.text || "", source: row.source_lang || "",
            at: row.created_at || 0, translations: {} };
          chunks.set(row.chunk_id, chunk);
        }
        if (chunk.translations[lang] !== undefined) continue;
        chunk.translations[lang] = translation.text;
        arrivals.push({ id: row.chunk_id, lang, text: translation.text, number: chunk.number, at: translation.timestamp || chunk.at });
      }
    }
    return arrivals.sort(order);
  }

  function order(a, b) { return (a.number - b.number) || (a.at - b.at); }

  function newestChunks(chunks, count) {
    return [...chunks.values()].sort(order).reverse().slice(0, count);
  }

  // What to read aloud from a batch of arrivals in one language: the newest few, still in order.
  function toSpeak(arrivals, lang) {
    const mine = lang ? arrivals.filter((entry) => entry.lang === lang) : arrivals;
    return mine.slice(-MAX_QUEUED_SPEECH);
  }

  const api = { languageFromPath, chooseLanguages, mergeChunks, newestChunks, toSpeak, order };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PolyglotListen = api;

  if (typeof document === "undefined" || !document.getElementById || !document.getElementById("listen-view")) return;

  // ---- the page ----------------------------------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  $("listen-fallback").hidden = true;       // the scripts loaded, so the page is being served by the relay
  let accessToken = "";
  try {
    accessToken = new URLSearchParams(window.location.search).get("token") || sessionStorage.getItem("polyglot-token") || "";
    if (accessToken) sessionStorage.setItem("polyglot-token", accessToken);
  } catch (_) { /* no storage here: stay open */ }
  // The room this page follows: ?room=hall-a, or the default room.
  let room = "main";
  try { room = (new URLSearchParams(window.location.search).get("room") || "main").toLowerCase(); } catch (_) { /* default */ }
  const roomQuery = room === "main" ? "" : `&room=${encodeURIComponent(room)}`;
  function stored(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
  function store(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* optional */ } }

  const state = $("listen-state");
  const setState = (online, text) => {
    state.classList.toggle("online", online);
    state.lastElementChild.textContent = text;
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function start() {
    const { languages } = await (await fetch("/api/languages")).json();
    const byCode = Object.fromEntries(languages.map((entry) => [entry.code, entry]));
    let langs = chooseLanguages(languages, { search: window.location.search, pathname: window.location.pathname,
      stored: stored("polyglot-listen-langs") });
    let reading = false;
    let readLang = langs[0];
    let size = Number(stored("polyglot-listen-size")) || 22;
    let chunks = new Map();
    let first = true;
    let generation = 0;

    $("listen-view").hidden = false;
    document.title = room === "main" ? "Live captions — Polyglot Relay" : `Live captions · ${room} — Polyglot Relay`;
    const roomLabel = $("listen-room");
    if (roomLabel) { roomLabel.textContent = room === "main" ? "" : `Room ${room}`; roomLabel.hidden = room === "main"; }

    // The relay's voices and today's speech allowance; the page warns before relay-voice clips run out.
    let relayVoices = [];
    let quotaLeft = null;
    let warnWhenLeft = 3;
    const speaker = root.PolyglotSpeech.createSpeaker({
      speechSynthesis: window.speechSynthesis, SpeechSynthesisUtterance: window.SpeechSynthesisUtterance,
      fetch: window.fetch.bind(window), Audio: window.Audio, URL: window.URL,
      headers: () => ({ "Content-Type": "application/json", ...(accessToken ? { "X-Access-Token": accessToken } : {}) }),
      onQuota: (left) => { quotaLeft = left; describeReading(); },
    });
    const note = $("listen-note");
    const button = $("listen-speak");
    const readSelect = $("listen-read-lang");
    const voiceSelect = $("listen-voice");
    try {
      const info = await (await fetch("/api/tts/info")).json();
      relayVoices = Array.isArray(info.voices) ? info.voices : [];
      quotaLeft = info.quota && typeof info.quota.left === "number" ? info.quota.left : null;
      warnWhenLeft = info.warn_when_left || warnWhenLeft;
    } catch (_) { /* speech details are optional */ }

    // A voice choice per language, remembered: "" (best available), "b:<device voice>" or "r:<relay voice>".
    function voiceChoice(lang) {
      const saved = stored(`polyglot-voice-${lang}`) || "";
      if (saved.startsWith("b:")) return { browser: saved.slice(2) };
      if (saved.startsWith("r:")) return { relay: saved.slice(2) };
      return undefined;
    }
    function usesRelay(lang) {
      const choice = voiceChoice(lang);
      return Boolean(choice && choice.relay) || !speaker.hasBrowserVoice(lang);
    }
    function drawVoices() {
      if (!voiceSelect) return;
      const automatic = element("option", "", "Voice: automatic");
      automatic.value = "";
      const device = (speaker.browserVoiceNames ? speaker.browserVoiceNames(readLang) : []).map((name) => {
        const option = element("option", "", `Device: ${name}`);
        option.value = `b:${name}`;
        return option;
      });
      const relay = relayVoices.map((name) => {
        const option = element("option", "", `Relay: ${name}`);
        option.value = `r:${name}`;
        return option;
      });
      voiceSelect.replaceChildren(automatic, ...device, ...relay);
      voiceSelect.value = stored(`polyglot-voice-${readLang}`) || "";
      voiceSelect.hidden = !reading;
    }
    if (voiceSelect) {
      voiceSelect.addEventListener("change", () => {
        store(`polyglot-voice-${readLang}`, voiceSelect.value);
        speaker.stop();
        describeReading();
      });
    }

    function applySize() {
      document.documentElement.style.setProperty("--listen-size", `${size}px`);
      store("polyglot-listen-size", String(size));
    }
    applySize();
    $("listen-larger").addEventListener("click", () => { size = Math.min(64, size + 4); applySize(); });
    $("listen-smaller").addEventListener("click", () => { size = Math.max(16, size - 4); applySize(); });

    function drawChips() {
      $("listen-chips").replaceChildren(...languages.map((entry) => {
        const chip = element("button", "history-chip listen-chip", entry.name);
        chip.type = "button";
        chip.dataset.group = entry.group;
        chip.setAttribute("aria-pressed", String(langs.includes(entry.code)));
        chip.title = entry.native;
        chip.addEventListener("click", () => {
          const next = langs.includes(entry.code) ? langs.filter((c) => c !== entry.code) : [...langs, entry.code];
          if (!next.length) return;                                     // at least one stays on
          changeLanguages(languages.map((l) => l.code).filter((c) => next.includes(c)));
        });
        return chip;
      }));
      readSelect.replaceChildren(...langs.map((code) => {
        const option = element("option", "", byCode[code].name);
        option.value = code;
        return option;
      }));
      readSelect.value = readLang;
    }

    function changeLanguages(next) {
      langs = next;
      if (!langs.includes(readLang)) { readLang = langs[0]; speaker.stop(); drawVoices(); }
      store("polyglot-listen-langs", langs.join(","));
      chunks = new Map();            // refetched below with the new choice
      first = true;
      generation += 1;
      $("listen-latest").replaceChildren(element("p", "empty", "Waiting for the first caption…"));
      $("listen-earlier").replaceChildren();
      $("listen-earlier-heading").hidden = true;
      drawChips();
      describeReading();
      poll();
    }

    readSelect.addEventListener("change", () => { readLang = readSelect.value; speaker.stop(); drawVoices(); describeReading(); });
    function describeReading() {
      if (!reading) { note.textContent = ""; return; }
      const choice = voiceChoice(readLang);
      let text;
      if (choice && choice.relay) text = `New ${byCode[readLang].name} captions will be read aloud with the relay's ${choice.relay} voice.`;
      else if (speaker.hasBrowserVoice(readLang)) text = `New ${byCode[readLang].name} captions will be read aloud with your device's voice.`;
      else text = `Your device has no ${byCode[readLang].name} voice, so the relay's voice is used.`;
      if (usesRelay(readLang) && quotaLeft !== null) {
        if (quotaLeft <= 0) text += " The relay's speech allowance for today is used up, so captions cannot be read aloud until it resets.";
        else if (quotaLeft <= warnWhenLeft) text += ` Only ${quotaLeft} relay-voice clip${quotaLeft === 1 ? "" : "s"} left today.`;
      }
      note.textContent = text;
    }
    button.addEventListener("click", () => {
      reading = !reading;
      button.setAttribute("aria-pressed", String(reading));
      button.textContent = reading ? "Stop reading" : "Read aloud";
      readSelect.hidden = !reading;
      if (!reading) speaker.stop();
      drawVoices();
      describeReading();
      // Browsers only allow speech after a tap: speak an empty utterance on the same gesture.
      if (reading && window.speechSynthesis && window.SpeechSynthesisUtterance) {
        try { window.speechSynthesis.speak(new window.SpeechSynthesisUtterance("")); } catch (_) { /* optional */ }
      }
    });

    function sentence(chunk, big) {
      const block = element("article", big ? "listen-chunk listen-chunk-latest" : "listen-chunk");
      if (chunk.original) {
        const said = chunk.source && byCode[chunk.source] ? ` · ${byCode[chunk.source].name}` : "";
        block.append(element("p", "listen-original", `“${chunk.original}”${said}`));
      }
      for (const code of langs) {
        const row = element("p", "listen-line");
        row.dataset.group = byCode[code].group;
        row.dataset.lang = code;
        row.append(element("span", "language-code", code.toUpperCase()));
        const text = chunk.translations[code];
        row.append(element("span", text === undefined ? "listen-text listen-waiting" : "listen-text", text === undefined ? "…" : text));
        block.append(row);
      }
      return block;
    }

    function render() {
      const [latest, ...rest] = newestChunks(chunks, EARLIER_SHOWN + 1);
      if (!latest) return;
      $("listen-latest").replaceChildren(sentence(latest, true));
      $("listen-earlier-heading").hidden = rest.length === 0;
      $("listen-earlier").replaceChildren(...rest.map((chunk) => sentence(chunk, false)));
    }

    // New captions, from a poll or pushed by the server: show them, and read the new ones aloud.
    function absorb(rows, speak) {
      const arrivals = mergeChunks(chunks, rows, langs);
      if (arrivals.length) render();
      if (reading && speak) {
        for (const entry of toSpeak(arrivals, readLang)) {
          speaker.speak(entry.text, readLang, voiceChoice(readLang)).catch((error) => {
            if (error.message !== "stopped") note.textContent = error.message;
          });
        }
      }
      return arrivals;
    }

    // Pushed captions (Server-Sent Events) arrive the moment a worker answers; polling then only fills gaps.
    let streamOpen = false;
    function openStream() {
      if (typeof root.EventSource === "undefined") return;
      const stream = new root.EventSource(`/api/events?x=1${roomQuery}`);
      stream.onopen = () => { streamOpen = true; setState(true, "Live"); };
      stream.onerror = () => { streamOpen = false; };
      stream.addEventListener("caption", (event) => {
        try {
          const data = JSON.parse(event.data);
          if (!first) absorb([data.row], true);
        } catch (_) { /* a malformed event is ignored; the next poll catches up */ }
      });
      stream.addEventListener("clear", () => {
        chunks = new Map();
        $("listen-latest").replaceChildren(element("p", "empty", "Waiting for the first caption…"));
        $("listen-earlier").replaceChildren();
        $("listen-earlier-heading").hidden = true;
      });
      stream.addEventListener("reload", () => { poll(); });
      // Words still being spoken (POLYGLOT_INTERIM): shown under the newest sentence until it is finished.
      stream.addEventListener("interim", (event) => {
        try {
          const data = JSON.parse(event.data);
          const box = $("listen-interim");
          if (box && data.text) { box.textContent = `\u2026 ${data.text}`; box.hidden = false; }
        } catch (_) { /* ignore */ }
      });
      stream.addEventListener("chunk", () => { const box = $("listen-interim"); if (box) { box.textContent = ""; box.hidden = true; } });
    }

    async function poll() {
      const mine = generation;
      try {
        const query = `/api/history?langs=${encodeURIComponent(langs.join(","))}&last_minutes=${WINDOW_MINUTES}&limit=80${roomQuery}`;
        const reply = await fetch(query);
        if (reply.status === 401) { setState(false, "Needs the link with its token"); return; }
        const data = await reply.json();
        if (mine !== generation) return;           // the choice changed while this was in flight
        absorb(data.chunks, !first);
        first = false;
        setState(true, "Live");
      } catch (_) {
        setState(false, "Reconnecting");
      }
    }

    drawChips();
    openStream();
    (async function loop() { await poll(); setTimeout(loop, streamOpen ? STREAM_POLL_MS : POLL_MS); })();
  }

  start().catch(() => setState(false, "Could not load"));
})(typeof window !== "undefined" ? window : globalThis);
