"""Rooms (separate audiences on one relay), the SQLite history behind them, translation context, and the
push stream (/api/events) that delivers changes to browsers as they happen."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import history
import web_ui
from web_ui import DEFAULT_ROOM, DashboardHandler, DashboardRuntime, DashboardState

THAI = "สวัสดี"


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def row(n, room_text="t", **translations):
    return {"chunk_id": f"c{n}", "number": n, "created_at": 1000.0 + n, "text": f"{room_text}{n}", "source_lang": "en",
            "translations": {lang: {"text": text} for lang, text in translations.items()}}


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "history.db")

    def store(self, path=None, **kwargs):
        store = history.SqliteHistory(path or self.path, **kwargs)
        self.addCleanup(store.close)
        return store


class SqliteHistoryTests(TempDir):
    def test_rows_come_back_per_room_in_order_with_their_translations(self):
        store = self.store()
        store.save("main", row(2, th=THAI))
        store.save("main", row(1))
        store.save("other", row(9))
        loaded = store.load("main", 10)
        self.assertEqual(list(loaded), ["c1", "c2"])
        self.assertEqual(loaded["c2"]["translations"]["th"]["text"], THAI)
        self.assertEqual(list(store.load("other", 10)), ["c9"])
        self.assertEqual(store.rooms(), ["main", "other"])

    def test_saving_a_chunk_again_updates_it(self):
        store = self.store()
        store.save("main", row(1))
        store.save("main", row(1, th="x"))
        self.assertEqual(store.count("main"), 1)
        self.assertIn("th", store.load("main", 10)["c1"]["translations"])

    def test_load_returns_only_the_newest_rows(self):
        store = self.store()
        for n in range(1, 8):
            store.save("main", row(n))
        self.assertEqual(list(store.load("main", 3)), ["c5", "c6", "c7"])

    def test_query_returns_a_time_window_oldest_first(self):
        store = self.store()
        for n in range(1, 6):
            store.save("main", row(n))
        self.assertEqual([r["chunk_id"] for r in store.query("main", 1002.0, 1004.0)], ["c2", "c3", "c4"])
        self.assertEqual(len(store.query("main", None, None)), 5)

    def test_wipe_clears_one_room_only(self):
        store = self.store()
        store.save("main", row(1))
        store.save("other", row(2))
        store.wipe("main")
        self.assertEqual((store.count("main"), store.count("other")), (0, 1))

    def test_each_room_is_pruned_to_its_row_limit(self):
        store = self.store(max_rows=100)
        with mock.patch.object(history.SqliteHistory, "PRUNE_EVERY", 10):
            for n in range(150):
                store.save("main", row(n))
        self.assertLessEqual(store.count("main"), 110)
        self.assertIn("c149", store.load("main", 5))

    def test_it_survives_reopening(self):
        first = history.SqliteHistory(self.path)
        first.save("main", row(1, th=THAI))
        first.close()
        self.assertEqual(self.store().load("main", 5)["c1"]["translations"]["th"]["text"], THAI)

    def test_a_broken_database_is_reported_once_and_never_raises(self):
        log = mock.Mock()
        store = self.store(logger=log)
        store._db.close()                                         # every call now fails
        store.save("main", row(1))
        store.save("main", row(2))
        self.assertEqual(store.load("main", 5), {})
        self.assertEqual(store.query("main", None, None), [])
        self.assertEqual((store.count("main"), store.rooms()), (0, []))
        store.wipe("main")
        self.assertEqual(log.warning.call_count, 1)
        store._db = __import__("sqlite3").connect(self.path, check_same_thread=False)   # so cleanup can close it

    def test_an_old_json_lines_history_is_moved_into_the_database(self):
        legacy = os.path.join(self.dir.name, "history.jsonl")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(row(1, th=THAI)) + "\n")
            handle.write(json.dumps(row(2)) + "\n")
        store = history.open_history(legacy)
        self.addCleanup(store.close)
        self.assertEqual(list(store.load("main", 10)), ["c1", "c2"])
        self.assertTrue(os.path.exists(legacy + ".jsonl.bak"))
        with open(legacy, "rb") as handle:
            self.assertEqual(handle.read(16), history.SQLITE_MAGIC)

    def test_a_new_database_takes_over_the_old_file_beside_it(self):
        legacy = os.path.join(self.dir.name, "history.jsonl")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(row(1, th=THAI)) + "\n")
        store = history.open_history(self.path)                       # history.db, next to history.jsonl
        self.addCleanup(store.close)
        self.assertEqual(store.load("main", 5)["c1"]["translations"]["th"]["text"], THAI)
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(legacy + ".bak"))
        store.close()
        again = history.open_history(self.path)                       # only once: the .bak is not imported again
        self.addCleanup(again.close)
        self.assertEqual(again.count("main"), 1)

    def test_an_existing_database_is_opened_as_it_is(self):
        first = history.open_history(self.path)
        first.save("main", row(1))
        first.close()
        again = history.open_history(self.path)
        self.addCleanup(again.close)
        self.assertEqual(again.count("main"), 1)
        self.assertFalse(os.path.exists(self.path + ".jsonl.bak"))


class RoomNameTests(unittest.TestCase):
    def test_valid_names(self):
        for name in ("main", "a", "room-2", "x" * 32, "UIT-Hall"):
            self.assertEqual(web_ui.room_name(name), name.lower())
        self.assertEqual(web_ui.room_name(""), DEFAULT_ROOM)
        self.assertEqual(web_ui.room_name(None), DEFAULT_ROOM)

    def test_invalid_names(self):
        for name in ("-a", "a b", "a/b", "x" * 33, "../etc", "a~b", "room!", "ก"):
            with self.assertRaises(ValueError, msg=name):
                web_ui.room_name(name)

    def test_the_room_is_read_back_from_the_chunk_id(self):
        self.assertEqual(web_ui.room_of_chunk("hall-b~ui-1-1"), "hall-b")
        for chunk_id in ("ui-1-1", None, "", "bad name~ui-1", "~ui-1"):
            self.assertEqual(web_ui.room_of_chunk(chunk_id), DEFAULT_ROOM, chunk_id)


class RuntimeTestCase(unittest.TestCase):
    def runtime(self, **env):
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": "", **env})
        patcher.start()
        self.addCleanup(patcher.stop)
        runtime = DashboardRuntime()
        runtime.producer = mock.Mock()
        self.addCleanup(runtime.stop)
        return runtime

    def send_all(self, runtime):
        """Run the sender loop until the outbox is empty."""
        thread = threading.Thread(target=runtime._send_loop, daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: runtime.outbox.unfinished_tasks == 0))
        runtime.stop_event.set()
        thread.join(2)


class RoomTests(RuntimeTestCase):
    def test_rooms_are_created_on_first_use_and_listed(self):
        runtime = self.runtime()
        hall = runtime.room("hall-a")
        self.assertIs(runtime.room("HALL-A"), hall)
        self.assertEqual(sorted(r["room"] for r in runtime.room_list()), ["hall-a", "main"])

    def test_the_number_of_rooms_is_capped(self):
        runtime = self.runtime(POLYGLOT_MAX_ROOMS="3")
        runtime.room("a")
        runtime.room("b")
        with self.assertRaisesRegex(ValueError, "already has 3 rooms"):
            runtime.room("c")
        runtime.room("a")                                      # existing rooms still work

    def test_a_caption_in_a_room_stays_in_that_room(self):
        runtime = self.runtime()
        runtime.submit("hello hall", "en", room="hall-a")
        runtime.submit("hello main", "en")
        self.assertEqual(runtime.outbox.queue[0], ("hello hall", "en", "hall-a"))
        self.assertEqual(runtime.outbox.queue[1], ("hello main", "en"))      # the default room keeps its old shape
        self.send_all(runtime)
        hall, main = runtime.room("hall-a"), runtime.state
        self.assertEqual([r["text"] for r in hall.history.values()], ["hello hall"])
        self.assertEqual([r["text"] for r in main.history.values()], ["hello main"])
        sent = [call.args[1] for call in runtime.producer.send.call_args_list]
        self.assertTrue(any(cid.startswith("hall-a~ui-") for cid in sent))
        self.assertTrue(any(cid.startswith("ui-") for cid in sent))

    def test_translations_coming_back_find_their_room(self):
        runtime = self.runtime()
        runtime.submit("hello", "en", room="hall-a")
        self.send_all(runtime)
        chunk_id = next(iter(runtime.room("hall-a").history))
        runtime.room_for_chunk(chunk_id).add_caption({"lang": "th", "chunk_id": chunk_id, "text": THAI})
        self.assertEqual(runtime.room("hall-a").history[chunk_id]["translations"]["th"]["text"], THAI)
        self.assertEqual(len(runtime.state.history), 0)

    def test_a_caption_for_a_room_that_cannot_be_made_lands_in_the_main_room(self):
        runtime = self.runtime(POLYGLOT_MAX_ROOMS="1")
        self.assertIs(runtime.room_for_chunk("elsewhere~ui-1-1"), runtime.state)

    def test_worker_changes_reach_every_room_and_new_rooms_start_with_them(self):
        runtime = self.runtime()
        hall = runtime.room("hall-a")
        runtime._worker_status("th", True, "th-1")
        self.assertTrue(hall.workers["th"]["connected"])
        self.assertTrue(runtime.state.workers["th"]["connected"])
        self.assertTrue(runtime.room("hall-b").workers["th"]["connected"])

    def test_status_is_for_the_requested_room(self):
        runtime = self.runtime()
        runtime.room("hall-a").event("only in hall a")
        with mock.patch.object(runtime, "broker_status", return_value=[]):
            hall = runtime.status(room="hall-a")
            main = runtime.status()
        self.assertEqual((hall["room"], main["room"]), ("hall-a", "main"))
        self.assertIn("only in hall a", [a["message"] for a in hall["activity"]])
        self.assertNotIn("only in hall a", [a["message"] for a in main["activity"]])

    def test_a_leader_change_interrupts_unfinished_work_in_every_room(self):
        runtime = self.runtime()
        runtime.submit("hello", "en", room="hall-a")
        self.send_all(runtime)
        leader = lambda n: [{"id": n, "online": True, "leader": True, "jobs": []}]     # noqa: E731
        with mock.patch.object(runtime, "broker_status", return_value=leader(2)):
            runtime.status()
        with mock.patch.object(runtime, "broker_status", return_value=leader(1)):
            runtime.status()
        chunk = next(iter(runtime.room("hall-a").chunks.values()))
        self.assertEqual(chunk["targets"]["th"]["status"], "interrupted")

    def test_rooms_with_saved_history_come_back_after_a_restart(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "h.db")
        with mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": path}):
            first = DashboardRuntime()
            first.producer = mock.Mock()
            first.room("hall-a").record_original("hall-a~ui-1-1", "kept", "en")
            first.stop()
            first.store.close()
            second = DashboardRuntime()
        self.addCleanup(second.store.close)
        self.addCleanup(second.stop)
        self.assertIn("hall-a", second.rooms)
        self.assertEqual([r["text"] for r in second.room("hall-a").history.values()], ["kept"])
        self.assertEqual(len(second.state.history), 0)

    def test_clearing_a_room_leaves_the_others(self):
        runtime = self.runtime()
        runtime.room("hall-a").record_original("hall-a~1", "a", "en")
        runtime.state.record_original("1", "m", "en")
        runtime.room("hall-a").clear()
        self.assertEqual((len(runtime.room("hall-a").history), len(runtime.state.history)), (0, 1))

    def test_live_slices_are_released_into_their_own_room(self):
        runtime = self.runtime()
        runtime.submit = mock.Mock()
        runtime.enqueue_live_slice(b"x", "th", room="hall-a")
        seq, _, _ = runtime.live_queue.get_nowait()
        with runtime._live_lock:
            runtime._live_finish(seq, [("sawasdee", "th")])
        runtime.submit.assert_called_once_with("sawasdee", "th", room="hall-a")
        self.assertEqual(runtime.room("hall-a").live["slices"], 1)
        self.assertEqual(runtime.state.live["slices"], 0)


class ContextTests(RuntimeTestCase):
    def test_each_caption_carries_the_sentences_said_just_before_it_in_its_room(self):
        runtime = self.runtime()
        for text in ("one", "two", "three"):
            runtime.submit(text, "en")
        runtime.submit("elsewhere", "en", room="hall-a")
        self.send_all(runtime)
        calls = {call.args[0]: call.kwargs.get("context") for call in runtime.producer.send.call_args_list}
        self.assertIsNone(calls["one"])
        self.assertEqual(calls["two"], ["one"])
        self.assertEqual(calls["three"], ["one", "two"])
        self.assertIsNone(calls["elsewhere"])                  # another room's sentences are not context

    def test_context_can_be_switched_off_or_lengthened(self):
        runtime = self.runtime()
        for text in ("a", "b", "c", "d"):
            runtime.submit(text, "en")
        with mock.patch.dict(os.environ, {"POLYGLOT_CONTEXT_SENTENCES": "3"}):
            self.send_all(runtime)
        self.assertEqual(runtime.producer.send.call_args_list[-1].kwargs["context"], ["a", "b", "c"])
        runtime = self.runtime()
        runtime.submit("a", "en")
        runtime.submit("b", "en")
        with mock.patch.dict(os.environ, {"POLYGLOT_CONTEXT_SENTENCES": "0"}):
            self.send_all(runtime)
        self.assertNotIn("context", runtime.producer.send.call_args_list[-1].kwargs)

    def test_the_producer_puts_at_most_five_context_lines_in_the_message(self):
        from stt_service import TranscriptProducer
        producer = TranscriptProducer()
        producer._connect = lambda: False
        producer.sock = mock.Mock()
        producer.send("now", "c1", context=[f"s{n}" for n in range(8)])
        message = json.loads(producer.sock.sendall.call_args.args[0])
        self.assertEqual(message["context"], ["s3", "s4", "s5", "s6", "s7"])
        producer.send("now", "c2")
        self.assertNotIn("context", json.loads(producer.sock.sendall.call_args.args[0]))


class FeedTests(unittest.TestCase):
    def state(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": ""}):
            return DashboardState()

    def test_every_change_is_published_with_a_rising_number(self):
        state = self.state()
        state.record_original("c1", "hello", "en")
        state.add_caption({"lang": "th", "chunk_id": "c1", "text": THAI})
        state.worker_status("th", True, "th-1")
        state.clear()
        kinds = [kind for _, kind, _ in state.events_after(0)]
        for expected in ("chunk", "caption", "workers", "clear", "activity"):
            self.assertIn(expected, kinds)
        numbers = [seq for seq, _, _ in state.events_after(0)]
        self.assertEqual(numbers, sorted(numbers))

    def test_a_caption_event_carries_the_whole_row_for_the_listener(self):
        state = self.state()
        state.record_original("c1", "hello", "en")
        state.add_caption({"lang": "th", "chunk_id": "c1", "text": THAI})
        caption = [data for _, kind, data in state.events_after(0) if kind == "caption"][0]
        self.assertEqual(caption["lang"], "th")
        self.assertEqual(caption["row"]["text"], "hello")
        self.assertEqual(caption["row"]["translations"]["th"]["text"], THAI)

    def test_only_newer_events_are_returned(self):
        state = self.state()
        state.event("one")
        mark = state.feed_seq
        state.event("two")
        self.assertEqual([d["message"] for _, _, d in state.events_after(mark)], ["two"])
        self.assertEqual(state.events_after(state.feed_seq), [])

    def test_a_waiting_reader_wakes_as_soon_as_something_happens(self):
        state = self.state()
        mark = state.feed_seq
        threading.Timer(0.05, lambda: state.event("now")).start()
        started = time.monotonic()
        events = state.events_after(mark, timeout=5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(events[0][2]["message"], "now")

    def test_a_reader_too_far_behind_is_told_to_reload(self):
        state = self.state()
        for n in range(DashboardState.FEED_EVENTS + 20):
            state.event(str(n))
        self.assertEqual([kind for _, kind, _ in state.events_after(3)], ["reload"])


class HttpTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": "", "POLYGLOT_ACCESS_TOKEN": "",
                                               "POLYGLOT_VIEW_TOKEN": "", "POLYGLOT_MAX_ROOMS": "5"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = DashboardRuntime()
        self.runtime.producer = mock.Mock()
        DashboardHandler.runtime = self.runtime
        DashboardHandler.limiter = web_ui.RequestRateLimiter()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.runtime.stop)

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")


class RoomHttpTests(HttpTestCase):
    def test_captions_history_and_clear_are_kept_apart_by_room(self):
        self.runtime.room("hall-a").record_original("hall-a~1", "in hall a", "en")
        self.runtime.state.record_original("1", "in main", "en")
        hall = self.call("/api/history?room=hall-a")[1]
        main = self.call("/api/history")[1]
        self.assertEqual(([r["text"] for r in hall["chunks"]], hall["room"]), (["in hall a"], "hall-a"))
        self.assertEqual([r["text"] for r in main["chunks"]], ["in main"])
        self.assertEqual(self.call("/api/clear?room=hall-a", {})[0], 200)
        self.assertEqual(self.call("/api/history?room=hall-a")[1]["chunks"], [])
        self.assertEqual(len(self.call("/api/history")[1]["chunks"]), 1)

    def test_a_typed_caption_goes_to_the_room_in_the_address(self):
        self.assertEqual(self.call("/api/captions?room=hall-b", {"text": "hi", "source_lang": "en"})[0], 202)
        self.assertEqual(self.runtime.outbox.get_nowait(), ("hi", "en", "hall-b"))

    def test_the_room_list(self):
        self.runtime.room("hall-a")
        status, body = self.call("/api/rooms")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(r["room"] for r in body["rooms"]), ["hall-a", "main"])
        self.assertEqual(body["max_rooms"], 5)

    def test_status_reports_its_room(self):
        with mock.patch.object(self.runtime, "broker_status", return_value=[]):
            self.assertEqual(self.call("/api/status?room=hall-c")[1]["room"], "hall-c")

    def test_a_bad_room_name_is_a_400_everywhere(self):
        for path in ("/api/history?room=a%20b", "/api/status?room=../x", "/api/captions?room=!", "/api/export?room=a~b",
                     "/api/events?room=%20"):
            self.assertEqual(self.call(path)[0], 400, path)
        self.assertEqual(self.call("/api/captions?room=bad%20name", {"text": "x", "source_lang": "en"})[0], 400)

    def test_one_room_too_many_is_refused_politely(self):
        for name in ("r1", "r2", "r3", "r4"):
            self.runtime.room(name)
        status, body = self.call("/api/captions?room=r5", {"text": "x", "source_lang": "en"})
        self.assertEqual(status, 400)
        self.assertIn("rooms", body["error"])


class EventStreamTests(HttpTestCase):
    def open_stream(self, path="/api/events", headers=None):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n{extra}\r\n".encode())
        reader = sock.makefile("rb")
        self.addCleanup(reader.close)
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            head += reader.read(1)
        return head.decode(), reader

    @staticmethod
    def next_event(reader):
        fields = {}
        while True:
            line = reader.readline().decode("utf-8").rstrip("\n")
            if line == "":
                if fields.get("event") or fields.get("comment"):
                    return fields
                continue
            if line.startswith(":"):
                fields["comment"] = line
                continue
            key, _, value = line.partition(": ")
            fields[key] = value

    def test_the_stream_opens_with_a_hello_and_the_right_headers(self):
        head, reader = self.open_stream("/api/events?room=hall-a")
        self.assertIn("200", head.splitlines()[0])
        self.assertIn("text/event-stream", head)
        self.assertIn("Cache-Control: no-store", head)
        hello = self.next_event(reader)
        self.assertEqual(hello["event"], "hello")
        self.assertEqual(json.loads(hello["data"])["room"], "hall-a")
        self.assertEqual(hello["retry"], "2000")

    def test_a_caption_is_pushed_the_moment_it_arrives(self):
        _, reader = self.open_stream()
        self.next_event(reader)                                            # hello
        state = self.runtime.state
        state.record_original("c1", "hello", "en")
        state.add_caption({"lang": "th", "chunk_id": "c1", "text": THAI})
        started = time.monotonic()
        seen = []
        while not any(e["event"] == "caption" for e in seen):
            seen.append(self.next_event(reader))
        self.assertLess(time.monotonic() - started, 2)
        caption = next(e for e in seen if e["event"] == "caption")
        self.assertEqual(json.loads(caption["data"])["row"]["translations"]["th"]["text"], THAI)
        self.assertTrue(caption["id"].isdigit())

    def test_events_of_other_rooms_are_not_sent(self):
        _, reader = self.open_stream("/api/events?room=hall-a")
        self.next_event(reader)
        self.runtime.state.record_original("m1", "main only", "en")
        self.runtime.room("hall-a").event("for hall a")
        event = self.next_event(reader)
        self.assertEqual(event["event"], "activity")
        self.assertIn("for hall a", event["data"])

    def test_a_reconnecting_browser_gets_what_it_missed(self):
        state = self.runtime.state
        state.event("before")
        mark = state.feed_seq
        state.event("missed one")
        state.event("missed two")
        _, reader = self.open_stream(headers={"Last-Event-ID": str(mark)})
        self.next_event(reader)
        messages = [json.loads(self.next_event(reader)["data"])["message"] for _ in range(2)]
        self.assertEqual(messages, ["missed one", "missed two"])

    def test_a_quiet_stream_sends_keep_alives(self):
        with mock.patch.object(web_ui, "EVENTS_KEEPALIVE_SECONDS", 0.05):
            _, reader = self.open_stream()
            self.next_event(reader)
            self.assertIn("keep-alive", self.next_event(reader)["comment"])

    def test_the_stream_ends_after_its_time_so_the_browser_reconnects(self):
        with mock.patch.object(web_ui, "EVENTS_MAX_SECONDS", 0.2), mock.patch.object(web_ui, "EVENTS_KEEPALIVE_SECONDS", 0.05):
            _, reader = self.open_stream()
            started = time.monotonic()
            while reader.readline():
                if time.monotonic() - started > 5:
                    self.fail("the stream did not end")

    def test_a_private_room_needs_the_viewing_token_for_the_stream_too(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_VIEW_TOKEN": "look"}):
            head, _ = self.open_stream()
            self.assertIn("401", head.splitlines()[0])
            head, reader = self.open_stream(headers={"Cookie": "polyglot_view=look"})
            self.assertIn("200", head.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
