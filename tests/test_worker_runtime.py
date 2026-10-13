"""Translator worker runtime: error classification, the Gemini text and Live
calls (against a fake client), the LibreTranslate HTTP helpers, and the worker
process talking to a real TCP "broker" on localhost."""

import asyncio
import contextlib
import http.server
import json
import queue
import socket
import sys
import threading
import time
import unittest

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)
from unittest import mock

import translator_worker
from common import config
from common.protocol import decode_stream, encode, recv_one
from translator_worker import (
    Translator, TranslatorWorker, _is_daily_quota_exhausted, _is_retryable, _suggested_retry_delay,
)

try:
    from google.genai import types as genai_types
except ImportError:  # the lightweight test image has no SDK
    genai_types = None


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class ApiError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ErrorClassificationTests(unittest.TestCase):
    def test_retryable_by_numeric_code(self):
        for code in (429, 500, 503):
            self.assertTrue(_is_retryable(ApiError("x", code)), code)

    def test_not_retryable_codes_and_plain_errors(self):
        for exc in (ApiError("bad", 400), ApiError("denied", 403), ApiError("nf", 404), ValueError("nope")):
            self.assertFalse(_is_retryable(exc), repr(exc))

    def test_retryable_by_message_marker(self):
        for text in ("429 RESOURCE_EXHAUSTED", "503 UNAVAILABLE", "Server disconnected without sending a response",
                     "Connection reset by peer", "connection closed unexpectedly", "ConnectionError: x"):
            self.assertTrue(_is_retryable(RuntimeError(text)), text)

    def test_retry_delay_parsed_from_either_quote_style(self):
        self.assertEqual(_suggested_retry_delay(RuntimeError("{'retryDelay': '20s'}")), 20.0)
        self.assertEqual(_suggested_retry_delay(RuntimeError('"retryDelay": "1.5s"')), 1.5)
        self.assertIsNone(_suggested_retry_delay(RuntimeError("no hint here")))

    def test_daily_quota_needs_perday_and_no_delay(self):
        self.assertTrue(_is_daily_quota_exhausted(RuntimeError("quotaId: RequestsPerDay")))
        self.assertTrue(_is_daily_quota_exhausted(RuntimeError("GenerateRequestsPerDayPerProject")))
        self.assertFalse(_is_daily_quota_exhausted(RuntimeError("PerDay but 'retryDelay': '20s'")))
        self.assertFalse(_is_daily_quota_exhausted(RuntimeError("429 per minute")))


