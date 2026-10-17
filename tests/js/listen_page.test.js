// Runs the real ui/listen.js page code against a fake DOM, fake network and a controllable clock.
// Run with: node --test tests/js/listen_page.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const UI = path.join(__dirname, "..", "..", "ui");

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.attributes = {};
    this._text = "";
    this.className = "";
    this.value = "";
    this.hidden = false;
    this.title = "";
    this.type = "";
    const classes = new Set();
    this.classList = { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), contains: (name) => classes.has(name) };
  }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text; }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(value) { throw new Error("listen.js must not write innerHTML (XSS risk): " + value); }
  get lastElementChild() { return this.children[this.children.length - 1]; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ""; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  async click() { for (const handler of this.listeners.click || []) await handler({}); }
  async change(value) { this.value = value; for (const handler of this.listeners.change || []) await handler({}); }
  find(predicate, found = []) {
    if (predicate(this)) found.push(this);
    this.children.forEach((child) => child.find(predicate, found));
    return found;
  }
  byClass(name) { return this.find((e) => e.className.split(" ").includes(name)); }
}

const LANGUAGES = [
  ["en", "English", "ASEAN"], ["my", "Burmese", "ASEAN"], ["id", "Indonesian", "ASEAN"], ["ms", "Malay", "ASEAN"],
  ["th", "Thai", "ASEAN"], ["vi", "Vietnamese", "ASEAN"], ["tl", "Filipino", "ASEAN"], ["km", "Khmer", "ASEAN"], ["lo", "Lao", "ASEAN"],
].map(([code, name, group]) => ({ code, name, native: name.toLowerCase(), group }));

const IDS = ["listen-view", "listen-fallback", "listen-note", "listen-speak", "listen-read-lang", "listen-voice", "listen-interim", "listen-chips",
  "listen-latest", "listen-earlier", "listen-earlier-heading", "listen-smaller", "listen-larger"];

const row = (id, number, translations = {}, extra = {}) => ({
  chunk_id: id, number, text: `said ${number}`, source_lang: "en", created_at: 100 + number,
  translations: Object.fromEntries(Object.entries(translations).map(([lang, text]) => [lang, { text, timestamp: 100 + number }])),
  ...extra,
});

const flush = async () => { for (let i = 0; i < 30; i++) await new Promise((resolve) => setImmediate(resolve)); };

