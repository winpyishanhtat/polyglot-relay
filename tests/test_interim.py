"""Interim captions (POLYGLOT_INTERIM=1): the words of a phrase still being spoken, pushed to the room."""

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import web_ui
from web_ui import DashboardHandler, DashboardRuntime, LiveBacklogError


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class InterimRuntimeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": "", "POLYGLOT_INTERIM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = DashboardRuntime()
        self.runtime.producer = mock.Mock()
        self.addCleanup(self.runtime.stop)

    def interim_events(self, state):
        return [data for _, kind, data in state.events_after(0) if kind == "interim"]

    def test_the_words_so_far_are_pushed_to_the_room_and_not_kept(self):
        with mock.patch.object(self.runtime, "_transcribe_blob", return_value=iter([("good morning every", "en")])):
            self.runtime.interim(b"audio", "en", room="hall-a")
            hall = self.runtime.room("hall-a")
            self.assertTrue(wait_for(lambda: self.interim_events(hall)))
        event = self.interim_events(hall)[0]
        self.assertEqual((event["text"], event["lang"]), ("good morning every", "en"))
        self.assertEqual(len(hall.history), 0)                                 # a preview, not a caption
        self.runtime.producer.send.assert_not_called()                          # and never translated
        self.assertEqual(self.interim_events(self.runtime.state), [])           # other rooms do not see it

    def test_it_is_off_unless_switched_on(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_INTERIM": ""}), self.assertRaisesRegex(ValueError, "switched off"):
            self.runtime.interim(b"audio", "en")

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError):
            self.runtime.interim(b"", "en")
        with self.assertRaises(ValueError):
            self.runtime.interim(b"audio", "xx")

    def test_finished_speech_comes_first(self):
        self.runtime.live_queue.put_nowait((0, b"slice", "en"))
        with self.assertRaises(LiveBacklogError):
            self.runtime.interim(b"audio", "en")

    def test_only_one_interim_runs_at_a_time(self):
        started = threading.Event()
        release = threading.Event()

        def slow(*args, **kwargs):
            started.set()
            release.wait(2)
            return iter([("x", "en")])
        with mock.patch.object(self.runtime, "_transcribe_blob", side_effect=slow):
            self.runtime.interim(b"audio", "en")
            self.assertTrue(started.wait(2))
            with self.assertRaises(LiveBacklogError):
                self.runtime.interim(b"audio", "en")
            release.set()
            self.assertTrue(wait_for(lambda: self.runtime._interim_lock.acquire(blocking=False)))
        self.runtime._interim_lock.release()

    def test_a_failed_transcription_publishes_nothing_and_frees_the_slot(self):
        with mock.patch.object(self.runtime, "_transcribe_blob", side_effect=RuntimeError("down")):
            self.runtime.interim(b"audio", "en")
            self.assertTrue(wait_for(lambda: self.runtime._interim_lock.acquire(blocking=False)))
        self.runtime._interim_lock.release()
        self.assertEqual(self.interim_events(self.runtime.state), [])

    def test_the_page_is_told_whether_interim_captions_are_on(self):
        with mock.patch.object(self.runtime, "broker_status", return_value=[]):
            self.assertTrue(self.runtime.status()["settings"]["interim"])
            with mock.patch.dict(os.environ, {"POLYGLOT_INTERIM": "0"}):
                self.assertFalse(self.runtime.status()["settings"]["interim"])


class InterimHttpTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": "", "POLYGLOT_INTERIM": "1",
                                               "POLYGLOT_ACCESS_TOKEN": "", "POLYGLOT_VIEW_TOKEN": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = DashboardRuntime()
        self.runtime.producer = mock.Mock()
        DashboardHandler.runtime = self.runtime
        DashboardHandler.limiter = web_ui.RequestRateLimiter()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.runtime.stop)

    def post(self, path, body=b"audio"):
        request = urllib.request.Request(self.base + path, data=body, method="POST", headers={"Content-Type": "audio/webm"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_an_interim_is_accepted_for_the_room_in_the_address(self):
        with mock.patch.object(self.runtime, "interim") as interim:
            self.assertEqual(self.post("/api/live/interim?source_lang=th&room=hall-a")[0], 202)
        interim.assert_called_once_with(b"audio", "th", room="hall-a")

    def test_a_busy_relay_answers_429_so_the_page_simply_skips(self):
        with mock.patch.object(self.runtime, "interim", side_effect=LiveBacklogError("busy")):
            status, body = self.post("/api/live/interim?source_lang=th")
        self.assertEqual(status, 429)
        self.assertTrue(body["dropped"])

    def test_switched_off_is_a_400_with_the_reason(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_INTERIM": "0"}):
            status, body = self.post("/api/live/interim?source_lang=th")
        self.assertEqual(status, 400)
        self.assertIn("POLYGLOT_INTERIM", body["error"])


if __name__ == "__main__":
    unittest.main()
