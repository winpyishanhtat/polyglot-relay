"""Dashboard server behaviour beyond uploads and live slicing: state, input
validation, the caption sender and listeners, broker status, every HTTP route
and the slicing limits handed to the browser."""

import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import web_ui
from common import config
from common.protocol import encode, recv_one
from web_ui import DashboardHandler, DashboardRuntime, DashboardState, live_slicing_settings


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class LiveSlicingSettingsTests(unittest.TestCase):
    def settings(self, **env):
        with mock.patch.dict("os.environ", env):
            return live_slicing_settings()

    def test_defaults(self):
        for name in ("POLYGLOT_LIVE_MIN_SLICE_SECONDS", "POLYGLOT_LIVE_MAX_SLICE_SECONDS", "POLYGLOT_LIVE_PAUSE_MS"):
            self.assertNotIn(name, __import__("os").environ)
        self.assertEqual(self.settings(), {"min_ms": 2000, "max_ms": 8000, "pause_ms": 350})

    def test_empty_values_mean_the_default_as_compose_passes_them(self):
        self.assertEqual(self.settings(POLYGLOT_LIVE_MIN_SLICE_SECONDS="", POLYGLOT_LIVE_MAX_SLICE_SECONDS="",
                                       POLYGLOT_LIVE_PAUSE_MS=""), {"min_ms": 2000, "max_ms": 8000, "pause_ms": 350})

    def test_values_are_clamped_to_safe_ranges(self):
        low = self.settings(POLYGLOT_LIVE_MIN_SLICE_SECONDS="0", POLYGLOT_LIVE_MAX_SLICE_SECONDS="1", POLYGLOT_LIVE_PAUSE_MS="1")
        self.assertEqual((low["min_ms"], low["pause_ms"]), (1000, 150))
        high = self.settings(POLYGLOT_LIVE_MIN_SLICE_SECONDS="99", POLYGLOT_LIVE_MAX_SLICE_SECONDS="99", POLYGLOT_LIVE_PAUSE_MS="99999")
        self.assertEqual((high["min_ms"], high["pause_ms"]), (5000, 1000))
        self.assertLessEqual(high["max_ms"], 15000)

    def test_max_is_always_longer_than_min(self):
        both = self.settings(POLYGLOT_LIVE_MIN_SLICE_SECONDS="5", POLYGLOT_LIVE_MAX_SLICE_SECONDS="4")
        self.assertGreater(both["max_ms"], both["min_ms"])


class DashboardStateExtraTests(unittest.TestCase):
    def test_a_caption_for_the_same_chunk_is_not_added_twice(self):
        state = DashboardState()
        message = {"lang": "th", "chunk_id": "1", "text": "x", "timestamp": 1.0}
        state.add_caption(message)
        state.add_caption({**message, "text": "changed"})
        captions = state.snapshot()["captions"]["th"]
        self.assertEqual([c["text"] for c in captions], ["x"])

    def test_newest_caption_comes_first_and_history_is_bounded(self):
        state = DashboardState()
        for n in range(60):
            state.add_caption({"lang": "th", "chunk_id": str(n), "text": str(n), "timestamp": n})
        captions = state.snapshot()["captions"]["th"]
        self.assertEqual(len(captions), 50)
        self.assertEqual(captions[0]["text"], "59")

    def test_a_translation_marks_the_chunk_target_completed(self):
        state = DashboardState()
        state.record_original("c1", "hello", "en")
        state.add_caption({"lang": "th", "chunk_id": "c1", "text": "x", "worker_id": "th-1"})
        state.add_caption({"lang": "en", "chunk_id": "c1", "text": "hello"})
        targets = state.chunks["c1"]["targets"]
        self.assertEqual(targets["th"], {"status": "completed", "worker_id": "th-1"})
        self.assertEqual(targets["en"]["status"], "source")

    def test_chunks_are_numbered_in_submission_order(self):
        state = DashboardState()
        for name in "abc":
            state.record_original(name, name, "en")
        self.assertEqual([state.chunks[n]["number"] for n in "abc"], [1, 2, 3])

    def test_a_new_chunk_starts_awaiting_the_broker_except_for_its_source_language(self):
        state = DashboardState()
        state.record_original("c1", "hi", "th")
        statuses = {lang: t["status"] for lang, t in state.chunks["c1"]["targets"].items()}
        self.assertEqual(statuses["th"], "source")
        self.assertEqual({s for lang, s in statuses.items() if lang != "th"}, {"awaiting_broker"})

    def test_worker_status_logs_only_real_changes(self):
        state = DashboardState()
        state.worker_status("th", True, "th-1")
        state.worker_status("th", True, "th-1")
        state.worker_status("th", False)
        messages = [a["message"] for a in state.snapshot()["activity"]]
        self.assertEqual(messages, ["TH listener reconnecting", "TH listener connected"])
        self.assertEqual(state.workers["th"], {"connected": False, "worker_id": None})

    def test_clear_empties_captions_but_not_worker_status(self):
        state = DashboardState()
        state.worker_status("th", True, "th-1")
        state.add_caption({"lang": "th", "chunk_id": "1", "text": "x"})
        state.clear()
        self.assertEqual(state.snapshot()["captions"]["th"], [])
        self.assertTrue(state.workers["th"]["connected"])

    def test_activity_log_is_bounded_newest_first(self):
        state = DashboardState()
        for n in range(60):
            state.event(f"e{n}")
        activity = state.snapshot()["activity"]
        self.assertEqual((len(activity), activity[0]["message"]), (40, "e59"))

    def test_snapshot_is_a_copy(self):
        state = DashboardState()
        state.snapshot()["workers"]["th"]["connected"] = True
        self.assertFalse(state.workers["th"]["connected"])


