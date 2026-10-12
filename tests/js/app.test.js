// Runs the real ui/app.js against a minimal fake DOM and checks what it renders.
// Run with: node --test tests/js/app.test.js
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
    this.style = {};
    this.listeners = {};
    this.attributes = {};
    this._text = "";
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.title = "";
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : Boolean(force);
        on ? classes.add(name) : classes.delete(name);
        return on;
      },
      contains: (name) => classes.has(name),
    };
  }
  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(value) { throw new Error("app.js must not write innerHTML (XSS risk): " + value); }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ""; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  removeEventListener() {}
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector() { return new FakeElement(); }
  querySelectorAll() { return []; }
  remove() {}
  focus() {}
  async click() { for (const h of this.listeners.click || []) await h({ preventDefault() {} }); }
  allText() { return [this.textContent, ...this.children.map((c) => c.allText())].join(" "); }
}

const LANGUAGES = ["en", "my", "id", "ms", "th", "vi", "tl", "km", "lo"].map((code) => ({
  code, name: code.toUpperCase() + "-name", native: code, group: "ASEAN",
}));

function load({ fetchImpl, search, stored } = {}) {
  const registry = new Map();
  const get = (selector) => {
    if (!registry.has(selector)) registry.set(selector, new FakeElement());
    return registry.get(selector);
  };
  const calls = [];
  const sandbox = {
    document: {
      querySelector: get,
      querySelectorAll: () => [],
      createElement: (tag) => new FakeElement(tag),
      addEventListener() {},
      body: new FakeElement("body"),
    },
    fetch: fetchImpl || (async (url, options) => {
      calls.push([url, options]);
      if (url === "/api/languages") return { ok: true, json: async () => ({ languages: LANGUAGES }) };
      return { ok: true, json: async () => ({}) };
    }),
    setInterval: () => 0,
    setTimeout: () => 0,
    clearTimeout() {},
    navigator: {},
    sessionStorage: (() => {
      const data = new Map(stored ? Object.entries(stored) : []);
      return { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)), data };
    })(),
    performance: { now: () => 0 },
    Intl, Date, JSON, Math, Object, Array, String, Number, Boolean, Promise, Set, Map, Error, console, URLSearchParams,
    Blob: class {},
  };
  sandbox.window = sandbox;
  if (search !== undefined) sandbox.location = { search };
  vm.createContext(sandbox);
  // live_slicer.js is a classic script that defines LiveSlicer globally for app.js.
  vm.runInContext(fs.readFileSync(path.join(UI, "live_slicer.js"), "utf8"), sandbox, { filename: "live_slicer.js" });
  vm.runInContext(fs.readFileSync(path.join(UI, "app.js"), "utf8"), sandbox, { filename: "app.js" });
  const run = (code) => vm.runInContext(code, sandbox);
  return { sandbox, registry, get, calls, run };
}

async function booted() {
  const page = load();
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));  // let boot() finish
  return page;
}

function caption(overrides = {}) {
  return { chunk_id: "c", text: "hello", original_text: null, original_source_lang: null,
           translation_backend: "gemini-text", worker_id: "th-1", number: 1, timestamp: 1700000000, ...overrides };
}

test("formatUptime shows minutes and seconds, then hours and minutes", async () => {
  const { sandbox } = await booted();
  assert.equal(sandbox.formatUptime(5), "0m 5s");
  assert.equal(sandbox.formatUptime(65), "1m 5s");
  assert.equal(sandbox.formatUptime(3600), "1h 0m");
  assert.equal(sandbox.formatUptime(7385), "2h 3m");
});

test("channel numbers are zero padded and start at 01", async () => {
  const { sandbox } = await booted();
  assert.equal(sandbox.channelNo(0), "01");
  assert.equal(sandbox.channelNo(8), "09");
  assert.equal(sandbox.channelNo(9), "10");
});

test("formatTime renders a clock time for a unix timestamp", async () => {
  const { sandbox } = await booted();
  assert.match(sandbox.formatTime(1700000000), /\d{1,2}:\d{2}:\d{2}/);
});

test("boot loads the nine languages from the server", async () => {
  const { run, calls } = await booted();
  assert.equal(calls[0][0], "/api/languages");
  assert.equal(run("LANGS.length"), 9);
  assert.equal(run("LANG_BY_CODE.th.name"), "TH-name");
});

