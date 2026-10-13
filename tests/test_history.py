"""Caption history: what the dashboard remembers, how it is filtered by time, language and text,
how it is bounded, and how it is exported."""

import csv
import io
import json
import threading
import time
import unittest

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import history
import web_ui
from web_ui import DashboardHandler, DashboardRuntime, DashboardState

BURMESE = "\u1019\u1004\u103a\u1039\u1002\u101c\u102c\u1015\u102b"
THAI = "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35"


def say(state, chunk_id, text, source="en", at=None, **translations):
    """Record one spoken chunk and its translations, as the pipeline would."""
    state.record_original(chunk_id, text, source)
    if at is not None:
        state.history[chunk_id]["created_at"] = at
    for lang, translated in translations.items():
        state.add_caption({"lang": lang, "chunk_id": chunk_id, "text": translated, "worker_id": f"{lang}-1",
                           "translation_backend": "gemini-text", "timestamp": at or time.time()})


class HistoryStoreTests(unittest.TestCase):
    def test_a_chunk_keeps_its_original_and_every_translation(self):
        state = DashboardState()
        say(state, "c1", "Good morning", "en", th=THAI, my=BURMESE)
        row = state.query_history()["chunks"][0]
        self.assertEqual((row["text"], row["source_lang"], row["number"]), ("Good morning", "en", 1))
        self.assertEqual({lang: t["text"] for lang, t in row["translations"].items()}, {"th": THAI, "my": BURMESE})
        self.assertEqual(row["translations"]["th"]["backend"], "gemini-text")

    def test_history_outlives_the_fifty_caption_live_feed(self):
        state = DashboardState()
        for n in range(120):
            say(state, f"c{n}", f"sentence {n}", "en", th=f"t{n}")
        self.assertEqual(len(state.snapshot()["captions"]["th"]), 50)          # the live feed is short
        self.assertEqual(state.query_history(limit=1000)["total"], 120)         # the history is not

    def test_chunks_come_back_oldest_first(self):
        state = DashboardState()
        for n in range(5):
            say(state, f"c{n}", f"s{n}", "en", at=1000 + n)
        self.assertEqual([r["text"] for r in state.query_history()["chunks"]], ["s0", "s1", "s2", "s3", "s4"])

    def test_a_time_window_selects_only_the_chunks_inside_it(self):
        state = DashboardState()
        for n in range(10):
            say(state, f"c{n}", f"s{n}", "en", at=1000 + n * 60)          # one a minute
        got = state.query_history(since=1000 + 3 * 60, until=1000 + 6 * 60)["chunks"]
        self.assertEqual([r["text"] for r in got], ["s3", "s4", "s5", "s6"])      # both ends inclusive
        self.assertEqual(state.query_history(since=1000 + 9 * 60)["total"], 1)
        self.assertEqual(state.query_history(until=999)["total"], 0)

    def test_last_minutes_is_measured_on_the_servers_clock(self):
        state = DashboardState()
        now = time.time()
        say(state, "old", "ten minutes ago", "en", at=now - 600)
        say(state, "new", "just now", "en", at=now - 5)
        self.assertEqual([r["text"] for r in state.query_history(last_minutes=2)["chunks"]], ["just now"])
        self.assertEqual(state.query_history(last_minutes=15)["total"], 2)

    def test_only_the_requested_languages_are_returned(self):
        state = DashboardState()
        say(state, "c1", "hello", "en", th=THAI, my=BURMESE, km="khmer")
        translations = state.query_history(langs=["th", "km"])["chunks"][0]["translations"]
        self.assertEqual(set(translations), {"th", "km"})

    def test_unknown_language_codes_are_ignored_in_the_store_and_all_languages_are_the_default(self):
        state = DashboardState()
        say(state, "c1", "hello", "en", th=THAI, my=BURMESE)
        self.assertEqual(set(state.query_history()["chunks"][0]["translations"]), {"th", "my"})
        self.assertEqual(set(state.query_history(langs=["zz", "th"])["chunks"][0]["translations"]), {"th"})

    def test_text_search_matches_the_original_or_a_shown_translation_ignoring_case(self):
        state = DashboardState()
        say(state, "c1", "The Meeting starts", "en", th=THAI)
        say(state, "c2", "Lunch is ready", "en", th="\u0e2d\u0e32\u0e2b\u0e32\u0e23")
        self.assertEqual([r["text"] for r in state.query_history(query="meeting")["chunks"]], ["The Meeting starts"])
        self.assertEqual([r["text"] for r in state.query_history(query=THAI)["chunks"]], ["The Meeting starts"])
        self.assertEqual(state.query_history(query="nothing like this")["total"], 0)

    def test_search_only_looks_in_the_languages_being_shown(self):
        state = DashboardState()
        say(state, "c1", "hello", "en", th=THAI, my=BURMESE)
        self.assertEqual(state.query_history(query=BURMESE, langs=["th"])["total"], 0)
        self.assertEqual(state.query_history(query=BURMESE, langs=["my"])["total"], 1)

    def test_when_more_than_the_limit_match_the_newest_are_returned_and_flagged(self):
        state = DashboardState()
        for n in range(30):
            say(state, f"c{n}", f"s{n}", "en", at=1000 + n)
        result = state.query_history(limit=10)
        self.assertEqual((result["total"], result["returned"], result["truncated"]), (30, 10, True))
        self.assertEqual([r["text"] for r in result["chunks"]][0], "s20")
        self.assertFalse(state.query_history(limit=30)["truncated"])

    def test_the_source_language_caption_is_part_of_the_history(self):
        state = DashboardState()
        state.record_original("c1", BURMESE, "my")
        state.add_caption({"lang": "my", "chunk_id": "c1", "text": BURMESE, "worker_id": "source",
                           "translation_backend": "passthrough"})
        self.assertEqual(state.query_history()["chunks"][0]["translations"]["my"]["backend"], "passthrough")

    def test_a_caption_with_no_recorded_original_still_gets_a_row(self):
        state = DashboardState()
        state.add_caption({"lang": "th", "chunk_id": "cli-1", "text": THAI, "timestamp": 5000.0})
        row = state.query_history()["chunks"][0]
        self.assertEqual((row["text"], row["number"], row["created_at"]), (None, None, 5000.0))
        self.assertEqual(row["translations"]["th"]["text"], THAI)

    def test_a_late_translation_is_added_to_the_existing_row(self):
        state = DashboardState()
        say(state, "c1", "hello", "en", th=THAI)
        state.add_caption({"lang": "km", "chunk_id": "c1", "text": "khmer", "timestamp": time.time()})
        self.assertEqual(len(state.query_history()["chunks"]), 1)
        self.assertEqual(set(state.query_history()["chunks"][0]["translations"]), {"th", "km"})

    def test_a_repeated_translation_does_not_overwrite_the_first(self):
        state = DashboardState()
        say(state, "c1", "hello", "en", th="first")
        state.add_caption({"lang": "th", "chunk_id": "c1", "text": "second"})
        self.assertEqual(state.query_history()["chunks"][0]["translations"]["th"]["text"], "first")

    def test_results_are_copies_so_callers_cannot_change_the_history(self):
        state = DashboardState()
        say(state, "c1", "hello", "en", th=THAI)
        state.query_history()["chunks"][0]["translations"]["th"]["text"] = "tampered"
        self.assertEqual(state.query_history()["chunks"][0]["translations"]["th"]["text"], THAI)