class FakeClient:
    """Stands in for google.genai.Client; records generate_content calls."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return mock.Mock(text=reply)


@unittest.skipUnless(genai_types is not None, "google-genai is installed only in the full image")
class GeminiTextCallTests(unittest.TestCase):
    def translator(self, replies, lang="km", **env):
        with mock.patch.dict("os.environ", {"GEMINI_TEXT_MODEL": "model-x", "POLYGLOT_STAGGER_STEP_SECONDS": "0",
                                            "GEMINI_TIER": "paid", **env}):
            t = Translator(lang)
        t._client = FakeClient(replies)
        return t

    def test_sends_text_with_the_translation_instruction_and_model(self):
        t = self.translator(["\u179f\u17bd\u179f\u17d2\u178f\u17b8"])
        self.assertEqual(t._translate_text("hello", "en"), "\u179f\u17bd\u179f\u17d2\u178f\u17b8")
        call = t._client.calls[0]
        self.assertEqual((call["model"], call["contents"]), ("model-x", "hello"))
        self.assertIn("Khmer", call["config"].system_instruction)
        self.assertAlmostEqual(call["config"].temperature, 0.2)

    def test_strips_whitespace_around_the_reply(self):
        self.assertEqual(self.translator(["  hi\n"], "th")._translate_text("x"), "hi")

    def test_an_empty_reply_is_an_error_not_a_blank_caption(self):
        with self.assertRaises(RuntimeError):
            self.translator([""])._translate_text("hello")
        with self.assertRaises(RuntimeError):
            self.translator([None])._translate_text("hello")

    def test_retries_a_429_then_succeeds_and_reports_the_retry(self):
        t = self.translator([ApiError("429 RESOURCE_EXHAUSTED", 429), "ok"], GEMINI_MAX_RETRIES="3")
        statuses = []
        t.report_status = lambda status, detail="": statuses.append(status)
        with mock.patch.object(translator_worker.time, "sleep"):
            self.assertEqual(t._translate_text("hello"), "ok")
        self.assertEqual(len(t._client.calls), 2)
        self.assertIn("retrying", statuses)
        self.assertEqual(statuses.count("translating"), 2)

    def test_stagger_sleeps_once_per_chunk_before_the_first_attempt(self):
        t = self.translator(["ok"])
        t.stagger_seconds = 2.5
        with mock.patch.object(translator_worker.time, "sleep") as sleep:
            t._translate_text("hello")
        sleep.assert_called_once_with(2.5)

    def test_translate_routes_to_gemini_text_for_unreadable_sources(self):
        t = self.translator(["\u1781\u17d2\u1798\u17c2\u179a"], "km", TRANSLATION_BACKEND="gemini")
        self.assertEqual(t.translate("\u1019\u1004\u103a\u1039\u1002\u101c\u102c\u1015\u102b", source_lang="my"), ("\u1781\u17d2\u1798\u17c2\u179a", "gemini-text"))


class AsyncRetryTests(unittest.TestCase):
    def translator(self, retries="3"):
        with mock.patch.dict("os.environ", {"GEMINI_MAX_RETRIES": retries}):
            return Translator("th")

    def test_recovers_after_a_transient_failure(self):
        t = self.translator()
        attempts = []

        async def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise ApiError("503 UNAVAILABLE", 503)
            return "ok"
        with mock.patch.object(translator_worker.asyncio, "sleep", new=mock.AsyncMock()):
            self.assertEqual(asyncio.run(t._call_with_retry_async(flaky)), "ok")
        self.assertEqual(len(attempts), 2)

    def test_gives_up_after_max_retries(self):
        t = self.translator("2")
        attempts = []

        async def always():
            attempts.append(1)
            raise ApiError("503 UNAVAILABLE", 503)
        with mock.patch.object(translator_worker.asyncio, "sleep", new=mock.AsyncMock()):
            with self.assertRaises(ApiError):
                asyncio.run(t._call_with_retry_async(always))
        self.assertEqual(len(attempts), 2)

    def test_never_retries_a_permanent_error_or_a_daily_quota(self):
        t = self.translator()
        for error in (ApiError("bad request", 400), RuntimeError("quotaId RequestsPerDay")):
            attempts = []

            async def fail():
                attempts.append(1)
                raise error
            with self.assertRaises(type(error)):
                asyncio.run(t._call_with_retry_async(fail))
            self.assertEqual(len(attempts), 1)

    def test_honours_googles_suggested_delay(self):
        t = self.translator()
        sleeps = []

        async def record(seconds):
            sleeps.append(seconds)
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '12s'}")
            return "ok"
        with mock.patch.object(translator_worker.asyncio, "sleep", new=record):
            asyncio.run(t._call_with_retry_async(flaky))
        self.assertGreaterEqual(sleeps[0], 12.0)


class FakeLiveSession:
    def __init__(self, responses):
        self.audio, self.ended, self.responses = [], False, responses

    async def send_realtime_input(self, audio=None, audio_stream_end=None):
        if audio is not None:
            self.audio.append(audio)
        if audio_stream_end:
            self.ended = True

    async def receive(self):
        for response in self.responses:
            yield response


def live_response(text=None, done=False):
    transcription = mock.Mock(text=text) if text is not None else None
    content = mock.Mock(output_transcription=transcription, turn_complete=done)
    return mock.Mock(server_content=content)


@unittest.skipUnless(genai_types is not None, "google-genai is installed only in the full image")
class GeminiLiveTests(unittest.TestCase):
    def run_live(self, responses, audio=b"\x00\x01" * 3200 * 3, timeout="30"):
        session = FakeLiveSession(responses)

        @contextlib.asynccontextmanager
        async def connect(model, config):
            session.model, session.config = model, config
            yield session
        client = mock.Mock()
        client.aio.live.connect = connect
        with mock.patch.dict("os.environ", {"GEMINI_LIVE_TIMEOUT": timeout}):
            t = Translator("th")
        t._client = client
        return t, session, asyncio.run(t._translate_live(audio)) if responses is not None else None

    def test_streams_audio_in_100ms_pieces_then_signals_end_and_joins_the_pieces(self):
        t, session, text = self.run_live([live_response("\u0e2a\u0e27\u0e31\u0e2a"), live_response("\u0e14\u0e35"), live_response(None, done=True)])
        self.assertEqual(text, "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35")
        self.assertTrue(session.ended)
        self.assertEqual(len(session.audio), 6)  # 3 x 6400 bytes -> six 3200-byte (100 ms) pieces
        self.assertTrue(all(len(b.data) == 3200 for b in session.audio))
        self.assertEqual(session.audio[0].mime_type, "audio/pcm;rate=16000")
        self.assertEqual(session.config.translation_config.target_language_code, "th")

    def test_stops_reading_at_turn_complete(self):
        _, _, text = self.run_live([live_response("a", done=True), live_response("b")])
        self.assertEqual(text, "a")

    def test_no_transcription_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.run_live([live_response(None, done=True)])

    def test_responses_without_server_content_are_skipped(self):
        _, _, text = self.run_live([mock.Mock(server_content=None), live_response("ok", done=True)])
        self.assertEqual(text, "ok")

    def test_silent_service_times_out(self):
        class Silent(FakeLiveSession):
            async def receive(self):
                await asyncio.sleep(5)
                yield live_response("late", done=True)
        session = Silent([])

        @contextlib.asynccontextmanager
        async def connect(model, config):
            yield session
        client = mock.Mock()
        client.aio.live.connect = connect
        with mock.patch.dict("os.environ", {"GEMINI_LIVE_TIMEOUT": "0.1", "GEMINI_MAX_RETRIES": "1"}):
            t = Translator("th")
        t._client = client
        with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
            asyncio.run(t._translate_live_once(b"\x00" * 3200))

    def test_translate_live_dispatches_through_translate(self):
        with mock.patch.dict("os.environ", {"GEMINI_MAX_RETRIES": "1"}):
            t = Translator("th")
        with mock.patch.object(Translator, "_translate_live", new=mock.AsyncMock(return_value="live text")):
            import base64
            self.assertEqual(t.translate("", audio_pcm_b64=base64.b64encode(b"\x00" * 3200).decode()),
                             ("live text", "gemini-live"))


class LibreTranslateHelperTests(unittest.TestCase):
    @contextlib.contextmanager
    def http_server(self, routes):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                seen.append((self.command, self.path, body))
                status, payload = routes[self.command, self.path](body, len(seen))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            do_GET = do_POST = respond

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", seen
        finally:
            server.shutdown()
            server.server_close()

    def translator(self, url, lang="th"):
        with mock.patch.dict("os.environ", {"LIBRETRANSLATE_URL": url}):
            return Translator(lang)

    def test_post_sends_json_and_parses_the_reply(self):
        routes = {("POST", "/translate"): lambda body, n: (200, {"translatedText": "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35"})}
        with self.http_server(routes) as (url, seen):
            reply = translator_worker._libretranslate_post(url + "/translate", {"q": "hello", "target": "th"})
        self.assertEqual(reply["translatedText"], "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35")
        self.assertEqual(json.loads(seen[0][2]), {"q": "hello", "target": "th"})

    def test_get_parses_json(self):
        with self.http_server({("GET", "/languages"): lambda body, n: (200, [{"code": "th"}])}) as (url, _):
            self.assertEqual(translator_worker._libretranslate_get(url + "/languages"), [{"code": "th"}])

    def test_translation_request_shape(self):
        routes = {("POST", "/translate"): lambda body, n: (200, {"translatedText": " ok "})}
        with self.http_server(routes) as (url, seen):
            self.assertEqual(self.translator(url)._translate_via_libretranslate("hello", "en"), "ok")
        self.assertEqual(json.loads(seen[0][2]), {"q": "hello", "source": "en", "target": "th", "format": "text"})

    def test_unknown_source_is_sent_as_auto(self):
        routes = {("POST", "/translate"): lambda body, n: (200, {"translatedText": "ok"})}
        with self.http_server(routes) as (url, seen):
            self.translator(url)._translate_via_libretranslate("hello")
        self.assertEqual(json.loads(seen[0][2])["source"], "auto")

    def test_retries_transient_failures_up_to_three_times(self):
        def flaky(body, n):
            return (500, {}) if n < 3 else (200, {"translatedText": "ok"})
        with self.http_server({("POST", "/translate"): flaky}) as (url, seen), \
                mock.patch.object(translator_worker.time, "sleep"):
            self.assertEqual(self.translator(url)._translate_via_libretranslate("hello", "en"), "ok")
        self.assertEqual(len(seen), 3)

    def test_gives_up_after_three_failures(self):
        with self.http_server({("POST", "/translate"): lambda body, n: (500, {})}) as (url, seen), \
                mock.patch.object(translator_worker.time, "sleep"):
            with self.assertRaises(Exception):
                self.translator(url)._translate_via_libretranslate("hello", "en")
        self.assertEqual(len(seen), 3)

    def test_an_empty_translation_is_an_error(self):
        routes = {("POST", "/translate"): lambda body, n: (200, {"translatedText": "  "})}
        with self.http_server(routes) as (url, _), mock.patch.object(translator_worker.time, "sleep"):
            with self.assertRaises(RuntimeError):
                self.translator(url)._translate_via_libretranslate("hello", "en")

    def test_language_support_is_read_from_the_languages_endpoint(self):
        routes = {("GET", "/languages"): lambda body, n: (200, [{"code": "en"}, {"code": "th"}])}
        with self.http_server(routes) as (url, _):
            self.assertTrue(self.translator(url, "th")._libretranslate_supports_target())
            self.assertFalse(self.translator(url, "vi")._libretranslate_supports_target())


class FakeBroker:
    """A real TCP broker stand-in: accepts worker registrations and records traffic."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.registrations, self.received, self.conns = [], queue.Queue(), {}
        self.accept_new = True
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            if not self.accept_new:
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            first = recv_one(conn)
            worker_id = first["worker_id"]
            self.registrations.append(first)
            self.conns[worker_id] = conn
            conn.sendall(encode({"type": "register_ack", "accepted": True, "leader_id": 1}))
            for message in decode_stream(conn):
                self.received.put((worker_id, message))
        except Exception:
            pass

    def send(self, message, worker_id):
        self.conns[worker_id].sendall(encode(message))

    def drop_connections(self):
        for conn in list(self.conns.values()):
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
        self.conns.clear()

    def next_message(self, kind, worker_id, timeout=6.0):
        # Only this worker's traffic: a worker left running by an earlier test may
        # still be re-registering against whichever fake broker owns the patched port.
        deadline = time.monotonic() + timeout
        skipped = []
        while time.monotonic() < deadline:
            try:
                sender, message = self.received.get(timeout=0.1)
            except queue.Empty:
                continue
            if sender == worker_id and message["type"] == kind:
                return message
            skipped.append((sender, message["type"], message.get("status")))
        raise AssertionError(f"no {kind} message arrived from {worker_id}; saw {skipped[-6:]}; "
                             f"registered {[r['worker_id'] for r in self.registrations]}")

    def close(self):
        self._stop.set()
        self.drop_connections()
        self.server.close()


class WorkerProcessTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        for patcher in (
            mock.patch.object(config, "BROKERS", [{"id": 1, "host": "127.0.0.1", "port": self.broker.port}]),
            mock.patch.object(config, "RECONNECT_DELAY", 0.05),
            mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.05),
            mock.patch.object(config, "CONNECT_TIMEOUT", 0.5),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.workers = []
        self.addCleanup(self.broker.close)

    next_id = iter(range(1, 10_000))

    def start_worker(self, lang="th", mock_mode=True):
        worker_id = f"{lang}-test{next(self.next_id)}"
        worker = TranslatorWorker(lang, "127.0.0.1", 0, worker_id, mock=mock_mode, bind_host="127.0.0.1")
        thread = threading.Thread(target=worker.serve, daemon=True)
        thread.start()
        self.workers.append(worker)
        self.worker = worker
        self.addCleanup(worker.shutdown)
        self.assertTrue(wait_for(lambda: self.mine()), "worker never registered")
        return worker

    def mine(self):
        return [r for r in self.broker.registrations if r["worker_id"] == self.worker.worker_id]

    def send(self, message):
        self.broker.send(message, self.worker.worker_id)

    def next(self, kind, timeout=6.0):
        return self.broker.next_message(kind, self.worker.worker_id, timeout)

    def test_registers_with_its_language_endpoint_and_mode(self):
        worker = self.start_worker("th")
        registration = self.mine()[0]
        self.assertEqual((registration["role"], registration["lang"], registration["worker_id"]),
                         ("worker", "th", worker.worker_id))
        self.assertEqual((registration["host"], registration["port"]), ("127.0.0.1", worker.port))
        self.assertEqual(registration["mode"], "mock")

    def test_real_mode_registers_the_configured_backend(self):
        with mock.patch.dict("os.environ", {"TRANSLATION_BACKEND": "gemini"}):
            self.start_worker("th", mock_mode=False)
        self.assertEqual(self.mine()[0]["mode"], "gemini")

    def test_a_chunk_is_acknowledged_translated_and_returned(self):
        self.start_worker("th")
        self.send({"type": "transcript_chunk", "chunk_id": "c1", "text": "hello", "source_lang": "en"})
        status = self.next("chunk_status")
        self.assertEqual((status["chunk_id"], status["status"], status["lang"]), ("c1", "processing", "th"))
        result = self.next("translated_chunk")
        self.assertEqual(result["text"], "[th] hello")
        self.assertEqual((result["lang"], result["translation_backend"], result["worker_id"]), ("th", "mock", self.worker.worker_id))
        self.assertAlmostEqual(result["timestamp"], time.time(), delta=5)

    def test_a_failed_translation_is_reported_not_swallowed(self):
        worker = self.start_worker("th")
        with mock.patch.object(worker.translator, "translate", side_effect=RuntimeError("boom")):
            self.send({"type": "transcript_chunk", "chunk_id": "c2", "text": "x"})
            deadline = time.monotonic() + 3
            failed = None
            while time.monotonic() < deadline and not failed:
                status = self.next("chunk_status")
                failed = status if status["status"] == "failed" else None
        self.assertEqual(failed["chunk_id"], "c2")
        self.assertIn("inspect worker logs", failed["detail"])

    def test_a_daily_quota_failure_says_so(self):
        worker = self.start_worker("th")
        with mock.patch.object(worker.translator, "translate", side_effect=RuntimeError("RequestsPerDay exceeded")):
            self.send({"type": "transcript_chunk", "chunk_id": "c3", "text": "x"})
            deadline = time.monotonic() + 3
            failed = None
            while time.monotonic() < deadline and not failed:
                status = self.next("chunk_status")
                failed = status if status["status"] == "failed" else None
        self.assertEqual(failed["detail"], "Gemini daily quota exhausted")

    def test_chunk_fields_reach_the_translator(self):
        worker = self.start_worker("th")
        seen = {}

        def fake_translate(text, **kwargs):
            seen.update(text=text, **kwargs)
            return "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35", "libretranslate-pivot"
        with mock.patch.object(worker.translator, "translate", side_effect=fake_translate):
            self.send({"type": "transcript_chunk", "chunk_id": "c4", "text": "\u1019\u1004\u103a\u1039\u1002\u101c\u102c\u1015\u102b",
                              "source_lang": "my", "pivot_en": "Hello", "audio_rate": 16000})
            result = self.next("translated_chunk")
        self.assertEqual(result["translation_backend"], "libretranslate-pivot")
        self.assertEqual((seen["text"], seen["source_lang"], seen["pivot_en"], seen["audio_rate"]),
                         ("\u1019\u1004\u103a\u1039\u1002\u101c\u102c\u1015\u102b", "my", "Hello", 16000))

    def test_translated_chunks_from_the_broker_reach_every_connected_listener(self):
        worker = self.start_worker("th")
        listeners = [socket.create_connection(("127.0.0.1", worker.port), 2) for _ in range(2)]
        self.addCleanup(lambda: [s.close() for s in listeners])
        self.assertTrue(wait_for(lambda: len(worker.listeners) == 2))
        self.send({"type": "translated_chunk", "chunk_id": "c5", "lang": "th", "text": "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35"})
        for sock in listeners:
            sock.settimeout(3)
            self.assertEqual(recv_one(sock)["text"], "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35")

    def test_a_disconnected_listener_is_dropped_without_affecting_the_others(self):
        worker = self.start_worker("th")
        gone = socket.create_connection(("127.0.0.1", worker.port), 2)
        stays = socket.create_connection(("127.0.0.1", worker.port), 2)
        self.addCleanup(stays.close)
        self.assertTrue(wait_for(lambda: len(worker.listeners) == 2))
        gone.close()
        for number in range(3):  # a closed peer only errors after a write or two
            self.send({"type": "translated_chunk", "chunk_id": f"d{number}", "lang": "th", "text": "x"})
            time.sleep(0.05)
        self.assertTrue(wait_for(lambda: len(worker.listeners) == 1))
        stays.settimeout(3)
        self.assertEqual(recv_one(stays)["type"], "translated_chunk")

    def test_worker_re_registers_after_the_broker_connection_drops(self):
        self.start_worker("th")
        self.broker.drop_connections()
        self.assertTrue(wait_for(lambda: len(self.mine()) >= 2), "worker did not reconnect")

    def test_worker_keeps_retrying_while_no_broker_is_reachable(self):
        self.broker.accept_new = False
        worker = self.worker = TranslatorWorker("th", "127.0.0.1", 0, "th-retry", mock=True, bind_host="127.0.0.1")
        threading.Thread(target=worker.serve, daemon=True).start()
        self.addCleanup(worker.shutdown)
        time.sleep(0.4)
        self.assertEqual(self.mine(), [])
        self.broker.accept_new = True
        self.assertTrue(wait_for(lambda: self.mine()), "worker never found the broker")

    def test_heartbeats_are_sent_while_connected(self):
        self.start_worker("th")
        beat = self.next("heartbeat")
        self.assertEqual((beat["sender_id"], beat["role"]), (self.worker.worker_id, "worker"))

    def test_a_full_queue_marks_extra_chunks_failed(self):
        worker = self.start_worker("th")
        release = threading.Event()
        started = threading.Event()

        def blocked(text, **kwargs):
            started.set()
            release.wait(5)
            return "x", "mock"
        worker._work = queue.Queue(maxsize=1)
        self.addCleanup(release.set)
        with mock.patch.object(worker.translator, "translate", side_effect=blocked):
            self.send({"type": "transcript_chunk", "chunk_id": "q0", "text": "a"})
            self.assertTrue(started.wait(3))
            self.send({"type": "transcript_chunk", "chunk_id": "q1", "text": "b"})  # fills the queue
            self.send({"type": "transcript_chunk", "chunk_id": "q2", "text": "c"})  # overflows
            deadline = time.monotonic() + 3
            overflow = None
            while time.monotonic() < deadline and not overflow:
                status = self.next("chunk_status")
                if status["status"] == "failed":
                    overflow = status
        self.assertEqual(overflow["chunk_id"], "q2")
        self.assertIn("queue full", overflow["detail"])

    def test_shutdown_closes_the_listener_endpoint_and_its_connections(self):
        worker = self.start_worker("th")
        client = socket.create_connection(("127.0.0.1", worker.port), 2)
        self.addCleanup(client.close)
        self.assertTrue(wait_for(lambda: len(worker.listeners) == 1))
        worker.shutdown()
        client.settimeout(2)
        self.assertEqual(client.recv(10), b"")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", worker.port), 0.5).close()
            raise OSError  # a freed port that something else grabbed also counts as closed for us
        self.assertEqual(worker.listeners, set())


