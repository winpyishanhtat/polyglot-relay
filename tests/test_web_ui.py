import json
import threading
import unittest
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
        transcribe.assert_called_once_with(mock.ANY, mock.ANY, language="th")

    def test_auto_source_lang_passes_no_language_hint(self):
        from web_ui import DashboardRuntime

        runtime = DashboardRuntime()
        with mock.patch("web_ui.transcribe_file", return_value=iter([])) as transcribe:
            with self.assertRaises(ValueError):
                runtime.submit_audio(b"fake-mp3-bytes", "auto")
        transcribe.assert_called_once_with(mock.ANY, mock.ANY, language=None)

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


if __name__ == "__main__":
    unittest.main()