class HistoryBoundsTests(unittest.TestCase):
    def test_the_oldest_chunks_are_dropped_beyond_the_count_limit(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_HISTORY_CHUNKS": "20"}):
            state = DashboardState()
        for n in range(35):
            say(state, f"c{n}", f"s{n}", "en", th="x")
        result = state.query_history(limit=100)
        self.assertEqual((result["total"], result["retained"], result["max_retained"]), (20, 20, 20))
        self.assertEqual(result["chunks"][0]["text"], "s15")

    def test_the_default_keeps_two_thousand_chunks(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_HISTORY_CHUNKS": ""}):
            self.assertEqual(DashboardState().query_history()["max_retained"], 2000)

    def test_the_count_limit_has_a_sane_floor(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_HISTORY_CHUNKS": "1"}):
            self.assertEqual(DashboardState().query_history()["max_retained"], 10)

    def test_an_age_limit_drops_old_chunks_the_next_time_something_arrives(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_HISTORY_MINUTES": "30", "POLYGLOT_HISTORY_CHUNKS": ""}):
            state = DashboardState()
        now = time.time()
        say(state, "ancient", "old", "en", at=now - 3600)
        say(state, "recent", "new", "en")
        self.assertEqual([r["text"] for r in state.query_history()["chunks"]], ["new"])

    def test_without_an_age_limit_nothing_expires_by_time(self):
        state = DashboardState()
        say(state, "ancient", "old", "en", at=time.time() - 10 * 86400)
        say(state, "recent", "new", "en")
        self.assertEqual(state.query_history()["total"], 2)

    def test_clearing_captions_clears_the_history_too(self):
        state = DashboardState()
        say(state, "c1", "hello", "en", th=THAI)
        state.clear()
        self.assertEqual(state.query_history()["total"], 0)

    def test_concurrent_writers_and_readers_stay_consistent(self):
        state = DashboardState()
        errors = []

        def writer(offset):
            try:
                for n in range(100):
                    say(state, f"w{offset}-{n}", "x", "en", th="y")
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(100):
                    state.query_history(limit=50)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)] + [threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(state.query_history(limit=1000)["total"], 300)


def rows():
    state = DashboardState()
    say(state, "c1", "Good morning", "en", at=1_700_000_000.0, th=THAI, my=BURMESE)
    say(state, "c2", BURMESE, "my", at=1_700_000_005.0, th="\u0e1a\u0e32\u0e22", en="Goodbye")
    say(state, "c3", "No translations yet", "en", at=1_700_000_030.0)
    return state.query_history()["chunks"]


class ExportFormatTests(unittest.TestCase):
    def test_text_has_a_block_per_chunk_with_original_and_translations(self):
        text = history.to_txt(rows(), ["th", "my"])
        blocks = text.strip().split("\n\n")
        self.assertEqual(len(blocks), 3)
        self.assertIn("#1 (English)", blocks[0])
        self.assertIn("  Good morning", blocks[0])
        self.assertIn(f"  TH: {THAI}", blocks[0])
        self.assertIn("(Burmese)", blocks[1])
        self.assertNotIn("TH:", blocks[2])          # nothing translated yet

    def test_text_does_not_repeat_the_original_as_a_translation_into_its_own_language(self):
        state = DashboardState()
        say(state, "c1", "Hello", "en", en="Hello", th=THAI)         # the passthrough copy in the spoken language
        block = history.to_txt(state.query_history()["chunks"], ["en", "th"])
        self.assertEqual(block.count("Hello"), 1)
        self.assertIn(f"TH: {THAI}", block)

    def test_text_names_a_chunk_without_a_recorded_original(self):
        state = DashboardState()
        state.add_caption({"lang": "th", "chunk_id": "cli", "text": THAI, "timestamp": 1.0})
        self.assertIn("(original not recorded)", history.to_txt(state.query_history()["chunks"], ["th"]))

    def test_an_empty_export_is_an_empty_string(self):
        self.assertEqual(history.to_txt([], ["th"]), "")
        self.assertEqual(history.to_srt([], "th"), "")

    def test_csv_round_trips_commas_quotes_and_newlines_in_every_script(self):
        state = DashboardState()
        tricky = 'He said, "go"\nthen left'
        say(state, "c1", tricky, "en", th=THAI, my=BURMESE)
        parsed = list(csv.reader(io.StringIO(history.to_csv(state.query_history()["chunks"], ["th", "my"]), newline="")))
        self.assertEqual(parsed[0], ["number", "time", "spoken_language", "original", "th", "my"])
        self.assertEqual(parsed[1][3:], [tricky, THAI, BURMESE])
        self.assertEqual(parsed[1][2], "en")

    def test_csv_leaves_untranslated_cells_empty(self):
        parsed = list(csv.reader(io.StringIO(history.to_csv(rows(), ["th"]), newline="")))
        self.assertEqual(parsed[3][4], "")

    def test_json_contains_only_the_chosen_languages(self):
        data = json.loads(history.to_json(rows(), ["th"]))
        self.assertEqual(data["languages"], ["th"])
        self.assertEqual(len(data["chunks"]), 3)
        self.assertEqual(set(data["chunks"][0]["translations"]), {"th"})
        self.assertIn(THAI, history.to_json(rows(), ["th"]))        # not \\u-escaped

    def test_srt_times_cues_from_the_first_chunk_and_skips_chunks_without_the_language(self):
        srt = history.to_srt(rows(), "th")
        self.assertEqual(srt.count("\n\n"), 1)         # two cues for th (c1, c2), c3 has none
        first, second = srt.strip().split("\n\n")
        self.assertEqual(first.splitlines()[:2], ["1", "00:00:00,000 --> 00:00:05,000"])
        self.assertEqual(first.splitlines()[2], THAI)
        self.assertEqual(second.splitlines()[:2], ["2", "00:00:05,000 --> 00:00:09,000"])   # the last cue stays 4 s

    def test_a_burst_of_chunks_becomes_readable_non_overlapping_cues(self):
        # e.g. an uploaded file: every chunk is created within the same few milliseconds
        state = DashboardState()
        for n in range(4):
            say(state, f"c{n}", "x", "en", at=500.0 + n * 0.001, th="a sentence of a reasonable length here")
        cues = history.to_srt(state.query_history()["chunks"], "th").strip().split("\n\n")
        spans = []
        for cue in cues:
            start, end = cue.splitlines()[1].split(" --> ")
            to_s = lambda t: int(t[:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8]) + int(t[9:]) / 1000
            spans.append((to_s(start), to_s(end)))
        for (start, end), (next_start, _) in zip(spans, spans[1:]):
            self.assertGreaterEqual(end - start, 1.5)           # long enough to read
            self.assertLessEqual(end, next_start + 1e-9)        # never overlaps the next cue
        self.assertEqual(spans[0][0], 0.0)

    def test_longer_text_stays_up_longer_but_never_past_the_cap(self):
        state = DashboardState()
        say(state, "a", "x", "en", at=0.0, th="word " * 40)       # 200 characters
        say(state, "b", "y", "en", at=0.1, th="short")
        first = history.to_srt(state.query_history()["chunks"], "th").split("\n\n")[0]
        self.assertIn("00:00:00,000 --> 00:00:08,000", first)

    def test_srt_caps_how_long_a_cue_stays_on_screen(self):
        state = DashboardState()
        say(state, "a", "x", "en", at=100.0, th="one")
        say(state, "b", "y", "en", at=160.0, th="two")      # a minute later
        self.assertIn("00:00:00,000 --> 00:00:08,000", history.to_srt(state.query_history()["chunks"], "th"))

    def test_srt_time_format(self):
        self.assertEqual(history.srt_time(0), "00:00:00,000")
        self.assertEqual(history.srt_time(3661.5), "01:01:01,500")
        self.assertEqual(history.srt_time(-3), "00:00:00,000")

    def test_unknown_spoken_languages_get_a_readable_name(self):
        self.assertEqual(history.language_name("auto"), "auto-detected")
        self.assertEqual(history.language_name(None), "auto-detected")
        self.assertEqual(history.language_name("th"), "Thai")


class HistoryRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = DashboardRuntime()
        DashboardHandler.runtime = cls.runtime
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.runtime.stop()

    def setUp(self):
        self.runtime.state.clear()
        state = self.runtime.state
        now = time.time()
        say(state, "c1", "Good morning", "en", at=now - 600, th=THAI, my=BURMESE)
        say(state, "c2", "Thank you", "en", at=now - 30, th="\u0e02\u0e2d\u0e1a\u0e04\u0e38\u0e13", my="\u1000\u103b\u1031\u1038\u1007\u1030\u1038")

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_history_returns_json_for_the_whole_session_by_default(self):
        status, headers, body = self.get("/api/history")
        data = json.loads(body)
        self.assertEqual((status, data["total"], len(data["chunks"])), (200, 2, 2))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("server_time", data)

    def test_history_can_be_limited_to_the_last_few_minutes(self):
        data = json.loads(self.get("/api/history?last_minutes=2")[2])
        self.assertEqual([c["text"] for c in data["chunks"]], ["Thank you"])

    def test_history_accepts_an_explicit_time_range_search_and_languages(self):
        now = time.time()
        data = json.loads(self.get(f"/api/history?since={now - 700}&until={now - 300}&langs=th&q=morning")[2])
        self.assertEqual(data["total"], 1)
        self.assertEqual(set(data["chunks"][0]["translations"]), {"th"})

    def test_history_search_works_with_non_latin_text_in_the_url(self):
        query = urllib.parse.quote(BURMESE)
        self.assertEqual(json.loads(self.get(f"/api/history?q={query}")[2])["total"], 1)

    def test_bad_history_parameters_are_a_400_with_the_reason(self):
        for query, fragment in (("since=yesterday", "since must be a number"), ("last_minutes=-5", "greater than 0"),
                                ("last_minutes=nan", "finite"), ("until=inf", "finite"),
                                ("langs=th,xx", "Unknown language"), ("q=" + "a" * 201, "200 characters")):
            status, _, body = self.get("/api/history?" + query)
            self.assertEqual(status, 400, query)
            self.assertIn(fragment, json.loads(body)["error"], query)

    def test_limit_is_clamped_to_a_safe_range(self):
        self.assertEqual(json.loads(self.get("/api/history?limit=0")[2])["returned"], 1)
        self.assertEqual(json.loads(self.get("/api/history?limit=999999")[2])["returned"], 2)
        self.assertEqual(self.get("/api/history?limit=abc")[0], 400)

    def test_history_is_readable_without_the_access_token_like_the_live_captions(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_ACCESS_TOKEN": "s3cret"}):
            self.assertEqual(self.get("/api/history")[0], 200)
            self.assertEqual(self.get("/api/export?format=txt")[0], 200)

    def test_text_export_is_a_download_with_a_dated_filename(self):
        status, headers, body = self.get("/api/export?format=txt&langs=th")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertRegex(headers["Content-Disposition"], r'^attachment; filename="polyglot-captions-\d{8}-\d{6}\.txt"$')
        self.assertIn(THAI, body.decode("utf-8"))
        self.assertNotIn(BURMESE, body.decode("utf-8"))

    def test_export_defaults_to_text_and_all_languages(self):
        text = self.get("/api/export")[2].decode("utf-8")
        self.assertIn(THAI, text)
        self.assertIn(BURMESE, text)

    def test_csv_export_starts_with_a_byte_order_mark_so_excel_reads_the_scripts(self):
        body = self.get("/api/export?format=csv&langs=th,my")[2]
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        self.assertIn(BURMESE, body.decode("utf-8-sig"))

    def test_json_and_srt_exports(self):
        data = json.loads(self.get("/api/export?format=json&langs=th")[2])
        self.assertEqual(len(data["chunks"]), 2)
        status, headers, body = self.get("/api/export?format=srt&lang=th")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Disposition"].endswith('.srt"'))
        self.assertIn("-->", body.decode("utf-8"))

    def test_srt_uses_the_first_chosen_language_when_none_is_named(self):
        self.assertIn(BURMESE, self.get("/api/export?format=srt&langs=my,th")[2].decode("utf-8"))

    def test_an_export_covers_only_the_requested_time_range(self):
        text = self.get("/api/export?format=txt&last_minutes=2")[2].decode("utf-8")
        self.assertIn("Thank you", text)
        self.assertNotIn("Good morning", text)

    def test_exporting_an_empty_range_is_a_404_with_a_message_not_an_empty_file(self):
        status, _, body = self.get("/api/export?format=txt&since=1&until=2")
        self.assertEqual(status, 404)
        self.assertIn("No captions", json.loads(body)["error"])
        status, _, body = self.get("/api/export?format=srt&lang=km")
        self.assertEqual(status, 404)
        self.assertIn("KM", json.loads(body)["error"])

    def test_export_validates_the_format_and_language(self):
        self.assertEqual(self.get("/api/export?format=pdf")[0], 400)
        self.assertEqual(self.get("/api/export?format=srt&lang=zz")[0], 400)

    def test_clearing_over_http_empties_the_history_and_the_exports(self):
        request = urllib.request.Request(self.base + "/api/clear", data=b"", method="POST")
        urllib.request.urlopen(request, timeout=5).read()
        self.assertEqual(json.loads(self.get("/api/history")[2])["total"], 0)
        self.assertEqual(self.get("/api/export?format=txt")[0], 404)


class StatusPayloadTests(unittest.TestCase):
    def test_the_live_status_stays_small_however_long_the_history_grows(self):
        runtime = DashboardRuntime()
        for n in range(600):
            say(runtime.state, f"c{n}", f"sentence number {n}", "en", th="x" * 80, my="y" * 80)
        snapshot = runtime.state.snapshot()
        self.assertEqual(len(snapshot["captions"]["th"]), 50)
        self.assertNotIn("history", snapshot)
        self.assertLess(len(json.dumps(snapshot)), 400_000)
        runtime.stop()


if __name__ == "__main__":
    unittest.main()