class HeartbeatLoopTests(unittest.TestCase):
    def test_a_broken_broker_socket_is_shut_down_and_the_loop_exits(self):
        worker = TranslatorWorker("th", "127.0.0.1", 0, "th-hb", mock=True)
        broker = mock.Mock()
        broker.sendall.side_effect = OSError("gone")
        with mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.01):
            thread = threading.Thread(target=worker._heartbeat_loop, args=(broker, threading.Event()), daemon=True)
            thread.start()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        broker.shutdown.assert_called_once()

    def test_stop_event_ends_the_loop_without_sending(self):
        worker = TranslatorWorker("th", "127.0.0.1", 0, "th-hb", mock=True)
        broker, stop = mock.Mock(), threading.Event()
        stop.set()
        worker._heartbeat_loop(broker, stop)
        broker.sendall.assert_not_called()


class TranslationLoopTests(unittest.TestCase):
    def test_chunks_for_a_closed_broker_connection_are_skipped(self):
        worker = TranslatorWorker("th", "127.0.0.1", 0, "th-loop", mock=True)
        closed = mock.Mock()
        closed.fileno.return_value = -1
        worker._work.put((closed, {"chunk_id": "x", "text": "hello"}))
        with mock.patch.object(worker, "_process_chunk") as process:
            thread = threading.Thread(target=worker._translation_loop, daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: worker._work.unfinished_tasks == 0))
            worker.stop_event.set()
            thread.join(2)
        process.assert_not_called()