// `history` is a function (url) => {status?, body?} | throws; it decides what each poll receives.
async function openPage({ search = "", pathname = "/listen", history = () => ({ body: { chunks: [] } }), stored = {}, hasVoice = true, streaming = false,
                         ttsInfo = { voices: ["Kore", "Puck"], quota: { left: null }, warn_when_left: 3 }, deviceVoices = ["Device A"] } = {}) {
  const els = Object.fromEntries(IDS.map((id) => [id, new FakeElement(id === "listen-read-lang" || id === "listen-voice" ? "select" : "div")]));
  els["listen-fallback"].hidden = false;
  els["listen-view"].hidden = true;
  els["listen-earlier-heading"].hidden = true;
  els["listen-read-lang"].hidden = true;
  const stateElement = new FakeElement("span");
  stateElement.append(new FakeElement("i"), new FakeElement("span"));
  els["listen-state"] = stateElement;

  const fetchLog = [];
  const timers = [];
  const delays = [];
  const streams = [];
  // A stand-in for the browser's EventSource: tests open it, then push events through `emit`.
  class FakeEventSource {
    constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
    addEventListener(kind, handler) { (this.listeners[kind] ||= []).push(handler); }
    emit(kind, data) { for (const handler of this.listeners[kind] || []) handler({ data: JSON.stringify(data) }); }
    emitRaw(kind, raw) { for (const handler of this.listeners[kind] || []) handler({ data: raw }); }
  }
  const spoken = [];
  const speakerLog = { stops: 0, speakFails: null, choices: [], onQuota: null };
  const storage = new Map(Object.entries(stored));
  const cssVars = {};
  const sandbox = {
    document: {
      getElementById: (id) => els[id],
      createElement: (tag) => new FakeElement(tag),
      title: "",
      documentElement: { lang: "", style: { setProperty: (k, v) => { cssVars[k] = v; } } },
    },
    window: null,
    location: { search, pathname },
    fetch: async (url, options) => {
      fetchLog.push(url);
      if (url === "/api/languages") return { status: 200, ok: true, json: async () => ({ languages: LANGUAGES }) };
      if (url === "/api/tts/info") return { status: 200, ok: true, json: async () => ttsInfo };
      const answer = await history(url);
      return { status: answer.status || 200, ok: (answer.status || 200) < 400, json: async () => answer.body };
    },
    setTimeout: (callback, delay) => { timers.push(callback); delays.push(delay); return timers.length; },
    localStorage: { getItem: (k) => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)) },
    sessionStorage: { getItem: () => null, setItem() {} },
    URLSearchParams, Object, Array, Map, Promise, Number, String, JSON, Math, Error, encodeURIComponent,
    PolyglotSpeech: {
      createSpeaker: (env) => ({
        _env: (speakerLog.onQuota = env.onQuota),
        speak: (text, lang, choice) => { spoken.push([text, lang]); speakerLog.choices.push(choice); return speakerLog.speakFails ? Promise.reject(new Error(speakerLog.speakFails)) : Promise.resolve("browser"); },
        browserVoiceNames: () => deviceVoices,
        stop: () => { speakerLog.stops += 1; },
        hasBrowserVoice: () => hasVoice,
      }),
    },
  };
  if (streaming) sandbox.EventSource = FakeEventSource;
  sandbox.window = sandbox;
  sandbox.window.location = sandbox.location;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(UI, "listen.js"), "utf8"), sandbox, { filename: "listen.js" });
  await flush();
  return {
    els, fetchLog, spoken, speakerLog, storage, cssVars, sandbox, streams, delays,
    historyUrls: () => fetchLog.filter((u) => u.startsWith("/api/history")),
    // Runs the next scheduled poll and lets it finish.
    async poll() { const next = timers.shift(); assert.ok(next, "no poll is scheduled"); await next(); await flush(); },
    status: () => els["listen-state"].lastElementChild.textContent,
    lines: (container) => container.find((e) => e.dataset.lang).map((e) => [e.dataset.lang, e.byClass("listen-text")[0].textContent]),
    chip: (name) => els["listen-chips"].children.find((c) => c.textContent === name),
  };
}

test("the page replaces the fallback message, shows the view and asks for every language by default", async () => {
  const page = await openPage();
  assert.equal(page.els["listen-fallback"].hidden, true);
  assert.equal(page.els["listen-view"].hidden, false);
  assert.match(page.historyUrls()[0], /langs=en%2Cmy%2Cid%2Cms%2Cth%2Cvi%2Ctl%2Ckm%2Clo/);
  assert.equal(page.els["listen-chips"].children.length, 9);
  assert.ok(page.els["listen-chips"].children.every((chip) => chip.attributes["aria-pressed"] === "true"));
  assert.equal(page.status(), "Live");
});

test("a sentence shows what was said and one line per language, all together", async () => {
  const page = await openPage({ history: () => ({ body: { chunks: [row("a", 1, { en: "Hello", th: "สวัสดี", my: "မင်္ဂလာပါ" })] } }) });
  const latest = page.els["listen-latest"];
  assert.match(latest.textContent, /“said 1” · English/);
  const lines = Object.fromEntries(page.lines(latest));
  assert.equal(lines.th, "สวัสดี");
  assert.equal(lines.my, "မင်္ဂလာပါ");
  assert.equal(lines.en, "Hello");
  assert.equal(lines.km, "…");                  // not translated yet
  assert.equal(page.lines(latest).length, 9);
  assert.equal(page.els["listen-earlier-heading"].hidden, true);
});

