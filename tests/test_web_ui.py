import json
import threading
import time
import unittest

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from web_ui import MAX_AUDIO_UPLOAD_BYTES, DashboardHandler, DashboardState


class DashboardTests(unittest.TestCase):
    def test_caption_state_round_trip(self):
        state = DashboardState()
        state.add_caption({"lang": "th", "chunk_id": "1", "text": "สวัสดี",
                           "timestamp": 1.0, "worker_id": "th-1"})
        snapshot = state.snapshot()
        self.assertEqual(snapshot["captions"]["th"][0]["text"], "สวัสดี")
        state.clear()
        self.assertEqual(state.snapshot()["captions"]["th"], [])

    def test_caption_carries_the_original_pre_translation_text(self):
        state = DashboardState()
        state.record_original("1", "Good morning", "en")
        state.add_caption({"lang": "th", "chunk_id": "1", "text": "สวัสดี",
                           "timestamp": 1.0, "worker_id": "th-1"})
        caption = state.snapshot()["captions"]["th"][0]
        self.assertEqual(caption["original_text"], "Good morning")
        self.assertEqual(caption["original_source_lang"], "en")

    def test_caption_without_a_recorded_original_gets_none(self):
        # A chunk_id that was never passed to record_original (e.g. one
        # sent by the CLI producer rather than the dashboard) must not
        # crash caption rendering - just no original text to show.
        state = DashboardState()
        state.add_caption({"lang": "th", "chunk_id": "unknown-chunk", "text": "สวัสดี",
                           "timestamp": 1.0, "worker_id": "th-1"})
        caption = state.snapshot()["captions"]["th"][0]
        self.assertIsNone(caption["original_text"])
        self.assertIsNone(caption["original_source_lang"])

    def test_originals_are_bounded_and_evict_oldest_first(self):
        state = DashboardState()
        limit = state._originals_order.maxlen
        for i in range(limit + 5):
            state.record_original(f"chunk-{i}", f"text {i}", "en")
        self.assertEqual(len(state.originals), limit)
        self.assertNotIn("chunk-0", state.originals)
        self.assertIn(f"chunk-{limit + 4}", state.originals)

    def test_health_and_dashboard_are_served(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                self.assertEqual(json.load(response), {"status": "ok"})
            with urllib.request.urlopen(base + "/", timeout=2) as response:
                self.assertIn(b"Polyglot Relay", response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_languages_endpoint_matches_config(self):
        from common import config

        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/api/languages", timeout=2) as response:
                payload = json.load(response)
            codes = [entry["code"] for entry in payload["languages"]]
            self.assertEqual(codes, config.TARGET_LANGUAGES)
            self.assertEqual(len(codes), 9)
            self.assertEqual(payload["languages"][0]["name"], "English")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_submit_validates_source_lang(self):
        # Regression coverage for the "any language to multiple languages"
        # feature: /api/captions now accepts an explicit source_lang, which
        # must be either "auto" or one of the registered language codes -
        # anything else should be rejected before it ever reaches the relay.
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        with self.assertRaises(ValueError):
            runtime.submit("hello", "not-a-real-language")
        runtime.submit("hello", "th")  # a real code is accepted
        runtime.submit("hello", "auto")  # explicit auto-detect is accepted
        runtime.submit("hello")  # omitted entirely is accepted (defaults through)
        self.assertEqual(runtime.state.pending, 3)


class AudioUploadTests(unittest.TestCase):
    """Covers the dashboard's browser upload/recording input: transcription
    happens in-process (mocked here so the suite doesn't need a real Whisper
    install) and only the resulting text ever reaches submit() - never the
    broker/worker wire protocol's audio_pcm_b64 field.
    """

    def test_successful_upload_submits_one_caption_per_transcribed_chunk(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        fake_chunks = [("hello there", b"pcm-1", "en"), ("how are you", b"pcm-2", "en")]
        with mock.patch("web_ui.transcribe_file", return_value=iter(fake_chunks)) as transcribe:
            count, detected = runtime.submit_audio(b"fake-mp3-bytes", "th")
        transcribe.assert_called_once()
        self.assertEqual(count, 2)
        # source_lang was explicit ("th"), so Whisper's own detection isn't
        # what's actually driving the pipeline here - nothing to report.
        self.assertIsNone(detected)
        self.assertEqual(runtime.state.pending, 2)

    def test_detected_language_is_surfaced_when_dropdown_was_auto(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        fake_chunks = [("sawasdee", b"pcm-1", "th")]
        with mock.patch("web_ui.transcribe_file", return_value=iter(fake_chunks)):
            count, detected = runtime.submit_audio(b"fake-mp3-bytes", "auto")
        self.assertEqual(count, 1)
        self.assertEqual(detected, "th")

    def test_whisper_detected_language_is_used_when_dropdown_left_on_auto(self):
        # "Auto-detect" shouldn't mean every downstream worker also has to
        # guess independently - Whisper's own per-chunk detection should
        # flow through as the caption's source_lang, unlocking passthrough
        # for the matching worker and the known-source Gemini prompt for
        # everyone else.
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        fake_chunks = [("sawasdee", b"pcm-1", "th")]
        with mock.patch("web_ui.transcribe_file", return_value=iter(fake_chunks)), \
             mock.patch.object(DashboardRuntime, "submit") as submit_mock:
            runtime.submit_audio(b"fake-mp3-bytes", "auto")
        submit_mock.assert_called_once_with(mock.ANY, "th")

    def test_explicit_dropdown_pick_wins_over_whisper_detection(self):
        # An explicit "Speaking in" choice is the user overriding Whisper,
        # not a hint for it to second-guess - if Whisper's own detection
        # disagrees, the explicit pick still wins (matches the language
        # hint already forced into the transcription itself).
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        fake_chunks = [("hello", b"pcm-1", "en")]
        with mock.patch("web_ui.transcribe_file", return_value=iter(fake_chunks)), \
             mock.patch.object(DashboardRuntime, "submit") as submit_mock:
            runtime.submit_audio(b"fake-mp3-bytes", "th")
        submit_mock.assert_called_once_with(mock.ANY, "th")

    def test_unrecognized_detected_language_falls_back_to_auto(self):
        # Whisper's detected code isn't guaranteed to be one of the 9
        # target languages (it recognizes far more) - an unrecognized code
        # must not be passed through as source_lang, which submit() would
        # reject outright.
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        fake_chunks = [("bonjour", b"pcm-1", "fr")]
        with mock.patch("web_ui.transcribe_file", return_value=iter(fake_chunks)), \
             mock.patch.object(DashboardRuntime, "submit") as submit_mock:
            runtime.submit_audio(b"fake-mp3-bytes", "auto")
        submit_mock.assert_called_once_with(mock.ANY, "auto")

    def test_concrete_source_lang_is_passed_to_whisper_as_a_language_hint(self):
        # A "Speaking in" selection other than auto-detect must reach
        # Whisper itself, not just tag the resulting captions - telling it
        # the language up front skips its own detection pass.
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        with mock.patch("web_ui.transcribe_file", return_value=iter([])) as transcribe:
            with self.assertRaises(ValueError):
                runtime.submit_audio(b"fake-mp3-bytes", "th")
        transcribe.assert_called_once_with(mock.ANY, mock.ANY, language="th", on_unrecognized=mock.ANY)

    def test_auto_source_lang_passes_no_language_hint(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        with mock.patch("web_ui.transcribe_file", return_value=iter([])) as transcribe:
            with self.assertRaises(ValueError):
                runtime.submit_audio(b"fake-mp3-bytes", "auto")
        transcribe.assert_called_once_with(mock.ANY, mock.ANY, language=None, on_unrecognized=mock.ANY)

    def test_empty_upload_rejected_before_transcription_is_attempted(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        with mock.patch("web_ui.transcribe_file") as transcribe:
            with self.assertRaises(ValueError):
                runtime.submit_audio(b"", "th")
        transcribe.assert_not_called()

    def test_silent_upload_with_no_detected_speech_raises(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        with mock.patch("web_ui.transcribe_file", return_value=iter([])):
            with self.assertRaises(ValueError):
                runtime.submit_audio(b"fake-silent-bytes", "auto")

    def test_missing_whisper_install_surfaces_as_value_error_not_system_exit(self):
        # _load_whisper (inside transcribe_file) raises SystemExit when the
        # whisper package isn't installed - that must not be allowed to
        # propagate out of a request handler and kill the thread.
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()

        def _raise(*_args, **_kwargs):
            raise SystemExit("Whisper is not installed")
            yield  # pragma: no cover - makes this a generator, never reached

        with mock.patch("web_ui.transcribe_file", side_effect=_raise):
            with self.assertRaises(ValueError):
                runtime.submit_audio(b"fake-bytes", "auto")

    def test_oversized_upload_is_rejected_by_content_length_check_alone(self):
        # The size cap must be enforced from the Content-Length header before
        # any body bytes are read, so a handler can reject a too-large
        # upload without buffering it first.
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.headers = {"Content-Length": str(MAX_AUDIO_UPLOAD_BYTES + 1)}
        with self.assertRaises(ValueError):
            handler._raw_body(MAX_AUDIO_UPLOAD_BYTES)


class LiveStreamingTests(unittest.TestCase):
    """Live mic streaming: the browser POSTs short self-contained audio
    slices; a single worker transcribes them in order into the normal chunk
    pipeline. Whisper is mocked, as in AudioUploadTests."""

    def _runtime_with_consumer(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        thread = threading.Thread(target=runtime._live_loop, daemon=True)
        thread.start()
        return runtime, thread

    def test_slices_are_transcribed_in_order_and_silence_is_not_an_error(self):
        runtime, thread = self._runtime_with_consumer()
        results = iter([iter([("first", b"", "th")]), iter([]), iter([("third", b"", "th")])])
        submitted = []
        try:
            with mock.patch("web_ui.transcribe_file", side_effect=lambda *a, **k: next(results)),                  mock.patch.object(type(runtime), "submit", lambda self, text, lang=None: submitted.append((text, lang))):
                for index in range(3):
                    runtime.enqueue_live_slice(f"slice-{index}".encode(), "th")
                    runtime.live_queue.join()  # keep arrival order unambiguous
        finally:
            runtime.stop_event.set()
            thread.join(timeout=2)
        self.assertEqual(submitted, [("first", "th"), ("third", "th")])
        self.assertEqual(runtime.state.live["slices"], 3)
        self.assertEqual(runtime.state.live["chunks"], 2)

    def test_a_failing_slice_does_not_stop_later_slices(self):
        runtime, thread = self._runtime_with_consumer()
        outcomes = [RuntimeError("bad slice"), iter([("recovered", b"", "en")])]
        submitted = []

        def fake_transcribe(*_args, **_kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        try:
            with mock.patch("web_ui.transcribe_file", side_effect=fake_transcribe),                  mock.patch.object(type(runtime), "submit", lambda self, text, lang=None: submitted.append(text)):
                runtime.enqueue_live_slice(b"one", "en")
                runtime.live_queue.join()
                runtime.enqueue_live_slice(b"two", "en")
                runtime.live_queue.join()
        finally:
            runtime.stop_event.set()
            thread.join(timeout=2)
        self.assertEqual(submitted, ["recovered"])

    def test_saturated_transcriber_rejects_instead_of_queueing_without_bound(self):
        from web_ui import LIVE_QUEUE_SLICES, DashboardRuntime, LiveBacklogError

        runtime = DashboardRuntime()  # no consumer running: the queue only fills
        for _ in range(LIVE_QUEUE_SLICES):
            runtime.enqueue_live_slice(b"audio", "th")
        with self.assertRaises(LiveBacklogError):
            runtime.enqueue_live_slice(b"audio", "th")
        self.assertEqual(runtime.state.live["dropped"], 1)
        self.assertEqual(runtime.live_queue.qsize(), LIVE_QUEUE_SLICES)

    def test_live_slice_requires_audio_and_a_concrete_language(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        for data, lang in [(b"", "th"), (b"audio", "auto"), (b"audio", None), (b"audio", "xx")]:
            with self.assertRaises(ValueError):
                runtime.enqueue_live_slice(data, lang)
        self.assertEqual(runtime.live_queue.qsize(), 0)

    def test_http_route_status_codes(self):
        from web_ui import LIVE_QUEUE_SLICES, DashboardRuntime

        runtime = DashboardRuntime()
        previous = getattr(DashboardHandler, "runtime", None)
        DashboardHandler.runtime = runtime
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path, body):
            request = urllib.request.Request(base + path, data=body, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        try:
            for _ in range(LIVE_QUEUE_SLICES):
                status, payload = post("/api/live/slice?source_lang=my", b"audio")
                self.assertEqual(status, 202)
                self.assertTrue(payload["accepted"])
            status, payload = post("/api/live/slice?source_lang=my", b"audio")
            self.assertEqual(status, 429)
            self.assertTrue(payload["dropped"])
            self.assertEqual(post("/api/live/slice?source_lang=auto", b"audio")[0], 400)
            self.assertEqual(post("/api/live/slice?source_lang=my", b"")[0], 400)
            with urllib.request.urlopen(base + "/api/status", timeout=3) as response:
                live = json.load(response)["live"]
            self.assertEqual(live["dropped"], 1)
            self.assertEqual(live["queued"], LIVE_QUEUE_SLICES)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if previous is not None:
                DashboardHandler.runtime = previous

    def test_oversized_live_slice_is_rejected_from_content_length_alone(self):
        from web_ui import MAX_LIVE_SLICE_BYTES

        handler = DashboardHandler.__new__(DashboardHandler)
        handler.headers = {"Content-Length": str(MAX_LIVE_SLICE_BYTES + 1)}
        with self.assertRaises(ValueError):
            handler._raw_body(MAX_LIVE_SLICE_BYTES)


class LiveParallelOrderingTests(unittest.TestCase):
    """Slices are transcribed in parallel (Gemini is network-bound) but their
    captions must still reach the pipeline in the order they were spoken."""

    def _start(self, workers):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        runtime.start_live_workers(workers)
        submitted = []
        runtime.submit = lambda text, lang=None: submitted.append(text)  # per instance, never class-wide
        self.addCleanup(runtime.stop_event.set)
        return runtime, submitted

    @staticmethod
    def _wait(condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_slices_run_in_parallel_but_captions_stay_in_speech_order(self):
        delays = {b"s0": 0.9, b"s1": 0.3, b"s2": 0.5}  # distinct, so the finish order is deterministic
        finished = []

        def fake_transcribe(path, *_args, **_kwargs):
            name = path.read_bytes()
            time.sleep(delays[name])
            finished.append(name)
            return iter([(name.decode(), b"", "my")])

        runtime, submitted = self._start(3)
        started = time.monotonic()
        with mock.patch("web_ui.transcribe_file", side_effect=fake_transcribe):
            for name in (b"s0", b"s1", b"s2"):
                runtime.enqueue_live_slice(name, "my")
            self.assertTrue(self._wait(lambda: len(submitted) == 3))
        elapsed = time.monotonic() - started
        self.assertEqual(finished, [b"s1", b"s2", b"s0"])    # finished out of order...
        self.assertEqual(submitted, ["s0", "s1", "s2"])      # ...released in speech order
        self.assertLess(elapsed, 1.4)                        # sequential would take ~1.7s

    def _max_concurrency(self, language):
        active, peak, lock = [0], [0], threading.Lock()

        def fake_transcribe(path, *_args, **_kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.25)
            with lock:
                active[0] -= 1
            return iter([("text", b"", language)])

        runtime, submitted = self._start(3)
        with mock.patch("web_ui.transcribe_file", side_effect=fake_transcribe):
            for index in range(4):
                runtime.enqueue_live_slice(f"s{index}".encode(), language)
                time.sleep(0.05)  # a browser sends one slice every few seconds, not four at once
            self.assertTrue(self._wait(lambda: len(submitted) == 4))
        return peak[0]

    def test_gemini_only_languages_transcribe_in_parallel(self):
        self.assertGreaterEqual(self._max_concurrency("my"), 2)

    def test_languages_known_to_gemini_transcribe_in_parallel_by_default(self):
        for language in ("vi", "th", "en"):
            self.assertGreaterEqual(self._max_concurrency(language), 2, language)

    def test_whisper_languages_stay_serialized_when_gemini_is_switched_off_for_them(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_GEMINI_STT_LANGUAGES": "none"}):
            self.assertEqual(self._max_concurrency("th"), 1)

    def test_a_stuck_slice_is_skipped_so_later_captions_still_flow(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def fake_transcribe(path, *_args, **_kwargs):
            name = path.read_bytes()
            if name == b"s0":
                release.wait(5)
            return iter([(name.decode(), b"", "my")])

        runtime, submitted = self._start(3)
        with mock.patch("web_ui.LIVE_MAX_HOLD_SECONDS", 0.4), \
                mock.patch("web_ui.transcribe_file", side_effect=fake_transcribe):
            for name in (b"s0", b"s1", b"s2"):
                runtime.enqueue_live_slice(name, "my")
            self.assertTrue(self._wait(lambda: submitted == ["s1", "s2"], 4), submitted)
            self.assertEqual(runtime.state.live["late"], 1)
            release.set()                                    # the stuck slice finally completes...
            self.assertTrue(self._wait(lambda: not runtime._live_late))
        self.assertEqual(submitted, ["s1", "s2"])           # ...and its stale caption is discarded

    def test_unrecognized_slices_are_counted_as_gaps_not_errors(self):
        def fake_transcribe(path, *_args, on_unrecognized=None, **_kwargs):
            on_unrecognized()
            return iter([])

        runtime, submitted = self._start(1)
        with mock.patch("web_ui.transcribe_file", side_effect=fake_transcribe):
            runtime.enqueue_live_slice(b"s0", "my")
            self.assertTrue(self._wait(lambda: runtime.state.live["slices"] == 1))
        self.assertEqual(runtime.state.live["unrecognized"], 1)
        self.assertEqual(runtime.state.live["chunks"], 0)
        self.assertEqual(submitted, [])

    def test_upload_where_every_window_failed_says_gemini_was_unavailable(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()

        def fake_transcribe(path, *_args, on_unrecognized=None, **_kwargs):
            on_unrecognized()
            on_unrecognized()
            return iter([])

        with mock.patch("web_ui.transcribe_file", side_effect=fake_transcribe):
            with self.assertRaises(ValueError) as caught:
                runtime.submit_audio(b"media", "my")
        self.assertIn("Gemini transcription was unavailable", str(caught.exception))


class EnglishPivotDashboardTests(unittest.TestCase):
    """A Burmese/Khmer/Lao caption is sent in two stages: the Gemini-only
    languages at once, and English plus the LibreTranslate languages once ONE
    shared English rendering is ready (or without it if that fails or is slow).
    Nothing here may block or break sending."""

    def _runtime(self, *, real=True):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        runtime._cluster_is_real = lambda: real
        runtime.producer = mock.Mock()
        self.addCleanup(runtime._pivot_pool.shutdown, False)
        self.addCleanup(runtime.stop_event.set)
        return runtime

    @staticmethod
    def _with_translator(runtime, translate):
        translator = mock.Mock()
        translator.translate.side_effect = translate
        runtime._pivot_translator = translator
        return translator

    @staticmethod
    def _wait(condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def _send(self, runtime, text="ဟာ", lang="my"):
        runtime.outbox.put((text, lang))
        thread = threading.Thread(target=runtime._send_loop, daemon=True)
        thread.start()
        return thread

    # ---- when the pivot applies ------------------------------------------
    def test_applies_only_to_sources_libretranslate_cannot_read(self):
        runtime = self._runtime()
        self.assertTrue(all(runtime._pivot_applies(lang) for lang in ("my", "km", "lo")))
        self.assertFalse(any(runtime._pivot_applies(lang) for lang in ("en", "th", "id", "auto", None)))

    def test_does_not_apply_against_a_mock_cluster(self):
        self.assertFalse(self._runtime(real=False)._pivot_applies("my"))

    def test_can_be_switched_off(self):
        runtime = self._runtime()
        with mock.patch.dict("os.environ", {"POLYGLOT_PIVOT": "0"}):
            self.assertFalse(runtime._pivot_applies("my"))

    def test_an_unreachable_broker_check_does_not_break_sending(self):
        runtime = self._runtime()
        runtime._cluster_is_real = mock.Mock(side_effect=OSError("no broker"))
        self.assertFalse(runtime._pivot_applies("my"))

    # ---- the pivot call itself ---------------------------------------------
    def test_pivot_returns_stripped_english(self):
        runtime = self._runtime()
        translator = self._with_translator(runtime, lambda text, source_lang=None: (" I am about to eat ", "gemini-text"))
        self.assertEqual(runtime._pivot_to_english("ဟာ", "my"), "I am about to eat")
        translator.translate.assert_called_once_with("ဟာ", source_lang="my")

    def test_a_failing_pivot_degrades_to_none_instead_of_raising(self):
        runtime = self._runtime()

        def boom(text, source_lang=None):
            raise RuntimeError("503 UNAVAILABLE")

        self._with_translator(runtime, boom)
        self.assertIsNone(runtime._pivot_to_english("ဟာ", "my"))
        self.assertTrue(any("pivot unavailable" in a["message"] for a in runtime.state.activity))

    def test_a_slow_pivot_is_cut_off_at_the_timeout(self):
        runtime = self._runtime()
        release = threading.Event()
        self.addCleanup(release.set)
        self._with_translator(runtime, lambda text, source_lang=None: (release.wait(5) and "late", "gemini-text"))
        with mock.patch("web_ui.PIVOT_TIMEOUT_SECONDS", 0.2):
            started = time.monotonic()
            self.assertIsNone(runtime._pivot_to_english("ဟာ", "my"))
        self.assertLess(time.monotonic() - started, 1.5)

    # ---- the two-stage send ----------------------------------------------
    def test_stage_one_goes_out_first_and_stage_two_carries_the_pivot(self):
        from common import config

        runtime = self._runtime()
        self._with_translator(runtime, lambda text, source_lang=None: ("I am about to eat", "gemini-text"))
        self._send(runtime)
        self.assertTrue(self._wait(lambda: runtime.producer.send.call_count == 2))
        first, second = [call.kwargs for call in runtime.producer.send.call_args_list]
        self.assertEqual(first["targets"], ["km", "lo", "my"])
        self.assertNotIn("pivot_en", first)
        self.assertEqual(second["targets"], [lang for lang in config.TARGET_LANGUAGES if lang not in ("my", "km", "lo")])
        self.assertEqual(second["pivot_en"], "I am about to eat")
        # together the two stages cover every language exactly once
        self.assertEqual(sorted(first["targets"] + second["targets"]), sorted(config.TARGET_LANGUAGES))

    def test_a_slow_pivot_never_delays_stage_one(self):
        runtime = self._runtime()
        release = threading.Event()
        self.addCleanup(release.set)
        self._with_translator(runtime, lambda text, source_lang=None: (release.wait(5) and "late", "gemini-text"))
        with mock.patch("web_ui.PIVOT_TIMEOUT_SECONDS", 0.4):
            started = time.monotonic()
            self._send(runtime)
            self.assertTrue(self._wait(lambda: runtime.producer.send.call_count >= 1, 1.0))
            self.assertLess(time.monotonic() - started, 0.35)  # stage one did not wait for the pivot
            self.assertEqual(runtime.producer.send.call_args_list[0].kwargs["targets"], ["km", "lo", "my"])
            self.assertTrue(self._wait(lambda: runtime.producer.send.call_count == 2))
        second = runtime.producer.send.call_args_list[1].kwargs
        self.assertIsNone(second["pivot_en"])               # timed out: workers translate directly
        self.assertIn("en", second["targets"])               # ...and the second stage is still sent

    def test_a_failing_pivot_still_sends_stage_two_without_it(self):
        runtime = self._runtime()

        def boom(text, source_lang=None):
            raise RuntimeError("down")

        self._with_translator(runtime, boom)
        self._send(runtime)
        self.assertTrue(self._wait(lambda: runtime.producer.send.call_count == 2))
        self.assertIsNone(runtime.producer.send.call_args_list[1].kwargs["pivot_en"])

    def test_other_sources_are_sent_once_to_everyone(self):
        runtime = self._runtime()
        self._send(runtime, "hello", "th")
        self.assertTrue(self._wait(lambda: runtime.producer.send.call_count == 1))
        time.sleep(0.2)
        self.assertEqual(runtime.producer.send.call_count, 1)
        self.assertNotIn("targets", runtime.producer.send.call_args.kwargs)


class EmptyEnvironmentVariableTests(unittest.TestCase):
    """Docker Compose passes unset ${VAR:-} settings as empty strings; every
    numeric setting must treat "" as "use the default", not crash at import."""

    def test_dashboard_imports_with_every_numeric_setting_empty(self):
        import os
        import subprocess
        import sys

        names = ["POLYGLOT_PIVOT_TIMEOUT", "POLYGLOT_LIVE_WORKERS", "POLYGLOT_LIVE_MAX_HOLD_SECONDS",
                 "POLYGLOT_LIVE_SLICE_SECONDS", "GEMINI_MAX_RETRIES", "GEMINI_STT_MAX_RETRIES"]
        env = {**os.environ, **{name: "" for name in names}}
        code = ("import web_ui, translator_worker; "
                "t = translator_worker.Translator('th'); "
                "print(web_ui.PIVOT_TIMEOUT_SECONDS, web_ui.LIVE_WORKERS, web_ui.LIVE_MAX_HOLD_SECONDS, t.max_retries)")
        done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                              cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["25.0", "3", "20.0", "5"])

    def test_gemini_transcription_retries_accept_an_empty_setting(self):
        from stt_service import _transcribe_window_via_gemini

        generate = mock.Mock(side_effect=Exception("503 UNAVAILABLE"))
        fake_genai = mock.MagicMock()
        fake_genai.Client.return_value.models.generate_content = generate
        modules = {"google": mock.MagicMock(genai=fake_genai), "google.genai": fake_genai,
                   "google.genai.types": mock.MagicMock()}
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k", "GEMINI_STT_MAX_RETRIES": ""}, clear=True),              mock.patch.dict("sys.modules", modules),              mock.patch("stt_service._pcm_window_to_wav_bytes", return_value=b"wav"),              mock.patch("stt_service.time.sleep"):
            self.assertIsNone(_transcribe_window_via_gemini(object(), 16000, "my"))
        self.assertEqual(generate.call_count, 3)  # the default of 2 retries


if __name__ == "__main__":
    unittest.main()