class SubmitValidationTests(unittest.TestCase):
    def setUp(self):
        self.runtime = DashboardRuntime()
        self.addCleanup(self.runtime.stop)

    def test_text_is_whitespace_normalised_and_queued(self):
        self.runtime.submit("  hello \n  world  ", "en")
        self.assertEqual(self.runtime.outbox.get_nowait(), ("hello world", "en"))
        self.assertEqual(self.runtime.state.pending, 1)

    def test_empty_and_oversized_captions_are_rejected(self):
        for text in ("", "   \n ", "x" * 2001):
            with self.assertRaises(ValueError, msg=text[:10]):
                self.runtime.submit(text)
        self.assertEqual(self.runtime.state.pending, 0)

    def test_exactly_2000_characters_are_accepted(self):
        self.runtime.submit("x" * 2000)

    def test_auto_and_unset_source_languages_are_accepted(self):
        self.runtime.submit("hello", "auto")
        self.runtime.submit("hello", None)
        self.assertEqual(self.runtime.state.pending, 2)

    def test_the_hundred_job_limit_blocks_more_submissions(self):
        self.runtime.state.pending = 100
        with self.assertRaisesRegex(ValueError, "still processing"):
            self.runtime.submit("one more")

    def test_unfinished_chunks_count_towards_the_limit_but_finished_ones_do_not(self):
        state = self.runtime.state
        for n in range(100):
            state.record_original(f"c{n}", "x", "en")      # all targets awaiting the broker
        with self.assertRaises(ValueError):
            self.runtime.submit("blocked")
        for chunk in state.chunks.values():
            for target in chunk["targets"].values():
                target["status"] = "completed"
        self.runtime.submit("allowed now")

    def test_demo_queues_every_demo_sentence_as_english(self):
        self.runtime.run_demo()
        queued = [self.runtime.outbox.get_nowait() for _ in range(len(web_ui.DEMO_SENTENCES))]
        self.assertEqual(queued, [(s, "en") for s in web_ui.DEMO_SENTENCES])


