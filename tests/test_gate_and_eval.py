"""The Gemini rate gate (limiter + HTTP service) and the evaluation metrics."""

import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import gemini_rate_gate
from common import config
from eval import evaluate as ev
from gemini_rate_gate import GateHandler, SlidingWindowLimiter


class LimiterEdgeTests(unittest.TestCase):
    def test_rejects_a_non_positive_limit_or_window(self):
        for limit, window in ((0, 1.0), (-1, 1.0), (1, 0), (1, -5.0)):
            with self.assertRaises(ValueError, msg=(limit, window)):
                SlidingWindowLimiter(limit, window)

    def test_reports_how_many_slots_are_in_use(self):
        limiter = SlidingWindowLimiter(3, 5.0)
        self.assertEqual([limiter.acquire()[1] for _ in range(3)], [1, 2, 3])

    def test_callers_are_served_in_arrival_order(self):
        limiter = SlidingWindowLimiter(1, 0.15)
        limiter.acquire()
        order, lock = [], threading.Lock()

        def caller(name):
            limiter.acquire()
            with lock:
                order.append(name)
        threads = []
        for name in "abcd":
            thread = threading.Thread(target=caller, args=(name,))
            thread.start()
            threads.append(thread)
            time.sleep(0.03)   # make arrival order unambiguous
        for thread in threads:
            thread.join(5)
        self.assertEqual(order, list("abcd"))

    def test_old_slots_free_up_after_the_window(self):
        limiter = SlidingWindowLimiter(2, 0.2)
        limiter.acquire()
        limiter.acquire()
        time.sleep(0.25)
        waited, in_window = limiter.acquire()
        self.assertEqual((waited, in_window), (0.0, 1))


