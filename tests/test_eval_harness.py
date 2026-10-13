"""The evaluation harness: metrics (checked against sacrebleu's published values), the
manifest-driven speech and translation runner, and the failover trial tool."""

import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
import wave
from unittest import mock

from eval import failover_trials, fleurs_eval, metrics, summarize_results

REFS = ["The meeting starts now in the main hall.", "Welcome everyone, thank you.",
        "If you have questions, please raise your hand and ask.", "There will be a five minute break later."]
HYPS = ["The meeting begins now in the main hall!", "Welcome all, thanks.",
        "Raise your hand if you have a question.", "There will be a break of five minutes later."]


class NormalizeTests(unittest.TestCase):
    def test_punctuation_case_and_spacing_are_removed(self):
        self.assertEqual(metrics.normalize("  Hello,   WORLD!! "), "hello world")

    def test_burmese_khmer_and_lao_punctuation_is_removed_too(self):
        self.assertEqual(metrics.normalize("\u1019\u1004\u103a\u1039\u1002\u101c\u102c\u1015\u102b\u104a \u1012\u102e\u1014\u1031\u1037\u104b"),
                         "\u1019\u1004\u103a\u1039\u1002\u101c\u102c\u1015\u102b \u1012\u102e\u1014\u1031\u1037")
        self.assertEqual(metrics.normalize("\u1780\u17d4\u17d5"), "\u1780")

    def test_unicode_forms_are_unified(self):
        self.assertEqual(metrics.normalize("e\u0301"), metrics.normalize("\xe9"))

    def test_without_spaces_for_character_scoring(self):
        self.assertEqual(metrics.normalize("a b  c", keep_spaces=False), "abc")


class ErrorRateTests(unittest.TestCase):
    def test_character_errors(self):
        self.assertEqual(metrics.cer_counts("kitten", "sitting"), (3, 6))
        self.assertEqual(metrics.cer_counts("abc", "abc"), (0, 3))

    def test_spaces_and_punctuation_do_not_count_as_character_errors(self):
        self.assertEqual(metrics.cer_counts("hello, world", "helloworld")[0], 0)

    def test_an_empty_hypothesis_is_a_total_miss(self):
        self.assertEqual(metrics.cer_counts("abcd", ""), (4, 4))
        self.assertEqual(metrics.wer_counts("one two three", ""), (3, 3))

    def test_word_errors(self):
        self.assertEqual(metrics.wer_counts("one two three", "one three"), (1, 3))

    def test_rate_is_corpus_level_not_an_average_of_averages(self):
        # one long perfect utterance and one short bad one: 2 errors in 102 characters
        self.assertAlmostEqual(metrics.rate([(0, 100), (2, 2)]), 2 / 102)
        self.assertEqual(metrics.rate([]), 0.0)

    def test_bootstrap_interval_is_reproducible_and_brackets_the_estimate(self):
        counts = [(i % 4, 20) for i in range(60)]
        first = metrics.bootstrap_ci(counts, metrics.rate, 500)
        self.assertEqual(first, metrics.bootstrap_ci(counts, metrics.rate, 500))
        self.assertLessEqual(first[0], metrics.rate(counts))
        self.assertGreaterEqual(first[1], metrics.rate(counts))

    def test_more_data_gives_a_tighter_interval(self):
        small = metrics.bootstrap_ci([(i % 4, 20) for i in range(10)], metrics.rate, 500)
        large = metrics.bootstrap_ci([(i % 4, 20) for i in range(400)], metrics.rate, 500)
        self.assertLess(large[1] - large[0], small[1] - small[0])

    def test_bootstrap_of_nothing_is_zero(self):
        self.assertEqual(metrics.bootstrap_ci([], metrics.rate), (0.0, 0.0))