class SendLoopTests(unittest.TestCase):
    def test_queued_captions_are_recorded_and_sent_with_their_source_language(self):
        runtime = DashboardRuntime()
        runtime.producer = mock.Mock()
        with mock.patch.object(runtime, "_pivot_applies", return_value=False):
            thread = threading.Thread(target=runtime._send_loop, daemon=True)
            thread.start()
            runtime.state.pending = 1
            runtime.outbox.put(("hello", "en"))
            self.assertTrue(wait_for(lambda: runtime.producer.send.called))
        runtime.stop_event.set()
        thread.join(2)
        text, chunk_id = runtime.producer.send.call_args.args[:2]
        self.assertEqual(text, "hello")
        self.assertEqual(runtime.producer.send.call_args.kwargs["source_lang"], "en")
        self.assertRegex(chunk_id, r"^ui-\d+-1$")
        self.assertEqual(runtime.state.originals[chunk_id]["text"], "hello")
        self.assertTrue(wait_for(lambda: runtime.state.pending == 0))
        # the source language shows its own text straight away
        self.assertEqual(runtime.state.snapshot()["captions"]["en"][0]["translation_backend"], "passthrough")

    def test_a_send_failure_is_reported_and_does_not_kill_the_loop(self):
        runtime = DashboardRuntime()
        runtime.producer = mock.Mock()
        runtime.producer.send.side_effect = [RuntimeError("broker down"), None]
        with mock.patch.object(runtime, "_pivot_applies", return_value=False):
            thread = threading.Thread(target=runtime._send_loop, daemon=True)
            thread.start()
            runtime.state.pending = 2
            runtime.outbox.put(("first", "en"))
            runtime.outbox.put(("second", "en"))
            self.assertTrue(wait_for(lambda: runtime.producer.send.call_count == 2))
        runtime.stop_event.set()
        thread.join(2)
        messages = [a["message"] for a in runtime.state.snapshot()["activity"]]
        self.assertTrue(any("Caption failed" in m for m in messages))
        self.assertTrue(wait_for(lambda: runtime.state.pending == 0))


class FakeWorker:
    """A worker's listener endpoint: sends scripted frames to whoever connects."""

    def __init__(self, frames):
        self.frames, self.connections = frames, 0
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            for frame in self.frames:
                conn.sendall(encode(frame))
            time.sleep(0.05)
            conn.close()

    def close(self):
        self.stop.set()
        self.server.close()


class ListenerWatchTests(unittest.TestCase):
    def test_translated_chunks_from_a_worker_become_captions_and_the_listener_reconnects(self):
        worker = FakeWorker([{"type": "heartbeat"},
                             {"type": "translated_chunk", "lang": "th", "chunk_id": "a", "text": "one"}])
        self.addCleanup(worker.close)
        runtime = DashboardRuntime()
        endpoint = {"worker_id": "th-1", "host": "127.0.0.1", "port": worker.port}
        with mock.patch.object(web_ui, "discover_worker", return_value=endpoint), \
                mock.patch.object(config, "RECONNECT_DELAY", 0.05):
            thread = threading.Thread(target=runtime._watch_language, args=("th",), daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: runtime.state.snapshot()["captions"]["th"]))
            self.assertTrue(wait_for(lambda: worker.connections >= 2), "listener did not reconnect")
            runtime.stop_event.set()
            thread.join(3)
        self.assertEqual(runtime.state.snapshot()["captions"]["th"][0]["text"], "one")

    def test_with_no_worker_available_the_language_is_shown_as_disconnected(self):
        runtime = DashboardRuntime()
        runtime.state.worker_status("th", True, "th-1")
        with mock.patch.object(web_ui, "discover_worker", return_value=None), \
                mock.patch.object(config, "RECONNECT_DELAY", 0.02):
            thread = threading.Thread(target=runtime._watch_language, args=("th",), daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: not runtime.state.workers["th"]["connected"]))
            runtime.stop_event.set()
            thread.join(2)