test("languages that arrive on a later poll fill in the same sentence", async () => {
  const replies = [
    { chunks: [row("a", 1, { th: "one" })] },
    { chunks: [row("a", 1, { th: "one", km: "bir" })] },
  ];
  const page = await openPage({ history: () => ({ body: replies.shift() || { chunks: [] } }) });
  assert.equal(Object.fromEntries(page.lines(page.els["listen-latest"])).km, "…");
  await page.poll();
  assert.equal(Object.fromEntries(page.lines(page.els["listen-latest"])).km, "bir");
  assert.equal(page.els["listen-earlier"].children.length, 0);
});

test("a newer sentence becomes the big one and the previous moves under Earlier", async () => {
  const replies = [{ chunks: [row("a", 1, { th: "one" })] }, { chunks: [row("a", 1, { th: "one" }), row("b", 2, { th: "two" })] }];
  const page = await openPage({ search: "?langs=th", history: () => ({ body: replies.shift() || { chunks: [] } }) });
  await page.poll();
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "two"]]);
  assert.deepEqual(page.lines(page.els["listen-earlier"]), [["th", "one"]]);
  assert.equal(page.els["listen-earlier-heading"].hidden, false);
});

test("only the most recent sentences are kept on screen", async () => {
  const rows = Array.from({ length: 30 }, (_, i) => row(`c${i}`, i + 1, { th: `t${i + 1}` }));
  const page = await openPage({ search: "?langs=th", history: () => ({ body: { chunks: rows } }) });
  assert.equal(page.els["listen-earlier"].children.length, 10);
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "t30"]]);
});

test("caption text is inserted as text, never as markup", async () => {
  const page = await openPage({ search: "?langs=th", history: () => ({ body: { chunks: [row("a", 1, { th: "<img src=x onerror=alert(1)>" }, { text: "<b>x</b>" })] } }) });
  assert.match(page.els["listen-latest"].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(page.els["listen-latest"].textContent, /<b>x<\/b>/);
});

test("the address picks the languages: ?langs= and /listen/<code>", async () => {
  const fromQuery = await openPage({ search: "?langs=km,th" });
  assert.match(fromQuery.historyUrls()[0], /langs=th%2Ckm/);
  const fromPath = await openPage({ pathname: "/listen/lo" });
  assert.match(fromPath.historyUrls()[0], /langs=lo&/);
  assert.equal(fromPath.chip("Lao").attributes["aria-pressed"], "true");
  assert.equal(fromPath.chip("Thai").attributes["aria-pressed"], "false");
});

test("the last choice is remembered when the address does not say", async () => {
  const page = await openPage({ stored: { "polyglot-listen-langs": "my,th" } });
  assert.match(page.historyUrls()[0], /langs=my%2Cth/);
});

test("switching a language off or on refetches with the new choice and remembers it", async () => {
  const page = await openPage({ search: "?langs=th,my" });
  await page.chip("Burmese").click();
  await flush();
  assert.match(page.historyUrls().at(-1), /langs=th&/);
  assert.equal(page.storage.get("polyglot-listen-langs"), "th");
  assert.equal(page.chip("Burmese").attributes["aria-pressed"], "false");
  await page.chip("Khmer").click();
  await flush();
  assert.match(page.historyUrls().at(-1), /langs=th%2Ckm/);        // in the page's own order
  assert.equal(page.storage.get("polyglot-listen-langs"), "th,km");
});

test("the last language cannot be switched off", async () => {
  const page = await openPage({ search: "?langs=th" });
  const before = page.historyUrls().length;
  await page.chip("Thai").click();
  await flush();
  assert.equal(page.historyUrls().length, before);
  assert.equal(page.chip("Thai").attributes["aria-pressed"], "true");
});

test("changing the languages clears the screen and fills it again from the server", async () => {
  const page = await openPage({ search: "?langs=th", history: (url) => ({ body: { chunks: [row("a", 1, { th: "one", km: "bir" })] } }) });
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "one"]]);
  await page.chip("Khmer").click();
  await flush();
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "one"], ["km", "bir"]]);
});