class ChrfTests(unittest.TestCase):
    """Golden values come from sacrebleu 2.x (CHRF(word_order=2) and CHRF())."""

    def test_corpus_chrf_plus_plus_matches_sacrebleu(self):
        self.assertAlmostEqual(metrics.chrf(REFS, HYPS), 57.3448, places=3)

    def test_plain_chrf_matches_sacrebleu(self):
        self.assertAlmostEqual(metrics.chrf(REFS, HYPS, word_order=0), 59.787, places=3)

    def test_single_sentence_matches_sacrebleu(self):
        self.assertAlmostEqual(metrics.chrf([REFS[1]], [HYPS[1]]), 34.6513, places=3)

    def test_a_hypothesis_longer_than_its_reference_follows_sacrebleus_rule(self):
        self.assertAlmostEqual(metrics.chrf(["one"], ["one two three"]), 59.7998, places=3)

    def test_identical_text_scores_100_and_unrelated_text_near_zero(self):
        self.assertAlmostEqual(metrics.chrf(["same text"], ["same text"]), 100.0)
        self.assertLess(metrics.chrf(["aaaa bbbb"], ["xyzw qrst"]), 1.0)

    def test_empty_inputs(self):
        self.assertEqual(metrics.chrf([], []), 0.0)
        self.assertEqual(metrics.chrf(["something"], [""]), 0.0)

    def test_scripts_without_spaces_are_scored_on_characters(self):
        thai = "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\u0e17\u0e38\u0e01\u0e04\u0e19"
        self.assertAlmostEqual(metrics.chrf([thai], [thai]), 100.0)
        self.assertGreater(metrics.chrf([thai], [thai[:-2]]), 40.0)

    def test_corpus_score_is_not_the_mean_of_sentence_scores(self):
        corpus = metrics.chrf(REFS, HYPS)
        mean = sum(metrics.chrf([r], [h]) for r, h in zip(REFS, HYPS)) / len(REFS)
        self.assertNotAlmostEqual(corpus, mean, places=2)


def write_wav(path, seconds=1.0, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(seconds * rate))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def manifest(self, rows):
        path = self.dir / "manifest.jsonl"
        path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        return path

    def row(self, n, lang="my", **extra):
        return {"id": f"{lang}-{n}", "lang": lang, "audio": f"{lang}/{n}.wav", "transcript": f"text {n}", **extra}

    def test_paths_resolve_relative_to_the_manifest(self):
        items = fleurs_eval.load_manifest(self.manifest([self.row(1)]))
        self.assertEqual(pathlib.Path(items[0]["audio"]), (self.dir / "my" / "1.wav").resolve())

    def test_absolute_paths_are_kept(self):
        absolute = str(self.dir / "x.wav")
        items = fleurs_eval.load_manifest(self.manifest([{**self.row(1), "audio": absolute}]))
        self.assertEqual(items[0]["audio"], absolute)

    def test_filters_by_language_and_limits_per_language(self):
        rows = [self.row(n, lang) for lang in ("my", "km") for n in range(5)]
        path = self.manifest(rows)
        self.assertEqual({i["lang"] for i in fleurs_eval.load_manifest(path, ["km"])}, {"km"})
        limited = fleurs_eval.load_manifest(path, limit_per_language=2)
        self.assertEqual(sorted(i["lang"] for i in limited), ["km", "km", "my", "my"])

    def test_blank_lines_are_ignored(self):
        self.assertEqual(len(fleurs_eval.load_manifest(self.manifest([self.row(1), "", "  ", self.row(2)]))), 2)

    def test_bad_manifests_are_rejected_with_the_line_number(self):
        for rows, fragment in (([self.row(1), "{not json"], ":2:"),
                               ([{**self.row(1), "transcript": ""}], "transcript"),
                               ([{k: v for k, v in self.row(1).items() if k != "audio"}], "audio"),
                               ([self.row(1), self.row(1)], "duplicate"),
                               ([], "no usable")):
            with self.assertRaisesRegex(ValueError, fragment):
                fleurs_eval.load_manifest(self.manifest(rows))

    def test_wav_duration_of_the_32_bit_float_files_fleurs_ships(self):
        import struct
        rate, seconds = 16000, 2
        data = b"\x00\x00\x00\x00" * rate * seconds
        header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 3, 1, rate, rate * 4, 4, 32)
                  + b"data" + struct.pack("<I", len(data)))
        (self.dir / "float.wav").write_bytes(header + data)
        self.assertAlmostEqual(fleurs_eval.wav_seconds(str(self.dir / "float.wav")), 2.0)

    def test_wav_duration_skips_extra_chunks_and_tolerates_a_streamed_size(self):
        import struct
        data = b"\x01\x00" * 8000
        body = (b"WAVE" + b"LIST" + struct.pack("<I", 5) + b"abcde\x00"      # odd-sized chunk is padded
                + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                + b"data" + struct.pack("<I", 0xFFFFFFFF) + data)
        (self.dir / "streamed.wav").write_bytes(b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + body)
        self.assertAlmostEqual(fleurs_eval.wav_seconds(str(self.dir / "streamed.wav")), 0.5)

    def test_wav_duration_of_garbage_and_truncated_headers_is_none(self):
        (self.dir / "short.wav").write_bytes(b"RIFF\x00\x00")
        (self.dir / "nofmt.wav").write_bytes(b"RIFF\x00\x00\x00WAVEdata\x00\x00\x00abcd")
        for name in ("short.wav", "nofmt.wav"):
            self.assertIsNone(fleurs_eval.wav_seconds(str(self.dir / name)), name)

    def test_wav_duration(self):
        write_wav(self.dir / "a.wav", 2.5)
        self.assertAlmostEqual(fleurs_eval.wav_seconds(str(self.dir / "a.wav")), 2.5)
        self.assertIsNone(fleurs_eval.wav_seconds(str(self.dir / "missing.wav")))
        (self.dir / "b.mp3").write_bytes(b"not a wav")
        self.assertIsNone(fleurs_eval.wav_seconds(str(self.dir / "b.mp3")))


class AsrRunTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def items(self, spec):
        items = []
        for n, (lang, transcript) in enumerate(spec):
            audio = self.dir / f"{n}.wav"
            write_wav(audio, 2.0)
            items.append({"id": f"{lang}-{n}", "lang": lang, "audio": str(audio), "transcript": transcript})
        return items

    def test_a_perfect_transcriber_scores_zero_error(self):
        items = self.items([("th", "hello there"), ("th", "good morning")])
        said = {i["audio"]: i["transcript"] for i in items}
        results = fleurs_eval.run_asr(items, lambda path, lang: (said[path], 0))
        summary = fleurs_eval.summarize_asr(results)["th"]
        self.assertEqual((summary["cer_percent"], summary["wer_percent"], summary["empty_outputs"]), (0.0, 0.0, 0))
        self.assertEqual(summary["utterances"], 2)

    def test_errors_are_counted_and_scored_per_language(self):
        items = self.items([("my", "abcd"), ("km", "abcd")])
        outputs = {items[0]["audio"]: "abcd", items[1]["audio"]: "abxx"}
        summary = fleurs_eval.summarize_asr(fleurs_eval.run_asr(items, lambda p, l: (outputs[p], 0)))
        self.assertEqual((summary["my"]["cer_percent"], summary["km"]["cer_percent"]), (0.0, 50.0))

    def test_a_crash_or_empty_output_counts_as_a_total_failure_not_a_skip(self):
        items = self.items([("my", "abcd"), ("my", "efgh")])

        def flaky(path, lang):
            if path == items[0]["audio"]:
                raise RuntimeError("quota")
            return "", 2
        results = fleurs_eval.run_asr(items, flaky)
        summary = fleurs_eval.summarize_asr(results)["my"]
        self.assertEqual(summary["cer_percent"], 100.0)
        self.assertEqual((summary["empty_outputs"], summary["errors"], summary["unrecognised_windows"]), (2, 1, 2))
        self.assertIn("RuntimeError: quota", results[0]["error"])

    def test_real_time_factor_is_processing_time_over_audio_time(self):
        items = self.items([("th", "hi")])
        clock = iter([100.0, 101.0])   # 1 s to process 2 s of audio
        results = fleurs_eval.run_asr(items, lambda p, l: ("hi", 0), clock=lambda: next(clock))
        self.assertAlmostEqual(fleurs_eval.summarize_asr(results)["th"]["real_time_factor"], 0.5)
        self.assertEqual(fleurs_eval.summarize_asr(results)["th"]["median_seconds"], 1.0)

    def test_confidence_intervals_bracket_the_estimate(self):
        items = self.items([("th", "abcdefgh")] * 1)
        items = [dict(i, id=f"th-{n}") for n, i in enumerate(items * 20)]
        outputs = iter(["abcdefgh" if n % 3 else "abcdxxxx" for n in range(20)])
        summary = fleurs_eval.summarize_asr(fleurs_eval.run_asr(items, lambda p, l: (next(outputs), 0)))["th"]
        low, high = summary["cer_ci95"]
        self.assertLessEqual(low, summary["cer_percent"])
        self.assertGreaterEqual(high, summary["cer_percent"])

    def test_default_transcriber_collects_windows_and_gaps(self):
        def fake_transcribe_file(path, model, language=None, on_unrecognized=None):
            on_unrecognized()
            yield "first", b"", language
            yield "second", b"", language
        with mock.patch("stt_service.transcribe_file", fake_transcribe_file):
            text, gaps = fleurs_eval.default_transcriber("tiny")("clip.wav", "km")
        self.assertEqual((text, gaps), ("first second", 1))


