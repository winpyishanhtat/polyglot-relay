"""Features around the dashboard: history kept on disk, the viewing token, listener pages and read-aloud."""

import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import history
import tts
import web_ui
from web_ui import DashboardHandler, DashboardRuntime, DashboardState

THAI = "สวัสดี"


def say(state, chunk_id, text, **translations):
    state.record_original(chunk_id, text, "en")
    for lang, translated in translations.items():
        state.add_caption({"lang": lang, "chunk_id": chunk_id, "text": translated, "timestamp": 1.0})


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "nested", "history.jsonl")

    def row(self, n, **extra):
        return {"chunk_id": f"c{n}", "number": n, "created_at": 100.0 + n, "text": f"t{n}", "source_lang": "en",
                "translations": {}, **extra}

    def test_a_missing_file_is_an_empty_history(self):
        self.assertEqual(len(history.HistoryStore(self.path, 10).load()), 0)

    def test_rows_saved_come_back_in_order_with_non_ascii_text_intact(self):
        store = history.HistoryStore(self.path, 10)
        store.save(self.row(1, translations={"th": {"text": THAI}}))
        store.save(self.row(2))
        loaded = history.HistoryStore(self.path, 10).load()
        self.assertEqual(list(loaded), ["c1", "c2"])
        self.assertEqual(loaded["c1"]["translations"]["th"]["text"], THAI)
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn(THAI, handle.read())      # stored as text, not \u escapes

    def test_a_later_line_for_a_chunk_replaces_the_earlier_one(self):
        store = history.HistoryStore(self.path, 10)
        store.save(self.row(1))
        store.save(self.row(1, translations={"th": {"text": "x"}}))
        loaded = history.HistoryStore(self.path, 10).load()
        self.assertEqual(len(loaded), 1)
        self.assertIn("th", loaded["c1"]["translations"])

    def test_only_the_newest_rows_up_to_the_limit_are_loaded(self):
        store = history.HistoryStore(self.path, 3)
        for n in range(1, 7):
            store.save(self.row(n))
        self.assertEqual(list(history.HistoryStore(self.path, 3).load()), ["c4", "c5", "c6"])

    def test_damaged_lines_are_skipped_not_fatal(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self.row(1)) + "\n")
            handle.write("{not json\n\n[]\n")
            handle.write(json.dumps({"chunk_id": "x"}) + "\n")                       # missing fields
            handle.write(json.dumps({**self.row(2), "translations": []}) + "\n")      # wrong type
            handle.write(json.dumps(self.row(3)) + "\n")
            handle.write('{"chunk_id": "c4", "creat')                                 # torn by a crash
        self.assertEqual(list(history.HistoryStore(self.path, 10).load()), ["c1", "c3"])

    def test_the_file_is_compacted_once_it_holds_many_more_lines_than_rows(self):
        store = history.HistoryStore(self.path, 5)
        rows = {}
        for n in range(60):
            rows["c1"] = self.row(1, translations={"th": {"text": str(n)}})
            store.save(rows["c1"], rows.values())
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertLess(len(lines), 5 * history.HistoryStore.COMPACT_FACTOR + 2)
        self.assertEqual(history.HistoryStore(self.path, 5).load()["c1"]["translations"]["th"]["text"], "59")

    def test_wipe_empties_the_file(self):
        store = history.HistoryStore(self.path, 10)
        store.save(self.row(1))
        store.wipe()
        self.assertEqual(len(history.HistoryStore(self.path, 10).load()), 0)

    def test_a_disk_that_cannot_be_written_is_reported_once_and_never_raises(self):
        blocked = os.path.join(self.dir.name, "file")
        open(blocked, "w").close()
        log = mock.Mock()
        store = history.HistoryStore(os.path.join(blocked, "history.jsonl"), 10, log)     # a file where a directory is needed
        for n in range(3):
            store.save(self.row(n))
        store.wipe()
        self.assertEqual(log.warning.call_count, 1)


class PersistedStateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "history.jsonl")
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": self.path})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_the_setting_nothing_is_written(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": ""}):
            state = DashboardState()
            say(state, "a", "hello", th=THAI)
        self.assertFalse(os.path.exists(self.path))

    def test_what_was_said_survives_a_restart_and_numbering_continues(self):
        state = DashboardState()
        say(state, "a", "hello", th=THAI)
        say(state, "b", "again")
        restarted = DashboardState()
        found = restarted.query_history()["chunks"]
        self.assertEqual([row["text"] for row in found], ["hello", "again"])
        self.assertEqual(found[0]["translations"]["th"]["text"], THAI)
        restarted.record_original("c", "third", "en")
        self.assertEqual(restarted.history["c"]["number"], 3)

    def test_a_translation_that_arrives_later_is_saved_too(self):
        state = DashboardState()
        state.record_original("a", "hello", "en")
        state.add_caption({"lang": "th", "chunk_id": "a", "text": THAI, "timestamp": 1.0})
        self.assertIn("th", DashboardState().history["a"]["translations"])

    def test_clearing_forgets_the_saved_history_too(self):
        state = DashboardState()
        say(state, "a", "hello")
        state.clear()
        self.assertEqual(len(DashboardState().history), 0)

    def test_the_restored_history_obeys_the_row_limit(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_CHUNKS": "10"}):
            state = DashboardState()
            for n in range(25):
                say(state, f"c{n}", f"text {n}")
            self.assertEqual(len(DashboardState().history), 10)


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.runtime = DashboardRuntime()
        self.runtime.producer = mock.Mock()
        DashboardHandler.runtime = self.runtime
        DashboardHandler.limiter = web_ui.RequestRateLimiter()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.runtime.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_ACCESS_TOKEN": "", "POLYGLOT_VIEW_TOKEN": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, path, data=None, method=None, headers=None):
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def speak(self, headers=None):
        return self.call("/api/tts", json.dumps({"text": "hello", "lang": "th"}).encode(), "POST", headers)


class ViewTokenTests(WebTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_VIEW_TOKEN": "look-only"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_a_viewing_token_everything_can_be_read(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_VIEW_TOKEN": ""}):
            for path in ("/", "/api/status", "/api/history", "/listen", "/listen/th", "/app.js"):
                self.assertEqual(self.call(path)[0], 200, path)

    def test_every_page_and_api_read_is_refused_without_the_token(self):
        for path in ("/", "/app.js", "/listen", "/listen/th", "/api/status", "/api/history", "/api/captions",
                     "/api/languages", "/api/export?format=txt"):
            self.assertEqual(self.call(path)[0], 401, path)

    def test_the_refusal_to_a_person_is_a_page_and_to_a_program_is_json(self):
        status, headers, body = self.call("/")
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"private", body)
        self.assertIn("token", json.loads(self.call("/api/status")[2])["error"])

    def test_the_health_check_stays_open(self):
        self.assertEqual(self.call("/health")[0], 200)

    def test_a_wrong_token_is_refused_and_sets_no_cookie(self):
        for wrong in ("", "look", "look-only ", "LOOK-ONLY", "look-onlyx"):
            status, headers, _ = self.call("/?token=" + urllib.parse.quote(wrong))
            self.assertEqual(status, 401, repr(wrong))
            self.assertIsNone(headers["Set-Cookie"])

    def test_the_right_token_in_the_address_lets_you_in_and_is_remembered_in_a_cookie(self):
        status, headers, _ = self.call("/listen/th?token=look-only")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("polyglot_view=look-only", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertEqual(self.call("/api/status", headers={"Cookie": "polyglot_view=look-only"})[0], 200)
        self.assertEqual(self.call("/api/status", headers={"Cookie": "other=1; polyglot_view=look-only"})[0], 200)
        self.assertEqual(self.call("/api/status", headers={"Cookie": "polyglot_view=nope"})[0], 401)

    def test_a_token_with_awkward_characters_survives_the_cookie(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_VIEW_TOKEN": "a b;c=d"}):
            status, headers, _ = self.call("/?token=a%20b%3Bc%3Dd")
            self.assertEqual(status, 200)
            cookie = headers["Set-Cookie"].split(";")[0]
            self.assertEqual(self.call("/api/status", headers={"Cookie": cookie})[0], 200)

    def test_a_program_can_send_the_token_in_a_header(self):
        self.assertEqual(self.call("/api/status", headers={"X-View-Token": "look-only"})[0], 200)
        self.assertEqual(self.call("/api/status", headers={"X-View-Token": "wrong"})[0], 401)

    def test_the_access_token_also_opens_the_room_for_whoever_runs_it(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_ACCESS_TOKEN": "operator"}):
            self.assertEqual(self.call("/?token=operator")[0], 200)
            self.assertEqual(self.call("/api/status", headers={"X-Access-Token": "operator"})[0], 200)

    def test_when_only_a_viewing_token_is_set_changes_need_it_too(self):
        body = json.dumps({"text": "hello", "source_lang": "en"}).encode()
        self.assertEqual(self.call("/api/captions", body, "POST")[0], 401)
        self.assertEqual(self.call("/api/clear", b"", "POST")[0], 401)
        self.assertEqual(self.call("/api/captions", body, "POST", {"Cookie": "polyglot_view=look-only"})[0], 202)

    def test_a_viewer_cannot_change_anything_when_an_access_token_is_also_set(self):
        body = json.dumps({"text": "hello", "source_lang": "en"}).encode()
        with mock.patch.dict(os.environ, {"POLYGLOT_ACCESS_TOKEN": "operator"}):
            self.assertEqual(self.call("/api/captions", body, "POST", {"Cookie": "polyglot_view=look-only"})[0], 401)
            self.assertEqual(self.call("/api/captions", body, "POST", {"X-Access-Token": "operator"})[0], 202)

    def test_a_refused_viewer_does_not_use_up_the_posting_allowance(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_RATE_LIMIT_PER_MINUTE": "2"}):
            body = json.dumps({"text": "hello", "source_lang": "en"}).encode()
            for _ in range(6):
                self.assertEqual(self.call("/api/captions", body, "POST")[0], 401)
            self.assertEqual(self.call("/api/captions", body, "POST", {"Cookie": "polyglot_view=look-only"})[0], 202)


class ListenerPageTests(WebTestCase):
    def test_the_picker_and_every_language_page_are_served(self):
        for path in ("/listen", "/listen/", "/listen/th", "/listen/my", "/listen/km?token=x"):
            status, headers, body = self.call(path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn(b"listen-view", body)

    def test_an_unknown_language_is_not_found(self):
        for path in ("/listen/xx", "/listen/th/extra", "/listen/../app.js", "/listen/TH"):
            self.assertEqual(self.call(path)[0], 404, path)

    def test_the_scripts_the_page_needs_are_served(self):
        for path, marker in (("/speech.js", b"createSpeaker"), ("/listen.js", b"mergeRows")):
            status, headers, body = self.call(path)
            self.assertEqual(status, 200)
            self.assertIn("javascript", headers["Content-Type"])
            self.assertIn(marker, body)

    def test_the_dashboard_loads_the_speech_helper_and_links_to_the_listener_pages(self):
        page = self.call("/")[2].decode("utf-8")
        self.assertIn('src="/speech.js"', page)
        self.assertIn('href="/listen"', page)

    def test_the_history_the_page_reads_holds_the_translation_for_its_language(self):
        say(self.runtime.state, "a", "hello", th=THAI)
        rows = json.loads(self.call("/api/history?langs=th&last_minutes=30&limit=80")[2])["chunks"]
        self.assertEqual(rows[0]["translations"]["th"]["text"], THAI)


def _wav(rate=24000, frames=100):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x01\x00" * frames)
    return buffer.getvalue()


class SpeechEndpointTests(WebTestCase):
    def test_it_returns_a_wav_for_the_text(self):
        with mock.patch.object(tts.SYNTHESIZER, "synthesize", return_value=_wav()) as synth:
            status, headers, body = self.speak()
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/wav"))
        self.assertEqual(body[:4], b"RIFF")
        synth.assert_called_once_with("hello", "th")

    def test_a_failure_is_reported_with_its_own_status_and_message(self):
        for status in (400, 429, 502, 503):
            with mock.patch.object(tts.SYNTHESIZER, "synthesize", side_effect=tts.TtsError("nope", status)):
                code, _, body = self.speak()
            self.assertEqual((code, json.loads(body)["error"]), (status, "nope"))

    def test_without_any_token_set_it_is_open(self):
        with mock.patch.object(tts.SYNTHESIZER, "synthesize", return_value=_wav()):
            self.assertEqual(self.speak()[0], 200)

    def test_with_an_access_token_it_needs_that_token(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_ACCESS_TOKEN": "operator"}), \
                mock.patch.object(tts.SYNTHESIZER, "synthesize", return_value=_wav()) as synth:
            self.assertEqual(self.speak()[0], 401)
            self.assertEqual(self.speak({"X-Access-Token": "wrong"})[0], 401)
            self.assertEqual(self.speak({"X-Access-Token": "operator"})[0], 200)
        self.assertEqual(synth.call_count, 1)

    def test_a_listener_with_the_viewing_token_may_ask_for_speech(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_VIEW_TOKEN": "look-only", "POLYGLOT_ACCESS_TOKEN": "operator"}), \
                mock.patch.object(tts.SYNTHESIZER, "synthesize", return_value=_wav()):
            self.assertEqual(self.speak()[0], 401)
            self.assertEqual(self.speak({"Cookie": "polyglot_view=look-only"})[0], 200)
            self.assertEqual(self.speak({"X-View-Token": "look-only"})[0], 200)

    def test_it_is_rate_limited_like_every_other_post(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_RATE_LIMIT_PER_MINUTE": "2"}), \
                mock.patch.object(tts.SYNTHESIZER, "synthesize", return_value=_wav()):
            self.assertEqual([self.speak()[0] for _ in range(3)], [200, 200, 429])

    def test_nonsense_in_the_body_is_a_400_not_a_crash(self):
        self.assertEqual(self.call("/api/tts", b"{broken", "POST")[0], 400)
        code, _, body = self.call("/api/tts", json.dumps({"text": "", "lang": "th"}).encode(), "POST")
        self.assertEqual(code, 400)
        code, _, body = self.call("/api/tts", json.dumps({"text": "hi", "lang": "zz"}).encode(), "POST")
        self.assertEqual(code, 400)


# --- the speech module, with a stand-in for the Gemini SDK -------------------------------------------

class FakeModels:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self.behaviour(model, config, len(self.calls))


def audio_response(data):
    part = types.SimpleNamespace(inline_data=types.SimpleNamespace(data=data))
    return types.SimpleNamespace(candidates=[types.SimpleNamespace(content=types.SimpleNamespace(parts=[part]))])


class FakeSdk:
    """Installs fake `google.genai` modules; `behaviour(model, config, call_number)` plays the service."""

    def __init__(self, behaviour):
        self.models = FakeModels(behaviour)
        models = self.models

        class Client:
            def __init__(self, **kwargs):
                self.models = models

        class Plain:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        self.genai = types.ModuleType("google.genai")
        self.genai.Client = Client
        self.types = types.ModuleType("google.genai.types")
        for name in ("SpeechConfig", "VoiceConfig", "PrebuiltVoiceConfig", "GenerateContentConfig"):
            setattr(self.types, name, Plain)
        self.genai.types = self.types
        self.google = types.ModuleType("google")
        self.google.genai = self.genai

    def __enter__(self):
        self.patch = mock.patch.dict(sys.modules, {"google": self.google, "google.genai": self.genai,
                                                   "google.genai.types": self.types})
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class SpeechSynthesizerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_TTS_MODEL": "", "GEMINI_TTS_VOICE": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.synth = tts.SpeechSynthesizer()

    def test_raw_pcm_becomes_a_playable_wav(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00" * 480)):
            wav = self.synth.synthesize("hello", "en")
        with wave.open(io.BytesIO(wav)) as handle:
            self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()),
                             (1, 2, tts.SAMPLE_RATE, 480))

    def test_a_ready_made_wav_is_passed_through_untouched(self):
        ready = _wav(rate=16000)
        with FakeSdk(lambda m, c, n: audio_response(ready)):
            self.assertEqual(self.synth.synthesize("hello", "en"), ready)

    def test_the_same_phrase_twice_costs_one_call_and_whitespace_does_not_matter(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00" * 10)) as sdk:
            first = self.synth.synthesize("hello   world", "th")
            second = self.synth.synthesize("hello world\n", "th")
            self.synth.synthesize("hello world", "en")           # another language is another clip
        self.assertEqual(first, second)
        self.assertEqual(len(sdk.models.calls), 2)

    def test_the_cache_is_bounded(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")), mock.patch.object(tts, "CACHE_ENTRIES", 3):
            for n in range(6):
                self.synth.synthesize(f"phrase {n}", "en")
        self.assertEqual(len(self.synth._cache), 3)

    def test_the_text_language_and_key_are_checked_before_any_call(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")) as sdk:
            for text, lang, status in (("", "en", 400), ("   ", "en", 400), ("x" * 501, "en", 400), ("hi", "zz", 400)):
                with self.assertRaises(tts.TtsError) as caught:
                    self.synth.synthesize(text, lang)
                self.assertEqual(caught.exception.status, status)
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}), \
                    self.assertRaises(tts.TtsError) as caught:
                self.synth.synthesize("hello", "en")
            self.assertEqual(caught.exception.status, 503)
        self.assertEqual(sdk.models.calls, [])

    def test_the_language_tag_is_sent_and_dropped_when_the_model_refuses_it(self):
        def behaviour(model, config, n):
            if hasattr(config.speech_config, "language_code"):
                raise RuntimeError("400 INVALID_ARGUMENT: language_code is not supported")
            return audio_response(b"\x01\x00" * 5)
        with FakeSdk(behaviour) as sdk:
            self.synth.synthesize("hello", "th")
        tags = [getattr(call["config"].speech_config, "language_code", None) for call in sdk.models.calls]
        self.assertEqual(tags, ["th-TH", None])

    def test_a_model_that_gives_no_audio_is_followed_by_the_fallback_model(self):
        def behaviour(model, config, n):
            return audio_response(b"") if model == tts.DEFAULT_MODEL else audio_response(b"\x01\x00" * 5)
        with FakeSdk(behaviour) as sdk:
            self.synth.synthesize("hello", "en")
        self.assertEqual([c["model"] for c in sdk.models.calls], [tts.DEFAULT_MODEL, tts.FALLBACK_MODEL])

    def test_a_rate_limit_on_every_model_is_reported_as_busy(self):
        def behaviour(model, config, n):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: slow down")
        with FakeSdk(behaviour), self.assertRaises(tts.TtsError) as caught:
            self.synth.synthesize("hello", "en")
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("busy", str(caught.exception))

    def test_a_daily_quota_is_reported_as_such(self):
        def behaviour(model, config, n):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota_metric GenerateRequestsPerDayPerProjectPerModel exceeded")
        with FakeSdk(behaviour), self.assertRaises(tts.TtsError) as caught:
            self.synth.synthesize("hello", "en")
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("daily", str(caught.exception))

    def test_a_response_with_no_audio_anywhere_is_a_502(self):
        for response in (types.SimpleNamespace(candidates=[]), types.SimpleNamespace(candidates=None), audio_response(b"")):
            with FakeSdk(lambda m, c, n, r=response: r), self.assertRaises(tts.TtsError) as caught:
                self.synth.synthesize("hello", "en")
            self.assertEqual(caught.exception.status, 502)

    def test_a_failure_is_not_cached(self):
        replies = [RuntimeError("500 boom"), RuntimeError("500 boom"), audio_response(b"\x01\x00" * 4)]

        def behaviour(model, config, n):
            reply = replies[min(n - 1, 2)]
            if isinstance(reply, Exception):
                raise reply
            return reply
        with FakeSdk(behaviour):
            with self.assertRaises(tts.TtsError):
                self.synth.synthesize("hello", "en")
            self.assertEqual(self.synth.synthesize("hello", "en")[:4], b"RIFF")

    def test_a_missing_sdk_is_a_503(self):
        with mock.patch.dict(sys.modules, {"google": None, "google.genai": None}), self.assertRaises(tts.TtsError) as caught:
            self.synth.synthesize("hello", "en")
        self.assertEqual(caught.exception.status, 503)

    def test_no_more_than_the_allowed_number_of_calls_run_at_once(self):
        running, peak, lock = [0], [0], threading.Lock()

        def behaviour(model, config, n):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            threading.Event().wait(0.05)
            with lock:
                running[0] -= 1
            return audio_response(b"\x01\x00")
        synth = tts.SpeechSynthesizer(concurrency=2)
        with FakeSdk(behaviour):
            threads = [threading.Thread(target=synth.synthesize, args=(f"phrase {n}", "en")) for n in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertLessEqual(peak[0], 2)

    def test_the_voice_and_model_can_be_chosen_from_the_environment(self):
        with mock.patch.dict(os.environ, {"GEMINI_TTS_MODEL": "my-model", "GEMINI_TTS_VOICE": "Puck"}), \
                FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")) as sdk:
            self.synth.synthesize("hello", "en")
        call = sdk.models.calls[0]
        self.assertEqual(call["model"], "my-model")
        self.assertEqual(call["config"].speech_config.voice_config.prebuilt_voice_config.voice_name, "Puck")


if __name__ == "__main__":
    unittest.main()