class CommandLineTests(unittest.TestCase):
    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["translator_worker.py", *argv]), \
                mock.patch.object(translator_worker, "TranslatorWorker") as cls:
            translator_worker.main()
        return cls

    def test_flags_become_constructor_arguments(self):
        cls = self.run_main("--lang", "km", "--host", "worker-km", "--port", "8100", "--worker-id", "km-1", "--mock")
        args = cls.call_args.args
        self.assertEqual(args[:5], ("km", "worker-km", 8100, "km-1", True))
        cls.return_value.serve.assert_called_once()
        cls.return_value.shutdown.assert_called_once()

    def test_worker_id_is_generated_from_the_language_when_omitted(self):
        cls = self.run_main("--lang", "lo")
        self.assertRegex(cls.call_args.args[3], r"^lo-[0-9a-f]{8}$")
        self.assertFalse(cls.call_args.args[4])

    def test_unknown_language_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            self.run_main("--lang", "xx")

    def test_ctrl_c_still_shuts_the_worker_down(self):
        with mock.patch.object(sys, "argv", ["translator_worker.py", "--lang", "th", "--mock"]), \
                mock.patch.object(translator_worker, "TranslatorWorker") as cls:
            cls.return_value.serve.side_effect = KeyboardInterrupt
            translator_worker.main()
        cls.return_value.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
