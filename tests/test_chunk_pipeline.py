import pathlib
import unittest

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)
from unittest import mock

from broker import Broker, WorkerConnection
from stt_service import transcribe_file
from web_ui import DashboardRuntime
from translator_worker import TranslatorWorker


class ChunkPipelineTests(unittest.TestCase):
    def test_first_window_is_yielded_before_later_transcription(self):
        whisper = mock.Mock()
        whisper.load_audio.return_value = list(range(35))
        whisper.audio.SAMPLE_RATE = 1
        model = mock.Mock()
        model.transcribe.return_value = {"language": "th", "segments": [{"text": "hello"}]}
        with mock.patch("stt_service._load_whisper", return_value=(whisper, model)), \
             mock.patch("stt_service._pcm16", return_value=b"pcm"), \
             mock.patch.dict("os.environ", {"POLYGLOT_FILE_CHUNK_SECONDS": "15"}):
            chunks = transcribe_file(pathlib.Path("input.mp4"), "base", "th")
            self.assertEqual(next(chunks), ("hello", b"pcm", "th"))
            self.assertEqual(model.transcribe.call_count, 1)
            self.assertEqual(len(list(chunks)), 2)
            self.assertEqual([len(c.args[0]) for c in model.transcribe.call_args_list], [15, 15, 5])

    def test_upload_dispatches_before_generator_finishes(self):
        runtime = DashboardRuntime()
        def chunks(*args, **kwargs):
            yield "first", b"", "en"
            self.assertEqual(runtime.state.pending, 1)
            yield "second", b"", "en"
        with mock.patch("web_ui.transcribe_file", side_effect=chunks):
            self.assertEqual(runtime.submit_audio(b"media", "en"), (2, None))

    def test_source_bypasses_translation_and_other_eight_are_routed(self):
        from common.config import TARGET_LANGUAGES
        broker = Broker(1)
        sockets = {}
        for lang in TARGET_LANGUAGES:
            sockets[lang] = mock.Mock()
            broker.registry.add(WorkerConnection(lang, lang, "localhost", 1, sockets[lang]))
        broker.route_chunk({"type": "transcript_chunk", "chunk_id": "one", "text": "hello", "source_lang": "my"})
        import json
        for lang, sock in sockets.items():
            message = json.loads(sock.sendall.call_args.args[0])
            self.assertEqual(message["type"], "translated_chunk" if lang == "my" else "transcript_chunk")
        states = broker.telemetry()["jobs"]
        self.assertEqual(sum(s["status"] == "queued" for s in states), 8)
        self.assertEqual(sum(s["status"] == "source" for s in states), 1)

    def test_failure_is_reported_to_broker(self):
        worker = TranslatorWorker("th", "localhost", 0, "th-1", mock=True)
        with mock.patch.object(worker.translator, "translate", side_effect=RuntimeError("bad")), \
             mock.patch.object(worker, "_send_broker") as send:
            worker._process_chunk(mock.Mock(), {"chunk_id": "one", "text": "hello"})
        self.assertEqual(send.call_args.args[1]["status"], "failed")

    def test_leader_change_marks_unfinished_work_interrupted(self):
        runtime = DashboardRuntime()
        runtime.state.record_original("one", "hello", "en")
        with mock.patch.object(runtime, "broker_status", return_value=[{"id": 2, "online": True, "leader": True}]):
            runtime.status()
        with mock.patch.object(runtime, "broker_status", return_value=[{"id": 1, "online": True, "leader": True}]):
            status = runtime.status()
        self.assertEqual(status["chunks"][0]["targets"]["th"]["status"], "interrupted")
        self.assertEqual(status["chunks"][0]["targets"]["en"]["status"], "source")

    def test_async_upload_reports_failure_and_releases_upload_lock(self):
        runtime = DashboardRuntime()
        with mock.patch.object(runtime, "submit_audio", side_effect=ValueError("No speech detected")):
            runtime.start_audio(b"media", "en")
            self.assertTrue(runtime._media_lock.acquire(timeout=2))
        runtime._media_lock.release()
        self.assertEqual(runtime.state.media["status"], "failed")


if __name__ == "__main__":
    unittest.main()