test("sentences from before a change are dropped when the server no longer has them", async () => {
  const replies = [{ chunks: [row("a", 1, { th: "one" })] }, { chunks: [row("b", 2, { th: "two", km: "pii" })] }];
  const page = await openPage({ search: "?langs=th", history: () => ({ body: replies.shift() || { chunks: [] } }) });
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "one"]]);
  await page.chip("Khmer").click();
  await flush();
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "two"], ["km", "pii"]]);
  assert.equal(page.els["listen-earlier"].children.length, 0);            // "one" is gone, not pushed down
});

test("an answer that arrives after the choice changed is ignored", async () => {
  let release;
  const late = new Promise((resolve) => { release = resolve; });
  let calls = 0;
  const page = await openPage({ search: "?langs=th", history: () => {
    calls += 1;
    if (calls === 2) return late.then(() => ({ body: { chunks: [row("stale", 99, { th: "stale answer" })] } }));
    return { body: { chunks: [row("a", 1, { th: "one", km: "bir" })] } };
  } });
  const slowPoll = page.poll();                               // call 2: still waiting for the server
  await page.chip("Khmer").click();                           // the choice changes, so call 3 is made
  await flush();
  release();                                                  // now the old answer comes in
  await slowPoll;
  await flush();
  assert.doesNotMatch(page.els["listen-latest"].textContent, /stale answer/);
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "one"], ["km", "bir"]]);
});

test("a refused viewer is told to use the link with its token", async () => {
  const page = await openPage({ history: () => ({ status: 401, body: { error: "A viewing token is required" } }) });
  assert.equal(page.status(), "Needs the link with its token");
});

test("a network failure shows Reconnecting and the page recovers by itself", async () => {
  let up = false;
  const page = await openPage({ search: "?langs=th", history: () => { if (!up) throw new Error("offline"); return { body: { chunks: [row("a", 1, { th: "back" })] } }; } });
  assert.equal(page.status(), "Reconnecting");
  up = true;
  await page.poll();
  assert.equal(page.status(), "Live");
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "back"]]);
});

test("a language list that cannot be loaded says so instead of staying blank", async () => {
  const els = { "listen-view": new FakeElement(), "listen-fallback": new FakeElement() };
  const stateElement = new FakeElement("span");
  stateElement.append(new FakeElement("i"), new FakeElement("span"));
  els["listen-state"] = stateElement;
  const sandbox = { document: { getElementById: (id) => els[id] }, window: null, location: { search: "", pathname: "/listen" },
    fetch: async () => { throw new Error("down"); }, setTimeout() {}, URLSearchParams, Object, Array, Map, Promise, Number, String, JSON, Math, Error,
    localStorage: { getItem: () => null, setItem() {} }, sessionStorage: { getItem: () => null, setItem() {} }, PolyglotSpeech: {} };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(UI, "listen.js"), "utf8"), sandbox);
  await flush();
  assert.equal(stateElement.lastElementChild.textContent, "Could not load");
});

test("text size buttons resize within limits and remember the size", async () => {
  const page = await openPage();
  assert.equal(page.cssVars["--listen-size"], "22px");
  await page.els["listen-larger"].click();
  assert.equal(page.cssVars["--listen-size"], "26px");
  assert.equal(page.storage.get("polyglot-listen-size"), "26");
  for (let i = 0; i < 30; i++) await page.els["listen-larger"].click();
  assert.equal(page.cssVars["--listen-size"], "64px");
  for (let i = 0; i < 30; i++) await page.els["listen-smaller"].click();
  assert.equal(page.cssVars["--listen-size"], "16px");
  assert.equal((await openPage({ stored: { "polyglot-listen-size": "40" } })).cssVars["--listen-size"], "40px");
});

