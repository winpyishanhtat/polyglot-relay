const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { languageFromPath, mergeRows, newestFirst, toSpeak } = require(path.join(__dirname, "..", "..", "ui", "listen.js"));

const row = (id, number, th, extra = {}) => ({
  chunk_id: id, number, text: `said ${number}`, source_lang: "en", created_at: 100 + number,
  translations: th === undefined ? {} : { th: { text: th, timestamp: 100 + number } }, ...extra,
});

test("the language comes from the address", () => {
  assert.equal(languageFromPath("/listen/th"), "th");
  assert.equal(languageFromPath("/listen/th/"), "th");
  assert.equal(languageFromPath("/listen/fil"), "fil");
  for (const path of ["/listen", "/listen/", "/", "/listen/TH", "/listen/th/x", "/listen/t", "/listenth", ""]) {
    assert.equal(languageFromPath(path), null, path);
  }
  assert.equal(languageFromPath(undefined), null);
});

test("only chunks that already have the language are taken, in speaking order", () => {
  const seen = new Map();
  const fresh = mergeRows(seen, [row("b", 2, "two"), row("a", 1, "one"), row("c", 3)], "th");
  assert.deepEqual(fresh.map((e) => e.text), ["one", "two"]);
  assert.equal(seen.size, 2);
});

test("a chunk is reported once, however many times the server repeats it", () => {
  const seen = new Map();
  assert.equal(mergeRows(seen, [row("a", 1, "one")], "th").length, 1);
  assert.equal(mergeRows(seen, [row("a", 1, "one")], "th").length, 0);
  assert.equal(mergeRows(seen, [row("a", 1, "changed")], "th").length, 0);
  assert.equal(seen.get("a").text, "one");
});

test("a chunk translated late is picked up on a later poll even though its number is older", () => {
  const seen = new Map();
  mergeRows(seen, [row("a", 1), row("b", 2, "two")], "th");
  const fresh = mergeRows(seen, [row("a", 1, "one"), row("b", 2, "two")], "th");
  assert.deepEqual(fresh.map((e) => e.text), ["one"]);
  assert.deepEqual(newestFirst(seen, 10).map((e) => e.text), ["two", "one"]);
});

test("empty or missing translations are ignored", () => {
  const seen = new Map();
  const rows = [row("a", 1, ""), { chunk_id: "b", number: 2 }, null, row("c", 3, "three")].filter(Boolean);
  assert.deepEqual(mergeRows(seen, rows, "th").map((e) => e.text), ["three"]);
  assert.deepEqual(mergeRows(seen, undefined, "th"), []);
});

test("the original wording and spoken language travel with the caption", () => {
  const [entry] = mergeRows(new Map(), [row("a", 1, "one", { source_lang: "my" })], "th");
  assert.equal(entry.original, "said 1");
  assert.equal(entry.source, "my");
});

test("the newest captions come first and the list is capped", () => {
  const seen = new Map();
  mergeRows(seen, Array.from({ length: 20 }, (_, i) => row(`c${i}`, i + 1, `t${i + 1}`)), "th");
  const shown = newestFirst(seen, 5);
  assert.deepEqual(shown.map((e) => e.number), [20, 19, 18, 17, 16]);
});

test("of a big batch only the newest few are read aloud, still in order", () => {
  const entries = [1, 2, 3, 4, 5].map((n) => ({ number: n }));
  assert.deepEqual(toSpeak(entries).map((e) => e.number), [4, 5]);
  assert.deepEqual(toSpeak(entries.slice(0, 1)).map((e) => e.number), [1]);
  assert.deepEqual(toSpeak([]), []);
});