class GateServiceTests(unittest.TestCase):
    def setUp(self):
        GateHandler.limiter = SlidingWindowLimiter(1, 0.4)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GateHandler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def status(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_health_and_unknown_paths(self):
        self.assertEqual(self.status("/health"), 200)
        self.assertEqual(self.status("/nope"), 404)

    def test_acquire_answers_200_immediately_while_a_slot_is_free(self):
        start = time.monotonic()
        self.assertEqual(self.status("/acquire"), 200)
        self.assertLess(time.monotonic() - start, 0.3)

    def test_acquire_holds_the_request_open_until_a_slot_frees_up(self):
        self.status("/acquire")
        start = time.monotonic()
        self.assertEqual(self.status("/acquire"), 200)
        self.assertGreaterEqual(time.monotonic() - start, 0.3)

    def test_acquire_with_a_query_string_still_counts_as_acquire(self):
        self.assertEqual(self.status("/acquire?worker=th"), 200)


class GateCommandLineTests(unittest.TestCase):
    def run_main(self, *argv, env=None):
        server = mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(sys, "argv", ["gemini_rate_gate.py", *argv]), \
                mock.patch.dict("os.environ", env or {}), \
                mock.patch.object(gemini_rate_gate, "ThreadingHTTPServer", return_value=server) as cls:
            gemini_rate_gate.main()
        return cls, server

    def test_defaults_to_five_requests_a_minute_on_port_8090(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GEMINI_RATE_LIMIT_PER_MINUTE", None)
            cls, server = self.run_main()
        self.assertEqual(cls.call_args.args[0], ("0.0.0.0", 8090))
        self.assertEqual(GateHandler.limiter.limit, 5)
        server.shutdown.assert_called_once()

    def test_limit_comes_from_the_flag_or_the_environment(self):
        self.run_main("--limit-per-minute", "12", "--port", "9000")
        self.assertEqual(GateHandler.limiter.limit, 12)
        self.run_main(env={"GEMINI_RATE_LIMIT_PER_MINUTE": "7"})
        self.assertEqual(GateHandler.limiter.limit, 7)


class EditDistanceTests(unittest.TestCase):
    def test_basic_operations(self):
        self.assertEqual(ev.edit_distance(list("kitten"), list("sitting")), 3)
        self.assertEqual(ev.edit_distance(["a", "b"], ["a", "b"]), 0)
        self.assertEqual(ev.edit_distance(["a"], ["a", "b", "c"]), 2)   # insertions
        self.assertEqual(ev.edit_distance(["a", "b", "c"], ["a"]), 2)   # deletions
        self.assertEqual(ev.edit_distance(["a"], ["b"]), 1)             # substitution

    def test_empty_sides(self):
        self.assertEqual(ev.edit_distance([], ["a", "b"]), 2)
        self.assertEqual(ev.edit_distance(["a", "b"], []), 2)
        self.assertEqual(ev.edit_distance([], []), 0)


class WerTests(unittest.TestCase):
    def test_counts_errors_across_all_pairs_over_total_reference_words(self):
        pairs = [{"reference": "a b c d", "hypothesis": "a b c d"},      # 0 errors / 4
                 {"reference": "one two", "hypothesis": "one"}]          # 1 error / 2
        self.assertAlmostEqual(ev.wer(pairs), 1 / 6)

    def test_is_case_insensitive(self):
        self.assertEqual(ev.wer([{"reference": "Hello World", "hypothesis": "hello world"}]), 0.0)

    def test_no_data_is_zero_not_a_crash(self):
        self.assertEqual(ev.wer([]), 0.0)

    def test_can_exceed_one_when_the_hypothesis_is_much_longer(self):
        self.assertGreater(ev.wer([{"reference": "a", "hypothesis": "x y z"}]), 1.0)


class BleuTests(unittest.TestCase):
    def pair(self, reference, hypothesis):
        return [{"reference": reference, "hypothesis": hypothesis}]

    def test_tokens_split_on_spaces_or_fall_back_to_characters(self):
        self.assertEqual(ev._tokens("a b c"), ["a", "b", "c"])
        self.assertEqual(ev._tokens("\u0e2a\u0e27\u0e31\u0e2a"), list("\u0e2a\u0e27\u0e31\u0e2a"))  # unsegmented script
        self.assertEqual(ev._tokens("  hello  "), list("hello"))

    def test_no_output_scores_zero(self):
        self.assertEqual(ev.corpus_bleu(self.pair("some reference text here", "")), 0.0)
        self.assertEqual(ev.corpus_bleu([]), 0.0)

    def test_a_better_translation_scores_higher(self):
        ref = "the meeting starts now in the main hall"
        good = ev.corpus_bleu(self.pair(ref, "the meeting starts now in the hall"))
        bad = ev.corpus_bleu(self.pair(ref, "a gathering begins soon somewhere else"))
        self.assertGreater(good, bad)
        self.assertTrue(0 <= bad < good <= 100)

    def test_a_too_short_hypothesis_is_penalised(self):
        ref = "one two three four five six seven eight"
        full = ev.corpus_bleu(self.pair(ref, ref))
        short = ev.corpus_bleu(self.pair(ref, "one two three four"))
        self.assertLess(short, full)

    def test_scores_all_pairs_together(self):
        pairs = self.pair("a b c d e", "a b c d e") + self.pair("v w x y z", "q q q q q")
        self.assertLess(ev.corpus_bleu(pairs), 100.0)
        self.assertGreater(ev.corpus_bleu(pairs), 0.0)


class TimingTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: pathlib.Path(handle.name).unlink())
        return pathlib.Path(handle.name)

    def test_recovery_is_the_gap_between_failure_and_recovered(self):
        path = self.write('{"scenario":"leader_failure","event":"failure","timestamp":10.0}\n'
                          '{"scenario":"leader_failure","event":"recovered","timestamp":17.25}\n')
        self.assertEqual(ev.parse_timings(path), {"leader_failure": {"recovery_seconds": 7.25}})

    def test_plain_text_log_lines_are_understood_too(self):
        path = self.write("scenario=worker_failure event=failure timestamp=3.0\n"
                          "scenario=worker_failure event=recovered timestamp=4.5\n")
        self.assertEqual(ev.parse_timings(path)["worker_failure"]["recovery_seconds"], 1.5)

    def test_incomplete_scenarios_and_junk_lines_are_ignored(self):
        path = self.write('{"scenario":"a","event":"failure","timestamp":1}\n'
                          'not a log line at all\n'
                          '{"scenario":"b","event":"recovered","timestamp":"soon"}\n'
                          '{"event":"failure"}\n')
        self.assertEqual(ev.parse_timings(path), {})

    def test_missing_file_or_no_path_gives_nothing(self):
        self.assertEqual(ev.parse_timings(None), {})
        self.assertEqual(ev.parse_timings(pathlib.Path("does-not-exist.jsonl")), {})

    def test_several_scenarios_are_reported_separately(self):
        path = self.write("\n".join(json.dumps({"scenario": s, "event": e, "timestamp": t})
                                    for s, e, t in (("x", "failure", 0), ("x", "recovered", 2),
                                                    ("y", "failure", 5), ("y", "recovered", 11))))
        self.assertEqual({k: v["recovery_seconds"] for k, v in ev.parse_timings(path).items()}, {"x": 2.0, "y": 6.0})


class EvaluateTests(unittest.TestCase):
    def benchmark(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, handle)
        handle.close()
        self.addCleanup(lambda: pathlib.Path(handle.name).unlink())
        return pathlib.Path(handle.name)

    def test_summarises_wer_bleu_per_language_and_recovery(self):
        path = self.benchmark({
            "transcription": [{"reference": "a b", "hypothesis": "a b"}],
            "translation": {"th": [{"reference": "a b c d e", "hypothesis": "a b c d e"}]},
        })
        result = ev.evaluate(path)
        self.assertEqual(result["wer"], 0.0)
        self.assertEqual(result["bleu"], {"th": 100.0})
        self.assertEqual(result["failure_recovery"], {})

    def test_missing_sections_are_empty_not_errors(self):
        self.assertEqual(ev.evaluate(self.benchmark({})), {"wer": 0.0, "bleu": {}, "failure_recovery": {}})

    def test_command_line_writes_results_json(self):
        path = self.benchmark({"transcription": [{"reference": "a b", "hypothesis": "a"}]})
        out = pathlib.Path(tempfile.mkdtemp()) / "results.json"
        with mock.patch.object(sys, "argv", ["evaluate.py", "--benchmark", str(path), "--output", str(out)]), \
                mock.patch("builtins.print"):
            ev.main()
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["wer"], 0.5)
        out.unlink()

    def test_the_shipped_benchmark_only_covers_languages_the_system_supports(self):
        data = json.loads((ev.HERE / "benchmark_sentences.json").read_text(encoding="utf-8"))
        self.assertTrue(set(data["translation"]) <= set(config.LANGUAGES), set(data["translation"]))
        result = ev.evaluate(ev.HERE / "benchmark_sentences.json")
        self.assertTrue(0.0 <= result["wer"] < 0.5)   # a sample transcript with a few deliberate slips
        self.assertTrue(all(score == 100.0 for score in result["bleu"].values()))


if __name__ == "__main__":
    unittest.main()