test("read aloud: nothing already on screen is spoken, new captions in the chosen language are", async () => {
  const replies = [
    { chunks: [row("a", 1, { th: "old", km: "kao" })] },
    { chunks: [row("a", 1, { th: "old", km: "kao" }), row("b", 2, { th: "new", km: "thmei" })] },
  ];
  const page = await openPage({ search: "?langs=th,km", history: () => ({ body: replies.shift() || { chunks: [] } }) });
  await page.els["listen-speak"].click();
  assert.equal(page.els["listen-speak"].textContent, "Stop reading");
  assert.equal(page.els["listen-speak"].attributes["aria-pressed"], "true");
  assert.equal(page.els["listen-read-lang"].hidden, false);
  assert.match(page.els["listen-note"].textContent, /Thai captions will be read aloud with your device's voice/);
  assert.deepEqual(page.spoken, []);
  await page.poll();
  assert.deepEqual(page.spoken, [["new", "th"]]);            // not "old", and not the Khmer line
});

test("reading from the first poll onward skips what was already there when it was switched on", async () => {
  const page = await openPage({ search: "?langs=th", history: () => ({ body: { chunks: [row("a", 1, { th: "old" })] } }) });
  await page.els["listen-speak"].click();
  await page.poll();
  assert.deepEqual(page.spoken, []);
});

test("changing the languages while reading does not read out everything again", async () => {
  const page = await openPage({ search: "?langs=th", history: () => ({ body: { chunks: [row("a", 1, { th: "one", km: "bir" }), row("b", 2, { th: "two", km: "pii" })] } }) });
  await page.els["listen-speak"].click();
  await page.chip("Khmer").click();                           // refetches the same sentences under a new choice
  await flush();
  assert.deepEqual(page.spoken, []);
  await page.poll();
  assert.deepEqual(page.spoken, []);                          // still nothing new
});

test("choosing another language to read stops the current one and follows the new one", async () => {
  const replies = [{ chunks: [] }, { chunks: [row("a", 1, { th: "t", km: "m" })] }];
  const page = await openPage({ search: "?langs=th,km", history: () => ({ body: replies.shift() || { chunks: [] } }) });
  await page.els["listen-speak"].click();
  const stopsBefore = page.speakerLog.stops;
  await page.els["listen-read-lang"].change("km");
  assert.equal(page.speakerLog.stops, stopsBefore + 1);
  assert.match(page.els["listen-note"].textContent, /Khmer/);
  await page.poll();
  assert.deepEqual(page.spoken, [["m", "km"]]);
});

test("the note says the relay's voice is used when the device has none", async () => {
  const page = await openPage({ search: "?langs=km", hasVoice: false });
  await page.els["listen-speak"].click();
  assert.match(page.els["listen-note"].textContent, /no Khmer voice, so the relay's voice is used/);
});

test("stopping silences the speaker, hides the language choice and clears the note", async () => {
  const page = await openPage({ search: "?langs=th" });
  await page.els["listen-speak"].click();
  const stops = page.speakerLog.stops;
  await page.els["listen-speak"].click();
  assert.equal(page.speakerLog.stops, stops + 1);
  assert.equal(page.els["listen-speak"].textContent, "Read aloud");
  assert.equal(page.els["listen-read-lang"].hidden, true);
  assert.equal(page.els["listen-note"].textContent, "");
});

test("switching off the language being read moves reading to one that is still shown", async () => {
  const page = await openPage({ search: "?langs=km,lo" });
  await page.els["listen-speak"].click();                    // reading Khmer, the first shown
  assert.match(page.els["listen-note"].textContent, /Khmer/);
  await page.chip("Khmer").click();
  await flush();
  assert.equal(page.els["listen-read-lang"].value, "lo");
  assert.match(page.els["listen-note"].textContent, /Lao/);
});

test("a speech failure is shown to the listener, but a deliberate stop is not", async () => {
  const replies = [{ chunks: [] }, { chunks: [row("a", 1, { th: "x" })] }, { chunks: [row("a", 1, { th: "x" }), row("b", 2, { th: "y" })] }];
  const page = await openPage({ search: "?langs=th", history: () => ({ body: replies.shift() || { chunks: [] } }) });
  await page.els["listen-speak"].click();
  page.speakerLog.speakFails = "The daily speech quota for this API key is used up";
  await page.poll();
  assert.match(page.els["listen-note"].textContent, /daily speech quota/);
  page.speakerLog.speakFails = "stopped";
  await page.els["listen-speak"].click();                    // turn off: note is cleared
  page.els["listen-note"].textContent = "";
  await page.els["listen-speak"].click();
  await page.poll();
  assert.equal(page.els["listen-note"].textContent.includes("stopped"), false);
});

// ---- rooms and pushed captions --------------------------------------------------------------

test("a room in the address is followed: its history, its stream and its name on the page", async () => {
  const page = await openPage({ search: "?room=hall-a&langs=th", streaming: true });
  assert.match(page.historyUrls()[0], /&room=hall-a$/);
  assert.equal(page.streams[0].url, "/api/events?x=1&room=hall-a");
  assert.match(page.sandbox.document.title, /hall-a/);
});

test("the default room adds nothing to the requests", async () => {
  const page = await openPage({ search: "?langs=th", streaming: true });
  assert.doesNotMatch(page.historyUrls()[0], /room=/);
  assert.equal(page.streams[0].url, "/api/events?x=1");
});

test("a pushed caption appears at once, without waiting for a poll", async () => {
  const page = await openPage({ search: "?langs=th,km", streaming: true });
  page.streams[0].emit("caption", { lang: "th", row: row("a", 1, { th: "pushed" }) });
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "pushed"], ["km", "…"]]);
  page.streams[0].emit("caption", { lang: "km", row: row("a", 1, { th: "pushed", km: "kh" }) });
  assert.deepEqual(page.lines(page.els["listen-latest"]), [["th", "pushed"], ["km", "kh"]]);
});