class BrokerStatusTests(unittest.TestCase):
    def fake_broker(self, reply):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(0.2)
        stop = threading.Event()

        def run():
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except (socket.timeout, OSError):
                    continue
                with conn:
                    try:
                        recv_one(conn)
                        conn.sendall(encode(reply))
                    except OSError:
                        pass
        threading.Thread(target=run, daemon=True).start()
        self.addCleanup(lambda: (stop.set(), server.close()))
        return server.getsockname()[1]

    def runtime_with(self, *ports):
        brokers = [{"id": i + 1, "host": "127.0.0.1", "port": port} for i, port in enumerate(ports)]
        patcher = mock.patch.object(config, "BROKERS", brokers)
        patcher.start()
        self.addCleanup(patcher.stop)
        timeout = mock.patch.object(config, "CONNECT_TIMEOUT", 0.3)
        timeout.start()
        self.addCleanup(timeout.stop)
        return DashboardRuntime()

    def dead_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_reports_online_leader_and_telemetry_per_broker(self):
        leader = self.fake_broker({"type": "heartbeat_ack", "is_leader": True, "leader_id": 1,
                                   "members": [{"worker_id": "th-1"}], "jobs": [{"chunk_id": "c"}]})
        runtime = self.runtime_with(leader, self.dead_port())
        first, second = runtime.broker_status()
        self.assertEqual((first["online"], first["leader"], first["leader_id"]), (True, True, 1))
        self.assertEqual(first["members"][0]["worker_id"], "th-1")
        self.assertEqual((second["online"], second["leader"]), (False, False))

    def test_status_includes_brokers_and_settings(self):
        leader = self.fake_broker({"type": "heartbeat_ack", "is_leader": True, "leader_id": 1})
        runtime = self.runtime_with(leader)
        with mock.patch.dict("os.environ", {"GEMINI_TIER": "paid", "POLYGLOT_FILE_CHUNK_SECONDS": "20"}):
            status = runtime.status()
        self.assertEqual(status["brokers"][0]["leader"], True)
        self.assertEqual(status["settings"]["tier"], "paid")
        self.assertEqual(status["settings"]["chunk_seconds"], 20.0)
        self.assertIn("pause_ms", status["settings"]["live_slicing"])
        self.assertIn("captions", status)

    def test_broker_job_updates_replace_unfinished_chunk_targets_only(self):
        jobs = [{"chunk_id": "c1", "lang": "th", "status": "translating", "worker_id": "th-1"},
                {"chunk_id": "c1", "lang": "km", "status": "queued", "worker_id": "km-1"}]
        leader = self.fake_broker({"type": "heartbeat_ack", "is_leader": True, "leader_id": 1, "jobs": jobs})
        runtime = self.runtime_with(leader)
        runtime.state.record_original("c1", "hello", "en")
        runtime.state.add_caption({"lang": "km", "chunk_id": "c1", "text": "done", "worker_id": "km-1"})
        status = runtime.status()
        targets = {c["chunk_id"]: c["targets"] for c in status["chunks"]}["c1"]
        self.assertEqual(targets["th"]["status"], "translating")
        self.assertEqual(targets["km"]["status"], "completed")   # a finished caption is never regressed

    def test_a_leader_change_interrupts_unfinished_work_and_says_so(self):
        runtime = self.runtime_with(self.fake_broker({"type": "heartbeat_ack", "is_leader": True, "leader_id": 2}))
        runtime.state.record_original("c1", "hello", "en")
        runtime.state.add_caption({"lang": "th", "chunk_id": "c1", "text": "x"})
        runtime._last_leader = 1
        # broker id 1 in config is the fake above -> make it report id 2 as leader id
        with mock.patch.object(runtime, "broker_status", return_value=[{"id": 2, "online": True, "leader": True, "jobs": []}]):
            status = runtime.status()
        targets = status["chunks"][0]["targets"]
        self.assertEqual(targets["km"]["status"], "interrupted")
        self.assertEqual(targets["th"]["status"], "completed")
        self.assertTrue(any("Leader changed to broker 2" in a["message"] for a in status["activity"]))

    def test_no_leader_means_no_interruption_marking(self):
        runtime = self.runtime_with(self.dead_port())
        runtime.state.record_original("c1", "hello", "en")
        runtime._last_leader = 1
        status = runtime.status()
        self.assertEqual({t["status"] for lang, t in status["chunks"][0]["targets"].items() if lang != "en"},
                         {"awaiting_broker"})


class HttpRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = DashboardRuntime()
        cls.runtime.producer = mock.Mock()
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
        # Drain anything earlier tests queued so the 100-job limit and assertions stay independent.
        self.runtime.state.pending = 0
        while not self.runtime.outbox.empty():
            self.runtime.outbox.get_nowait()

    def call(self, path, data=None, method=None, headers=None):
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def post_json(self, path, payload):
        return self.call(path, json.dumps(payload).encode(), "POST", {"Content-Type": "application/json"})

    def test_static_files_are_served_with_the_right_types(self):
        for path, kind in (("/", "text/html"), ("/index.html", "text/html"), ("/app.css", "text/css"),
                           ("/app.js", "text/javascript"), ("/live_slicer.js", "text/javascript")):
            status, headers, body = self.call(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(kind), path)
            self.assertGreater(len(body), 100, path)

    def test_a_query_string_does_not_hide_a_static_file(self):
        self.assertEqual(self.call("/app.js?v=3")[0], 200)

    def test_unknown_paths_are_404_for_get_and_post(self):
        self.assertEqual(self.call("/secret")[0], 404)
        self.assertEqual(self.call("/../web_ui.py")[0], 404)
        self.assertEqual(self.post_json("/api/nope", {})[0], 404)

    def test_status_endpoint_is_json_and_never_cached(self):
        status, headers, body = self.call("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        data = json.loads(body)
        self.assertEqual(set(data["captions"]), set(config.TARGET_LANGUAGES))
        self.assertIn("brokers", data)

    def test_captions_endpoint_returns_the_per_language_lists(self):
        status, _, body = self.call("/api/captions")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body)), set(config.TARGET_LANGUAGES))

    def test_posting_a_caption_is_accepted_and_queued(self):
        status, _, body = self.post_json("/api/captions", {"text": "Good morning", "source_lang": "en"})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"accepted": True})
        self.assertEqual(self.runtime.outbox.get_nowait(), ("Good morning", "en"))

    def test_invalid_captions_get_a_400_with_the_reason(self):
        for payload, fragment in (({"text": ""}, "empty"), ({"text": "x" * 2001}, "2,000"),
                                  ({"text": "hi", "source_lang": "xx"}, "Unknown source language")):
            status, _, body = self.post_json("/api/captions", payload)
            self.assertEqual(status, 400)
            self.assertIn(fragment, json.loads(body)["error"])

    def test_malformed_or_oversized_json_is_a_400_not_a_crash(self):
        self.assertEqual(self.call("/api/captions", b"{not json", "POST")[0], 400)
        valid_but_huge = json.dumps({"text": "a" * 20_000}).encode()
        status, _, body = self.call("/api/captions", valid_but_huge, "POST", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("too large", json.loads(body)["error"])   # refused by size, before it is even parsed

    def test_demo_endpoint_queues_the_sentences(self):
        status, _, body = self.call("/api/demo", b"", "POST")
        self.assertEqual((status, json.loads(body)["count"]), (202, len(web_ui.DEMO_SENTENCES)))

    def test_clear_endpoint_empties_captions(self):
        self.runtime.state.add_caption({"lang": "th", "chunk_id": "zz", "text": "x"})
        status, _, body = self.call("/api/clear", b"", "POST")
        self.assertEqual((status, json.loads(body)), (200, {"cleared": True}))
        self.assertEqual(self.runtime.state.snapshot()["captions"]["th"], [])

    def test_audio_upload_needs_a_language_and_a_body(self):
        status, _, body = self.call("/api/captions/audio", b"", "POST")
        self.assertEqual(status, 400)
        self.assertIn("empty", json.loads(body)["error"])
        status, _, body = self.call("/api/captions/audio?source_lang=auto", b"RIFFdata", "POST")
        self.assertEqual(status, 400)

    def test_audio_upload_is_handed_to_the_transcriber(self):
        with mock.patch.object(self.runtime, "start_audio") as start:
            status, _, _ = self.call("/api/captions/audio?source_lang=th", b"audio-bytes", "POST")
        self.assertEqual(status, 202)
        start.assert_called_once_with(b"audio-bytes", "th")

    def test_a_saturated_live_queue_answers_429_and_marks_the_slice_dropped(self):
        with mock.patch.object(self.runtime, "enqueue_live_slice", side_effect=web_ui.LiveBacklogError("behind")):
            status, _, body = self.call("/api/live/slice?source_lang=my", b"slice", "POST")
        self.assertEqual(status, 429)
        self.assertTrue(json.loads(body)["dropped"])

    def test_a_live_slice_is_queued(self):
        with mock.patch.object(self.runtime, "enqueue_live_slice", return_value=2) as enqueue:
            status, _, body = self.call("/api/live/slice?source_lang=my", b"slice", "POST")
        self.assertEqual((status, json.loads(body)["queued"]), (202, 2))
        enqueue.assert_called_once_with(b"slice", "my")


class AccessControlTests(unittest.TestCase):
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

    def call(self, path, data=None, method=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Access-Token"] = token
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def post(self, token=None, path="/api/captions"):
        return self.call(path, json.dumps({"text": "hello", "source_lang": "en"}).encode(), "POST", token)

    def test_with_no_token_configured_the_dashboard_stays_open(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_ACCESS_TOKEN": ""}):
            self.assertEqual(self.post()[0], 202)

    def test_a_configured_token_is_required_for_every_post(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_ACCESS_TOKEN": "s3cret"}):
            for path in ("/api/captions", "/api/demo", "/api/clear", "/api/live/slice?source_lang=my",
                         "/api/captions/audio?source_lang=my"):
                status, _, body = self.post(path=path)
                self.assertEqual(status, 401, path)
                self.assertIn("Access token", json.loads(body)["error"])

    def test_a_wrong_or_partial_token_is_refused_and_the_right_one_accepted(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_ACCESS_TOKEN": "s3cret"}):
            for wrong in ("", "s3cre", "s3cret ", "S3CRET", "s3cretx"):
                self.assertEqual(self.post(wrong)[0], 401, repr(wrong))
            self.assertEqual(self.post("s3cret")[0], 202)

    def test_viewing_stays_open_so_an_audience_can_watch_without_the_token(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_ACCESS_TOKEN": "s3cret"}):
            for path in ("/", "/api/status", "/api/captions", "/api/languages", "/health", "/app.js"):
                self.assertEqual(self.call(path)[0], 200, path)

    def test_a_refused_request_does_nothing(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_ACCESS_TOKEN": "s3cret"}):
            self.runtime.state.add_caption({"lang": "th", "chunk_id": "keep", "text": "x"})
            self.call("/api/clear", b"", "POST")
            self.post()
        self.assertEqual(len(self.runtime.state.snapshot()["captions"]["th"]), 1)
        self.assertTrue(self.runtime.outbox.empty())

    def test_posts_beyond_the_limit_get_429_with_retry_after_but_reads_do_not(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_RATE_LIMIT_PER_MINUTE": "3", "POLYGLOT_ACCESS_TOKEN": ""}):
            self.assertEqual([self.post()[0] for _ in range(3)], [202, 202, 202])
            status, headers, body = self.post()
            self.assertEqual(status, 429)
            self.assertGreaterEqual(int(headers["Retry-After"]), 1)
            self.assertIn("Too many", json.loads(body)["error"])
            self.assertEqual(self.call("/api/status")[0], 200)

    def test_a_refused_upload_still_gets_a_readable_answer_not_a_dropped_connection(self):
        body = b"\x00" * 600_000
        with mock.patch.dict("os.environ", {"POLYGLOT_ACCESS_TOKEN": "s3cret"}):
            for _ in range(5):
                status, _, payload = self.call("/api/captions/audio?source_lang=my", body, "POST")
                self.assertEqual(status, 401)
                self.assertIn("Access token", json.loads(payload)["error"])

    def test_a_limit_of_zero_means_unlimited(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_RATE_LIMIT_PER_MINUTE": "0"}):
            self.assertTrue(all(self.post()[0] in (202, 400) for _ in range(30)))

    def test_requests_with_a_wrong_token_do_not_use_up_the_allowance(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_RATE_LIMIT_PER_MINUTE": "2", "POLYGLOT_ACCESS_TOKEN": "s3cret"}):
            for _ in range(10):
                self.assertEqual(self.post("nope")[0], 401)
            self.assertEqual(self.post("s3cret")[0], 202)


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_the_limit_then_reports_how_long_to_wait(self):
        limiter = web_ui.RequestRateLimiter(60.0)
        self.assertEqual([limiter.allow("a", 2) for _ in range(2)], [0.0, 0.0])
        wait = limiter.allow("a", 2)
        self.assertTrue(0 < wait <= 60.0)

    def test_each_client_has_its_own_allowance(self):
        limiter = web_ui.RequestRateLimiter(60.0)
        limiter.allow("a", 1)
        self.assertGreater(limiter.allow("a", 1), 0)
        self.assertEqual(limiter.allow("b", 1), 0.0)

    def test_the_allowance_returns_after_the_window(self):
        limiter = web_ui.RequestRateLimiter(0.1)
        limiter.allow("a", 1)
        self.assertGreater(limiter.allow("a", 1), 0)
        time.sleep(0.15)
        self.assertEqual(limiter.allow("a", 1), 0.0)

    def test_zero_or_negative_limit_never_blocks(self):
        limiter = web_ui.RequestRateLimiter(60.0)
        self.assertTrue(all(limiter.allow("a", 0) == 0.0 for _ in range(100)))
        self.assertTrue(all(limiter.allow("a", -5) == 0.0 for _ in range(100)))

    def test_idle_clients_are_forgotten_so_memory_stays_bounded(self):
        limiter = web_ui.RequestRateLimiter(0.01)
        for n in range(5000):
            limiter.allow(f"10.0.{n // 256}.{n % 256}", 5)
        time.sleep(0.02)
        limiter.allow("fresh", 5)
        self.assertLess(len(limiter._hits), 5000)

    def test_settings_read_from_the_environment(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_RATE_LIMIT_PER_MINUTE": "", "POLYGLOT_ACCESS_TOKEN": ""}):
            self.assertEqual(web_ui.rate_limit_per_minute(), 240)    # compose passes unset values as ""
            self.assertEqual(web_ui.access_token(), "")
        with mock.patch.dict("os.environ", {"POLYGLOT_RATE_LIMIT_PER_MINUTE": "-3", "POLYGLOT_ACCESS_TOKEN": "t"}):
            self.assertEqual(web_ui.rate_limit_per_minute(), 0)
            self.assertEqual(web_ui.access_token(), "t")


class PivotAndPreloadTests(unittest.TestCase):
    def test_no_english_pivot_when_every_language_goes_straight_to_gemini(self):
        runtime = DashboardRuntime()
        with mock.patch.object(runtime, "_cluster_is_real", return_value=True):
            with mock.patch.dict("os.environ", {"TRANSLATION_BACKEND": "gemini"}):
                self.assertFalse(runtime._pivot_applies("my"))
            for backend in ("auto", "libretranslate", ""):
                with mock.patch.dict("os.environ", {"TRANSLATION_BACKEND": backend}):
                    self.assertTrue(runtime._pivot_applies("my"), backend)

    def test_a_gemini_only_backend_sends_the_caption_once_to_everyone(self):
        runtime = DashboardRuntime()
        runtime.producer = mock.Mock()
        with mock.patch.object(runtime, "_cluster_is_real", return_value=True), \
                mock.patch.dict("os.environ", {"TRANSLATION_BACKEND": "gemini"}):
            thread = threading.Thread(target=runtime._send_loop, daemon=True)
            thread.start()
            runtime.state.pending = 1
            runtime.outbox.put(("\u1019", "my"))
            self.assertTrue(wait_for(lambda: runtime.producer.send.called))
        runtime.stop_event.set()
        thread.join(2)
        self.assertEqual(runtime.producer.send.call_count, 1)
        self.assertNotIn("targets", runtime.producer.send.call_args.kwargs)

    def test_whisper_is_preloaded_in_the_background_at_start(self):
        runtime = DashboardRuntime()
        loaded = threading.Event()
        with mock.patch.object(web_ui, "_load_whisper", side_effect=lambda name: loaded.set()) as load, \
                mock.patch.object(runtime, "_watch_language"), mock.patch.object(runtime, "_send_loop"), \
                mock.patch.object(runtime, "start_live_workers"), \
                mock.patch.dict("os.environ", {"POLYGLOT_PRELOAD_WHISPER": "1"}):
            runtime.start()
            self.assertTrue(loaded.wait(2))
        load.assert_called_once_with(web_ui.WHISPER_MODEL)
        runtime.stop()

    def test_preload_can_be_switched_off(self):
        runtime = DashboardRuntime()
        with mock.patch.object(web_ui, "_load_whisper") as load, \
                mock.patch.object(runtime, "_watch_language"), mock.patch.object(runtime, "_send_loop"), \
                mock.patch.object(runtime, "start_live_workers"), \
                mock.patch.dict("os.environ", {"POLYGLOT_PRELOAD_WHISPER": "0"}):
            runtime.start()
            time.sleep(0.2)
        load.assert_not_called()
        runtime.stop()

    def test_a_missing_whisper_install_does_not_break_startup(self):
        runtime = DashboardRuntime()
        for failure in (SystemExit("Whisper is not installed"), RuntimeError("out of memory")):
            with mock.patch.object(web_ui, "_load_whisper", side_effect=failure):
                runtime._preload_whisper()   # must simply return


class MainTests(unittest.TestCase):
    def test_wires_the_runtime_into_the_handler_and_shuts_down_cleanly(self):
        server, runtime = mock.Mock(), mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(sys, "argv", ["web_ui.py", "--port", "9999"]), \
                mock.patch.object(web_ui, "DashboardRuntime", return_value=runtime), \
                mock.patch.object(web_ui, "ThreadingHTTPServer", return_value=server) as cls:
            web_ui.main()
        self.assertEqual(cls.call_args.args[0], ("127.0.0.1", 9999))
        self.assertIs(DashboardHandler.runtime, runtime)
        runtime.start.assert_called_once()
        server.shutdown.assert_called_once()
        runtime.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
