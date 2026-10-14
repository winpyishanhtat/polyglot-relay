// A phone-friendly page for one language: /listen/<lang> shows that language's captions large, newest first,
// and can read each new one aloud. /listen shows a language picker.
(function (root) {
  const POLL_MS = 1000;
  const WINDOW_MINUTES = 30;
  const EARLIER_SHOWN = 12;
  const MAX_QUEUED_SPEECH = 2;     // a caption read late is worse than one skipped

  // ---- pure helpers (tested in tests/js/listen.test.js) -------------------------------------------

  function languageFromPath(pathname) {
    const match = /^\/listen\/([a-z]{2,3})\/?$/.exec(pathname || "");
    return match ? match[1] : null;
  }

  // Folds a /api/history reply into `seen` (chunk_id -> {number, text, original, source}) and returns the rows
  // that are new, in speaking order. Only rows that already have this language count: a chunk translated
  // late shows up on a later poll and is still reported then, whatever its number.
  function mergeRows(seen, rows, lang) {
    const fresh = [];
    for (const row of rows || []) {
      const translation = row.translations && row.translations[lang];
      if (!translation || !translation.text || seen.has(row.chunk_id)) continue;
      const entry = { id: row.chunk_id, number: row.number || 0, text: translation.text, original: row.text || "",
        source: row.source_lang || "", at: translation.timestamp || row.created_at || 0 };
      seen.set(row.chunk_id, entry);
      fresh.push(entry);
    }
    return fresh.sort(order);
  }

  function order(a, b) { return (a.number - b.number) || (a.at - b.at); }

  function newestFirst(seen, count) {
    return [...seen.values()].sort(order).reverse().slice(0, count);
  }

  // What to read aloud from a batch of new captions: the newest few, still in order.
  function toSpeak(fresh) { return fresh.slice(-MAX_QUEUED_SPEECH); }

  const api = { languageFromPath, mergeRows, newestFirst, toSpeak, order };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PolyglotListen = api;

  if (typeof document === "undefined" || !document.getElementById || !document.getElementById("listen-view")) return;

  // ---- the page ----------------------------------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  let accessToken = "";
  try {
    accessToken = new URLSearchParams(window.location.search).get("token") || sessionStorage.getItem("polyglot-token") || "";
    if (accessToken) sessionStorage.setItem("polyglot-token", accessToken);
  } catch (_) { /* no storage here: stay open */ }
  const tokenSuffix = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";

  const lang = languageFromPath(window.location.pathname);
  const state = $("listen-state");
  const setState = (online, text) => {
    state.classList.toggle("online", online);
    state.lastElementChild.textContent = text;
  };

  async function showPicker() {
    $("listen-picker").hidden = false;
    const reply = await fetch("/api/languages");
    const { languages } = await reply.json();
    $("listen-choices").replaceChildren(...languages.map((entry) => {
      const link = document.createElement("a");
      link.className = "listen-choice";
      link.href = `/listen/${entry.code}${tokenSuffix}`;
      link.dataset.group = entry.group;
      const name = document.createElement("strong");
      name.textContent = entry.name;
      const native = document.createElement("span");
      native.textContent = entry.native;
      link.append(name, native);
      return link;
    }));
    setState(true, "Ready");
  }

  async function showLanguage() {
    const info = await (await fetch("/api/languages")).json();
    const meta = info.languages.find((entry) => entry.code === lang);
    if (!meta) { showPicker(); return; }
    $("listen-view").hidden = false;
    $("listen-title").textContent = meta.name;
    $("listen-native").textContent = meta.native;
    $("listen-home").href = `/listen${tokenSuffix}`;
    document.title = `${meta.name} captions — Polyglot Relay`;
    document.documentElement.lang = lang;

    const seen = new Map();
    let first = true;
    let reading = false;
    let size = Number(localStorageGet("polyglot-listen-size")) || 34;
    applySize();

    const speaker = root.PolyglotSpeech.createSpeaker({
      speechSynthesis: window.speechSynthesis, SpeechSynthesisUtterance: window.SpeechSynthesisUtterance,
      fetch: window.fetch.bind(window), Audio: window.Audio, URL: window.URL,
      headers: () => ({ "Content-Type": "application/json", ...(accessToken ? { "X-Access-Token": accessToken } : {}) }),
    });
    const note = $("listen-note");
    const button = $("listen-speak");

    function localStorageGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
    function localStorageSet(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* optional */ } }
    function applySize() {
      document.documentElement.style.setProperty("--listen-size", `${size}px`);
      localStorageSet("polyglot-listen-size", String(size));
    }
    $("listen-larger").addEventListener("click", () => { size = Math.min(80, size + 4); applySize(); });
    $("listen-smaller").addEventListener("click", () => { size = Math.max(18, size - 4); applySize(); });

    button.addEventListener("click", () => {
      reading = !reading;
      button.setAttribute("aria-pressed", String(reading));
      button.textContent = reading ? "Stop reading" : "Read aloud";
      if (!reading) { speaker.stop(); note.textContent = ""; return; }
      note.textContent = speaker.hasBrowserVoice(lang)
        ? "New captions will be read aloud with your device's voice."
        : "Your device has no voice for this language, so the relay's voice is used.";
      // Browsers only allow speech after a tap: speak a short silent-ish unlock on the same gesture.
      if (window.speechSynthesis && window.SpeechSynthesisUtterance) {
        try { window.speechSynthesis.speak(new window.SpeechSynthesisUtterance("")); } catch (_) { /* optional */ }
      }
    });

    function render() {
      const [latest, ...rest] = newestFirst(seen, EARLIER_SHOWN + 1);
      const latestBox = $("listen-latest");
      if (!latest) return;
      const big = document.createElement("p");
      big.className = "listen-big";
      big.textContent = latest.text;
      const parts = [big];
      if (latest.original && latest.original !== latest.text) {
        const original = document.createElement("p");
        original.className = "listen-original";
        original.textContent = latest.original;
        parts.push(original);
      }
      latestBox.replaceChildren(...parts);
      $("listen-earlier-heading").hidden = rest.length === 0;
      $("listen-earlier").replaceChildren(...rest.map((entry) => {
        const line = document.createElement("p");
        line.textContent = entry.text;
        return line;
      }));
    }

    async function poll() {
      try {
        const query = `/api/history?langs=${encodeURIComponent(lang)}&last_minutes=${WINDOW_MINUTES}&limit=80`;
        const reply = await fetch(query);
        if (reply.status === 401) { setState(false, "Needs the link with its token"); return; }
        const data = await reply.json();
        const fresh = mergeRows(seen, data.chunks, lang);
        if (fresh.length) render();
        if (reading && !first) {
          for (const entry of toSpeak(fresh)) {
            speaker.speak(entry.text, lang).catch((error) => {
              if (error.message !== "stopped") note.textContent = error.message;
            });
          }
        }
        first = false;
        setState(true, "Live");
      } catch (_) {
        setState(false, "Reconnecting");
      }
    }
    (async function loop() { await poll(); setTimeout(loop, POLL_MS); })();
  }

  (lang ? showLanguage() : showPicker()).catch(() => setState(false, "Could not load"));
})(typeof window !== "undefined" ? window : globalThis);