class MtRunTests(unittest.TestCase):
    ITEMS = [{"id": "my-1", "lang": "my", "audio": "a", "transcript": "mingalaba", "english": "Hello there everyone."},
             {"id": "my-2", "lang": "my", "audio": "b", "transcript": "kyezutinbade", "english": "Thank you very much."},
             {"id": "my-3", "lang": "my", "audio": "c", "transcript": "no reference here"}]

    def test_clips_without_a_human_translation_are_left_out(self):
        results = fleurs_eval.run_mt(self.ITEMS, lambda text, lang: "x")
        self.assertEqual([r["id"] for r in results], ["my-1", "my-2"])

    def test_a_perfect_translator_scores_100(self):
        answers = {"mingalaba": "Hello there everyone.", "kyezutinbade": "Thank you very much."}
        summary = fleurs_eval.summarize_mt(fleurs_eval.run_mt(self.ITEMS, lambda text, lang: answers[text]))
        self.assertEqual(summary["my"]["chrf_pp"], 100.0)
        self.assertEqual(summary["my"]["sentences"], 2)

    def test_end_to_end_uses_the_speech_recognition_output(self):
        seen = []
        fleurs_eval.run_mt(self.ITEMS, lambda text, lang: seen.append((text, lang)) or "x", {"my-1": "heard one", "my-2": "heard two"})
        self.assertEqual(seen, [("heard one", "my"), ("heard two", "my")])

    def test_a_clip_with_no_recognised_text_scores_as_a_failure(self):
        results = fleurs_eval.run_mt(self.ITEMS, lambda text, lang: "translated", {"my-1": "heard", "my-2": "   "})
        summary = fleurs_eval.summarize_mt(results)["my"]
        self.assertEqual(summary["empty_outputs"], 1)
        self.assertLess(summary["chrf_pp"], 100.0)

    def test_translator_exceptions_are_recorded_and_scored_as_empty(self):
        def broken(text, lang):
            raise RuntimeError("429")
        summary = fleurs_eval.summarize_mt(fleurs_eval.run_mt(self.ITEMS, broken))["my"]
        self.assertEqual((summary["errors"], summary["empty_outputs"], summary["chrf_pp"]), (2, 2, 0.0))

    def test_default_translator_returns_just_the_text(self):
        with mock.patch("translator_worker.Translator") as cls:
            cls.return_value.translate.return_value = ("Hello", "gemini-text")
            self.assertEqual(fleurs_eval.default_translator("en")("\u1019", "my"), "Hello")
        cls.assert_called_once_with("en")
        cls.return_value.translate.assert_called_once_with("\u1019", source_lang="my")