test("every status the broker can report has a human label", async () => {
  const { run } = await booted();
  const statuses = ["source", "awaiting_broker", "queued", "processing", "quota_wait", "retrying",
                    "translating", "completed", "failed", "unavailable", "interrupted"];
  for (const status of statuses) assert.ok(run(`STATE_LABELS[${JSON.stringify(status)}]`), status);
});

test("post sends JSON and returns the parsed reply", async () => {
  const seen = [];
  const page = load({ fetchImpl: async (url, options) => {
    seen.push([url, options]);
    return url === "/api/languages" ? { ok: true, json: async () => ({ languages: LANGUAGES }) }
                                    : { ok: true, json: async () => ({ accepted: true }) };
  } });
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  const reply = await page.sandbox.post("/api/captions", { text: "hi" });
  assert.deepEqual(reply, { accepted: true });
  const [, options] = seen.at(-1);
  assert.equal(options.method, "POST");
  assert.equal(options.headers["Content-Type"], "application/json");
  assert.equal(options.body, '{"text":"hi"}');
});

test("post turns a server error into an exception carrying the server's message", async () => {
  const page = load({ fetchImpl: async (url) => url === "/api/languages"
    ? { ok: true, json: async () => ({ languages: LANGUAGES }) }
    : { ok: false, json: async () => ({ error: "Caption text cannot be empty" }) } });
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(page.sandbox.post("/api/captions", { text: "" }), /Caption text cannot be empty/);
});

test("post falls back to a generic message when the error has no text", async () => {
  const page = load({ fetchImpl: async (url) => url === "/api/languages"
    ? { ok: true, json: async () => ({ languages: LANGUAGES }) }
    : { ok: false, json: async () => ({}) } });
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(page.sandbox.post("/x"), /Request failed/);
});

test("captions render oldest-first by chunk number, newest twelve only", async () => {
  const { sandbox, registry, get } = await booted();
  const card = get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  const items = Array.from({ length: 15 }, (_, i) => caption({ number: 15 - i, text: `line ${15 - i}`, chunk_id: `c${i}` }));
  sandbox.renderCaptions({ th: items }, { th: { connected: true } });
  assert.equal(feed.children.length, 12);
  assert.match(feed.children[0].allText(), /line 4\b/);
  assert.match(feed.children[11].allText(), /line 15\b/);
  assert.ok(card.classList.contains("connected"));
});

test("a language with no captions shows its waiting message, Burmese in Burmese", async () => {
  const { sandbox, get } = await booted();
  get('.caption-card[data-lang="th"]');
  get('.caption-card[data-lang="my"]');
  sandbox.renderCaptions({ th: [], my: [] }, {});
  assert.equal(get("#captions-th").children[0].textContent, "Waiting for the first caption…");
  assert.equal(get("#captions-my").children[0].textContent, "ပထမစာတန်းကို စောင့်နေသည်…");
});

test("the original line appears only when it differs and names the spoken language", async () => {
  const { sandbox, get } = await booted();
  get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  sandbox.renderCaptions({ th: [
    caption({ number: 1, text: "translated", original_text: "spoken", original_source_lang: "my" }),
    caption({ number: 2, text: "same", original_text: "same", original_source_lang: "en" }),
    caption({ number: 3, text: "detected", original_text: "orig", original_source_lang: "auto" }),
  ] }, {});
  const [first, second, third] = feed.children.map((c) => c.allText());
  assert.match(first, /spoken/);
  assert.match(first, /MY-name/);
  assert.doesNotMatch(second, /“same”/);   // not repeated
  assert.match(third, /auto-detected/);
});

test("same-language captions are labelled as such and other backends keep their name", async () => {
  const { sandbox, get } = await booted();
  get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  sandbox.renderCaptions({ th: [
    caption({ number: 1, translation_backend: "passthrough" }),
    caption({ number: 2, translation_backend: "libretranslate-pivot" }),
  ] }, {});
  assert.match(feed.children[0].allText(), /same language/);
  assert.match(feed.children[1].allText(), /libretranslate-pivot/);
});