test("a pushed caption is read aloud when reading is on", async () => {
  const page = await openPage({ search: "?langs=th", streaming: true });
  await page.els["listen-speak"].click();
  page.streams[0].emit("caption", { lang: "th", row: row("a", 1, { th: "say this" }) });
  await flush();
  assert.deepEqual(page.spoken, [["say this", "th"]]);
});

test("a malformed pushed event is ignored", async () => {
  const page = await openPage({ search: "?langs=th", streaming: true });
  page.streams[0].emitRaw("caption", "{not json");
  page.streams[0].emit("caption", { lang: "th" });
  assert.equal(page.lines(page.els["listen-latest"]).length, 0);
});

test("clearing the room empties the page", async () => {
  const page = await openPage({ search: "?langs=th", streaming: true, history: () => ({ body: { chunks: [row("a", 1, { th: "x" })] } }) });
  page.streams[0].emit("clear", {});
  assert.match(page.els["listen-latest"].textContent, /Waiting for the first caption/);
  assert.equal(page.els["listen-earlier"].children.length, 0);
});

test("a reload event fetches everything again", async () => {
  const page = await openPage({ search: "?langs=th", streaming: true });
  const before = page.historyUrls().length;
  page.streams[0].emit("reload", {});
  await flush();
  assert.equal(page.historyUrls().length, before + 1);
});

test("with an open stream polling slows to a safety net, and speeds up again when it drops", async () => {
  const page = await openPage({ search: "?langs=th", streaming: true });
  assert.equal(page.delays.at(-1), 1000);                      // not open yet
  page.streams[0].onopen();
  await page.poll();
  assert.equal(page.delays.at(-1), 10000);
  page.streams[0].onerror();
  await page.poll();
  assert.equal(page.delays.at(-1), 1000);
});

test("without EventSource the page simply polls every second", async () => {
  const page = await openPage({ search: "?langs=th" });
  assert.equal(page.streams.length, 0);
  assert.equal(page.delays.at(-1), 1000);
});

// ---- voices and the speech allowance ---------------------------------------------------------