class ReportTests(unittest.TestCase):
    def test_markdown_table_shows_intervals_and_dashes_for_missing_values(self):
        summary = {"my": {"utterances": 10, "cer_percent": 4.5, "cer_ci95": [3.1, 6.0], "wer_percent": 9.0,
                          "wer_ci95": [7.0, 11.0], "empty_outputs": 0, "median_seconds": 1.2, "real_time_factor": None}}
        table = fleurs_eval.markdown_table(summary, fleurs_eval.ASR_COLUMNS)
        lines = table.splitlines()
        self.assertEqual(lines[0], "| Language | n | CER % | WER % | empty | median s | RTF |")
        self.assertEqual(lines[2], "| my | 10 | 4.5 (3.1-6.0) | 9.0 (7.0-11.0) | 0 | 1.2 | - |")

    def test_command_line_writes_results_and_prints_the_table(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        write_wav(tmp / "a.wav", 1.0)
        manifest = tmp / "m.jsonl"
        manifest.write_text(json.dumps({"id": "th-1", "lang": "th", "audio": "a.wav", "transcript": "abcd",
                                        "english": "Hello."}) + "\n", encoding="utf-8")
        out = tmp / "out" / "asr.json"
        with mock.patch.object(fleurs_eval, "default_transcriber", return_value=lambda p, l: ("abcd", 0)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(fleurs_eval.main(["asr", "--manifest", str(manifest), "--out", str(out)]), 0)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(saved["summary"]["th"]["cer_percent"], 0.0)
        self.assertIn("| th |", stdout.getvalue())
        mt_out = tmp / "out" / "mt.json"
        with mock.patch.object(fleurs_eval, "default_translator", return_value=lambda t, l: "Hello."), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            fleurs_eval.main(["mt", "--manifest", str(manifest), "--out", str(mt_out), "--from-asr", str(out)])
        self.assertEqual(json.loads(mt_out.read_text(encoding="utf-8"))["summary"]["th"]["chrf_pp"], 100.0)


class SummaryTableTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def write(self, name, lang, summary):
        (self.dir / name).write_text(json.dumps({"summary": {lang: summary}}), encoding="utf-8")

    def asr(self, lang, **extra):
        base = {"utterances": 150, "cer_percent": 4.0, "cer_ci95": [3.0, 5.0], "wer_percent": 9.5, "wer_ci95": [8.0, 11.0],
                "empty_outputs": 1, "median_seconds": 2.5, "real_time_factor": 0.2}
        self.write(f"asr_{lang}.json", lang, {**base, **extra})

    def test_languages_come_out_in_a_fixed_order_and_missing_ones_are_left_out(self):
        self.asr("lo")
        self.asr("en")
        self.assertEqual([r["lang"] for r in summarize_results.build_rows(self.dir)], ["en", "lo"])

    def test_a_row_combines_recognition_and_both_translation_scores(self):
        self.asr("id")
        self.write("mt_ref_id.json", "id", {"chrf_pp": 68.4, "chrf_pp_ci95": [66.3, 70.4]})
        self.write("mt_e2e_id.json", "id", {"chrf_pp": 60.1, "chrf_pp_ci95": [58.0, 62.2]})
        line = summarize_results.markdown(summarize_results.build_rows(self.dir)).splitlines()[2]
        self.assertEqual(line, "| id | 150 | 4.0 (3.0-5.0) | 9.5 (8.0-11.0) | 1 | 2.5 | 0.2 | 68.4 (66.3-70.4) | 60.1 (58.0-62.2) |")

    def test_word_error_rate_is_not_reported_for_scripts_without_word_spaces(self):
        for lang in ("my", "th", "km", "lo"):
            self.asr(lang)
        for line in summarize_results.markdown(summarize_results.build_rows(self.dir)).splitlines()[2:]:
            self.assertIn("| n/a |", line)

    def test_missing_parts_show_as_dashes(self):
        self.write("mt_ref_vi.json", "vi", {"chrf_pp": 62.0, "chrf_pp_ci95": [60.0, 64.0]})
        line = summarize_results.markdown(summarize_results.build_rows(self.dir)).splitlines()[2]
        self.assertEqual(line, "| vi | - | - | - | - | - | - | 62.0 (60.0-64.0) | - |")

    def test_command_line_writes_the_table(self):
        self.asr("en")
        out = self.dir / "report.md"
        with mock.patch("builtins.print"):
            self.assertEqual(summarize_results.main(["--results", str(self.dir), "--out", str(out)]), 0)
        self.assertIn("| en | 150 |", out.read_text(encoding="utf-8"))

    def test_an_empty_directory_gives_just_the_header(self):
        self.assertEqual(len(summarize_results.markdown(summarize_results.build_rows(self.dir)).splitlines()), 2)


class FailoverToolTests(unittest.TestCase):
    def test_summary_statistics(self):
        summary = failover_trials.summarize([7.0, 8.0, 9.0, 10.0, 11.0])
        self.assertEqual((summary["trials"], summary["mean_seconds"], summary["median_seconds"]), (5, 9.0, 9.0))
        self.assertEqual((summary["min_seconds"], summary["max_seconds"]), (7.0, 11.0))
        self.assertLessEqual(summary["mean_ci95"][0], 9.0)
        self.assertGreaterEqual(summary["mean_ci95"][1], 9.0)

    def test_summary_of_one_or_no_trials(self):
        self.assertEqual(failover_trials.summarize([]), {"trials": 0})
        self.assertEqual(failover_trials.summarize([5.0])["stdev_seconds"], 0.0)

    def test_settings_report_the_values_the_brokers_will_use(self):
        settings = failover_trials.timing_settings({"POLYGLOT_HEARTBEAT_TIMEOUT": "9"})
        self.assertEqual(settings["POLYGLOT_HEARTBEAT_TIMEOUT"], 9.0)
        self.assertEqual(set(settings), set(failover_trials.TIMING_VARIABLES))

    def test_wait_until_reports_how_long_it_waited_or_gives_up(self):
        self.assertLess(failover_trials.wait_until(lambda: True, 1), 0.2)
        self.assertIsNone(failover_trials.wait_until(lambda: False, 0.1))

    def test_heartbeat_of_a_closed_port_is_none(self):
        self.assertIsNone(failover_trials.heartbeat(failover_trials.free_port(), timeout=0.2))

    def test_a_real_trial_kills_the_leader_and_measures_recovery(self):
        fast = {"POLYGLOT_HEARTBEAT_INTERVAL": "0.2", "POLYGLOT_HEARTBEAT_TIMEOUT": "0.6",
                "POLYGLOT_ELECTION_TIMEOUT": "0.2", "POLYGLOT_CONNECT_TIMEOUT": "0.2"}
        seconds = failover_trials.run_trial(fast, settle_timeout=15, recovery_timeout=15)
        self.assertGreater(seconds, 0.3)    # cannot beat the heartbeat timeout
        self.assertLess(seconds, 8.0)

    def test_command_line_runs_the_requested_number_of_trials(self):
        out = pathlib.Path(tempfile.mkdtemp()) / "failover.json"
        with mock.patch.object(failover_trials, "run_trial", side_effect=[7.5, 8.5, 9.5]) as run, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            failover_trials.main(["--trials", "3", "--out", str(out)])
        self.assertEqual(run.call_count, 3)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(saved["recovery_seconds"], [7.5, 8.5, 9.5])
        self.assertEqual(saved["summary"]["median_seconds"], 8.5)
        self.assertIn("POLYGLOT_HEARTBEAT_TIMEOUT", saved["settings"])


if __name__ == "__main__":
    unittest.main()