test("caption text is inserted as text, never as markup", async () => {
  const { sandbox, get } = await booted();
  get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  // FakeElement throws if innerHTML is ever assigned, so this also proves it is not used.
  sandbox.renderCaptions({ th: [caption({ text: '<img src=x onerror=alert(1)>', original_text: "<b>x</b>" })] }, {});
  assert.match(feed.children[0].allText(), /<img src=x onerror=alert\(1\)>/);
});

test("unchanged captions are not re-rendered", async () => {
  const { sandbox, get } = await booted();
  get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  const data = { th: [caption()] };
  sandbox.renderCaptions(data, {});
  const first = feed.children[0];
  sandbox.renderCaptions(JSON.parse(JSON.stringify(data)), {});
  assert.equal(feed.children[0], first);
});

test("languages without a card on the page are skipped without error", async () => {
  const { sandbox } = await booted();
  assert.doesNotThrow(() => sandbox.renderCaptions({ zz: [caption()] }, {}));
});

test("the activity feed shows the newest twelve entries and ignores an empty list", async () => {
  const { sandbox, get } = await booted();
  const list = get("#activity-list");
  sandbox.renderActivity([]);
  assert.equal(list.children.length, 0);
  sandbox.renderActivity(Array.from({ length: 20 }, (_, i) => ({ message: `event ${i}`, kind: "info", timestamp: 1700000000 })));
  assert.equal(list.children.length, 12);
  assert.match(list.children[0].allText(), /event 0/);
  assert.match(list.children[0].className, /activity-item info/);
});

test("chunk progress counts finished and troubled translations", async () => {
  const { sandbox, get } = await booted();
  const targets = (statuses) => Object.fromEntries(LANGUAGES.map((l, i) => [l.code, { status: statuses[i % statuses.length] }]));
  sandbox.renderChunks({
    chunks: [{ number: 1, source_lang: "en", text: "a", targets: targets(["completed"]) },
             { number: 2, source_lang: "my", text: "b", targets: targets(["failed", "completed", "source"]) }],
    media: { status: "idle" }, live: {},
  });
  assert.match(get("#translation-progress").textContent, /2 chunks · 15\/18 translations done · 3 need attention/);
});

test("chunk progress uses the singular for one chunk and hides the attention note", async () => {
  const { sandbox, get } = await booted();
  sandbox.renderChunks({ chunks: [{ number: 1, source_lang: "en", text: "a",
    targets: Object.fromEntries(LANGUAGES.map((l) => [l.code, { status: "completed" }])) }], media: { status: "idle" } });
  assert.equal(get("#translation-progress").textContent, "1 chunk · 9/9 translations done");
});

test("the chunk table has a column per language and a labelled badge per target", async () => {
  const { sandbox, get } = await booted();
  sandbox.renderChunks({
    chunks: [{ number: 7, source_lang: "my", text: "hello", targets: { th: { status: "translating", worker_id: "th-1", detail: "Gemini request in progress" } } }],
    media: { status: "idle" },
  });
  const head = get("#chunk-table thead").children[0];
  assert.equal(head.children.length, 10);            // chunk column + nine languages
  const row = get("#chunk-table tbody").children[0];
  assert.match(row.children[0].allText(), /#7 · MY-name/);
  const thCell = row.children[1 + LANGUAGES.findIndex((l) => l.code === "th")];
  assert.equal(thCell.children[0].textContent, "Translating");
  assert.equal(thCell.children[0].title, "th-1 · Gemini request in progress");
  assert.equal(row.children[1].children[0].textContent, "Queued");   // a language with no state yet
});

test("the media line reports live slicing counters and gaps honestly", async () => {
  const { sandbox, get, run } = await booted();
  run("liveActive = true");
  sandbox.renderChunks({ chunks: [], media: { status: "idle" },
    live: { slices: 5, chunks: 4, dropped: 1, unrecognized: 2, late: 1 } });
  const line = get("#media-progress").textContent;
  assert.match(line, /Live: 5 slices transcribed/);
  assert.match(line, /4 captions sent/);
  assert.match(line, /1 skipped \(transcriber behind\)/);
  assert.match(line, /2 not recognised \(gap\)/);
  assert.match(line, /1 too slow \(gap\)/);
});

test("the media line explains upload progress and failures", async () => {
  const { sandbox, get } = await booted();
  sandbox.renderChunks({ chunks: [], media: { status: "transcribing", chunks: 3 }, live: {} });
  assert.match(get("#media-progress").textContent, /Transcribing file… 3 speech chunks/);
  sandbox.renderChunks({ chunks: [], media: { status: "failed", error: "no speech" }, live: {} });
  assert.equal(get("#media-progress").textContent, "Transcription failed: no speech");
  sandbox.renderChunks({ chunks: [], media: { status: "transcribed", chunks: 4 }, live: {} });
  assert.match(get("#media-progress").textContent, /Transcription finished: 4 speech chunks/);
});

test("showToast shows the message and the clear button resets caption history", async () => {
  const { sandbox, get, calls } = await booted();
  sandbox.showToast("Saved");
  assert.equal(get("#toast").textContent, "Saved");
  assert.ok(get("#toast").classList.contains("show"));
  await get("#clear-button").click();
  assert.ok(calls.some(([url, options]) => url === "/api/clear" && options.method === "POST"));
});


async function settle() { for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve)); }