test("the voice list offers automatic, the device's voices and the relay's voices", async () => {
  const page = await openPage({ search: "?langs=th" });
  await page.els["listen-speak"].click();
  const options = page.els["listen-voice"].children.map((o) => [o.value, o.textContent]);
  assert.deepEqual(options, [["", "Voice: automatic"], ["b:Device A", "Device: Device A"], ["r:Kore", "Relay: Kore"], ["r:Puck", "Relay: Puck"]]);
  assert.equal(page.els["listen-voice"].hidden, false);
  await page.els["listen-speak"].click();
  assert.equal(page.els["listen-voice"].hidden, true);
});

test("a chosen voice is remembered per language and used for reading", async () => {
  const replies = [{ chunks: [] }, { chunks: [row("a", 1, { th: "x" })] }];
  const page = await openPage({ search: "?langs=th", history: () => ({ body: replies.shift() || { chunks: [] } }) });
  await page.els["listen-speak"].click();
  await page.els["listen-voice"].change("r:Puck");
  assert.equal(page.storage.get("polyglot-voice-th"), "r:Puck");
  assert.match(page.els["listen-note"].textContent, /relay's Puck voice/);
  await page.poll();
  assert.equal(JSON.stringify(page.speakerLog.choices.at(-1)), JSON.stringify({ relay: "Puck" }));
});

test("a remembered device voice is passed as a browser choice", async () => {
  const replies = [{ chunks: [] }, { chunks: [row("a", 1, { th: "x" })] }];
  const page = await openPage({ search: "?langs=th", stored: { "polyglot-voice-th": "b:Device A" }, history: () => ({ body: replies.shift() || { chunks: [] } }) });
  await page.els["listen-speak"].click();
  await page.poll();
  assert.equal(JSON.stringify(page.speakerLog.choices.at(-1)), JSON.stringify({ browser: "Device A" }));
});

test("the listener is warned when only a few relay-voice clips are left today", async () => {
  const page = await openPage({ search: "?langs=km", hasVoice: false, ttsInfo: { voices: [], quota: { left: 2 }, warn_when_left: 3 } });
  await page.els["listen-speak"].click();
  assert.match(page.els["listen-note"].textContent, /Only 2 relay-voice clips left today/);
});

test("the warning updates as clips are used, and says when none are left", async () => {
  const page = await openPage({ search: "?langs=km", hasVoice: false, ttsInfo: { voices: [], quota: { left: 5 }, warn_when_left: 3 } });
  await page.els["listen-speak"].click();
  assert.doesNotMatch(page.els["listen-note"].textContent, /left today/);
  page.speakerLog.onQuota(1);
  assert.match(page.els["listen-note"].textContent, /Only 1 relay-voice clip left today/);
  page.speakerLog.onQuota(0);
  assert.match(page.els["listen-note"].textContent, /allowance for today is used up/);
});

test("no quota warning when the device's own voice is doing the reading", async () => {
  const page = await openPage({ search: "?langs=th", hasVoice: true, ttsInfo: { voices: [], quota: { left: 0 }, warn_when_left: 3 } });
  await page.els["listen-speak"].click();
  assert.doesNotMatch(page.els["listen-note"].textContent, /allowance|left today/);
});

test("a page whose speech details cannot be loaded still reads aloud", async () => {
  const page = await openPage({ search: "?langs=th", ttsInfo: null });
  await page.els["listen-speak"].click();
  assert.match(page.els["listen-note"].textContent, /read aloud/);
});

test("words still being spoken appear under the newest sentence until it is finished", async () => {
  const page = await openPage({ search: "?langs=th", streaming: true });
  page.streams[0].emit("interim", { text: "sawasdee khrap", lang: "th" });
  assert.equal(page.els["listen-interim"].textContent, "\u2026 sawasdee khrap");
  assert.equal(page.els["listen-interim"].hidden, false);
  page.streams[0].emit("chunk", {});
  assert.equal(page.els["listen-interim"].hidden, true);
});
