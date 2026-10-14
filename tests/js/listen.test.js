const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { languageFromPath, chooseLanguages, mergeChunks, newestChunks, toSpeak } =
  require(path.join(__dirname, "..", "..", "ui", "listen.js"));

const ALL = ["en", "my", "id", "ms", "th", "vi", "tl", "km", "lo"].map((code) => ({ code }));

const row = (id, number, translations = {}, extra = {}) => ({
  chunk_id: id, number, text: `said ${number}`, source_lang: "en", created_at: 100 + number,
  translations: Object.fromEntries(Object.entries(translations).map(([lang, text]) => [lang, { text, timestamp: 100 + number }])),
  ...extra,
});

test("the language comes from the address", () => {
  assert.equal(languageFromPath("/listen/th"), "th");
  assert.equal(languageFromPath("/listen/th/"), "th");
  for (const p of ["/listen", "/listen/", "/", "/listen/TH", "/listen/th/x", "/listen/t", "/listenth", ""]) {
    assert.equal(languageFromPath(p), null, p);
  }
  assert.equal(languageFromPath(undefined), null);
});

test("with nothing chosen every language is shown", () => {
  assert.deepEqual(chooseLanguages(ALL), ALL.map((l) => l.code));
  assert.deepEqual(chooseLanguages(ALL, { pathname: "/listen" }), ALL.map((l) => l.code));
});

test("?langs= picks the languages, in the page's own order, ignoring unknown codes", () => {
  assert.deepEqual(chooseLanguages(ALL, { search: "?langs=km,th,zz,my" }), ["my", "th", "km"]);
  assert.deepEqual(chooseLanguages(ALL, { search: "?token=x&langs=lo" }), ["lo"]);
  assert.deepEqual(chooseLanguages(ALL, { search: "?langs=zz" }), ALL.map((l) => l.code));
});

test("a language in the path starts the page with just that one", () => {
  assert.deepEqual(chooseLanguages(ALL, { pathname: "/listen/th" }), ["th"]);
  assert.deepEqual(chooseLanguages(ALL, { pathname: "/listen/zz" }), ALL.map((l) => l.code));
});

test("the address beats the path, and the path beats what was chosen last time", () => {
  assert.deepEqual(chooseLanguages(ALL, { search: "?langs=my", pathname: "/listen/th", stored: "km" }), ["my"]);
  assert.deepEqual(chooseLanguages(ALL, { pathname: "/listen/th", stored: "km,lo" }), ["th"]);
  assert.deepEqual(chooseLanguages(ALL, { pathname: "/listen", stored: "lo,km" }), ["km", "lo"]);
  assert.deepEqual(chooseLanguages(ALL, { stored: "zz" }), ALL.map((l) => l.code));
});

test("one sentence collects every chosen language into a single entry", () => {
  const chunks = new Map();
  const arrivals = mergeChunks(chunks, [row("a", 1, { th: "สวัสดี", my: "မင်္ဂလာပါ", km: "x" })], ["th", "my"]);
  assert.equal(chunks.size, 1);
  assert.deepEqual(chunks.get("a").translations, { th: "สวัสดี", my: "မင်္ဂလာပါ" });   // km was not chosen
  assert.deepEqual(arrivals.map((a) => a.lang).sort(), ["my", "th"]);
});

test("languages that arrive later fill in the same sentence", () => {
  const chunks = new Map();
  assert.equal(mergeChunks(chunks, [row("a", 1, { th: "one" })], ["th", "my"]).length, 1);
  const later = mergeChunks(chunks, [row("a", 1, { th: "one", my: "bir" })], ["th", "my"]);
  assert.deepEqual(later.map((a) => [a.lang, a.text]), [["my", "bir"]]);
  assert.deepEqual(chunks.get("a").translations, { th: "one", my: "bir" });
  assert.equal(chunks.size, 1);
});

test("a translation is reported once, however many times the server repeats it", () => {
  const chunks = new Map();
  const rows = [row("a", 1, { th: "one" })];
  assert.equal(mergeChunks(chunks, rows, ["th"]).length, 1);
  assert.equal(mergeChunks(chunks, rows, ["th"]).length, 0);
  assert.equal(mergeChunks(chunks, [row("a", 1, { th: "changed" })], ["th"]).length, 0);
  assert.equal(chunks.get("a").translations.th, "one");
});

test("a sentence with none of the chosen languages yet is not shown", () => {
  const chunks = new Map();
  assert.deepEqual(mergeChunks(chunks, [row("a", 1, { km: "x" }), row("b", 2, {})], ["th"]), []);
  assert.equal(chunks.size, 0);
});

test("empty, missing or malformed rows are ignored", () => {
  const chunks = new Map();
  const rows = [row("a", 1, { th: "" }), { chunk_id: "b", number: 2 }, null, row("c", 3, { th: "three" })];
  assert.deepEqual(mergeChunks(chunks, rows, ["th"]).map((a) => a.text), ["three"]);
  assert.deepEqual(mergeChunks(chunks, undefined, ["th"]), []);
});

test("arrivals come back in speaking order whatever order the server sent", () => {
  const arrivals = mergeChunks(new Map(), [row("b", 2, { th: "two" }), row("a", 1, { th: "one" })], ["th"]);
  assert.deepEqual(arrivals.map((a) => a.text), ["one", "two"]);
});

test("the spoken original and its language travel with the sentence", () => {
  const chunks = new Map();
  mergeChunks(chunks, [row("a", 1, { th: "one" }, { source_lang: "my" })], ["th"]);
  assert.equal(chunks.get("a").original, "said 1");
  assert.equal(chunks.get("a").source, "my");
});

test("the newest sentences come first and the list is capped", () => {
  const chunks = new Map();
  mergeChunks(chunks, Array.from({ length: 20 }, (_, i) => row(`c${i}`, i + 1, { th: `t${i + 1}` })), ["th"]);
  assert.deepEqual(newestChunks(chunks, 5).map((c) => c.number), [20, 19, 18, 17, 16]);
});

test("only the newest few captions in the reading language are read aloud, still in order", () => {
  const arrivals = [
    { lang: "th", number: 1 }, { lang: "my", number: 1 }, { lang: "th", number: 2 },
    { lang: "my", number: 2 }, { lang: "th", number: 3 }, { lang: "th", number: 4 },
  ];
  assert.deepEqual(toSpeak(arrivals, "th").map((a) => a.number), [3, 4]);
  assert.deepEqual(toSpeak(arrivals, "my").map((a) => a.number), [1, 2]);
  assert.deepEqual(toSpeak(arrivals, "km"), []);
  assert.deepEqual(toSpeak([], "th"), []);
});