function recordingFetch(calls) {
  return async (url, options) => {
    calls.push([url, options || {}]);
    if (url === "/api/languages") return { ok: true, status: 200, json: async () => ({ languages: LANGUAGES }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
}

test("without an access token nothing extra is sent and the page still works", async () => {
  const calls = [];
  const page = load({ fetchImpl: recordingFetch(calls) });
  await settle();
  await page.sandbox.post("/api/clear");
  const [, options] = calls.at(-1);
  assert.equal(options.headers["X-Access-Token"], undefined);
});

test("a token in the link is sent with every change request and remembered for the tab", async () => {
  const calls = [];
  const page = load({ fetchImpl: recordingFetch(calls), search: "?token=abc123" });
  await settle();
  await page.sandbox.post("/api/captions", { text: "hi" });
  assert.equal(calls.at(-1)[1].headers["X-Access-Token"], "abc123");
  assert.equal(calls.at(-1)[1].headers["Content-Type"], "application/json");
  assert.equal(page.sandbox.sessionStorage.data.get("polyglot-token"), "abc123");
});

test("a remembered token is used after the link's query string is gone", async () => {
  const calls = [];
  const page = load({ fetchImpl: recordingFetch(calls), search: "", stored: { "polyglot-token": "kept" } });
  await settle();
  await page.sandbox.post("/api/clear");
  assert.equal(calls.at(-1)[1].headers["X-Access-Token"], "kept");
});

test("file uploads carry the token too", async () => {
  const calls = [];
  const page = load({ fetchImpl: recordingFetch(calls), search: "?token=up1" });
  await settle();
  await page.sandbox.postAudio({ size: 10, type: "audio/webm" }, "my", "clip");
  const upload = calls.find(([url]) => url.startsWith("/api/captions/audio"));
  assert.equal(upload[1].headers["X-Access-Token"], "up1");
  assert.equal(upload[1].headers["Content-Type"], "audio/webm");
});

test("live slices carry the token and a refusal is explained to the speaker", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push([url, options || {}]);
    if (url === "/api/languages") return { ok: true, status: 200, json: async () => ({ languages: LANGUAGES }) };
    if (url.startsWith("/api/live/slice")) return { ok: false, status: 401, json: async () => ({ error: "Access token required" }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const page = load({ fetchImpl, search: "?token=live1" });
  await settle();
  page.sandbox.sendLiveSlice({ type: "audio/webm" }, "my");
  await settle();
  const slice = calls.find(([url]) => url.startsWith("/api/live/slice"));
  assert.equal(slice[1].headers["X-Access-Token"], "live1");
  assert.match(page.get("#toast").textContent, /Access token required/);
});

test("a server that demands a token reports it clearly instead of failing silently", async () => {
  const fetchImpl = async (url) => url === "/api/languages"
    ? { ok: true, status: 200, json: async () => ({ languages: LANGUAGES }) }
    : { ok: false, status: 401, json: async () => ({ error: "Access token required" }) };
  const page = load({ fetchImpl });
  await settle();
  await assert.rejects(page.sandbox.post("/api/captions", { text: "x" }), /Access token required/);
});
