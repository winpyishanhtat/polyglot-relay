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

function load({ fetchImpl, search, stored, speech = false, streaming = false } = {}) {
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
      if (String(url).startsWith("/api/history")) {
        return { ok: true, json: async () => ({ chunks: [], total: 0, returned: 0, truncated: false, retained: 0, max_retained: 2000, server_time: 0 }) };
      }
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
    confirm: () => true,
    URL: { createObjectURL: () => "blob:default", revokeObjectURL() {} },
  };
  if (streaming) {
    sandbox.streams = [];
    sandbox.EventSource = class {
      constructor(url) { this.url = url; this.listeners = {}; sandbox.streams.push(this); }
      addEventListener(kind, handler) { (this.listeners[kind] ||= []).push(handler); }
      emit(kind, data) { for (const handler of this.listeners[kind] || []) handler({ data: data === undefined ? "{}" : JSON.stringify(data) }); }
    };
  }
  sandbox.window = sandbox;
  if (search !== undefined) sandbox.location = { search };
  vm.createContext(sandbox);
  // live_slicer.js is a classic script that defines LiveSlicer globally for app.js.
  vm.runInContext(fs.readFileSync(path.join(UI, "live_slicer.js"), "utf8"), sandbox, { filename: "live_slicer.js" });
  if (speech) vm.runInContext(fs.readFileSync(path.join(UI, "speech.js"), "utf8"), sandbox, { filename: "speech.js" });
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

test("a live card shows every caption the server keeps, oldest first", async () => {
  const { sandbox, get } = await booted();
  get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  const items = Array.from({ length: 50 }, (_, i) => caption({ number: 50 - i, text: `line ${50 - i}`, chunk_id: `c${i}` }));
  sandbox.renderCaptions({ th: items }, { th: { connected: true } });
  assert.equal(feed.children.length, 50);
  assert.match(feed.children[0].allText(), /line 1\b/);
  assert.match(feed.children[49].allText(), /line 50\b/);
  assert.ok(get('.caption-card[data-lang="th"]').classList.contains("connected"));
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


// ---- history -------------------------------------------------------------------------

function historyPayload(chunks, extra = {}) {
  return { chunks, total: chunks.length, returned: chunks.length, truncated: false, retained: chunks.length,
           max_retained: 2000, server_time: 1700000100, ...extra };
}

function chunkRow(overrides = {}) {
  return { chunk_id: "c1", number: 1, created_at: 1700000000, text: "Good morning", source_lang: "en",
           translations: { th: { text: "sawasdee", backend: "gemini-text" } }, ...overrides };
}

function historyFetch(calls, payload) {
  return async (url, options) => {
    calls.push([url, options || {}]);
    if (url === "/api/languages") return { ok: true, status: 200, json: async () => ({ languages: LANGUAGES }) };
    if (url.startsWith("/api/history")) return { ok: true, status: 200, json: async () => payload };
    if (url.startsWith("/api/export")) {
      return { ok: true, status: 200, blob: async () => ({}), headers: { get: () => 'attachment; filename="polyglot-captions-1.txt"' } };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
}

async function historyPage(payload = historyPayload([]), options = {}) {
  const calls = [];
  const page = load({ fetchImpl: historyFetch(calls, payload), ...options });
  page.sandbox.URL = { createObjectURL: () => "blob:test", revokeObjectURL() {} };
  await settle();
  return { page, calls, run: page.run, get: page.get };
}

test("the history asks for the last 15 minutes in English by default", async () => {
  const { calls } = await historyPage();
  const url = calls.find(([u]) => u.startsWith("/api/history"))[0];
  const params = new URLSearchParams(url.split("?")[1]);
  assert.equal(params.get("last_minutes"), "15");
  assert.equal(params.get("langs"), "en");
  assert.equal(params.get("limit"), "500");
  assert.equal(params.get("q"), null);
});

test("the time frame choices become the right request", async () => {
  const { page, run } = await historyPage();
  const params = (range) => { run(`historyView.range = ${JSON.stringify(range)}`); return page.sandbox.historyParams(); };
  assert.equal(params("5").get("last_minutes"), "5");
  assert.equal(params("60").get("last_minutes"), "60");
  const whole = params("all");
  assert.equal(whole.get("last_minutes"), null);
  assert.equal(whole.get("since"), null);
});

test("a custom range sends the chosen start and end as unix times", async () => {
  const { page, run, get } = await historyPage();
  get("#history-from").value = "2026-10-13T10:00:00";
  get("#history-to").value = "2026-10-13T10:30:00";
  run('historyView.range = "custom"');
  const params = page.sandbox.historyParams();
  assert.equal(Number(params.get("until")) - Number(params.get("since")), 1800);
  assert.equal(params.get("last_minutes"), null);
});

test("a custom range with only a start is open ended", async () => {
  const { page, run, get } = await historyPage();
  get("#history-from").value = "2026-10-13T10:00:00";
  get("#history-to").value = "";
  run('historyView.range = "custom"');
  const params = page.sandbox.historyParams();
  assert.ok(params.get("since"));
  assert.equal(params.get("until"), null);
});

test("languages are requested in the page's order and the search text is passed on", async () => {
  const { page, run } = await historyPage();
  run('historyView.langs = new Set(["lo", "th", "en"]); historyView.query = "break time"');
  const params = page.sandbox.historyParams();
  assert.equal(params.get("langs"), "en,th,lo");
  assert.equal(params.get("q"), "break time");
});

test("choosing no languages asks for the original text only", async () => {
  const { page, run } = await historyPage();
  run("historyView.langs = new Set()");
  assert.equal(page.sandbox.historyParams().get("langs"), "");
});

test("each chunk shows when it was said, the language spoken, the original and the chosen translations", async () => {
  const { page, get, run } = await historyPage();
  run('historyView.langs = new Set(["th"])');
  page.sandbox.renderHistory(historyPayload([chunkRow({ number: 7, source_lang: "my", text: "original text" })]));
  const row = get("#history-list").children[0];
  const text = row.allText();
  assert.match(text, /#7/);
  assert.match(text, /MY-name/);
  assert.match(text, /original text/);
  assert.match(text, /TH/);
  assert.match(text, /sawasdee/);
  assert.match(text, /gemini-text/);
  assert.match(row.children[0].allText(), /\d{1,2}:\d{2}:\d{2}/);
});

test("languages that are not chosen are not shown", async () => {
  const { page, get } = await historyPage();
  page.sandbox.renderHistory(historyPayload([chunkRow({ translations: { th: { text: "sawasdee" }, km: { text: "khmer text" } } })]));
  const text = get("#history-list").children[0].allText();
  assert.doesNotMatch(text, /sawasdee/);       // only English is chosen by default
  assert.doesNotMatch(text, /khmer text/);
});

test("the spoken language's own copy is not repeated under the original", async () => {
  const { page, run, get } = await historyPage();
  run('historyView.langs = new Set(["en", "th"])');
  page.sandbox.renderHistory(historyPayload([chunkRow({ translations: {
    en: { text: "Good morning", backend: "passthrough" }, th: { text: "sawasdee", backend: "gemini-text" } } })]));
  const lines = get("#history-list").children[0].children.filter((c) => c.className === "history-line");
  assert.equal(lines.length, 1);
  assert.match(lines[0].allText(), /sawasdee/);
});

test("a language still being translated says so, and later says there is none", async () => {
  const { page, run, get } = await historyPage();
  run('historyView.langs = new Set(["th"])');
  page.sandbox.renderHistory(historyPayload([chunkRow({ created_at: 1700000090, translations: {} })]));   // 10 s old
  assert.match(get("#history-list").children[0].allText(), /translating/);
  page.sandbox.renderHistory(historyPayload([chunkRow({ created_at: 1699990000, translations: {} })]));   // long ago
  assert.match(get("#history-list").children[0].allText(), /no translation/);
});

test("a chunk whose original was never recorded is labelled rather than blank", async () => {
  const { page, get } = await historyPage();
  page.sandbox.renderHistory(historyPayload([chunkRow({ text: null, number: null, source_lang: null })]));
  const text = get("#history-list").children[0].allText();
  assert.match(text, /original not recorded/);
  assert.match(text, /auto-detected/);
});

test("the count line says how much is shown, whether the newest were cut, and what is kept", async () => {
  const { page, get } = await historyPage();
  page.sandbox.renderHistory(historyPayload([chunkRow()], { total: 1, returned: 1, retained: 40, max_retained: 2000 }));
  assert.equal(get("#history-count").textContent, "1 of 1 chunk · 40 kept (up to 2000)");
  page.sandbox.renderHistory(historyPayload([chunkRow(), chunkRow({ chunk_id: "c2" })], { total: 900, returned: 2, truncated: true }));
  assert.match(get("#history-count").textContent, /2 of 900 chunks · newest 2 shown/);
});

test("empty history gives different advice for no speech yet and no match", async () => {
  const { page, get } = await historyPage();
  page.sandbox.renderHistory(historyPayload([], { retained: 0, total: 0 }));
  assert.match(get("#history-list").children[0].textContent, /Nothing spoken yet/);
  page.sandbox.renderHistory(historyPayload([], { retained: 25, total: 0 }));
  assert.match(get("#history-list").children[0].textContent, /Nothing matches this time frame/);
});

test("history text is inserted as text, never as markup", async () => {
  const { page, get } = await historyPage();
  // FakeElement throws if innerHTML is ever assigned.
  page.sandbox.renderHistory(historyPayload([chunkRow({ text: "<img src=x onerror=alert(1)>",
    translations: { th: { text: "<b>bold</b>", backend: "<script>" } } })]));
  assert.match(get("#history-list").children[0].allText(), /<img src=x onerror=alert\(1\)>/);
});

test("with follow on, the list stays at the newest; scrolled up, it is left where it was", async () => {
  const { page, get } = await historyPage();
  const list = get("#history-list");
  list.scrollHeight = 5000; list.clientHeight = 500; list.scrollTop = 4500;     // at the bottom
  page.sandbox.renderHistory(historyPayload([chunkRow()]));
  assert.equal(list.scrollTop, 5000);
  list.scrollHeight = 5000; list.scrollTop = 1200;                              // reading older entries
  page.sandbox.renderHistory(historyPayload([chunkRow()]));
  assert.equal(list.scrollTop, 1200);
});

test("with follow switched off the list never jumps to the bottom", async () => {
  const { page, run, get } = await historyPage();
  const list = get("#history-list");
  run("historyView.follow = false");
  list.scrollHeight = 5000; list.clientHeight = 500; list.scrollTop = 4500;
  page.sandbox.renderHistory(historyPayload([chunkRow()]));
  assert.equal(list.scrollTop, 4500);
});

test("changing the time frame reloads, and a custom range reveals its date fields", async () => {
  const { calls, get } = await historyPage();
  const before = calls.filter(([u]) => u.startsWith("/api/history")).length;
  const range = get("#history-range");
  range.value = "custom";
  for (const handler of range.listeners.change) handler({});
  await settle();
  assert.equal(get("#history-custom").hidden, false);
  assert.ok(get("#history-from").value && get("#history-to").value);       // prefilled with the last 15 minutes
  assert.ok(calls.filter(([u]) => u.startsWith("/api/history")).length > before);
  range.value = "60";
  for (const handler of range.listeners.change) handler({});
  assert.equal(get("#history-custom").hidden, true);
});

test("the time frame menu offers recent windows, the whole session and a custom range", async () => {
  const { get } = await historyPage();
  assert.deepEqual(get("#history-range").children.map((o) => o.value), ["5", "15", "60", "all", "custom"]);
});

test("toggling a language chip changes what is requested", async () => {
  const { calls, get, run } = await historyPage();
  const chips = get("#history-langs").children;
  assert.equal(chips.length, 9);
  const thai = chips.find((c) => c.dataset.lang === "th");
  await thai.click();
  await settle();
  assert.equal(run("Array.from(historyView.langs).sort().join()"), "en,th");
  assert.equal(thai.attributes["aria-pressed"], "true");
  const last = calls.filter(([u]) => u.startsWith("/api/history")).at(-1)[0];
  assert.equal(new URLSearchParams(last.split("?")[1]).get("langs"), "en,th");
  await thai.click();
  assert.equal(run("historyView.langs.has('th')"), false);
});

test("All selects every language and Original only selects none", async () => {
  const { get, run } = await historyPage();
  await get("#history-all").click();
  assert.equal(run("historyView.langs.size"), 9);
  await get("#history-none").click();
  assert.equal(run("historyView.langs.size"), 0);
  assert.ok(get("#history-langs").children.every((c) => c.attributes["aria-pressed"] === "false"));
});

test("typing in the search box sets the query after a short pause", async () => {
  const { get, run, page } = await historyPage();
  const timers = [];
  page.sandbox.setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
  const search = get("#history-search");
  for (const handler of search.listeners.input) handler({ target: { value: "  coffee break " } });
  assert.equal(run("historyView.query"), "coffee break");
  assert.equal(timers.at(-1)[1], 250);
});

test("the follow checkbox controls live updating", async () => {
  const { get, run } = await historyPage();
  for (const handler of get("#history-follow").listeners.change) handler({ target: { checked: false } });
  assert.equal(run("historyView.follow"), false);
});

test("export asks the server for the chosen format over the same range and downloads the file", async () => {
  const { page, calls } = await historyPage();
  const clicked = [];
  const originalCreate = page.sandbox.document.createElement;
  page.sandbox.document.createElement = (tag) => {
    const element = originalCreate(tag);
    if (tag === "a") element.click = () => clicked.push(element);
    return element;
  };
  await page.sandbox.exportHistory("csv");
  const url = calls.find(([u]) => u.startsWith("/api/export"))[0];
  const params = new URLSearchParams(url.split("?")[1]);
  assert.equal(params.get("format"), "csv");
  assert.equal(params.get("last_minutes"), "15");
  assert.equal(params.get("langs"), "en");
  assert.equal(clicked.length, 1);
  assert.equal(clicked[0].download, "polyglot-captions-1.txt");
  assert.equal(clicked[0].href, "blob:test");
});

test("subtitle export names the first chosen language", async () => {
  const { page, calls, run } = await historyPage();
  run('historyView.langs = new Set(["lo", "km"])');
  await page.sandbox.exportHistory("srt");
  const params = new URLSearchParams(calls.find(([u]) => u.startsWith("/api/export"))[0].split("?")[1]);
  assert.equal(params.get("lang"), "km");
});

test("subtitles need a language", async () => {
  const { page, calls, run, get } = await historyPage();
  run("historyView.langs = new Set()");
  await page.sandbox.exportHistory("srt");
  assert.equal(calls.filter(([u]) => u.startsWith("/api/export")).length, 0);
  assert.match(get("#toast").textContent, /Choose a language/);
});

test("an export the server refuses is explained, not downloaded", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push([url, options || {}]);
    if (url === "/api/languages") return { ok: true, status: 200, json: async () => ({ languages: LANGUAGES }) };
    if (url.startsWith("/api/export")) return { ok: false, status: 404, json: async () => ({ error: "No captions in that time range" }) };
    return { ok: true, status: 200, json: async () => historyPayload([]) };
  };
  const page = load({ fetchImpl });
  await settle();
  await page.sandbox.exportHistory("txt");
  assert.match(page.get("#toast").textContent, /No captions in that time range/);
});

test("if the history cannot be loaded the count line says why", async () => {
  const fetchImpl = async (url) => url === "/api/languages"
    ? { ok: true, status: 200, json: async () => ({ languages: LANGUAGES }) }
    : { ok: false, status: 400, json: async () => ({ error: "since must be a number" }) };
  const page = load({ fetchImpl });
  await settle();
  assert.equal(page.get("#history-count").textContent, "since must be a number");
});

test("clearing asks first, and does nothing if you decline", async () => {
  const calls = [];
  const page = load({ fetchImpl: historyFetch(calls, historyPayload([])) });
  page.sandbox.confirm = () => false;
  await settle();
  await page.get("#clear-button").click();
  assert.equal(calls.filter(([u]) => u === "/api/clear").length, 0);
  page.sandbox.confirm = () => true;
  await page.get("#clear-button").click();
  assert.equal(calls.filter(([u]) => u === "/api/clear").length, 1);
});

// ---- live caption cards ----------------------------------------------------------------

test("a live card follows the newest caption unless the reader has scrolled up", async () => {
  const { sandbox, get } = await booted();
  get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  feed.scrollHeight = 3000; feed.scrollTop = 0;
  sandbox.renderCaptions({ th: [caption({ number: 1, text: "a" })] }, {});
  assert.equal(feed.scrollTop, 3000);                           // following by default
  feed.dataset.pinned = "1"; feed.scrollTop = 700;              // reader scrolled up
  sandbox.renderCaptions({ th: [caption({ number: 1, text: "a" }), caption({ number: 2, text: "b" })] }, {});
  assert.equal(feed.scrollTop, 700);
});

test("scrolling a live card up pins it and scrolling back down releases it", async () => {
  const { get } = await booted();
  const feed = get("#caption-grid").children.find((card) => card.dataset.lang === "th").children[1];
  feed.scrollHeight = 3000; feed.clientHeight = 232; feed.scrollTop = 500;
  for (const handler of feed.listeners.scroll || []) handler({});
  assert.equal(feed.dataset.pinned, "1");
  feed.scrollTop = 2760;
  for (const handler of feed.listeners.scroll || []) handler({});
  assert.equal(feed.dataset.pinned, "");
});

// ---- reading captions aloud ---------------------------------------------------------------

function findAll(element, predicate, found = []) {
  if (predicate(element)) found.push(element);
  for (const child of element.children) findAll(child, predicate, found);
  return found;
}

async function bootedWithSpeech() {
  const page = load({ speech: true });
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  return page;
}

test("each caption gets a Listen button when the speech helper is loaded", async () => {
  const { sandbox, get } = await bootedWithSpeech();
  get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  sandbox.renderCaptions({ th: [caption({ number: 4, text: "one" }), caption({ number: 5, text: "two", chunk_id: "d" })] }, {});
  const buttons = findAll(feed, (e) => e.className.includes("speak-button"));
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0].textContent, "Listen");
  assert.equal(buttons[0].attributes["aria-label"], "Read caption 4 aloud");
});

test("there is no Listen button, and nothing breaks, without the speech helper", async () => {
  const { sandbox, get } = await booted();
  get('.caption-card[data-lang="th"]');
  const feed = get("#captions-th");
  sandbox.renderCaptions({ th: [caption()] }, {});
  assert.equal(findAll(feed, (e) => e.className.includes("speak-button")).length, 0);
});

test("pressing Listen asks the relay to speak that caption in its language", async () => {
  const { sandbox, get, calls } = await bootedWithSpeech();
  get('.caption-card[data-lang="km"]');
  const feed = get("#captions-km");
  sandbox.renderCaptions({ km: [caption({ text: "hello in khmer" })] }, {});
  const [button] = findAll(feed, (e) => e.className.includes("speak-button"));
  await button.click();
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
  const [, options] = calls.find(([url]) => url === "/api/tts");
  assert.equal(options.method, "POST");
  assert.deepEqual(JSON.parse(options.body), { text: "hello in khmer", lang: "km" });
});

test("the Listen request carries the access token when the page was opened with one", async () => {
  const page = load({ speech: true, search: "?token=s3cret" });
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  page.get('.caption-card[data-lang="th"]');
  page.sandbox.renderCaptions({ th: [caption()] }, {});
  const [button] = findAll(page.get("#captions-th"), (e) => e.className.includes("speak-button"));
  await button.click();
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
  const [, options] = page.calls.find(([url]) => url === "/api/tts");
  assert.equal(options.headers["X-Access-Token"], "s3cret");
});

// ---- rooms and pushed updates --------------------------------------------------------------

async function bootedWith(options) {
  const page = load(options);
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  return page;
}

test("in a room every request carries the room", async () => {
  const page = await bootedWith({ search: "?room=hall-a" });
  await page.sandbox.refresh();
  await page.sandbox.post("/api/captions", { text: "hi" });
  const urls = page.calls.map(([url]) => url);
  assert.ok(urls.includes("/api/status?room=hall-a"), urls.join(" "));
  assert.ok(urls.includes("/api/captions?room=hall-a"), urls.join(" "));
  assert.ok(urls.some((u) => u.startsWith("/api/history?") && u.includes("room=hall-a")), urls.join(" "));
});

test("the default room leaves the requests as they were", async () => {
  const page = await bootedWith({ search: "" });
  await page.sandbox.refresh();
  const urls = page.calls.map(([url]) => url);
  assert.ok(urls.includes("/api/status"));
  assert.ok(!urls.some((u) => u.includes("room=")), urls.join(" "));
});

test("withRoom adds the room with the right separator", async () => {
  const page = await bootedWith({ search: "?room=r2" });
  assert.equal(page.run('withRoom("/api/status")'), "/api/status?room=r2");
  assert.equal(page.run('withRoom("/api/live/slice?source_lang=th")'), "/api/live/slice?source_lang=th&room=r2");
});

test("the dashboard opens a push stream for its room", async () => {
  const page = await bootedWith({ search: "?room=hall-b", streaming: true });
  assert.equal(page.sandbox.streams.length, 1);
  assert.equal(page.sandbox.streams[0].url, "/api/events?room=hall-b");
});

test("a pushed caption refreshes the dashboard straight away", async () => {
  const page = await bootedWith({ streaming: true });
  const timers = [];
  page.sandbox.setTimeout = (callback) => { timers.push(callback); return timers.length; };
  const before = page.calls.filter(([url]) => url === "/api/status").length;
  page.sandbox.streams[0].emit("caption");
  page.sandbox.streams[0].emit("caption");                    // a burst is folded into one refresh
  for (const callback of timers.splice(0)) await callback();
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(page.calls.filter(([url]) => url === "/api/status").length, before + 1);
  assert.ok(page.calls.some(([url]) => String(url).startsWith("/api/history")));
});

test("polling slows down while the stream is open and speeds up when it drops", async () => {
  const page = await bootedWith({ streaming: true });
  assert.equal(page.run("pollDelay()"), 1000);
  page.sandbox.streams[0].onopen();
  assert.equal(page.run("pollDelay()"), 4000);
  page.sandbox.streams[0].onerror();
  assert.equal(page.run("pollDelay()"), 1000);
});

// ---- interim captions and the voice band ---------------------------------------------------

test("words still being spoken are shown as a preview and cleared by the finished sentence", async () => {
  const page = await bootedWith({ streaming: true });
  const box = page.get("#live-interim");
  page.sandbox.streams[0].emit("interim", { text: "good morning every", lang: "en" });
  assert.equal(box.textContent, "Hearing: good morning every");
  assert.equal(box.hidden, false);
  page.sandbox.streams[0].emit("chunk", { chunk_id: "c1" });
  assert.equal(box.textContent, "");
  assert.equal(box.hidden, true);
});

test("the voice-band share is computed from the microphone spectrum", async () => {
  const page = await bootedWith({});
  // 2048-point FFT at 48 kHz: 23.4 Hz per bin. Put all energy at ~1 kHz (voice) then at ~60 Hz (hum).
  page.run(`
    liveAudioContext = { sampleRate: 48000 };
    liveAnalyser = { fftSize: 2048, frequencyBinCount: 1024, bin: 43,
      getFloatFrequencyData(out) { out.fill(-200); out[this.bin] = -20; } };
  `);
  assert.ok(page.run("currentVoiceRatio()") > 0.99);
  page.run("liveAnalyser.bin = 3; liveSpectrum = null;");
  assert.ok(page.run("currentVoiceRatio()") < 0.01);
  page.run("liveAnalyser = null;");
  assert.equal(page.run("currentVoiceRatio()"), undefined);
});

test("an interim is only sent while someone is speaking a phrase that has run a while", async () => {
  const page = await bootedWith({});
  page.run(`
    liveActive = true; liveSource = "th";
    liveSlicer = { inSpeech: true };
    liveRecorder = { state: "recording", startedAt: Date.now() - 3000, parts: [], mimeType: "audio/webm",
      requested: 0, requestData() { this.requested += 1; } };
  `);
  page.run("sendInterim()");
  assert.equal(page.run("liveRecorder.requested"), 1);
  page.run("liveSlicer.inSpeech = false; sendInterim()");
  assert.equal(page.run("liveRecorder.requested"), 1);            // a pause: nothing to preview
  page.run("liveSlicer.inSpeech = true; liveRecorder.startedAt = Date.now(); sendInterim()");
  assert.equal(page.run("liveRecorder.requested"), 1);            // the phrase has only just started
});
